# -*- coding: utf-8 -*-

# ---------------------------------------------------------------------------
# Диалоги начальной настройки
# ---------------------------------------------------------------------------
# Этот файл содержит единый класс диалогового окна для первоначальной
# настройки и запуска различных режимов работы приложения.
# ---------------------------------------------------------------------------

import os
import re
import json
import uuid
import zipfile
from bs4 import BeautifulSoup
import math  # <--- ДОБАВЬТЕ ЭТУ СТРОКУ
import traceback # <--- ДОБАВЬТЕ ЭТУ СТРОКУ

# --- Импорты из PyQt6 ---
from PyQt6 import QtWidgets, QtCore, QtGui
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QListWidget, QPushButton, QDialogButtonBox, QLabel,
    QTextEdit, QFileDialog, QDoubleSpinBox, QListWidgetItem, QCheckBox,
    QMessageBox, QStyle,
    QTableWidget, QTableWidgetItem, QGroupBox, QFormLayout, QHBoxLayout, QHeaderView,
    QScrollArea, QWidget, QTabWidget, QGridLayout,
    QPlainTextEdit, QComboBox, QSpinBox, QSplitter, QAbstractItemView
)

from PyQt6.QtCore import QMimeData, pyqtSlot, pyqtSignal, QThread, QItemSelectionModel, QItemSelection
from ...scripts.package_filter_tasks import FilterPackagingDialog

# --- Импорты из нашего проекта ---
from ...api import config as api_config
from ...api.managers import ApiKeyManager
from ...core.translation_engine import TranslationEngine
from ...core.task_manager import ChapterQueueManager, TaskDBWorker
from ...utils.settings import SettingsManager
from ...utils.epub_tools import extract_number_from_path, calculate_potential_output_size, get_epub_chapter_sizes_with_cache
from ...utils.helpers import TokenCounter
from ...utils.language_tools import SmartGlossaryFilter, GlossaryReplacer
from ...utils.project_migrator import ProjectMigrator
from ...utils.project_manager import TranslationProjectManager

from ..widgets import (
    KeyManagementWidget, TranslationOptionsWidget, ModelSettingsWidget,
    ProjectPathsWidget, GlossaryWidget, PresetWidget, ProjectActionsWidget,
    TaskManagementWidget, LogWidget, StatusBarWidget
)
from ..widgets.common_widgets import NoScrollSpinBox
from .epub import EpubHtmlSelectorDialog, TranslatedChaptersManagerDialog
from .misc import ProjectHistoryDialog, ProjectFolderDialog, GeoBlockDialog
from .glossary import MainWindow as GlossaryToolWindow
from .glossary import ImporterWizardDialog
from datetime import datetime
import time # <-- НОВЫЙ ИМПОРТ


# --- НОВЫЕ КОНСТАНТЫ ДЛЯ КАЛИБРОВКИ ---
BENCHMARK_GLOSSARY_SIZE = 100    # Увеличиваем количество терминов
BENCHMARK_TEXT_SIZE = 10000     # Увеличиваем размер текста
# --- КОНЕЦ НОВЫХ КОНСТАНТ ---

