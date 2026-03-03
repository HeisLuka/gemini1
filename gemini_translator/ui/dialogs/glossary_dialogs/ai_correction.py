
import io
import os
import time
import zipfile
import json
import re
from collections import defaultdict
import uuid # <--- ДОБАВИТЬ ЭТОТ ИМПОРТ
# --- Импорты из PyQt6 ---
from PyQt6 import QtWidgets, QtCore, QtGui
from PyQt6.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QPushButton, QDialogButtonBox, QLabel,
    QWidget, QGroupBox, QCheckBox, QHBoxLayout, QGridLayout, QTableWidget,
    QHeaderView, QTableWidgetItem, QMessageBox, QAbstractItemView
)
from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QFont

# --- Импорты из вашего проекта ---

from gemini_translator.ui.widgets.common_widgets import NoScrollSpinBox
from gemini_translator.ui.widgets.key_management_widget import KeyManagementWidget
from gemini_translator.ui.widgets.model_settings_widget import ModelSettingsWidget
from gemini_translator.ui.widgets.log_widget import LogWidget
from gemini_translator.ui.widgets.preset_widget import PresetWidget

# API и утилиты
from gemini_translator.api import config as api_config
from gemini_translator.utils.helpers import TokenCounter
from gemini_translator.utils.language_tools import LanguageDetector
from gemini_translator.utils.settings import SettingsManager

# --- Аннотация типа для избежания циклического импорта ---
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..glossary import MainWindow





