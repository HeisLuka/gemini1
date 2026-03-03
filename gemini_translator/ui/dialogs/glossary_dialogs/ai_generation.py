# НОВЫЙ ФАЙЛ: gemini_translator\ui\dialogs\glossary_dialogs\ai_generation.py
import re
import json
import os
import io
import zipfile
import time
import json
import math
from os_patch import PatientLock
from PyQt6 import QtWidgets, QtCore, QtGui
from PyQt6.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QPushButton, QDialogButtonBox, QLabel,
    QWidget, QGroupBox, QHBoxLayout, QTableWidget, QHeaderView, QTableWidgetItem,
    QMessageBox, QCheckBox
)
from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot, QObject, QTimer

# Импорты виджетов, которые мы переиспользуем
from gemini_translator.ui.widgets.glossary_widget import GlossaryWidget 
from gemini_translator.ui.widgets.key_management_widget import KeyManagementWidget
from gemini_translator.ui.widgets.model_settings_widget import ModelSettingsWidget
from gemini_translator.ui.widgets.log_widget import LogWidget
from gemini_translator.ui.widgets.preset_widget import PresetWidget
from gemini_translator.ui.widgets.chapter_list_widget import ChapterListWidget
from gemini_translator.utils.language_tools import LanguageDetector
# Импорты для работы движка
from gemini_translator.api import config as api_config
from gemini_translator.utils.glossary_tools import GlossaryAggregator, ContextManager
from gemini_translator.utils.settings import SettingsManager
from gemini_translator.core.task_manager import TaskDBWorker
from gemini_translator.ui.widgets.common_widgets import NoScrollSpinBox
from gemini_translator.core.workers import RPMLimiter
from gemini_translator.utils.text import prettify_html_for_ai
from .numbers_master import NumeralsExtractionWorker

class SequentialTaskProvider(QObject):
    """
    Класс-оркестратор для последовательной генерации глоссария.
    Общается с TranslationEngine исключительно через глобальную шину событий.
    """

    def __init__(self, settings_getter, parent=None, event_bus=None, translate_engine=None): # <-- Аргументы изменились
        super().__init__(parent)
        
        self.bus = event_bus
        if self.bus is None:
            app = QtWidgets.QApplication.instance()
            if hasattr(app, 'event_bus'):
                self.bus = app.event_bus
            else:
                raise RuntimeError("SequentialTaskProvider requires an event bus.")
        
        self.MANAGED_SESSION_FLAG_KEY = f"managed_session_active_{id(self)}"
        
        self.engine = translate_engine
        if self.engine is None:
            app = QtWidgets.QApplication.instance()
            if hasattr(app, 'engine'):
                self.engine = app.engine
            else:
                raise RuntimeError("SequentialTaskProvider requires an engine.")
        
        self.task_manager = self.engine.task_manager
        
        # --- НОВЫЕ, УПРОЩЕННЫЕ АТРИБУТЫ ---
        self.settings_getter = settings_getter
        self.current_task_index = -1
        self.total_tasks = 0
        self._is_running = False
        self._is_stopping = False
        self._task_in_flight = False
        self.rpm_limiter = None
        self.bus.event_posted.connect(self.on_event)
        
    def _post_event(self, name: str, data: dict = None):
        session_id = self.engine.session_id if self.engine and self.engine.session_id else None
        event = {
            'event': name,
            'source': 'SequentialTaskProvider',
            'session_id': session_id,
            'data': data or {}
        }
        self.bus.event_posted.emit(event)

    def start(self):
        """
        Запускает управляемую последовательную сессию.
        1. "Замораживает" все реальные задачи.
        2. Добавляет в очередь "задачу-стража" в качестве "якоря".
        3. Запускает TranslationEngine.
        4. С небольшой задержкой инициирует выполнение первой реальной задачи.
        """
        # Проверяем наличие задач в центральном TaskManager
        if not (self.engine and self.engine.task_manager and self.engine.task_manager.has_pending_tasks() ):
            self._post_event('log_message', {'message': "[ORCHESTRATOR] Нет задач для запуска последовательной генерации."})
            return
        if self._is_running:
            return

        # Считаем общее количество РЕАЛЬНЫХ задач для UI
        if self.engine and self.engine.task_manager:
            self.total_tasks = len(self.engine.task_manager.get_all_pending_tasks())
        
        self._is_running = True
        self._is_stopping = False

        # Получаем и настраиваем параметры сессии
        settings = self.settings_getter()
        rpm_value = settings.get('rpm_limit', 10)
        self.rpm_limiter = RPMLimiter(rpm_limit=rpm_value)
        settings['num_instances'] = 1
        settings['max_concurrent_requests'] = 1
        
        if self.engine and self.engine.task_manager:
            self.engine.task_manager.hold_all_pending_tasks()
            self.bus.set_data(self.MANAGED_SESSION_FLAG_KEY, True)
            self._post_event('log_message', {'message': "[ORCHESTRATOR] Установлен флаг управляемой сессии."})
            
            # 1. Немедленно готовим первую задачу. Состояние TaskManager обновлено.
            self._run_next_task()
            
        else:
            self._post_event('log_message', {'message': "[ORCHESTRATOR-ERROR] TaskManager не найден. Невозможно запустить сессию."})
            self._is_running = False
            return
            
        # --- КЛЮЧЕВОЕ ИЗМЕНЕНИЕ (ВАШЕ ПРЕДЛОЖЕНИЕ) ---
        # 2. Запускаем сессию с гарантированной задержкой.
        # Это дает 100% уверенность, что движок увидит задачу, которую мы подготовили выше.
        QTimer.singleShot(
            100, 
            lambda: self._post_event('start_session_requested', {'settings': settings})
        )

    
    def _get_request_data_snapshot(self, settings):
        """Создает 'снимок' списков временных меток для всех ключей сессии."""
        snapshot = {}
        model_id = settings.get('model_config', {}).get('id')
        if not model_id:
            return {}
            
        for key in self.session_keys:
            key_info = self.settings_manager.get_key_info(key)
            if key_info:
                # Используем новый метод из SettingsManager
                snapshot[key] = self.settings_manager.get_request_timestamps(key_info, model_id)
        return snapshot

    def _get_request_counts_snapshot(self, settings):
        """Создает 'снимок' текущих счетчиков запросов для всех ключей сессии."""
        counts = {}
        model_id = settings.get('model_config', {}).get('id')
        if not model_id:
            return {}
            
        for key in self.session_keys:
            key_info = self.settings_manager.get_key_info(key)
            if key_info:
                counts[key] = self.settings_manager.get_request_count(key_info, model_id)
        return counts
    
    @pyqtSlot(dict)
    def on_event(self, event: dict):
        if not self._is_running: return
        event_name = event.get('event')
        if event_name == 'session_finished':
            self._is_running = False
            # Если сессия завершилась, а мы этого не ожидали (например, из-за ошибки)
            if not self._is_stopping:
                self._post_event('generation_finished', {'was_cancelled': True})
        
        # --- Слушаем стандартное событие --- 
        if event_name == 'task_finished':
            data = event.get('data', {})
            task_info = data.get('task_info') # Получаем (id, payload)
            if task_info and isinstance(task_info, tuple):
                task_id, task_payload = task_info
                if task_payload[0] == 'glossary_batch_task' and self._task_in_flight == task_id:
                # Наша задача выполнена! Запускаем обработку.
                    self._on_batch_finished(event)



    def _on_batch_finished(self, finish_event):
        if not self._task_in_flight:
            return
        self._task_in_flight = None

        if self._is_stopping:
            self._finish_session(was_cancelled=True)
            return
        
        # Просто запускаем следующую задачу. Больше никакой логики слияния.
        self._run_next_task()

    def _run_next_task(self):
        
        if self._is_stopping:
            self._finish_session(was_cancelled=True)
            return

        if self.engine:
            if self.task_manager:
                if not self.task_manager.has_held_tasks():
                    self._finish_session(was_cancelled=False)
                    return

        if self.rpm_limiter and not self.rpm_limiter.can_proceed():
            QTimer.singleShot(100, self._run_next_task)
            return
        


        next_task_info = self.engine.task_manager.peek_next_held_task()
        if not next_task_info:
            self._finish_session(was_cancelled=False)
            return
            
        task_id, task_payload = next_task_info

        self.current_task_index += 1
        self._post_event('progress_updated', {'current': self.current_task_index, 'total': self.total_tasks})
        task_name = f"пакет #{self.current_task_index + 1}/{self.total_tasks}"
        
        # --- ГЛАВНОЕ УПРОЩЕНИЕ ---
        # Мы больше НЕ модифицируем payload. Он уже был подготовлен UI.
        # Просто "пробуждаем" задачу в ее исходном виде.
        self.engine.task_manager.promote_held_task(task_id, task_payload)
        
        self._post_event('log_message', {'message': f"Задача {task_name} отправлена на выполнение..."})
        self._task_in_flight = task_id

    def stop(self):
        """Инициирует ТОЛЬКО плавную остановку со стороны оркестратора."""
        if not self._is_running or self._is_stopping: 
            return
            
        self._is_stopping = True
        self._post_event('log_message', {'message': "[ORCHESTRATOR] Инициирована плавная остановка. Новые задачи выдаваться не будут."})
        self.bus.pop_data(self.MANAGED_SESSION_FLAG_KEY, None)
        # Если в данный момент нет активной задачи, то _on_batch_finished не будет вызван.
        # Значит, мы должны сами запустить процесс завершения.
        if not self._task_in_flight:
            self._finish_session(was_cancelled=True)
             
    def _finish_session(self, was_cancelled):
        """Финальная стадия завершения. Снимает флаг и отправляет финальные события."""
        if not self._is_running: 
            return

        if self.bus.pop_data(self.MANAGED_SESSION_FLAG_KEY, None):
            self._post_event('log_message', {'message': "[ORCHESTRATOR] Флаг управляемой сессии снят."})
        
        self.bus.pop_data(self.MANAGED_SESSION_FLAG_KEY, None)
        self._post_event('manual_stop_requested')
        
        # Просто сообщаем о факте завершения.
        self._post_event('generation_finished', {'was_cancelled': was_cancelled})
        
        self._is_running = False

