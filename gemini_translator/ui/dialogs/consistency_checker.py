# -*- coding: utf-8 -*-
"""
ConsistencyValidatorDialog v2 — UI для проверки согласованности перевода.
Включает полноценный выбор ключей через KeyManagementWidget, подсветку diff, массовое исправление.
"""

import difflib
import os
import logging
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QTableWidget,
    QTableWidgetItem, QLabel, QHeaderView, QSplitter, QTextEdit,
    QProgressBar, QMessageBox, QWidget, QComboBox, QSpinBox,
    QGroupBox, QCheckBox, QApplication, QDialogButtonBox, QTabWidget,
    QFrame
)
import json
import shutil
from pathlib import Path
from datetime import datetime
from PyQt6.QtCore import Qt, pyqtSlot, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QTextCharFormat, QFont, QTextCursor, QBrush

from ...core.consistency_engine import ConsistencyEngine
from ...api.config import _load_providers_config
from ...api import config as api_config
from ..widgets.key_management_widget import KeyManagementWidget
from ..widgets.model_settings_widget import ModelSettingsWidget

# Импорт для fuzzy matching (опционально)
try:
    from fuzzywuzzy import fuzz
    FUZZYWUZZY_AVAILABLE = True
except ImportError:
    FUZZYWUZZY_AVAILABLE = False

logger = logging.getLogger(__name__)

ERROR_TYPE_TRANSLATIONS = {
    'gender_mismatch': 'Несовпадение рода',
    'term_inconsistency': 'Несогласованность терминов',
    'name_change': 'Изменение имени',
    'logic_error': 'Логическая ошибка',
    'typo': 'Опечатка',
    'meta_comment': 'Мета-комментарий'
}


class AnalysisWorker(QThread):
    """Воркер для фонового анализа глав."""
    finished = pyqtSignal()
    error = pyqtSignal(str)
    
    def __init__(self, engine, chapters, config, active_keys, mode='standard'):
        super().__init__()
        self.engine = engine
        self.chapters = chapters
        self.config = config
        self.active_keys = active_keys
        self.mode = mode

    def run(self):
        try:
            self.engine.analyze_chapters(self.chapters, self.config, self.active_keys, self.mode)
            self.finished.emit()
        except Exception as e:
            self.error.emit(str(e))


class FixWorker(QThread):
    """Воркер для фонового исправления глав."""
    finished = pyqtSignal(dict)  # {path: new_content}
    error = pyqtSignal(str)
    
    def __init__(self, engine, chapters, config, active_keys):
        super().__init__()
        self.engine = engine
        self.chapters = chapters
        self.config = config
        self.active_keys = active_keys

    def run(self):
        try:
            results = self.engine.fix_all_chapters(self.chapters, self.config, self.active_keys)
            self.finished.emit(results)
        except Exception as e:
            self.error.emit(str(e))