class CorrectionSessionDialog(QDialog):
    """Минималистичный диалог для настройки сессии AI-коррекции."""
    correction_accepted = pyqtSignal(list)

    def __init__(self, settings_manager=None, parent=None):
        super().__init__(parent)
        
        if settings_manager is None:
            app = QtWidgets.QApplication.instance()
            if not hasattr(app, 'settings_manager'):
                raise RuntimeError("SettingsManager не был передан и не найден в экземпляре QApplication.")
            self.settings_manager = app.get_settings_manager()
        else:
            self.settings_manager = settings_manager
        
        self.setWindowTitle("Настройка AI-корректора")
        self.setMinimumWidth(1200)
        self.setMinimumHeight(700)
        
        # --- Геометрия окна ---
        available_geometry = self.screen().availableGeometry()
        
        height = min(int(available_geometry.height() * 0.75), 650)
        width = min(int(available_geometry.width() * 0.65), 1000)
        self.setMinimumSize(width, height)
       
       
        height = max(int(available_geometry.height() * 0.75), 650)
        width = max(int(available_geometry.width() * 0.65), 1000)
        
        self.resize(width, height)
        self.move(
            available_geometry.center().x() - self.width() // 2,
            available_geometry.center().y() - self.height() // 2
        )
        
        self.setWindowFlags(
            self.windowFlags() | 
            Qt.WindowType.WindowMaximizeButtonHint | 
            Qt.WindowType.WindowCloseButtonHint
        )
        
        # --- Состояние ---
        self._is_loaded = False
        self._ui_is_fully_loaded = False
        self.is_session_active = False
        self.partial_overlaps_included = False
        self.patterns_included = False # <-- НОВЫЙ ФЛАГ
        self._cached_analysis_results = None 
        self._cached_pattern_results = None # <-- НОВЫЙ КЭШ

        # Сохраняем доступ к анализатору из родительского окна
        main_window = self.parent()
        self.morph_analyzer = None
        if main_window and hasattr(main_window, 'morph_analyzer'):
            self.morph_analyzer = main_window.morph_analyzer

        self._init_base_ui()

        app = QtWidgets.QApplication.instance()
        if app:
            self.engine = app.engine
            
    def _update_start_button_state(self):
        if not self.is_session_active:
            has_keys = len(self.key_widget.get_active_keys()) > 0
            self.start_stop_btn.setEnabled(has_keys)
            # Dry run доступен, если UI загружен (данные готовы к сбору)
            self.dry_run_btn.setEnabled(self._ui_is_fully_loaded)
    
    def _init_base_ui(self):
        main_layout = QVBoxLayout(self)
        self.loading_label = QLabel("<h2>Загрузка компонентов…</h2>")
        self.loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(self.loading_label, 1)
        self.main_content_widget = QWidget()
        self.main_content_widget.setVisible(False)
        main_layout.addWidget(self.main_content_widget, 1)
        
        bottom_panel_layout = QHBoxLayout()
        # --- НОВОЕ: Кнопка пробного запуска ---
        self.dry_run_btn = QPushButton("🧪 Пробный запуск")
        self.dry_run_btn.clicked.connect(self.perform_dry_run)
        self.dry_run_btn.setEnabled(False)
        bottom_panel_layout.addWidget(self.dry_run_btn)
        
        bottom_panel_layout.addStretch()
        self.button_box = QDialogButtonBox()
        self.start_stop_btn = self.button_box.addButton("🚀 Запустить коррекцию", QDialogButtonBox.ButtonRole.ActionRole)
        self.cancel_close_btn = self.button_box.addButton("Отмена", QDialogButtonBox.ButtonRole.RejectRole)
        self.start_stop_btn.setEnabled(False)
        
        self.start_stop_btn.clicked.connect(self._on_start_stop_clicked)
        self.cancel_close_btn.clicked.connect(self.reject)
        
        bottom_panel_layout.addWidget(self.button_box)
        main_layout.addLayout(bottom_panel_layout)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._is_loaded:
            self._is_loaded = True
            QtCore.QTimer.singleShot(50, self._async_load_and_populate)


    def _async_load_and_populate(self):
        content_layout = QVBoxLayout(self.main_content_widget)
        
        # 1. Верхняя панель: Ключи
        self.key_widget = KeyManagementWidget(self.settings_manager, self)
        distribution_group = self.key_widget.findChild(QWidget, "distribution_group")
        if distribution_group:
            distribution_group.setVisible(False)
        content_layout.addWidget(self.key_widget)

        # 2. Средняя панель: Настройки модели + Лог
        middle_panel_layout = QHBoxLayout()
        self.model_settings_widget = ModelSettingsWidget(self)
        
        def safe_hide_widget(object_name):
            widget_to_hide = self.model_settings_widget.findChild(QWidget, object_name)
            if widget_to_hide:
                widget_to_hide.setVisible(False)
        safe_hide_widget("right_column_widget") 
        safe_hide_widget("rpm_row")
        safe_hide_widget("concurrent_row")
        middle_panel_layout.addWidget(self.model_settings_widget, 1)

        log_group = QGroupBox("Лог выполнения")
        log_group_layout = QVBoxLayout(log_group)
        self.log_widget = LogWidget(self)
        log_group_layout.addWidget(self.log_widget)
        middle_panel_layout.addWidget(log_group, 1)
        content_layout.addLayout(middle_panel_layout)
        
        # --- Нижняя панель (Промпт + Оптимизация СЛЕВА-НАПРАВО) ---
        prompt_settings_layout = QHBoxLayout()

        # 3. Левая колонка: Промпт
        self.prompt_widget = PresetWidget(
            parent=self,
            preset_name="Промпт коррекции",
            default_prompt_func=api_config.default_correction_prompt,
            load_presets_func=self.settings_manager.load_correction_prompts,
            save_presets_func=self.settings_manager.save_correction_prompts,
            get_last_text_func=self.settings_manager.get_last_correction_prompt_text,
            get_last_preset_func=self.settings_manager.get_last_correction_prompt_preset_name,
            save_last_preset_func=self.settings_manager.save_last_correction_prompt_preset_name
        )
        self.prompt_widget.load_last_session_state()
        prompt_settings_layout.addWidget(self.prompt_widget, 2)

        # 4. Правая колонка: Оптимизация
        optimization_group = QGroupBox("Оптимизация запроса")
        optimization_group.setObjectName("ai_correction_optimization_group")
        optimization_layout = QVBoxLayout(optimization_group) 
        
        # --- ТРИ ВКЛАДКИ ---
        opt_tabs = QtWidgets.QTabWidget()
        
        # === ВКЛАДКА 1: ДАННЫЕ (Адаптивная) ===
        tab_general = QWidget()
        layout_general = QVBoxLayout(tab_general)
        layout_general.setContentsMargins(5, 10, 5, 5)
        
        # Создаем сетку и сохраняем ссылку на неё
        self.data_grid_layout = QGridLayout()
        self.data_grid_layout.setColumnStretch(0, 1)
        self.data_grid_layout.setColumnStretch(1, 1)

        # Создаем чекбоксы (но пока не кладем в сетку)
        self.cb_context = QCheckBox("Весь глоссарий")
        self.cb_context.setToolTip("Если включено: отправляет весь глоссарий как контекст.\nЕсли выключено: отправляет только термины, связанные с проблемами.")
        self.cb_context.setChecked(True)

        self.cb_notes = QCheckBox("Примечания")
        self.cb_notes.setToolTip("Включать примечания к терминам (Notes).")
        self.cb_notes.setChecked(True)

        self.cb_direct = QCheckBox("Прямые конфликты")
        self.cb_direct.setToolTip("Одинаковый оригинал = Разные переводы.")
        self.cb_direct.setChecked(False)

        self.cb_reverse = QCheckBox("Обратные конфликты")
        self.cb_reverse.setToolTip("Разные оригиналы = Одинаковый перевод.")
        self.cb_reverse.setChecked(False)

        self.cb_overlaps = QCheckBox("Наложения")
        self.cb_overlaps.setToolTip("Проблемы вхождения одного термина в другой.")
        self.cb_overlaps.setChecked(False)
        
        layout_general.addLayout(self.data_grid_layout)
        layout_general.addStretch(1) # Пружина снизу
        opt_tabs.addTab(tab_general, "Данные")
        
        # === ВКЛАДКА 2: ПАТТЕРНЫ ===
        tab_patterns = QWidget()
        layout_patterns = QVBoxLayout(tab_patterns)
        layout_patterns.setContentsMargins(5, 10, 5, 5)
        
        pattern_grid = QGridLayout()
        
        self.toggle_patterns_btn = QPushButton("Включить анализ")
        self.toggle_patterns_btn.setToolTip("Найти группы с общей структурой.")
        self.toggle_patterns_btn.setCheckable(True)
        
        self.pattern_group_size_spinbox = NoScrollSpinBox(self)
        self.pattern_group_size_spinbox.setMinimum(2); self.pattern_group_size_spinbox.setMaximum(20); self.pattern_group_size_spinbox.setValue(3)

        pattern_grid.addWidget(self.toggle_patterns_btn, 0, 0, 1, 2)
        pattern_grid.addWidget(QLabel("Мин. группа:"), 1, 0)
        pattern_grid.addWidget(self.pattern_group_size_spinbox, 1, 1)
        
        self.cb_hierarchical_patterns = QCheckBox("Иерархическая структура")
        self.cb_hierarchical_patterns.setToolTip("Группировать термины внутри паттернов по подгруппам для лучшего контекста.")
        self.cb_hierarchical_patterns.setChecked(True)
        pattern_grid.addWidget(self.cb_hierarchical_patterns, 2, 0, 1, 2) 
        
        layout_patterns.addLayout(pattern_grid)
        layout_patterns.addStretch(1)

        opt_tabs.addTab(tab_patterns, "Паттерны")

        # === ВКЛАДКА 3: СКРЫТЫЕ ===
        tab_hidden = QWidget()
        layout_hidden = QVBoxLayout(tab_hidden)
        layout_hidden.setContentsMargins(5, 10, 5, 5)

        overlap_grid = QGridLayout()
        
        self.toggle_partial_btn = QPushButton("Включить анализ")
        self.toggle_partial_btn.setToolTip("Найти похожие оригиналы с разными переводами.")
        self.toggle_partial_btn.setCheckable(True)
        
        self.overlap_len_spinbox = NoScrollSpinBox(self)
        self.overlap_len_spinbox.setMinimum(1); self.overlap_len_spinbox.setMaximum(20)
        
        self.divergence_spinbox = NoScrollSpinBox(self)
        self.divergence_spinbox.setMinimum(5); self.divergence_spinbox.setMaximum(95); self.divergence_spinbox.setValue(30); self.divergence_spinbox.setSuffix(" %")

        overlap_grid.addWidget(self.toggle_partial_btn, 0, 0, 1, 2)
        overlap_grid.addWidget(QLabel("Мин. длина:"), 1, 0)
        overlap_grid.addWidget(self.overlap_len_spinbox, 1, 1)
        overlap_grid.addWidget(QLabel("Расхождение:"), 2, 0)
        overlap_grid.addWidget(self.divergence_spinbox, 2, 1)
        
        layout_hidden.addLayout(overlap_grid)
        layout_hidden.addStretch(1)
        opt_tabs.addTab(tab_hidden, "Скрытые")

        # Добавляем табы в layout группы
        optimization_layout.addWidget(opt_tabs)
        
        # Информация о токенах - ВНИЗУ
        self.token_info_label = QLabel("Расчет…")
        self.token_info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.token_info_label.setStyleSheet("font-weight: bold; font-size: 10pt; margin-top: 5px;")
        optimization_layout.addWidget(self.token_info_label)
        
        prompt_settings_layout.addWidget(optimization_group, 1)
        content_layout.addLayout(prompt_settings_layout, 1)

        # --- Подключение сигналов ---
        self.cb_context.stateChanged.connect(self.update_token_estimation)
        self.cb_notes.stateChanged.connect(self.update_token_estimation)
        self.cb_direct.stateChanged.connect(self.update_token_estimation)
        self.cb_reverse.stateChanged.connect(self.update_token_estimation)
        self.cb_overlaps.stateChanged.connect(self.update_token_estimation)
        self.cb_hierarchical_patterns.stateChanged.connect(self.update_token_estimation)
        
        self.toggle_partial_btn.clicked.connect(self._on_toggle_partial_overlaps)
        self.overlap_len_spinbox.valueChanged.connect(self._on_overlap_settings_changed)
        self.divergence_spinbox.valueChanged.connect(self._on_overlap_settings_changed)
        self.toggle_patterns_btn.clicked.connect(self._on_toggle_patterns)
        self.pattern_group_size_spinbox.valueChanged.connect(self._on_pattern_settings_changed)
        self.key_widget.active_keys_changed.connect(self._update_start_button_state)
        
        # Инициализация
        main_window = self.parent()
        if main_window and main_window.__class__.__name__ == 'MainWindow':
            glossary_sample = main_window.get_glossary()[:50]
            cjk_count = sum(1 for entry in glossary_sample if LanguageDetector.is_cjk_text(entry.get('original', '')))
            if glossary_sample and (cjk_count / len(glossary_sample) > 0.3):
                self.overlap_len_spinbox.setValue(2)
            else:
                self.overlap_len_spinbox.setValue(4)
        else:
            self.overlap_len_spinbox.setValue(4)

        self.key_widget.provider_combo.currentIndexChanged.emit(self.key_widget.provider_combo.currentIndex())
        self.loading_label.setVisible(False)
        self.main_content_widget.setVisible(True)
        
        # ЗАПУСКАЕМ АДАПТИВНУЮ КОМПОНОВКУ
        self._repack_data_tab_layout()
        self._update_start_button_state()
        
        app = QtWidgets.QApplication.instance()
        if app and hasattr(app, 'event_bus'):
            app.event_bus.event_posted.connect(self._on_global_event)
        
        self._ui_is_fully_loaded = True
        self.update_token_estimation()
    def perform_dry_run(self):
        """
        Запускает симуляцию процесса (Dry Run).
        Использует фейковый провайдер для проверки цикла запроса без траты токенов.
        """
        if self.is_session_active:
            return

        # 1. Готовим задачу (проверки, данные, промпт)
        settings = self._prepare_task_context()
        if not settings:
            return

        # 2. Подменяем настройки на Dry Run
        settings.update({
            'provider': 'dry_run', 
            'api_keys': ['dry_run_dummy_key'], 
            'num_instances': 1, 
            'rpm_limit': 1000
        })

        self.log_widget.clear()
        
        # 3. Активируем UI режим сессии
        self._set_session_active(True)
        self.dry_run_btn.setText("Обработка…")
        self.dry_run_btn.setEnabled(False) # Визуально блокируем

        # 4. Запускаем
        app = QtWidgets.QApplication.instance()
        app.event_bus.event_posted.emit({
            'event': 'start_session_requested',
            'source': 'CorrectionDialog',
            'data': {'settings': settings}
        })
        
    def _prepare_task_context(self):
        """
        Вспомогательный метод: собирает данные, проверяет лимиты, 
        генерирует промпт, создает виртуальный файл и ставит задачу в очередь.
        Возвращает dict settings, если все успешно, или None, если отмена/ошибка.
        """
        settings = self.get_settings()
        
        # --- Шаг 1: Подготовка данных ---
        data_for_ai, estimated_tokens, found_blocks, context_was_added, _, _, _ = self._get_data_and_estimate_tokens()
        if data_for_ai is None: 
            return None
        
        # --- Логика проверки контента ---
        is_full_context = self.cb_context.isChecked()
        if not is_full_context and not found_blocks:
            QMessageBox.information(self, "Нет проблем", 
                                    "Вы отключили отправку всего глоссария, но не выбрали ни одной категории проблем (или проблем нет).\n"
                                    "Данных для отправки нет.")
            return None

        if not context_was_added and not found_blocks:
            QMessageBox.warning(self, "Пусто", "Нет данных для отправки. Проверьте настройки или содержимое глоссария.")
            return None

        # --- Проверка токенов ---
        model_config = settings.get('model_config', {})
        SAFE_PROMPT_TOKEN_LIMIT = int(model_config.get("context_length", 128000) * 0.9)
        if estimated_tokens > SAFE_PROMPT_TOKEN_LIMIT:
            msg_box = QMessageBox(self)
            msg_box.setIcon(QMessageBox.Icon.Warning)
            msg_box.setWindowTitle("Запрос слишком велик")
            msg_box.setText(
                f"Расчетное количество токенов ({estimated_tokens:,}) превышает безопасный лимит ({SAFE_PROMPT_TOKEN_LIMIT:,})."
            )
            msg_box.setInformativeText(
                "Отправка такого большого запроса может привести к ошибке API или неполному результату.\n"
                "Вы уверены, что хотите продолжить?"
            )
            continue_button = msg_box.addButton("Все равно продолжить", QMessageBox.ButtonRole.DestructiveRole)
            cancel_button = msg_box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
            msg_box.setDefaultButton(cancel_button)
            msg_box.exec()
            
            if msg_box.clickedButton() != continue_button:
                return None

        # --- Сборка промпта и задачи ---
        final_prompt_template = self._build_final_prompt(found_blocks, context_was_added)
        settings['glossary_generation_prompt'] = final_prompt_template
        
        if not self.engine.task_manager:
            QMessageBox.warning(self, "Критическая ошибка", "TaskManager не доступен.")
            return None

        VIRTUAL_CHAPTER_PATH = "correction_data.txt"
        try:
            virtual_epub_path = self._create_virtual_epub(data_for_ai, VIRTUAL_CHAPTER_PATH)
        except Exception as e:
            QMessageBox.critical(self, "Ошибка создания данных", f"Не удалось подготовить виртуальный файл:\n{e}")
            return None
        
        task = ('glossary_batch_task', virtual_epub_path, (VIRTUAL_CHAPTER_PATH,))
        
        # Очищаем очередь и добавляем задачу
        self.engine.task_manager.clear_all_queues()
        self.engine.task_manager.clear_glossary_results()
        self.engine.task_manager.add_pending_tasks([task])

        return settings

    def _repack_data_tab_layout(self):
        """
        Адаптивно перестраивает сетку чекбоксов во вкладке 'Данные'.
        Скрытые (пустые) категории не занимают место в layout.
        """
        main_window = self.parent()
        if not main_window or main_window.__class__.__name__ != 'MainWindow': 
            return

        # 1. Определяем, какие виджеты должны быть показаны
        widgets_to_show = []
        
        # Context и Notes всегда доступны, если глоссарий не пуст (но покажем всегда для простоты управления)
        widgets_to_show.append(self.cb_context)
        widgets_to_show.append(self.cb_notes)

        # Проверяем наличие данных для остальных
        if main_window.direct_conflicts:
            widgets_to_show.append(self.cb_direct)
        else:
            self.cb_direct.setChecked(False) # Сбрасываем, чтобы не влияло на расчет

        if main_window.reverse_issues:
            widgets_to_show.append(self.cb_reverse)
        else:
            self.cb_reverse.setChecked(False)

        has_overlaps = (len(main_window.overlap_groups) > 0) or (len(main_window.inverted_overlaps) > 0)
        if has_overlaps:
            widgets_to_show.append(self.cb_overlaps)
        else:
            self.cb_overlaps.setChecked(False)

        # 2. Очищаем текущую сетку (удаляем элементы из Layout, но не удаляем сами объекты виджетов)
        # Обратный цикл нужен, чтобы корректно удалять по индексу
        for i in reversed(range(self.data_grid_layout.count())):
            item = self.data_grid_layout.itemAt(i)
            if item.widget():
                item.widget().setParent(None) # Визуально извлекаем виджет (он остается в памяти self.cb_...)

        # 3. Заполняем сетку заново (2 колонки)
        columns = 2
        for index, widget in enumerate(widgets_to_show):
            row = index // columns
            col = index % columns
            self.data_grid_layout.addWidget(widget, row, col)
            widget.setVisible(True) # Убеждаемся, что он видим
 
 
    def refresh_data(self):
        """
        Публичный метод для принудительного обновления данных из родительского окна.
        Сбрасывает кэши и перезапускает анализ токенов.
        """
        self.log_widget.append_message({'message': "[SYSTEM] Данные обновлены из основного окна. Пересчет..."})
        self._cached_analysis_results = None
        self._cached_pattern_results = None
        self._reset_partial_overlap_button()
        self._reset_pattern_button()
        self._repack_data_tab_layout()
        self.update_token_estimation()
    
    
    def _get_data_and_estimate_tokens(self):
        main_window = self.parent()
        if not main_window or main_window.__class__.__name__ != 'MainWindow': 
            return None, 0, None, False, 0, 0, 0
    
        # Сброс реестра усыновленных терминов
        self.adopted_terms_registry = set()

        # --- Шаг 1: Сбор базовых данных ---
        all_direct = main_window.direct_conflicts
        all_reverse = main_window.reverse_issues
        all_overlaps = main_window.overlap_groups
        all_inv_overlaps = main_window.inverted_overlaps
        
        cached_patterns = (self._cached_pattern_results or {}) if self.patterns_included else {}
        cached_hidden_data = (self._cached_analysis_results or {}) if self.partial_overlaps_included else {}
    
        processed_terms = set()
        found_blocks = {}
    
        # 2.1 Прямые
        if self.cb_direct.isChecked() and all_direct:
            conflict_terms = sorted(list(all_direct.keys()))
            if conflict_terms:
                found_blocks['direct'] = conflict_terms
                processed_terms.update(conflict_terms)
        
        # 2.2 Обратные
        if self.cb_reverse.isChecked() and all_reverse:
            rev_terms = set()
            for data in all_reverse.values():
                rev_terms.update(e['original'] for e in data.get('complete', []))
            
            if rev_terms:
                found_blocks['reverse'] = sorted(list(rev_terms))
                processed_terms.update(rev_terms)
    
        # === 2.3 НАЛОЖЕНИЯ (Подготовка) ===
        
        final_overlap_blocks = []
        mobile_overlaps = [] 
        
        if self.cb_overlaps.isChecked():
            # 1. Получаем отсортированные группы
            raw_overlap_blocks = self._analyze_overlaps_complex(all_overlaps, all_inv_overlaps, processed_terms)
            
            # 2. Разделяем: Связанные (остаются) vs Мобильные (кандидаты в паттерны)
            connected_overlaps = []
            
            if raw_overlap_blocks:
                for i, block in enumerate(raw_overlap_blocks):
                    is_entangled = False
                    my_cluster = block['full_cluster']
                    
                    for j, other in enumerate(raw_overlap_blocks):
                        if i == j: continue
                        # Если есть пересечение с другой НЕ пустой группой
                        if not my_cluster.isdisjoint(other['full_cluster']) and len(other['unique_terms']) > 0:
                            is_entangled = True
                            break
                    
                    if is_entangled:
                        connected_overlaps.append(block)
                    else:
                        mobile_overlaps.append(block)
            
            # 3. Добавляем связанные сразу (они слишком запутаны для паттернов)
            for block in connected_overlaps:
                final_overlap_blocks.append(block)
                processed_terms.update(block['unique_terms'])
        
        # === 2.4 ПАТТЕРНЫ (Complex Logic) ===
        actual_pattern_count = 0
        
        if cached_patterns:
            # Вызываем новый метод для анализа, балансировки и сортировки
            # Он изменяет mobile_overlaps (удаляет поглощенные) и обновляет adopted_terms_registry
            ordered_patterns = self._analyze_patterns_complex(cached_patterns, mobile_overlaps, processed_terms)
            
            if ordered_patterns:
                found_blocks['patterns'] = ordered_patterns
                actual_pattern_count = len(ordered_patterns)
                
                # Обновляем processed_terms
                for members in ordered_patterns.values():
                    processed_terms.update(members)
        
        # 2.5 Остатки мобильных (те, что не вошли ни в один паттерн)
        if mobile_overlaps:
            for mob in mobile_overlaps:
                # Перепроверяем уникальность, так как паттерны могли забрать часть терминов
                mob['unique_terms'] = [t for t in mob['unique_terms'] if t not in processed_terms]
                if mob['unique_terms']:
                    final_overlap_blocks.append(mob)
                    processed_terms.update(mob['unique_terms'])
        
        # Финализируем наложения (если остались)
        if final_overlap_blocks:
            # Еще раз сортируем, так как состав мог измениться
            found_blocks['overlaps'] = self._sort_groups_by_gravity(final_overlap_blocks)
        
        # 2.6 Скрытые (без изменений)
        actual_hidden_count = 0
        actual_neighbors_count = 0
        raw_hidden_conflicts = set()
        raw_context_neighbors = set()
        
        if cached_hidden_data:
            threshold = self.divergence_spinbox.value() / 100.0
            for group_data in cached_hidden_data.values():
                raw_context_neighbors.update(group_data['terms'])
                divergence = abs(group_data['original_dossier']['universal_similarity'] - group_data['translation_dossier']['universal_similarity'])
                if divergence > threshold: 
                    raw_hidden_conflicts.update(group_data['terms'])
    
        final_hidden_conflicts = sorted(list(raw_hidden_conflicts - processed_terms))
        
        if final_hidden_conflicts and self.partial_overlaps_included:
            found_blocks['hidden'] = []
            hidden_graph = defaultdict(set)
            hidden_set = set(final_hidden_conflicts)
            
            for data in cached_hidden_data.values():
                terms = list(data['terms'])
                if len(terms) == 2 and terms[0] in hidden_set and terms[1] in hidden_set:
                    hidden_graph[terms[0]].add(terms[1])
                    hidden_graph[terms[1]].add(terms[0])
            
            visited = set()
            for term in final_hidden_conflicts:
                if term not in visited:
                    component = []
                    q = [term]
                    visited.add(term)
                    while q:
                        node = q.pop(0)
                        component.append(node)
                        for neighbor in hidden_graph[node]:
                            if neighbor not in visited:
                                visited.add(neighbor)
                                q.append(neighbor)
                    found_blocks['hidden'].append(sorted(component))
            
            actual_hidden_count = len(final_hidden_conflicts)
            processed_terms.update(final_hidden_conflicts)
            
        final_neighbors_set = raw_context_neighbors - processed_terms
        actual_neighbors_count = len(final_neighbors_set)
    
        # --- Шаг 3: Вывод ---
        raw_glossary = main_window.get_glossary()
        
        if self.cb_context.isChecked():
            glossary_to_format = [e for e in raw_glossary if e.get('original') not in processed_terms]
        else:
            glossary_to_format = [e for e in raw_glossary if e.get('original') in final_neighbors_set]
        
        glossary_multimap = defaultdict(list)
        for e in raw_glossary:
            if e.get('original'):
                glossary_multimap[e['original']].append(e)
        
        include_notes = self.cb_notes.isChecked()
        output_lines = []
        context_was_added = False

        if glossary_to_format:
            output_lines.append("\n--- GLOSSARY CONTEXT ---\n")
            output_lines.extend(self._format_compact_group(
                [e['original'] for e in glossary_to_format], glossary_multimap, include_notes
            ))
            context_was_added = True

        if found_blocks.get('direct'):
            output_lines.append("\n--- DIRECT CONFLICTS ---\n")
            for term in found_blocks['direct']:
                lines = self._format_compact_group([term], glossary_multimap, include_notes)
                if lines:
                    output_lines.extend(lines)
                    output_lines.append("")

        if found_blocks.get('reverse'):
            output_lines.append("\n--- REVERSE CONFLICTS ---\n")
            rev_map_by_trans = defaultdict(list)
            for term in found_blocks['reverse']:
                entries = glossary_multimap.get(term, [])
                for entry in entries:
                    rus = entry.get('rus', '').strip()
                    if rus: rev_map_by_trans[rus].append(term)
            for rus_key in sorted(rev_map_by_trans.keys()):
                lines = self._format_compact_group(sorted(list(set(rev_map_by_trans[rus_key]))), glossary_multimap, include_notes)
                if lines:
                    output_lines.extend(lines)
                    output_lines.append("")
        
        if found_blocks.get('overlaps'):
            output_lines.append("\n--- OVERLAPS ---\n")
            
            overlap_list = found_blocks['overlaps']
            for i, group in enumerate(overlap_list):
                if i > 0: output_lines.append("")
                output_lines.append(f'--- "{group["leader"]}" ---')
                
                matches = group.get('matches', {})
                for term in group['unique_terms']:
                    lines = self._format_compact_group([term], glossary_multimap, include_notes)
                    output_lines.extend(lines)
                    
                    if term in matches:
                        children = sorted(matches[term])
                        children_lines = self._format_compact_group(children, glossary_multimap, include_notes)
                        for child_line in children_lines:
                            output_lines.append(f"> {child_line}")

        if found_blocks.get('patterns'):
            output_lines.append("\n--- PATTERNS ---\n")
            # found_blocks['patterns'] теперь OrderedDict отсортированный по гравитации
            if hasattr(self, 'cb_hierarchical_patterns') and self.cb_hierarchical_patterns.isChecked():
                for p, m in found_blocks['patterns'].items():
                    output_lines.extend(self._format_hierarchical_pattern_block(p, m, glossary_multimap, include_notes))
            else:
                for p, m in found_blocks['patterns'].items():
                    realized_p = self._determine_realized_pattern(p, m)
                    output_lines.append(f'\n--- Pattern: "{realized_p}" ---')
                    output_lines.extend(self._format_compact_group(self._sort_members_with_leader(m, p), glossary_multimap, include_notes))

        if found_blocks.get('hidden'):
            output_lines.append("\n--- HIDDEN CONFLICTS ---\n")
            for i, comp in enumerate(found_blocks['hidden']):
                output_lines.append(f'--- Group {i+1} ---')
                output_lines.extend(self._format_compact_group(comp, glossary_multimap, include_notes))

        data_as_free_text = "\n".join(output_lines)
        data_as_free_text = re.sub(r'\n---\s*\n\s*---\n', r'\n---\n', data_as_free_text)
        data_as_free_text = re.sub(r'\n{3,}', r'\n\n', data_as_free_text)
        # if found_blocks.get('patterns'):
            # print(data_as_free_text)
        estimated_tokens = TokenCounter().estimate_tokens(data_as_free_text)
        
        return (data_as_free_text, estimated_tokens, found_blocks, context_was_added,
                actual_hidden_count, actual_neighbors_count, actual_pattern_count)

                

    def _analyze_overlaps_complex(self, all_overlaps, all_inv_overlaps, processed_terms):
        """
        Продвинутый анализ:
        1. Сортировка кандидатов.
        2. Формирование групп с одновременным "усыновлением" сирот.
        3. Гравитационная сортировка итоговых групп.
        """
        groups_source = all_overlaps if len(all_overlaps) < len(all_inv_overlaps) else all_inv_overlaps
        if not groups_source: return []

        # 1. Подготовка кандидатов
        candidates = []
        for leader, members in groups_source.items():
            cluster = sorted(list(set([leader] + members)))
            score = len(leader) * len(cluster)
            candidates.append({
                'leader': leader,
                'full_cluster': set(cluster),
                'score': score
            })
        
        # Сортируем: сначала самые жирные и важные группы
        candidates.sort(key=lambda x: x['score'], reverse=True)
        
        final_groups = []
        
        # 2. Жадное формирование + Усыновление
        for cand in candidates:
            # Вычисляем уникальные термины (вычитаем глобально обработанные)
            unique_terms = [t for t in sorted(list(cand['full_cluster'])) if t not in processed_terms]
            
            if not unique_terms:
                continue

            # Проверка на сиротство
            is_orphan = (len(unique_terms) == 1)
            adopted = False
            
            if is_orphan:
                orphan = unique_terms[0]
                
                # Ищем родителя среди УЖЕ созданных групп
                for group in final_groups:
                    # Критерий: сирота является частью какого-то термина в группе
                    # или входит в полный кластер группы (родственные связи)
                    
                    target_parent_term = None
                    
                    # А. Проверка через полный кластер (самая надежная связь)
                    if orphan in group['full_cluster']:
                        # Привязываем к лидеру или первому термину
                        target_parent_term = group['unique_terms'][0]
                    
                    # Б. Проверка подстроки среди уникальных терминов (визуальная связь)
                    if not target_parent_term:
                        for term in group['unique_terms']:
                            if orphan in term: # Mandara содержит Manda
                                target_parent_term = term
                                break
                    
                    # Если нашли родителя
                    if target_parent_term:
                        if 'matches' not in group: group['matches'] = defaultdict(list)
                        group['matches'][target_parent_term].append(orphan)
                        
                        # Добавляем в полный кластер группы для будущей гравитации
                        group['full_cluster'].add(orphan)
                        # Добавляем в список "усыновленных" для учета веса
                        if 'adopted_list' not in group: group['adopted_list'] = set()
                        group['adopted_list'].add(orphan)
                        
                        processed_terms.add(orphan)
                        adopted = True
                        break # Сирота пристроен, дальше не ищем
            
            if not adopted:
                # Создаем новую полноценную группу
                new_group = {
                    'leader': cand['leader'],
                    'unique_terms': unique_terms,
                    'full_cluster': cand['full_cluster'], # Копия множества
                    'score': cand['score'],
                    'matches': defaultdict(list), # {parent_term: [orphans]}
                    'adopted_list': set()
                }
                final_groups.append(new_group)
                processed_terms.update(unique_terms)

        # 3. Отправляем на гравитационную сортировку
        return self._sort_groups_by_gravity(final_groups)
        
    def _analyze_overlaps_with_gravity(self, all_overlaps, all_inv_overlaps, processed_terms):
        """
        Сложная логика обработки наложений:
        1. Расчет веса (Score).
        2. Жадное вычитание (получение уникальных 'остатков').
        3. Гравитационная сортировка (сближение связанных групп).
        """
        # --- 1. Подготовка кандидатов ---
        groups_source = all_overlaps if len(all_overlaps) < len(all_inv_overlaps) else all_inv_overlaps
        if not groups_source:
            return []

        candidates = []
        for leader, members in groups_source.items():
            # Полный кластер (все участники группы)
            cluster = sorted(list(set([leader] + members)))
            # Score = Длина лидера * Размер группы (чем больше и длиннее, тем важнее)
            score = len(leader) * len(cluster)
            candidates.append({
                'leader': leader,
                'full_cluster': set(cluster), # Для расчета связей
                'score': score
            })
        
        # Сортируем по убыванию важности (первичная сортировка)
        candidates.sort(key=lambda x: x['score'], reverse=True)
        
        # --- 2. Жадное вычитание (формирование блоков) ---
        valid_groups = []
        
        for cand in candidates:
            # Вычисляем уникальные термины (которые еще не были обработаны)
            unique_terms = [t for t in sorted(list(cand['full_cluster'])) if t not in processed_terms]
            
            if not unique_terms:
                continue
                
            # Регистрируем группу
            group_data = {
                'leader': cand['leader'],
                'unique_terms': unique_terms,    # То, что будем выводить
                'full_cluster': cand['full_cluster'], # То, по чему будем искать связи
                'score': cand['score']
            }
            valid_groups.append(group_data)
            processed_terms.update(unique_terms)
            
        # --- 3. Гравитационная сортировка ---
        return self._sort_groups_by_gravity(valid_groups)

    # --- Методы для управления СКРЫТЫМИ КОНФЛИКТАМИ ---
    def _reset_partial_overlap_button(self):
        # --- НОВОЕ: Сначала инвалидируем кэш ---
        self._cached_analysis_results = None
        
        self.partial_overlaps_included = False
        self.toggle_partial_btn.setChecked(False)
        self.toggle_partial_btn.setText("Анализ скрытых конфликтов")
        self.toggle_partial_btn.setStyleSheet("")
        self.update_token_estimation()

    def _on_overlap_settings_changed(self):
        """При любом изменении настроек - принудительно сбрасываем состояние анализа."""
        self._reset_partial_overlap_button()
    
    def _post_event(self, name: str, data: dict = None):
        """
        Просто и безопасно отправляет событие в шину из любого потока.
        Qt сам позаботится о межпоточной доставке.
        """
        app = QtWidgets.QApplication.instance()
        if app and hasattr(app, 'event_bus'):
            session_id = self.engine.session_id if self.engine and self.engine.session_id else None
            event = {
                'event': name,
                'source': f'CorrectionSessionDialog',
                'session_id': session_id,
                'data': data or {}
            }
            # Просто вызываем emit. Qt сделает все остальное.
            app.event_bus.event_posted.emit(event)
        
    def _on_toggle_partial_overlaps(self):
        # Если UI еще не загружен, ничего не делаем
        if not hasattr(self, 'toggle_partial_btn'): return

        cache_was_invalid = self._cached_analysis_results is None
        if cache_was_invalid:
            self.toggle_partial_btn.setText("Анализ…")
            self.toggle_partial_btn.setEnabled(False)
            QApplication.processEvents()
            # Вызываем "безголовый" метод для реальной работы
            self._run_partial_overlap_analysis()
            self.toggle_partial_btn.setEnabled(True)
        
        # Управляем состоянием ВКЛ/ВЫКЛ
        self.partial_overlaps_included = not self.partial_overlaps_included if not cache_was_invalid else True
        self.update_token_estimation()

    def _on_toggle_patterns(self):
        # Если UI еще не загружен, ничего не делаем
        if not hasattr(self, 'toggle_patterns_btn'): return

        cache_was_invalid = self._cached_pattern_results is None
        if cache_was_invalid:
            self.toggle_patterns_btn.setText("Анализ…")
            self.toggle_patterns_btn.setEnabled(False)
            QApplication.processEvents()
            # Вызываем "безголовый" метод для реальной работы
            self._run_pattern_analysis()
            self.toggle_patterns_btn.setEnabled(True)
        
        # Управляем состоянием ВКЛ/ВЫКЛ
        self.patterns_included = not self.patterns_included if not cache_was_invalid else True
        self.update_token_estimation()


    def _run_partial_overlap_analysis(self):
        """Безопасный 'безголовый' метод для запуска анализа скрытых конфликтов."""
        if self._cached_analysis_results is None:
            main_window = self.parent()
            if not main_window or main_window.__class__.__name__ != 'MainWindow': return
            
            # --- ИЗМЕНЕНИЕ: Не собираем base_conflicts, передаем пустой set() ---
            # Мы хотим найти ВСЕ возможные скрытые конфликты в кэш.
            # Фильтрация будет происходить динамически в _get_data_and_estimate_tokens
            
            min_len = self.overlap_len_spinbox.value() if hasattr(self, 'overlap_len_spinbox') else 4
            self._cached_analysis_results = main_window.logic.find_partial_overlaps(
                main_window.get_glossary(), 
                set(), # <-- ПУСТОЙ НАБОР ИСКЛЮЧЕНИЙ
                main_window.chinese_processor, 
                min_overlap_len=min_len
            )
        self.partial_overlaps_included = True

    def _run_pattern_analysis(self):
        """Безопасный 'безголовый' метод для запуска анализа паттернов."""
        if self._cached_pattern_results is None:
            main_window = self.parent()
            if not main_window or main_window.__class__.__name__ != 'MainWindow': return
            
            min_size = self.pattern_group_size_spinbox.value() if hasattr(self, 'pattern_group_size_spinbox') else 3
            
            # ИСПОЛЬЗУЕМ НОВЫЙ МЕТОД
            # Он сразу вернет очищенные, схлопнутые и красивые паттерны
            self._cached_pattern_results = main_window.logic.analyze_patterns_smart(
                main_window.get_glossary(), 
                existing_conflicts_set=set(), 
                min_group_size=min_size
            )
        self.patterns_included = True
        

    def _reset_pattern_button(self):
        # --- НОВОЕ: Сначала инвалидируем кэш ---
        self._cached_pattern_results = None
    
        self.patterns_included = False
        self.toggle_patterns_btn.setChecked(False)
        self.toggle_patterns_btn.setText("Анализ паттернов")
        self.toggle_patterns_btn.setStyleSheet("")
        self.update_token_estimation()

    def _on_pattern_settings_changed(self):
        """При любом изменении настроек - принудительно сбрасываем состояние анализа."""
        self._reset_pattern_button()


    def update_token_estimation(self):
        if not self._ui_is_fully_loaded:
            return
        # Распаковываем все 7 значений
        data, tokens, _, _, hidden_count, neighbors_count, pattern_count = self._get_data_and_estimate_tokens()
        if data is None: return

        # --- НАЧАЛО ИСПРАВЛЕНИЯ ---
        # Определяем стиль для активной кнопки ОДИН РАЗ, чтобы не повторяться
        # Используем селектор 'QPushButton', чтобы стиль не "протекал" в QToolTip
        active_style = """
            QPushButton {
                background-color: #2ECC71;
                color: white;
            }
        """
        # --- КОНЕЦ ИСПРАВЛЕНИЯ ---
    
        # Обновляем кнопку паттернов
        if self.patterns_included:
            self.toggle_patterns_btn.setText(f"✓ Включено ({pattern_count} паттернов)")
            self.toggle_patterns_btn.setStyleSheet(active_style) # <-- Применяем правильный стиль
        else:
            self.toggle_patterns_btn.setText("Анализ паттернов")
            self.toggle_patterns_btn.setStyleSheet("")
    
        # Обновляем кнопку скрытых конфликтов
        if self.partial_overlaps_included:
            self.toggle_partial_btn.setText(f"✓ Включено (Конфл: {hidden_count}, Сосед: {neighbors_count})")
            self.toggle_partial_btn.setStyleSheet(active_style) # <-- Применяем правильный стиль
        else:
            self.toggle_partial_btn.setText("Анализ скрытых конфликтов")
            self.toggle_partial_btn.setStyleSheet("")
                
        settings = self.get_settings()
        model_name = settings.get('model_config', {}).get('name', 'unknown')
        model_config = api_config.all_models().get(model_name, {})
        limit = int(model_config.get("context_length", 128000) * 0.9)
    
        self.token_info_label.setText(f"Запрос: <b>{tokens:,}</b> / {limit:,} токенов")
        self.token_info_label.setStyleSheet("color: red;" if tokens > limit else "color: green;")
        self._update_start_button_state()
    
    def _on_start_stop_clicked(self):
        if self.is_session_active:
            # Логика остановки
            app = QtWidgets.QApplication.instance()
            if app and hasattr(app, 'event_bus'):
                session_id = self.engine.session_id if self.engine and self.engine.session_id else None
                app.event_bus.event_posted.emit({
                    'event': 'manual_stop_requested', 'source': 'CorrectionDialog', 'session_id': session_id
                })
                self.start_stop_btn.setText("Остановка…")
                self.start_stop_btn.setEnabled(False)
        else:
            # Логика ЗАПУСКА
            if not self.key_widget.get_active_keys():
                QMessageBox.warning(self, "Нет ключей", "Не выбрано ни одного активного API ключа.")
                return
            
            # 1. Готовим задачу через общий метод
            settings = self._prepare_task_context()
            if not settings:
                return

            # 2. Сохраняем пресеты (только при реальном запуске)
            self.settings_manager.save_last_correction_prompt_text(self.prompt_widget.get_prompt())
            self.settings_manager.save_last_correction_prompt_preset_name(self.prompt_widget.get_current_preset_name())
    
            self._set_session_active(True) 
            
            # 3. Запускаем
            app = QtWidgets.QApplication.instance()
            app.event_bus.event_posted.emit({
                'event': 'start_session_requested',
                'source': 'CorrectionDialog',
                'data': {'settings': settings}
            })
    
    def _determine_realized_pattern(self, pattern_str, members):
        """
        Определяет, как лучше отобразить заголовок паттерна:
        слитно ("Fireball") или раздельно ("Fire Ball"),
        основываясь на реальных терминах в группе.
        """
        # Варианты написания
        spaced = pattern_str
        merged = pattern_str.replace(" ", "")
        
        # 1. Приоритет: Полное совпадение (термин == паттерну)
        # Ищем, есть ли в группе термин, который совпадает с одним из вариантов (case-insensitive)
        for m in members:
            m_lower = m.lower()
            if m_lower == merged.lower():
                return m # Возвращаем реальное написание (например "Fireball")
            if m_lower == spaced.lower():
                return m # Возвращаем реальное написание (например "Fire Ball")

        # 2. Если полных совпадений нет, считаем частотность вхождения как подстроки
        count_spaced = 0
        count_merged = 0
        
        for m in members:
            m_lower = m.lower()
            if merged.lower() in m_lower: count_merged += 1
            elif spaced.lower() in m_lower: count_spaced += 1
            
        # Если слитное написание встречается чаще или равно (при условии наличия) — используем его
        if count_merged > 0 and count_merged >= count_spaced:
            return merged
            
        return spaced
    
    def _analyze_patterns_complex(self, cached_patterns, mobile_overlaps, processed_terms):
        """
        Продвинутый анализ паттернов 11.0 (Super Glue Gravity):
        Усиленная гравитация для родственных заголовков, чтобы избежать разрывов контекста.
        """
        main_window = self.parent()
        if not main_window or not cached_patterns:
            return []

        # --- ЭТАП 0: Подготовка сырых данных ---
        raw_groups = {}
        for pat_str, members in cached_patterns.items():
            valid_members = {m for m in members if m not in processed_terms}
            if not valid_members: continue

            raw_groups[pat_str] = {
                'leader': pat_str,
                'visual_members': set(),       
                'gravity_pool': valid_members.copy(), 
                'adopted': set(),
                'len_leader': len(pat_str),
                'initial_size': len(valid_members),
                'clean_leader': pat_str.replace(" ", "").lower(),
                'gravity_score': 0, 
                'internal_score': (len(pat_str) ** 2.0) * len(valid_members) # Усиливаем вес длины заголовка
            }

        # --- ЭТАП 1: Построение Глобальной Гравитации (Super Glue) ---
        all_keys = list(raw_groups.keys())
        connections = defaultdict(float) 

        for i in range(len(all_keys)):
            key1 = all_keys[i]
            g1 = raw_groups[key1]
            l1 = g1['clean_leader']
            
            for j in range(i + 1, len(all_keys)):
                key2 = all_keys[j]
                g2 = raw_groups[key2]
                l2 = g2['clean_leader']
                
                # Базовый вес: Пересечение терминов
                common = g1['gravity_pool'].intersection(g2['gravity_pool'])
                weight = sum(len(t) for t in common)
                
                # --- SUPER GLUE: Бонусы за родство заголовков ---
                
                # 1. Прямое вхождение (Parent-Child)
                # "Fire" in "Fireball" -> x10
                if l1 in l2 or l2 in l1:
                    weight += 50.0 # Огромный константный бонус, чтобы гарантировать соседство
                    weight *= 5.0  # И мультипликатор для масштаба
                
                # 2. Общий суффикс/префикс (Siblings)
                # "Fireball" и "Firestorm" (общий "Fire")
                # Это сложно проверить быстро без токенизации, но можно проверить перекрытие множеств
                elif common:
                    # Если у групп много общих терминов (более 30% от меньшей группы)
                    min_size = min(len(g1['gravity_pool']), len(g2['gravity_pool']))
                    if len(common) > min_size * 0.3:
                         weight *= 3.0
                
                if weight > 0:
                    g1['gravity_score'] += weight
                    g2['gravity_score'] += weight
                    connections[frozenset([key1, key2])] = weight

        # --- ЭТАП 2: Поглощение Наложений (без изменений) ---
        term_to_orphans_map = defaultdict(set)
        if mobile_overlaps:
            all_pattern_terms = set()
            for g in raw_groups.values():
                all_pattern_terms.update(g['gravity_pool'])

            for mob in list(mobile_overlaps):
                if set(mob['unique_terms']).issubset(all_pattern_terms):
                    mobile_overlaps.remove(mob)
                    if 'matches' in mob:
                        for parent, children in mob['matches'].items():
                            term_to_orphans_map[parent].update(children)
                            self.adopted_terms_registry.update(children)

        # --- ЭТАП 3: THE SOLVER (Dictator Logic) ---
        term_conflicts = defaultdict(list)
        for key, g in raw_groups.items():
            for term in g['gravity_pool']:
                term_conflicts[term].append(key)

        for term, contenders in term_conflicts.items():
            if len(contenders) == 1:
                self._assign_term_to_group(contenders[0], term, raw_groups, term_to_orphans_map)
                continue

            # Сортируем по длине заголовка (Desc)
            candidates = [raw_groups[k] for k in contenders]
            candidates.sort(key=lambda x: x['len_leader'], reverse=True)
            
            # Ищем "Дом" (полное совпадение)
            home_candidate = None
            term_clean = term.replace(" ", "").lower()
            for cand in candidates:
                if term_clean == cand['clean_leader']:
                    home_candidate = cand
                    break
            
            if home_candidate:
                self._assign_term_to_group(home_candidate['leader'], term, raw_groups, term_to_orphans_map)
                continue

            # --- НОВАЯ ЛОГИКА ВЕРСИИ 3.0: Прямой выбор лучшего кандидата ---
            # Вместо сложной логики "свержения" мы сразу выбираем лучшего кандидата
            # из ВСЕХ претендентов по вашему набору правил.
            
            # ВАЖНО: 'candidates' уже отсортированы по длине заголовка, 
            # но мы пересортируем их по более сложному ключу для финального выбора.
            
            final_winner = max(
                candidates, # Используем ВЕСЬ список претендентов
                key=lambda x: (
                    # 1. ПРИОРИТЕТ: Специфичность (Длина заголовка / Размер группы).
                    #    Чем выше это значение, тем более "экспертной" является группа.
                    (x['len_leader'] / (len(x['gravity_pool']) or 1)),
                    
                    # 2. При равной специфичности - у кого длиннее заголовок.
                    x['len_leader'],
                    
                    # 3. При прочих равных - у кого больше группа.
                    len(x['gravity_pool']),
                    
                    # 4. Финальный критерий для стабильности - алфавитный порядок.
                    #    Используем отрицание для сортировки от 'z' к 'a'.
                    -ord(x['clean_leader'][0]) if x['clean_leader'] else 0
                )
            )

            self._assign_term_to_group(final_winner['leader'], term, raw_groups, term_to_orphans_map)

        # --- ЭТАП 4: Финализация ---
        final_groups_list = []
        for grp in raw_groups.values():
            visual_count = len(grp['visual_members']) + len(grp['adopted'])
            if len(grp['visual_members']) >= 2 or (grp['visual_members'] and visual_count >= 2):
                grp['final_score'] = grp['internal_score']
                final_groups_list.append(grp)

        if not final_groups_list:
            return {}

        # --- ЭТАП 5: Сортировка ---
        sorted_groups = self._sort_by_iterative_rank_shifting(final_groups_list, connections, iterations=100)
        
        result_ordered_dict = {}
        for grp in sorted_groups:
            result_ordered_dict[grp['leader']] = sorted(list(grp['visual_members']))
            
        return result_ordered_dict




    def _sort_by_iterative_rank_shifting(self, groups, connections, iterations=20):
        """
        Сортировка V7.6 (Hybrid: Magnetic Mooring + Advanced Surgery):
        Сочетает два этапа:
        1. Основной (Magnetic Mooring): Создает плотные кластеры путем "швартовки"
           групп к их ближайшим уже размещенным родственникам.
        2. Периодическая "Хирургическая" сессия, включающая два приема:
           a) Long-Distance Castling: Исправляет крупные разрывы [A, X, B].
           b) Triangle Flip: Находит "мостовые" группы и ставит их на место.
              Если в последовательности [A, B, C] группа C связана и с A, и с B,
              но A и B не связаны между собой, происходит рокировка -> [A, C, B].
              Это минимизирует локальную энтропию.
        """
        if not groups:
            return []

        # 1. Предварительная сортировка
        group_connections_count = defaultdict(int)
        for key_pair in connections:
            k1, k2 = list(key_pair)
            group_connections_count[k1] += 1
            group_connections_count[k2] += 1
        
        current_order = sorted(
            groups,
            key=lambda g: (
                -group_connections_count.get(g['leader'], 0),
                -(g['len_leader'] * len(g['visual_members'])),
                g['clean_leader']
            )
        )

        starting_time = time.time()
        for i in range(iterations):
            n = len(current_order)
            if n < 2: break
            elapsed = time.time() - starting_time
            if elapsed >= 5 and i >= 10:
                break
            # Периодическая "Хирургическая" сессия для тонкой доводки
            if (i > int(iterations / 2) or elapsed >= 2) and i % 5 == 0:
                moved = True
                # Повторяем, пока вносятся какие-либо улучшения
                while moved:
                    moved = False
                    
                    # --- Прием 1: Long-Distance Castling (исправление разрывов) ---
                    max_check_distance = min(n - 1, 2 + (i // 5)) 
                    for distance in range(max_check_distance, 1, -1):
                        if moved: break
                        for j in range(n - distance):
                            if moved: break
                            g1, g2 = current_order[j], current_order[j + distance]
                            g1_leader, g2_leader = g1['leader'], g2['leader']
                            if frozenset([g1_leader, g2_leader]) not in connections:
                                continue
                            is_isolated_block = True
                            for k in range(j + 1, j + distance):
                                g_mid_leader = current_order[k]['leader']
                                if frozenset([g1_leader, g_mid_leader]) in connections or \
                                   frozenset([g2_leader, g_mid_leader]) in connections:
                                    is_isolated_block = False; break
                            if is_isolated_block:
                                group_to_move = current_order.pop(j + distance)
                                current_order.insert(j + 1, group_to_move)
                                moved = True
                    
                    
                    if (i > int(iterations / 2) or elapsed >= 2) and (i % 7 == 0):
                        # --- Прием 2: Triangle Flip (размещение "мостов") ---
                        for j in range(n - 2):
                            g1, g2, g3 = current_order[j], current_order[j+1], current_order[j+2]
                            l1, l2, l3 = g1['leader'], g2['leader'], g3['leader']
                            
                            # Условие: G3 связан с G1 и G2, но G1 и G2 не связаны
                            cond1 = frozenset([l1, l3]) in connections
                            cond2 = frozenset([l2, l3]) in connections
                            cond3 = frozenset([l1, l2]) not in connections
                            
                            if cond1 and cond2 and cond3:
                                # Меняем местами G2 и G3, чтобы G3 стал мостом
                                current_order[j+1], current_order[j+2] = current_order[j+2], current_order[j+1]
                                moved = True
                                break # Список изменился, начинаем проверку заново
                    
                    if moved: continue # Если была рокировка, начинаем проверку зановоif moved: continue # Если была рокировка, начинаем проверку заново
                
                continue # После хирургии переходим к следующей основной итерации

            # --- Основная логика: Магнитная Швартовка (v7.5) ---
            rank_map_before = {g['leader']: i for i, g in enumerate(current_order)}
            
            groups_to_place = sorted(
                current_order,
                key=lambda g: -group_connections_count.get(g['leader'], 0)
            )

            placed_groups = []
            
            for group_to_move in groups_to_place:
                leader_to_move = group_to_move['leader']
                
                if not placed_groups:
                    placed_groups.append(group_to_move)
                    continue
                
                rank_map_placed = {g['leader']: i for i, g in enumerate(placed_groups)}
                
                connected_partners = []
                for key_pair, strength in connections.items():
                    if leader_to_move in key_pair:
                        other_leader = list(key_pair - {leader_to_move})[0]
                        if other_leader in rank_map_placed:
                            partner_rank = rank_map_placed[other_leader]
                            connected_partners.append({'rank': partner_rank, 'weight': strength, 'leader': other_leader})
                
                if not connected_partners:
                    ideal_rank = len(placed_groups)
                    temp_partners = []
                    for key_pair, strength in connections.items():
                        if leader_to_move in key_pair:
                            other = list(key_pair - {leader_to_move})[0]
                            if other in rank_map_before:
                                temp_partners.append({'rank': rank_map_before[other], 'weight': strength})
                    if temp_partners:
                        num = sum(p['rank'] * p['weight'] for p in temp_partners)
                        den = sum(p['weight'] for p in temp_partners)
                        ideal_rank = min(len(placed_groups), max(0, int(round(num / den if den > 0 else 0))))
                else:
                    numerator = sum(p['rank'] * p['weight'] for p in connected_partners)
                    denominator = sum(p['weight'] for p in connected_partners)
                    center_of_mass = numerator / denominator if denominator > 0 else len(placed_groups) / 2
                    anchor = min(connected_partners, key=lambda p: abs(p['rank'] - center_of_mass))
                    anchor_rank = anchor['rank']
                    if center_of_mass > anchor_rank: ideal_rank = anchor_rank + 1
                    elif center_of_mass < anchor_rank: ideal_rank = anchor_rank
                    else: ideal_rank = anchor_rank

                placed_groups.insert(ideal_rank, group_to_move)

            if len(placed_groups) == n:
                current_order = placed_groups
            else:
                break

        return current_order


    def _assign_term_to_group(self, group_key, term, raw_groups, term_to_orphans_map):
        """Helper: Приписывает термин и его сирот к конкретной группе."""
        group = raw_groups[group_key]
        group['visual_members'].add(term)
        if term in term_to_orphans_map:
            orphans = term_to_orphans_map[term]
            group['adopted'].update(orphans)

    def _sort_groups_by_gravity(self, groups):
        """
        Сортирует группы по силе связей (Гравитации).
        Версия 2.0: Добавлен "Бонус Синергии" для составных терминов.
        """
        if not groups: return []

        def calc_weight(g1, g2):
            # Пересечение полных кластеров (включая скрытые/исторические связи)
            intersection = g1['full_cluster'].intersection(g2['full_cluster'])
            weight = 0.0
            
            leader1 = g1['leader']
            leader2 = g2['leader']
            
            for term in intersection:
                w = len(term)
                
                # --- БОНУС СИНЕРГИИ ---
                # Если термин-мост содержит в себе ОБА паттерна, это мощнейшая связь.
                # Пример: term="武装色霸气" физически соединяет паттерны "武装色" и "霸气".
                if leader1 in term and leader2 in term:
                    w *= 3.0 # Утраиваем вес!
                
                # Штраф за усыновление (слабая связь)
                is_adopted_1 = term in g1.get('adopted_list', set())
                is_adopted_2 = term in g2.get('adopted_list', set())
                
                if is_adopted_1 or is_adopted_2:
                    weight += (w * 0.5)
                else:
                    weight += w
            return weight

        unplaced = groups.copy()
        
        # Находим самую значимую группу для начала
        unplaced.sort(key=lambda x: x['score'], reverse=True)
        
        ordered = []
        current = unplaced.pop(0)
        ordered.append(current)
        
        while unplaced:
            best_next = None
            max_weight = -1
            best_index = -1
            
            for i, candidate in enumerate(unplaced):
                weight = calc_weight(current, candidate)
                
                # Приоритет: Вес связи -> Изначальный Score
                if weight > max_weight:
                    max_weight = weight
                    best_next = candidate
                    best_index = i
                elif weight == max_weight and weight > 0:
                    if candidate['score'] > best_next['score']:
                        best_next = candidate
                        best_index = i
            
            if max_weight > 0:
                current = best_next
                unplaced.pop(best_index)
            else:
                # Разрыв цепи, берем следующую по важности
                current = unplaced.pop(0) 
            
            ordered.append(current)
            
        return ordered


    def _format_hierarchical_pattern_block(self, root_pattern, members, glossary_multimap, include_notes):
        """
        Форматирует паттерн с умным определением заголовков.
        Версия 3.0 (No Redundant Parents):
        - Игнорирует под-паттерны, которые охватывают 100% участников родителя.
        - Выносит одиночек наверх.
        """
        lines = []
        
        # Определяем умное имя для ГЛАВНОГО паттерна
        display_root_name = self._determine_realized_pattern(root_pattern, members)
        
        # --- Helper: Функция плоского вывода ---
        def _add_flat_block(name, terms, display_title=None):
            final_title = display_title if display_title else self._determine_realized_pattern(name, terms)
            sorted_m = self._sort_members_with_leader(terms, name)
            lines.append("") 
            lines.append(f'--- Pattern: "{final_title}" ---')
            lines.extend(self._format_compact_group(sorted_m, glossary_multimap, include_notes))

        # 1. Если группа слишком мала -> Плоский список
        if len(members) < 5:
            _add_flat_block(root_pattern, members, display_title=display_root_name)
            return lines

        # 2. Анализ подгрупп
        local_glossary = [{'original': m} for m in members]
        main_window = self.parent()
        
        sub_patterns = main_window.logic.analyze_patterns_with_substring(
            local_glossary, min_group_size=2, return_hierarchy=True, min_overlap_len=3
        )
        
        # --- ФИЛЬТРАЦИЯ ДУБЛИРУЮЩИХ РОДИТЕЛЕЙ ---
        sorted_sub_keys = sorted(sub_patterns.keys(), key=len, reverse=True)
        valid_sub_patterns = []
        
        root_members_set = set(members)
        
        for p in sorted_sub_keys:
            if p == root_pattern: continue 
            
            # Если под-паттерн содержит ТЕ ЖЕ САМЫЕ термины, что и рут (или больше, что невозможно в данном контексте),
            # значит это просто "синоним" или более короткая версия заголовка. Она нам не нужна как подгруппа.
            sub_m_set = set(sub_patterns[p])
            if sub_m_set == root_members_set:
                continue
                
            valid_sub_patterns.append(p)

        # 3. Распределение (Жадное)
        processed_members = set()
        grouped_terms = defaultdict(list)

        for pattern in valid_sub_patterns:
            pat_members = sub_patterns[pattern]
            # Берем только тех, кто еще не попал в более специфичную (длинную) подгруппу
            valid_members = [m for m in pat_members if m in members and m not in processed_members]
            if valid_members:
                grouped_terms[pattern] = valid_members
                processed_members.update(valid_members)
        
        leftovers = [m for m in members if m not in processed_members]
        if leftovers:
            grouped_terms["__BASE__"] = leftovers

        # --- ЭТАП 4: Оптимизация Одиночек ---
        promoted_singletons = []
        keys_to_remove = []

        for sub_pat, sub_mems in grouped_terms.items():
            if sub_pat == "__BASE__": continue

            if len(sub_mems) == 1:
                promoted_singletons.extend(sub_mems)
                keys_to_remove.append(sub_pat)
        
        for k in keys_to_remove:
            del grouped_terms[k]

        # --- Проверка на вырождение ---
        real_subgroups = [k for k in grouped_terms.keys() if k != "__BASE__"]
        
        if not real_subgroups and not promoted_singletons:
             _add_flat_block(root_pattern, members, display_title=display_root_name)
             return lines

        # --- ВЫВОД ПОЛНОЦЕННОЙ СЕМЬИ ---
        lines.append(f'\n---\n\n### PATTERN FAMILY: "{display_root_name}"')
        
        # Вывод Одиночек
        if promoted_singletons:
            sorted_promoted = self._sort_members_with_leader(promoted_singletons, root_pattern)
            lines.extend(self._format_compact_group(sorted_promoted, glossary_multimap, include_notes))

        # Вывод Базы
        if "__BASE__" in grouped_terms:
            base_members = self._sort_members_with_leader(grouped_terms["__BASE__"], root_pattern)
            if promoted_singletons: lines.append("") 
            lines.extend(self._format_compact_group(base_members, glossary_multimap, include_notes))
            del grouped_terms["__BASE__"]
            
        # Вывод Крупных Подгрупп
        for sub_pat in sorted(grouped_terms.keys()):
            sub_members = grouped_terms[sub_pat]
            display_sub_name = self._determine_realized_pattern(sub_pat, sub_members)
            leader_sort = self._sort_members_with_leader(sub_members, sub_pat)

            lines.append(f'\n>> SUB-GROUP: "{display_sub_name}"')
            lines.extend(self._format_compact_group(leader_sort, glossary_multimap, include_notes))
        
        lines.append('\n---')
            
        return lines
        
    def _sort_members_with_leader(self, members_list, pattern_str):
        """
        Сортирует список: термин, совпадающий с паттерном (или его слитной версией), идет первым.
        """
        leader = None
        others = []
        
        # Нормализация для поиска "слипшегося" лидера (Fire Ball -> fireball)
        pat_clean = pattern_str.replace(" ", "").lower()
        
        for m in members_list:
            m_clean = m.replace(" ", "").lower()
            
            # 1. Точное совпадение
            if m == pattern_str:
                leader = m # Абсолютный приоритет
            # 2. Совпадение без пробелов (если абсолютного лидера еще нет)
            elif leader != pattern_str and m_clean == pat_clean:
                leader = m
            else:
                others.append(m)
        
        # Если лидер был в others (из-за второго условия), убираем его оттуда
        if leader and leader in others:
            others.remove(leader)
            
        others.sort()
        
        if leader:
            return [leader] + others
        return others
        
        
        
    def _format_term_group(self, title, term_list, glossary_map, include_notes):
        """Форматирует группу терминов в стандартный блок (Переводы, Примечания)."""
        lines = []
        if not term_list:
            return lines

        lines.append(f'\n--- {title} ---')
        
        # Сначала переводы
        translation_lines = []
        for term in term_list:
            entry = glossary_map.get(term)
            if entry and entry.get("rus"):
                translation_lines.append(f'"{entry.get("original")}" = "{entry.get("rus")}"')
        
        if translation_lines:
            lines.append("--- Translations ---")
            lines.extend(translation_lines)

        # Затем примечания (если включены)
        if include_notes:
            note_lines = []
            for term in term_list:
                entry = glossary_map.get(term)
                if entry and entry.get("note"):
                    note_lines.append(f'"{entry.get("rus")}" - "{entry.get("note")}"')
            
            if note_lines:
                lines.append("--- Notes ---")
                lines.extend(note_lines)
        
        return lines
    
    def _format_compact_group(self, term_list, glossary_multimap, include_notes):
        """
        Форматирует группу терминов.
        Автоматически добавляет префикс '> ', если термин был помечен как 'усыновленный' (child)
        в процессе анализа наложений и перемещен в паттерны.
        """
        lines = []
        if not term_list: return lines

        # Подготовка констант для валидации
        noise_chars = set(' .,;:!?"\'()[]{}-–—_=/\\|<>`~@#$%^&*+0123456789\t\n\r')
        structural_chars = set('/()[]+<>')
        cjk_pattern = re.compile(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]')
        
        unique_terms = sorted(list(set(term_list)))
        
        # Получаем доступ к реестру (безопасно)
        registry = getattr(self, 'adopted_terms_registry', set())

        for term in unique_terms:
            entries = glossary_multimap.get(term, [])
            
            for entry in entries:
                data_obj = {'rus': entry.get('rus', '')}
                
                if include_notes and entry.get('note'):
                    data_obj['note'] = entry.get('note')

                if include_notes:
                    rus_clean = str(data_obj['rus']).lower().strip()
                    orig_clean = str(term).lower().strip()
                    warnings = []

                    if rus_clean == orig_clean:
                        if cjk_pattern.search(orig_clean):
                            warnings.append("Term is untranslated (contains CJK characters).")
                    else:
                        missing_structure = []
                        for char in structural_chars:
                            in_orig = char in orig_clean
                            in_rus = char in rus_clean
                            if in_orig != in_rus:
                                missing_structure.append(char)
                        
                        if missing_structure:
                            warnings.append(f"Structural mismatch (check symbols: {' '.join(missing_structure)}).")

                    if warnings:
                        data_obj['__WARNING__'] = f"{' '.join(warnings)} Fix if error."

                json_key = json.dumps(term, ensure_ascii=False)
                json_val = json.dumps(data_obj, ensure_ascii=False)
                
                # === ГЛАВНОЕ ИЗМЕНЕНИЕ: Визуализация структуры ===
                prefix = "> " if term in registry else ""
                lines.append(f'{prefix}{json_key}: {json_val},')

        return lines


    def _build_final_prompt(self, found_blocks, context_was_added):
        """
        Собирает промпт, используя тексты из internal_prompts.json.
        Все схемы и инструкции теперь загружаются из конфига.
        """
        base_prompt_template = self.prompt_widget.get_prompt()
        
        # Мы не хотим, чтобы универсальный обработчик задач добавлял
        # свой контекстный глоссарий или примеры.
        # Добавляем их с новой строки для чистоты.
        base_prompt_template += "\n<suppress_glossary_injection/>\n<suppress_examples_injection/>"
        
        include_notes = self.cb_notes.isChecked()
        
        # Загружаем словарь текстов из конфига
        prompts = api_config.internal_prompts().get("correction_prompts", {})
        block_descs = prompts.get("block_descriptions", {})
        fmt_instr = prompts.get("format_instructions", {})
        schemas = fmt_instr.get("schemas", {}) # <-- Новая секция
        examples = prompts.get("examples", {})
    
        description_parts = []
        description_parts.append(prompts.get("intro", "Data provided:"))
        
        block_counter = 1
        
        if context_was_added:
            desc = block_descs.get("context", "Context block.")
            description_parts.append(f'{block_counter}. {desc}')
            block_counter += 1
    
        keys_to_check = ['conflicts', 'overlaps', 'patterns', 'hidden']
        for key in keys_to_check:
            if key in found_blocks and found_blocks[key]:
                desc = block_descs.get(key, f"{key} block.")
                description_parts.append(f'{block_counter}. {desc}')
                block_counter += 1

        description_parts.append(fmt_instr.get("json_intro", "JSON Format:"))
        
        if include_notes:
            # === СЛОЖНЫЙ РЕЖИМ (Rus + Note) ===
            # 1. Входная схема
            description_parts.append(schemas.get("input_with_notes", "`Input Schema`"))
            
            # 2. Инструкции по валидации и нотам
            description_parts.append(fmt_instr.get("warning_hint", "Check warnings."))
            description_parts.append(fmt_instr.get("note_policy", "Keep metadata."))
            
            # 3. Выходная схема
            description_parts.append(fmt_instr.get("task_goal", "Task:"))
            description_parts.append(schemas.get("output_with_notes", "`Output Schema`"))
            
            example_json = examples.get("with_notes", "{}")
        else:
            # === ПРОСТОЙ РЕЖИМ (Rus only) ===
            # 1. Входная схема
            description_parts.append(schemas.get("input_simple", "`Input Simple Schema`"))
            
            # 2. Выходная схема
            description_parts.append(fmt_instr.get("task_goal", "Task:"))
            description_parts.append(schemas.get("output_simple", "`Output Simple Schema`"))
            
            example_json = examples.get("simple", "{}")
        
        input_format_description = '\n'.join(description_parts)
        prompt_with_format = base_prompt_template.replace("{input_format_description}", input_format_description)
        final_prompt_for_session = prompt_with_format.replace("{example_json}", example_json)
        
        return final_prompt_for_session

    def _create_virtual_epub(self, content_str: str, internal_chapter_path: str) -> str:
        """
        Создает виртуальный EPUB в памяти НАПРЯМУЮ, используя os_patch.
        Возвращает путь 'mem://...' к файлу.
        """
        # 1. Генерируем уникальный путь в нашей виртуальной файловой системе.
        virtual_path = f"mem://{uuid.uuid4().hex}.epub"

        try:
            # 2. Используем "патченный" open() для получения файлового объекта,
            #    который на самом деле является потоком в памяти.
            with zipfile.ZipFile(open(virtual_path, 'wb'), 'w', zipfile.ZIP_DEFLATED) as zf:
                # 3. Записываем нашу "главу" в этот виртуальный архив.
                zf.writestr(internal_chapter_path, content_str.encode('utf-8'))
            
            # 4. Файл уже создан в памяти. Просто возвращаем путь к нему.
            return virtual_path
            
        except Exception as e:
            # Эта ошибка может возникнуть, если os_patch не сработал
            # или произошла проблема при записи в zip.
            raise IOError(f"Не удалось напрямую создать виртуальный EPUB в памяти: {e}")
    
    @pyqtSlot(dict)
    def _on_global_event(self, event_data: dict):
        event_name = event_data.get('event')
        if event_name == 'model_changed': self.update_token_estimation()
        if event_name == 'session_started': self._set_session_active(True)
        if event_name == 'session_finished':
            # 1. Сначала деактивируем UI
            self._set_session_active(False)
            
            # Сбрасываем текст кнопки Dry Run
            self.dry_run_btn.setText("🧪 Пробный запуск")
            
            # 2. Затем выполняем "косметическую" синхронизацию стилей ключей
            try:
                model_name = self.model_settings_widget.model_combo.currentText()
                model_id = api_config.all_models().get(model_name, {}).get('id')
                if model_id:
                    self.key_widget.set_current_model(model_id)
            except Exception as e:
                print(f"[ERROR] Syncing keys in AI-corrector: {e}")

            # 3. Проверяем, как завершилась сессия
            reason = event_data.get('data', {}).get('reason', '')
            if "Отменено" in reason or "Ошибка" in reason or "исчерпаны" in reason:
                return
            
            # 4. Обработка результата
            QtCore.QTimer.singleShot(0, self._process_results_from_db)
    
    def _process_results_from_db(self):
        """
        Извлекает все строки-термины из БД, собирает из них единый патч,
        запускает диалог предпросмотра и очищает очередь.
        """
        app = QtWidgets.QApplication.instance()
        if not (app.engine and app.engine.task_manager):
            return

        try:
            # --- ЭТАП 1: Чтение (неблокирующая операция) ---
            all_term_rows = []
            with app.engine.task_manager._get_read_only_conn() as conn:
                cursor = conn.execute("SELECT original, rus, note FROM glossary_results")
                all_term_rows = cursor.fetchall()
            
            # --- ЭТАП 2: Очистка (атомарная операция записи) ---
            with app.engine.task_manager._get_write_conn() as conn:
                conn.execute("DELETE FROM glossary_results")

            # --- ЭТАП 3: Очистка очереди задач ---
            app.engine.task_manager.clear_all_queues()
            self._post_event('log_message', {'message': "[CORRECTOR] Очередь задач очищена после получения результата."})

            if not all_term_rows:
                QMessageBox.information(self, "Результат", "AI-корректор не вернул данных для исправления.")
                return

            # 4. Собираем единый словарь-патч из отдельных строк
            patch_dict = {}
            for term_row in all_term_rows:
                original = term_row['original']
                if not original:
                    continue
                
                term_data = {'rus': term_row['rus']}
                if term_row['note']:
                    term_data['note'] = term_row['note']
                
                patch_dict[original] = term_data

        except Exception as e:
            # На случай, если ошибка произошла до очистки, приберемся здесь
            if app.engine and app.engine.task_manager:
                app.engine.task_manager.clear_all_queues()
            QMessageBox.critical(self, "Ошибка обработки результата", f"Не удалось извлечь или обработать результат из базы данных:\n{e}")
            return
        
        # 5. Передаем собранный патч на обработку
        self._handle_correction_patch(patch_dict)
    
    def _handle_correction_patch(self, patch_dict: dict):
        """Принимает ГОТОВЫЙ словарь-патч и запускает диалог предпросмотра."""
        if not patch_dict:
            QMessageBox.information(self, "Результат", "AI-корректор не нашел терминов, требующих исправления.")
            return
            
        main_window = self.parent()
        if not main_window or main_window.__class__.__name__ != 'MainWindow':
            return
            
        preview = CorrectionPreviewDialog(
            main_window.get_glossary(), 
            patch_dict, 
            main_window.direct_conflicts,
            notes_were_included_in_prompt=self.cb_notes.isChecked(),
            morph_analyzer=self.morph_analyzer,
            parent=self
        )
        if preview.exec():
            accepted_patch_list = preview.get_accepted_patch()
            if accepted_patch_list:
                self.correction_accepted.emit(accepted_patch_list)
                
                # Считаем общее количество реальных изменений для сообщения
                # (изменение = не просто удаление)
                total_changes = sum(1 for p in accepted_patch_list if p.get('after'))
                
                QMessageBox.information(self, "Готово", f"Будет применено {total_changes} исправлений/добавлений.")
            else:
                QMessageBox.information(self, "Нет изменений", "Вы не выбрали ни одного исправления.")
    
    def _set_session_active(self, active):
        self.is_session_active = active
        self.key_widget.setEnabled(not active)
        self.model_settings_widget.setEnabled(not active)
        self.prompt_widget.setEnabled(not active)
        self.dry_run_btn.setEnabled(not active) # Блокируем Dry Run во время сессии
        
        if active:
            self.start_stop_btn.setText("❌ Стоп")
            self.start_stop_btn.setStyleSheet("background-color: #C0392B; color: #ffffff;")
            self.start_stop_btn.setEnabled(True)
            self.cancel_close_btn.setText("Закрыть")
        else:
            self.start_stop_btn.setText("🚀 Запустить коррекцию")
            self.start_stop_btn.setStyleSheet("")
            self.cancel_close_btn.setText("Отмена")
            self._update_start_button_state()
    
    def reject(self):
        if self.is_session_active: self._on_start_stop_clicked()
        else: super().reject()
    
    def get_settings(self):
        settings = self.model_settings_widget.get_settings()
        settings['provider'] = self.key_widget.get_selected_provider()
        settings['api_keys'] = self.key_widget.get_active_keys()
        model_name = settings.get('model')
        model_config = api_config.all_models().get(model_name, {}).copy()
        if model_config: model_config['name'] = model_name
        settings['model_config'] = model_config
        settings['custom_prompt'] = self.prompt_widget.get_prompt()
        settings['force_accept'] = True
        settings['num_instances'] = 1
        settings['rpm_limit'] = 1
        settings['glossary_merge_mode'] = "accumulate"
        return settings

class CorrectionPreviewDialog(QDialog):
    """
    Интерактивный диалог для предпросмотра, фильтрации и применения
    исправлений. Версия 3.1 (Strict Logic: AI Patching vs Safety Wipe).
    """
    def __init__(self, original_glossary_list, patch_dict, direct_conflicts, notes_were_included_in_prompt=True, morph_analyzer=None, parent=None):
        super().__init__(parent)
        self.original_glossary_list = original_glossary_list
        self.patch_dict = patch_dict
        self.direct_conflicts = direct_conflicts
        
        self.main_window_ref = parent.parent() if parent and parent.parent() else None
        
        self.notes_were_included = notes_were_included_in_prompt
        self.morph_analyzer = morph_analyzer
        self.pymorphy_available = morph_analyzer is not None
        
        self.display_data = []
        self.hidden_data = []
        self.new_terms_data = []
        self.showing_hidden = False
        self.showing_new_terms = False

        self.setWindowTitle("Предпросмотр AI-исправлений")
        self.setMinimumSize(1200, 700)

        self._filter_and_prepare_data()

        if not self.display_data and not self.hidden_data and not self.new_terms_data:
            QMessageBox.information(self, "Нет изменений", "AI не предложил корректных исправлений или не нашел проблем.")
            QtCore.QTimer.singleShot(0, self.reject)
            return

        self._init_ui()
        self._populate_table()

    def _determine_final_note(self, ai_note_str, old_note_str, old_trans_str, new_trans_str):
        """
        Центральная логика принятия решения по примечанию.
        
        Правила:
        1. Если AI прислал текст -> Берем его.
        2. Если AI прислал пустоту -> Оставляем старое (Patching).
        3. OVERRIDE: Если AI НЕ видел примечаний (optimization) И сменил перевод И возник конфликт -> Удаляем (Wipe).
        """
        # Шаг 1: Базовое решение (Patching)
        if ai_note_str.strip():
            candidate_note = ai_note_str.strip()
        else:
            candidate_note = old_note_str.strip()

        # Шаг 2: Проверка безопасности (Safety Wipe)
        # Удаляем ТОЛЬКО если ИИ был слеп к примечаниям, но своими действиями (сменой перевода) сделал старое примечание невалидным.
        is_trans_changed = (old_trans_str != new_trans_str) and new_trans_str
        
        if not self.notes_were_included and is_trans_changed:
            # Если возник конфликт грамматики
            if not self._is_note_compatible(old_trans_str, new_trans_str, candidate_note):
                return "" # Принудительное удаление
        
        return candidate_note

    def _filter_and_prepare_data(self):
        """
        Фильтрует патч:
        1. Игнорирует пустые переводы для новых терминов.
        2. Игнорирует пустые переводы для конфликтов (безопасность).
        3. Если перевод пуст, но термин уникален и есть новое примечание -> обновляет только примечание.
        4. Разделяет изменения на Категории (Add, Resolve, Update).
        """
        original_map = {entry.get('original', ''): entry for entry in self.original_glossary_list}
        
        for original_term, new_data in self.patch_dict.items():
            if not original_term: continue

            new_trans = new_data.get('rus', '').strip()
            ai_provided_note = new_data.get('note', '').strip()
            
            # Находим все существующие записи для этого термина
            old_entries = [e for e in self.original_glossary_list if e.get('original') == original_term]
            count_existing = len(old_entries)

            # --- ЛОГИКА ОБРАБОТКИ ПУСТОГО ПЕРЕВОДА (из запроса) ---
            if not new_trans:
                # 1. Если термина нет вообще (Новый) -> Игнорируем (зачем нам термин без перевода?)
                if count_existing == 0:
                    continue
                
                # 2. Если есть дубликаты/конфликты -> Игнорируем (непонятно, к кому применять note, опасно)
                if count_existing > 1:
                    continue
                
                # 3. Если термин уникален (1 шт)
                if count_existing == 1:
                    # Если ИИ прислал только ключ (ни перевода, ни примечания) -> Игнорируем
                    if not ai_provided_note:
                        continue
                    
                    # Если есть примечание, но нет перевода -> Считаем, что перевод не меняется
                    # Подставляем старый перевод, чтобы логика ниже отработала корректно
                    new_trans = old_entries[0].get('rus', '').strip()
            
            # --------------------------------------------------------

            # --- СЛУЧАЙ 1: Новый термин (Addition) ---
            if count_existing == 0:
                # new_trans здесь гарантированно не пуст (проверка выше)
                self.new_terms_data.append({
                    "type": "addition", 
                    "original": original_term, 
                    "new_trans": new_trans, 
                    "new_note": ai_provided_note, 
                    "is_new": True
                })
                continue
            
            # --- СЛУЧАЙ 2: Разрешение конфликта (Resolution) ---
            if original_term in self.direct_conflicts or count_existing > 1:
                # Для конфликтов берем самое длинное существующее примечание как "базовое"
                best_old_entry = max(old_entries, key=lambda x: len(x.get('note', '')))
                old_note_str = best_old_entry.get('note', '')
                old_trans_str = best_old_entry.get('rus', '')

                final_new_note = self._determine_final_note(ai_provided_note, old_note_str, old_trans_str, new_trans)

                trans_changed = any(e.get('rus', '').strip() != new_trans for e in old_entries)
                note_changed = any(e.get('note', '').strip() != final_new_note for e in old_entries)
                
                if not final_new_note and any(e.get('note', '').strip() for e in old_entries):
                    note_changed = True

                data_payload = {
                    "type": "resolution", 
                    "original": original_term, 
                    "old_entries": old_entries, 
                    "new_trans": new_trans, 
                    "new_note": final_new_note
                }

                if trans_changed or note_changed:
                    self.display_data.append({**data_payload, "is_hidden": False})
                else:
                    self.hidden_data.append({**data_payload, "is_hidden": True})

            # --- СЛУЧАЙ 3: Обновление (Update) ---
            else: 
                # count_existing == 1
                old_data = old_entries[0]
                old_trans = old_data.get('rus', '').strip()
                old_note = old_data.get('note', '').strip()

                final_new_note = self._determine_final_note(ai_provided_note, old_note, old_trans, new_trans)

                trans_changed = (old_trans != new_trans)
                note_changed = (old_note != final_new_note)
                
                data_payload = {
                    "type": "update", 
                    "original": original_term, 
                    "old_trans": old_trans, 
                    "new_trans": new_trans, 
                    "old_note": old_note, 
                    "new_note": final_new_note
                }
                
                if trans_changed or note_changed:
                    self.display_data.append({**data_payload, "is_hidden": False})
                else:
                    self.hidden_data.append({**data_payload, "is_hidden": True})
    
    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        info_label = QLabel(
            "AI предлагает следующие изменения. Проверьте и выберите, какие из них применить.<br>"
            "<b>Жирным шрифтом</b> выделены изменения. <span style='color:red; font-weight:bold;'>[УДАЛЕНИЕ]</span> означает автоматическое или ручное удаление примечания."
        )
        main_layout.addWidget(info_label)
        
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["Применить?", "Оригинал", "Было (Перевод)", "Стало (Перевод)", "Было (Примечание)", "Стало (Примечание)"])
        header = self.table.horizontalHeader()
        for i in range(6): header.setSectionResizeMode(i, QHeaderView.ResizeMode.Stretch if i > 0 else QHeaderView.ResizeMode.ResizeToContents)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        main_layout.addWidget(self.table)
        
        bottom_panel = QHBoxLayout()
        select_all_btn = QPushButton("Выделить всё"); select_all_btn.clicked.connect(lambda: self._toggle_all_checkboxes(True))
        deselect_all_btn = QPushButton("Снять выделение"); deselect_all_btn.clicked.connect(lambda: self._toggle_all_checkboxes(False))
        
        self.toggle_hidden_btn = QPushButton(f"Показать неизмененные ({len(self.hidden_data)})")
        self.toggle_hidden_btn.clicked.connect(self._toggle_hidden_visibility)
        self.toggle_hidden_btn.setVisible(bool(self.hidden_data))

        self.toggle_new_terms_btn = QPushButton(f"Показать новые ({len(self.new_terms_data)})")
        self.toggle_new_terms_btn.clicked.connect(self._toggle_new_terms_visibility)
        self.toggle_new_terms_btn.setVisible(bool(self.new_terms_data))

        bottom_panel.addWidget(select_all_btn)
        bottom_panel.addWidget(deselect_all_btn)
        bottom_panel.addWidget(self.toggle_hidden_btn)
        bottom_panel.addWidget(self.toggle_new_terms_btn)
        bottom_panel.addStretch()
        
        apply_btn = QPushButton("Применить выделенное"); apply_btn.clicked.connect(self._apply_and_accept)
        cancel_btn = QPushButton("Отмена"); cancel_btn.clicked.connect(self.reject)
        bottom_panel.addWidget(cancel_btn); bottom_panel.addWidget(apply_btn)
        main_layout.addLayout(bottom_panel)

    def _get_current_view_data(self):
        """Возвращает список данных, соответствующих текущему отображению таблицы."""
        return self.display_data + \
               (self.hidden_data if self.showing_hidden else []) + \
               (self.new_terms_data if self.showing_new_terms else [])

    def _save_checkbox_states(self):
        """Сохраняет состояние чекбоксов перед перестройкой таблицы."""
        current_data = self._get_current_view_data()
        for i in range(self.table.rowCount()):
            container = self.table.cellWidget(i, 0)
            if container and container.layout() and container.layout().count() > 0:
                checkbox = container.layout().itemAt(0).widget()
                if isinstance(checkbox, QCheckBox):
                    current_data[i]['user_checked'] = checkbox.isChecked()

    def _populate_table(self):
        self.table.setRowCount(0)
        current_data = self._get_current_view_data()
        self.table.setRowCount(len(current_data))

        resolution_color = QColor(255, 193, 7, 50) 
        hidden_color = QColor(108, 117, 125, 40)
        new_term_color = QColor(13, 202, 240, 40)
        bold_font = QFont(); bold_font.setBold(True)

        for i, data in enumerate(current_data):
            checkbox_widget = QWidget(); chk_layout = QHBoxLayout(checkbox_widget)
            chk_layout.setAlignment(Qt.AlignmentFlag.AlignCenter); chk_layout.setContentsMargins(0,0,0,0)
            checkbox = QCheckBox()
            
            if 'user_checked' in data:
                is_checked = data['user_checked']
            else:
                is_checked = not data.get("is_hidden", False)
            
            checkbox.setChecked(is_checked)
            chk_layout.addWidget(checkbox); self.table.setCellWidget(i, 0, checkbox_widget)

            original_item = QTableWidgetItem(data["original"])
            new_trans_item = QTableWidgetItem(data["new_trans"])
            new_note_item = QTableWidgetItem(data["new_note"])
            
            self.table.setItem(i, 1, original_item)
            self.table.setItem(i, 3, new_trans_item)
            self.table.setItem(i, 5, new_note_item)

            # --- ВИЗУАЛИЗАЦИЯ УДАЛЕНИЯ ---
            # Проверяем: если новое примечание пустое, а старое (или одно из старых) было не пустым.
            is_note_wiped = False
            old_note_text = ""
            
            if data["type"] == "update":
                old_note_text = data.get("old_note", "")
            elif data["type"] == "resolution":
                # Если хотя бы у одного старого варианта было примечание
                old_entries_notes = [e.get("note", "").strip() for e in data.get("old_entries", [])]
                if any(old_entries_notes):
                    old_note_text = " (было) " # Маркер наличия
            
            if old_note_text and not data["new_note"]:
                is_note_wiped = True
                new_note_item.setText("[УДАЛЕНИЕ]")
                new_note_item.setForeground(QColor("#E74C3C")) # Red
                new_note_item.setFont(bold_font)
                new_note_item.setToolTip("Примечание удалено, так как оно конфликтует с новым переводом или удалено ИИ.")

            base_color = QColor("transparent")
            if data.get("is_hidden"): base_color = hidden_color
            elif data.get("is_new"): base_color = new_term_color
            elif data["type"] == "resolution": base_color = resolution_color

            if data["type"] == "resolution":
                old_trans = "\n".join([f"• {e.get('rus', '')}" for e in data["old_entries"]])
                old_note = "\n".join([f"• {e.get('note', '')}" for e in data["old_entries"] if e.get('note')])
                self.table.setItem(i, 2, QTableWidgetItem(old_trans))
                self.table.setItem(i, 4, QTableWidgetItem(old_note))
                if not data.get("is_hidden"):
                    new_trans_item.setFont(bold_font)
                    if not is_note_wiped and data["new_note"]: new_note_item.setFont(bold_font)
            elif data["type"] == "update":
                self.table.setItem(i, 2, QTableWidgetItem(data["old_trans"]))
                self.table.setItem(i, 4, QTableWidgetItem(data["old_note"]))
                if not data.get("is_hidden"):
                    if data["old_trans"] != data["new_trans"]: new_trans_item.setFont(bold_font)
                    if data["old_note"] != data["new_note"] and not is_note_wiped: new_note_item.setFont(bold_font)
            elif data["type"] == "addition":
                self.table.setItem(i, 2, QTableWidgetItem("---"))
                self.table.setItem(i, 4, QTableWidgetItem("---"))
                if not data.get("is_hidden"):
                    new_trans_item.setFont(bold_font)
                    new_note_item.setFont(bold_font)
            
            for col in range(1, 6):
                item = self.table.item(i, col)
                if not item: item = QTableWidgetItem(""); self.table.setItem(i, col, item)
                if base_color.alpha() > 0: item.setBackground(base_color)

        self.table.resizeRowsToContents()
    
    def _get_pymorphy_tags(self, text: str) -> set:
        if not self.pymorphy_available or not text or not self.main_window_ref: return set()
        _, _, main_word_parse = self.main_window_ref._generate_note_logic(text, return_raw_parse=True)
        if not main_word_parse: return set()
        tag = main_word_parse.tag
        all_tags = set()
        relevant_tags = {'masc', 'femn', 'neut', 'sing', 'plur', 'Sgtm', 'Pltm'}
        for t in relevant_tags:
            if t in tag: all_tags.add(t)
        if 'Sgtm' in all_tags: all_tags.add('sing')
        if 'Pltm' in all_tags: all_tags.add('plur')
        return all_tags

    def _is_note_compatible(self, old_translation: str, new_translation: str, old_note: str) -> bool:
        if not self.pymorphy_available or not old_translation or not new_translation or not old_note: return True 
        note_lower = old_note.lower()
        SYNONYMS = {'gender': ['род', 'рода', 'полу'], 'masc': ['мужской', 'муж', 'мужск', 'м'], 'femn': ['женский', 'жен', 'женск', 'ж'], 'neut': ['средний', 'ср', 'средн'], 'number': ['число', 'числа', 'ч'], 'sing': ['единственное', 'ед'], 'plur': ['множественное', 'мн']}
        def has_any_word(text, word_list): return bool(re.search(r'\b(' + '|'.join(re.escape(w) + r'\.?' for w in word_list) + r')\b', text))

        note_mentions_gender = has_any_word(note_lower, SYNONYMS['gender']) and (has_any_word(note_lower, SYNONYMS['masc']) or has_any_word(note_lower, SYNONYMS['femn']) or has_any_word(note_lower, SYNONYMS['neut']))
        note_mentions_number = has_any_word(note_lower, SYNONYMS['number']) and (has_any_word(note_lower, SYNONYMS['sing']) or has_any_word(note_lower, SYNONYMS['plur']))

        if not note_mentions_gender and not note_mentions_number: return True
        old_tags = self._get_pymorphy_tags(old_translation); new_tags = self._get_pymorphy_tags(new_translation)
        if not old_tags or not new_tags: return True
        if note_mentions_gender:
            genders = {'masc', 'femn', 'neut'}
            if (old_tags & genders) and (new_tags & genders) and (old_tags & genders) != (new_tags & genders): return False
        if note_mentions_number:
            numbers = {'sing', 'plur'}
            if (old_tags & numbers) and (new_tags & numbers) and (old_tags & numbers) != (new_tags & numbers): return False
        return True
        
    def _toggle_hidden_visibility(self):
        self._save_checkbox_states()
        self.showing_hidden = not self.showing_hidden
        self.toggle_hidden_btn.setText(f"{'Скрыть' if self.showing_hidden else 'Показать'} неизмененные ({len(self.hidden_data)})")
        self._populate_table()

    def _toggle_new_terms_visibility(self):
        self._save_checkbox_states()
        self.showing_new_terms = not self.showing_new_terms
        self.toggle_new_terms_btn.setText(f"{'Скрыть' if self.showing_new_terms else 'Показать'} новые ({len(self.new_terms_data)})")
        self._populate_table()

    def _toggle_all_checkboxes(self, checked):
        current_data = self._get_current_view_data()
        for i in range(self.table.rowCount()):
            container = self.table.cellWidget(i, 0)
            if container and container.layout():
                checkbox = container.layout().itemAt(0).widget()
                if checkbox:
                    checkbox.setChecked(checked)
                    current_data[i]['user_checked'] = checked

    def _apply_and_accept(self):
        self.accepted_patch = []
        original_map = {entry.get('original'): entry for entry in self.original_glossary_list}
        current_data = self._get_current_view_data()

        for i in range(self.table.rowCount()):
            container = self.table.cellWidget(i, 0)
            if not container or not container.layout(): continue
            checkbox = container.layout().itemAt(0).widget()
            
            if not isinstance(checkbox, QCheckBox) or not checkbox.isChecked():
                continue
            
            data = current_data[i]
            original = data["original"]
            
            # Гарантируем, что ключ 'note' всегда присутствует в патче, даже если значение пустое.
            new_data = {
                "rus": data["new_trans"],
                "note": data.get("new_note", "") 
            }

            if data["type"] == "resolution":
                for old_entry in data["old_entries"]: self.accepted_patch.append({'before': old_entry, 'after': None})
                new_resolved_entry = {'original': original, **new_data}
                self.accepted_patch.append({'before': None, 'after': new_resolved_entry})
            elif data["type"] == "update":
                before_state = original_map.get(original)
                after_state = (before_state or {}).copy()
                after_state.update({'original': original, **new_data})
                self.accepted_patch.append({'before': before_state, 'after': after_state})
            elif data["type"] == "addition":
                after_state = {'original': original, **new_data}
                self.accepted_patch.append({'before': None, 'after': after_state})
        
        self.accept()

    def get_accepted_patch(self):
        return getattr(self, "accepted_patch", [])



