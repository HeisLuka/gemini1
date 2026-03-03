# gemini_translator/ui/widgets/status_bar_widget.py

from PyQt6 import QtWidgets, QtCore
from collections import Counter
class StatusBarWidget(QtWidgets.QWidget):
    """
    Виджет, отображающий многоцветный прогресс-бар и текстовый статус.
    """
    def __init__(self, parent=None, event_bus=None, engine=None):
        super().__init__(parent)
        self.success_count = 0
        self.filtered_count = 0
        self.error_count = 0
        self.total_tasks = 0

        self._init_ui()
        
        # --- НОВАЯ ГИБКАЯ ИНИЦИАЛИЗАЦИЯ ---
        app = QtWidgets.QApplication.instance()
        
        # 1. Получаем event_bus
        self.bus = event_bus
        if not self.bus and hasattr(app, 'event_bus'):
            self.bus = app.event_bus

        # 2. Получаем engine
        self.engine = engine
        if not self.engine and hasattr(app, 'engine'):
            self.engine = app.engine
            
        # 3. Подписываемся на события, если шина найдена
        if self.bus:
            self.bus.event_posted.connect(self.on_event)
        else:
            print("[StatusBarWidget WARN] Шина событий не предоставлена. Статус-бар не будет обновляться.")


    def _init_ui(self):
        # Используем QStackedLayout для наложения прозрачного прогресс-бара
        # поверх цветных сегментов.
        main_layout = QtWidgets.QStackedLayout(self)
        main_layout.setStackingMode(QtWidgets.QStackedLayout.StackingMode.StackAll)
        main_layout.setContentsMargins(0, 0, 0, 0)
        self.setFixedHeight(28)

        # Слой 1: Цветные сегменты
        color_bar_widget = QtWidgets.QWidget()
        self.color_bar_layout = QtWidgets.QHBoxLayout(color_bar_widget)
        self.color_bar_layout.setContentsMargins(0, 0, 0, 0)
        self.color_bar_layout.setSpacing(0)

        self.part_success = QtWidgets.QLabel()
        self.part_success.setStyleSheet("background-color: #2ECC71;")
        self.part_filtered = QtWidgets.QLabel()
        self.part_filtered.setStyleSheet("background-color: #9B59B6;")
        self.part_error = QtWidgets.QLabel()
        self.part_error.setStyleSheet("background-color: #E74C3C;")
        self.part_pending = QtWidgets.QLabel()
        self.part_pending.setStyleSheet("background-color: #373e4b;")
        
        for part in [self.part_success, self.part_filtered, self.part_error, self.part_pending]:
            self.color_bar_layout.addWidget(part, 0)
        
        # Слой 2: Прозрачный QProgressBar для текста
        self.progress_bar_text = QtWidgets.QProgressBar()
        self.progress_bar_text.setStyleSheet("""
            QProgressBar {
                background-color: transparent;
                border: 1px solid #4d5666;
                border-radius: 5px;
                text-align: center;
                color: #f0f0f0;
            }
            QProgressBar::chunk { background-color: transparent; }
        """)
        self.progress_bar_text.setTextVisible(True)
        
        self.temp_message_timer = QtCore.QTimer(self)
        self.temp_message_timer.setSingleShot(True)
        self.temp_message_timer.timeout.connect(self.clear_message)
        
        main_layout.addWidget(color_bar_widget)
        main_layout.addWidget(self.progress_bar_text)

        self.reset() # Устанавливаем начальное состояние
        self.setVisible(False)
    
    @QtCore.pyqtSlot(dict)
    def on_event(self, event_data: dict):
        """Слушает глобальную шину и реагирует на нужные события."""
        event_name = event_data.get('event')
        data = event_data.get('data', {})

        if event_name == 'session_started':
            self.start_session(data.get('total_tasks', 0))
        
        elif event_name == 'session_finished':
            self.stop_session()
            
        elif event_name == 'task_state_changed':
            # При каждом "пульсе" системы полностью пересчитываем состояние с нуля
            if self.engine and self.engine.task_manager:
                ui_state_list = self.engine.task_manager.get_ui_state_list()
                
                # Обновляем общее количество задач
                self.total_tasks = len(ui_state_list)
                self.progress_bar_text.setRange(0, self.total_tasks)
                
                # Считаем статусы
                status_counts = Counter(item[1] for item in ui_state_list)
                error_total = sum(count for status, count in status_counts.items() if 'error' in status)
                
                self.update_counts(
                    success=status_counts.get('success', 0) + status_counts.get('glossary_success', 0),
                    filtered=status_counts.get('filtered', 0),
                    error=error_total
                )
    
    def start_session(self, total_tasks=0):
        """Вызывается в начале сессии для настройки."""
        self.reset()
        self.total_tasks = total_tasks
        self.progress_bar_text.setRange(0, total_tasks)
        self._update_display()
        self.setVisible(True)

    def stop_session(self):
        """Вызывается в конце сессии для сброса и скрытия."""
        # Не сбрасываем счетчики, чтобы пользователь видел финальный результат
        self.setVisible(False)
        
    def reset(self):
        """Сбрасывает все счетчики и виджеты в исходное состояние."""
        self.success_count = 0
        self.filtered_count = 0
        self.error_count = 0
        self.total_tasks = 0
        self.progress_bar_text.setValue(0)
        self._update_display()

    def update_counts(self, success, filtered, error):
        """Напрямую устанавливает количество задач каждого типа."""
        self.success_count = success
        self.filtered_count = filtered
        self.error_count = error
        self._update_display()

    def _update_display(self):
        """Обновляет текстовое и графическое представление прогресс-бара."""
        if self.total_tasks == 0:
            self.progress_bar_text.setFormat("Нет задач")
            return

        processed_count = self.success_count + self.filtered_count + self.error_count
        self.progress_bar_text.setValue(processed_count)
        
        # Обновляем текст
        self.progress_bar_text.setFormat(
            f"Успех: {self.success_count} | Фильтр: {self.filtered_count} | Ошибки: {self.error_count} | Готово: {processed_count}/{self.total_tasks} (%p%)"
        )
        
        # Обновляем цветные сегменты
        self.part_success.setVisible(self.success_count > 0)
        self.part_filtered.setVisible(self.filtered_count > 0)
        self.part_error.setVisible(self.error_count > 0)
        
        remaining_count = self.total_tasks - processed_count
        self.part_pending.setVisible(remaining_count > 0)

        self.color_bar_layout.setStretch(0, self.success_count)
        self.color_bar_layout.setStretch(1, self.filtered_count)
        self.color_bar_layout.setStretch(2, self.error_count)
        self.color_bar_layout.setStretch(3, remaining_count)
    
    def show_message(self, message: str, temporary: bool = True, duration_ms: int = 3000):
        """
        Универсальный метод для отображения сообщений в статус-баре.
        
        :param message: Текст сообщения.
        :param temporary: Если True, сообщение исчезнет через duration_ms.
        :param duration_ms: Время отображения временного сообщения в миллисекундах.
        """
        # Если было временное сообщение, останавливаем его таймер
        if self.temp_message_timer.isActive():
            self.temp_message_timer.stop()
            
        self.progress_bar_text.setFormat(message)
        
        if temporary:
            # Запускаем таймер для очистки
            self.temp_message_timer.start(duration_ms)

    def clear_message(self):
        """
        Очищает сообщение и возвращает отображение стандартного прогресса.
        """
        # Останавливаем таймер, если он был запущен
        if self.temp_message_timer.isActive():
            self.temp_message_timer.stop()
            
        # Возвращаем стандартный формат, который обновляется по событиям
        self._update_display()
        
    def set_permanent_message(self, message: str):
        """Отображает постоянное сообщение, которое не сбрасывается."""
        self.show_message(message, temporary=False)
        
    def closeEvent(self, event):
        """Отписываемся от шины при закрытии/уничтожении виджета."""
        if self.bus:
            try:
                self.bus.event_posted.disconnect(self.on_event)
            except (TypeError, RuntimeError):
                pass
        super().closeEvent(event)