# gemini_translator/ui/widgets/log_widget.py

import time
import html
from PyQt6 import QtWidgets, QtCore, QtGui
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QCheckBox
from PyQt6.QtCore import pyqtSlot # <-- Добавляем pyqtSlot
# --- НОВЫЙ БЛОК: Выносим все правила стилизации в одно место ---
LOG_STYLES = [
    {
        'keywords': ['[FILTER]', '[API BLOCK]', 'CONTENT FILTER', 'ЗАБЛОКИРОВАНА', '🛡️ ФИЛЬТР', 'CONTENT_FILTER'],
        'color': "#9B59B6",  # Фиолетовый (Amethyst) - для блокировок контента
        'bold': True
    },
    {
        'keywords': ['[VALIDATION]', 'ВАЛИДАЦИЯ', 'VALIDATION FAILED', 'НЕ ПРОШЕЛ ВАЛИДАЦИЮ', '📋 ВАЛИДАЦИЯ', 'VALIDATION'],
        'color': "#1ABC9C",  # Бирюзовый (Turquoise) - для ошибок структуры и валидации
        'bold': True
    },
    {
        'keywords': ['[WARN]', '[WARNING]', 'ПРЕДУПРЕЖДЕНИЕ', '❗️', 'NETWORK', 'PARTIAL_GENERATION'],
        'color': "#F39C12",  # Оранжевый (Orange) - для предупреждений
        'bold': False
    },
    {
        'keywords': ['[SUCCESS]', 'УСПЕШНО', 'ГОТОВО', '✅', 'СЕССИЯ УСПЕШНО ЗАВЕРШЕНА'],
        'color': "#2ECC71",  # Зеленый (Emerald) - для сообщений об успехе
        'bold': True
    },
    {
        'keywords': ['[CANCELLED]', '[SKIP]', 'ОТМЕНЕНО', '[INFO]', 'СЕССИЯ ОСТАНОВЛЕНА', 'CANCEL'],
        'color': "#3498DB",  # Синий (Peter River) - для информационных сообщений
        'bold': False
    },
    {
        'keywords': ['[RATE LIMIT]', 'QUOTA EXCEEDED', 'QUOTA_EXCEEDED', 'ИСЧЕРПАН', 'TEMPORARY_LIMIT'], # <--- Добавлен QUOTA_EXCEEDED
        'color': "#E91E63",  # Розовый/Малиновый (Vivid Cerise) - для ошибок, связанных с лимитами API
        'bold': True
    },
    {
        'keywords': [
            '[FAIL]', '[ERROR]', '[FATAL]', '[CRITICAL]', 'ОШИБКА', '❌ ОШИБКА', 'ОКОНЧАТЕЛЬНЫЙ ПРОВАЛ',
            'GEOBLOCK', 'MODEL_NOT_FOUND', 'API_ERROR'
        ],
        'color': "#E74C3C",  # Красный (Alizarin) - для критических ошибок и провалов
        'bold': True
    },
    {
        'keywords': ['▶▶▶', '■■■', '[MANAGER]', "[TASK]"],
        'color': "#BDC3C7",  # Светло-серый (Silver) - для системных сообщений, структуры и этапов
        'bold': True
    }
]
# --- КОНЕЦ НОВОГО БЛОКА ---
class LogWidget(QWidget):
    """Виджет для отображения цветного лога выполнения с автопрокруткой."""
    def __init__(self, parent=None, event_bus=None):
        """
        Конструктор с гибридным получением зависимости (event_bus).
        
        Args:
            parent (QWidget, optional): Родительский виджет.
            event_bus (QObject, optional): Экземпляр шины событий. 
                Если не предоставлен, будет получен из QApplication.
        """
        super().__init__(parent)
        self._init_ui()
        
        # --- НАЧАЛО ИЗМЕНЕНИЯ: Гибридный подход ---
        self.bus = event_bus
        if self.bus is None:
            app = QtWidgets.QApplication.instance()
            if hasattr(app, 'event_bus'):
                self.bus = app.event_bus

        if self.bus:
            self.bus.event_posted.connect(self.on_event)
        else:
            # Это может быть полезно при отладке или изолированном запуске виджета
            print("[LogWidget WARN] Шина событий не предоставлена и не найдена. Логи не будут отображаться.")
        # --- КОНЕЦ ИЗМЕНЕНИЯ ---

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        
        controls_layout = QHBoxLayout()
        self.autoscroll_checkbox = QCheckBox("Автопрокрутка")
        self.autoscroll_checkbox.setChecked(True)
        controls_layout.addWidget(self.autoscroll_checkbox)
        controls_layout.addStretch()

        layout.addWidget(self.log_view)
        layout.addLayout(controls_layout)
    
    @pyqtSlot(dict)
    def on_event(self, event_data: dict):
        """
        Слот, который ловит события из шины.
        Реагирует только на событие 'log_message'.
        """
        if event_data.get('event') == 'log_message':
            data = event_data.get('data', {})
            # Вызываем наш существующий метод обработки
            self.append_message(data)
    
    def append_message(self, data: dict):
        """
        Принимает словарь данных, валидирует их и добавляет сообщение в лог.
        """
        # --- 1. ВАЛИДАЦИЯ ВХОДНЫХ ДАННЫХ ---
        if not isinstance(data, dict):
            return # Игнорируем, если пришло что-то кроме словаря

        message = data.get('message')
        # Игнорируем, если сообщение не строка или пустое (после удаления пробелов)
        if not isinstance(message, str) or not message.strip():
            return
            
        priority = data.get('priority', 'normal')

        # Если сообщение имеет низкий приоритет (финальное),
        # откладываем его обработку на следующий цикл событий.
        if priority == 'final':
            QtCore.QTimer.singleShot(0, lambda: self._add_html_to_log(message))
        else:
            self._add_html_to_log(message)
    
    def _add_html_to_log(self, message: str):
        """Внутренний метод для форматирования и добавления HTML в виджет."""
        if message == "---SEPARATOR---":
            separator_html = "<br><hr style='border: 1px dashed #4d5666;'><br>"
            cursor = self.log_view.textCursor()
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
            cursor.insertHtml(separator_html)
            return

        current_time = time.strftime("%H:%M:%S", time.localtime())
        formatted_line = f"[{current_time}] {message}"

        color = None
        bold = False
        msg_upper = message.upper()

        for style_rule in LOG_STYLES:
            if any(keyword in msg_upper for keyword in style_rule['keywords']):
                color = style_rule['color']
                bold = style_rule.get('bold', False)
                break
        
        # --- ИСПРАВЛЕНИЕ: Используем стандартный html.escape ---
        escaped_line = html.escape(formatted_line)

        html_line = "<span style='"
        if color:
            html_line += f"color: {color};"
        if bold:
            html_line += "font-weight: bold;"
        html_line += f"'>{escaped_line}</span><br>"

        cursor = self.log_view.textCursor()
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
        cursor.insertHtml(html_line)
        
        if self.autoscroll_checkbox.isChecked():
            self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())
    
    def clear(self):
        """Очищает лог."""
        self.log_view.clear()