class ConsistencyValidatorDialog(QDialog):
    """
    Диалог проверки согласованности перевода v2.
    
    Функционал:
    - Полноценный выбор ключей через KeyManagementWidget (как на скриншоте)
    - Выбор провайдера и модели
    - Анализ чанков текста
    - Таблица найденных проблем
    - Side-by-side сравнение с diff-подсветкой
    - Одиночное и массовое исправление
    """
    
    def __init__(self, chapters, settings_manager, parent=None, project_manager=None):
        super().__init__(parent)
        # [{'name': str, 'content': str, 'path': str}]
        self.chapters = chapters
        self.settings_manager = settings_manager
        self.project_manager = project_manager
        self.engine = ConsistencyEngine(settings_manager)
        self.analysis_thread = None
        self.fix_thread = None
        
        # Кэш исправлений для применения
        self.pending_fixes = {}  # {path: new_content}
        self.fix_previews = {}   # {problem_id: (old_content, fixed_content)}
        self.current_problem = None
        self.current_chapter = None

        # Файл сессии
        self.session_file = Path(os.getcwd()) / "consistency_session.json"
        
        # Файл глоссария проекта (постоянный)
        self.project_glossary_file = None
        if self.project_manager and hasattr(self.project_manager, 'project_folder'):
            self.project_glossary_file = Path(self.project_manager.project_folder) / "project_glossary.json"

        self.setWindowTitle("🔍 Проверка согласованности (Consistency Checker)")
        self.resize(1400, 950)

        self._init_ui()
        self._setup_connections()
        
        # Проверяем наличие предыдущей сессии
        self._check_for_previous_session()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)

        # Основной контейнер с вкладками
        self.main_tabs = QTabWidget()
        layout.addWidget(self.main_tabs)

        # === Вкладка 1: Анализ (Analysis Tab) ===
        analysis_tab = QWidget()
        analysis_layout = QVBoxLayout(analysis_tab)
        
        # --- Панель управления (Toolbar) ---
        toolbar_layout = QHBoxLayout()
        
        # Основные кнопки управления
        self.start_btn = QPushButton("🚀 Начать анализ")
        self.start_btn.setStyleSheet(
            "background-color: #4CAF50; color: white; font-weight: bold; padding: 6px 20px;")
        toolbar_layout.addWidget(self.start_btn)
        
        self.stop_btn = QPushButton("⏹️ Остановить")
        self.stop_btn.setEnabled(False)
        self.stop_btn.setStyleSheet("padding: 6px 15px;")
        toolbar_layout.addWidget(self.stop_btn)
        
        analysis_control_sep = QFrame()
        analysis_control_sep.setFrameShape(QFrame.Shape.VLine)
        analysis_control_sep.setFrameShadow(QFrame.Shadow.Sunken)
        toolbar_layout.addWidget(analysis_control_sep)

        # Опции запуска
        self.glossary_first_checkbox = QCheckBox("Сначала собрать глоссарий")
        self.glossary_first_checkbox.setToolTip(
            "Два прохода: сначала только сбор персонажей/терминов, затем поиск проблем")
        toolbar_layout.addWidget(self.glossary_first_checkbox)

        # Прогресс (компактный)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setFixedWidth(200)
        toolbar_layout.addWidget(self.progress_bar)
        
        toolbar_layout.addStretch()
        
        # Кнопки действий с результатами
        self.glossary_btn = QPushButton("📖 Глоссарий")
        self.glossary_btn.setEnabled(False)
        toolbar_layout.addWidget(self.glossary_btn)
        
        self.save_all_btn = QPushButton("💾 Сохранить всё")
        self.save_all_btn.setEnabled(False)
        toolbar_layout.addWidget(self.save_all_btn)
        
        analysis_layout.addLayout(toolbar_layout)

        # --- Основная рабочая область (Splitter) ---
        # Делим экран: Слева проблемы (40%), Справа текст (60%)
        self.work_splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # 1. Левая панель: Список проблем
        problems_panel = QWidget()
        problems_layout = QVBoxLayout(problems_panel)
        problems_layout.setContentsMargins(0, 0, 0, 0)
        
        # Хедер таблицы проблем
        probs_header = QHBoxLayout()
        probs_header.addWidget(QLabel("<b>Проблемы:</b>"))
        
        # Фильтры (компактно)
        self.type_filter_combo = QComboBox()
        self.type_filter_combo.addItems(['Все типы'] + list(ERROR_TYPE_TRANSLATIONS.values()))
        self.type_filter_combo.setToolTip("Фильтр по типу ошибки")
        self.type_filter_combo.currentTextChanged.connect(self._apply_filters)
        probs_header.addWidget(self.type_filter_combo)
        
        self.confidence_filter_combo = QComboBox()
        self.confidence_filter_combo.addItems(['Любая уверенность', 'high', 'medium', 'low'])
        self.confidence_filter_combo.setToolTip("Фильтр по уверенности")
        self.confidence_filter_combo.currentTextChanged.connect(self._apply_filters)
        probs_header.addWidget(self.confidence_filter_combo)
        
        probs_header.addStretch()
        
        # Чекбокс "Все" и кнопка исправления
        self.toggle_all_checkbox = QCheckBox("Все")
        self.toggle_all_checkbox.setChecked(True)
        self.toggle_all_checkbox.stateChanged.connect(self._toggle_all_problems)
        probs_header.addWidget(self.toggle_all_checkbox)
        
        self.batch_fix_btn = QPushButton("⚡ Исправить")
        self.batch_fix_btn.setStyleSheet("background-color: #FF9800; color: white; padding: 4px 10px;")
        self.batch_fix_btn.setEnabled(False)
        probs_header.addWidget(self.batch_fix_btn)
        
        problems_layout.addLayout(probs_header)
        
        # Таблица
        self.problems_table = QTableWidget(0, 8)
        self.problems_table.setHorizontalHeaderLabels([
            "V", "ID", "Тип", "Глава", "Цитата", "Описание", "Как исправить", "Уверенность"
        ])
        header = self.problems_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.problems_table.setColumnWidth(0, 30) # Checkbox
        self.problems_table.setColumnWidth(1, 40) # ID
        self.problems_table.setColumnWidth(2, 100) # Type
        self.problems_table.setColumnWidth(3, 100) # Chapter
        # Растягиваем смысловые колонки
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch) # Quote
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch) # Desc
        
        self.problems_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.problems_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.problems_table.setAlternatingRowColors(True)
        problems_layout.addWidget(self.problems_table)
        
        # Поле деталей (внизу списка проблем)
        self.problem_info_box = QTextEdit()
        self.problem_info_box.setReadOnly(True)
        self.problem_info_box.setMaximumHeight(200) # Увеличено по просьбе
        self.problem_info_box.setPlaceholderText("Выберите проблему для просмотра деталей...")
        # Используем цвета, совместимые с темной темой
        self.problem_info_box.setStyleSheet(
            "background-color: #2b2b2b; color: #eeeeee; border-top: 1px solid #444; padding: 5px;"
        )
        problems_layout.addWidget(self.problem_info_box)
        
        # Статистика (под таблицей)
        stats_layout = QHBoxLayout()
        self.stats_label = QLabel("Нет проблем")
        stats_layout.addWidget(self.stats_label)
        stats_layout.addStretch()
        self.size_info_label = QLabel("")
        self.size_info_label.setStyleSheet("color: #777;")
        stats_layout.addWidget(self.size_info_label)
        problems_layout.addLayout(stats_layout)
        
        self.work_splitter.addWidget(problems_panel)

        # 2. Правая панель: Работа с текстом (Diff)
        text_panel = QWidget()
        text_layout = QVBoxLayout(text_panel)
        text_layout.setContentsMargins(0, 0, 0, 0)
        
        # Хедер текстовой панели
        text_header = QHBoxLayout()
        text_header.addWidget(QLabel("<b>Анализ текста:</b>"))
        text_header.addStretch()
        
        # Действия над текущей проблемой
        self.fix_btn = QPushButton("🔧 Создать исправление")
        self.fix_btn.setEnabled(False)
        text_header.addWidget(self.fix_btn)
        
        self.apply_btn = QPushButton("✅ Применить")
        self.apply_btn.setEnabled(False)
        self.apply_btn.setStyleSheet("background-color: #2196F3; color: white;")
        text_header.addWidget(self.apply_btn)
        
        self.skip_btn = QPushButton("⏭ Пропустить")
        self.skip_btn.setEnabled(False)
        text_header.addWidget(self.skip_btn)
        
        text_layout.addLayout(text_header)
        
        # Область сравнения (Сплиттер внутри правой панели)
        self.diff_splitter = QSplitter(Qt.Orientation.Vertical)
        
        # Оригинал
        orig_group = QGroupBox("Оригинал (Контекст)")
        orig_layout = QVBoxLayout(orig_group)
        orig_layout.setContentsMargins(0, 5, 0, 0)
        self.original_text = QTextEdit()
        self.original_text.setReadOnly(True)
        self.original_text.setFont(QFont("Consolas", 10))
        orig_layout.addWidget(self.original_text)
        self.diff_splitter.addWidget(orig_group)
        
        # Исправление
        fix_group = QGroupBox("Предварительный просмотр исправления")
        fix_layout = QVBoxLayout(fix_group)
        fix_layout.setContentsMargins(0, 5, 0, 0)
        self.corrected_text = QTextEdit()
        self.corrected_text.setFont(QFont("Consolas", 10))
        self.corrected_text.setPlaceholderText("Здесь появится сгенерированный вариант исправления...")
        fix_layout.addWidget(self.corrected_text)
        self.diff_splitter.addWidget(fix_group)
        
        self.diff_splitter.setSizes([400, 300])
        text_layout.addWidget(self.diff_splitter)
        
        self.work_splitter.addWidget(text_panel)
        self.work_splitter.setSizes([500, 700]) # Примерно 40% на 60%
        
        analysis_layout.addWidget(self.work_splitter)
        self.main_tabs.addTab(analysis_tab, "🔍 Анализ проекта")

        # === Вкладка 2: Настройки (Settings Tab) ===
        settings_tab = QWidget()
        settings_layout = QHBoxLayout(settings_tab)
        
        # Сплиттер настроек: Слева панели, Справа Лог
        settings_splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # Левая часть: Табы с настройками
        settings_left_tabs = QTabWidget()
        settings_left_tabs.setMinimumWidth(450)
        
        # 1. Ключи
        self.key_management_widget = KeyManagementWidget(
            self.settings_manager, 
            parent=self,
            distribution_group_widget=None
        )
        settings_left_tabs.addTab(self.key_management_widget, "API Ключи")
        
        # 2. Модель
        settings_container = self._create_model_settings_widget()
        settings_left_tabs.addTab(settings_container, "Модель и Параметры")
        
        # 3. Промты
        self.prompts_tab = self._create_prompts_settings_widget()
        settings_left_tabs.addTab(self.prompts_tab, "Системные Промты")
        
        settings_splitter.addWidget(settings_left_tabs)
        
        # Правая часть: Лог
        log_group = QGroupBox("Лог выполнения")
        log_layout = QVBoxLayout(log_group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Consolas", 9))
        log_layout.addWidget(self.log_text)
        
        settings_splitter.addWidget(log_group)
        settings_splitter.setSizes([500, 700])
        
        settings_layout.addWidget(settings_splitter)
        self.main_tabs.addTab(settings_tab, "⚙️ Настройки и Логи")
        
        # Нижняя панель (Общая кнопка Закрыть)
        close_btn_layout = QHBoxLayout()
        close_btn_layout.addStretch()
        self.close_btn = QPushButton("Закрыть")
        close_btn_layout.addWidget(self.close_btn)
        layout.addLayout(close_btn_layout)

        # Обновляем информацию о размере
        self._update_size_info()

    def _create_model_settings_widget(self) -> QWidget:
        """Создаёт виджет настроек модели для встраивания в KeyManagementWidget."""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Полноценный виджет настроек модели
        self.model_settings_widget = ModelSettingsWidget(
            parent=self, 
            settings_manager=self.settings_manager
        )
        layout.addWidget(self.model_settings_widget)
        
        # Дополнительный блок настроек именно для Consistency Checker
        extra_group = QGroupBox("Параметры анализа")
        extra_layout = QVBoxLayout(extra_group)
        
        # Размер чанка
        chunk_layout = QHBoxLayout()
        chunk_layout.addWidget(QLabel("Глав в чанке:"))
        self.chunk_size_spin = QSpinBox()
        self.chunk_size_spin.setRange(1, 100)
        self.chunk_size_spin.setValue(3)
        self.chunk_size_spin.setToolTip("Сколько глав отправлять на анализ за один запрос")
        self.chunk_size_spin.valueChanged.connect(self._update_chunk_stats)
        chunk_layout.addWidget(self.chunk_size_spin)
        chunk_layout.addStretch()
        extra_layout.addLayout(chunk_layout)
        
        # Инфо о чанке (Токены)
        self.chunk_info_label = QLabel("~0 токенов")
        self.chunk_info_label.setStyleSheet("color: #666; font-size: 8pt;")
        extra_layout.addWidget(self.chunk_info_label)
        
        layout.addWidget(extra_group)
        
        # Подключаем сигнал смены модели в новом виджете для обновления статистики чанка
        self.model_settings_widget.settings_changed.connect(self._update_chunk_stats)
        
        # Скрываем ненужные группы для чистоты интерфейса
        self.model_settings_widget.set_cjk_options_visible(False)
        self.model_settings_widget.set_glossary_options_visible(False)
        self.model_settings_widget.set_misc_options_visible(False)
        
        return container

    def _create_prompts_settings_widget(self) -> QWidget:
        """Создаёт виджет для редактирования промтов."""
        container = QWidget()
        layout = QVBoxLayout(container)
        
        self.prompts_editors = {}
        
        # Загружаем текущие промты
        prompts_file = api_config.get_resource_path("config/consistency_prompts.json")
        prompts_data = {}
        if prompts_file.exists():
            try:
                with open(prompts_file, 'r', encoding='utf-8') as f:
                    prompts_data = json.load(f)
            except: pass
            
        scroll = QSplitter(Qt.Orientation.Vertical)
        
        sections = [
            ("consistency_analysis", "Анализ (поиск проблем)"),
            ("consistency_correction", "Исправление (одиночное)"),
            ("batch_chapter_fix", "Пакетное исправление"),
            ("glossary_collection", "Сбор глоссария")
        ]
        
        for key, label in sections:
            group = QGroupBox(label)
            g_layout = QVBoxLayout(group)
            editor = QTextEdit()
            editor.setAcceptRichText(False)
            current_val = "\n".join(prompts_data.get(key, []))
            editor.setPlainText(current_val)
            g_layout.addWidget(editor)
            self.prompts_editors[key] = editor
            scroll.addWidget(group)
            
        layout.addWidget(scroll)
        
        btn_layout = QHBoxLayout()
        save_prompts_btn = QPushButton("💾 Сохранить промты")
        save_prompts_btn.clicked.connect(self._save_custom_prompts)
        btn_layout.addWidget(save_prompts_btn)
        
        reset_prompts_btn = QPushButton("🔄 Сбросить")
        reset_prompts_btn.clicked.connect(self._reset_prompts)
        btn_layout.addWidget(reset_prompts_btn)
        
        layout.addLayout(btn_layout)
        return container

    def _save_custom_prompts(self):
        """Сохраняет измененные промты в файл."""
        prompts_file = api_config.get_resource_path("config/consistency_prompts.json")
        
        data = {}
        for key, editor in self.prompts_editors.items():
            content = editor.toPlainText().strip()
            data[key] = content.split('\n')
            
        try:
            with open(prompts_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            QMessageBox.information(self, "Успех", "Промты успешно сохранены.")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось сохранить промты: {e}")

    def _reset_prompts(self):
        """Сброс к значениям по умолчанию (загрузка из файла заново)."""
        prompts_file = api_config.get_resource_path("config/consistency_prompts.json")
        if not prompts_file.exists(): return
        
        try:
            with open(prompts_file, 'r', encoding='utf-8') as f:
                prompts_data = json.load(f)
                for key, editor in self.prompts_editors.items():
                    editor.setPlainText("\n".join(prompts_data.get(key, [])))
        except: pass

    def _toggle_all_problems(self, state):
        """Включает/выключает чекбоксы для всех строк."""
        checked = state == Qt.CheckState.Checked.value
        for row in range(self.problems_table.rowCount()):
            item = self.problems_table.item(row, 0)
            if item:
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def _update_chunk_stats(self):
        """Обновляет информацию о размере чанка в токенах."""
        if not self.chapters:
            return
            
        chunk_size = self.chunk_size_spin.value()
        # Берем средний размер первых 3 чанков для оценки
        total_chars = 0
        samples = 0
        
        for i in range(0, min(len(self.chapters), chunk_size * 3), chunk_size):
            chunk = self.chapters[i:i + chunk_size]
            chars = sum(len(ch['content']) for ch in chunk)
            total_chars += chars
            samples += 1
            
        if samples == 0:
            avg_chars = 0
        else:
            avg_chars = total_chars / samples
            
        # Оценка токенов (грубая)
        # Assuming Cyrillic mostly, so divide by CHARS_PER_CYRILLIC_TOKEN (~2.2) 
        # But let's verify what config says or use a safe constant.
        # api_config.CHARS_PER_CYRILLIC_TOKEN is available.
        
        tokens_est = int(avg_chars / api_config.CHARS_PER_CYRILLIC_TOKEN)
        
        # Получаем лимит модели
        limit_text = ""
        provider_id = self.key_management_widget.get_selected_provider()
        
        # Получаем текущую выбранную модель из ModelSettingsWidget
        msw_settings = self.model_settings_widget.get_settings()
        model_name = msw_settings.get('model')
        
        if not model_name:
            return

        providers = api_config.api_providers()
        if provider_id in providers:
            models = providers[provider_id].get('models', {})
            if model_name in models:
                max_input = models[model_name].get('maxInputTokens') # CamelCase in JSON? Or snake_case?
                # Check config structure. Usually it is snake_case in Python dict if loaded, 
                # but let's check how it's stored.
                # It seems it's often stored as is from JSON.
                if not max_input:
                     max_input = models[model_name].get('max_input_tokens')
                
                if max_input:
                     limit_text = f" / {max_input}"
                     if tokens_est > max_input:
                         self.chunk_info_label.setStyleSheet("color: red; font-weight: bold; font-size: 8pt;")
                     else:
                         self.chunk_info_label.setStyleSheet("color: #666; font-size: 8pt;")

        self.chunk_info_label.setText(f"~{tokens_est}{limit_text} токенов/чанк")

    def _setup_connections(self):
        self.start_btn.clicked.connect(self.run_analysis)
        self.stop_btn.clicked.connect(self._stop_analysis)
        self.problems_table.itemSelectionChanged.connect(self.on_problem_selected)
        self.fix_btn.clicked.connect(self.run_fix)
        self.apply_btn.clicked.connect(self.apply_fix)
        self.skip_btn.clicked.connect(self.skip_problem)
        self.batch_fix_btn.clicked.connect(self.run_batch_fix)
        self.glossary_btn.clicked.connect(self.show_glossary)
        self.save_all_btn.clicked.connect(self.save_all_fixes)
        self.close_btn.clicked.connect(self.close)

        # Сигналы от engine
        self.engine.progress_updated.connect(self.update_progress)
        self.engine.chunk_analyzed.connect(self.on_chunk_done)
        self.engine.finished.connect(self.on_analysis_finished)
        self.engine.error_occurred.connect(self.on_error)
        self.engine.fix_progress.connect(self.on_fix_progress)
        self.engine.fix_completed.connect(self.on_single_fix_completed)
        
        # Обновление моделей при смене провайдера
        if hasattr(self.key_management_widget, 'provider_combo'):
            self.key_management_widget.provider_combo.currentTextChanged.connect(
                self._on_provider_changed)
            # Инициализируем модели
            self._on_provider_changed(self.key_management_widget.provider_combo.currentText())

    def _on_provider_changed(self, provider_display_name):
        """Обновляет список моделей при смене провайдера."""
        try:
            # Найти provider_id по display_name
            provider_id = None
            for p_id, p_data in api_config.api_providers().items():
                if p_data.get('display_name') == provider_display_name:
                    provider_id = p_id
                    break
            
            if not provider_id:
                provider_id = self.key_management_widget.get_selected_provider()
            
            # Передаём управление списком моделей в ModelSettingsWidget
            self.model_settings_widget.set_available_models(provider_id)
            
        except Exception as e:
            self._log(f"Ошибка обновления моделей: {e}")

    def _update_size_info(self):
        """Обновляет информацию о размере текста."""
        total_chars = sum(len(ch.get('content', '')) for ch in self.chapters)
        total_chapters = len(self.chapters)
        self.size_info_label.setText(
            f"Глав: {total_chapters} | Символов: {total_chars:,}"
        )

    def _log(self, message: str):
        """Добавляет сообщение в лог."""
        self.log_text.append(message)
        self.log_text.verticalScrollBar().setValue(
            self.log_text.verticalScrollBar().maximum()
        )

    def _get_current_config(self) -> dict:
        """Возвращает текущую конфигурацию из UI."""
        provider_id = self.key_management_widget.get_selected_provider()
        
        # Получаем все настройки из ModelSettingsWidget (Thinking, Temperature, Model, etc.)
        config = self.model_settings_widget.get_settings()
        
        # Добавляем специфичные для валидатора поля
        config.update({
            'provider': provider_id,
            'chunk_size': self.chunk_size_spin.value()
        })
        
        return config

    def _get_active_keys(self) -> list:
        """Возвращает список активных ключей для сессии."""
        return self.key_management_widget.get_active_keys()

    def run_analysis(self):
        """Запускает анализ глав."""
        if self.analysis_thread and self.analysis_thread.isRunning():
            return

        if not self.chapters:
            QMessageBox.warning(self, "Предупреждение", "Нет глав для анализа.")
            return

        active_keys = self._get_active_keys()
        if not active_keys:
            QMessageBox.warning(self, "Нет ключей", 
                "Добавьте ключи в 'Активные ключи для сессии' для запуска анализа.")
            return

        self.problems_table.setRowCount(0)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.batch_fix_btn.setEnabled(False)
        self.pending_fixes.clear()
        self.log_text.clear()

        config = self._get_current_config()
        
        # Определяем режим анализа
        mode = 'glossary_first' if self.glossary_first_checkbox.isChecked() else 'standard'
        
        self._log(f"▶ Начало анализа: {len(self.chapters)} глав, "
                  f"провайдер: {config['provider']}, модель: {config['model']}")
        self._log(f"  Активных ключей: {len(active_keys)}, глав в чанке: {config['chunk_size']}")
        if mode == 'glossary_first':
            self._log("  📚 Режим: сначала сбор глоссария (два прохода)")

        self.analysis_thread = AnalysisWorker(
            self.engine, self.chapters, config, active_keys, mode)
        self.analysis_thread.error.connect(self.on_error)
        self.analysis_thread.start()

    def _stop_analysis(self):
        """Останавливает анализ."""
        self.engine.cancel()
        self._log("⏹ Анализ остановлен пользователем")

    @pyqtSlot(int, int)
    def update_progress(self, current, total):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
        self._log(f"  Чанк {current}/{total} обработан")

    @pyqtSlot(dict)
    def on_chunk_done(self, result):
        """Обрабатывает результат анализа одного чанка."""
        problems = result.get('problems', [])
        
        # Добавляем информацию о новых терминах в лог
        glossary = result.get('glossary_update', {})
        new_chars = len(glossary.get('characters', []))
        new_terms = len(glossary.get('terms', []))
        if new_chars > 0 or new_terms > 0:
            self._log(f"    + {new_chars} персонажей, {new_terms} терминов в глоссарий")
        
        for prob in problems:
            row = self.problems_table.rowCount()
            self.problems_table.insertRow(row)

            # Колонка 0: Чекбокс
            check_item = QTableWidgetItem()
            check_item.setCheckState(Qt.CheckState.Checked)
            self.problems_table.setItem(row, 0, check_item)

            self.problems_table.setItem(row, 1, QTableWidgetItem(str(prob.get('id', ''))))
            
            # Тип проблемы с переводом и улучшенными цветами
            type_raw = prob.get('type', '')
            type_text = ERROR_TYPE_TRANSLATIONS.get(type_raw, type_raw)
            type_item = QTableWidgetItem(type_text)
            bg_color, text_color = self._get_type_colors(type_raw)
            type_item.setBackground(QColor(bg_color))
            type_item.setForeground(QBrush(QColor(text_color)))
            self.problems_table.setItem(row, 2, type_item)
            
            self.problems_table.setItem(row, 3, QTableWidgetItem(prob.get('chapter', '')))
            quote = prob.get('quote', '')
            self.problems_table.setItem(row, 4, QTableWidgetItem(
                quote[:50] + '...' if len(quote) > 50 else quote))
            self.problems_table.setItem(row, 5, QTableWidgetItem(prob.get('description', '')))
            self.problems_table.setItem(row, 6, QTableWidgetItem(prob.get('suggestion', '')))
            
            # Уверенность с улучшенными цветами
            conf_text = prob.get('confidence', 'medium')
            conf_item = QTableWidgetItem(conf_text)
            conf_bg, conf_fg = self._get_confidence_colors(conf_text)
            conf_item.setBackground(QColor(conf_bg))
            conf_item.setForeground(QBrush(QColor(conf_fg)))
            self.problems_table.setItem(row, 7, conf_item)

            # Сохраняем полные данные проблемы в колонке ID
            self.problems_table.item(row, 1).setData(Qt.ItemDataRole.UserRole, prob)

        # Обновляем статистику
        self.stats_label.setText(f"Проблем: {self.problems_table.rowCount()}")
        
        # Разблокируем кнопку глоссария, если есть данные
        if self.engine.glossary_session.characters or self.engine.glossary_session.terms:
            self.glossary_btn.setEnabled(True)
            self.glossary_btn.setText(f"📖 Глоссарий ({len(self.engine.glossary_session.characters)} перс., {len(self.engine.glossary_session.terms)} терм.)")
            
        # Автосохранение сессии "на лету"
        self._save_session()
        
        # Применяем фильтры сразу
        self._apply_filters()

    def _apply_filters(self):
        """Применяет фильтры к таблице проблем."""
        type_filter = self.type_filter_combo.currentText()
        conf_filter = self.confidence_filter_combo.currentText()
        
        visible_count = 0
        for row in range(self.problems_table.rowCount()):
            show = True
            
            # Фильтр по типу
            if type_filter != 'Все типы':
                type_item = self.problems_table.item(row, 2)
                if type_item and type_item.text() != type_filter:
                    show = False
            
            # Фильтр по уверенности
            # Проверяем индекс - 0 это "Любая уверенность"
            if self.confidence_filter_combo.currentIndex() > 0:
                conf_item = self.problems_table.item(row, 7)
                # Для сравнения используем text(), который должен совпадать с одним из значений в combo (кроме первого)
                # Значения в combo: 'Любая...', 'high', 'medium', 'low'
                # Значения в таблице: 'high', 'medium', 'low'
                if conf_item and conf_item.text() != conf_filter:
                    show = False
            
            self.problems_table.setRowHidden(row, not show)
            if show:
                visible_count += 1
        
        # Обновляем статистику
        total_count = self.problems_table.rowCount()
        if self.type_filter_combo.currentIndex() > 0 or self.confidence_filter_combo.currentIndex() > 0:
            self.stats_label.setText(f"Проблем: {visible_count}/{total_count}")
        else:
            self.stats_label.setText(f"Проблем: {total_count}")

    def _get_type_colors(self, problem_type: str) -> tuple:
        """Возвращает (bg_color, text_color) для типа проблемы."""
        colors = {
            'gender_mismatch': ('#fff0f0', '#c62828'),    # светло-красный фон, тёмно-красный текст
            'term_inconsistency': ('#fffde7', '#f57f17'), # светло-жёлтый фон, тёмно-оранжевый текст
            'name_change': ('#e8f5e9', '#2e7d32'),        # светло-зелёный фон, тёмно-зелёный текст
            'logic_error': ('#fff3e0', '#e65100'),        # светло-оранжевый фон, тёмно-оранжевый текст
            'typo': ('#f3e5f5', '#7b1fa2'),               # светло-фиолетовый фон, тёмно-фиолетовый текст
            'meta_comment': ('#e3f2fd', '#1565c0'),       # светло-голубой фон, тёмно-голубой текст
        }
        return colors.get(problem_type, ('#f5f5f5', '#424242'))

    def _get_type_color(self, problem_type: str) -> QColor:
        """Возвращает цвет фона для типа проблемы (обратная совместимость)."""
        bg_color, _ = self._get_type_colors(problem_type)
        return QColor(bg_color)

    def _get_confidence_colors(self, confidence: str) -> tuple:
        """Возвращает (bg_color, text_color) для уровня уверенности."""
        colors = {
            'high': ('#e8f5e9', '#2e7d32'),    # зелёный
            'medium': ('#fffde7', '#f57f17'),  # жёлтый
            'low': ('#ffebee', '#c62828'),     # красный
        }
        return colors.get(confidence, ('#f5f5f5', '#424242'))

    def _get_confidence_color(self, confidence: str) -> QColor:
        """Возвращает цвет фона для уровня уверенности (обратная совместимость)."""
        bg_color, _ = self._get_confidence_colors(confidence)
        return QColor(bg_color)

    @pyqtSlot(list)
    def on_analysis_finished(self, all_problems):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress_bar.setVisible(False)
        self.glossary_btn.setEnabled(True)
        
        if all_problems:
            self.batch_fix_btn.setEnabled(True)
        
        self._log(f"✅ Анализ завершён. Найдено проблем: {len(all_problems)}")
        self._log(f"   Персонажей в глоссарии: {len(self.engine.glossary_session.characters)}")
        self._log(f"   Терминов в глоссарии: {len(self.engine.glossary_session.terms)}")
        
        # Статистика токенов глоссария
        token_count = self.engine.get_glossary_token_count()
        self._log(f"   Токенов в глоссарии: ~{token_count}")

    @pyqtSlot(str)
    def on_error(self, error_msg):
        self._log(f"❌ Ошибка: {error_msg}")
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress_bar.setVisible(False)

    def on_problem_selected(self):
        """Обрабатывает выбор проблемы в таблице."""
        selected_rows = self.problems_table.selectionModel().selectedRows()
        if not selected_rows:
            return

        row = selected_rows[0].row()
        prob_data = self.problems_table.item(row, 1).data(Qt.ItemDataRole.UserRole)
        self.current_problem = prob_data

        # Показываем полное описание в информационном блоке
        info_text = (
            f"<b>Тип:</b> {ERROR_TYPE_TRANSLATIONS.get(prob_data.get('type',''), prob_data.get('type',''))}<br>"
            f"<b>Глава:</b> {prob_data.get('chapter', '')}<br>"
            f"<b>Описание:</b> {prob_data.get('description', '')}<br>"
            f"<b>Предложение:</b> {prob_data.get('suggestion', '')}<br>"
            f"<b>Цитата:</b> {prob_data.get('quote', '')}"
        )
        self.problem_info_box.setHtml(info_text)

        # Находим соответствующую главу
        chapter_name = prob_data.get('chapter', '')
        for ch in self.chapters:
            if ch['name'] == chapter_name or chapter_name in ch['name']:
                self.current_chapter = ch
                break

        if self.current_chapter:
            # Показываем оригинальный текст с подсветкой проблемного места
            self._display_original_with_highlight(prob_data)
            self.fix_btn.setEnabled(True)
            self.skip_btn.setEnabled(True)
            
            # Восстанавливаем превью, если оно есть
            prob_id = prob_data.get('id')
            if prob_id in self.fix_previews:
                old_c, new_c = self.fix_previews[prob_id]
                # Проверяем, актуально ли превью для текущего контента главы
                if old_c == self.current_chapter['content']:
                    self._show_diff(old_c, new_c)
                    self.apply_btn.setEnabled(True)
                else:
                    self.corrected_text.clear()
                    self.apply_btn.setEnabled(False)
            else:
                self.corrected_text.clear()
                self.apply_btn.setEnabled(False)
        else:
            self.original_text.setPlainText(f"Глава '{chapter_name}' не найдена в списке.")
            self.corrected_text.clear()
            self.apply_btn.setEnabled(False)

    def _display_original_with_highlight(self, prob_data: dict):
        """Показывает оригинальный текст с подсветкой проблемного места."""
        if not self.current_chapter:
            return
            
        content = self.current_chapter['content']
        quote = prob_data.get('quote', '')
        
        if not quote:
            self.original_text.setPlainText(content)
            return
        
        # Точный поиск цитаты
        if quote in content:
            # Подсвечиваем найденную цитату
            escaped_content = self._escape_html(content)
            escaped_quote = self._escape_html(quote)
            # Используем CYAN для подсветки, чтобы текст оставался читаемым
            highlighted = escaped_content.replace(
                escaped_quote, 
                f'<span id="current_problem" style="background-color: #80deea; color: #000; font-weight: bold;">{escaped_quote}</span>'
            )
            self.original_text.setHtml(f"<pre style='white-space: pre-wrap;'>{highlighted}</pre>")
            # Авто-прокрутка к якорю
            self.original_text.find("current_problem") # Это сфокусирует курсор на тексте
            return
        
        # Fuzzy поиск: ищем наиболее похожий фрагмент
        if FUZZYWUZZY_AVAILABLE and len(quote) > 10:
            best_match, best_ratio, best_pos = None, 0, -1
            window = len(quote)
            step = max(1, window // 4)  # Шаг для оптимизации
            
            for i in range(0, len(content) - window + 1, step):
                candidate = content[i:i + window]
                ratio = fuzz.ratio(quote, candidate)
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_match = candidate
                    best_pos = i
            
            if best_ratio >= 70:  # Порог схожести
                # Подсвечиваем найденный фрагмент с пометкой о fuzzy match
                escaped_content = self._escape_html(content)
                escaped_match = self._escape_html(best_match)
                highlighted = escaped_content.replace(
                    escaped_match,
                    f'<span id="current_problem" style="background-color: #e0f7fa; color: #000; font-weight: bold;" title="Fuzzy match: {best_ratio}%">{escaped_match}</span>',
                    1  # Заменяем только первое вхождение
                )
                self.original_text.setHtml(f"<pre style='white-space: pre-wrap;'>{highlighted}</pre>")
                self.original_text.find("current_problem")
                self._log(f"   ℹ️ Fuzzy match: {best_ratio}% (цитата не найдена точно)")
                return
            else:
                self._log(f"   ⚠️ Цитата не найдена (лучшее совпадение: {best_ratio}%)")
        
        # Fallback: показываем без подсветки
        self.original_text.setPlainText(content)

    def run_fix(self):
        """Генерирует исправление для выбранной проблемы."""
        if not self.current_problem or not self.current_chapter:
            return

        active_keys = self._get_active_keys()
        if not active_keys:
            QMessageBox.warning(self, "Нет ключей", 
                "Добавьте ключи в 'Активные ключи для сессии'.")
            return

        self.fix_btn.setEnabled(False)
        self.fix_btn.setText("⏳ Генерация...")
        QApplication.processEvents()

        try:
            config = self._get_current_config()
            fixed_content = self.engine.fix_chapter(
                self.current_chapter['content'],
                [self.current_problem],
                config,
                active_keys,
                batch_mode=False
            )
            
            # Показываем diff
            self._show_diff(self.current_chapter['content'], fixed_content)
            self.apply_btn.setEnabled(True)
            
            # Сохраняем в кэш превью
            self.fix_previews[self.current_problem.get('id')] = (self.current_chapter['content'], fixed_content)
            
            self._log(f"🔧 Сгенерировано исправление для: {self.current_chapter['name']}")
            
        except Exception as e:
            self._log(f"❌ Ошибка генерации: {e}")
            QMessageBox.warning(self, "Ошибка", f"Не удалось сгенерировать исправление: {e}")
        
        finally:
            self.fix_btn.setEnabled(True)
            self.fix_btn.setText("🔧 Сгенерировать исправление")

    def _show_diff(self, old_text: str, new_text: str):
        """Показывает diff между старым и новым текстом."""
        self.corrected_text.clear()
        
        old_lines = old_text.splitlines()
        new_lines = new_text.splitlines()
        
        # Формируем HTML с подсветкой
        html_parts = ['<pre style="font-family: Consolas; font-size: 10pt;">']
        
        for line in new_lines:
            # Проверяем, изменилась ли эта строка
            if line not in old_lines:
                html_parts.append(f'<span style="background-color: #c8e6c9;">{self._escape_html(line)}</span>')
            else:
                html_parts.append(self._escape_html(line))
            html_parts.append('<br>')
        
        html_parts.append('</pre>')
        self.corrected_text.setHtml(''.join(html_parts))
        
        # Сохраняем новый текст для применения
        self.corrected_text.setProperty('new_content', new_text)

    def _escape_html(self, text: str) -> str:
        """Экранирует HTML-символы."""
        return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    def apply_fix(self):
        """Применяет исправление (сохраняет в pending_fixes)."""
        if not self.current_chapter:
            return
            
        new_content = self.corrected_text.property('new_content')
        if not new_content:
            new_content = self.corrected_text.toPlainText()
        
        if new_content:
            path = self.current_chapter['path']
            self.pending_fixes[path] = new_content
            
            # Обновляем кэш главы
            self.current_chapter['content'] = new_content
            
            # Помечаем проблему как исправленную
            selected_rows = self.problems_table.selectionModel().selectedRows()
            if selected_rows:
                row = selected_rows[0].row()
                self.problems_table.item(row, 1).setBackground(QColor('#c8e6c9'))
            
            self.save_all_btn.setEnabled(True)
            self._log(f"✅ Исправление принято: {os.path.basename(path)}")

    def skip_problem(self):
        """Пропускает текущую проблему."""
        self.original_text.clear()
        self.corrected_text.clear()
        self.current_problem = None
        self.current_chapter = None
        self.fix_btn.setEnabled(False)
        self.apply_btn.setEnabled(False)
        self.skip_btn.setEnabled(False)

    def run_batch_fix(self):
        """Запускает массовое исправление всех проблем."""
        if self.fix_thread and self.fix_thread.isRunning():
            return

        # Собираем только выбранные (с чекбоксами) проблемы
        selected_problems_map = {} # {chapter_name: [problems]}
        count = 0
        for row in range(self.problems_table.rowCount()):
            if self.problems_table.item(row, 0).checkState() == Qt.CheckState.Checked:
                prob = self.problems_table.item(row, 1).data(Qt.ItemDataRole.UserRole)
                ch_name = prob.get('chapter')
                if ch_name not in selected_problems_map:
                    selected_problems_map[ch_name] = []
                selected_problems_map[ch_name].append(prob)
                count += 1
        
        if count == 0:
            QMessageBox.information(self, "Выбор", "Не выбрано ни одной проблемы для исправления.")
            return

        active_keys = self._get_active_keys()
        if not active_keys:
            QMessageBox.warning(self, "Нет ключей", 
                "Добавьте ключи в 'Активные ключи для сессии'.")
            return

        reply = QMessageBox.question(
            self, "Массовое исправление",
            f"Исправить выбранные ({count}) проблемы автоматически?\n\n"
            "Это может занять некоторое время.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.batch_fix_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        
        config = self._get_current_config()
        
        self._log(f"⚡ Начало массового исправления ({count} проблем)...")
        # Временно подменяем карту проблем в движке на отфильтрованную
        old_map = self.engine.chapter_problems_map
        self.engine.chapter_problems_map = selected_problems_map
        
        self.fix_thread = FixWorker(self.engine, self.chapters, config, active_keys)
        self.fix_thread.finished.connect(lambda res: self._on_batch_fix_finished_wrapper(res, old_map))
        self.fix_thread.error.connect(self.on_error)
        self.fix_thread.start()

    def _on_batch_fix_finished_wrapper(self, results, old_map):
        """Восстанавливает карту проблем после пакетного исправления."""
        self.engine.chapter_problems_map = old_map
        self.on_batch_fix_finished(results)

    @pyqtSlot(int, int, str)
    def on_fix_progress(self, current, total, chapter_name):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
        self._log(f"  [{current}/{total}] Исправление: {chapter_name}")

    @pyqtSlot(str, str)
    def on_single_fix_completed(self, path, new_content):
        """Обрабатывает завершение исправления одной главы."""
        self.pending_fixes[path] = new_content

    @pyqtSlot(dict)
    def on_batch_fix_finished(self, results):
        """Обрабатывает завершение массового исправления."""
        self.pending_fixes.update(results)
        self.batch_fix_btn.setEnabled(True)
        self.start_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.save_all_btn.setEnabled(bool(self.pending_fixes))
        
        self._log(f"✅ Массовое исправление завершено. Исправлено глав: {len(results)}")

    def show_glossary(self):
        """Показывает накопленный глоссарий сессии."""
        glossary = self.engine.get_glossary_summary()
        
        dialog = QDialog(self)
        dialog.setWindowTitle("📖 Глоссарий сессии")
        dialog.resize(700, 500)
        
        layout = QVBoxLayout(dialog)
        
        text = QTextEdit()
        text.setReadOnly(True)
        text.setFont(QFont("Consolas", 10))
        
        # Форматируем глоссарий
        html = "<h2>Персонажи</h2><ul>"
        for char in glossary.get('characters', []):
            html += f"<li><b>{char.get('name', '?')}</b>"
            if char.get('aliases'):
                html += f" ({', '.join(char['aliases'])})"
            html += f" — {char.get('role', '')} [{char.get('gender', '')}]"
            if char.get('notes'):
                html += f"<br><i>{char['notes']}</i>"
            html += "</li>"
        html += "</ul>"
        
        html += "<h2>Термины</h2><ul>"
        for term in glossary.get('terms', []):
            html += f"<li><b>{term.get('term', '?')}</b> — {term.get('definition', '')}</li>"
        html += "</ul>"
        
        html += "<h2>Сюжетные линии</h2><ul>"
        for plot in glossary.get('plots', []):
            html += f"<li>{plot}</li>"
        html += "</ul>"
        
        text.setHtml(html)
        layout.addWidget(text)
        
        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(dialog.close)
        layout.addWidget(close_btn)
        
        dialog.exec()

    def save_all_fixes(self):
        """Сохраняет все накопленные исправления в файлы."""
        if not self.pending_fixes:
            QMessageBox.information(self, "Нет изменений", "Нет исправлений для сохранения.")
            return

        reply = QMessageBox.question(
            self, "Сохранение исправлений",
            f"Сохранить изменения в {len(self.pending_fixes)} файл(ов)?\n\n"
            "Это перезапишет оригинальные файлы.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply != QMessageBox.StandardButton.Yes:
            return

        saved = 0
        errors = []
        
        for path, content in self.pending_fixes.items():
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(content)
                saved += 1
                self._log(f"💾 Сохранено: {os.path.basename(path)}")
            except Exception as e:
                errors.append(f"{os.path.basename(path)}: {e}")

        if errors:
            QMessageBox.warning(
                self, "Ошибки при сохранении",
                f"Сохранено: {saved}\nОшибок: {len(errors)}\n\n" + "\n".join(errors[:5])
            )
        else:
            self.pending_fixes.clear()
            self._save_session() # Сохраняем состояние после записи исправлений
            QMessageBox.information(
                self, "Успех", 
                f"Успешно сохранено {saved} файл(ов)."
            )

    def _update_size_info(self):
        """Выводит информацию о размере выбранных глав (в символах и токенах)."""
        if not self.chapters:
            return
        
        total_chars = sum(len(ch['content']) for ch in self.chapters)
        # Грубая оценка токенов: 1 токен ~ 4 символа (для русского ~2-3, берем среднее 3 для безопасности)
        est_tokens = total_chars // 3
        
        self.size_info_label.setText(f"Глав: {len(self.chapters)} | Символов: {total_chars:,} (~{est_tokens:,} токенов)")

    def _check_for_previous_session(self):
        """
        Проверяет наличие файла сессии и предлагает восстановить.
        Если сессии нет или пользователь отказался, пробуем загрузить глоссарий проекта.
        """
        if not self.session_file.exists():
            self._load_project_glossary()
            return
            
        try:
            with open(self.session_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            self._load_project_glossary()
            return # Битая сессия
            
        reply = QMessageBox.question(
            self, "Восстановление сессии",
            f"Найдена предыдущая сессия ({data.get('timestamp', 'н/д')}).\n"
            f"- Обработано глав: {len(data.get('processed_chapters', []))}\n"
            f"- Найдено проблем: {len(data.get('problems', []))}\n"
            f"- Персонажей в глоссарии: {len(data.get('glossary', {}).get('characters', []))}\n\n"
            "Восстановить работу с места остановки?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            self._restore_session(data)
        else:
            self._load_project_glossary()

    def _load_project_glossary(self):
        """Загружает глоссарий из файла проекта, если он есть."""
        if not self.project_glossary_file or not self.project_glossary_file.exists():
            return
            
        try:
            with open(self.project_glossary_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
            # Загружаем только глоссарий
            glossary_data = data.get('glossary', data) # Поддержка и полной структуры и чистого глоссария
            
            # Если это полная структура сессии, берем glossary
            if 'glossary' in data:
                 glossary_data = data['glossary']
            
            if glossary_data:
                self.engine.glossary_session.characters = glossary_data.get('characters', [])
                self.engine.glossary_session.terms = glossary_data.get('terms', [])
                
                # Обновляем кнопку
                if self.engine.glossary_session.characters or self.engine.glossary_session.terms:
                    self.glossary_btn.setEnabled(True)
                    self.glossary_btn.setText(f"📖 Глоссарий ({len(self.engine.glossary_session.characters)} перс., {len(self.engine.glossary_session.terms)} терм.)")
                    
                self._log("📂 Загружен глоссарий проекта.")
                
        except Exception as e:
            logger.error(f"Error loading project glossary: {e}")

    def _save_session(self):
        """Сохраняет текущее состояние в JSON (сессия + глоссарий проекта)."""
        try:
            problems_data = []
            for probs_list in self.engine.chapter_problems_map.values():
                problems_data.extend(probs_list)
                
            data = {
                'timestamp': str(datetime.now()),
                'glossary': self.engine.glossary_session.to_dict(),
                'processed_chapters': self.engine.glossary_session.processed_chapters,
                'problems': problems_data,
            }
            
            # 1. Сохраняем временную сессию
            with open(self.session_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                
            # 2. Сохраняем глоссарий проекта (если есть путь)
            if self.project_glossary_file:
                # Для проекта сохраняем только глоссарий или полную структуру?
                # Логичнее сохранять структуру с глоссарием, чтобы формат был универсальным.
                # Но лучше сохранять чистый глоссарий или структуру, совместимую с session.
                # Сохраним полную структуру, чтобы можно было переносить.
                with open(self.project_glossary_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    
        except Exception as e:
            logger.error(f"Failed to save session: {e}", exc_info=True)

    def _restore_session(self, data):
        """Восстанавливает состояние из словаря данных."""
        try:
            # 1. Восстанавливаем глоссарий
            glossary_data = data.get('glossary', {})
            self.engine.glossary_session.characters = glossary_data.get('characters', [])
            self.engine.glossary_session.terms = glossary_data.get('terms', [])
            self.engine.glossary_session.processed_chapters = data.get('processed_chapters', [])
            
            # 2. Восстанавливаем проблемы
            problems = data.get('problems', [])
            
            # Для восстановления таблицы используем тот же метод, что и при чанке
            # Но сначала очистим карту проблем в движке, чтобы заполнить заново
            self.engine.chapter_problems_map = {}
            for p in problems:
                ch = p.get('chapter')
                if ch not in self.engine.chapter_problems_map:
                    self.engine.chapter_problems_map[ch] = []
                self.engine.chapter_problems_map[ch].append(p)
            
            if problems:
                self.problems_table.setRowCount(0)
                self.on_chunk_done({'problems': problems, 'glossary_update': {}})  # пустое обновление глоссария
            
            # 3. Обновляем UI
            if self.engine.glossary_session.characters or self.engine.glossary_session.terms:
                self.glossary_btn.setEnabled(True)
                self.glossary_btn.setText(f"📖 Глоссарий ({len(self.engine.glossary_session.characters)} перс., {len(self.engine.glossary_session.terms)} терм.)")
            
            self._log("♻️ Сессия успешно восстановлена.")
            
        except Exception as e:
            QMessageBox.critical(self, "Ошибка восстановления", f"Не удалось восстановить сессию:\n{e}")
            self.save_all_btn.setEnabled(False)
            self._log(f"❌ Ошибка восстановления сессии: {e}")


    def closeEvent(self, event):
        """Обрабатывает закрытие диалога."""
        if self.pending_fixes:
            reply = QMessageBox.question(
                self, "Несохранённые изменения",
                f"У вас есть {len(self.pending_fixes)} несохранённых исправлений.\n\n"
                "Закрыть без сохранения?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        
        # Отменяем фоновые операции
        self.engine.cancel()
        if self.analysis_thread and self.analysis_thread.isRunning():
            self.analysis_thread.wait(1000)
        if self.fix_thread and self.fix_thread.isRunning():
            self.fix_thread.wait(1000)
        
        event.accept()