class GenerationSessionDialog(QDialog):
    generation_finished = pyqtSignal(list, set)

    def __init__(self, settings_manager, initial_glossary, merge_mode, html_files, epub_path, project_manager, initial_ui_settings, parent=None, event_bus=None, translate_engine=None):
        super().__init__(parent)
        self.settings_manager = settings_manager
        self._accumulated_processed_chapters = set()

        self._recovery_lock = PatientLock()
        self.html_files = html_files
        self.epub_path = epub_path
        self.project_manager = project_manager

        self.orchestrator = None
        self.recovery_file_path = None
        self.is_session_active = False
        self.is_soft_stopping = False
        self.force_exit_on_interrupt = False
        self._session_was_restored = False
        self._initial_load_done = False
        
        app = QtWidgets.QApplication.instance()
        self.bus = event_bus
        if self.bus is None:
            if hasattr(app, 'event_bus'): self.bus = app.event_bus
            else: raise RuntimeError("GenerationSessionDialog requires an event bus.")
        
        self.engine = translate_engine
        if self.engine is None:
            if hasattr(app, 'engine'): self.engine = app.engine
            else: raise RuntimeError("GenerationSessionDialog requires an engine.")
        
        self.task_manager = self.engine.task_manager if self.engine.task_manager else None
        
        self.bus.event_posted.connect(self._on_global_event)

        self.setWindowTitle("Генерация Глоссария с помощью AI")
            
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
        
        
        self.autosave_timer = QTimer(self)
        self.autosave_timer.setSingleShot(True)
        self.autosave_timer.setInterval(1500)
        self.autosave_timer.timeout.connect(self._trigger_autosave)
        
        self._init_ui()
        
        self._check_for_recovery_session()

        if not self._session_was_restored:
            if initial_ui_settings:
                self._apply_initial_settings(initial_ui_settings)
            
            # Просто устанавливаем начальный глоссарий в виджет
            if initial_glossary:
                self.glossary_widget.set_glossary(initial_glossary)
        
        
    def _post_event(self, name: str, data: dict = None):
        session_id = self.engine.session_id if self.engine and self.engine.session_id else None
        event = {
            'event': name,
            'source': 'GenerationSessionDialog',
            'session_id': session_id,
            'data': data or {}
        }
        self.bus.event_posted.emit(event)
    
    
    
    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        self.tabs = QtWidgets.QTabWidget()

        settings_tab = self._create_settings_tab()
        tasks_tab = self._create_tasks_tab()
        prompt_tab = self._create_prompt_tab()
        results_tab = self._create_results_tab()

        self.tabs.addTab(settings_tab, "⚙️ Настройки")
        self.tabs.addTab(tasks_tab, "📋 Список Задач")
        self.tabs.addTab(prompt_tab, "📝 Промпт")
        self.tabs.addTab(results_tab, "📊 Результаты и Лог")

        main_layout.addWidget(self.tabs)

        # --- НИЖНЯЯ ПАНЕЛЬ УПРАВЛЕНИЯ ---
        bottom_control_layout = QHBoxLayout()
        bottom_control_layout.setContentsMargins(0, 5, 0, 0)

        # Секция лимита новых терминов
        limit_container = QWidget()
        limit_layout = QHBoxLayout(limit_container)
        limit_layout.setContentsMargins(0, 0, 0, 0)
        
        limit_label = QLabel("Лимит новых терминов:")
        limit_label.setToolTip("Сколько МАКСИМУМ новых терминов брать из ответа AI за один проход. Ноль — без лимита.\n"
                               "Отбирает те термины, что в начале, отбрасывая те, что в конце ответа.\n"
                               "Термины, которые уже есть в базе (обновление), принимаются всегда без лимита.")
        
        self.new_terms_limit_spin = NoScrollSpinBox()
        self.new_terms_limit_spin.setRange(5, 500)
        self.new_terms_limit_spin.setValue(50) # Дефолт, будет пересчитан
        self.new_terms_limit_spin.setSuffix(" шт.")
        self.new_terms_limit_spin.setToolTip(limit_label.toolTip())
        
        limit_layout.addWidget(limit_label)
        limit_layout.addWidget(self.new_terms_limit_spin)
        
        bottom_control_layout.addWidget(limit_container)
        bottom_control_layout.addStretch() # Распорка между спинбоксом и кнопками

        # Стандартные кнопки
        self.button_box = QDialogButtonBox()
        self.start_btn = self.button_box.addButton("🚀 Начать", QDialogButtonBox.ButtonRole.ActionRole)
        self.soft_stop_btn = self.button_box.addButton("Завершить плавно", QDialogButtonBox.ButtonRole.ActionRole)
        self.hard_stop_btn = self.button_box.addButton("❌ Прервать", QDialogButtonBox.ButtonRole.DestructiveRole)

        self.apply_btn = self.button_box.addButton("Применить и Закрыть", QDialogButtonBox.ButtonRole.AcceptRole)
        self.close_btn = self.button_box.addButton("Закрыть", QDialogButtonBox.ButtonRole.RejectRole)
        
        self.soft_stop_btn.setVisible(False)
        self.hard_stop_btn.setVisible(False)
        self.apply_btn.setVisible(False)

        self.start_btn.clicked.connect(self._on_start_stop_clicked)
        self.soft_stop_btn.clicked.connect(self._on_soft_stop_clicked)
        self.hard_stop_btn.clicked.connect(self._on_hard_stop_clicked)
        self.apply_btn.clicked.connect(self.accept)
        self.close_btn.clicked.connect(self.reject)

        bottom_control_layout.addWidget(self.button_box)
        main_layout.addLayout(bottom_control_layout)
        
        # Подключения сигналов
        self.key_widget.active_keys_changed.connect(self._update_start_button_state)
        self.glossary_widget.glossary_changed.connect(self._update_start_button_state)
        self.key_widget.provider_combo.currentIndexChanged.emit(
            self.key_widget.provider_combo.currentIndex()
        )
    
    def _create_tasks_tab(self):
        """Создает и настраивает вкладку со списком задач."""
        tasks_tab_widget = QWidget()
        layout = QVBoxLayout(tasks_tab_widget)

        from gemini_translator.ui.widgets.translation_options_widget import TranslationOptionsWidget
        self.translation_options_widget = TranslationOptionsWidget(self)
        
        self.translation_options_widget.batch_checkbox.setChecked(True)
        modes_group = self.translation_options_widget.findChild(QGroupBox, "modes_group")
        if modes_group:
            modes_group.setVisible(False)

        layout.addWidget(self.translation_options_widget)
        
        action_panel_layout = QHBoxLayout()
        self.reselect_chapters_btn = QPushButton("Главы: ...")
        self.reselect_chapters_btn.setToolTip("Нажмите, чтобы выбрать главы заново")
        self.reselect_chapters_btn.clicked.connect(self._reselect_chapters_for_glossary)
        
        self.extract_numerals_btn = QPushButton("🔢 Найти числительные")
        self.extract_numerals_btn.setToolTip("Просканировать текст всех глав и найти числа на разных языках,\nчтобы добавить их в глоссарий в русской транскрипции.")
        self.extract_numerals_btn.clicked.connect(self._on_extract_numerals_clicked)
        
        self.rebuild_tasks_btn = QPushButton("🔄 Применить и пересобрать")
        self.rebuild_tasks_btn.clicked.connect(self._rebuild_glossary_tasks)
        
        self.remove_generated_btn = QPushButton("🗑️ Убрать сгенерированные")
        self.remove_generated_btn.setToolTip("Убрать из списка главы, для которых глоссарий уже был успешно сгенерирован ранее.")
        self.remove_generated_btn.clicked.connect(self._remove_generated_chapters)
        self.remove_generated_btn.setEnabled(bool(self._get_all_processed_chapters()))
    
        action_panel_layout.addWidget(self.reselect_chapters_btn)
        action_panel_layout.addWidget(self.extract_numerals_btn)
        action_panel_layout.addWidget(self.remove_generated_btn)
        action_panel_layout.addWidget(self.rebuild_tasks_btn)
        action_panel_layout.addStretch()
        layout.addLayout(action_panel_layout)
        
        
        
        

        self.chapter_list_widget = ChapterListWidget(self)
        self.chapter_list_widget.set_copy_originals_visible(False)
        # --- Подключаем сигналы к новым слотам ---
        self.chapter_list_widget.reorder_requested.connect(self._handle_task_reorder)
        self.chapter_list_widget.duplicate_requested.connect(self._handle_task_duplication)
        self.chapter_list_widget.remove_selected_requested.connect(self._handle_task_removal)
        self.chapter_list_widget.reanimate_requested.connect(self._handle_task_reanimation)
        layout.addWidget(self.chapter_list_widget, 1)

        self.reselect_chapters_btn.setText(f"Главы: {len(self.html_files)}")

        return tasks_tab_widget
        
    def _update_new_terms_limit_from_current_size(self):
        """
        Обновляет лимит новых терминов на основе ТЕКУЩЕГО значения размера пакета.
        Вызывается автоматически при изменении task_size_spin.
        """
        if not hasattr(self, 'new_terms_limit_spin') or not hasattr(self, 'translation_options_widget'):
            return

        current_chars = self.translation_options_widget.task_size_spin.value()
        
        # Определяем коэффициент (chars_per_token)
        chars_per_token = api_config.CHARS_PER_ASCII_TOKEN 
        if self.html_files and self.epub_path:
            try:
                with zipfile.ZipFile(open(self.epub_path, 'rb'), 'r') as zf:
                    sample = zf.read(self.html_files[0]).decode('utf-8', 'ignore')[:2000]
                    if LanguageDetector.is_cjk_text(sample):
                        chars_per_token = 1.5
                    elif re.search(r'[а-яА-ЯёЁ]', sample):
                        chars_per_token = api_config.CHARS_PER_CYRILLIC_TOKEN
            except Exception: pass

        # Расчет в токенах: ~1 новый термин на каждые 500 токенов контента
        estimated_tokens = current_chars / chars_per_token
        recommended_limit = self.round_up_to_tens(max(10, int(estimated_tokens / 500)))
        
        clamped_limit = max(self.new_terms_limit_spin.minimum(), 
                            min(recommended_limit, self.new_terms_limit_spin.maximum()))
        
        self.new_terms_limit_spin.blockSignals(True)
        self.new_terms_limit_spin.setValue(clamped_limit)
        self.new_terms_limit_spin.blockSignals(False)
    def round_up_to_tens(self, n):
        """
        Округляет число n до ближайшего десятка в большую сторону.
        """
        # 1. Делим на 10.0, чтобы получить число с плавающей точкой (например, 23 -> 2.3)
        # 2. math.ceil() округляет его до ближайшего целого ВВЕРХ (2.3 -> 3.0)
        # 3. Умножаем обратно на 10 (3.0 * 10 -> 30.0)
        # 4. Преобразуем в целое число (30.0 -> 30)
        return int(math.ceil(n / 10.0)) * 10
  
    def _apply_initial_settings(self, settings: dict):
        """Применяет начальные настройки, принудительно отключая системные инструкции."""
        if not hasattr(self, 'model_settings_widget'):
            return
            
        # --- КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ ---
        # Перед применением настроек мы принудительно сбрасываем состояние
        # системных инструкций, чтобы оно не "наследовалось" из главного окна.
        settings['system_instruction'] = None
        
        # Теперь применяем уже модифицированные настройки
        self.model_settings_widget.set_settings(settings)
        
        # Обновляем зависимые виджеты, такие как CJK, после применения настроек
        self._update_dependent_widgets()
    
    def _update_task_status_in_list(self, task_tuple, status):
        """
        Находит строку с задачей в списке и обновляет ее статус и цвет.
        Теперь это ЕДИНЫЙ источник для всех статусов в этом диалоге.
        """
        table = self.chapter_list_widget.table
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if item and item.data(QtCore.Qt.ItemDataRole.UserRole) == task_tuple:
                status_item = table.item(row, 1)
                if not status_item:
                    status_item = QTableWidgetItem()
                    table.setItem(row, 1, status_item)
    
                # Ваше изменение для консистентности UI
                status_map = {
                    'success': ("✅ Сгенерировано", "#2ECC71"),
                    'error': ("❌ Ошибка", "#E74C3C"),
                    'filtered': ("🛡️ Фильтр", "#9B59B6"),
                    'pending': ("⏳ Ожидание", self.palette().color(QtGui.QPalette.ColorRole.Text).name())
                }
                display_text, color_hex = status_map.get(status, ("?", "#FFFFFF"))
    
                status_item.setText(display_text)
                
                brush = QtGui.QBrush(QtGui.QColor(color_hex))
                if item: item.setForeground(brush)
                status_item.setForeground(brush)
                break
                
    def _check_and_sync_active_session(self):
        """
        Принудительно проверяет наличие активной сессии в глобальном состоянии (EventBus/Engine).
        Используется для восстановления UI, если событие 'session_started' было пропущено.
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
            # Принудительно переводим UI в режим "Сессия идет"
            self._set_ui_active(True)
            return True
        
        # 4. Если сессия ЕСТЬ и мы ЗНАЕМ об этом — просто подтверждаем статус
        if active_session_id and self.is_session_active:
            return True

        # Сессии нет
        return False
    
    # --- Проверка и восстановление сессии ---
    def _check_for_recovery_session(self):
        """Проверяет наличие цепочки файлов восстановления и пытается загрузить самый свежий валидный."""
        self._session_was_restored = False
        if not (self.project_manager and self.project_manager.project_folder):
            return

        candidates = self._get_recovery_candidates()
        if not candidates:
            return

        # Пытаемся прочитать файлы, начиная с последнего (i+1, затем i)
        recovery_data = None
        valid_candidate_path = None
        
        for idx, path in candidates:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                # Если успешно прочитали JSON, считаем файл живым
                recovery_data = data
                valid_candidate_path = path
                break
            except (json.JSONDecodeError, OSError):
                # Если файл битый (свет моргнул при записи), пробуем предыдущий
                continue
        
        if not recovery_data:
            # Если ни один файл не прочитался
            QMessageBox.warning(self, "Ошибка восстановления", 
                                "Обнаружены файлы восстановления, но все они повреждены.\nСессия будет начата с нуля.")
            self._cleanup_all_recovery_files()
            return

        # Если нашли рабочий файл
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Обнаружена прерванная сессия")
        msg_box.setText(f"Найден файл восстановления (версия {os.path.basename(valid_candidate_path)}).")
        msg_box.setInformativeText("Хотите восстановить все настройки, прогресс и продолжить с того места, где остановились?")
        resume_btn = msg_box.addButton("Да, восстановить сессию", QMessageBox.ButtonRole.YesRole)
        restart_btn = msg_box.addButton("Нет, начать заново", QMessageBox.ButtonRole.NoRole)
        msg_box.exec()
        
        if msg_box.clickedButton() == resume_btn:
            try:
                recovered_glossary = recovery_data.get("progress", {}).get("glossary", [])
                recovered_chapters = set(recovery_data.get("progress", {}).get("processed_chapters", []))
                recovered_settings = recovery_data.get("settings", {})
                
                self._accumulated_processed_chapters.update(recovered_chapters)
                self._apply_full_ui_settings(recovered_settings)
                
                self.html_files = [ch for ch in self.html_files if ch not in recovered_chapters]
                self._rebuild_glossary_tasks()
                
                self._session_was_restored = True
                QMessageBox.information(self, "Сессия восстановлена", 
                                        f"Загружено {len(recovered_glossary)} терминов.\n"
                                        f"Исключено {len(recovered_chapters)} готовых глав.")
                
                self.apply_btn.setVisible(True)
                self.glossary_widget.set_glossary(recovered_glossary)
                
                # Удаляем старые файлы, чтобы начать чистую цепочку сохранений
                self._cleanup_all_recovery_files()

            except Exception as e:
                QMessageBox.critical(self, "Ошибка восстановления", f"Сбой при применении данных: {e}")
                self._cleanup_all_recovery_files()
        else:
            # Пользователь выбрал "Нет" -> удаляем все
            self._cleanup_all_recovery_files()
            
    def _get_recovery_candidates(self):
        """Возвращает список (index, full_path) найденных файлов восстановления, от новых к старым."""
        if not (self.project_manager and self.project_manager.project_folder):
            return []
        
        candidates = []
        try:
            # Ищем файлы вида ~glossary_session_recovery_123.json
            prefix = "~glossary_session_recovery_"
            suffix = ".json"
            for fname in os.listdir(self.project_manager.project_folder):
                if fname.startswith(prefix) and fname.endswith(suffix):
                    try:
                        # Парсим индекс из имени файла
                        idx_str = fname[len(prefix):-len(suffix)]
                        idx = int(idx_str)
                        candidates.append((idx, os.path.join(self.project_manager.project_folder, fname)))
                    except ValueError:
                        continue
        except OSError:
            pass
        
        # Сортируем: сначала самые большие индексы (самые свежие)
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates
    
    def _perform_safe_recovery_save(self):
        """Сохраняет состояние с инкрементом версии и удалением старых файлов (схема i и i+1)."""
        if not (self.project_manager and self.project_manager.project_folder):
            return

        snapshot = self._create_recovery_snapshot()
        
        with self._recovery_lock:
            # 1. Вычисляем следующий индекс
            candidates = self._get_recovery_candidates()
            last_idx = candidates[0][0] if candidates else 0
            next_idx = last_idx + 1
            
            base_name = os.path.join(self.project_manager.project_folder, f"~glossary_session_recovery_{next_idx}.json")
            
            try:
                # 2. Пишем новый файл (i+1)
                with open(base_name, 'w', encoding='utf-8') as f:
                    json.dump(snapshot, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno()) # Принудительный сброс на диск для защиты от сбоев питания
                
                # 3. Удаляем ВСЕ старые файлы (i, i-1...)
                # Удаляем только после успешной записи нового.
                for _, old_path in candidates:
                    try:
                        os.remove(old_path)
                    except OSError:
                        pass
            except Exception as e:
                # Если запись не удалась, старый файл (i) остается нетронутым!
                error_msg = f"[SYSTEM-WARN] Не удалось сохранить файл восстановления: {e}"
                if hasattr(self, 'log_widget'):
                    self.log_widget.append_message({"message": error_msg})
                print(error_msg)
    
    def _cleanup_all_recovery_files(self):
        """Удаляет все найденные файлы восстановления."""
        candidates = self._get_recovery_candidates()
        for _, path in candidates:
            try:
                os.remove(path)
            except OSError:
                pass
                
    def _on_extract_numerals_clicked(self):
        if not self.epub_path or not self.html_files:
            QMessageBox.warning(self, "Нет данных", "Сначала выберите EPUB файл и главы.")
            return

        msg = QMessageBox(self)
        msg.setWindowTitle("Поиск числительных")
        msg.setIcon(QMessageBox.Icon.Information) # Добавим иконку для красоты
        msg.setText("Этот процесс просканирует текст и найдет числа (Английские, Китайские, Японские, Корейские и др.), "
                    "преобразовав их в русские слова (например: 'twenty-one' -> 'двадцать один').")
        msg.setInformativeText("Это может занять некоторое время. Добавить найденное в текущий глоссарий?")
        
        # --- КАСТОМНЫЕ КНОПКИ ---
        start_btn = msg.addButton("Начать поиск", QMessageBox.ButtonRole.AcceptRole)
        cancel_btn = msg.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        
        msg.exec()
        
        # Проверяем, какая именно кнопка была нажата
        if msg.clickedButton() != start_btn:
            return

        # Блокируем UI
        self.extract_numerals_btn.setEnabled(False)
        self.extract_numerals_btn.setText("Сканирование...")
        
        # Запускаем Worker
        self.num_worker = NumeralsExtractionWorker(self.epub_path, self.html_files, self)
        self.num_worker.progress.connect(self._on_numerals_progress)
        self.num_worker.finished.connect(self._on_numerals_finished)
        self.num_worker.start()

    def _on_numerals_progress(self, current, total):
        self.extract_numerals_btn.setText(f"Сканирование... {current}/{total}")

    def _on_numerals_finished(self, new_items, status_msg):
        self.extract_numerals_btn.setEnabled(True)
        self.extract_numerals_btn.setText("🔢 Найти числительные")
        self.num_worker = None # Очистка
        
        if not new_items:
            QMessageBox.information(self, "Результат", status_msg)
            return

        # Записываем данные в базу, имитируя результат работы AI-движка.
        # Используем текущее время, чтобы эти данные считались "свежими".
        initial_glossary_for_session = self.glossary_widget.get_glossary()
        if self.task_manager:
            try:
                current_timestamp = time.time()
                
                self.task_manager.clear_glossary_results()
                if initial_glossary_for_session:
                    data_to_insert = [
                        ('initial', 0, '[]', item.get('original'), item.get('rus'), item.get('note'))
                        for item in initial_glossary_for_session
                    ]
                    with self.task_manager._get_write_conn() as conn:
                        conn.executemany("INSERT INTO glossary_results (task_id, timestamp, chapters_json, original, rus, note) VALUES (?, ?, ?, ?, ?, ?)", data_to_insert)
                
                # Подготавливаем данные для SQL: (task_id, timestamp, chapters_json, original, rus, note)
                # '[]' в chapters_json означает, что термин глобальный (найден во всей книге)
                data_to_insert = [
                    ('numerals', current_timestamp, '[]', item['original'], item['rus'], item['note'])
                    for item in new_items
                ]
                
                with self.task_manager._get_write_conn() as conn:
                    conn.executemany(
                        "INSERT INTO glossary_results (task_id, timestamp, chapters_json, original, rus, note) VALUES (?, ?, ?, ?, ?, ?)", 
                        data_to_insert
                    )
                
                # Самый важный шаг: вызываем стандартное обновление.
                # Этот метод сам сходит в базу, применит режим (Дополнить/Обновить/Накопить)
                # и отобразит результат в таблице.
                self._refresh_glossary_from_db()
                
                # Обновляем файл восстановления на случай сбоя
                self._perform_safe_recovery_save()

                QMessageBox.information(self, "Готово", f"{status_msg}\nЗаписано в базу: {len(data_to_insert)} записей.\nТаблица обновлена.")
                
            except Exception as e:
                QMessageBox.critical(self, "Ошибка базы данных", f"Не удалось сохранить числительные:\n{e}")
    
    
    def clear_glossary_results(self):
        """Очищает таблицу с результатами глоссария."""
        with self._get_write_conn() as conn:
            conn.execute("DELETE FROM glossary_results")
    
    # --- Сбор всех настроек UI ---
    def _get_full_ui_settings(self):
        """Собирает полный 'слепок' настроек из всех виджетов этого диалога."""
        settings = self._get_common_settings()
        
        # Добавляем специфичные для UI настройки
        settings['is_sequential'] = self.sequential_mode_checkbox.isChecked()
        settings['merge_mode'] = self.get_merge_mode()
        settings.update(self.translation_options_widget.get_settings())
        
        # Удаляем "тяжелые" данные, которые не являются настройками
        settings.pop('full_glossary_data', None)
        settings.pop('initial_glossary_list', None)
        settings.pop('file_path', None)
        
        return settings

    # --- Применение всех настроек к UI ---
    def _apply_full_ui_settings(self, settings: dict):
        """Применяет полный 'слепок' настроек ко всем виджетам."""
        if not settings: return
        
        # Блокируем сигналы, чтобы избежать каскадных обновлений
        # ... (здесь можно добавить блокировку сигналов для всех виджетов ??? TODO) ...

        # Восстанавливаем ключи (провайдер и активные)
        self.key_widget.set_active_keys_for_provider(
            settings.get('provider'), 
            settings.get('api_keys', [])
        )

        # Восстанавливаем настройки модели
        self.model_settings_widget.set_settings(settings)

        # Восстанавливаем промпт
        self.prompt_widget.set_prompt(settings.get('custom_prompt', ''))

        # Восстанавливаем опции трансляции (размер пакета и т.д.)
        self.translation_options_widget.set_settings(settings)
        
        # Восстанавливаем режимы
        is_sequential = settings.get('is_sequential', False) # <-- Сохраняем значение
        self.sequential_mode_checkbox.setChecked(is_sequential)

        self.send_notes_checkbox.setChecked(settings.get('send_notes_in_sequence', True))

        merge_mode = settings.get('merge_mode', 'supplement')
        if merge_mode == 'update': self.ai_mode_update_radio.setChecked(True)
        elif merge_mode == 'accumulate': self.ai_mode_accumulate_radio.setChecked(True)
        else: self.ai_mode_supplement_radio.setChecked(True)
        
        # --- Вызываем чистый метод для обновления UI ---
        self._update_sequential_mode_widgets(is_sequential)

    
    
    
    def _update_dependent_widgets(self):
        """
        Централизованно обновляет виджеты, зависящие от списка ГЛАВ,
        такие как CJK-опции.
        """
        if not self.html_files:
            self.model_settings_widget.update_cjk_options_availability(enabled=False)
            return
            
        is_any_cjk = False
        try:
            with zipfile.ZipFile(open(self.epub_path, 'rb'), 'r') as zf:
                # Проверяем до 3 глав из списка self.html_files
                for chapter_path in self.html_files[:3]:
                    content = zf.read(chapter_path).decode('utf-8', 'ignore')
                    if LanguageDetector.is_cjk_text(content):
                        is_any_cjk = True
                        break
            self.model_settings_widget.update_cjk_options_availability(enabled=True, is_cjk_recommended=is_any_cjk)
        except Exception as e:
            print(f"[WARN] Не удалось определить CJK для генерации глоссария: {e}")
            self.model_settings_widget.update_cjk_options_availability(enabled=True, error=True)

    def _reselect_chapters_for_glossary(self):
        """Открывает диалог выбора глав для генерации глоссария."""
        if not self.epub_path:
            QMessageBox.warning(self, "Ошибка", "Исходный EPUB файл не определен.")
            return

        from ..epub import EpubHtmlSelectorDialog
        success, selected_files = EpubHtmlSelectorDialog.get_selection(
            parent=self,
            epub_filename=self.epub_path,
            pre_selected_chapters=self.html_files,
            project_manager=self.project_manager
        )

        if success:
            self.html_files = selected_files
            self.reselect_chapters_btn.setText(f"Главы: {len(self.html_files)}")
            self._rebuild_glossary_tasks()
            self._update_dependent_widgets()
            

    def _emit_task_manipulation_signal(self, action: str, task_ids: list):
        """
        Общий метод для ЗАПУСКА фоновых команд в TaskManager.
        Использует QThread для предотвращения зависания UI.
        """
        if not (self.engine and self.task_manager):
            return

        target_method = None
        args = []

        if action in ['top', 'bottom', 'up', 'down']:
            target_method = self.task_manager.reorder_tasks
            args = [action, task_ids]
        elif action == 'remove':
            target_method = self.task_manager.remove_tasks
            args = [task_ids]
        elif action == 'duplicate':
            target_method = self.task_manager.duplicate_tasks
            args = [task_ids]

        if not target_method:
            return

        # Блокируем UI на время операции
        self.chapter_list_widget.setEnabled(False)
        self.rebuild_tasks_btn.setEnabled(False)

        # Создаем и запускаем "грузчика"
        self.db_worker = TaskDBWorker(target_method, *args)
        
        # После завершения - разблокируем
        self.db_worker.finished.connect(self._on_db_worker_finished)
        self.db_worker.start()

    def _on_db_worker_finished(self):
        """Слот, который вызывается по завершении фоновой DB-задачи."""
        self.chapter_list_widget.setEnabled(True)
        self.rebuild_tasks_btn.setEnabled(True)
        # TaskManager сам отправит сигнал _notify_ui_of_change,
        # который будет пойман в _on_global_event и вызовет перерисовку.

    @pyqtSlot(str, list)
    def _handle_task_reorder(self, action: str, task_ids: list):
        self._emit_task_manipulation_signal(action, task_ids)
    
    @pyqtSlot(list)
    def _handle_task_duplication(self, task_ids: list):
        self._emit_task_manipulation_signal('duplicate', task_ids)
    
    @pyqtSlot(list)
    def _handle_task_removal(self, task_ids: list):
        self._emit_task_manipulation_signal('remove', task_ids)

    # Также нужно обновить _handle_task_reanimation
    def _handle_task_reanimation(self, task_ids: list):
        if self.engine and self.task_manager:
            self.chapter_list_widget.setEnabled(False)
            self.rebuild_tasks_btn.setEnabled(False)
            
            self.db_worker = TaskDBWorker(self.task_manager.reanimate_tasks, task_ids)
            self.db_worker.finished.connect(self._on_db_worker_finished)
            self.db_worker.start()

    
    def _remove_generated_chapters(self):
        """Убирает из текущего списка глав те, что есть в базе данных."""
        processed_chapters = self._get_all_processed_chapters()
        
        if not processed_chapters:
            QMessageBox.information(self, "Нечего убирать", "Список сгенерированных глав в базе данных пуст.")
            return
    
        initial_count = len(self.html_files)
        # Находим только те главы из текущего списка, которые есть в истории
        chapters_to_remove = [ch for ch in self.html_files if ch in processed_chapters]
        removed_count = len(chapters_to_remove)

        if removed_count == 0:
            QMessageBox.information(self, "Нет совпадений", "В текущем списке нет глав, для которых глоссарий был сгенерирован ранее.")
            return

        # --- НАЧАЛО НОВОЙ ЛОГИКИ С ПОДТВЕРЖДЕНИЕМ ---
        msg_box = QtWidgets.QMessageBox(self)
        msg_box.setWindowTitle("Подтверждение фильтрации")
        msg_box.setText(f"Будет убрано {removed_count} глав из текущего списка задач, так как для них уже есть сгенерированный глоссарий.")
        msg_box.setInformativeText("Вы уверены, что хотите продолжить?")
        msg_box.setIcon(QtWidgets.QMessageBox.Icon.Question)
        yes_button = msg_box.addButton("Да, убрать", QtWidgets.QMessageBox.ButtonRole.YesRole)
        no_button = msg_box.addButton("Нет", QtWidgets.QMessageBox.ButtonRole.NoRole)
        msg_box.exec()

        if msg_box.clickedButton() != yes_button:
            return # Пользователь отменил
        # --- КОНЕЦ НОВОЙ ЛОГИКИ ---

        self.html_files = [ch for ch in self.html_files if ch not in chapters_to_remove]
        final_count = len(self.html_files)
        
        QMessageBox.information(self, "Главы убраны", f"Убрано {removed_count} глав.\nОсталось: {final_count} глав.\n\nТеперь нажмите 'Применить и пересобрать', чтобы обновить список задач.")
        self.reselect_chapters_btn.setText(f"Главы: {len(self.html_files)}")
        self._rebuild_glossary_tasks()
    
    def _get_all_processed_chapters(self) -> set:
        """
        Получает ПОЛНЫЙ список обработанных глав, объединяя:
        1. Долговременную память (файл проекта).
        2. Накопленную память сессии (self._accumulated_processed_chapters).
        3. Текущую БД (self.task_manager).
        Автоматически обновляет накопитель данными из БД.
        """
        # 1. Загружаем постоянную историю из файла проекта
        persistent_chapters = set()
        if self.project_manager:
            persistent_chapters = self.project_manager.load_glossary_generation_map()

        # 2. Загружаем временный прогресс из БД
        db_chapters = set()
        if self.engine and self.task_manager:
            try:
                with self.task_manager._get_read_only_conn() as conn:
                    # Проверяем наличие таблицы перед запросом, чтобы избежать ошибок при инициализации
                    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='glossary_results'")
                    if cursor.fetchone():
                        cursor = conn.execute("SELECT chapters_json FROM glossary_results")
                        for row in cursor.fetchall():
                            if row['chapters_json'] and row['chapters_json'] != '[]':
                                db_chapters.update(json.loads(row['chapters_json']))
            except Exception as e:
                print(f"[UI ERROR] Не удалось прочитать карту глав из БД: {e}")
        
        # 3. Сливаем данные из БД в наш накопитель в памяти, 
        # чтобы они не пропали при очистке БД (например, при перезапуске сессии)
        self._accumulated_processed_chapters.update(db_chapters)
        
        # 4. Возвращаем объединение всех источников
        return persistent_chapters.union(self._accumulated_processed_chapters)
    
    def _rebuild_glossary_tasks(self):
        """
        Пересобирает задачи для глоссария, наполняет центральный ChapterQueueManager
        и инициирует обновление UI.
        """
        self._update_new_terms_limit_from_current_size()
        from gemini_translator.utils.glossary_tools import TaskPreparer
        import uuid
        if not self.task_manager: return

        if not self.html_files or not self.epub_path:
            self.task_manager.clear_all_queues()
            return

        settings = self.translation_options_widget.get_settings()
        settings['file_path'] = self.epub_path
        
        real_chapter_sizes = {}
        try:
            with zipfile.ZipFile(open(self.epub_path, 'rb'), 'r') as zf:
                for chapter in self.html_files:
                    real_chapter_sizes[chapter] = len(zf.read(chapter).decode('utf-8', 'ignore'))
        except Exception as e:
            QMessageBox.critical(self, "Ошибка чтения файла", f"Не удалось прочитать главы из EPUB: {e}")
            return
    
        preparer = TaskPreparer(settings, real_chapter_sizes)
        epub_tasks = preparer.prepare_tasks(self.html_files)

        tasks_for_core_engine = []
        context_glossary_for_payload = {}
        for task_payload in epub_tasks:
            task_type = task_payload[0]
            if task_type == 'epub':
                _, epub_path, chapter = task_payload
                payload_for_glossary = ('glossary_batch_task', epub_path, (chapter,), context_glossary_for_payload)
            elif task_type == 'epub_batch':
                _, epub_path, chapters = task_payload
                payload_for_glossary = ('glossary_batch_task', epub_path, chapters, context_glossary_for_payload)
            else:
                continue
            
            tasks_for_core_engine.append(payload_for_glossary)
        
        # --- НАЧАЛО ИЗМЕНЕНИЯ: Установка флага ---
        self._is_rebuilding = True
        try:
            self.task_manager.set_pending_tasks(tasks_for_core_engine)
        finally:
            self._is_rebuilding = False
        # --- КОНЕЦ ИЗМЕНЕНИЯ ---

    
    def _create_settings_tab(self):
        """Создает вкладку с основными настройками."""
        settings_tab = QWidget()
        settings_layout = QVBoxLayout(settings_tab)
        
        # --- ОБЪЕДИНЕННЫЙ БЛОК 1: Основные настройки (Остается без изменений) ---
        main_settings_group = QGroupBox("Основные настройки")
        main_settings_layout = QHBoxLayout(main_settings_group)

        # 1. Элемент слева: Режим обработки
        self.sequential_mode_checkbox = QCheckBox("Последовательный (контекст нарастает)")
        self.sequential_mode_checkbox.setChecked(False)
        self.sequential_mode_checkbox.setToolTip(
            "Включено: Задачи выполняются одна за другой, каждая следующая использует\n"
            "результаты предыдущих для лучшей консистентности.\n\n"
            "Выключено (Параллельный режим): Все задачи выполняются одновременно для максимальной скорости."
        )
        main_settings_layout.addWidget(self.sequential_mode_checkbox)

        # Распорка
        main_settings_layout.addStretch(1)

        # 2. Элемент по центру: Настройки контекста
        self.send_notes_checkbox = QCheckBox("Включать примечания в контекст")
        self.send_notes_checkbox.setToolTip(
            "При внедрении контекста, отправлять также и примечания для лучшего результата."
        )
        self.send_notes_checkbox.setChecked(True)
        main_settings_layout.addWidget(self.send_notes_checkbox)

        # Распорка
        main_settings_layout.addStretch(1)

        # 3. Элемент справа: Количество обработчиков
        self.distribution_group = QWidget()
        dist_layout = QHBoxLayout(self.distribution_group)
        dist_layout.setContentsMargins(0, 0, 0, 0)
        dist_layout.addWidget(QLabel("Обработчиков:  "))
        self.instances_spin = NoScrollSpinBox()
        self.instances_spin.setRange(1, 1)
        self.instances_spin.setToolTip(
            "Количество параллельных обработчиков.\n"
            "Этот параметр активен только в 'Параллельном режиме'."
        )
        dist_layout.addWidget(self.instances_spin)
        main_settings_layout.addWidget(self.distribution_group)
        
        settings_layout.addWidget(main_settings_group)

        # --- БЛОК 2: Настройки API и Модели ---
        api_group = QGroupBox("2. Настройки API и Модели")
        api_layout = QVBoxLayout(api_group)
        
        self.key_widget = KeyManagementWidget(self.settings_manager, self)
        self.model_settings_widget = ModelSettingsWidget(self)
        
        # --- ЧИСТКА ИНТЕРФЕЙСА ---
        # 1. Скрываем ненужные группы
        for group_name in ["cjk_group_box", "glossary_group_box"]:
            group = self.model_settings_widget.findChild(QtWidgets.QGroupBox, group_name)
            if group: group.setVisible(False)
            
        # 2. Отключаем флажки логики
        self.model_settings_widget.dynamic_glossary_checkbox.setChecked(False)
        self.model_settings_widget.use_jieba_glossary_checkbox.setChecked(False)
        self.model_settings_widget.segment_text_checkbox.setChecked(False)
        
        # --- ИНЪЕКЦИЯ: Вставляем "Режим слияния" в правую колонку ModelSettingsWidget ---
        right_column = self.model_settings_widget.findChild(QWidget, "right_column_widget")
        if right_column and right_column.layout():
            # Создаем группу слияния
            merge_mode_group = QGroupBox("3. Режим слияния результатов")
            merge_mode_layout = QVBoxLayout(merge_mode_group) # Используем Vertical для компактности в колонке
            
            self.ai_mode_update_radio = QtWidgets.QRadioButton("Обновить (перезапись)")
            self.ai_mode_update_radio.setToolTip("Если термин уже есть, он будет ОБНОВЛЕН.")
            
            self.ai_mode_supplement_radio = QtWidgets.QRadioButton("Дополнить (только новые)")
            self.ai_mode_supplement_radio.setToolTip("Добавляются ТОЛЬКО термины, которых еще нет.")
            self.ai_mode_supplement_radio.setChecked(True)
            
            self.ai_mode_accumulate_radio = QtWidgets.QRadioButton("Накопить (все подряд)")
            self.ai_mode_accumulate_radio.setToolTip("Добавляются ВСЕ термины, создавая дубликаты.")
            
            merge_mode_layout.addWidget(self.ai_mode_supplement_radio)
            merge_mode_layout.addWidget(self.ai_mode_update_radio)
            merge_mode_layout.addWidget(self.ai_mode_accumulate_radio)
            
            # Добавляем группу в конец правой колонки (под "Прочие опции")
            right_column.layout().addWidget(merge_mode_group)
            
            # Добавляем распорку в конец, чтобы поджать все вверх
            right_column.layout().addStretch(1)

        api_layout.addWidget(self.key_widget)
        api_layout.addWidget(self.model_settings_widget)
        
        settings_layout.addWidget(api_group)
        settings_layout.addStretch(1)

        # --- ПОДКЛЮЧЕНИЕ СИГНАЛОВ ---
        self.key_widget.active_keys_changed.connect(
            lambda: self.instances_spin.setMaximum(len(self.key_widget.get_active_keys()) or 1)
        )
        self.sequential_mode_checkbox.toggled.connect(
            lambda checked: self.model_settings_widget.set_concurrent_requests_visible(not checked)
        )
        self.sequential_mode_checkbox.toggled.connect(self._on_mode_changed)

        return settings_tab


    def _create_results_tab(self):
        """Создает вкладку с результатами и логом внутри сплиттера."""
        results_tab = QWidget()
        results_layout = QVBoxLayout(results_tab)
        splitter = QtWidgets.QSplitter(Qt.Orientation.Vertical)
        
        self.glossary_widget = GlossaryWidget(self, self.settings_manager)
        self.glossary_widget.set_simplified_mode()
        
        # --- СКВОЗНАЯ ПЕРЕДАЧА ПУТИ ---
        # Передаем путь к файлу, чтобы инструменты очистки могли сверяться с оригиналом
        if self.epub_path:
            self.glossary_widget.set_epub_path(self.epub_path)
        # ------------------------------
        
        # Включаем кнопку пост-обработки
        self.glossary_widget.set_cleanup_button_visible(True) 
        
        self.glossary_widget.glossary_changed.connect(self._on_glossary_manually_changed)
        
        log_group = QGroupBox("Лог выполнения")
        log_layout = QVBoxLayout(log_group)
        self.log_widget = LogWidget(self)
        log_layout.addWidget(self.log_widget)
        
        splitter.addWidget(self.glossary_widget)
        splitter.addWidget(log_group)
        splitter.setSizes([500, 300])
        results_layout.addWidget(splitter)
        
        return results_tab


    def _create_prompt_tab(self):
        """Создает вкладку с редактором промпта."""
        self.prompt_widget = PresetWidget(
            parent=self,
            preset_name="Промпт глоссария",
            default_prompt_func=api_config.default_glossary_prompt,
            load_presets_func=self.settings_manager.load_glossary_prompts,
            save_presets_func=self.settings_manager.save_glossary_prompts,
            get_last_text_func=self.settings_manager.get_last_glossary_prompt_text,
            get_last_preset_func=self.settings_manager.get_last_glossary_prompt_preset_name,
            save_last_preset_func=self.settings_manager.save_last_glossary_prompt_preset_name
        )
        self.prompt_widget.load_last_session_state()
        return self.prompt_widget

    def _load_data(self):
        self.key_widget.provider_combo.currentIndexChanged.emit(0)
        self._update_start_button_state()

    def _update_start_button_state(self):
        """
        Обновляет доступность кнопок 'Начать' и видимость 'Применить'.
        Кнопка 'Применить' видна ВСЕГДА, когда есть данные и не идет сессия.
        """
        # 1. Логика кнопки "Начать"
        if self.is_session_active:
            self.start_btn.setEnabled(False)
        else:
            num_active_keys = len(self.key_widget.get_active_keys())
            can_start = all([
                self.epub_path,
                self.html_files, 
                num_active_keys > 0
            ])
            self.start_btn.setEnabled(can_start)

        # 2. Логика кнопки "Применить"
        # Видна, если сессия НЕ активна И в глоссарии есть хотя бы одна запись
        has_glossary_items = len(self.glossary_widget.get_glossary()) > 0
        self.apply_btn.setVisible(not self.is_session_active and has_glossary_items)
    
    @pyqtSlot()
    def _on_glossary_manually_changed(self):
        """
        Слот, который реагирует на ручные правки и запускает таймер
        отложенного автосохранения.
        """
        if self.project_manager and self.project_manager.project_folder:
            self.autosave_timer.start()
    
    def _trigger_autosave(self):
        """Безопасно инициирует сохранение в файл восстановления с ротацией."""
        self._perform_safe_recovery_save()
            
    @pyqtSlot(bool)
    def _on_mode_changed(self):
        """
        Слот, обрабатывающий ИЗМЕНЕНИЕ режима пользователем.
        Обновляет UI и пересобирает задачи.
        """
        is_sequential = self.sequential_mode_checkbox.isChecked()

        # 1. Блокируем сигналы, чтобы избежать рекурсивных вызовов
        self.sequential_mode_checkbox.blockSignals(True)
        self.send_notes_checkbox.blockSignals(True)

        # 2. Вызываем чистый метод для обновления UI
        self._update_sequential_mode_widgets(is_sequential)
        
        # 3. Разблокируем сигналы
        self.sequential_mode_checkbox.blockSignals(False)
        self.send_notes_checkbox.blockSignals(False)
        
        # 4. Пересобираем задачи, так как режим изменился (это побочный эффект)
        self._rebuild_glossary_tasks()

    def _update_sequential_mode_widgets(self, is_sequential: bool):
        """
        Обновляет только видимость и доступность виджетов, зависящих
        от последовательного режима. Не вызывает побочных эффектов.
        """
        # Управляем видимостью группы с выбором количества клиентов
        self.distribution_group.setVisible(not is_sequential)

        # Чекбокс "Примечаний" всегда доступен в этом диалоге
        self.send_notes_checkbox.setEnabled(True)

    @pyqtSlot(dict)
    def _on_global_event(self, event: dict):
        """Обрабатывает глобальные события, делегируя их нужным компонентам."""

        event_name, data = event.get('event'), event.get('data', {})

        # --- ДОБАВЛЕНО: Реагируем на смену модели ---
        if event_name == 'model_changed':
             self._calculate_optimal_batch_size() # Для вкладки задач
             return
        
        if event_name == 'session_started':
            self._set_ui_active(True)
        elif event_name == 'session_finished':
            self._shutdown_reason = data.get('reason')
            self._log_session_id = data.get('session_id_log')
            QtCore.QMetaObject.invokeMethod(self, "_on_session_finished", QtCore.Qt.ConnectionType.QueuedConnection)
        elif event_name in ['task_finished', 'task_state_changed', 'generation_state_updated']:
            # --- Проверка флага ---
            if hasattr(self, '_is_rebuilding') and self._is_rebuilding:
                self._redraw_task_list_and_update_map()
                return
            
            # Обновляем прогресс задач всегда
            self._redraw_task_list_and_update_map()
            
            # ВАЖНОЕ ИЗМЕНЕНИЕ: Обновляем глоссарий из БД ТОЛЬКО если сессия активна.
            # Если сессия остановлена, пользователь может править таблицу вручную,
            # и мы не должны перезаписывать его правки устаревшими данными из БД.
            if self.is_session_active:
                self._refresh_glossary_from_db()

            # Автосохранение
            if self.is_session_active:
                self._perform_safe_recovery_save()
    
    def _on_start_stop_clicked(self):
        """Обрабатывает только нажатие на кнопку 'Начать'."""
        if self.engine and self.engine.session_id:
            QMessageBox.warning(self, "Движок занят", "Другая операция уже выполняется. Пожалуйста, дождитесь ее завершения.")
            return
        
        
        can_start = len(self.key_widget.get_active_keys()) > 0
        if not can_start:
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Нет Ключей")
            msg_box.setText("Запуск невозможен, так как вы не выбрали ни одного ключа.")
            yes_btn = msg_box.addButton("Понял", QMessageBox.ButtonRole.YesRole)
            no_btn = msg_box.addButton("Осознал", QMessageBox.ButtonRole.NoRole)
            msg_box.exec()
            return
        
        self._start_session()

    def _on_soft_stop_clicked(self):
        """Инициирует ПЛАВНУЮ остановку через оркестратор."""
        if self.orchestrator and self.orchestrator._is_running:
            self.is_soft_stopping = True
            self.soft_stop_btn.setText("Завершение...")
            self.soft_stop_btn.setEnabled(False)
            self.hard_stop_btn.setEnabled(False) 
            # Просто говорим оркестратору начать процедуру плавной остановки
            self.orchestrator.stop()

    def _on_hard_stop_clicked(self):
        """Инициирует ЭКСТРЕННУЮ, немедленную остановку."""
        if self.engine and self.engine.session_id:
            self.hard_stop_btn.setText("Прерывание...")
            self.hard_stop_btn.setEnabled(False)
            self.soft_stop_btn.setEnabled(False)
            
            # --- ЛОГИКА ЭКСТРЕННОЙ ОСТАНОВКИ ---
            # 1. Находим флаг нашего оркестратора и немедленно его снимаем
            orchestrator_flag_key = self.orchestrator.MANAGED_SESSION_FLAG_KEY if self.orchestrator else None
            if orchestrator_flag_key and self.bus.pop_data(orchestrator_flag_key, None):
                 self._post_event('log_message', {'message': "[SYSTEM] Глобальный флаг управляемой сессии снят принудительно."})

            # 2. Отправляем команду на немедленную остановку движка
            self._post_event('log_message', {'message': "[SYSTEM] Отправка запроса на ЭКСТРЕННУЮ остановку сессии…"})
            self._post_event('manual_stop_requested')

    

    def _start_session(self):
        """Запускает сессию генерации, предварительно очистив старые результаты в БД."""
        if self.engine and self.engine.session_id:
            QMessageBox.warning(self, "Движок занят", "Другая операция уже выполняется. Пожалуйста, дождитесь ее завершения.")
            return

        if not self.task_manager.has_pending_tasks():
            QMessageBox.warning(self, "Нет задач", "Список задач для генерации пуст. Пожалуйста, соберите задачи.")
            return
            
        if not self.epub_path or not os.path.exists(self.epub_path):
            QMessageBox.critical(self, "Критическая ошибка: Файл не найден", f"Не удалось найти исходный EPUB файл: {self.epub_path}")
            return
        
        # Гарантируем, что последнее редактирование сохранено
        self.glossary_widget.commit_active_editor()
        
        self._get_all_processed_chapters()
        # Очищаем таблицу глоссария в БД и добавляем начальные данные (включая ручные правки)
        initial_glossary_for_session = self.glossary_widget.get_glossary()
        if self.task_manager:
            self.task_manager.clear_glossary_results()
            if initial_glossary_for_session:
                data_to_insert = [
                    ('initial', 0, '[]', item.get('original'), item.get('rus'), item.get('note'))
                    for item in initial_glossary_for_session
                ]
                with self.task_manager._get_write_conn() as conn:
                    conn.executemany("INSERT INTO glossary_results (task_id, timestamp, chapters_json, original, rus, note) VALUES (?, ?, ?, ?, ?, ?)", data_to_insert)

        self.tabs.setCurrentWidget(self.tabs.widget(3))
        self.log_widget.clear()
        self.force_exit_on_interrupt = False
        self._set_ui_active(True)
        self.settings_manager.save_last_glossary_prompt_text(self.prompt_widget.get_prompt())
        self.settings_manager.save_last_glossary_prompt_preset_name(self.prompt_widget.get_current_preset_name())
        
        settings = self._get_common_settings()
        
        if self.sequential_mode_checkbox.isChecked():
            self.orchestrator = SequentialTaskProvider(
                self._get_common_settings, self,
                event_bus=self.bus, translate_engine=self.engine
            )
            self.orchestrator.start()
        else:
            settings['num_instances'] = self.instances_spin.value()
            settings['glossary_merge_mode'] = self.get_merge_mode()
            self._post_event('start_session_requested', {'settings': settings})
    
    def _refresh_glossary_from_db(self):
        """
        Читает термины из БД через умный SQL-запрос TaskManager'а и обновляет виджет.
        Автоматически применяет дедупликацию (First/Last write wins) и фоновую очистку.
        """
        try:
            # 1. Получаем режим слияния из UI или настроек
            current_mode = self.get_merge_mode()
            
            # 2. Делегируем всю работу TaskManager'у.
            # Он выполнит SQL-запрос с оконными функциями, вернет чистый список
            # и попутно удалит мусор из БД, если его > 30%.
            clean_terms = self.task_manager.fetch_and_clean_glossary(mode=current_mode, return_raw=True)
            
            # 3. Обновляем виджет
            if clean_terms:
                self.glossary_widget.set_glossary(clean_terms)
            
        except Exception as e:
            # Если есть метод логирования, используем его, иначе print
            error_msg = f"[UI ERROR] Ошибка обновления глоссария из БД: {e}"
            if hasattr(self, '_post_event'):
                self._post_event('log_message', {'message': error_msg})
            else:
                print(error_msg)
    
    # --- НОВЫЙ МЕТОД: Расчет размера пакета ---
    def _calculate_optimal_batch_size(self):
        """
        Предлагает оптимальный размер пакета на основе модели.
        При изменении значения в task_size_spin сработает цепочка обновлений для лимита.
        """
        if not hasattr(self, 'model_settings_widget') or not hasattr(self, 'translation_options_widget'):
            return

        settings = self.model_settings_widget.get_settings()
        model_name = settings.get('model')
        model_config = api_config.all_models().get(model_name, {})
        context_limit_tokens = model_config.get("context_length", 128000)

        chars_per_token = api_config.CHARS_PER_ASCII_TOKEN 
        if self.html_files and self.epub_path:
            try:
                with zipfile.ZipFile(open(self.epub_path, 'rb'), 'r') as zf:
                    content_sample = zf.read(self.html_files[0]).decode('utf-8', 'ignore')[:2000]
                    if LanguageDetector.is_cjk_text(content_sample):
                        chars_per_token = 1.5 
                    elif re.search(r'[а-яА-ЯёЁ]', content_sample):
                        chars_per_token = api_config.CHARS_PER_CYRILLIC_TOKEN 
            except Exception: pass

        target_budget_tokens = context_limit_tokens * 0.20
        recommended_chars = int(target_budget_tokens * chars_per_token)

        spin = self.translation_options_widget.task_size_spin
        final_val = max(5000, min(recommended_chars, spin.maximum()))
        
        # Установка вызовет сигнал valueChanged, который запустит _update_new_terms_limit_from_current_size
        spin.setValue(final_val)
        self._update_new_terms_limit_from_current_size()
        self.translation_options_widget.info_label.setText(
            f"Авто-размер: {final_val:,} симв.\n(~20% контекста {model_name})"
        )
        
    def _redraw_task_list_and_update_map(self):
        """Перерисовывает список задач и обновляет карту сгенерированных глав ИЗ БАЗЫ ДАННЫХ."""
        if not (self.engine and self.task_manager): return

        processed_chapters = self._get_all_processed_chapters()

        self.remove_generated_btn.setEnabled(bool(processed_chapters))
        
        ui_state_list = self.task_manager.get_ui_state_list()
        self.chapter_list_widget.update_list(ui_state_list)
        
        table = self.chapter_list_widget.table
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if not item: continue
            task_tuple = item.data(QtCore.Qt.ItemDataRole.UserRole)
            if not task_tuple: continue

            task_id, payload = task_tuple
            chapters_in_task = payload[2] if len(payload) > 2 else ()
            if isinstance(chapters_in_task, str): chapters_in_task = (chapters_in_task,)
            
            # Проверяем, все ли главы в этой задаче уже были обработаны
            if chapters_in_task and all(ch in processed_chapters for ch in chapters_in_task):
                # Если да, ДОБАВЛЯЕМ маркер к тексту, не меняя статус и цвет
                current_text = item.text()
                if "(Сгенерировано)" not in current_text:
                    item.setText(f"{current_text} (Сгенерировано)")

    
    def get_merge_mode(self):
        if self.ai_mode_accumulate_radio.isChecked(): return 'accumulate'
        if self.ai_mode_update_radio.isChecked(): return 'update'
        return 'supplement'

    def _get_common_settings(self):
        settings = self.model_settings_widget.get_settings()
        settings['provider'] = self.key_widget.get_selected_provider()
        settings['api_keys'] = self.key_widget.get_active_keys()
        model_name = settings.get('model')
        settings['model_config'] = api_config.all_models().get(model_name, {}).copy()
        
        settings['initial_glossary_list'] = self.glossary_widget.get_glossary()
        
        settings['file_path'] = self.epub_path
        settings['glossary_generation_prompt'] = self.prompt_widget.get_prompt() or api_config.default_glossary_prompt()
        settings['custom_prompt'] = api_config.default_prompt()
        settings['glossary_merge_mode'] = self.get_merge_mode()
        settings['send_notes_in_sequence'] = self.send_notes_checkbox.isChecked()
        
        # Передаем лимит новых терминов
        if hasattr(self, 'new_terms_limit_spin'):
            settings['new_terms_limit'] = self.new_terms_limit_spin.value()
        else:
            settings['new_terms_limit'] = 50 # Fallback
            
        return settings


    @pyqtSlot(list)
    def _on_engine_state_update(self, current_glossary_state):
        self.glossary_widget.set_glossary(current_glossary_state)
        self._perform_safe_recovery_save()
    
    @pyqtSlot()
    def _on_session_finished(self):
        """
        Финальная процедура. Обрабатывает результаты и чистит UI.
        """
        self._refresh_glossary_from_db()
        
        if self.engine and self.task_manager:
            self.engine.task_manager.release_held_tasks()
        
        self._post_event('log_message', {'message': "[SYSTEM] Получен сигнал завершения движка. Очистка интерфейса…"})
        self.key_widget._load_and_refresh_keys()
        
        # Возвращаем UI в "пассивное" состояние.
        # Это также обновит видимость кнопки "Применить" через _update_start_button_state
        self._set_ui_active(False)

        try:
            current_ui_model_name = self.model_settings_widget.model_combo.currentText()
            model_config = api_config.all_models().get(current_ui_model_name, {})
            model_id_to_sync = model_config.get('id')
            if model_id_to_sync:
                self.key_widget.set_current_model(model_id_to_sync)
        except Exception as e:
            print(f"[ERROR] Не удалось синхронизировать виджет ключей после сессии: {e}")
        
        QtCore.QMetaObject.invokeMethod(self, "_finalize_session_state", QtCore.Qt.ConnectionType.QueuedConnection)
    
        
    @pyqtSlot(dict)
    def _on_generation_finished(self, data: dict):
        """
        Обрабатывает РЕЗУЛЬТАТ от оркестратора.
        Содержит всю специфическую логику этого диалога.
        """
        final_glossary_from_engine = data.get('glossary')
        was_cancelled = data.get('was_cancelled', False)

        # Оркестратор больше не нужен, он свою работу сделал
        if self.orchestrator:
            self.orchestrator.setParent(None)
            self.orchestrator.deleteLater()
            self.orchestrator = None
        
        # --- Сценарий 1: Успешное штатное завершение ---
        if not was_cancelled:
            self.final_glossary = final_glossary_from_engine
            
            # 1. Сначала обновляем данные в виджете (так как _perform_safe_recovery_save берет данные оттуда)
            self.glossary_widget.set_glossary(self.final_glossary)
            
            # 2. Вместо удаления — делаем ФИНАЛЬНЫЙ СНАПШОТ.
            # Теперь, если пока пользователь пьет чай и смотрит на результаты, вырубится свет,
            # при следующем запуске он увидит полностью готовый результат.
            # Удаление произойдет только в методе accept() (кнопка "Применить").
            self._perform_safe_recovery_save()
            
            return
    
        # --- Сценарий 2: Прерывание сессии ---
        # Пытаемся найти хоть какой-то файл восстановления
        candidates = self._get_recovery_candidates()
        
        if candidates:
            # Берем самый свежий
            best_candidate_path = candidates[0][1]
            try:
                with open(best_candidate_path, 'r', encoding='utf-8') as f:
                    recovery_data = json.load(f)
                
                recovered_glossary = recovery_data.get("progress", {}).get("glossary", [])
                recovered_chapters = set(recovery_data.get("progress", {}).get("processed_chapters", []))
                
                # Здесь мы файлы НЕ удаляем, вдруг пользователь нажмет "Нет, отбросить" в диалоге ниже,
                # а потом передумает и перезапустит программу. Пусть файлы живут до явного решения.

                if recovered_glossary:
                    msg_box = QMessageBox(self)
                    msg_box.setWindowTitle("Процесс прерван")
                    msg_box.setText(f"Удалось сохранить {len(recovered_glossary)} терминов и прогресс по {len(recovered_chapters)} главам.")
                    msg_box.setInformativeText("Хотите применить эти промежуточные результаты?")
                    yes_btn = msg_box.addButton("Да, применить", QMessageBox.ButtonRole.YesRole)
                    no_btn = msg_box.addButton("Нет, отбросить", QMessageBox.ButtonRole.NoRole)
                    msg_box.exec()
                    
                    if msg_box.clickedButton() == yes_btn:
                        self.final_glossary = recovered_glossary
                        self.glossary_widget.set_glossary(self.final_glossary)
                        # Обновляем UI и делаем сейв текущего состояния
                        self._redraw_task_list_and_update_map()
                        self._perform_safe_recovery_save()
            except Exception as e:
                QMessageBox.warning(self, "Ошибка восстановления", f"Процесс был прерван, но не удалось прочитать файл восстановления: {e}")
        
        elif was_cancelled:
             QMessageBox.warning(self, "Прервано", "Процесс генерации был прерван. Промежуточные данные не применены.")

        # Если диалог должен был закрыться, но сессия прервана,
        # вызываем accept(), чтобы передать то, что успели накопить.
        if self.force_exit_on_interrupt:
            self.accept()
    
    def _create_recovery_snapshot(self):
        """Собирает все данные для сохранения в файл восстановления."""
        # Теперь мы просто берем полный глоссарий из виджета
        current_glossary = self.glossary_widget.get_glossary()
        processed_chapters = self._get_all_processed_chapters()
        current_ui_settings = self._get_full_ui_settings()
        return {"progress": {"glossary": current_glossary, "processed_chapters": sorted(list(processed_chapters))}, "settings": current_ui_settings}
        
    @pyqtSlot()
    def _finalize_session_state(self):
        """Асинхронно сбрасывает флаг сессии и показывает финальное сообщение."""
        self.is_session_active = False
        self._post_event('log_message', {'message': "[SYSTEM] Интерфейс полностью разблокирован."})
        self._update_start_button_state()

        if hasattr(self, '_shutdown_reason') and hasattr(self, '_log_session_id'):
            session_id_log = self._log_session_id
            reason = self._shutdown_reason
            QTimer.singleShot(100, lambda: self.summary_sep_session(session_id_log=session_id_log, reason=reason))

        del self._shutdown_reason
        del self._log_session_id
            
    
    def summary_sep_session(self, session_id_log, reason):
        final_message_data = {
            'message': f"■■■ СЕССИЯ {session_id_log[:8]} ОСТАНОВЛЕНА. {reason} ■■■",
            'priority': 'final' # <-- Наш новый флаг!
        }
        self._post_event('log_message', {'message': "---SEPARATOR---"})
        self._post_event('log_message', final_message_data)
    
    
    def _update_filter_button_state(self):
        """Обновляет состояние кнопки 'Убрать сгенерированные'."""
        if hasattr(self, 'remove_generated_btn'):
            # Запрашиваем актуальное состояние из БД
            processed_chapters = self._get_all_processed_chapters()
            self.remove_generated_btn.setEnabled(bool(processed_chapters))
            
    def _set_ui_active(self, active: bool):
        """
        Управляет состоянием всего UI в зависимости от того, активна ли сессия.
        'active = True' означает, что процесс запущен.
        'active = False' означает, что процесс остановлен.
        """
        self.is_session_active = active
        
        # Блокируем/разблокируем основные виджеты настроек
        self.key_widget.setEnabled(not active)
        self.model_settings_widget.setEnabled(not active)
        self.prompt_widget.setEnabled(not active)
        self.translation_options_widget.setEnabled(not active)
        self.send_notes_checkbox.setEnabled(not active)
        self.sequential_mode_checkbox.setEnabled(not active)
        self.glossary_widget.set_controls_enabled(not active)
        self.chapter_list_widget.set_session_mode(active)
        
        # Переключаем видимость кнопок управления сессией
        self.start_btn.setVisible(not active)
        self.soft_stop_btn.setVisible(active)
        self.hard_stop_btn.setVisible(active)
        
        if active:
            # Сессия ЗАПУЩЕНА
            self.is_soft_stopping = False
            self.soft_stop_btn.setEnabled(True)
            self.soft_stop_btn.setText("Завершить плавно") 
            self.hard_stop_btn.setEnabled(True)
            self.hard_stop_btn.setText("❌ Прервать")
            self.close_btn.setText("Прервать и закрыть")
            # Кнопка "Применить" скроется вызовом _update_start_button_state ниже
        else:
            # Сессия ОСТАНОВЛЕНА
            self.close_btn.setText("Закрыть")
            
        # Обновляем состояние кнопок (Start и Apply)
        self._update_start_button_state()
            
    def _cleanup(self, keep_recovery_file=False):
        """Централизованный метод для всей очистки перед закрытием."""
        if self.bus:
            try:
                self.bus.event_posted.disconnect(self._on_global_event)
                print("[DEBUG] GenerationSessionDialog отписался от глобальной шины.")
            except (TypeError, RuntimeError):
                pass
        
        if self.orchestrator:
            self.orchestrator.deleteLater()
            self.orchestrator = None

        if not keep_recovery_file:
            self._cleanup_all_recovery_files()

    def accept(self):
        """Применяет результат и удаляет файлы восстановления."""
        print("[DEBUG] GenerationSessionDialog.accept() called.")
        
        if hasattr(self, 'glossary_widget'):
            self.glossary_widget.commit_active_editor()
        
        final_glossary = self.glossary_widget.get_glossary()
        processed_chapters = self._get_all_processed_chapters()
        
        self.generation_finished.emit(final_glossary, processed_chapters)
    
        self._cleanup()
        super().accept()

    def reject(self):
        """
        Обрабатывает кнопку 'Закрыть' и крестик.
        """
        print("[DEBUG] GenerationSessionDialog.reject() called.")
        
        is_running = (self.orchestrator and self.orchestrator._is_running) or (self.engine and self.engine.session_id)
        if is_running:
            self.force_exit_on_interrupt = True
            self._on_hard_stop_clicked()
            return 
        
        if self.apply_btn.isVisible():
            msg_box = QtWidgets.QMessageBox(self)
            msg_box.setWindowTitle("Несохраненные результаты")
            msg_box.setIcon(QtWidgets.QMessageBox.Icon.Question)
            msg_box.setText("Генерация завершена, но результаты не были применены.")
            msg_box.setInformativeText("Выберите действие:")
            
            save_recovery_btn = msg_box.addButton("Сохранить в резервный файл и выйти", QtWidgets.QMessageBox.ButtonRole.ActionRole)
            discard_btn = msg_box.addButton("Отбросить и выйти", QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
            cancel_btn = msg_box.addButton("Отмена", QtWidgets.QMessageBox.ButtonRole.RejectRole)
            msg_box.setDefaultButton(cancel_btn)
            
            msg_box.exec()
            clicked_button = msg_box.clickedButton()

            if clicked_button == cancel_btn:
                return 

            if clicked_button == save_recovery_btn:
                self._perform_safe_recovery_save()
                self._cleanup(keep_recovery_file=True)
                super().reject()
                return
            
        self._cleanup()
        super().reject()
    
    def showEvent(self, event):
        """Перехватывает событие первого показа окна и запускает отложенную загрузку."""
        super().showEvent(event)
        
        # Проверяем, не идет ли уже сессия (восстановление состояния)
        self._check_and_sync_active_session()

        if not self._initial_load_done:
            self._initial_load_done = True
            # Запускаем с задержкой, чтобы дать окну полностью отрисоваться
            QTimer.singleShot(50, self._deferred_initial_load)

    def _deferred_initial_load(self):
        """Выполняет все действия по заполнению UI после отрисовки."""
        self.reselect_chapters_btn.setText(f"Главы: {len(self.html_files)}")
        # 1. Считаем оптимальный размер пакета (это вызовет расчет лимита терминов через сигнал)
        self._calculate_optimal_batch_size()
        # 2. И только теперь строим задачи
        self._rebuild_glossary_tasks()
        # 3. Обновляем визуальные CJK опции
        self._update_dependent_widgets()
        
    def closeEvent(self, event):
        """Перехватываем событие закрытия (крестик) и направляем его в нашу логику reject."""
        print("[DEBUG] GenerationSessionDialog.closeEvent() called.")
        self.reject()
        event.ignore()