class InitialSetupDialog(QDialog):
    """
    Единый диалог для настройки перевода.
    """
    tasks_changed = pyqtSignal()
    def __init__(self, parent=None, prefill_data=None):
        super().__init__(parent)
        
        # --- Флаги и базовые атрибуты (быстрая инициализация) ---
        self._initial_show_done = False
        self.prefill_data = prefill_data
        
        
        self.setMinimumSize(700, 690) # Компактный размер
        self.setWindowFlags(
            QtCore.Qt.WindowType.Dialog |
            QtCore.Qt.WindowType.WindowMinimizeButtonHint |
            QtCore.Qt.WindowType.WindowMaximizeButtonHint |
            QtCore.Qt.WindowType.WindowCloseButtonHint
        )
        
        app = QtWidgets.QApplication.instance()
        self.app = app
        
        self.version = ""
        if app and app.global_version:
            self.version = app.global_version
        self.setWindowTitle(f"Настройка сессии перевода {self.version}")
        
        self.settings_manager = app.get_settings_manager()
        self.context_manager = app.context_manager
        self.bus = app.event_bus
        self.engine = app.engine
        self.engine_thread = app.engine_thread
        self.task_manager = app.task_manager if hasattr(app, 'task_manager') else None
        
        self.selected_file = None
        self.html_files = []
        self.output_folder = None
        self.project_manager = None
        self.is_session_active = False
        self.current_project_folder_loaded = None # <--- ДОБАВЬТЕ ЭТУ СТРОКУ
        self.is_settings_dirty = False
        self.local_set = False
        self.cpu_performance_index = None
        self.is_fuzzy_disabled_by_system = False
        self.global_settings = None
        
        self.initial_glossary_state = {}
        self.active_session_id = None
        self.this_dialog_started_the_session = False # <<< ДОБАВЬТЕ ЭТУ СТРОКУ
        self.is_blocked_by_child_dialog = False # <<< ДОБАВЬТЕ ЭТУ СТРОКУ

        # --- Создание "скелета" UI ---
        self._init_lazy_ui_skeleton()

        # --- Подключение к глобальным событиям ---
        app.event_bus.event_posted.connect(self.on_event)
       

    def _populate_full_ui(self):
        """
        Создает и размещает все "тяжелые" виджеты.
        Версия 3.0: Объединенная вкладка 'Настройки' (Ключи + Модель).
        """
        content_layout = QVBoxLayout(self.main_content_widget)
        content_layout.setContentsMargins(10, 10, 10, 0)
    
        # --- ШАГ 1: СОЗДАЕМ ВСЕ КАСТОМНЫЕ ВИДЖЕТЫ-КОМПОНЕНТЫ ---
        self.paths_widget = ProjectPathsWidget(self)
        self.task_management_widget = TaskManagementWidget(self)
        self.log_widget = LogWidget(self)
        self.glossary_widget = GlossaryWidget(self, settings_manager=self.settings_manager)
        
        self.preset_widget = PresetWidget(
            parent=self, preset_name="Промпт", default_prompt_func=api_config.default_prompt,
            load_presets_func=self.settings_manager.load_named_prompts,
            save_presets_func=self.settings_manager.save_named_prompts,
            get_last_text_func=self.settings_manager.get_custom_prompt,
            get_last_preset_func=self.settings_manager.get_last_prompt_preset_name,
            save_last_preset_func=self.settings_manager.save_last_prompt_preset_name
        )
        self.preset_widget.load_last_session_state()
        
        self.translation_options_widget = TranslationOptionsWidget(self)
        server_manager = self.app.get_server_manager() if hasattr(self.app, 'get_server_manager') else None
        self.model_settings_widget = ModelSettingsWidget(self, settings_manager=self.settings_manager, server_manager=server_manager)
        self.project_actions_widget = ProjectActionsWidget(self)
        self.status_bar = StatusBarWidget(self, event_bus=self.bus, engine=self.engine)
    
        # --- ШАГ 2: СОЗДАЕМ ОБЪЕДИНЕННУЮ ВКЛАДКУ "НАСТРОЙКИ" ---
        settings_tab = QWidget()
        settings_layout = QVBoxLayout(settings_tab)
        settings_layout.setContentsMargins(5, 5, 5, 5)
        settings_layout.setSpacing(10)

        # 2.1. Группа Ключей и Распределения (Верхняя часть)
        # Сначала создаем виджет распределения, который внедрится в KeyManagementWidget
        distribution_group = QGroupBox("Параллельная обработка")
        dist_controls_layout = QHBoxLayout(distribution_group)
        dist_controls_layout.addWidget(QLabel("Обработчиков:  "))
        
        self.instances_spin = NoScrollSpinBox()
        self.instances_spin.setRange(1, 1)
        self.instances_spin.setToolTip(
            "Количество параллельных обработчиков для одновременного перевода глав.\n"
            "Каждый обработчик использует один активный API-ключ.\n"
            "Увеличение этого значения ускоряет перевод, но быстрее расходует ваши ключи."
        )
        self.instances_spin.valueChanged.connect(self._update_distribution_info_from_widget)
        dist_controls_layout.addWidget(self.instances_spin)
        dist_controls_layout.addStretch()
        
        self.distribution_label = QLabel("…")
        self.distribution_label.setStyleSheet("color: #90EE90; font-size: 10pt; font-weight: bold;") 
        dist_controls_layout.addWidget(self.distribution_label)
    
        # Теперь создаем сам KeyManagementWidget
        server_manager = self.app.get_server_manager() if hasattr(self.app, 'get_server_manager') else None
        self.key_management_widget = KeyManagementWidget(
            self.settings_manager,
            parent=self,
            distribution_group_widget=distribution_group,
            server_manager=server_manager
        )
        # Подключаем сигналы ключей
        self.key_management_widget.active_keys_changed.connect(self._update_distribution_info_from_widget)
        self.key_management_widget.active_keys_changed.connect(self.check_ready)

        # Оборачиваем в группу для визуальной целостности
        keys_container_group = QGroupBox("API Ключи и Распределение нагрузки")
        keys_container_layout = QVBoxLayout(keys_container_group)
        keys_container_layout.setContentsMargins(2, 8, 2, 2)
        keys_container_layout.addWidget(self.key_management_widget)
        
        # Добавляем группу ключей наверх (stretch=1, чтобы она занимала все свободное место)
        settings_layout.addWidget(keys_container_group, 1)

        # 2.2. Группа Настроек Модели (Нижняя часть)
        # model_settings_widget уже является QGroupBox, просто добавляем его
        # stretch=0, чтобы она занимала только необходимый минимум высоты
        settings_layout.addWidget(self.model_settings_widget, 0)
        self.model_settings_widget.prettify_checkbox.setVisible(True)
        # --- ШАГ 3: СОБИРАЕМ QTabWidget ---
        tabs_group = QTabWidget()
        
        # Вкладка 1: Настройки (Объединенная)
        tabs_group.addTab(settings_tab, "Настройки")
        
        # Вкладка 2: Список Задач + Оптимизация
        tasks_tab_container = QWidget()
        tasks_tab_layout = QVBoxLayout(tasks_tab_container)
        tasks_tab_layout.setContentsMargins(5, 5, 5, 5)
        tasks_tab_layout.addWidget(self.task_management_widget, 1) 
        tasks_tab_layout.addWidget(self.translation_options_widget, 0)
        tabs_group.addTab(tasks_tab_container, "Список Задач")

        # Остальные вкладки
        tabs_group.addTab(self.log_widget, "Логирование")
        tabs_group.addTab(self.glossary_widget, "Глоссарий")
        tabs_group.addTab(self.preset_widget, "Промпт")
        
        # --- ШАГ 4: КОМПОНОВКА ОСНОВНОГО ОКНА ---
        content_layout.addWidget(self.paths_widget)
        content_layout.addWidget(tabs_group, 1)
        
        # Нижняя панель с кнопками
        bottom_panel_layout = QHBoxLayout()
        bottom_panel_layout.setContentsMargins(6, 6, 6, 6)
        
        self.use_project_settings_btn = QtWidgets.QPushButton("🌐 Глобальные настройки")
        self.use_project_settings_btn.setCheckable(True)
        self.use_project_settings_btn.setChecked(False)
        self.use_project_settings_btn.setVisible(False)
        
        self.start_btn = QPushButton("🚀 Старт")
        self.stop_btn = QPushButton("❌ Стоп")
        self.stop_btn.setEnabled(False)
        self.dry_run_btn = QPushButton("🧪 Пробный запуск")
        self.close_btn = QPushButton("Выход")
        
        bottom_panel_layout.addWidget(self.project_actions_widget)
        bottom_panel_layout.addWidget(self.use_project_settings_btn)
        
        right_buttons_layout = QHBoxLayout()
        right_buttons_layout.addStretch()
        right_buttons_layout.addWidget(self.dry_run_btn)
        right_buttons_layout.addWidget(self.start_btn)
        right_buttons_layout.addWidget(self.stop_btn)
        right_buttons_layout.addWidget(self.close_btn)
        
        bottom_panel_layout.addLayout(right_buttons_layout)
        content_layout.addLayout(bottom_panel_layout)
        
        content_layout.addWidget(self.status_bar)
        
        self._connect_signals()
        self.check_ready()
        
    def _connect_signals(self):
        """Подключает все сигналы и слоты для виджетов диалога."""
        self.use_project_settings_btn.toggled.connect(self._toggle_project_settings_mode)
        self.paths_widget.file_selected.connect(self.on_file_selected)
        self.paths_widget.folder_selected.connect(self.on_folder_selected)
        self.paths_widget.chapters_reselection_requested.connect(self.reselect_chapters)
        self.paths_widget.swap_file_requested.connect(self._on_swap_file_requested)
        self.project_actions_widget.open_history_requested.connect(self._open_project_history)
        self.project_actions_widget.sync_project_requested.connect(self._run_project_sync) 
        
        self.translation_options_widget.settings_changed.connect(lambda: self._prepare_and_display_tasks(clean_rebuild=False))
        self.task_management_widget.tasks_changed.connect(lambda: self._prepare_and_display_tasks(clean_rebuild=True))
        
        self.model_settings_widget.recalibrate_requested.connect(self._calibrate_cpu)
        self.key_management_widget.active_keys_changed.connect(self._update_instances_spinbox_limit)
        self.key_management_widget.active_keys_changed.connect(self.check_ready)
        
        # --- ИЕРАРХИЯ Подключаемся только к TaskManagementWidget ---
        self.task_management_widget.tasks_changed.connect(lambda: self._prepare_and_display_tasks(clean_rebuild=True))
        self.task_management_widget.reorder_requested.connect(self._handle_task_reorder)
        self.task_management_widget.duplicate_requested.connect(self._handle_task_duplication)
        self.task_management_widget.remove_selected_requested.connect(self._handle_task_removal)
        self.task_management_widget.copy_originals_requested.connect(self._copy_original_chapters)
        self.task_management_widget.reanimate_requested.connect(self._handle_task_reanimation)
        self.task_management_widget.filter_all_translated_requested.connect(self._filter_all_translated_tasks)
        self.task_management_widget.filter_validated_requested.connect(self._filter_validated_tasks)
        self.task_management_widget.filter_packaging_requested.connect(self._open_filter_packaging_dialog)
        self.task_management_widget.validation_requested.connect(self.open_translation_validator)
        self.task_management_widget.backup_restore_requested.connect(self._handle_backup_restore)
        # --------------------------------------------------------------------------
        
        self.start_btn.clicked.connect(self._start_translation)
        self.stop_btn.clicked.connect(self._stop_translation)
        self.dry_run_btn.clicked.connect(self.perform_dry_run)
        self.close_btn.clicked.connect(self.reject)
        self.project_actions_widget.build_epub_requested.connect(self._open_epub_builder_standalone)
    
        self.model_settings_widget.settings_changed.connect(self._mark_settings_as_dirty)
        self.key_management_widget.active_keys_changed.connect(self._mark_settings_as_dirty)
        self.preset_widget.text_changed.connect(self._mark_promt_as_dirty)
        self.glossary_widget.glossary_changed.connect(self._mark_settings_as_dirty)
    
    def _create_glossary_tab_content(self) -> QWidget:
        """Просто возвращает уже созданный GlossaryWidget."""
        return self.glossary_widget
    
    def _create_prompt_tab_content(self) -> QWidget:
        """Просто возвращает уже созданный PresetWidget."""
        return self.preset_widget
    
    def _load_initial_data(self):
        """
        Выполняет всю долгую инициализацию виджетов после того,
        как окно было показано.
        """
        print("[DEBUG] Запуск отложенной загрузки данных для InitialSetupDialog…")
        
        # 1. Первоначальная синхронизация провайдера и ключей.
        #    Это может читать с диска, поэтому делаем это здесь.
        self.key_management_widget.provider_combo.currentIndexChanged.emit(
            self.key_management_widget.provider_combo.currentIndex()
        )

        # 3. Проверяем, нужно ли автозаполнение из валидатора
        if self.prefill_data and self.prefill_data.get("is_restarting"):
            self.autofill_from_validator()
        
        # 4. Финальная проверка состояния кнопок после загрузки всех данных
        self.check_ready()
        print("[DEBUG] Отложенная загрузка данных для InitialSetupDialog завершена.")
    
    # --------------------------------------------------------------------
    # МЕТОДЫ СОЗДАНИЯ ЭЛЕМЕНТОВ UI
    # --------------------------------------------------------------------

    def _update_distribution_info_from_widget(self):
        num_chapters = len(self.html_files)
        if num_chapters == 0:
            self.distribution_label.setText("…")
            self.distribution_label.setStyleSheet("color: grey;")
            return
    
        num_keys = len(self.key_management_widget.get_active_keys())
        self.instances_spin.setMaximum(num_keys if num_keys > 0 else 1)
        
        num_instances = self.instances_spin.value()
        
        if num_instances == 0: # На случай, если ключей 0
            self.distribution_label.setText("Нет активных ключей")
            self.distribution_label.setStyleSheet("color: orange; font-weight: bold;")
            return
            
        if num_instances > num_chapters:
            self.distribution_label.setText(f"Клиентов ({num_instances}) > глав ({num_chapters})")
            self.distribution_label.setStyleSheet("color: orange; font-weight: bold;")
            return
        
        # Расчет среднего с округлением вверх
        avg_chapters = math.ceil(num_chapters / num_instances)
        
        text = f"≈ {avg_chapters} глав / клиент"
        self.distribution_label.setText(text)
        self.distribution_label.setStyleSheet("color: #90EE90; font-size: 10pt; font-weight: bold;")

    def _post_event(self, name: str, data: dict = None):
        session_id = self.engine.session_id if self.engine and self.engine.session_id else None
        event = {
            'event': name,
            'source': 'InitialSetupDialog',
            'session_id': session_id,
            'data': data or {}
        }
        self.bus.event_posted.emit(event)

    def _handle_geoblock_detected(self):
        """
        Показывает пользователю кастомный, терапевтический диалог о геоблокировке,
        который не пугает, а предлагает решение.
        """
        # Просто создаем и запускаем наш новый, умный диалог.
        dialog = GeoBlockDialog(self)
        dialog.exec()
    
    def create_glossary_tab(self, tabs_group):
        # 1. Создаем экземпляр нашего виджета, передавая ему settings_manager
        self.glossary_widget = GlossaryWidget(self, settings_manager=self.settings_manager)
        
        # 3. Добавляем его как вкладку
        tabs_group.addTab(self.glossary_widget, "Глоссарий и Контекст Проекта")
    
    
    def save_ui_state(self, ui_state_dict):
        """
        Загружает текущие настройки, обновляет их значениями из UI
        и сохраняет обратно в файл. Это безопасный способ обновить
        только те настройки, которыми управляет UI.
        """
        with self.file_lock:
            settings = self.load_settings()
            
            # Обновляем только те ключи, которые приходят из UI
            # (используем префикс 'last_', как в save_last_settings)
            settings['last_model'] = ui_state_dict.get('model')
            settings['last_temperature'] = ui_state_dict.get('temperature')
            settings['last_concurrent_requests'] = ui_state_dict.get('rpm_limit')
            settings['last_chunking'] = ui_state_dict.get('chunking')
            settings['last_dynamic_glossary'] = ui_state_dict.get('dynamic_glossary')
            settings['last_system_instruction'] = ui_state_dict.get('use_system_instruction')
            settings['last_thinking_enabled'] = ui_state_dict.get('thinking_enabled')
            settings['last_thinking_budget'] = ui_state_dict.get('thinking_budget')

            # Также сохраняем последние использованные пресеты
            if 'last_prompt_preset' in ui_state_dict:
                settings['last_prompt_preset'] = ui_state_dict['last_prompt_preset']
            if 'custom_prompt' in ui_state_dict:
                settings['custom_prompt'] = ui_state_dict['custom_prompt']

            # Сохраняем обновленный словарь
            return self.save_settings(settings)
            
    def create_prompt_tab(self, tabs_group):
        # 1. Создаем экземпляр нашего виджета с полной конфигурацией
        self.preset_widget = PresetWidget(
            parent=self,
            preset_name="Промпт",
            default_prompt_func=api_config.default_prompt,
            load_presets_func=self.settings_manager.load_named_prompts,
            save_presets_func=self.settings_manager.save_named_prompts,
            get_last_text_func=self.settings_manager.get_custom_prompt,
            get_last_preset_func=self.settings_manager.get_last_prompt_preset_name,
            save_last_preset_func=self.settings_manager.save_last_prompt_preset_name
        )
        self.preset_widget.load_last_session_state()
        # 3. Добавляем его как вкладку
        tabs_group.addTab(self.preset_widget, "Промпт (опционально)")
        

    def _update_recommendations(self):
        """
        Централизованно обновляет рекомендации по размеру задачи.
        Берет модель из виджета моделей и передает в виджет опций.
        """
        if not self.model_settings_widget or not self.translation_options_widget:
            return
        
        model_name = self.model_settings_widget.model_combo.currentText()
        self.translation_options_widget.update_recommendations_from_model(model_name)

    
    def _update_distribution_info(self):
        num_chapters = len(self.html_files)
        if num_chapters == 0: self.distribution_label.setText("Сначала выберите главы."); return
        num_instances = self.instances_spin.value()
        if num_instances > num_chapters: self.distribution_label.setText(f"<font color='orange'><b>Предупреждение:</b> Клиентов ({num_instances}) больше, чем заданий ({num_chapters}).</font>"); return
        base, extra = num_chapters // num_instances, num_chapters % num_instances
        
        avg_chapters = math.ceil(num_chapters / num_instances)
        
        text = f"≈ {avg_chapters} глав / клиент"
        self.distribution_label.setText(text)


    # ЗАМЕНИТЕ ЭТОТ МЕТОД
    def _calculate_potential_output_size(self, html_content, is_cjk):
        """
        Вычисляет потенциальный размер ответа модели на основе содержимого HTML.
        Устаревший метод, используйте глобальную функцию calculate_potential_output_size.
        """
        return calculate_potential_output_size(html_content, is_cjk)
    
    # --------------------------------------------------------------------
    # ОБЩАЯ ЛОГИКА И ОБРАБОТЧИКИ
    # --------------------------------------------------------------------
    
    def autofill_from_validator(self):
        """Заполняет поля данными, полученными из валидатора."""
        if not self.prefill_data: return

        epub_path = self.prefill_data.get("epub_path")
        chapters = self.prefill_data.get("chapters")

        if epub_path and chapters:
            self.selected_file = epub_path
            
            self.paths_widget.set_file_path(epub_path)
            
            
            self._process_selected_file(pre_selected_chapters=chapters)
        
            if not self.output_folder:
                self.output_folder = os.path.dirname(epub_path)
                
                self.paths_widget.set_folder_path(self.output_folder)
                
    
    @pyqtSlot(dict)
    def on_event(self, event_data: dict):
        """
        Обрабатывает только те события, которые касаются самого диалога,
        а не его дочерних виджетов.
        """
        event_name = event_data.get('event')
        data = event_data.get('data', {})
        
        if self.is_blocked_by_child_dialog and event_name != 'tasks_for_retry_ready':
            return
        
        # Этот виджет теперь реагирует только на старт и финиш сессии
        if event_name == 'session_started':
            self.is_session_active = True
            # total_tasks теперь обрабатывается в StatusBarWidget
            self._set_controls_enabled(False)
            return
        if event_name == 'assembly_finished' and self.is_session_active == False:
            if self.project_manager:
                self.project_manager.reload_data_from_disk()
        
        if event_name == 'session_finished':
            self._shutdown_reason = data.get('reason')
            self._log_session_id = data.get('session_id_log')
            QtCore.QMetaObject.invokeMethod(
                self, "_on_session_finished", 
                QtCore.Qt.ConnectionType.QueuedConnection
            )
            self.this_dialog_started_the_session = False
            return
    
        if event_name == 'tasks_for_retry_ready':
            epub_path, chapter_paths = data.get('epub_path'), data.get('chapter_paths')
            if epub_path and chapter_paths: self.add_files_for_retry(epub_path, chapter_paths)
            return
    
        # Логика для geoblock остается здесь, так как она показывает модальное окно
        if self.is_session_active and event_name == 'geoblock_detected':
            self._handle_geoblock_detected()
    
    def reselect_chapters(self):
        """
        Повторно открывает диалог выбора глав для уже выбранного файла.
        Вызывается при нажатии на кнопку со счетчиком глав.
        """
        if not self.selected_file:
            # Эта проверка на всякий случай, если кнопка будет видна, когда не должна
            QMessageBox.warning(self, "Ошибка", "Сначала выберите EPUB файл.")
            return
        
        # --- НОВЫЙ БЛОК: Принудительная синхронизация ---
        if self.project_manager:
            self.project_manager.reload_data_from_disk()
            print("[INFO] Карта проекта принудительно обновлена перед выбором глав.")
        # --- КОНЕЦ НОВОГО БЛОКА ---
        self._process_selected_file()
    

    def _process_selected_file(self, pre_selected_chapters=None):
        """
        Главная функция для работы с EPUB. Финальная версия с правильной последовательностью.
        """
        if not self.selected_file or not os.path.exists(self.selected_file):
            return
        if self.task_manager:
            self.task_manager.clear_glossary_results()
        try:
            success, selected_files = EpubHtmlSelectorDialog.get_selection(
                parent=self,
                epub_filename=self.selected_file, 
                output_folder=self.output_folder, 
                pre_selected_chapters=pre_selected_chapters if pre_selected_chapters is not None else self.html_files,
                project_manager=self.project_manager
            )

            if success:
                self.html_files = selected_files
                self.paths_widget.update_chapters_info(len(self.html_files))

                if self.output_folder:
                    self._handle_project_initialization()
                else:
                    self._prepare_and_display_tasks(clean_rebuild=True)

        except Exception as e:
            # --- БЛОК НА ЗАМЕНУ ---
            tb_str = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            # --- ИЗМЕНЕНИЕ: Форматируем сообщение с двойным переносом строки ---
            error_message = (
                f"Не удалось проанализировать файл '{os.path.basename(self.selected_file)}'.\n\n" # <--- Основной текст
                f"--- Полный Traceback ---\n{tb_str}" # <--- Детали
            )
            print(f"[ERROR] Локальная ошибка в _process_selected_file:\n{error_message}")
            
            # Просто вызываем наш "патченный" метод
            QtWidgets.QMessageBox.critical(self, "Ошибка обработки EPUB", error_message)
            # --- КОНЕЦ БЛОКА ---
            self.selected_file = None
            self.html_files = []
            self.paths_widget.set_file_path(None)
            self.check_ready()
    
    def _mark_settings_as_dirty(self):
        """Слот, который устанавливает флаг 'грязного' состояния и обновляет заголовок окна."""
        if self.is_settings_dirty or self.is_session_active:
            return
        if not self.local_set:
            return
        self.is_settings_dirty = True
        self.setWindowTitle(self.windowTitle() + "*")

    def _mark_promt_as_dirty(self):
        """Слот, который устанавливает флаг 'грязного' состояния и обновляет заголовок окна."""
        if self.is_settings_dirty or self.is_session_active:
            return
        self.is_settings_dirty = True
        self.setWindowTitle(self.windowTitle() + "*")


    def _get_ui_state_for_saving(self):
        """Собирает все релевантные настройки из UI в один словарь для сохранения."""
        state = {}
        state.update(self.model_settings_widget.get_settings())
        state.update({
            'custom_prompt': self.preset_widget.get_prompt(),
            'last_prompt_preset': self.preset_widget.get_current_preset_name()
        })
        # Добавьте сюда другие настройки, если они должны сохраняться
        return state

    def _save_current_ui_settings(self):
        """Сохраняет текущее состояние UI в активный файл настроек."""
        app = QtWidgets.QApplication.instance()
        current_manager = app.get_settings_manager()
        ui_state = self._get_ui_state_for_saving()
        
        current_manager.save_ui_state(ui_state)
        
        self.is_settings_dirty = False
        self.setWindowTitle(self.windowTitle().replace("*", ""))
        print(f"[SETTINGS] Настройки сохранены в: {current_manager.config_file}")
    
    
    @QtCore.pyqtSlot()
    def _continue_loading_project_and_update_all(self):
        """
        Запускает полную асинхронную цепочку загрузки проекта.
        Используется после создания нового проекта или принудительной перезагрузки.
        """
        # Этот метод теперь просто "пробрасывает" вызов дальше,
        # обеспечивая единую точку входа для разных сценариев.
        self._process_selected_file()
    
    def _ask_and_filter_chapters(self):
        """
        Показывает диалог с опциями фильтрации для уже существующего списка глав.
        """
        if not self.project_manager or not self.html_files:
            return

        has_translated_chapters = any(self.project_manager.get_versions_for_original(ch) for ch in self.html_files)
        if not has_translated_chapters:
            return # Если переведенных глав нет, фильтровать нечего

        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Обновление списка глав")
        msg_box.setIcon(QMessageBox.Icon.Question)
        msg_box.setText("Проект уже содержит переведенные главы. Что делать с текущим списком?")
        
        btn_skip_all = msg_box.addButton("Пропустить все переведенные", QMessageBox.ButtonRole.ActionRole)
        btn_skip_validated = msg_box.addButton("Пропустить только 'готовые'", QMessageBox.ButtonRole.ActionRole)
        btn_keep_all = msg_box.addButton("Оставить все как есть", QMessageBox.ButtonRole.AcceptRole)
        
        msg_box.exec()
        clicked_button = msg_box.clickedButton()

        if clicked_button == btn_skip_all:
            self._filter_all_translated_chapters(silent=True)
        elif clicked_button == btn_skip_validated:
            self._filter_validated_chapters(silent=True)
        # Если нажата "Оставить все", ничего не делаем
    
    
    def _handle_project_initialization(self, select_mode=True):
        """
        Главный оркестратор. Вызывается, когда и файл, и папка, и главы заданы.
        Версия 2.0: Корректно обрабатывает создание подпапки и перемещает оригинал.
        """
        import shutil
    
        file_path = self.selected_file
        folder_path = self.output_folder
        
        history = self.settings_manager.load_project_history()
        is_known_project = any(p.get('epub_path') == file_path and p.get('output_folder') == folder_path for p in history)
    
        # Изначально считаем, что будем работать с выбранными путями
        effective_folder = folder_path.replace('\\', '/')
        effective_file_path = file_path.replace('\\', '/')
    
        if not is_known_project:
            is_folder_reused = any(p.get('output_folder') == folder_path and p.get('epub_path') != file_path for p in history)
            main_text = f"Вы выбрали папку <b>'{os.path.basename(folder_path)}'</b> для нового проекта."
            if is_folder_reused:
                main_text += "<br><br><b style='color: orange;'>Внимание:</b> Эта папка уже используется для другого проекта. Настоятельно рекомендуется создать подпапку."
            base_name = os.path.splitext(os.path.basename(file_path))[0]
            
            dialog = ProjectFolderDialog(self, main_text, base_name)
            if not dialog.exec():
                self.output_folder = None
                self.paths_widget.set_folder_path(None)
                self._on_project_data_changed()
                return
            
            choice = dialog.choice
            copy_original = dialog.copy_file_checked # Теперь это флаг "переместить"
    
            if choice == 'subfolder':
                subfolder_path = os.path.join(folder_path, base_name)
                try:
                    os.makedirs(subfolder_path, exist_ok=True)
                    # Переназначаем effective_folder на новую подпапку
                    effective_folder = subfolder_path
                except OSError as e:
                    QMessageBox.critical(self, "Ошибка", f"Не удалось создать подпапку:\n{e}")
                    return
            
            if copy_original: # Теперь это "переместить"
                try:
                    # os.path.join сам все нормализует
                    destination_path = os.path.join(effective_folder, os.path.basename(file_path))
                    
                    if os.path.abspath(file_path) != os.path.abspath(destination_path):
                        shutil.move(file_path, destination_path)
                        # Обновляем путь к файлу, с которым будет работать сессия
                        effective_file_path = destination_path 
                        print(f"[INFO] Оригинальный файл перемещен в папку проекта: {destination_path}")
                except (shutil.Error, OSError) as e:
                    QMessageBox.critical(self, "Ошибка перемещения", f"Не удалось переместить исходный файл:\n{e}")
                    return

        # Добавляем в историю уже финальные, эффективные пути
        self.settings_manager.add_to_project_history(effective_file_path, effective_folder)
    
        # Финально устанавливаем правильные пути в состояние диалога и UI
        self.selected_file = effective_file_path
        self.output_folder = effective_folder
        self.project_manager = TranslationProjectManager(self.output_folder)
        self.paths_widget.set_file_path(self.selected_file)
        self.paths_widget.set_folder_path(self.output_folder)
        
        if self.html_files:
            self._ask_and_filter_chapters()
    
        self._on_project_data_changed()

    def _update_cjk_options_for_widgets(self):
        """
        Анализирует данные, уже собранные виджетом оптимизации,
        и обновляет CJK опции.
        """
        if not self.html_files:
            self.model_settings_widget.update_cjk_options_availability(enabled=False)
            return

        # Берем готовые данные из виджета
        compositions = self.translation_options_widget.chapter_compositions
        if not compositions:
            self.model_settings_widget.update_cjk_options_availability(enabled=True, error=True)
            return
            
        is_any_cjk = any(comp.get('is_cjk', False) for comp in compositions.values())
        
        self.model_settings_widget.update_cjk_options_availability(enabled=True, is_cjk_recommended=is_any_cjk)
    
    @pyqtSlot(str)
    def on_file_selected(self, file_path):
        """Слот с логикой "разрыва связи" при смене файла."""
        if not file_path: return

        # --- НАЧАЛО КЛЮЧЕВОГО ИСПРАВЛЕНИЯ: Атомарный сброс состояния ---
        # Если выбранный файл отличается от текущего, это означает смену контекста.
        # Мы ОБЯЗАНЫ немедленно сбросить список глав, чтобы предотвратить
        # использование списка глав от старого файла с новым файлом.
        if self.selected_file != file_path:
            self.html_files = []
            # Немедленно обновляем UI, чтобы пользователь видел, что выбор глав сброшен
            self.paths_widget.update_chapters_info(0)
            if self.task_manager:
                # Очищаем очередь задач, так как она тоже относится к старому файлу
                self.task_manager.clear_all_queues()
        # --- КОНЕЦ КЛЮЧЕВОГО ИСПРАВЛЕНИЯ ---

        # Далее идет существующая логика проверки на "разрыв связи" с проектом.
        # Она остается без изменений, так как важна.
        if self.selected_file and self.output_folder:
            temp_pm = TranslationProjectManager(self.output_folder)
            cache_data = temp_pm.load_size_cache()
            
            if cache_data:
                _, is_cache_valid = get_epub_chapter_sizes_with_cache(temp_pm, file_path, return_cache_status=True)
                
                if not is_cache_valid:
                    QMessageBox.information(self, "Связь с проектом разорвана",
                                            f"Выбранный файл '{os.path.basename(file_path)}' не соответствует проекту в папке '{os.path.basename(self.output_folder)}'.\n\n"
                                            "Выбор папки был сброшен. Пожалуйста, выберите новую папку для этого файла.")
                    # --- РАДИКАЛЬНАЯ ОЧИСТКА ---
                    self.output_folder = None
                    self.project_manager = None
                    self.paths_widget.set_folder_path(None)
                    self.html_files = []
                    self.paths_widget.update_chapters_info(0) # Обновляем UI счетчика
                    if self.task_manager:
                        self.task_manager.clear_all_queues()
                    # --- КОНЕЦ ОЧИСТКИ ---
    
        # Устанавливаем новый выбранный файл
        self.selected_file = file_path
        self.paths_widget.set_file_path(file_path)
        
        # Запускаем дальнейшую обработку
        if self.output_folder:
            self._handle_project_initialization()
        else:
            self._process_selected_file()
        self.check_ready()
        
    def on_folder_selected(self, folder):
        """Слот с логикой "разрыва связи" при смене папки."""
        if not folder: return
    
        if self.selected_file and self.output_folder:
            temp_pm = TranslationProjectManager(folder)
            cache_data = temp_pm.load_size_cache()
            
            if cache_data:
                _, is_cache_valid = get_epub_chapter_sizes_with_cache(temp_pm, self.selected_file, return_cache_status=True)
                
                if not is_cache_valid:
                    QMessageBox.information(self, "Связь с проектом разорвана",
                                            f"Папка '{os.path.basename(folder)}' содержит проект для другого файла.\n\n"
                                            "Выбор файла был сброшен. Пожалуйста, выберите EPUB, соответствующий этому проекту, или создайте новый проект в другой папке.")
                    # --- РАДИКАЛЬНАЯ ОЧИСТКА ---
                    self.selected_file = None
                    self.project_manager = None
                    self.html_files = []
                    self.paths_widget.set_file_path(None)
                    self.paths_widget.update_chapters_info(0)
                    if self.task_manager:
                        self.task_manager.clear_all_queues()
                    # --- КОНЕЦ ОЧИСТКИ ---
    
        self.output_folder = folder
        self.paths_widget.set_folder_path(folder)
    
        if self.selected_file:
            self._handle_project_initialization()
        else:
            self._on_project_data_changed()
        self.check_ready()

    def _on_swap_file_requested(self):
        """
        Процедура бесшовного переезда на новый файл EPUB.
        Переименовывает старый в _old_i, перемещает новый в папку проекта.
        """
        if not self.selected_file or not self.output_folder:
            return

        # 1. Выбор нового файла
        new_file_source, _ = QFileDialog.getOpenFileName(
            self, "Выберите НОВУЮ версию EPUB файла", 
            os.path.dirname(self.selected_file), "EPUB файлы (*.epub)"
        )
        if not new_file_source or os.path.abspath(new_file_source) == os.path.abspath(self.selected_file):
            return

        # 2. Анализ совместимости
        self.status_bar.set_permanent_message("Анализ совместимости глав...")
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        
        from ...utils.epub_tools import compare_epubs_for_swap, get_epub_chapter_order
        comparison_results = compare_epubs_for_swap(self.selected_file, new_file_source)
        
        QtWidgets.QApplication.restoreOverrideCursor()
        self.status_bar.clear_message()

        if comparison_results is None:
            QMessageBox.critical(self, "Ошибка", "Не удалось прочитать или сравнить файлы.")
            return

        # Сводка
        matches = [p for p, s in comparison_results.items() if s == 'match']
        mismatches = [p for p, s in comparison_results.items() if s == 'mismatch']
        new_chaps = [p for p, s in comparison_results.items() if s == 'new']
        
        msg = QMessageBox(self)
        msg.setWindowTitle("Переезд на новую версию файла")
        msg.setIcon(QMessageBox.Icon.Question)
        msg_text = (
            f"✅ <b>Совпало: {len(matches)}</b> (переводы сохранятся)\n"
            f"❌ <b>Изменилось: {len(mismatches)}</b> (переводы будут удалены)\n"
            f"🆕 <b>Новых глав: {len(new_chaps)}</b>"
        )
        msg.setText(msg_text)
        msg.setInformativeText(
            "Программа переименует текущий файл в '_old', перенесет новый файл на его место "
            "и обновит базу проекта. Продолжить?"
        )
        btn_proceed = msg.addButton("Да, выполнить переезд", QMessageBox.ButtonRole.AcceptRole)
        msg.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        msg.exec()
        
        if msg.clickedButton() != btn_proceed:
            return

        # 3. ФИЗИЧЕСКИЙ ПЕРЕЕЗД ФАЙЛОВ
        import shutil
        try:
            # А. Генерируем имя для архивации старого
            base, ext = os.path.splitext(self.selected_file)
            i = 1
            while os.path.exists(f"{base}_old_{i}{ext}"):
                i += 1
            old_version_path = f"{base}_old_{i}{ext}"

            # Б. Архивируем старый (переименовываем)
            os.rename(self.selected_file, old_version_path)
            
            # В. Перемещаем новый файл на место старого (или рядом, если имена разные)
            # Мы будем использовать путь в папке проекта для нового файла
            target_new_path = os.path.join(os.path.dirname(self.selected_file), os.path.basename(new_file_source))
            
            # Если пользователь выбрал файл, который и так лежит в этой папке (но под другим именем)
            if os.path.abspath(new_file_source) != os.path.abspath(target_new_path):
                shutil.copy2(new_file_source, target_new_path)
            
            # Запоминаем новый путь
            new_active_file = target_new_path

        except Exception as e:
            QMessageBox.critical(self, "Ошибка файловой системы", f"Не удалось переместить файлы: {e}")
            return

        # 4. ЧИСТКА КАРТЫ ПРОЕКТА И ДИСКА
        self.project_manager.reload_data_from_disk()
        files_deleted_count = 0
        
        # Удаляем переводы для несовпавших глав
        for path in mismatches:
            versions = self.project_manager.get_versions_for_original(path)
            for suffix, rel_path in versions.items():
                full_path = os.path.join(self.output_folder, rel_path)
                if os.path.exists(full_path):
                    try: os.remove(full_path); files_deleted_count += 1
                    except: pass
            
            # Сносим ветку из JSON
            with self.project_manager.lock:
                current_data = self.project_manager._load_unsafe()
                if path in current_data: del current_data[path]
                self.project_manager._save_unsafe(current_data)

        # Удаляем из карты главы, которых вообще нет в новом EPUB
        current_map = self.project_manager.get_full_map()
        new_file_all_paths = set(comparison_results.keys())
        for old_path in list(current_map.keys()):
            if old_path not in new_file_all_paths:
                versions = current_map[old_path]
                for suffix, rel_path in versions.items():
                    full_path = os.path.join(self.output_folder, rel_path)
                    if os.path.exists(full_path):
                        try: os.remove(full_path); files_deleted_count += 1
                        except: pass
                with self.project_manager.lock:
                    data = self.project_manager._load_unsafe()
                    if old_path in data: del data[old_path]
                    self.project_manager._save_unsafe(data)

        # 5. ОБНОВЛЕНИЕ UI
        self.selected_file = new_active_file
        self.paths_widget.set_file_path(self.selected_file)
        
        # Обновляем историю проектов
        self.settings_manager.add_to_project_history(self.selected_file, self.output_folder)
        
        # Берем все главы из нового файла как текущий выбор
        self.html_files = get_epub_chapter_order(self.selected_file)
        
        # Полная перерисовка
        self._on_project_data_changed()
        
        QMessageBox.information(self, "Переезд завершен", 
            f"Новый файл: {os.path.basename(new_active_file)}\n"
            f"Старая версия сохранена как: {os.path.basename(old_version_path)}\n\n"
            f"Удалено неактуальных переводов: {files_deleted_count}.")

      
    def _on_folder_sync_finished(self, is_project_ready, message):
        """
        Слот, который вызывается после завершения фоновой синхронизации папки.
        Версия 2.0: Использует новые, централизованные методы для фильтрации и обновления.
        """
        if hasattr(self, 'wait_dialog') and self.wait_dialog:
            self.wait_dialog.accept()

        if not is_project_ready:
            QMessageBox.warning(self, "Операция прервана", message)
            self.output_folder = None
            self.project_manager = None
            self.paths_widget.set_folder_path(None)
            self.check_ready()
            return
            
        # 1. Загружаем ассеты проекта (например, глоссарий).
        self._process_project_folder(self.output_folder)

        # 2. Вызываем "умный" диалог, который предложит отфильтровать список глав, если это необходимо.
        self._ask_and_filter_chapters()
        
        # 3. Вызываем единый "оркестратор" для обновления всего UI на основе
        #    (возможно, измененного) списка глав.
        self._on_project_data_changed()
    
    def _handle_backup_restore(self):
        """
        Обрабатывает нажатие на кнопку 'Очередь...'.
        Предлагает сохранить или загрузить состояние очереди.
        """
        if not self.output_folder or not self.selected_file:
            QtWidgets.QMessageBox.warning(self, "Проект не определен", "Для работы с бэкапом очереди необходимо выбрать файл и папку проекта.")
            return

        if not (self.engine and self.engine.task_manager):
            return

        snapshot_path = os.path.join(self.output_folder, "queue_snapshot.db")
        has_snapshot = os.path.exists(snapshot_path)

        msg_box = QtWidgets.QMessageBox(self)
        msg_box.setWindowTitle("Управление очередью задач")
        msg_box.setText("Вы можете сохранить текущее состояние очереди на диск или загрузить ранее сохраненное.")
        
        if has_snapshot:
            # Получаем время изменения файла для инфо
            import datetime
            mtime = os.path.getmtime(snapshot_path)
            dt = datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
            msg_box.setInformativeText(f"На диске найден бэкап от: {dt}")
        else:
            msg_box.setInformativeText("Сохраненных бэкапов не найдено.")

        btn_save = msg_box.addButton("💾 Сохранить текущую", QtWidgets.QMessageBox.ButtonRole.ActionRole)
        btn_load = msg_box.addButton("📂 Загрузить с диска", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        btn_cancel = msg_box.addButton("Отмена", QtWidgets.QMessageBox.ButtonRole.RejectRole)
        
        btn_load.setEnabled(has_snapshot)
        
        msg_box.exec()
        clicked = msg_box.clickedButton()

        if clicked == btn_save:
            # СОХРАНЕНИЕ
            if self.engine.task_manager.save_queue_snapshot(snapshot_path, self.selected_file):
                QtWidgets.QMessageBox.information(self, "Успех", "Очередь задач успешно сохранена в файл проекта.")
            else:
                QtWidgets.QMessageBox.critical(self, "Ошибка", "Не удалось сохранить очередь.")
                
        elif clicked == btn_load:
            # ЗАГРУЗКА
            try:
                # 1. Загружаем базу
                restored_chapters = self.engine.task_manager.load_queue_snapshot(snapshot_path, self.selected_file)
                
                if restored_chapters is not None:
                    # 2. Обновляем список глав в UI, чтобы он соответствовал загруженной очереди
                    self.html_files = restored_chapters
                    
                    # 3. Обновляем все виджеты и счетчики через главный оркестратор
                    self._on_project_data_changed()
                    
                    QtWidgets.QMessageBox.information(self, "Успех", f"Очередь восстановлена. Список глав обновлен ({len(self.html_files)} шт).")
                
            except Exception as e:
                QtWidgets.QMessageBox.critical(self, "Ошибка загрузки", f"Не удалось загрузить очередь:\n{e}")
                # Если загрузка провалилась (например, хеш не совпал), лучше очистить текущий UI от греха подальше,
                # или оставить как есть, если ошибка была перехвачена до деструктивных действий.
                # В load_queue_snapshot база восстанавливается атомарно, так что если исключение вылетело - 
                # скорее всего база в памяти осталась старой (если ошибка до backup) или пустой.
                # Обновим UI на всякий случай.
                self._on_project_data_changed()
                
    def _emit_task_manipulation_signal(self, action: str, task_ids: list):
        """
        Общий метод для ЗАПУСКА фоновых команд в TaskManager и обновления UI.
        Версия 2.0: Использует QThread для предотвращения зависания UI.
        """
        if not (self.engine and self.engine.task_manager):
            return

        target_method = None
        args = []

        if action in ['top', 'bottom', 'up', 'down']:
            target_method = self.engine.task_manager.reorder_tasks
            args = [action, task_ids]
        elif action == 'remove':
            target_method = self.engine.task_manager.remove_tasks
            args = [task_ids]
        elif action == 'duplicate':
            target_method = self.engine.task_manager.duplicate_tasks
            args = [task_ids]

        if not target_method:
            return

        # --- НОВАЯ ЛОГИКА С QTHREAD ---
        # 1. Блокируем UI, чтобы пользователь не нажал ничего лишнего
        self.task_management_widget.setEnabled(False)
        self.status_bar.set_permanent_message("Обновление списка задач...")

        # 2. Создаем и запускаем "грузчика"
        self.db_worker = TaskDBWorker(target_method, *args)
        
        # 3. После того как грузчик закончит, разблокируем UI
        self.db_worker.finished.connect(self._on_db_worker_finished)
        self.db_worker.start()

    def _on_db_worker_finished(self):
        """Слот, который вызывается по завершении фоновой DB-задачи."""
        self.status_bar.clear_message()
        self.task_management_widget.setEnabled(True)

    
    def _handle_task_reorder(self, action: str, task_ids: list):
        self._emit_task_manipulation_signal(action, task_ids)
    
    def _handle_task_duplication(self, task_ids: list):
        self._emit_task_manipulation_signal('duplicate', task_ids)
    
    def _handle_task_removal(self, task_ids: list):
        self._emit_task_manipulation_signal('remove', task_ids)

    def _filter_validated_chapters(self, silent=False):
        """
        Фильтрует self.html_files, оставляя только те главы, для которых НЕТ 'готовой' версии.
        """
        if not self.project_manager or not self.html_files:
            return

        chapters_to_keep = [ch for ch in self.html_files if '_validated.html' not in self.project_manager.get_versions_for_original(ch)]

        if len(chapters_to_keep) < len(self.html_files):
            self.html_files = chapters_to_keep
            if not silent:
                QMessageBox.information(self, "Главы отфильтрованы", f"Скрыты 'готовые' главы. Осталось для перевода: {len(self.html_files)}.")
                # Обновляем UI, так как это был прямой вызов от пользователя
                self._on_project_data_changed()
        elif not silent:
            QMessageBox.information(self, "Нет изменений", "В текущем списке нет глав, помеченных как 'готовые'.")

    def _filter_all_translated_tasks(self):
        """Фильтрует задачи, убирая все, у которых есть любая версия перевода."""
        all_possible_suffixes = api_config.all_translated_suffixes() + ['_validated.html']

        def filter_logic(chapters_to_filter):
            untracked = []
            chapters_to_keep = []
            for chapter_path in chapters_to_filter:
                base_name = os.path.splitext(os.path.basename(chapter_path))[0]
                internal_dir = os.path.dirname(chapter_path)
                
                is_translated = False
                for suffix in all_possible_suffixes:
                    full_disk_path = os.path.join(self.project_manager.project_folder, internal_dir, f"{base_name}{suffix}")
                    if os.path.exists(full_disk_path):
                        is_translated = True
                        # Проверяем, зарегистрирован ли файл, и добавляем в список, если нет
                        versions = self.project_manager.get_versions_for_original(chapter_path)
                        if suffix not in versions:
                            relative_path = os.path.relpath(full_disk_path, self.project_manager.project_folder)
                            untracked.append((chapter_path, suffix, relative_path))
                        break # Нашли перевод, дальше не ищем
                
                if not is_translated:
                    chapters_to_keep.append(chapter_path)
            
            return chapters_to_keep, untracked

        filtered_chapters, original_count = self._flatten_and_filter_tasks(filter_logic)
        
        if filtered_chapters is None: # Если была ошибка
            return

        if len(filtered_chapters) == original_count:
            QMessageBox.information(self, "Нет изменений", "Не найдено переведенных глав для скрытия.")
        else:
            QMessageBox.information(self, "Готово", "Список задач отфильтрован и пересобран.")
       
    def _flatten_and_filter_tasks(self, filter_function):
        """
        Универсальный оркестратор фильтрации.
        1. "Расплющивает" все задачи в упорядоченный список глав.
        2. Применяет переданную функцию-фильтр.
        3. Запускает полную пересборку задач на основе отфильтрованного списка.
        """
        if not (self.project_manager and self.engine and self.engine.task_manager):
            QMessageBox.information(self, "Нет данных", "Менеджер проекта или задач не инициализирован.")
            return None, 0 # Возвращаем None, чтобы показать, что операция не удалась

        tasks_to_check = self.engine.task_manager.get_all_tasks_for_rebuild()
        if not tasks_to_check:
            QMessageBox.information(self, "Нет данных", "Список задач для фильтрации пуст.")
            return None, 0

        # Шаг 1: "Расплющивание"
        ordered_unique_chapters = []
        seen_chapters = set()
        for task_id, task_payload in tasks_to_check:
            chapters_in_task = []
            task_type = task_payload[0]
            if task_type in ('epub', 'epub_chunk'):
                chapters_in_task.append(task_payload[2])
            elif task_type == 'epub_batch':
                chapters_in_task.extend(task_payload[2])
            
            for chapter in chapters_in_task:
                if chapter not in seen_chapters:
                    ordered_unique_chapters.append(chapter)
                    seen_chapters.add(chapter)
        
        original_chapter_count = len(ordered_unique_chapters)

        # Шаг 2: Фильтрация
        self.project_manager.reload_data_from_disk()
        
        # Функция filter_function вернет отфильтрованный список глав и список "беспризорников"
        filtered_chapters, untracked_files = filter_function(ordered_unique_chapters)
        if untracked_files:
            self.project_manager.register_multiple_translations(untracked_files)
            print(f"[INFO] Фильтр обнаружил и зарегистрировал {len(untracked_files)} ранее неучтенных файлов.")

        # Шаг 3: Пересборка
        # Обновляем self.html_files - это наш новый источник правды для UI
        self.html_files = filtered_chapters
        
        # Запускаем единый "оркестратор" для полного и консистентного
        # обновления всего UI на основе нового списка глав.
        self._on_project_data_changed()
        
        # Возвращаем результат для отображения сообщения пользователю.
        return filtered_chapters, original_chapter_count
       
    def _filter_validated_tasks(self):
        """Фильтрует задачи, убирая 'готовые'."""
        VALIDATED_SUFFIX = "_validated.html"

        def filter_logic(chapters_to_filter):
            # Мы можем просто переиспользовать существующий _is_chapter_validated!
            untracked = []
            chapters_to_keep = [
                ch for ch in chapters_to_filter 
                if not self._is_chapter_validated(ch, VALIDATED_SUFFIX, untracked)
            ]
            return chapters_to_keep, untracked

        filtered_chapters, original_count = self._flatten_and_filter_tasks(filter_logic)

        if filtered_chapters is None:
            return

        if len(filtered_chapters) == original_count:
            QMessageBox.information(self, "Нет изменений", "Не найдено 'готовых' глав для скрытия.")
        else:
            QMessageBox.information(self, "Готово", "Список задач отфильтрован и пересобран. 'Готовые' главы скрыты.")


    def _is_chapter_validated(self, chapter_path, validated_suffix, untracked_list):
        """
        Вспомогательный метод. Проверяет, существует ли для главы "готовый" файл.
        Если да, то также проверяет, зарегистрирован ли он, и при необходимости добавляет в список для тихого обновления.
        Возвращает True, если глава считается "готовой", иначе False.
        """
        base_name = os.path.splitext(os.path.basename(chapter_path))[0]
        internal_dir = os.path.dirname(chapter_path)
        validated_filename = f"{base_name}{validated_suffix}"
        full_disk_path = os.path.join(self.project_manager.project_folder, internal_dir, validated_filename)

        if os.path.exists(full_disk_path):
            # Файл существует. Проверяем, есть ли он в карте.
            versions = self.project_manager.get_versions_for_original(chapter_path)
            if validated_suffix not in versions:
                relative_path = os.path.relpath(full_disk_path, self.project_manager.project_folder)
                untracked_list.append((chapter_path, validated_suffix, relative_path))
            return True # Глава "готова"
            
        return False # Файл не найден, глава не "готова"

    def _ask_and_run_migration(self, migrator, file_count):
        """Показывает диалог с предложением о миграции и запускает ее."""
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Обнаружен старый проект")
        msg_box.setIcon(QMessageBox.Icon.Question)
        msg_box.setText(f"В выбранной папке найдено {file_count} файлов в старом 'плоском' формате.")
        
        msg_box.setInformativeText(
            "Программа может попытаться автоматически преобразовать этот проект в новую структурированную систему "
            "(с вложенными папками и файлом-картой 'translation_map.json').\n\n"
            "Это позволит использовать новые функции, такие как 'Обновление EPUB'.\n\n"
            "<b>Рекомендуется сделать резервную копию папки перед миграцией.</b>\n\n"
            "Выполнить миграцию?"
        )
        
        migrate_button = msg_box.addButton("Да, мигрировать", QMessageBox.ButtonRole.YesRole)
        cancel_button = msg_box.addButton("Нет, пропустить", QMessageBox.ButtonRole.NoRole)
        
        msg_box.exec()
        
        if msg_box.clickedButton() == migrate_button:
            moved, errors = migrator.run_migration()
            
            summary_message = f"Миграция завершена.\n\n- Успешно перемещено и зарегистрировано: {moved}\n- Ошибок (файлы оставлены на месте): {errors}"
            
            if errors > 0:
                QMessageBox.warning(self, "Миграция завершена с ошибками", summary_message)
            else:
                QMessageBox.information(self, "Миграция завершена успешно", summary_message)

   
    def _copy_original_chapters(self):
        """
        Копирует оригиналы выбранных глав, управляя пакетной обработкой
        для замены терминов по глоссарию и обновляя статус задач.
        """
        selected_rows = {item.row() for item in self.task_management_widget.chapter_list_widget.table.selectedItems()}
        if not selected_rows:
            self._show_custom_message("Нет выбора", "Пожалуйста, выберите задачи в списке.", QMessageBox.Icon.Information)
            return
    
        if not all([self.selected_file, self.output_folder, self.project_manager]):
            self._show_custom_message("Ошибка проекта", "Для операции нужен EPUB и папка проекта.", QMessageBox.Icon.Warning)
            return
    
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Способ копирования")
        msg_box.setIcon(QMessageBox.Icon.Question)
        msg_box.setText("Как скопировать оригиналы выбранных глав?")
        msg_box.setInformativeText(
            "<b>'Скопировать как есть'</b>: Создает точную копию исходного файла.\n\n"
            "<b>'Обработать по глоссарию'</b>: Находит в тексте термины из глоссария и заменяет их на переводы. Полезно для подготовки к ручному переводу."
        )
        
        btn_as_is = msg_box.addButton("Скопировать как есть", QMessageBox.ButtonRole.ActionRole)
        btn_process = msg_box.addButton("Обработать по глоссарию", QMessageBox.ButtonRole.AcceptRole)
        msg_box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        
        glossary_list = self.glossary_widget.get_glossary()
        if not glossary_list:
            btn_process.setEnabled(False)
            btn_process.setToolTip("Кнопка неактивна, так как глоссарий проекта пуст.")
    
        msg_box.exec()
        clicked_button = msg_box.clickedButton()
    
        if clicked_button == btn_as_is:
            process_with_glossary = False
            mode_text = "(копии оригиналов)"
        elif clicked_button == btn_process:
            process_with_glossary = True
            mode_text = "(обработано по глоссарию)"
        else:
            return
    
        provider_id = self.key_management_widget.get_selected_provider()
        provider_config = api_config.api_providers().get(provider_id, {})
        file_suffix = provider_config.get('file_suffix')
    
        if not file_suffix:
            self._show_custom_message("Ошибка конфигурации", f"Не удалось определить суффикс для провайдера '{provider_id}'.", QMessageBox.Icon.Critical)
            return
    
        selected_tasks = []
        chapters_to_process = set()
        for row in selected_rows:
            task_item = self.task_management_widget.chapter_list_widget.table.item(row, 0)
            if not task_item: continue
            
            task_tuple = task_item.data(QtCore.Qt.ItemDataRole.UserRole)
            selected_tasks.append(task_tuple)
            
            task_type = task_tuple[1][0]
            if task_type in ('epub', 'epub_chunk'):
                chapters_to_process.add(task_tuple[1][2])
            elif task_type == 'epub_batch':
                chapters_to_process.update(task_tuple[1][2])
    
        if not chapters_to_process:
            self._show_custom_message("Нечего обрабатывать", "Выбранные задачи не содержат глав.", QMessageBox.Icon.Warning)
            return
    
        replacer = None
        if process_with_glossary:
            full_glossary_data = {
                entry['original']: {
                    'rus': entry.get('rus') or entry.get('translation'),
                    'note': entry.get('note')
                }
                for entry in glossary_list
                if entry.get('original')
            }
            if full_glossary_data:
                replacer = GlossaryReplacer(full_glossary_data)
    
        copied_count, skipped_count, errors = 0, 0, []
        successfully_processed_chapters = set()
    
        try:
            if replacer:
                replacer.prepare()
    
            with zipfile.ZipFile(open(self.selected_file, 'rb'), 'r') as epub_zip:
                for chapter_path in chapters_to_process:
                    try:
                        base_name = os.path.splitext(os.path.basename(chapter_path))[0]
                        internal_dir = os.path.dirname(chapter_path)
                        
                        new_filename = f"{base_name}{file_suffix}"
                        destination_dir = os.path.join(self.output_folder, internal_dir)
                        os.makedirs(destination_dir, exist_ok=True)
                        full_dest_path = os.path.join(destination_dir, new_filename)
    
                        if os.path.exists(full_dest_path):
                            skipped_count += 1
                        else:
                            html_str = epub_zip.read(chapter_path).decode('utf-8', 'ignore')
                            content_to_write = (replacer.process_html(html_str).encode('utf-8') if replacer else html_str.encode('utf-8'))
                            
                            with open(full_dest_path, 'wb') as f:
                                f.write(content_to_write)
                            copied_count += 1
                        
                        relative_path = os.path.relpath(full_dest_path, self.output_folder)
                        self.project_manager.register_translation(chapter_path, file_suffix, relative_path)
                        
                        successfully_processed_chapters.add(chapter_path)
                    except Exception as e:
                        errors.append(f"Ошибка для главы '{chapter_path}': {e}")
        except Exception as e:
            self._show_custom_message("Критическая ошибка обработки", f"Произошла ошибка во время пакетной обработки: {e}", QMessageBox.Icon.Critical)
            return
        finally:
            if replacer:
                replacer.cleanup()
    
        for task_tuple in selected_tasks:
            task_type = task_tuple[1][0]
            chapters_in_task = []
            if task_type in ('epub', 'epub_chunk'):
                chapters_in_task.append(task_tuple[1][2])
            elif task_type == 'epub_batch':
                chapters_in_task.extend(task_tuple[1][2])
    
            if all(ch in successfully_processed_chapters for ch in chapters_in_task):
                self.task_manager.task_done("UI_ACTION", task_tuple)
    
        total_processed = copied_count + skipped_count
        summary_text = f"Успешно обработано {total_processed} глав {mode_text}:"
        informative_text = f"- Скопировано новых: {copied_count}\n- Пропущено (уже существуют): {skipped_count}"
        
        if errors:
            informative_text += f"\n\nПроизошли ошибки ({len(errors)}):\n" + "\n".join(errors[:3])
            self._show_custom_message("Завершено с ошибками", summary_text, QMessageBox.Icon.Warning, informative_text, button_text="Принял")
        else:
            self._show_custom_message("Готово", summary_text, QMessageBox.Icon.Information, informative_text, button_text="Отлично")
    
    def _get_full_ui_settings(self):
        """Собирает полный 'слепок' настроек из всех релевантных виджетов (БЕЗ глоссария)."""
        settings = self.get_settings()
        
        settings.update(self.translation_options_widget.get_settings())
        
        # Удаляем данные, которые не должны сохраняться как "настройки"
        settings.pop('selected_chapters', None)
        settings.pop('file_path', None)
        settings.pop('output_folder', None)
        settings.pop('full_glossary_data', None)
        settings.pop('project_manager', None)
        
        return settings
        
        
    def _apply_full_ui_settings(self, settings: dict):
        """
        Применяет полный 'слепок' настроек ко всем виджетам (БЕЗ глоссария),
        блокируя сигналы, чтобы избежать ложного 'загрязнения' состояния.
        """
        if not settings:
            print("[INFO] Нет сохраненных настроек сессии для применения.")
            return

        # --- Блокируем сигналы, чтобы избежать ложного срабатывания is_settings_dirty ---
        self.model_settings_widget.blockSignals(True)
        self.translation_options_widget.blockSignals(True)
        self.preset_widget.blockSignals(True)

        try:
            self.model_settings_widget.set_settings(settings)
            self.translation_options_widget.set_settings(settings)
            
            if 'custom_prompt' in settings:
                self.preset_widget.set_prompt(settings['custom_prompt'])
        finally:
            # --- Обязательно разблокируем сигналы в блоке finally ---
            self.model_settings_widget.blockSignals(False)
            self.translation_options_widget.blockSignals(False)
            self.preset_widget.blockSignals(False)

        print("[INFO] Настройки сессии успешно применены к UI.")


    def _save_project_settings_only(self):
        """Сохраняет только настройки UI в файл проекта."""
        if not self.output_folder: return
        
        project_settings_path = os.path.join(self.output_folder, "project_settings.json")
        manager_to_save = SettingsManager(config_file=project_settings_path)
        manager_to_save.save_full_session_settings(self._get_full_ui_settings())
        
        self.is_settings_dirty = False
        self.setWindowTitle(self.windowTitle().replace("*", ""))
        print("[SETTINGS] Настройки проекта сохранены.")

    
    
    
    
    def _save_project_glossary_only(self):
        """Сохраняет только глоссарий в файл проекта и обновляет 'чистое' состояние."""
        if not self.output_folder: return

        project_glossary_path = os.path.join(self.output_folder, "project_glossary.json")
        current_glossary = self.glossary_widget.get_glossary()
        try:
            with open(project_glossary_path, 'w', encoding='utf-8') as f:
                json.dump(current_glossary, f, ensure_ascii=False, indent=2, sort_keys=True)
            
            # --- ИСПРАВЛЕНИЕ: Создаем независимую копию списка для фиксации состояния ---
            self.initial_glossary_state = [item.copy() for item in current_glossary]
            
            print("[SETTINGS] Глоссарий проекта сохранен.")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось сохранить глоссарий проекта: {e}")

    def _save_project_data(self):
        """
        Сохраняет ВСЕ данные проекта: и настройки UI, и глоссарий.
        """
        if not self.output_folder:
            return
        self._save_project_settings_only()
        self._save_project_glossary_only()
        
    def check_ready(self):
        """
        Проверяет, все ли условия выполнены для запуска, и обновляет
        состояние и СТИЛЬ всех кнопок управления.
        Версия 2.6: Раздельная логика для перевода и генерации глоссария.
        """
        
        # --- НОВАЯ ЗАЩИТА: Синхронизация с реальностью ---
        if self._check_and_sync_active_session():
            # Если мы обнаружили активную сессию, UI уже заблокирован внутри метода синхронизации.
            # Нам не нужно проверять валидность полей для старта. Выходим.
            return
            
        if self.is_session_active:
            return

        # --- Условие для основного перевода (требует ключи) ---
        num_active_keys = len(self.key_management_widget.get_active_keys())
        can_start_translation = all([
            self.selected_file, 
            self.output_folder, 
            self.html_files, 
            num_active_keys > 0
        ])
        
        self.start_btn.setEnabled(can_start_translation)
        if can_start_translation:
            self.start_btn.setStyleSheet("background-color: #2ECC71; color: #ffffff;")
        else:
            self.start_btn.setStyleSheet("")

        # --- Условие для генерации глоссария (НЕ требует ключи здесь) ---
        can_generate_glossary = bool(self.selected_file and self.output_folder and self.html_files)
        self.glossary_widget.set_generation_enabled(can_generate_glossary)

        # --- Остальные проверки ---
        can_dry_run = bool(self.selected_file and self.html_files)
        self.dry_run_btn.setEnabled(can_dry_run)
        
        can_validate_or_build = bool(self.selected_file and self.output_folder)
        self.task_management_widget.set_validation_enabled(can_validate_or_build)
        self.project_actions_widget.set_build_epub_enabled(can_validate_or_build)
        self.project_actions_widget.set_sync_enabled(can_validate_or_build)
        
        if hasattr(self, 'instances_spin'):
            self._update_distribution_info_from_widget()
    
    def _run_project_sync(self):
        """Запускает синхронизацию проекта в фоновом потоке."""
        if not self.project_manager: return

        from ...utils.project_migrator import ProjectMigrator, SyncThread

        self.wait_dialog = QMessageBox(self)
        self.wait_dialog.setWindowTitle("Синхронизация")
        self.wait_dialog.setText("Идет анализ проекта…")
        self.wait_dialog.setStandardButtons(QMessageBox.StandardButton.NoButton)
        self.wait_dialog.setModal(True)
        
        migrator = ProjectMigrator(self.output_folder, self.selected_file, self.project_manager)
        
        self.sync_thread = SyncThread(migrator, parent_widget=self)
        self.sync_thread.finished_sync.connect(self._on_sync_finished)
        
        self.sync_thread.start()
        self.wait_dialog.show()

    def _on_sync_finished(self, is_project_ready, message):
        """Обрабатывает результат фоновой синхронизации."""
        if hasattr(self, 'wait_dialog') and self.wait_dialog:
            self.wait_dialog.accept()
    
        if not is_project_ready:
            QMessageBox.warning(self, "Операция прервана", message)
            return
            
        QMessageBox.information(self, "Синхронизация", message)

        # --- ИСПРАВЛЕНИЕ ЗДЕСЬ ---
        # Вместо полной перезагрузки проекта, мы вызываем наш "оркестратор",
        # который обновит список задач и UI на основе свежих данных,
        # не заставляя пользователя заново выбирать главы.
        self._on_project_data_changed()
    
    def _update_instances_spinbox_limit(self):
        """
        Этот слот вызывается ТОЛЬКО при изменении списка активных ключей в UI.
        Он корректно обновляет максимум для spinbox'а, защищая значение пользователя.
        """
        if self.is_session_active:
            return # Не трогаем spinbox во время активной сессии!
        
        num_active_keys = len(self.key_management_widget.get_active_keys())
        
        # Устанавливаем новый максимум. QSpinBox АВТОМАТИЧЕСКИ уменьшит текущее значение, 
        # если оно больше максимума. Нам не нужно делать это вручную через setValue, 
        # так как это может сбить "память" виджета при кратковременных просадках максимума.
        self.instances_spin.setMaximum(num_active_keys if num_active_keys > 0 else 1)
        
        # Обновляем текстовую метку с распределением, так как она тоже зависит от этого.
        self._update_distribution_info_from_widget()
    
    def _filter_all_translated_chapters(self, silent=False):
        """
        Фильтрует self.html_files, оставляя только те главы, для которых НЕТ 
        ни одной версии перевода в карте проекта.
        """
        if not self.project_manager or not self.html_files:
            return

        chapters_to_keep = [ch for ch in self.html_files if not self.project_manager.get_versions_for_original(ch)]

        # Если список не изменился, ничего не делаем
        if len(chapters_to_keep) == len(self.html_files):
            if not silent: QMessageBox.information(self, "Нет изменений", "В текущем списке нет переведенных глав.")
            return

        # Если после фильтрации ничего не осталось
        if not chapters_to_keep and not silent:
            QMessageBox.information(self, "Все переведено", "Все выбранные главы уже имеют хотя бы одну версию перевода. Список будет очищен.")
        
        # Обновляем основной список глав
        self.html_files = chapters_to_keep
        
        # Показываем сообщение, только если мы не в "тихом" режиме
        if not silent:
            if chapters_to_keep:
                QMessageBox.information(self, "Готово", f"Список отфильтрован. Скрыты все переведенные главы. Осталось: {len(self.html_files)}.")
            # Обновляем UI, так как это был прямой вызов от пользователя
            self._on_project_data_changed()


    def _start_translation(self):
        """
        Собирает настройки и отправляет команду на запуск сессии.
        """
        
        if self._check_and_sync_active_session():
            # Если метод вернул True, значит сессия УЖЕ шла.
            # Мы только что обновили UI (включили Стоп, выключили Старт).
            # Просто выходим, не отправляя команду повторно.
            print("[INFO] Нажатие 'Старт' проигнорировано: сессия уже активна (интерфейс обновлен).")
            return
        
        # 1. Проверяем, существуют ли задачи, заглядывая напрямую в TaskManager
        tasks_exist = self.engine and self.engine.task_manager and self.engine.task_manager.has_pending_tasks()

        # 2. Проверяем все условия для старта
        if not all([self.selected_file, tasks_exist, self.output_folder, self.key_management_widget.get_active_keys()]):
            QMessageBox.warning(self, "Ошибка", "Необходимо выбрать файл, задачи, папку и активные ключи.")
            return
        
        if self.engine and self.engine.task_manager:
            self.engine.task_manager.release_held_tasks()
        
        # 3. Получаем настройки. В них больше нет 'selected_chapters'.
        settings = self.get_settings()
        
        # 4. Проверяем существование файла (эта проверка остается важной)
        original_epub_path = settings.get('file_path')
        if not original_epub_path or not os.path.exists(original_epub_path):
            QMessageBox.critical(self, "Критическая ошибка: Файл не найден", f"Не удалось найти исходный EPUB файл: {original_epub_path}")
            self.selected_file = None
            self.html_files = []
            self.paths_widget.set_file_path(None)
            self.check_ready()
            return

        # 5. Сохраняем все релевантные настройки перед запуском
        self.log_widget.clear()
        self.settings_manager.add_to_project_history(self.selected_file, self.output_folder)
        self.settings_manager.save_custom_prompt(self.preset_widget.get_prompt())
        self.settings_manager.save_last_prompt_preset_name(self.preset_widget.get_current_preset_name())
        if self.local_set:
            self._save_project_settings_only()
        if self.glossary_widget.get_glossary() != self.initial_glossary_state:
            self._save_project_glossary_only()
        
        # 6. Отправляем событие на запуск сессии
        self.this_dialog_started_the_session = True
        self._post_event(name='start_session_requested', data={'settings': settings})
        
        # 7. Обновляем UI
        self.start_btn.setEnabled(False)
        self._post_event('log_message', {'message': "[SYSTEM] Команда на запуск сессии отправлена…"})


    def _stop_translation(self):
        """
        Отправляет команду на остановку сессии через шину событий.
        """
        if self.engine and self.engine.session_id:
            self._post_event('log_message', {'message': "[SYSTEM] Отправка запроса на остановку сессии…"})
            # --- Отправляем событие вместо прямого вызова ---
            self._post_event('manual_stop_requested')

        elif self._check_and_sync_active_session():
            self._post_event('log_message', {'message': "[SYSTEM] Отправка запроса на остановку сессии…"})
            # --- Отправляем событие вместо прямого вызова ---
            self._post_event('manual_stop_requested')
    
    @pyqtSlot()
    def _on_session_finished(self):
        """
        Финальная процедура очистки UI. "Размораживает" задачи после dry_run.
        """
        # --- ИСПРАВЛЕНИЕ: Вместо восстановления, просто "размораживаем" ---
        if self.engine and self.engine.task_manager:
            self.engine.task_manager.release_held_tasks()
            
        # --- Кнопка dry_run теперь сбрасывается всегда ---
        self.dry_run_btn.setText("🧪 Пробный запуск")
        
        self._post_event('log_message', {'message': "[SYSTEM] Получен сигнал завершения. Очистка интерфейса…"})
        if self.project_manager:
            self.project_manager.reload_data_from_disk()
            print("[INFO] Карта проекта обновлена после завершения сессии.")
        
        if self.output_folder:
            self.project_manager = TranslationProjectManager(self.output_folder)
        
        self.key_management_widget._load_and_refresh_keys()
        self.task_management_widget.check_and_update_retry_button_visibility()
        self.status_bar.stop_session()
        self._set_controls_enabled(True)
        
        # --- НАЧАЛО БЛОКА СИНХРОНИЗАЦИИ ---
        # После завершения сессии мы должны обновить стили ключей
        # в соответствии с моделью, которая сейчас выбрана в UI.
        try:
            current_ui_model_name = self.model_settings_widget.model_combo.currentText()
            model_config = api_config.all_models().get(current_ui_model_name, {})
            model_id_to_sync = model_config.get('id')
            if model_id_to_sync:
                self.key_management_widget.set_current_model(model_id_to_sync)
                print(f"[INFO] Синхронизация статусов ключей для модели: {current_ui_model_name} ({model_id_to_sync})")
        except Exception as e:
            print(f"[ERROR] Не удалось синхронизировать виджет ключей после сессии: {e}")
        # --- КОНЕЦ НОВОГО БЛОКА ---
        
        QtCore.QMetaObject.invokeMethod(
            self, "_finalize_session_state", 
            QtCore.Qt.ConnectionType.QueuedConnection
        )
    
    @pyqtSlot()
    def _finalize_session_state(self):
        """Этот слот вызывается асинхронно для безопасного сброса флага сессии."""
        self.is_session_active = False
        self._post_event('log_message', {'message': "[SYSTEM] Интерфейс полностью разблокирован."})
        self.check_ready() # Теперь вызываем проверку, когда флаг точно сброшен
        
        
        
        # Проверяем, был ли это последний воркер и была ли сессия остановлена принудительно
        if hasattr(self, '_shutdown_reason') and hasattr(self, '_log_session_id'):
            session_id_log = self._log_session_id
            reason = self._shutdown_reason
            QtCore.QTimer.singleShot(100, lambda: self.summary_sep_session(session_id_log=session_id_log, reason=reason))
        
        del self._shutdown_reason
        del self._log_session_id
    
    
    def summary_sep_session(self, session_id_log, reason):
        final_message_data = {
            'message': f"■■■ СЕССИЯ {session_id_log[:8]} ОСТАНОВЛЕНА. {reason} ■■■",
            'priority': 'final' # <-- Наш новый флаг!
        }
        self._post_event('log_message', {'message': "---SEPARATOR---"})
        self._post_event('log_message', final_message_data)
    
    def _open_filter_packaging_dialog(self):
        """
        Открывает диалог для умной пакетной подготовки отфильтрованных глав.
        Версия 2.1: Исправлен поиск задач (теперь ищет 'error' + 'CONTENT_FILTER').
        """
        if not (self.engine and self.engine.task_manager):
            QMessageBox.information(self, "Нет данных", "Менеджер задач не инициализирован.")
            return

        # 1. Получаем ПОЛНЫЙ список состояния задач
        all_tasks_state = self.engine.task_manager.get_ui_state_list()

        filtered_chapters = set()
        successful_chapters = set()
        
        successful_map = {}
        if self.project_manager:
            for original, versions in self.project_manager.get_full_map().items():
                for suffix, rel_path in versions.items():
                    if suffix != 'filtered':
                        full_path = os.path.join(self.project_manager.project_folder, rel_path)
                        if os.path.exists(full_path):
                            successful_map[original] = full_path
                            break 

        # 2. Итерируемся по актуальному состоянию
        # ВАЖНО: распаковываем details (третий элемент), чтобы проверить ошибки
        for task_info, status, details in all_tasks_state:
            payload = task_info[1]
            chapters_in_task = []
            if payload[0] in ('epub', 'epub_chunk'):
                chapters_in_task.append(payload[2])
            elif payload[0] == 'epub_batch':
                chapters_in_task.extend(payload[2])

            # Проверяем наличие ошибки CONTENT_FILTER в деталях задачи
            is_filtered = (status == 'error' and 'CONTENT_FILTER' in details.get('errors', {}))

            for chapter in chapters_in_task:
                if is_filtered:
                    filtered_chapters.add(chapter)
                elif status == 'success' and chapter in successful_map:
                    successful_chapters.add(chapter)

        if not filtered_chapters:
            QMessageBox.information(self, "Нет данных", "Не найдено задач, остановленных фильтром контента.")
            return

        # 3. Получаем рекомендуемый размер из виджета опций
        recommended_size = self.translation_options_widget.task_size_spin.value()

        real_chapter_sizes = {
            path: composition.get('total_size', 0)
            for path, composition in self.translation_options_widget.chapter_compositions.items()
        }
        
        if not real_chapter_sizes:
             QMessageBox.warning(self, "Ошибка", "Не удалось получить данные о размерах глав. Попробуйте перезагрузить проект.")
             return
            
        # 4. Создаем и запускаем диалог
        dialog = FilterPackagingDialog(
            filtered_chapters=list(filtered_chapters),
            successful_chapters=list(successful_chapters),
            recommended_size=recommended_size,
            epub_path=self.selected_file,
            real_chapter_sizes=real_chapter_sizes, 
            parent=self
        )

        if dialog.exec():
            result = dialog.get_result()
            if result:
                self._process_filter_dialog_result(result)

    def _process_filter_dialog_result(self, result: dict):
        """
        Обрабатывает результат из FilterPackagingDialog.
        Версия 2.1: Добавляет искусственную историю ошибок (2x CONTENT_FILTER)
        для новых пакетов, чтобы форсировать атомарный режим генерации.
        """
        result_type = result.get('type')
        data = result.get('data')

        if not data:
            data = []

        plain_payloads = []
        
        # Создаем "прививку" от фильтров: 2 ошибки CONTENT_FILTER
        # Это сигнал для воркера использовать безопасный (атомарный) режим.
        artificial_history = {'errors': {'CONTENT_FILTER': 2}}

        if result_type == 'chapters':
            # Тип 1: Список глав. Отправляем в TaskPreparer через штатный метод.
            # В этом случае мы не можем легко внедрить историю, так как TaskPreparer внутри.
            # Но обычно диалог фильтрации возвращает payloads (Тип 2).
            self.html_files = data
            self._prepare_and_display_tasks(clean_rebuild=True) 

        elif result_type == 'payloads':
            # Тип 2: Готовые пейлоады. 
            plain_payloads = data
            
            # Обновляем UI счетчик глав
            all_chapters_in_payloads = set()
            for payload in plain_payloads:
                if payload[0] == 'epub_batch':
                    all_chapters_in_payloads.update(payload[2])
            
            self.html_files = sorted(list(all_chapters_in_payloads), key=extract_number_from_path)
            self.paths_widget.update_chapters_info(len(self.html_files))
            
            # Напрямую перезаписываем очередь в TaskManager с ВАКЦИНАЦИЕЙ
            self.task_manager.set_pending_tasks(plain_payloads, initial_history=artificial_history)
            self.translation_options_widget._update_info_text()
        
        # Общие действия после обработки
        self._post_event('log_message', {'message': f"[INFO] Сформированы задачи для обхода фильтров. Активирован безопасный режим (Content Filter x2)."})
        self.task_management_widget.set_retry_filtered_button_visible(False)


    def _set_controls_enabled(self, enabled):
        """
        Централизованно включает/выключает все элементы управления на время перевода.
        """
        is_session_active = not enabled
        
        # Кнопки Старт/Стоп
        self.start_btn.setEnabled(not is_session_active)
        self.stop_btn.setEnabled(is_session_active)
        
        # Эти виджеты блокируются полностью
        widgets_to_toggle = [
            self.paths_widget,
            self.key_management_widget,
            self.glossary_widget,
            self.preset_widget,
            self.translation_options_widget,
            self.model_settings_widget,
            self.project_actions_widget,
            self.dry_run_btn,
        ]
        for widget in widgets_to_toggle:
            widget.setEnabled(not is_session_active)
            
        # А этот виджет переводится в специальный режим
        self.task_management_widget.set_session_mode(is_session_active)
        
        if not enabled:
            # Сессия НАЧАЛАСЬ
            self.stop_btn.setStyleSheet("background-color: #C0392B; color: #ffffff;")
            self.start_btn.setStyleSheet("")
        else:
            # Сессия ЗАВЕРШИЛАСЬ
            self.stop_btn.setStyleSheet("")
            # --- НАША НОВАЯ СТРОКА ---
            self.dry_run_btn.setText("🧪 Пробный запуск") # Возвращаем кнопке исходный текст


    @pyqtSlot(str, object, bool, str, str, str)
    def _on_chapter_status_update(self, session_id, task_info_result, success, err_type, msg, final_status):
        """Обновляет статус задачи в UI."""
        task_info, _ = (task_info_result, None)
        if isinstance(task_info_result, tuple) and len(task_info_result) == 2:
            task_info, _ = task_info_result
        
        self.task_management_widget.update_task_status(task_info, final_status)

        # Обновляем счетчики
        self.status_bar.increment_status(final_status)


    # --- НОВЫЙ МЕТОД ДЛЯ ПРИЕМА ДАННЫХ ИЗ ВАЛИДАТОРА ---
    def add_files_for_retry(self, epub_path, chapter_paths):
        """
        Принимает список глав из Валидатора, полностью заменяет
        текущий список задач и обновляет весь UI.
        """
        if self.selected_file != epub_path:
            QMessageBox.warning(self, "Конфликт проектов", 
                                "Главы для повтора относятся к другому EPUB файлу. "
                                "Пожалуйста, сначала загрузите соответствующий проект.")
            return

        # 1. Заменяем текущий список выбранных глав на новый
        self.html_files = chapter_paths
        
        # 2. Логируем действие
        self._post_event('log_message', {'message': f"[INFO] Загружено {len(chapter_paths)} глав для повторного перевода из Валидатора."})

        # 3. Полностью обновляем UI на основе нового списка глав
        self._on_project_data_changed()
        
        # Перепроверяем готовность к запуску
        self.check_ready()


    def _open_project_history(self):
        """Открывает диалог с историей проектов."""
        history = self.settings_manager.load_project_history()
        if not history:
            QMessageBox.information(self, "История пуста", "Вы еще не запускали ни одного перевода.")
            return
    
        # Передаем settings_manager в диалог
        dialog = ProjectHistoryDialog(history, self.settings_manager, self)
        
        if dialog.exec():
            # Эта часть кода сработает, только если пользователь выбрал проект
            # и нажал "Загрузить". Удаление уже было сохранено внутри диалога.
            selected_project = dialog.get_selected_project()
            if selected_project:
                self._load_project(selected_project)

    def _load_project(self, project_data):
        """
        Загружает проект из истории. Устанавливает пути, загружает глоссарий
        и запускает процесс выбора глав.
        Версия 2.0: Добавлена логика сброса состояния при смене проекта.
        """
        epub_path = project_data.get("epub_path")
        output_folder = project_data.get("output_folder")

        if not os.path.exists(epub_path) or not os.path.isdir(output_folder):
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Пути не найдены")
            msg_box.setIcon(QMessageBox.Icon.Warning)
            msg_box.setText(f"Не удалось найти файл или папку для проекта '{project_data.get('name')}'.")
            msg_box.setInformativeText(f"Файл: {epub_path}\nПапка: {output_folder}\n\nУдалить эту некорректную запись из истории?")
            yes_button = msg_box.addButton("Да, удалить", QMessageBox.ButtonRole.YesRole)
            no_button = msg_box.addButton("Нет", QMessageBox.ButtonRole.NoRole)
            msg_box.exec()
            if msg_box.clickedButton() == yes_button:
                history = self.settings_manager.load_project_history()
                history = [p for p in history if p != project_data]
                self.settings_manager.save_project_history(history)
            return
        
        print("[INFO] Загрузка проекта из истории…")

        # --- НАЧАЛО НОВОЙ КЛЮЧЕВОЙ ЛОГИКИ ---
        # Проверяем, отличается ли загружаемый проект от текущего
        if self.selected_file != epub_path or self.output_folder != output_folder:
            print("[INFO] Обнаружена смена проекта. Полный сброс состояния...")
            self.html_files = []
            self.paths_widget.update_chapters_info(0)
            if self.task_manager:
                self.task_manager.clear_all_queues()
        # --- КОНЕЦ НОВОЙ КЛЮЧЕВОЙ ЛОГИКИ ---

        self.selected_file = epub_path
        self.output_folder = output_folder
        self.paths_widget.set_file_path(epub_path)
        self.paths_widget.set_folder_path(output_folder)
        self.project_manager = TranslationProjectManager(self.output_folder)
        
        # Запускаем процесс выбора глав. Дальнейшее обновление UI произойдет в колбэках.
        # Теперь это безопасно, так как self.html_files гарантированно либо пуст, либо актуален.
        self._process_selected_file()



    def _calibrate_cpu(self, no_log=False):
        """
        Выполняет эталонный тест ВСЕГО конвейера фильтрации глоссария,
        учитывая текущие настройки пользователя (порог Fuzzy, Jieba).
        """
        if not no_log:
            print("[INFO] Запуск ручной калибровки CPU на реальных данных проекта…")
        
        current_glossary_list = self.glossary_widget.get_glossary()
        if not current_glossary_list or not self.html_files:
            QMessageBox.warning(self, "Недостаточно данных", "Для калибровки необходимо выбрать EPUB с главами и загрузить глоссарий.")
            return

        glossary_sample_list = current_glossary_list[:BENCHMARK_GLOSSARY_SIZE]
        # Для теста нам нужен полный формат словаря
        glossary_sample_dict = {
            entry.get('original', ''): {'rus': entry.get('rus', ''), 'note': entry.get('note', '')}
            for entry in glossary_sample_list if entry.get('original')
        }
        
        text_sample = ""
        if self.html_files and self.selected_file:
            try:
                with zipfile.ZipFile(open(self.selected_file, 'rb'), 'r') as zf:
                    first_chapter_content = zf.read(self.html_files[0]).decode('utf-8', 'ignore')
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(first_chapter_content, 'html.parser')
                    full_text = soup.get_text()
                    start_index = max(0, (len(full_text) - BENCHMARK_TEXT_SIZE) // 2)
                    text_sample = full_text[start_index : start_index + BENCHMARK_TEXT_SIZE]
            except Exception:
                text_sample = "placeholder " * (BENCHMARK_TEXT_SIZE // 12)
        else:
            text_sample = "placeholder " * (BENCHMARK_TEXT_SIZE // 12)

        filter_instance = SmartGlossaryFilter()
        
        # 1. Получаем ВСЕ актуальные настройки фильтрации из UI.
        current_threshold = self.model_settings_widget.fuzzy_threshold_spin.value()
        use_jieba_for_test = self.model_settings_widget.use_jieba_glossary_checkbox.isChecked()

        start_time = time.perf_counter()
        
        # 2. Вызываем главный метод-оркестратор, а не его внутреннюю часть.
        #    Это гарантирует, что мы тестируем всю цепочку оптимизаций.
        settings = self.get_settings()
        self.context_manager.update_settings(settings)
        sim_map = self.context_manager.similarity_map
        
        filter_instance.filter_glossary_for_text(
            full_glossary=glossary_sample_dict, 
            text=text_sample, 
            fuzzy_threshold=current_threshold,
            use_jieba_for_glossary_search=use_jieba_for_test,
            similarity_map=sim_map
        )
        
        end_time = time.perf_counter()
        
        time_taken = end_time - start_time
        if time_taken < 0.001: time_taken = 0.001

        num_operations = len(glossary_sample_dict) * len(text_sample)
        self.cpu_performance_index = num_operations / time_taken
        
        # 3. Добавляем в лог все использованные параметры для полной прозрачности.
        fuzzy_mode_info = f"Fuzzy порог {current_threshold}%" if current_threshold < 100 else "Fuzzy выключен"
        if not no_log:
            print(f"[INFO] Калибровка ({fuzzy_mode_info}, Jieba: {'Вкл' if use_jieba_for_test else 'Выкл'}) завершена за {time_taken:.4f} сек. "
              f"Индекс: {self.cpu_performance_index:,.0f} (термин*сим)/сек.")

        self._update_fuzzy_status_display()
        if no_log == True:
            QtCore.QTimer.singleShot(600, lambda: self._calibrate_cpu(no_log=False))
    
    @QtCore.pyqtSlot()
    def _update_fuzzy_status_display(self):
        """
        ТОЛЬКО обновляет UI-лейбл на основе текущих настроек и последней калибровки.
        Версия 2.0: Корректно учитывает количество клиентов (параллельных окон).
        """
        if self.cpu_performance_index is None or self.cpu_performance_index == 0:
            self.model_settings_widget.fuzzy_status_label.setText("Fuzzy-поиск: (требуется калибровка 🔄)")
            self.model_settings_widget.fuzzy_status_label.setStyleSheet("color: #aaa;")
            return

        # --- Получаем все необходимые данные ---
        glossary_size = len(self.glossary_widget.get_glossary())
        rpm = self.model_settings_widget.rpm_spin.value()
        
        # --- НАЧАЛО КЛЮЧЕВОГО ИСПРАВЛЕНИЯ ---
        # 1. Получаем количество параллельных клиентов из spinbox'а.
        num_clients = self.instances_spin.value()
        # --- КОНЕЦ КЛЮЧЕВОГО ИСПРАВЛЕНИЯ ---

        use_batching = self.translation_options_widget.batch_checkbox.isChecked()
        use_chunking = self.translation_options_widget.chunking_checkbox.isChecked()
        avg_task_size = 0
        if use_batching or use_chunking:
            avg_task_size = self.translation_options_widget.task_size_spin.value()
        elif self.html_files:
            total_size = sum(self.translation_options_widget.chapter_compositions.get(f, {}).get('total_size', 0) for f in self.html_files)
            avg_task_size = total_size / len(self.html_files) if self.html_files else 0
        
        # --- Проверки и расчеты ---
        if glossary_size == 0 or rpm == 0 or avg_task_size == 0 or num_clients == 0:
            return

        num_operations = glossary_size * avg_task_size
        estimated_time = num_operations / self.cpu_performance_index
        
        # --- НАЧАЛО КЛЮЧЕВОГО ИСПРАВЛЕНИЯ ---
        # 2. Рассчитываем ОБЩУЮ пропускную способность и РЕАЛЬНЫЙ интервал между запросами.
        total_application_rpm = rpm * num_clients
        interval = 60 / total_application_rpm
        # --- КОНЕЦ КЛЮЧЕВОГО ИСПРАВЛЕНИЯ ---
        
        # --- Управление UI (теперь с корректными данными) ---
        label = self.model_settings_widget.fuzzy_status_label
        
        if estimated_time > interval:
            label.setText(f"Fuzzy-поиск: ~{estimated_time:.2f} сек. (Дольше, чем {interval:.2f}с/запрос. 🔴)")
            # Добавляем более детальную подсказку
            label.setToolTip(f"При {num_clients} клиентах общая частота запросов составляет ~{int(total_application_rpm)} RPM.\n"
                             f"Интервал между запросами от приложения: ~{interval:.2f} сек.\n"
                             f"Время поиска в глоссарии (~{estimated_time:.2f} сек.) превышает этот интервал, что грозит тотальным зависанием.")
            label.setStyleSheet("color: red; font-size: 10px; font-weight: bold;")
        else:
            label.setText(f"Fuzzy-поиск: ~{estimated_time:.2f} сек. (OK)")
            label.setToolTip(f"Время поиска в глоссарии (~{estimated_time:.2f} сек.) меньше интервала\n"
                             f"между запросами (~{interval:.2f} сек.), поэтому он не будет 'тормозить' перевод.")
            label.setStyleSheet("color: green; font-size: 10px; font-weight: bold;")

    def _process_project_folder(self, folder):
        """
        Центральный, но теперь УПРОЩЕННЫЙ метод для обработки папки проекта.
        Синхронизация и миграция теперь делегированы EpubHtmlSelectorDialog.
        """
        # Просто загружаем глоссарий проекта, если он есть.
        self._load_project_glossary(folder)
    
    
    def _open_epub_builder_standalone(self):
        """
        Открывает сборщик EPUB, используя уже выбранные файл и папку.
        """
        folder = self.output_folder

        map_file = os.path.join(folder, 'translation_map.json')
        if not os.path.exists(map_file):
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Проект не найден")
            msg_box.setIcon(QMessageBox.Icon.Warning)
            msg_box.setText("В выбранной папке отсутствует файл 'translation_map.json'.")
            msg_box.setInformativeText("Сборщик может работать некорректно. Продолжить?")
            yes_button = msg_box.addButton("Да, продолжить", QMessageBox.ButtonRole.YesRole)
            no_button = msg_box.addButton("Нет", QMessageBox.ButtonRole.NoRole)
            msg_box.setDefaultButton(no_button)
            msg_box.exec()
            if msg_box.clickedButton() == no_button:
                return

        try:
            # --- КЛЮЧЕВОЕ ИЗМЕНЕНИЕ: Передаем project_manager ---
            dialog = TranslatedChaptersManagerDialog(
                folder, 
                self, 
                original_epub_path=self.selected_file,
                project_manager=self.project_manager # <--- ВОТ ОНО
            )
            dialog.exec()
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось открыть менеджер EPUB: {e}")

    def _validate_translation_map(self, project_manager):
        """
        Проверяет карту, спрашивает пользователя и, если нужно, СИНХРОННО выполняет очистку.
        Возвращает True, если очистка была выполнена.
        """
        dead_entries = project_manager.validate_map_with_filesystem()
        if not dead_entries:
            return False

        num_dead = len(dead_entries)
        # --- ИСПРАВЛЕНИЕ: Создаем QMessageBox с родителем (self) для правильного стиля ---
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Синхронизация проекта")
        msg_box.setIcon(QMessageBox.Icon.Warning)
        msg_box.setText(f"Обнаружено {num_dead} записей о переводах, файлы которых отсутствуют.")
        
        details = "\n".join([f"- {rel_path}" for _, _, rel_path in dead_entries[:5]])
        if num_dead > 5:
            details += f"\n… и еще {num_dead - 5}."
        
        msg_box.setInformativeText(f"Рекомендуется очистить эти 'мертвые' записи из карты проекта.\n\nПримеры:\n{details}")
        
        cleanup_button = msg_box.addButton("Очистить записи", QMessageBox.ButtonRole.AcceptRole)
        msg_box.addButton("Оставить как есть", QMessageBox.ButtonRole.RejectRole)
        
        msg_box.exec()
        
        if msg_box.clickedButton() == cleanup_button:
            # Выполняем запись в файл немедленно. Это гарантирует целостность данных.
            project_manager.cleanup_dead_entries(dead_entries)
            # --- ИСПРАВЛЕНИЕ: Убираем лишнее и проблемное окно "Выполнено" ---
            return True
        
        return False
        
# gemini_translator/ui/dialogs/setup.py

    def _prepare_and_display_tasks(self, clean_rebuild=False):
        """
        Собирает задачи, создает/обновляет ChapterQueueManager и
        отправляет "пульс" для перерисовки UI.
        Версия 6.0: Правильная гибридная логика.
        - clean_rebuild=True: Строит задачи с нуля из self.html_files.
        - clean_rebuild=False: Пересобирает задачи на основе текущего порядка в TaskManager.
        """
        if not self.task_manager: return

        # --- КЛЮЧЕВОЕ ИЗМЕНЕНИЕ: Выбор источника глав ---
        if clean_rebuild:
            # "Режим Архитектора": источник - исходный список.
            source_chapters = self.html_files
        else:
            # "Режим Реорганизации": источник - текущее состояние TaskManager'а.
            source_chapters = self._unpack_tasks_to_chapters()
        
        if not source_chapters or not self.selected_file:
            QtCore.QTimer.singleShot(10, lambda: self.task_manager.set_pending_tasks([]))
        else:
            from ...utils.glossary_tools import TaskPreparer
            import zipfile
            import os
            import traceback

            virtual_epub_path = os.copy_to_mem(self.selected_file)
            if not virtual_epub_path:
                error_message = f"Не удалось скопировать EPUB в виртуальную память: {self.selected_file}"
                QtWidgets.QMessageBox.critical(self, "Критическая ошибка файла", error_message)
                return

            real_chapter_sizes = {}
            try:
                with zipfile.ZipFile(open(virtual_epub_path, 'rb'), 'r') as zf:
                    for chapter in set(source_chapters):
                        real_chapter_sizes[chapter] = len(zf.read(chapter).decode('utf-8', 'ignore'))
            except Exception as e:
                tb_str = "".join(traceback.format_exception(type(e), e, e.__traceback__))
                error_message = f"Не удалось прочитать EPUB из виртуальной памяти.\n\n--- Traceback ---\n{tb_str}"
                QtWidgets.QMessageBox.critical(self, "Ошибка обработки EPUB", error_message)
                return
            
            settings = self.get_settings()
            display_tasks_settings = settings.copy()

            preparer = TaskPreparer(display_tasks_settings, real_chapter_sizes)
            plain_payloads = preparer.prepare_tasks(source_chapters)
            self.task_manager.set_pending_tasks(plain_payloads)
        
        QtCore.QTimer.singleShot(15, lambda: self.translation_options_widget._update_info_text())
        

        if self.cpu_performance_index is None and self.html_files and self.glossary_widget.get_glossary():
            print("[INFO] Условия для калибровки выполнены. Запуск будет отложен…")
            self.cpu_performance_index = 1 
            QtCore.QTimer.singleShot(20, lambda: self._calibrate_cpu(no_log=True))


    
    
    def _load_project_glossary(self, folder_path):
        project_glossary_path = os.path.join(folder_path, "project_glossary.json")
        try:
            if os.path.exists(project_glossary_path):
                with open(project_glossary_path, 'r', encoding='utf-8') as f:
                    saved_data = json.load(f)
                
                self.glossary_widget.set_glossary(saved_data)
                print(f"[ИНФО] Глоссарий проекта загружен из: {project_glossary_path}")
            else:
                self.glossary_widget.clear()
        except Exception as e:
            QMessageBox.warning(self, "Ошибка загрузки", f"Не удалось загрузить project_glossary.json: {e}")
    
        # --- ИСПРАВЛЕНИЕ: Создаем независимую копию списка. Это критически важно для определения изменений. ---
        self.initial_glossary_state = [item.copy() for item in self.glossary_widget.get_glossary()]

    def open_translation_validator(self):
        """Открывает инструмент проверки качества переводов."""
        
        # Проверяем, есть ли папка для перевода (она нужна валидатору)
        if not self.output_folder or not os.path.isdir(self.output_folder):
            QMessageBox.warning(self, "Папка не выбрана", "Для запуска проверки необходимо выбрать папку проекта.")
            return
    
        # Проверяем, есть ли исходный EPUB (он тоже нужен)
        if not self.selected_file or not os.path.exists(self.selected_file):
            QMessageBox.warning(self, "Файл не выбран", "Для сравнения переводов необходимо выбрать исходный EPUB-файл.")
            return
    
        self._post_event('log_message', {'message': "[INFO] Открытие инструмента проверки переводов…"})
        
        
        self.setEnabled(False)
        self.is_blocked_by_child_dialog = True
        # Импортируем диалог прямо здесь, чтобы избежать циклических зависимостей
        from .validation import TranslationValidatorDialog
        self.validator_dialog = TranslationValidatorDialog(self.output_folder, self.selected_file, self, project_manager=self.project_manager)
        
        
        self.validator_dialog.exec()
        
        self.setEnabled(True)
        self.is_blocked_by_child_dialog = False
        self._check_and_sync_active_session()
        
        self._post_event('log_message', {'message': "[INFO] Инструмент проверки переводов закрыт."})
    
    def get_settings(self):
        active_keys = self.key_management_widget.get_active_keys()
        provider_id = self.key_management_widget.get_selected_provider()
        
        glossary_list = self.glossary_widget.get_glossary()
        full_glossary_data = {
            entry['original']: {
                'rus': entry.get('rus') or entry.get('translation'),
                'note': entry.get('note')
            }
            for entry in glossary_list
            if entry.get('original')
        }
        
        model_settings = self.model_settings_widget.get_settings()
        translation_options = self.translation_options_widget.get_settings()
    
        model_name = model_settings.get('model')
        model_config = api_config.all_models().get(model_name)
        
        settings = {
            'provider': provider_id,
            'model_config': model_config,
            'file_path': self.selected_file, 
            'output_folder': self.output_folder,
            'api_keys': active_keys, 
            'full_glossary_data': full_glossary_data,
            'custom_prompt': self.preset_widget.get_prompt() or api_config.default_prompt(),
            'auto_start': True, 
            'num_instances': self.instances_spin.value(),
        }
        
        if self.output_folder:
            project_manager = TranslationProjectManager(self.output_folder)
            settings['project_manager'] = project_manager
        
        settings.update(model_settings)
        settings.update(translation_options)
        
        return settings


# gemini_translator\ui\dialogs\setup.py -> class InitialSetupDialog

    # --- ЗАМЕНИТЕ ЭТОТ МЕТОД ЦЕЛИКОМ НА ФИНАЛЬНУЮ ВЕРСИЮ ---
    def perform_dry_run(self):
        """
        Запускает пробный запуск, "замораживая" все задачи, кроме первой.
        """
        if not (self.engine and self.engine.task_manager and self.engine.task_manager.has_pending_tasks() ):
            QMessageBox.warning(self, "Ошибка", "Нет задач для пробного запуска.")
            return
    
        try:
            # 1. "Замораживаем" задачи
            self.engine.task_manager.hold_all_except_first()
            
            # 2. Получаем настройки и модифицируем их для dry_run
            settings = self.get_settings()
            dry_run_settings = settings.copy()
            dry_run_settings.update({
                'provider': 'dry_run', 'api_keys': ['dry_run_dummy_key'], 'num_instances': 1, 'rpm_limit': 1000
            })
            
            # 3. Запускаем сессию (остальное без изменений)
            self.dry_run_start_time = time.perf_counter()
            self._post_event(name='start_session_requested', data={'settings': dry_run_settings})
            
            self.dry_run_btn.setText("Обработка…")
            self.dry_run_btn.setEnabled(False)
    
        except Exception as e:
            # В случае ошибки, "размораживаем" задачи обратно
            if self.engine and self.engine.task_manager:
                self.engine.task_manager.release_held_tasks()
            
            QMessageBox.critical(self, "Ошибка запуска", f"Не удалось запустить пробный запуск:\n{e}")
            self.dry_run_btn.setText("🧪 Пробный запуск")
            self.dry_run_btn.setEnabled(True)
    

    def check_unvalidated_chapters(self):
        """Проверяет, какие главы уже переведены, и предлагает их исключить."""
        if not self.output_folder or not self.html_files: return

        # --- НАЧАЛО ИЗМЕНЕНИЯ: Используем новую, правильную логику ---
        from ...api import config as api_config

        validated_chapters, unvalidated_chapters, untranslated_chapters = set(), set(), []
        epub_base_name = os.path.splitext(os.path.basename(self.selected_file))[0]
        validated_folder = os.path.join(self.output_folder, "validated_ok")
        
        for html_file in self.html_files:
            safe_html_name = re.sub(r'[\\/*?:"<>|]', "_", os.path.splitext(os.path.basename(html_file))[0])
            base_filename = f"{epub_base_name}_{safe_html_name}"

            # 1. Приоритетная проверка: ищем готовую версию
            validated_filepath = os.path.join(validated_folder, f"{base_filename}_validated.html")
            if os.path.exists(validated_filepath):
                validated_chapters.add(html_file)
                continue

            # 2. Вторая проверка: ищем любую переведенную версию
            is_unvalidated = False
            for suffix in api_config.all_translated_suffixes():
                unvalidated_filepath = os.path.join(self.output_folder, f"{base_filename}{suffix}")
                if os.path.exists(unvalidated_filepath):
                    is_unvalidated = True
                    break
            
            if is_unvalidated:
                unvalidated_chapters.add(html_file)
            else:
                untranslated_chapters.append(html_file)
        

        if not validated_chapters and not unvalidated_chapters:
            return

        msg = QMessageBox()
        msg.setWindowTitle("Обнаружены переведенные главы")
        msg.setIcon(QtWidgets.QMessageBox.Icon.Information)
    
        msg.setText(
            f"<b>Анализ выбранных глав ({len(self.html_files)}):</b>\n\n"
            f"✅ <font color='green'>Проверенные ('готовые'):</font> <b>{len(validated_chapters)}</b>\n"
            f"🔵 <font color='blue'>Непроверенные ('переведенные'):</font> <b>{len(unvalidated_chapters)}</b>\n"
            f"⚪ Непереведенные: <b>{len(untranslated_chapters)}</b>"
        )
        msg.setInformativeText("Выберите, какие главы вы хотите включить в текущую сессию перевода:")
            
        btn_skip_all = msg.addButton("Пропустить всё переведенное", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        btn_retranslate_unvalidated = msg.addButton("Перевести непроверенные", QtWidgets.QMessageBox.ButtonRole.ActionRole)
        btn_retranslate_all = msg.addButton("Перевести всё заново", QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
        
        btn_skip_all.setToolTip("Будут переведены только непереведенные главы.")
        btn_retranslate_unvalidated.setToolTip("Перезапишет 'непроверенные', но сохранит 'готовые'.")
        btn_retranslate_all.setToolTip("Полностью перезапишет все существующие переводы.")
        
        msg.exec()
        
        clicked_button = msg.clickedButton()
        original_html_files = self.html_files.copy() # Сохраняем исходный выбор

        if clicked_button == btn_skip_all:
            self.html_files = untranslated_chapters
            info = f"Выбрано глав: {len(self.html_files)} (все переведенные пропущены)"
        elif clicked_button == btn_retranslate_unvalidated:
            self.html_files = untranslated_chapters + list(unvalidated_chapters)
            info = f"Выбрано глав: {len(self.html_files)} (пропущены только 'готовые')"
        elif clicked_button == btn_retranslate_all:
            self.html_files = original_html_files # Возвращаем исходный выбор
            info = f"Выбрано глав: {len(self.html_files)} (все главы будут переведены заново)"
        else:
            self.html_files, self.selected_file = [], None
            self.paths_widget.set_file_path(None)
            info = ""
        
        self._on_project_data_changed()
  
  
    def reject(self):
        """
        Перехватывает событие закрытия. Корректно проверяет наличие ИЗМЕНЕНИЙ
        и предлагает сохранить их только в этом случае.
        """
        # --- ШАГ 1: Определяем, есть ли изменения, требующие диалога ---
        
        has_unsaved_settings = self.is_settings_dirty

        # Глоссарий считаем измененным только если есть папка проекта для сохранения
        has_unsaved_glossary = (self.output_folder and 
                                self.glossary_widget.get_glossary() != self.initial_glossary_state)
        
        # Если ни одна из "грязных" сущностей не изменилась, диалог не нужен
        should_show_dialog = has_unsaved_settings or has_unsaved_glossary
        
        user_choice_to_exit = True

        # --- ШАГ 2: Если изменения есть, показываем диалог ---
        
        if should_show_dialog:
            is_local_mode = self.local_set and self.output_folder
            
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Несохраненные изменения")
            msg_box.setIcon(QMessageBox.Icon.Question)
            
            messages = []
            if has_unsaved_settings: messages.append("настройки сессии")
            if has_unsaved_glossary: messages.append("глоссарий")
            msg_box.setText(f"Обнаружены несохраненные изменения: {', '.join(messages)}.")

            if is_local_mode:
                msg_box.setInformativeText("Сохранить все изменения в файлы текущего проекта?")
                save_btn = msg_box.addButton("Сохранить в Проект", QMessageBox.ButtonRole.AcceptRole)
            else:
                msg_box.setInformativeText("Выберите действие для сохранения.")
                save_btn = msg_box.addButton("Сохранить изменения", QMessageBox.ButtonRole.AcceptRole)
            
            discard_btn = msg_box.addButton("Выйти без сохранения", QMessageBox.ButtonRole.DestructiveRole)
            cancel_btn = msg_box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
            
            msg_box.exec()
            clicked_button = msg_box.clickedButton()

            if clicked_button == save_btn:
                if is_local_mode:
                    if has_unsaved_settings: self._save_project_settings_only()
                    if has_unsaved_glossary: self._save_project_glossary_only()
                else: # Глобальный режим
                    if has_unsaved_settings:
                        self.settings_manager.save_ui_state(self._get_ui_state_for_saving())
                    if has_unsaved_glossary:
                        self._save_project_glossary_only()
            
            elif clicked_button == cancel_btn:
                user_choice_to_exit = False # Отменяем выход
            
            # Если нажата "Выйти без сохранения", user_choice_to_exit остается True

        # --- ШАГ 3: Если пользователь не отменил выход, завершаем работу ---
        
        if user_choice_to_exit:
            # Вне зависимости от сохранения, всегда запоминаем последний использованный
            # промпт и пресет для удобства при следующем запуске.
            # Это состояние сессии, а не "настройка", о которой нужно предупреждать.
            self.settings_manager.save_custom_prompt(self.preset_widget.get_prompt())
            self.settings_manager.save_last_prompt_preset_name(self.preset_widget.get_current_preset_name())
            
            # Вызываем родительский метод для фактического закрытия окна
            super().reject()
    
    
    # --------------------------------------------------------------------
    # ОСТАЛЬНЫЕ ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ (общие для обоих режимов)
    # --------------------------------------------------------------------

    def estimate_tokens(self):
        """Оценивает количество токенов для выбранных глав"""
        if not self.selected_file or not self.html_files:
            QMessageBox.warning(self, "Ошибка", "Сначала выберите файл и главы")
            return
        counter = TokenCounter()
        prompt_text = self.custom_prompt_edit.toPlainText() or " "
        
        # Собираем данные из новой таблицы глоссария в одну строку,
        # чтобы симулировать текстовое представление для подсчета токенов.
        glossary_lines = []
        for row in range(self.glossary_table.rowCount()):
            original_item = self.glossary_table.item(row, 0)
            translation_item = self.glossary_table.item(row, 1)
            
            original = original_item.text().strip() if original_item else ""
            rus = translation_item.text().strip() if translation_item else ""
            
            if original and rus:
                glossary_lines.append(f"{original} = {rus}")
        
        glossary_text = "\n".join(glossary_lines)
        
        try:
            with zipfile.ZipFile(open(self.selected_file, 'rb'), 'r') as epub_zip:
                for html_file in self.html_files[:10]:
                    try:
                        html_content = epub_zip.read(html_file).decode('utf-8', errors='ignore')
                        counter.add_chapter_stats(
                            chapter_name=os.path.basename(html_file),
                            html_size=len(html_content),
                            prompt_size=len(prompt_text),
                            glossary_size=len(glossary_text), 
                            estimated_output=len(html_content)
                        )
                    except Exception as e:
                        print(f"Ошибка при оценке главы {html_file}: {e}")
            if counter.chapters_stats:
                report = counter.get_estimation_report(num_windows=len(self.api_keys))
                dialog = QDialog(self)
                dialog.setWindowTitle("Оценка токенов")
                dialog.setMinimumSize(600, 500)
                layout = QVBoxLayout(dialog)
                text_edit = QTextEdit()
                text_edit.setReadOnly(True)
                text_edit.setFont(QtGui.QFont("Consolas", 10))
                text_edit.setPlainText(report)
                close_btn = QPushButton("Закрыть")
                close_btn.clicked.connect(dialog.accept)
                layout.addWidget(text_edit)
                layout.addWidget(close_btn)
                dialog.exec()
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось оценить токены: {e}")
    
    
    @QtCore.pyqtSlot()
    def _on_project_data_changed(self):
        """
        Единый метод-оркестратор. Вызывается при любом изменении
        основных данных проекта. Централизованно управляет загрузкой
        глоссария и обновлением всего UI.
        """
        print("[DEBUG] Сработал оркестратор _on_project_data_changed")

        # --- "УМНАЯ" ЗАГРУЗКА ГЛОССАРИЯ (ЦЕНТРАЛИЗОВАННАЯ) ---
        if self.output_folder and self.output_folder != self.current_project_folder_loaded:
            print(f"[INFO] Обнаружена смена проекта. Загрузка глоссария для: {os.path.basename(self.output_folder)}")
            self._load_project_glossary(self.output_folder)
            self.current_project_folder_loaded = self.output_folder
        # --------------------------------------------------------

        # 1. Обновляем данные о главах в виджете опций (это быстро и нужно для расчетов)
        self.translation_options_widget.update_chapter_data(self.html_files, self.selected_file)
        
        # 2. Обновляем CJK-опции на основе новых данных о главах
        self._update_cjk_options_for_widgets()
        
        # 3. Пересобираем список задач
        self._prepare_and_display_tasks(clean_rebuild=True)
        self.paths_widget.update_chapters_info(len(self.html_files))
        # 4. Вызываем пересчет рекомендаций, так как данные о главах изменились
        self._update_recommendations()
        
        # 5. Обновляем все остальные зависимые UI элементы
        self.check_ready()
        self._update_distribution_info_from_widget()
        
        # --- НОВАЯ ЛОГИКА ДЛЯ КНОПКИ-МЕТАМОРФА ---
        is_project_defined = bool(self.selected_file and self.output_folder)
        self.use_project_settings_btn.setVisible(is_project_defined)

        if not is_project_defined and self.use_project_settings_btn.isChecked():
            self.use_project_settings_btn.setChecked(False)

        self._update_context_button_style(self.use_project_settings_btn.isChecked())
    
    def _toggle_project_settings_mode(self, use_local):
        """
        Переключает UI между глобальными настройками и настройками проекта,
        НЕ затрагивая глоссарий. Использует self.settings_manager для глобальных операций.
        """
        is_currently_local = not use_local

        if self.is_settings_dirty:
            msg_box = QMessageBox(self)
            msg_box.setIcon(QMessageBox.Icon.Question)
            msg_box.setWindowTitle("Несохраненные изменения")
            
            if is_currently_local:
                msg_box.setText("Вы изменили настройки текущего проекта.")
                msg_box.setInformativeText("Сохранить изменения в файл 'project_settings.json' перед переключением на глобальные?")
                save_btn_text = "Сохранить в Проект"
            else:
                msg_box.setText("Вы изменили глобальные настройки.")
                msg_box.setInformativeText("Перезаписать глобальные настройки перед переключением на проект?")
                save_btn_text = "Перезаписать Глобальные"
            
            save_btn = msg_box.addButton(save_btn_text, QMessageBox.ButtonRole.AcceptRole)
            discard_btn = msg_box.addButton("Не сохранять", QMessageBox.ButtonRole.DestructiveRole)
            cancel_btn = msg_box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
            msg_box.exec()
            clicked = msg_box.clickedButton()

            if clicked == save_btn:
                if is_currently_local:
                    self._save_project_settings_only()
                else:
                    self.settings_manager.save_ui_state(self._get_ui_state_for_saving())
            elif clicked == cancel_btn:
                self.use_project_settings_btn.blockSignals(True)
                self.use_project_settings_btn.setChecked(is_currently_local)
                self.use_project_settings_btn.blockSignals(False)
                return

        # --- Основная логика ЗАГРУЗКИ (БЕЗ глоссария) ---
        if use_local:
            print("[SETTINGS] Переключение на настройки проекта…")
            project_settings_path = os.path.join(self.output_folder, "project_settings.json")
            if os.path.exists(project_settings_path):
                local_manager = SettingsManager(config_file=project_settings_path)
                local_settings = local_manager.load_full_session_settings()
                self.global_settings = self._get_full_ui_settings()
                self._apply_full_ui_settings(local_settings)
                self.local_set = True
            else:
                print("[INFO] Файл настроек проекта не найден. Используются текущие настройки UI.")
        else:
            print("[SETTINGS] Переключение на глобальные настройки…")
            if self.global_settings:
                self._apply_full_ui_settings(self.global_settings)
            self.local_set = False
        # Сбрасываем флаг "грязных" настроек ПОСЛЕ любого переключения.
        # Теперь это работает корректно, т.к. _apply_full_ui_settings не генерирует сигналы.
        self.is_settings_dirty = False
        self.setWindowTitle(self.windowTitle().replace("*", ""))
        
        self._update_context_button_style(use_local)
    
    def _handle_task_reanimation(self, task_ids: list):
        if self.engine and self.engine.task_manager:
            # --- ПЕРЕНОСИМ В ФОНОВЫЙ ПОТОК ---
            self.task_management_widget.setEnabled(False)
            self.status_bar.set_permanent_message("Обновление статусов...")
            
            self.db_worker = TaskDBWorker(self.engine.task_manager.reanimate_tasks, task_ids)
            self.db_worker.finished.connect(self._on_db_worker_finished)
            self.db_worker.start()
        
    def _unpack_tasks_to_chapters(self):
        """
        Извлекает все главы из АКТУАЛЬНОГО списка задач в TaskManager,
        СОХРАНЯЯ ИХ ТОЧНЫЙ ПОРЯДОК, корректно "схлопывая" чанки
        и СОХРАНЯЯ намеренные дубликаты глав.
        """
        if not (self.engine and self.engine.task_manager):
            return []
        
        tasks_with_uuid = self.engine.task_manager.get_all_pending_tasks()
        
        unpacked_chapters_in_order = []
        # --- КЛЮЧЕВОЕ ИЗМЕНЕНИЕ: Отслеживаем ПОСЛЕДНЮЮ добавленную главу ---
        last_added_chapter_from_chunk = None 
            
        for task_id, task_payload in tasks_with_uuid:
            task_type = task_payload[0]
            
            if task_type == 'epub_chunk':
                chapter_path = task_payload[2]
                # Если текущий чанк относится к той же главе, что и предыдущий,
                # мы его просто ИГНОРИРУЕМ.
                if chapter_path == last_added_chapter_from_chunk:
                    continue
                else:
                    # Если это чанк от НОВОЙ главы, добавляем его и запоминаем.
                    unpacked_chapters_in_order.append(chapter_path)
                    last_added_chapter_from_chunk = chapter_path

            elif task_type == 'epub':
                chapter_path = task_payload[2]
                unpacked_chapters_in_order.append(chapter_path)
                # Сбрасываем "память о чанках", так как следующая задача может быть чанком
                last_added_chapter_from_chunk = None
                
            elif task_type == 'epub_batch':
                # Для пакетов просто добавляем все главы как есть, включая дубликаты
                unpacked_chapters_in_order.extend(task_payload[2])
                # Сбрасываем "память о чанках"
                last_added_chapter_from_chunk = None
                
        return unpacked_chapters_in_order
    
    def _update_context_button_style(self, is_local_mode):
        """Обновляет текст, подсказку и стиль кнопки контекста."""
        if is_local_mode:
            # --- ФИНАЛЬНОЕ ИСПРАВЛЕНИЕ ---
            # Используем селектор 'QPushButton', чтобы стиль не "протекал" в QToolTip
            style = """
                QPushButton {
                    background-color: #1A5276;
                    color: white;
                }
            """
            self.use_project_settings_btn.setStyleSheet(style)
            self.use_project_settings_btn.setText("⚙️ Настройки Проекта")
            self.use_project_settings_btn.setToolTip("Используются локальные настройки из файла project_settings.json\nНажмите, чтобы вернуться к глобальным.")
            
        else:
            # Сбрасываем стиль, чтобы вернуть его к системному по умолчанию
            self.use_project_settings_btn.setStyleSheet("")
            self.use_project_settings_btn.setText("🌐 Глобальные настройки")
            self.use_project_settings_btn.setToolTip("Используются глобальные настройки из домашней директории.\nНажмите, чтобы переключиться на настройки проекта (будет создан файл, если его нет).")
    
    def update_keys_count(self):
        """Обновляет счетчик API ключей"""
        keys = [k.strip() for k in self.keys_edit.toPlainText().splitlines() if k.strip()]
        unique_keys = list(set(keys))
        
        
        num_keys = len(unique_keys)
        self.instances_spin.setMaximum(num_keys if num_keys > 0 else 1)
        
        
        if len(keys) != len(unique_keys):
            self.keys_count_label.setText(f"Ключей: {len(unique_keys)} (уникальных из {len(keys)})")
            self.keys_count_label.setStyleSheet("color: orange; font-size: 10px;")
        else:
            self.keys_count_label.setText(f"Ключей: {len(keys)}")
            self.keys_count_label.setStyleSheet("color: blue; font-size: 10px;")
        self._update_distribution_info() # <--- ДОБАВЬ ЭТУ СТРОКУ
        
        
    def update_glossary_count(self):
        """Обновляет счетчик терминов в глоссарии"""
        self.glossary_count_label.setText(f"Терминов: {self.glossary_table.rowCount()}")

    
    def _init_lazy_ui_skeleton(self):
        """Создает минимальный 'скелет' UI для мгновенного отображения."""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        
        self.loading_label = QLabel("<h2>Загрузка интерфейса…</h2>")
        self.loading_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(self.loading_label)

        # Основной контейнер, который будет заполнен позже
        self.main_content_widget = QWidget()
        self.main_content_widget.setVisible(False)
        main_layout.addWidget(self.main_content_widget, 1)
    
    def showEvent(self, event):
        """
        Перехватывает событие первого показа окна и запускает отложенную
        загрузку тяжелых компонентов UI.
        """
        super().showEvent(event)
        
        # Если пока мы были скрыты/свернуты, сессия началась или закончилась — синхронизируемся.
        
        
        if not self._initial_show_done:
            self._initial_show_done = True
            
            # --- Геометрия окна ---
            available_geometry = self.screen().availableGeometry()
            height = int(available_geometry.height() * 0.75)
            width = int(self.minimumWidth() * 1.5)
            if width >= int(available_geometry.width() * 0.90):
                width = int(available_geometry.width() * 0.90)
            self.resize(width, height)
            self.move(
                available_geometry.center().x() - self.width() // 2,
                available_geometry.center().y() - self.height() // 2
            )
            
            # --- Отложенный запуск ---
            # QTimer.singleShot(0, …) выполнит функцию в следующем цикле событий,
            # дав Qt время полностью отрисовать текущее окно.
            QtCore.QTimer.singleShot(50, self._async_populate_and_load)
        else:
            self._check_and_sync_active_session()
            
    def _check_and_sync_active_session(self):
        """
        Принудительно проверяет наличие активной сессии в глобальном состоянии (EventBus/Engine).
        Используется для восстановления UI, если событие 'session_started' было пропущено.
        Возвращает True, если сессия активна (и UI был синхронизирован), иначе False.
        """
        # 1. Спрашиваем у Шины (Главный источник правды)
        active_session_id = None
        if self.bus and hasattr(self.bus, 'get_data'):
            active_session_id = self.bus.get_data("current_active_session")
        
        # 2. Если Шина молчит, спрашиваем у Движка напрямую (Резерв)
        if not active_session_id and self.engine and self.engine.session_id:
             active_session_id = self.engine.session_id

        # 3. АНАЛИЗ: Если сессия ЕСТЬ, но мы думаем, что СПИМ (is_session_active=False)
        if active_session_id and not self.is_session_active:
            print(f"[UI RECOVERY] ⚠️ Обнаружена рассинхронизация! Сессия {active_session_id} работает, а диалог спит. Блокирую интерфейс.")
            self.is_session_active = True
            
            # Принудительно переводим UI в режим "Сессия идет" (блокируем инпуты, включаем Стоп)
            self._set_controls_enabled(False)
            
            # Если это первая синхронизация, обновляем статус бар с актуальным количеством задач
            if self.status_bar:
                current_total = 0
                if self.task_manager:
                    # Получаем актуальное количество задач из менеджера (восстанавливаем контекст)
                    try:
                        current_total = len(self.task_manager.get_ui_state_list())
                    except Exception:
                        current_total = 0
                self.status_bar.start_session(current_total)
                
            return True
        
        # 4. Если сессия ЕСТЬ и мы ЗНАЕМ об этом — просто подтверждаем статус
        if active_session_id and self.is_session_active:
            self._set_controls_enabled(False)
            return True

        # Сессии нет
        self._set_controls_enabled(True)
        return False
        
    def _async_populate_and_load(self):
        """Асинхронный orchestrator: сначала строит UI, потом загружает данные."""
        # 1. Создаем все тяжелые виджеты
        self._populate_full_ui()
        
        # 2. Загружаем данные в уже созданные виджеты
        self._load_initial_data()

        # 3. "Подменяем" заглушку на готовый интерфейс
        self.loading_label.setVisible(False)
        self.main_content_widget.setVisible(True)
        
    def _show_custom_message(self, title, text, icon=QMessageBox.Icon.Information, informative_text="", button_text="ОК"):
        """Показывает QMessageBox с кастомной кнопкой 'ОК'."""
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle(title)
        msg_box.setIcon(icon)
        msg_box.setText(text)
        if informative_text:
            msg_box.setInformativeText(informative_text)
        # Добавляем свою кнопку с нужным текстом
        ok_button = msg_box.addButton(button_text, QMessageBox.ButtonRole.AcceptRole)
        msg_box.exec()

    def closeEvent(self, event):
        """Отписываемся от шины событий перед уничтожением окна."""
        if self.bus:
            try:
                self.bus.event_posted.disconnect(self.on_event)
            except (TypeError, RuntimeError):
                pass # Соединение уже могло быть разорвано
        super().closeEvent(event)