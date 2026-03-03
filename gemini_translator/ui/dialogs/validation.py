# -*- coding: utf-8 -*-

import sys
import os
import zipfile
import re
import json
from bs4 import BeautifulSoup, NavigableString, ProcessingInstruction, Comment, Declaration
import shutil
from ...utils.epub_tools import get_epub_chapter_order, extract_number_from_path
from ...utils.language_tools import LanguageDetector
from ...utils.text import is_well_formed_xml
from ...utils.project_migrator import ProjectMigrator

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QFileDialog, QTableWidget, QTableWidgetItem, QLabel, QHeaderView,
    QMessageBox, QAbstractItemView, QSpinBox, QCheckBox, QGroupBox,
    QDialog, QTextEdit, QSplitter, QComboBox, QScrollArea, QDialogButtonBox,
    QGridLayout, QProgressDialog
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, pyqtSlot, QUrl, QRegularExpression
from PyQt6.QtGui import QDesktopServices, QColor, QBrush, QSyntaxHighlighter, QTextCharFormat, QFont, QTextCursor
from PyQt6 import QtCore, QtGui, QtWidgets


from ..widgets.preset_widget import PresetWidget
from ...api import config as api_config
from .validation_dialogs.untranslated_fixer_dialog import UntranslatedFixerDialog

REGEX_DIGITS = re.compile(r'\d')

# 1. Восклицательные и вопросительные знаки
# Логика: Группируем последовательности с пробелами (например, "? !", "!!!").
# Это важно, так как стиль (количество знаков) может меняться, но сам факт наличия эмоции/вопроса сохраняется.
# Добавлено:
# \u00a1 \u00bf : Перевернутые ¡ ¿ (бывают в текстах как артефакты или цитаты)
# \u061f : Арабский/персидский вопрос ؟
# \u203d : Интерробанг ‽
# \u2047-\u2049 : Двойные знаки ⁇ ⁈ ⁉
REGEX_BANG_QUEST = re.compile(r'[!?\uff01\uff1f\u00a1\u00bf\u061f\u203d\u2047-\u2049](?:\s*[!?\uff01\uff1f\u00a1\u00bf\u061f\u203d\u2047-\u2049])*')

# 2. Точки (завершение утверждения)
# Логика: Группируем. Это позволяет отловить "ручное" многоточие из точек.
# Добавлено:
# \u3002 : Китайская/Японская "круглая" точка 。
# \uff0e : Полноширинная точка ．
# \u06d4 : Арабская точка (урду/перс) ۔
REGEX_DOTS = re.compile(r'[.\u3002\uff0e\u06d4](?:\s*[.\u3002\uff0e\u06d4])*')

# 3. Многоточия и прерывания речи
# Логика:
# 1. […\u2026\u2025\u22ef\ufe19]+ : Стандартные символы многоточия (включая CJK).
# 2. | : ИЛИ
# 3. [-–—\u2013\u2014\u2015\u2212]+ : Любые виды тире/дефисов...
# 4. (?=\s*[.!?:;»…\u2026]) : ...НО только если за ними следует знак препинания (Lookahead).
#    Это позволяет поймать "—!" как Эллипсис(—) + Воскл(!), но игнорировать диалоговое "— Привет".
REGEX_ELLIPSIS = re.compile(r'(?:[…\u2026\u2025\u22ef\ufe19]+|[-–—\u2013\u2014\u2015\u2212]+(?=\s*[.!?:;»…\u2026]))')

# 4. Запятые (перечисление/разделение частей)
# Логика: НЕ группируем. Количество запятых коррелирует с количеством простых предложений в составе сложного.
# Добавлено:
# \uff0c : Полноширинная запятая ，
# \u3001 : Каплевидная запятая (CJK enumeration comma) 、 — очень важна, аналог нашей запятой при перечислении.
# \u060c : Арабская запятая ،
REGEX_COMMAS = re.compile(r'[,\uff0c\u3001\u060c]')

# 5. Двоеточия и Точки с запятой (структурные разделители)
# Добавляем этот класс, так как при проверке "пересказа" важно видеть, где автор использовал сильное разделение.
# Обычно они маппятся 1 к 1 или заменяются на точку.
# Логика: НЕ группируем (или группируем с осторожностью), обычно считаем поштучно.
# Символы:
# : ; : ASCII
# \uff1a \uff1b : Полноширинные ： ；
# \u061b : Арабская точка с запятой ؛
REGEX_COLONS_SEMIS = re.compile(r'[:;\uff1a\uff1b\u061b]')

class LargeTextInputDialog(QDialog):
    """
    Финальная версия диалога: большое поле для ввода сверху и
    интерактивный список кликабельных примеров снизу, который
    автоматически сворачивается при начале ввода.
    """
    def __init__(self, initial_text="", parent=None, title="Редактор текста"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(600, 500)
        
        # Флаг, чтобы "схлопывание" сработало только один раз
        self.is_first_input = True

        # --- 1. Ритуалы Поиска ---
        self.examples = [
            {
                "title": "• Найти тире в конце абзаца (потенциально разорванный диалог):",
                "code": "—\\s*</p>"
            },
            {
                "title": "• Найти слова 'Хакер' или 'Вирус' (без учета регистра):",
                "code": "Хакер|Вирус"
            },
            {
                "title": "• Найти тире и кавычки внутри ОДНОГО абзаца (нарушение оформления):",
                "code": "<p[^>]*>(?=[^<]*—)(?=[^<]*[«»]).*?</p>"
            },
            {
                "title": "• Найти две идущие подряд точки (опечатка):",
                "code": "\\.\\."
            },
            {
                "title": "• Найти тег <img>, у которого нет атрибута alt:",
                "code": "<img(?!.*?alt=.*?>).*?>"
            },
            {
                "title": "• Найти абзац, который начинается с маленькой буквы (кроме случаев с «...»):",
                "code": "<p>(?!\\.\\.\\.)[а-я]"
            },
            {
                "title": "• Найти 'прямые' кавычки (должны быть «ёлочки»):",
                "code": "\""
            },
            {
                "title": "• Найти пустые абзацы (артефакт форматирования):",
                "code": "<p>\\s*(?:&nbsp;)?\\s*</p>"
            },
            {
                "title": "• Найти дефис в начале абзаца (вместо тире диалога):",
                "code": "<p>\\s*-"
            },
            {
                "title": "• Найти два и более пробела подряд (ошибка набора):",
                "code": " {2,}"
            },
            {
                "title": "• Найти абзац с «ёлочками», внутри которых есть 'прямая' кавычка:",
                "code": "«[^»\"]*\"[^»]*»"
            }
        ]

        # --- 2. Создание UI со сплиттером ---
        main_layout = QVBoxLayout(self)
        
        self.splitter = QtWidgets.QSplitter(Qt.Orientation.Vertical)

        # Верхняя часть: редактор текста
        self.text_edit = QTextEdit()
        self.text_edit.setPlaceholderText("Начните вводить текст или выберите пример ниже…")
        self.text_edit.setPlainText(initial_text)
        self.splitter.addWidget(self.text_edit)

        # Нижняя часть: список примеров
        examples_container = QWidget()
        examples_layout = QVBoxLayout(examples_container)
        examples_layout.setContentsMargins(0, 5, 0, 0)
        examples_layout.addWidget(QLabel("<b>Кликабельные примеры:</b>"))
        
        self.examples_list = QtWidgets.QListWidget()
        self.examples_list.setSpacing(5)
        self._populate_examples_list()
        self.examples_list.itemClicked.connect(self._on_example_clicked)
        
        examples_layout.addWidget(self.examples_list)
        self.splitter.addWidget(examples_container)
        
        # Устанавливаем начальные размеры для сплиттера
        # Если есть текст, сразу сворачиваем примеры
        if initial_text:
            QtCore.QTimer.singleShot(0, lambda: self.splitter.setSizes([1, 0])) # Сворачиваем асинхронно
            self.is_first_input = False # Считаем, что первый ввод уже был
        else:
            self.splitter.setSizes([200, 300])
        
        main_layout.addWidget(self.splitter)

        # Кнопки Ok/Cancel
        button_box = QDialogButtonBox()
        ok_button = button_box.addButton("Принять", QDialogButtonBox.ButtonRole.AcceptRole)
        cancel_button = button_box.addButton("Отмена", QDialogButtonBox.ButtonRole.RejectRole)
        
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        main_layout.addWidget(button_box)

        # --- 3. Подключаем новую логику ---
        self.text_edit.textChanged.connect(self._on_text_changed)

    def _populate_examples_list(self):
        """Заполняет список примерами, делая их красивыми и безопасными."""
        for item_data in self.examples:
            item_widget = QWidget()
            item_layout = QVBoxLayout(item_widget)
            item_layout.setContentsMargins(5, 5, 5, 5); item_layout.setSpacing(2)
            
            escaped_title = item_data["title"].replace('<', '&lt;').replace('>', '&gt;')
            title_label = QLabel(escaped_title)
            
            escaped_code = item_data["code"].replace('<', '&lt;').replace('>', '&gt;')
            code_label = QLabel(f"<code>{escaped_code}</code>")
            code_label.setStyleSheet("background-color: #313643; padding: 4px; border-radius: 3px; font-family: Consolas, monospace;")
            code_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

            # --- НАЧАЛО КЛЮЧЕВОГО ИЗМЕНЕНИЯ ---
            # Устанавливаем фильтр событий. self (диалог) будет "слушать" события code_label.
            code_label.installEventFilter(self)
            # --- КОНЕЦ КЛЮЧЕВОГО ИЗМЕНЕНИЯ ---

            item_layout.addWidget(title_label); item_layout.addWidget(code_label)

            list_item = QtWidgets.QListWidgetItem(self.examples_list)
            list_item.setSizeHint(item_widget.sizeHint())
            list_item.setData(Qt.ItemDataRole.UserRole, item_data["code"])
            
            self.examples_list.addItem(list_item)
            self.examples_list.setItemWidget(list_item, item_widget)
            
            # --- НАЧАЛО КЛЮЧЕВОГО ИЗМЕНЕНИЯ ---
            # Сохраняем ссылку на list_item внутри code_label, чтобы знать, какой элемент активировать.
            code_label.setProperty("list_item", list_item)
            # --- КОНЕЦ КЛЮЧЕВОГО ИЗМЕНЕНИЯ ---

    # ДОБАВЬТЕ ЭТОТ НОВЫЙ МЕТОД В КЛАСС LargeTextInputDialog
    def eventFilter(self, source_object, event):
        """
        Перехватывает события от отслеживаемых виджетов.
        В данном случае, ловит клики мыши по QLabel с кодом.
        """
        # Проверяем, что событие - это нажатие левой кнопки мыши
        # и что источник события - это QLabel (чтобы не реагировать на другие виджеты)
        if event.type() == QtCore.QEvent.Type.MouseButtonPress and isinstance(source_object, QLabel):
            if event.button() == Qt.MouseButton.LeftButton:
                # Извлекаем сохраненный QListWidgetItem из свойства виджета
                list_item = source_object.property("list_item")
                if list_item:
                    # Эмулируем клик по элементу списка
                    self._on_example_clicked(list_item)
                    return True # Сообщаем, что мы обработали событие, и его не нужно передавать дальше

        # Для всех остальных событий возвращаем стандартное поведение
        return super().eventFilter(source_object, event)


    def _on_example_clicked(self, item):
        # Этот метод остается почти без изменений, просто убираем переключение виджетов
        code_to_insert = item.data(Qt.ItemDataRole.UserRole)
        if code_to_insert:
            self.text_edit.setPlainText(code_to_insert)
            self.text_edit.setFocus()
            # После клика тоже сворачиваем
            self.splitter.setSizes([self.splitter.height(), 0])
            self.is_first_input = False

    def _on_text_changed(self):
        """При первом изменении текста в пустом поле сворачивает сплиттер."""
        # Если флаг уже снят или текста нет, ничего не делаем
        if not self.is_first_input or not self.text_edit.toPlainText():
            return
        
        # Сворачиваем панель с примерами, отдавая все место редактору
        # setSizes принимает список размеров для каждого виджета в сплиттере
        self.splitter.setSizes([self.splitter.height(), 0])
        
        # Снимаем флаг, чтобы это больше не повторялось
        self.is_first_input = False
        
    def get_text(self):
        return self.text_edit.toPlainText()
        
class SortableChapterItem(QTableWidgetItem):
    """
    Кастомный элемент таблицы, который использует централизованную функцию
    для извлечения числового ключа сортировки.
    """
    def __init__(self, display_text, sort_key_path):
        super().__init__(display_text)
        self.internal_path = sort_key_path
        # Мы больше не храним sort_value, так как __lt__ будет вычислять его на лету

    def __lt__(self, other):
        """
        Переопределяем оператор "меньше чем" (<), который используется для сортировки.
        """
        if isinstance(other, SortableChapterItem):
            # Вызываем универсальную функцию для обоих элементов
            return extract_number_from_path(self) < extract_number_from_path(other)
        return super().__lt__(other)

class ChapterStatusDelegate(QtWidgets.QStyledItemDelegate):
    """
    Кастомный делегат, который рисует название главы и индикатор 'готово',
    корректно обрабатывая фон, выделение и всплывающие подсказки.
    """
    def paint(self, painter, option, index):
        text = ""
        has_validated = index.data(Qt.ItemDataRole.UserRole)

        init_option = QtWidgets.QStyleOptionViewItem(option)
        init_option.text = ""
        super().paint(painter, init_option, index)
        
        if has_validated:
            painter.save()
            indicator_rect = QtCore.QRect(option.rect)
            indicator_rect.setLeft(indicator_rect.right() - 24)
            indicator_rect.adjust(0, 2, 0, -2)
            
            painter.setFont(QFont("Segoe UI Symbol", 10))
            painter.setPen(QColor("#2ECC71"))
            painter.drawText(indicator_rect, Qt.AlignmentFlag.AlignCenter, "✅")
            painter.restore()
            option.rect.setRight(option.rect.right() - 26)

        text_color = QColor("#2ECC71") if has_validated else option.palette.color(QtGui.QPalette.ColorRole.Text)
        painter.setPen(text_color)
        
        flags = Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap
        painter.drawText(option.rect.adjusted(5, 0, 0, 0), int(flags), text)

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setHeight(size.height() + 8)
        return size

    def helpEvent(self, event, view, option, index):
        if not (event and view and option and index):
            return False

        has_validated = index.data(Qt.ItemDataRole.UserRole)
        if has_validated:
            indicator_rect = QtCore.QRect(option.rect)
            indicator_rect.setLeft(indicator_rect.right() - 24)
            
            if indicator_rect.contains(event.pos()):
                tooltip_text = "Для этой главы уже существует проверенная версия в папке 'validated_ok'."
                QtWidgets.QToolTip.showText(event.globalPos(), tooltip_text, view)
                return True
                
        # --- КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ ЗДЕСЬ ---
        # Мы убираем вызов super() и просто возвращаем False, если не обработали событие сами.
        # Это гарантирует, что функция всегда вернет bool.
        return False
        

    def editorEvent(self, event, model, option, index):
        # Этот метод теперь не нужен, так как helpEvent всегда возвращает bool.
        # Но для совместимости и ясности лучше оставить его как есть,
        # он просто будет вызывать helpEvent.
        if event.type() == QtCore.QEvent.Type.ToolTip:
            return self.helpEvent(event, self.parent(), option, index)
        return super().editorEvent(event, model, option, index)

class StructureErrorsDialog(QDialog):
    """
    Диалоговое окно для детального отображения структурных несоответствий с
    интерактивными кнопками для поиска проблем в коде.
    """
    # Новый сигнал, который будет отправлять тег для поиска
    find_tag_in_code_requested = pyqtSignal(str)

    def __init__(self, errors_dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Детали структурных несоответствий")
        self.setMinimumSize(650, 450)
        
        layout = QVBoxLayout(self)
        
        self.details_view = QTextEdit()
        self.details_view.setReadOnly(True)
        # Устанавливаем макет для виджета, чтобы можно было добавлять другие виджеты
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setWidget(self.details_view)
        layout.addWidget(scroll_area)
        
        ok_button = QPushButton("Закрыть")
        ok_button.clicked.connect(self.accept)
        layout.addWidget(ok_button, 0, Qt.AlignmentFlag.AlignRight)
        
        self._populate_details(errors_dict)



# --- ЗАМЕНИТЕ ЭТОТ МЕТОД ЦЕЛИКОМ ---
    def _populate_details(self, errors):
        container = QWidget()
        details_layout = QVBoxLayout(container)
        self.details_view.setLayout(details_layout)
        
        details_layout.addWidget(QLabel("<h3>Обнаружены следующие несоответствия:</h3>"))
        
        CUSTOM_TAG_DESCRIPTIONS = {"<!-- RESTORED_IMAGE_WARNING -->": "<b>Восстановленное изображение:</b> Модель потеряла тег <img>, и он был восстановлен автоматически. Его положение в тексте может быть неточным."}
        
        # --- НАЧАЛО ИЗМЕНЕНИЯ: Отображение ошибки валидности XML ---
        if 'malformed_xml' in errors:
            group = QGroupBox("Критическая ошибка структуры (Malformed XML)")
            group_layout = QVBoxLayout(group)
            
            description, error_msg = errors['malformed_xml']
            group_layout.addWidget(QLabel(f"<b>Описание:</b> {description}"))
            
            error_label = QLabel(f"<b>Сообщение парсера:</b> <code>{error_msg.replace('<', '&lt;')}</code>")
            error_label.setWordWrap(True)
            group_layout.addWidget(error_label)
            
            details_layout.addWidget(group)
        

        if 'custom_tags' in errors:
            group = QGroupBox("Автоматические исправления (требуют проверки)")
            group_layout = QVBoxLayout(group)
            for tag in errors['custom_tags']:
                widget = QWidget()
                row_layout = QHBoxLayout(widget)
                label = QLabel(CUSTOM_TAG_DESCRIPTIONS.get(tag, f"Неизвестный маркер: {tag}"))
                label.setWordWrap(True)
                find_button = QPushButton("Найти в коде")
                find_button.setToolTip(f"Найти и выделить тег {tag.replace('<', '&lt;')} в редакторе")
                find_button.clicked.connect(lambda checked, t=tag: (self.find_tag_in_code_requested.emit(t), self.accept()))
                row_layout.addWidget(label, 1)
                row_layout.addWidget(find_button, 0, Qt.AlignmentFlag.AlignRight)
                group_layout.addWidget(widget)
            details_layout.addWidget(group)
        
        def format_line(name, orig, trans):
            color = "red" if str(orig) != str(trans) else "green"
            return f'<li><b>{name}:</b> Оригинал: {orig}, Перевод: {trans} <font color="{color}">({ "Несовпадение" if str(orig) != str(trans) else "OK"})</font></li>'
        
        has_tag_errors = False
        tag_html = "<ul>"
        
        if 'fundamental_tags' in errors:
            for tag, (orig_found, trans_found) in errors['fundamental_tags'].items():
                if not (orig_found == trans_found):
                    has_tag_errors = True
                    safe_tag = tag.replace('<', '&lt;').replace('>', '&gt;')
                    tag_html += format_line(f"Тег <code>{safe_tag}</code>", "Есть" if orig_found else "Нет", "Есть" if trans_found else "Нет")
        
        if 'unbalanced_p' in errors:
            has_tag_errors = True
            open_p, close_p = errors['unbalanced_p']
            tag_html += format_line("Теги &lt;p&gt; (Откр/Закр)", "Сбалансировано", f"{open_p} / {close_p}")

        for h in sorted(errors.get('headings', {}).keys()):
            orig_h, trans_h = errors['headings'][h]
            if orig_h != trans_h:
                has_tag_errors = True
                tag_html += format_line(f"Теги &lt;{h}&gt;", orig_h, trans_h)

        for tag in ['images', 'links', 'lists']:
            if tag in errors:
                orig_t, trans_t = errors[tag]
                if orig_t != trans_t:
                    has_tag_errors = True
                    tag_name = tag.replace('images', 'img').replace('links', 'a').replace('lists', 'ol/ul')
                    tag_html += format_line(f"Теги &lt;{tag_name}&gt;", orig_t, trans_t)
        
        tag_html += "</ul>"
        
        if has_tag_errors:
            group = QGroupBox("Общие структурные ошибки")
            group_layout = QVBoxLayout(group)
            group_layout.addWidget(QLabel(tag_html))
            details_layout.addWidget(group)

        details_layout.addStretch(1)


class HtmlHighlighter(QSyntaxHighlighter):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.highlightingRules = []

        # Набор правил для подсветки
        # Теги (<p>, <body>)
        tagFormat = QTextCharFormat()
        tagFormat.setForeground(QColor("#569CD6"))  # Более стандартный синий для тегов
        self.highlightingRules.append((QRegularExpression(r"</?\w+"), tagFormat))
        self.highlightingRules.append((QRegularExpression(r"[<>]"), tagFormat))


        # Атрибуты (class, href)
        attributeFormat = QTextCharFormat()
        attributeFormat.setForeground(QColor("#9CDCFE"))  # Светло-голубой для атрибутов
        self.highlightingRules.append((QRegularExpression(r'\s+([\w\-.:]+)\s*='), attributeFormat))

        # Значения атрибутов ("my-class")
        stringFormat = QTextCharFormat()
        stringFormat.setForeground(QColor("#CE9178"))  # Оранжевый для строк
        self.highlightingRules.append((QRegularExpression(r'"[^"]*"'), stringFormat))
        self.highlightingRules.append((QRegularExpression(r"'[^']*'"), stringFormat))

        # Комментарии <!-- ... -->
        commentFormat = QTextCharFormat()
        commentFormat.setForeground(QColor("#6A9955"))  # Зеленый для комментариев
        commentFormat.setFontItalic(True)
        self.highlightingRules.append((QRegularExpression(r"<!--.*?-->"), commentFormat))



        # DOCTYPE
        doctypeFormat = QTextCharFormat()
        doctypeFormat.setForeground(QColor("#4EC9B0")) # Бирюзовый
        self.highlightingRules.append((QRegularExpression(r'<!DOCTYPE[^>]+>', QRegularExpression.PatternOption.CaseInsensitiveOption), doctypeFormat))

    def highlightBlock(self, text):
        for pattern, format in self.highlightingRules:
            iterator = pattern.globalMatch(text)
            while iterator.hasNext():
                match = iterator.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), format)

class PunctuationHighlighter(QSyntaxHighlighter):
    """
    Подсвечивает ключевые знаки препинания в отформатированном тексте
    для быстрой проверки правильности оформления диалогов и мыслей.
    """
    def __init__(self, parent=None):
        super().__init__(parent)

        self.highlightingRules = []

        # Правило для длинных тире (прямая речь) - яркий, хорошо читаемый цвет
        dialogue_format = QTextCharFormat()
        dialogue_format.setForeground(QColor("#00FFFF")) # Яркий Аква (Cyan)
        dialogue_format.setFontWeight(QFont.Weight.Bold)
        self.highlightingRules.append((QRegularExpression("—"), dialogue_format))

        # Правило для кавычек-«ёлочек» (мысли, цитаты) - оставляем, он хорош
        thought_format = QTextCharFormat()
        thought_format.setForeground(QColor("#FFD700")) # Золотой
        thought_format.setFontWeight(QFont.Weight.Bold)
        self.highlightingRules.append((QRegularExpression("[«»]"), thought_format))

        # Правило для обычных кавычек и апострофов
        quote_format = QTextCharFormat()
        quote_format.setForeground(QColor("#ADFF2F")) # Яркий Зелено-Желтый (GreenYellow)
        self.highlightingRules.append((QRegularExpression("[\"']"), quote_format))

        # Правило для коротких тире и дефисов
        dash_format = QTextCharFormat()
        dash_format.setForeground(QColor("#D8BFD8")) # Светлая Лаванда (Thistle)
        self.highlightingRules.append((QRegularExpression("[-–]"), dash_format))

    def highlightBlock(self, text):
        # Применяем все правила к текущему блоку текста
        for pattern, format in self.highlightingRules:
            iterator = pattern.globalMatch(text)
            while iterator.hasNext():
                match = iterator.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), format)

class NumericTableWidgetItem(QTableWidgetItem):
    def __lt__(self, other):
        try:
            return float(self.text().split('|')[0].strip()) < float(other.text().split('|')[0].strip())
        except (ValueError, IndexError):
            return super().__lt__(other)


# --- Поток для анализа ---
class ValidationThread(QThread):
    result_found = pyqtSignal(dict)
    progress_update = pyqtSignal(str, int, int)
    analysis_finished = pyqtSignal(int, int)

    
    ERROR_PRIORITIES = {
        "Недоперевод": 1,
        "Повтор": 2,
        "Нарушение XML-структуры": 3,
        "Структурная ошибка": 4,
        "Большой абзац": 5,
        "Восстановлено изображение": 6,
        "Откл": 7, # <-- Изменено название ключа
        "Длина": 8,
        "Ошибка парсинга": 99
    }
    
    def __init__(self, translated_folder, original_epub_path, checks_config, word_exceptions_set, project_manager, files_to_scan=None):
        super().__init__()
        self.translated_folder = translated_folder
        self.original_epub_path = original_epub_path
        self.config = checks_config
        self._is_running = True
        self.word_exceptions = word_exceptions_set
        self.project_manager = project_manager
        self.files_to_scan = set(files_to_scan) if files_to_scan else None # Если None, сканируем всё


    def _analyze_html_content(self, original_content, translated_content, result_data):
        reasons = set() # Оставляем для критических ошибок, которые нельзя настроить (XML)
        structural_errors = {}
        custom_tags_found = []
        
        # Набор ключей, указывающий на наличие данных (а не на наличие проблемы!)
        detected_keys = set() 

        # --- 1. XML и Критика (Это всегда проблема) ---
        orig_is_ok, _ = is_well_formed_xml(original_content, validate=True)
        trans_is_ok, trans_error_msg = is_well_formed_xml(translated_content, validate=True)

        if orig_is_ok and not trans_is_ok:
            reasons.add("Нарушение XML-структуры")
            detected_keys.add('xml_syntax') # Это безусловный ключ
            structural_errors['malformed_xml'] = ("Синтаксическая ошибка XML.", trans_error_msg)

        # --- 2. Структура (Собираем данные всегда) ---
        clean_trans_content = translated_content.lower()
        tags_to_check = {'<html>': '<html', '</html>': '</html>', '<body>': '<body', '</body>': '</body>'}
        fundamental_tag_results = {}
        has_fundamental_error = False
        for display_name, search_string in tags_to_check.items():
            orig_found, trans_found = (search_string in original_content.lower(), search_string in clean_trans_content)
            fundamental_tag_results[display_name] = (orig_found, trans_found)
            if orig_found != trans_found: has_fundamental_error = True
        
        p_open_count = clean_trans_content.count('<p')
        p_close_count = clean_trans_content.count('</p>')
        
        # Сохраняем сырые данные о структуре
        if has_fundamental_error: structural_errors['fundamental_tags'] = fundamental_tag_results
        if p_open_count != p_close_count: structural_errors['unbalanced_p'] = (p_open_count, p_close_count)

        # --- НАЧАЛО БЛОКА TRY (Восстановлено) ---
        try:
            # Fingerprints
            soup_orig = BeautifulSoup(original_content, 'html.parser')
            soup_trans = BeautifulSoup(translated_content, 'html.parser')
            orig_fp, trans_fp = self._create_structural_fingerprint(soup_orig), self._create_structural_fingerprint(soup_trans)

            for h in set(orig_fp['headings'].keys()) | set(trans_fp['headings'].keys()):
                if orig_fp['headings'].get(h, 0) != trans_fp['headings'].get(h, 0):
                    # Исключаем случай 0 -> 1 (часто бывает с авто-генерацией)
                    if not (orig_fp['headings'].get(h, 0) == 0 and trans_fp['headings'].get(h, 0) == 1):
                         structural_errors.setdefault('headings', {})[h] = (orig_fp['headings'].get(h, 0), trans_fp['headings'].get(h, 0))
            
            for tag in ['images', 'links', 'lists']:
                if orig_fp[tag] != trans_fp[tag]: structural_errors[tag] = (orig_fp[tag], trans_fp[tag])
            
            # Если что-то нашли - помечаем наличие данных
            if structural_errors:
                detected_keys.add('structure_data')

            # --- 3. Длина и Абзацы (Собираем статистику) ---
            body_orig, body_trans = soup_orig.find('body'), soup_trans.find('body')
            text_orig, text_trans = (b.get_text(separator=' ', strip=True) if b else "" for b in [body_orig, body_trans])
            
            result_data['len_orig'], result_data['len_trans'] = len(text_orig), len(text_trans)
            
            # Ratio (просто сохраняем число)
            if len(text_orig) > 0:
                result_data['ratio_value'] = len(text_trans) / len(text_orig)
            else:
                result_data['ratio_value'] = 0.0

            # Largest Paragraph (просто сохраняем число)
            largest_paragraph_found = 0
            paragraphs = soup_trans.find_all('p')
            for p_tag in paragraphs:
                paragraph_len = len(p_tag.get_text(strip=True))
                if paragraph_len > largest_paragraph_found: largest_paragraph_found = paragraph_len
            
            # Fallback для br
            if not paragraphs:
                container = soup_trans.body or soup_trans
                if container:
                    br_splits = re.split(r'(?:<br\s*/?>\s*){2,}', str(container), flags=re.IGNORECASE)
                    for segment in br_splits:
                        s_len = len(BeautifulSoup(segment, 'html.parser').get_text(strip=True))
                        if s_len > largest_paragraph_found: largest_paragraph_found = s_len
            
            result_data['largest_paragraph'] = largest_paragraph_found # <-- СЫРОЕ ДАННОЕ

            # --- 4. Упрощение (Собираем статистику) ---
            orig_p_br_count = self._count_paragraph_equivalents(original_content)
            trans_p_br_count = self._count_paragraph_equivalents(translated_content)
            result_data['simplification_stats'] = (orig_p_br_count, trans_p_br_count) # <-- СЫРОЕ ДАННОЕ
            
            dig_o, punct_o = self.fast_stat_count(original_content)
            dig_t, punct_t = self.fast_stat_count(translated_content)

            # Распаковываем значение и тип
            dev_val, dev_type = self._calculate_combined_deviation(
                orig_p_br_count, trans_p_br_count,
                dig_o, dig_t,
                punct_o, punct_t
            )
            result_data['combined_deviation'] = dev_val
            result_data['deviation_type'] = dev_type # <-- Сохраняем тип (Абзац/Цифра/Пункт)

            # --- 5. Повторы (Исправленная логика) ---
            min_reps_scan = 5 
            max_pattern_len = 20
            
            best_repeat_candidate = None 
            max_reps_found = 0

            # Проходим по всем длинам, чтобы найти ТОТ, у которого больше всего повторений.
            # (Раньше мы останавливались на первом длинном, и это скрывало частые короткие повторы)
            for pattern_len in range(max_pattern_len, 0, -1):
                required_extra = max(1, min_reps_scan - 1)
                try:
                    regex = re.compile(r'(.{' + str(pattern_len) + r'})\1{' + str(required_extra) + r',}', re.DOTALL)
                    match = regex.search(translated_content)
                    if match:
                        full_sequence = match.group(0)
                        repeated_pattern = match.group(1)
                        
                        # Игнорируем обычные пробельные отступы, если их не экстремально много
                        if repeated_pattern.strip() == "" and len(full_sequence) // len(repeated_pattern) < 50:
                            continue
                        
                        actual_count = len(full_sequence) // len(repeated_pattern)
                        
                        # ГЛАВНОЕ ИСПРАВЛЕНИЕ:
                        # Мы сохраняем результат, только если количество повторений БОЛЬШЕ, 
                        # чем у того, что мы нашли ранее.
                        # Так мы найдем точку, повторенную 100 раз, даже если перед ней нашли тег, повторенный 6 раз.
                        if actual_count > max_reps_found:
                            max_reps_found = actual_count
                            best_repeat_candidate = (repeated_pattern, actual_count, pattern_len == 1)
                        
                        # Мы НЕ делаем break, чтобы проверить все варианты длин
                except re.error: 
                    continue
            
            if best_repeat_candidate:
                result_data['repeat_data'] = best_repeat_candidate # <-- СЫРОЕ ДАННОЕ: ('a', 15, True)

            # --- 6. Недоперевод ---
            if text_trans:
                # Тут логика сложная, поэтому список слов собираем сразу, 
                # но фильтровать его наличие будем в UI
                untranslated_words_to_highlight = []
                single_word_exceptions = {w for w in self.word_exceptions if ' ' not in w}
                phrase_exceptions = [p for p in self.word_exceptions if ' ' in p]; phrase_exceptions.sort(key=len, reverse=True)

                temp_text_trans = text_trans
                for phrase in phrase_exceptions:
                    pattern = r'\b' + re.escape(phrase) + r'\b'
                    temp_text_trans = re.sub(pattern, ' ', temp_text_trans, flags=re.IGNORECASE)
                
                no_cyrillic_text = re.sub(r'[а-яА-ЯёЁ]+', ' ', temp_text_trans)
                pure_residue_text = re.sub(r'[\W\d_]+', ' ', no_cyrillic_text)
                
                for word in pure_residue_text.split():
                    is_cjk = re.search(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]', word)
                    if len(word) < 2 and not is_cjk: continue
                    if word.lower() not in single_word_exceptions:
                        untranslated_words_to_highlight.append(word)

                if untranslated_words_to_highlight:
                    result_data['untranslated_words'] = sorted(list(set(untranslated_words_to_highlight)), key=len, reverse=True)

        except Exception as e:
            reasons.add("Ошибка парсинга"); print(f"Ошибка парсинга для {result_data['path']}: {e}")
            detected_keys.add('parsing_error')
        # --- КОНЕЦ БЛОКА TRY/EXCEPT ---

        if "<!-- RESTORED_IMAGE_WARNING -->" in translated_content:
            custom_tags_found.append("<!-- RESTORED_IMAGE_WARNING -->")
            detected_keys.add('restored_image')
        
        result_data['detected_keys'] = detected_keys
        if structural_errors: result_data['structural_errors'] = structural_errors
        if custom_tags_found: result_data.setdefault('structural_errors', {})['custom_tags'] = custom_tags_found

        if reasons:
            result_data['critical_reasons'] = list(reasons) # Сохраняем отдельно
        
        return result_data
    
    def fast_stat_count(self, text):
        """
        Возвращает кортеж: (кол-во цифр, взвешенное кол-во знаков препинания).
        Использует обновленные regex для CJK и специфичных русских конструкций (тире-прерывания).
        """
        # Считаем группы цифр (предполагаем, что REGEX_DIGITS определен где-то глобально, например r'\d+')
        digits = len(REGEX_DIGITS.findall(text))
        
        punct = 0
        
        # --- 1. Логика для ! и ? (Эмоциональная окраска) ---
        # Группируем с пробелами, чтобы "!!!", "?!", "?.." считались одной эмоциональной группой
        bq_groups = REGEX_BANG_QUEST.findall(text)
        for group in bq_groups:
            # Считаем реальные знаки внутри группы, игнорируя пробелы
            real_chars_count = len([c for c in group if not c.isspace()])
            
            if real_chars_count > 1:
                punct += 2  # Сильная эмоция (много знаков)
            elif real_chars_count == 1:
                punct += 1  # Обычная эмоция
    
        # --- 2. Логика для точек (Концы предложений) ---
        # Группа точек (., . . ., 。) считается за 1 структурный конец
        punct += len(REGEX_DOTS.findall(text))
        
        # --- 3. Логика для многоточий и прерываний (Тире перед знаком) ---
        # REGEX_ELLIPSIS теперь захватывает:
        # а) Обычные многоточия (…)
        # б) Тире, стоящие ПЕРЕД знаками препинания (—!, —., —»)
        # Важно: Благодаря Lookahead в регулярке, само тире считается здесь, 
        # а следующий за ним знак (! или .) будет посчитан в своих блоках (п.1 или п.2).
        # Итог: "—!" даст +1 тут и +1 в блоке BangQuest.
        punct += len(REGEX_ELLIPSIS.findall(text))
        
        # --- 4. Логика для запятых (Сложность предложения) ---
        # Включает азиатские каплевидные (、) и полноширинные (，)
        punct += len(REGEX_COMMAS.findall(text))

        # --- 5. Логика для двоеточий и точек с запятой (Структура) ---
        # Добавлено, так как мы определили REGEX_COLONS_SEMIS
        # Считаем их по 1 баллу, как сильные разделители
        punct += len(REGEX_COLONS_SEMIS.findall(text))

        return digits, punct
    
    def _create_structural_fingerprint(self, soup):
        fp = {'headings': {}, 'images': len(soup.find_all('img')), 'links': len(soup.find_all('a')), 'lists': len(soup.find_all(['ol', 'ul']))}
        for h_tag in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
            fp['headings'][h_tag.name] = fp['headings'].get(h_tag.name, 0) + 1
        return fp
    
    def _calculate_combined_deviation(self, orig_p, trans_p, dig_o, dig_t, punct_o, punct_t):
        """
        Рассчитывает отклонения и возвращает победителя: (float_value, str_label).
        str_label: 'Абзац', 'Цифра', 'Пункт' или '' если нет отклонений.
        """
        candidates = []
        
        # 1. Абзацы (только уменьшение)
        if orig_p > 5: 
            if trans_p < orig_p: 
                val = abs(orig_p - trans_p) / orig_p
                candidates.append((val, "Абзац"))

        # 2. Цифры (любое сильное отклонение)
        if dig_o > 5: 
            val = 0.0
            if dig_o > 0 and abs(dig_o - dig_t) / dig_o > 0.1: 
                val = abs(dig_o - dig_t) / dig_o
            if val > 0:
                candidates.append((val, "Цифра"))

        # 3. Пунктуация (только уменьшение)
        if punct_o > 10: 
            if punct_t < punct_o * 0.9: 
                val = (punct_o - punct_t) / punct_o
                candidates.append((val, "Пункт"))
        
        # Если кандидатов нет, возвращаем 0
        if not candidates:
            return 0.0, ""
            
        # Сортируем по величине отклонения (по убыванию) и берем первого
        best_match = max(candidates, key=lambda x: x[0])
        return best_match
        
    def _count_paragraph_equivalents(self, html_content):
            """
            Подсчитывает количество эквивалентов параграфа.
            Каждый тег <p> считается за один.
            Каждая последовательность из двух и более тегов <br> также считается за один.
            """
            content = html_content.lower()
            
            # 1. Считаем количество открывающих тегов <p>
            p_count = content.count('<p')
            
            # 2. Считаем количество "разрывов абзаца" с помощью <br>
            #    Ищем последовательности из двух или более <br> тегов,
            #    разделенных необязательными пробелами.
            #    (r'(<br[^>]*?>\s*){2,}') находит <br><br>, <br/> <br>, <br> <br/> и т.д.
            double_br_pattern = re.compile(r'(<br[^>]*?>\s*){2,}')
            br_breaks = double_br_pattern.findall(content)
            br_break_count = len(br_breaks)
            
            # 3. Суммируем результаты
            return p_count + br_break_count

    def run(self):
        suspicious_count = 0
        total_scanned = 0
        
        try:
            project_manager = self.project_manager
            ordered_originals, _ = get_epub_chapter_order(self.original_epub_path, return_method=True)
            
            files_to_process = []
            
            # 1. Сбор кандидатов
            for internal_path in ordered_originals:
                # --- ИЗМЕНЕНИЕ: Фильтрация по белому списку, если он задан ---
                if self.files_to_scan and internal_path not in self.files_to_scan:
                    continue
                    
                versions = project_manager.get_versions_for_original(internal_path)
                for suffix, rel_path in versions.items():
                    if (suffix == '_validated.html') and not self.config.get('revalidate_ok', False):
                        continue
                    files_to_process.append({'internal_html_path': internal_path, 'rel_path': rel_path})

            total_to_scan = len(files_to_process)

            with zipfile.ZipFile(open(self.original_epub_path, 'rb'), 'r') as epub_zip:
                epub_namelist = set(epub_zip.namelist())
                for i, file_info in enumerate(files_to_process):
                    if not self._is_running: break
                    
                    self.progress_update.emit(os.path.basename(file_info['rel_path']), i + 1, total_to_scan)

                    try:
                        internal_html_path = file_info['internal_html_path']
                        rel_path = file_info['rel_path']

                        if internal_html_path not in epub_namelist:
                            continue

                        original_content = epub_zip.read(internal_html_path).decode('utf-8', errors='ignore')
                        version_path = os.path.join(self.translated_folder, rel_path)
                        
                        if not os.path.exists(version_path):
                            continue
                            
                        with open(version_path, 'r', encoding='utf-8') as f:
                            translated_content = f.read()

                        # Подгрузка validated версии для сравнения (если есть)
                        all_versions = project_manager.get_versions_for_original(internal_html_path)
                        validated_content = None
                        if '_validated.html' in all_versions:
                            v_path = os.path.join(self.translated_folder, all_versions['_validated.html'])
                            if os.path.exists(v_path):
                                with open(v_path, 'r', encoding='utf-8') as f:
                                    validated_content = f.read()

                        result_data = {
                            'path': version_path, 'internal_html_path': internal_html_path,
                            'original_html': original_content, 'translated_html': translated_content,
                            'status': 'neutral' # По умолчанию нейтральный, анализ может изменить
                        }
                        if validated_content: result_data['validated_content'] = validated_content
                        
                        # Запускаем тяжелый анализ
                        result = self._analyze_html_content(original_content, translated_content, result_data)
                        
                        total_scanned += 1
                        
                        # --- ИЗМЕНЕНИЕ: Мы теперь отправляем результат ВСЕГДА, чтобы обновить строку таблицы
                        # Если проблем нет, result вернется с populated полями, но без error-ключей
                        if result:
                            # Проверяем, есть ли реальные проблемы для счетчика
                            if result.get('structural_errors') or result.get('critical_reasons') or 'untranslated_words' in result:
                                suspicious_count += 1
                            self.result_found.emit(result)
                            
                    except Exception as e:
                        print(f"Ошибка при обработке '{file_info.get('rel_path')}': {e}")

        except Exception as e:
            print(f"Критическая ошибка в потоке валидации: {e}")
        
        self.analysis_finished.emit(total_scanned, suspicious_count)
    
    def stop(self):
        self._is_running = False

# --- Главное окно диалога ---
class TranslationValidatorDialog(QDialog):
    
    RATIO_PRESETS = {
        "Алфавитный (A -> A)": (0.7, 1.8, "Стандартное соотношение (En/Fr/De -> Ru)"),
        "Иероглифический (象 -> A)": (2.5, 6.5, "Соотношение для плотных языков (Zh/Jp -> Ru)"),
        "Медиана ±20%": (-1.0, 0.20, "Отклонение от медианного значения по всем главам"),
        "Медиана ±25%": (-1.0, 0.25, "Отклонение от медианного значения по всем главам"),
        "Медиана ±30%": (-1.0, 0.30, "Отклонение от медианного значения по всем главам")
    }
    

    def __init__(self, translated_folder, original_epub_path, parent=None, retry_enabled=True, project_manager=None):
        super().__init__(parent)
        
        self.setWindowFlags(
            Qt.WindowType.Dialog |
            Qt.WindowType.WindowMinimizeButtonHint |
            Qt.WindowType.WindowMaximizeButtonHint |
            Qt.WindowType.WindowCloseButtonHint
        )
        
        self.translated_folder = translated_folder
        self.original_epub_path = original_epub_path
        self.project_manager = project_manager # <-- ДОБАВЛЯЕМ ЭТУ СТРОКУ
        self.retry_is_available = retry_enabled
        self.analysis_thread = None
        self.results_data = {} 
        self.is_code_view = False
        self.untranslated_found_count = 0
        
        app = QtWidgets.QApplication.instance()
        self.settings_manager = app.get_settings_manager() if hasattr(app, 'settings_manager') else None
        self.version = ""
        if app and app.global_version:
            self.version = app.global_version
        
        # Храним текущий порядок сортировки
        self.current_sort_col = 0
        self.current_sort_order = Qt.SortOrder.AscendingOrder
        
        self.initUI()
        
        # Настройка "раскрасчиков"
        self.html_highlighter_orig = HtmlHighlighter(self.view_original.document())
        self.html_highlighter_trans = HtmlHighlighter(self.view_translated.document())
        self.punctuation_highlighter_orig = PunctuationHighlighter(self.view_original.document())
        self.punctuation_highlighter_trans = PunctuationHighlighter(self.view_translated.document())
        self._update_highlighters() # Вызываем один раз для установки начального состояния
        
        self.is_comparing_validated = False
        self.validated_content_cache = {}
        
        self._perform_initial_cjk_scan()

    def initUI(self):
        """Главный метод-оркестратор, собирающий UI из частей."""
        
        self.setWindowTitle(f'Инструмент проверки переводов {self.version}')
        self.setGeometry(150, 150, 1200, 800)
        
        main_layout = QVBoxLayout(self)

        main_layout.addWidget(self._create_source_group())
        main_layout.addWidget(self._create_main_settings_group()) # <-- Теперь это главный контейнер
        
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self._create_results_widget())
        splitter.addWidget(self._create_comparison_widget())
        splitter.setSizes([300, 500])
        main_layout.addWidget(splitter)

        main_layout.addLayout(self._create_bottom_buttons())

        # Подключаем сигналы ПОСЛЕ создания всех виджетов
        self.check_length_ratio.stateChanged.connect(lambda state: self.ratio_presets_combo.setEnabled(bool(state)))
        self.check_paragraph_size.toggled.connect(self.max_paragraph_spinbox.setEnabled)
        self.check_simplification.toggled.connect(self.simplification_threshold_spinbox.setEnabled)
        self.check_repeating_chars.toggled.connect(self.repeating_chars_spinbox.setEnabled)
        
        # --- ПОДКЛЮЧЕНИЕ ЖИВОЙ ФИЛЬТРАЦИИ (ДОБАВИТЬ ЭТОТ БЛОК) ---
        # 1. Чекбоксы
        for chk in [self.check_structure, self.check_length_ratio, self.check_untranslated, 
                    self.check_simplification, self.check_repeating_chars, self.check_paragraph_size,
                    self.check_show_all]:
            chk.clicked.connect(self.reapply_filters)
            
        # 2. Спинбоксы (пороги) - используем valueChanged
        self.max_paragraph_spinbox.valueChanged.connect(self.reapply_filters)
        self.simplification_threshold_spinbox.valueChanged.connect(self.reapply_filters)
        self.repeating_chars_spinbox.valueChanged.connect(self.reapply_filters)
        
        # 4. Комбобокс
        self.ratio_presets_combo.currentIndexChanged.connect(self.reapply_filters)

        self._set_tooltips()
        
        self.dirty_files = set()
        self.path_row_map = {} 
        
        # Спец-сигнал для чекбокса "Включить готовые"
        try: self.check_revalidate_ok.clicked.disconnect() 
        except: pass
        self.check_revalidate_ok.clicked.connect(self._on_revalidate_ok_toggled)

        # --- ЛЕНИВАЯ ЗАГРУЗКА ---
        # 1. Сначала блокируем кнопку и пишем статус
        self.btn_analyze.setEnabled(False)
        self.btn_analyze.setText("⏳ Загрузка списка...")
        self.lbl_status.setText("Построение таблицы файлов...")
        
        # 2. Запускаем построение таблицы через 100мс ПОСЛЕ того, как окно отрисуется
        QtCore.QTimer.singleShot(150, self._populate_initial_table)

    def _create_main_settings_group(self):
        """Создает главный контейнер и располагает в нем группы слева направо (v5)."""
        main_group = QGroupBox("Настройки проверки и Поиск по содержимому")
        main_layout = QHBoxLayout(main_group)
        main_group.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Fixed)

        # Создаем объединенную панель для первых двух колонок
        main_layout.addWidget(self._create_combined_checks_panel()) 
        
        # Остальные группы остаются без изменений
        main_layout.addWidget(self._create_group3_options())
        main_layout.addWidget(self._create_group4_custom_filter(), 1)
        main_layout.addWidget(self._create_group5_actions())

        return main_group
    
    def _populate_initial_table(self):
        """
        Заполняет таблицу всеми файлами.
        Готовые файлы добавляются, но скрываются, если чекбокс выключен.
        Все добавленные файлы помечаются как требующие анализа.
        Вызывается отложенно через QTimer.
        """
        # --- 1. БЛОКИРОВКА ИНТЕРФЕЙСА ---
        # Чтобы пользователь не нажал ничего лишнего, пока таблица строится
        # self.table_results.setSortingEnabled(False)
        self.btn_analyze.setEnabled(False)
        self.btn_sync_project.setEnabled(False)
        self.btn_apply_changes.setEnabled(False)
        self.check_revalidate_ok.setEnabled(False)
        self.check_show_all.setEnabled(False)
        self.ratio_presets_combo.setEnabled(False)
        
        # Очистка
        self.table_results.setRowCount(0)
        self.results_data.clear()
        self.path_row_map.clear()
        self.dirty_files.clear()
        
        if not self.project_manager: 
            self.lbl_status.setText("Ошибка: Менеджер проекта не найден.")
            self.btn_analyze.setText("Ошибка")
            return

        ordered_originals, _ = get_epub_chapter_order(self.original_epub_path, return_method=True)
        row_pos = 0
        
        from ...api import config as api_config
        
        for internal_path in ordered_originals:
            # Даем интерфейсу "дышать" каждые 50 файлов
            if row_pos % 50 == 0:
                QApplication.processEvents()

            versions = self.project_manager.get_versions_for_original(internal_path)
            if not versions: continue

            is_validated_present = '_validated.html' in versions
            
            if is_validated_present:
                target_rel_path = versions['_validated.html']
            else:
                target_rel_path = next(iter(versions.values()))
            
            full_path = os.path.join(self.translated_folder, target_rel_path)
            
            # Данные
            data_placeholder = {
                'path': full_path,
                'internal_html_path': internal_path,
                'status': 'neutral',
                'translated_html': "", 
                'len_orig': 0, 'len_trans': 0,
                'is_validated_file': is_validated_present
            }
            
            self.table_results.insertRow(row_pos)
            
            # Колонка 0
            display_text = f"{os.path.basename(internal_path)}"
            if is_validated_present: display_text += " [Готов]"
            else: display_text += f" -> {os.path.basename(target_rel_path)}"

            display_path_item = SortableChapterItem(display_text, internal_path)
            display_path_item.setData(Qt.ItemDataRole.UserRole, is_validated_present)
            self.table_results.setItem(row_pos, 0, display_path_item)
            
            # Колонка 1, 2, 3
            self.table_results.setItem(row_pos, 1, QTableWidgetItem(""))
            self.table_results.setItem(row_pos, 2, NumericTableWidgetItem("- | -"))
            self.table_results.setItem(row_pos, 3, QTableWidgetItem("Ожидание..."))
            
            self.results_data[row_pos] = data_placeholder
            self.path_row_map[internal_path] = row_pos
            
            # Помечаем как "Грязный" (нужен анализ)
            self.dirty_files.add(internal_path)
            
            # Скрываем строку сразу, если это готовый файл, а галочка выключена
            if is_validated_present and not self.check_revalidate_ok.isChecked():
                self.table_results.setRowHidden(row_pos, True)
            
            row_pos += 1
            
        
        # --- 2. РАЗБЛОКИРОВКА ИНТЕРФЕЙСА ---
        # self.table_results.setSortingEnabled(True)
        self.check_revalidate_ok.setEnabled(True)
        self.check_show_all.setEnabled(True)
        self.ratio_presets_combo.setEnabled(True)
        self.btn_sync_project.setEnabled(True)
        self.btn_apply_changes.setEnabled(True)
        
        # --- 3. ФИНАЛИЗАЦИЯ ---
        self.lbl_status.setText(f"Файлы загружены. Ожидают проверки: {len(self.dirty_files)}")
        
        # Применяем фильтры (скрываем лишнее, если Show All выключен)
        self.reapply_filters()
        
        # Рассчитываем состояние главной кнопки "Анализ"
        # Она разблокируется внутри этого метода, если есть что проверять
        self._update_analyze_button_state()




    def _create_combined_checks_panel(self):
        """Создает единую панель для левых колонок с идеальным выравниванием и ЖИВОЙ фильтрацией."""
        container = QWidget()
        grid = QGridLayout(container)
        grid.setVerticalSpacing(4)
        
        self.check_structure = QCheckBox("Структура (теги, заголовки)")
        self.check_length_ratio = QCheckBox("Соотношение длин")
        
        untranslated_layout = QHBoxLayout()
        untranslated_layout.setContentsMargins(0,0,0,0); untranslated_layout.setSpacing(5)
        self.check_untranslated = QCheckBox()
        self.btn_fix_untranslated = QPushButton("Недоперевод (лат./иер.)")
        self.btn_fix_untranslated.clicked.connect(self._open_untranslated_fixer)
        self.btn_fix_untranslated.setEnabled(False)
        untranslated_layout.addWidget(self.check_untranslated)
        untranslated_layout.addWidget(self.btn_fix_untranslated)
        untranslated_layout.addStretch()
    
        grid.addWidget(self.check_structure, 0, 0)
        grid.addLayout(untranslated_layout, 1, 0)
        grid.addWidget(self.check_length_ratio, 2, 0)
        
        self.check_simplification = QCheckBox("Отклонение >")
        self.simplification_threshold_spinbox = QtWidgets.QSpinBox(); self.simplification_threshold_spinbox.setRange(10, 100); self.simplification_threshold_spinbox.setValue(30); self.simplification_threshold_spinbox.setSuffix(" %")
        
        self.check_repeating_chars = QCheckBox("Повтор символов >")
        self.repeating_chars_spinbox = QtWidgets.QSpinBox(); self.repeating_chars_spinbox.setRange(5, 100); self.repeating_chars_spinbox.setValue(10)
        
        self.check_paragraph_size = QCheckBox("Размер абзацев >")
        self.max_paragraph_spinbox = QtWidgets.QSpinBox(); self.max_paragraph_spinbox.setRange(300, 50000); self.max_paragraph_spinbox.setValue(1000); self.max_paragraph_spinbox.setSingleStep(50)
        
        grid.addWidget(self.check_simplification, 0, 1)
        grid.addWidget(self.simplification_threshold_spinbox, 0, 2)
        grid.addWidget(self.check_repeating_chars, 1, 1)
        grid.addWidget(self.repeating_chars_spinbox, 1, 2)
        grid.addWidget(self.check_paragraph_size, 2, 1)
        grid.addWidget(self.max_paragraph_spinbox, 2, 2)
    
        self.check_structure.setChecked(True)
        self.check_untranslated.setChecked(True)
        self.check_length_ratio.setChecked(True)
        self.check_simplification.setChecked(True)
        self.check_repeating_chars.setChecked(False); self.repeating_chars_spinbox.setEnabled(False)
        self.check_paragraph_size.setChecked(False); self.max_paragraph_spinbox.setEnabled(False)
        
        # --- НОВОЕ: ПОДКЛЮЧЕНИЕ ЖИВОЙ ФИЛЬТРАЦИИ ---
        # При клике по чекбоксу мы не запускаем анализ заново, а просто пересчитываем видимость
        for checkbox in [self.check_structure, self.check_untranslated, self.check_length_ratio, 
                         self.check_simplification, self.check_repeating_chars, self.check_paragraph_size]:
            checkbox.clicked.connect(self.reapply_filters)
            
        return container
    
    def _create_group3_options(self):
        """Группа 3: "Показать все" и пресеты."""
        container = QWidget()
        layout = QVBoxLayout(container)
        self.check_show_all = QCheckBox("Показать все файлы")
        

        self.check_revalidate_ok = QCheckBox("Включить 'Готовые' файлы")

        
        self.ratio_presets_combo = QComboBox()
        for i, text in enumerate(self.RATIO_PRESETS.keys()): self.ratio_presets_combo.addItem(text)
        
        layout.addStretch(1)
        layout.addWidget(self.check_show_all)
        layout.addStretch(1)
        layout.addWidget(self.check_revalidate_ok) # <-- Добавляем новый флажок
        layout.addStretch(1)
        layout.addWidget(self.ratio_presets_combo)
        layout.addStretch(1)
        
        self.check_revalidate_ok.clicked.connect(self._update_analyze_button_state)
        self.check_show_all.clicked.connect(self.reapply_filters)
        
        return container

    def _create_group4_custom_filter(self):
        """Группа 4: Поиск по регулярному выражению."""
        container = QWidget()
        layout = QGridLayout(container)
        self.regex_edit = QtWidgets.QLineEdit(); self.regex_edit.setPlaceholderText("Введите текст или регулярное выражение…")
        self.btn_open_large_editor = QPushButton("…"); self.btn_open_large_editor.setFixedSize(28, 28); self.btn_open_large_editor.clicked.connect(self._open_large_text_editor)
        regex_input_layout = QHBoxLayout(); regex_input_layout.setContentsMargins(0,0,0,0); regex_input_layout.setSpacing(5)
        regex_input_layout.addWidget(self.regex_edit); regex_input_layout.addWidget(self.btn_open_large_editor)
        
        self.filter_mode_contains = QtWidgets.QRadioButton("Содержит"); self.filter_mode_contains.setChecked(True)
        self.filter_mode_not_contains = QtWidgets.QRadioButton("НЕ содержит")
        self.check_case_sensitive = QCheckBox("Учитывать регистр")
        
        self.btn_apply_filter = QPushButton("🔍 Применить"); self.btn_apply_filter.clicked.connect(self._apply_custom_filter)
        self.btn_clear_filter = QPushButton("Сбросить"); self.btn_clear_filter.clicked.connect(self._clear_custom_filter)
        
        layout.addLayout(regex_input_layout, 0, 0, 1, 2)
        modes_layout = QHBoxLayout(); modes_layout.addWidget(self.filter_mode_contains); modes_layout.addWidget(self.filter_mode_not_contains); modes_layout.addWidget(self.check_case_sensitive)
        layout.addLayout(modes_layout, 1, 0, 1, 2)
        buttons_layout = QHBoxLayout(); buttons_layout.addWidget(self.btn_apply_filter); buttons_layout.addWidget(self.btn_clear_filter)
        layout.addLayout(buttons_layout, 2, 0, 1, 2)
        
        layout.setRowStretch(3, 1)
        return container
    
    def _create_group5_actions(self):
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10,0,0,0)
        
        # ↓↓↓ ДОБАВЬТЕ ЭТУ КНОПКУ ↓↓↓
        self.btn_sync_project = QPushButton("🔄 Сверить проект");
        self.btn_sync_project.clicked.connect(self._run_project_sync_and_reload)
    
        self.btn_analyze = QPushButton("🚀 Начать проверку"); self.btn_analyze.clicked.connect(self.start_analysis)
        self.btn_exceptions_manager = QPushButton("Списки исключений…"); self.btn_exceptions_manager.clicked.connect(self._open_exceptions_manager)
        
        layout.addStretch()
        layout.addWidget(self.btn_sync_project) # <-- Добавляем в layout
        layout.addWidget(self.btn_analyze)
        layout.addWidget(self.btn_exceptions_manager)
        layout.addStretch()
        return container
    
    def _set_tooltips(self):
        """Централизованно устанавливает все всплывающие подсказки для виджетов."""
        
        # Группа 1: Основные проверки
        structure_tooltip = "Проверяет соответствие ключевых тегов (<html>, <body>), заголовков (<h1>-<h6>), изображений и списков.\nТакже проверяет баланс тегов <p>."
        self.check_structure.setToolTip(structure_tooltip.replace('<', '&lt;').replace('>', '&gt;'))
        
        self.check_untranslated.setToolTip("Включить/выключить проверку на недоперевод.")
        self.btn_fix_untranslated.setToolTip("Ищет в переводе латинские слова (3+ букв) и иероглифы, которые также присутствуют в оригинале.\nОткрывает диалог для пакетного исправления, если что-то найдено.")
        
        self.check_length_ratio.setToolTip("Сравнивает соотношение длины текста в переводе к оригиналу. Помогает выявить 'пустые' или слишком 'водянистые' переводы.")
        
        # Группа 2: Настраиваемые проверки
        simplification_tooltip = "Проверяет, не было ли утеряно форматирование. Срабатывает, если количество тегов <p> и <br>\nв переводе отличается от оригинала больше, чем на указанный процент."
        self.check_simplification.setToolTip(simplification_tooltip.replace('<', '&lt;').replace('>', '&gt;'))
        self.simplification_threshold_spinbox.setToolTip("Максимально допустимое отклонение (по абзацам, цифрам или пунктуации) от оригинала.\n"
                                                     "Например, 30% позволит пропустить небольшие погрешности.")
        
        self.check_repeating_chars.setToolTip("Искать последовательности из N и более одинаковых символов подряд.\nПомогает найти 'мусорные' данные и невидимые комбинируемые символы.")
        self.repeating_chars_spinbox.setToolTip("Минимальное количество одинаковых символов подряд для срабатывания.")

        self.check_paragraph_size.setToolTip("Искать главы, где есть слишком большие абзацы, которые могут быть\nтрудными для чтения или результатом ошибки (потеря тегов).")
        self.max_paragraph_spinbox.setToolTip("Максимально допустимый размер абзаца в символах для удобочитаемости.\nРекомендуемые значения: 750-1500.")
        
        # Группа 3: Опции
        self.check_show_all.setToolTip("Показывает в таблице все найденные главы, а не только те, в которых обнаружены проблемы.")
        self.check_revalidate_ok.setToolTip("Добавляет в проверку файлы, которые уже помечены как 'Готовые' (с суффиксом _validated.html).\nПолезно для повторной проверки всего проекта по новым правилам.")
        self.ratio_presets_combo.setToolTip("Выбор пресета устанавливает ожидаемый диапазон соотношения длин (Оригинал vs Перевод).\n\n"
                                           "• 'Алфавитный': Для пар типа Английский -> Русский (коэффициент ~1.0).\n"
                                           "• 'Иероглифический': Для пар типа Китайский/Японский -> Русский (текст расширяется в 3-5 раз).\n"
                                           "• 'Медианы': Ищет отклонение от среднего соотношения длин на процент.")

        # Группа 4: Поиск
        self.btn_open_large_editor.setToolTip("Открыть большой редактор для ввода")
        
        # Группа 5: Действия
        self.btn_exceptions_manager.setToolTip("Открыть менеджер для настройки списков слов (бренды, имена, термины),\nкоторые должны игнорироваться при проверке на 'Недоперевод'.")
    
    
    def _create_source_group(self):
        source_group = QGroupBox("Источники анализа")
        source_layout = QVBoxLayout(source_group)
        source_layout.addWidget(QLabel(f"<b>Исходный EPUB:</b> {self.original_epub_path}"))
        source_layout.addWidget(QLabel(f"<b>Папка с переводами:</b> {self.translated_folder}"))
        return source_group

    def _on_revalidate_ok_toggled(self):
        """
        Показывает или скрывает готовые файлы.
        Если мы их показываем, и они непроверены — загорится кнопка Анализа.
        """
        show_validated = self.check_revalidate_ok.isChecked()
        
        for row in range(self.table_results.rowCount()):
            if row not in self.results_data: continue
            
            is_validated = self.results_data[row].get('is_validated_file', False)
            
            if is_validated:
                # Показываем или скрываем строку
                self.table_results.setRowHidden(row, not show_validated)
        
        # Пересчитываем фильтры (на случай если Show All выключен)
        self.reapply_filters()
        
        # Обновляем состояние кнопки "Начать проверку"
        # Она сама проверит, есть ли видимые dirty файлы
        self._update_analyze_button_state()

    def _create_results_widget(self):
        results_widget = QWidget()
        results_layout = QVBoxLayout(results_widget)
        results_layout.setContentsMargins(0,0,0,0)
        top_bar_layout = QHBoxLayout()
        self.btn_select_all = QPushButton("❏ Выделить всё"); self.btn_select_all.clicked.connect(self.select_all_rows)
        top_bar_layout.addWidget(self.btn_select_all); top_bar_layout.addWidget(QLabel("Найденные файлы с проблемами (двойной клик для открытия):")); top_bar_layout.addStretch()
        self.btn_mark_delete = QPushButton("🗑️ Пометить на удаление"); self.btn_mark_delete.clicked.connect(lambda: self.mark_selected_rows('delete'))
        self.btn_mark_ok = QPushButton("✅ Пометить как готовый"); self.btn_mark_ok.clicked.connect(lambda: self.mark_selected_rows('mark_ok'))
        self.btn_retry_selected = QPushButton("🔄 Пометить к переотправке"); self.btn_retry_selected.clicked.connect(lambda: self.mark_selected_rows('retry')); self.btn_retry_selected.setVisible(self.retry_is_available)
        self.btn_reset_marks = QPushButton("🚫 Снять пометки"); self.btn_reset_marks.clicked.connect(self.reset_selected_marks)
        self.btn_prev_item = QPushButton("↑"); self.btn_prev_item.setFixedSize(28, 28); self.btn_prev_item.clicked.connect(self._go_to_previous_item); self.btn_prev_item.setEnabled(False)
        self.btn_next_item = QPushButton("↓"); self.btn_next_item.setFixedSize(28, 28); self.btn_next_item.clicked.connect(self._go_to_next_item); self.btn_next_item.setEnabled(False)
        for btn in [self.btn_mark_delete, self.btn_mark_ok, self.btn_retry_selected, self.btn_reset_marks, self.btn_prev_item, self.btn_next_item]:
            top_bar_layout.addWidget(btn)
        results_layout.addLayout(top_bar_layout)
        
        
        
        
        self.table_results = QTableWidget()
        self.table_results.setItemDelegateForColumn(0, ChapterStatusDelegate(self.table_results))
        self.table_results.setColumnCount(4); self.table_results.setHorizontalHeaderLabels(["Исходный файл в EPUB", "Проблемы", "Длина (Ориг|Перевод)", "Статус"])
        
        header = self.table_results.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch); header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents); header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents); header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        
        # --- ВАЖНО: Отключаем встроенную сортировку и подключаем свою ---
        self.table_results.setSortingEnabled(False)
        header.setSectionsClickable(True)
        header.sectionClicked.connect(self._on_header_clicked)
        # ---------------------------------------------------------------

        self.table_results.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows); self.table_results.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection); self.table_results.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_results.itemSelectionChanged.connect(self.on_selection_changed); self.table_results.itemDoubleClicked.connect(self.open_file_external)
        results_layout.addWidget(self.table_results)
        
        return results_widget

    def _create_comparison_widget(self):
        comparison_widget = QWidget()
        comparison_layout = QVBoxLayout(comparison_widget)
        comparison_layout.setContentsMargins(0,5,0,0)
        comparison_top_bar = QHBoxLayout()
        comparison_top_bar.addWidget(QLabel("Сравнение (слева - оригинал/готовый, справа - перевод, **редактируемый**):")); comparison_top_bar.addStretch()
        self.btn_toggle_compare = QPushButton("Сравнить с Готовой версией"); self.btn_toggle_compare.setCheckable(True); self.btn_toggle_compare.toggled.connect(self.on_compare_toggle); self.btn_toggle_compare.setVisible(False)
        self.btn_toggle_code_view = QPushButton("Показать код"); self.btn_toggle_code_view.clicked.connect(self.toggle_code_view); self.btn_toggle_code_view.setEnabled(False)
        self.btn_save_changes = QPushButton("💾 Сохранить изменения"); self.btn_save_changes.clicked.connect(self.save_changes); self.btn_save_changes.setEnabled(False)
        for btn in [self.btn_toggle_compare, self.btn_toggle_code_view, self.btn_save_changes]:
            comparison_top_bar.addWidget(btn)
        comparison_layout.addLayout(comparison_top_bar)
        
        self.comparison_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.view_original = QTextEdit(); self.view_original.setReadOnly(True)
        self.view_translated = QTextEdit(); self.view_translated.setReadOnly(True); self.view_translated.textChanged.connect(self.on_text_edited)
        self.comparison_splitter.addWidget(self.view_original); self.comparison_splitter.addWidget(self.view_translated)
        comparison_layout.addWidget(self.comparison_splitter)
        
        return comparison_widget

    def _create_bottom_buttons(self):
        bottom_layout = QHBoxLayout()
        self.lbl_status = QLabel("Готов к проверке.")
        
        self.btn_consistency = QPushButton("🔍 Согласованность (AI)")
        self.btn_consistency.clicked.connect(self._on_consistency_check)
        self.btn_consistency.setStyleSheet(
            "background-color: #673AB7; color: white; padding: 5px 10px;")
        
        self.btn_apply_changes = QPushButton("✅ Применить действия"); self.btn_apply_changes.clicked.connect(self.apply_changes)
        self.btn_send_to_retry = QPushButton("▶️ Отправить на перевод и закрыть"); self.btn_send_to_retry.clicked.connect(self.request_retry_translation); self.btn_send_to_retry.setVisible(self.retry_is_available)
        self.btn_back = QPushButton("Закрыть"); self.btn_back.clicked.connect(self.close)
        
        bottom_layout.addWidget(self.lbl_status, 1)
        bottom_layout.addWidget(self.btn_consistency)
        bottom_layout.addWidget(self.btn_apply_changes)
        bottom_layout.addWidget(self.btn_send_to_retry)
        bottom_layout.addWidget(self.btn_back)
        
        return bottom_layout


    # --- ДОБАВЬТЕ ЭТОТ НОВЫЙ МЕТОД ---
    def _open_large_text_editor(self):
        """Открывает диалог для удобного редактирования текста/регулярного выражения."""
        dialog = LargeTextInputDialog(
            initial_text=self.regex_edit.text(),
            parent=self,
            title="Редактор поискового запроса"
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.regex_edit.setText(dialog.get_text())

    def _on_consistency_check(self):
        """Запуск нового режима проверки согласованности."""
        from .consistency_checker import ConsistencyValidatorDialog

        if not self.settings_manager:
            QMessageBox.warning(self, "Ошибка", "SettingsManager не доступен.")
            return

        # Загружаем все переведенные главы из проекта
        chapters_to_analyze = []
        
        # Получаем список всех оригиналов
        all_originals = self.project_manager.get_all_originals()
        
        # Прогресс-диалог, так как чтение файлов может занять время
        progress = QProgressDialog("Загрузка глав проекта...", "Отмена", 0, len(all_originals), self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.show()
        
        project_folder = self.project_manager.project_folder
        
        for i, internal_path in enumerate(all_originals):
            if progress.wasCanceled():
                return
            progress.setValue(i)
            
            # Ищем перевод
            versions = self.project_manager.get_versions_for_original(internal_path)
            if not versions:
                continue
                
            # Выбираем версию. Приоритет: пустой суффикс (основная) -> первый попавшийся
            # TODO: Можно добавить выбор версии
            rel_path = versions.get('')
            if not rel_path and versions:
                rel_path = next(iter(versions.values()))
                
            if not rel_path:
                continue
                
            full_path = os.path.join(project_folder, rel_path)
            
            try:
                if os.path.exists(full_path):
                    with open(full_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    
                    chapters_to_analyze.append({
                        'name': os.path.basename(internal_path), # Или full_path
                        'content': content,
                        'path': full_path
                    })
            except Exception as e:
                print(f"Error reading {full_path}: {e}")

        progress.setValue(len(all_originals))

        if not chapters_to_analyze:
            QMessageBox.warning(self, "Предупреждение",
                                "Нет переведенных глав для анализа.")
            return

        dialog = ConsistencyValidatorDialog(
            chapters_to_analyze, 
            self.settings_manager, 
            self,
            project_manager=self.project_manager
        )
        # Принудительно обновляем статистику после загрузки
        if hasattr(dialog, '_update_chunk_stats'):
            dialog._update_chunk_stats()
            
        dialog.exec()


    def _on_header_clicked(self, logical_index):
        """
        Ручная обработка сортировки.
        1. Сортирует визуальные элементы таблицы.
        2. Сразу же перестраивает словари данных, чтобы row 0 соответствовал данным row 0.
        """
        # Переключаем порядок, если кликнули по той же колонке
        if logical_index == self.current_sort_col:
            self.current_sort_order = Qt.SortOrder.DescendingOrder if self.current_sort_order == Qt.SortOrder.AscendingOrder else Qt.SortOrder.AscendingOrder
        else:
            self.current_sort_col = logical_index
            self.current_sort_order = Qt.SortOrder.AscendingOrder
        
        # 1. Визуальная сортировка средствами Qt
        self.table_results.sortItems(logical_index, self.current_sort_order)
        
        # 2. Синхронизация данных с новой визуальной реальностью
        self._sync_data_with_visual_order()
    
    def _get_current_ratio_bounds(self):
        """
        Возвращает (min, max) для текущего пресета.
        Если выбран медианный пресет, вычисляет границы динамически на основе загруженных данных.
        """
        ratio_preset_name = self.ratio_presets_combo.currentText()
        val_a, val_b, _ = self.RATIO_PRESETS.get(ratio_preset_name, (0.7, 1.8, ""))

        # Если первый параметр отрицательный, это признак динамического режима (Медиана)
        if val_a < 0:
            threshold_percent = val_b # Например, 0.10 для 10%
            
            # Собираем все валидные соотношения
            ratios = []
            for data in self.results_data.values():
                # Учитываем только файлы, где есть и оригинал, и перевод
                if data.get('len_orig', 0) > 0 and data.get('len_trans', 0) > 0:
                     ratios.append(data['ratio_value'])
            
            if not ratios:
                return 0.0, 100.0 # Если данных нет, границы максимально широкие
            
            import statistics
            median_val = statistics.median(ratios)
            
            # Рассчитываем границы отклонения от медианы
            min_r = median_val * (1.0 - threshold_percent)
            max_r = median_val * (1.0 + threshold_percent)
            return min_r, max_r
        else:
            # Статический режим
            return val_a, val_b
            
    def _calculate_status_for_data(self, data, override_bounds=None):
        """
        Чистая логика: принимает словарь данных, смотрит на чекбоксы
        и возвращает (список_причин_текстом, визуальный_статус).
        """
        # Считываем текущие настройки порогов
        if override_bounds:
            ratio_min, ratio_max = override_bounds
        else:
            ratio_min, ratio_max = self._get_current_ratio_bounds()
        
        max_paragraph_limit = self.max_paragraph_spinbox.value()
        simplification_limit = self.simplification_threshold_spinbox.value() / 100.0
        repeats_limit = self.repeating_chars_spinbox.value()
        
        # Определяем, какие проверки вообще включены
        check_struct = self.check_structure.isChecked()
        check_ratio = self.check_length_ratio.isChecked()
        check_simpl = self.check_simplification.isChecked()
        check_repeats = self.check_repeating_chars.isChecked()
        check_para = self.check_paragraph_size.isChecked()
        check_untrans = self.check_untranslated.isChecked()

        current_reasons = []
        
        # 1. Критические ошибки
        if 'critical_reasons' in data:
            current_reasons.extend(data['critical_reasons'])
        
        # 2. Структурные ошибки
        if check_struct:
            if 'structural_errors' in data or 'structure_data' in data.get('detected_keys', set()):
                 if data.get('structural_errors'):
                     current_reasons.append("Структурная ошибка")
            if 'restored_image' in data.get('detected_keys', set()):
                current_reasons.append("Восстановлено изображение")

        # 3. Соотношение длин
        if check_ratio:
            val = data.get('ratio_value', 1.0)
            if data.get('len_orig', 0) > 100: 
                if not (ratio_min < val < ratio_max):
                    current_reasons.append(f"Длина ({val:.1f}x)")

        # 4. Размер абзаца
        if check_para:
            largest = data.get('largest_paragraph', 0)
            if largest > max_paragraph_limit:
                current_reasons.append(f"Большой абзац ({largest} симв.)")

        # 5. Отклонение
        if check_simpl:
            combined_deviation = data.get('combined_deviation', 0.0)
            deviation_type = data.get('deviation_type', 'Общ') # Получаем тип, если его нет - 'Общ'
            deviation_threshold = self.simplification_threshold_spinbox.value() / 100.0
            
            if combined_deviation > deviation_threshold:
                # Формат: Откл-Пункт (35%)
                current_reasons.append(f"Откл-{deviation_type} ({combined_deviation*100:.0f}%)")

        # 6. Повторы
        if check_repeats and 'repeat_data' in data:
            pattern, count, is_char = data['repeat_data']
            if count >= repeats_limit:
                display_pattern = f"'{pattern}'" if len(pattern) <= 15 else f"'{pattern[:10]}...'"
                if pattern.strip() == "": display_pattern = "[пробельный шаблон]"
                prefix = "Повтор символа" if is_char else "Повтор шаблона"
                current_reasons.append(f"{prefix} {display_pattern} ({count} раз)")

        # 7. Недоперевод
        if check_untrans and 'untranslated_words' in data:
            current_reasons.append("Недоперевод")

        # Определяем статус
        has_problems = len(current_reasons) > 0
        
        # Сохраняем "ручные" статусы
        current_manual_status = data.get('status', 'neutral')
        if current_manual_status in ['ok', 'delete', 'retry', 'edited']:
            visual_status = current_manual_status
        else:
            visual_status = 'problem' if has_problems else 'neutral'
            
        return current_reasons, visual_status
        
    def _sync_data_with_visual_order(self):
        """
        Критически важный метод.
        Пересобирает self.results_data и self.path_row_map так, чтобы они
        соответствовали текущему порядку строк в таблице.
        Использует internal_path из SortableChapterItem как надежный ключ.
        """
        new_results_data = {}
        new_path_row_map = {}
        
        # Создаем временный индекс для быстрого поиска: { internal_path : data_blob }
        temp_data_lookup = {
            data['internal_html_path']: data 
            for data in self.results_data.values()
        }
        
        for row in range(self.table_results.rowCount()):
            # Берем элемент из первой колонки, так как он хранит internal_path
            item = self.table_results.item(row, 0)
            
            # Проверяем, что это наш кастомный элемент с путем
            if isinstance(item, SortableChapterItem):
                internal_path = item.internal_path
                
                # Если данные для этого пути есть (они должны быть), привязываем их к новой строке
                if internal_path in temp_data_lookup:
                    new_results_data[row] = temp_data_lookup[internal_path]
                    new_path_row_map[internal_path] = row
                else:
                    print(f"Внимание: Потеряна связь с данными для {internal_path} при сортировке.")
        
        # Подменяем старые карты на новые, синхронизированные
        self.results_data = new_results_data
        self.path_row_map = new_path_row_map
        
        # Пересчитываем визуальные фильтры (цвета, скрытие), так как индексы сдвинулись
        self.reapply_filters()

    def _load_default_exceptions(self):
        try:
            base_path = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.abspath('.')
            default_file = os.path.join(base_path, 'config', 'default_word_exceptions.txt')
            if os.path.exists(default_file):
                with open(default_file, 'r', encoding='utf-8') as f:
                    self.default_exceptions_text = f.read()
        except Exception as e:
            print(f"[VALIDATOR_ERROR] Не удалось загрузить стандартный список исключений: {e}")
    
    def reapply_filters(self):
        """
        Динамически пересчитывает статус для ВСЕХ строк.
        Используется ТОЛЬКО при изменении настроек (чекбоксов/спинбоксов).
        """
        self.table_results.blockSignals(True)
        self.table_results.setSortingEnabled(False)

        status_map = {'problem': "Проблема", 'neutral': "Проблем нет", 'ok': "Готов", 'delete': "На удаление", 'retry': "К переотправке", 'edited': "Редакт."}
        show_all = self.check_show_all.isChecked()
        
        current_bounds = self._get_current_ratio_bounds()

        for row in range(self.table_results.rowCount()):
            if row not in self.results_data: continue
            data = self.results_data[row]
            
            # 1. Вызываем чистую логику с предрасчитанными границами
            current_reasons, visual_status = self._calculate_status_for_data(data, override_bounds=current_bounds)
            
            # 2. Обновляем данные (только если это не ручной статус)
            if data.get('status') not in ['ok', 'delete', 'retry', 'edited']:
                data['status'] = visual_status

            # 3. Обновляем UI (С ЗАЩИТОЙ ОТ NoneType)
            
            # Колонка 1: Причины / Ошибки. Если там кнопка (widget), текст не трогаем.
            if not self.table_results.cellWidget(row, 1):
                item_1 = self.table_results.item(row, 1)
                if not item_1: # Если ячейка не создана, создаем её
                    item_1 = QTableWidgetItem("")
                    self.table_results.setItem(row, 1, item_1)
                item_1.setText(", ".join(current_reasons))

            # Колонка 3: Статус. Должна быть всегда текстовой.
            item_3 = self.table_results.item(row, 3)
            if not item_3: # Защита от отсутствующего элемента
                item_3 = QTableWidgetItem("")
                self.table_results.setItem(row, 3, item_3)
            
            item_3.setText(status_map.get(visual_status, visual_status))
            
            self.update_row_color(row, visual_status)

            # 4. Скрываем/Показываем строку
            if 'regex_matches' in data:
                 pass 
            else:
                 should_hide = (visual_status == 'neutral') and (not show_all)
                 self.table_results.setRowHidden(row, should_hide)
        
        self.table_results.setSortingEnabled(True)
        self.table_results.blockSignals(False)
        
        visible_rows = len([r for r in range(self.table_results.rowCount()) if not self.table_results.isRowHidden(r)])
        self.lbl_status.setText(f"Отображено записей: {visible_rows}")
      
    def _open_exceptions_manager(self):
        if not self.settings_manager:
            QMessageBox.warning(self, "Ошибка", "Менеджер настроек не инициализирован.")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Менеджер списков слов-исключений")
        dialog.setMinimumSize(700, 500)
        layout = QVBoxLayout(dialog)

        # Создаем и настраиваем PresetWidget для нашей задачи
        # --- ИЗМЕНЕНИЯ ЗДЕСЬ ---
        exceptions_widget = PresetWidget(
            parent=dialog,
            preset_name="Список исключений", # <-- Указываем имя
            default_prompt_func=api_config.default_word_exceptions,
            load_presets_func=self.settings_manager.load_word_exceptions_presets,
            save_presets_func=self.settings_manager.save_word_exceptions_presets,
            get_last_text_func=self.settings_manager.get_last_word_exceptions_text
        )
        exceptions_widget.load_last_session_state()
        layout.addWidget(exceptions_widget)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        ok_button = button_box.button(QDialogButtonBox.StandardButton.Ok)
        ok_button.setText("Принять и закрыть")
        
        cancel_button = button_box.button(QDialogButtonBox.StandardButton.Cancel)
        cancel_button.setText("Отмена")
        
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            # --- Сохраняем и имя пресета, и текст ---
            exceptions_widget.save_last_session_state()
            self.settings_manager.save_last_word_exceptions_text(exceptions_widget.get_prompt())
            QMessageBox.information(self, "Списки обновлены", "Изменения в списках исключений сохранены.")



    def reset_selected_marks(self):
        selected_rows = sorted(list(set(item.row() for item in self.table_results.selectedItems())))
        
        if not selected_rows:
            QMessageBox.information(self, "Нет выбора", "Сначала выделите строки, для которых нужно сбросить пометки.")
            return

        for row in selected_rows:
            if row in self.results_data:
                # Просто сбрасываем статус.
                # Если файл был отредактирован руками, возвращаем статус 'edited'.
                # Иначе 'neutral'. Метод reapply_filters сам решит, есть там проблема или нет.
                if self.results_data[row].get('is_edited', False):
                    self.results_data[row]['status'] = 'edited'
                else:
                    self.results_data[row]['status'] = 'neutral'
                
                # Сбрасываем цвет строки (визуально)
                self.update_row_color(row, self.results_data[row]['status'])
        
        # Вызываем пересчет фильтров. Если в файле осталась проблема (например, структурная),
        # reapply_filters увидит её и снова покрасит статус в "Проблема".
        self.reapply_filters()
    
    def _apply_custom_filter(self):
        regex_str = self.regex_edit.text()
        if not regex_str:
            QMessageBox.warning(self, "Пустой запрос", "Введите текст или выражение для поиска.")
            return

        try:
            # Инициализируем базовые опции
            options = QRegularExpression.PatternOption.DotMatchesEverythingOption
            
            # Условно добавляем флаг нечувствительности к регистру
            if not self.check_case_sensitive.isChecked():
                options |= QRegularExpression.PatternOption.CaseInsensitiveOption

            regex = QRegularExpression(regex_str, options)
            
            if not regex.isValid():
                QMessageBox.critical(self, "Ошибка регулярного выражения", f"Ошибка: {regex.errorString()}")
                return
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось скомпилировать выражение: {e}")
            return
            
        mode_contains = self.filter_mode_contains.isChecked()
        found_count = 0
        
        # Сначала сбрасываем все предыдущие результаты поиска
        self._clear_custom_filter(clear_text=False)
        
        self.table_results.setSortingEnabled(False)
        for row in range(self.table_results.rowCount()):
            if row not in self.results_data:
                continue

            # --- ЛЕКАРСТВО ЗДЕСЬ ---
            # Мы нормализуем текст к Unix-формату (\n), так как Qt внутри QTextEdit
            # всегда использует \n. Это гарантирует, что индексы поиска совпадут с позициями курсора.
            raw_html = self.results_data[row]['translated_html']
            normalized_html = raw_html.replace('\r\n', '\n').replace('\r', '\n')
            
            iterator = regex.globalMatch(normalized_html)
            # -----------------------
            
            matches = []
            while iterator.hasNext():
                matches.append(iterator.next())
            
            self.results_data[row]['regex_matches'] = matches
            
            is_match = bool(matches)
            should_be_visible = (mode_contains and is_match) or (not mode_contains and not is_match)

            self.table_results.setRowHidden(row, not should_be_visible)
            
            if should_be_visible and is_match:
                found_count += 1
                item = self.table_results.item(row, 0)
                if item:
                    # Нежно-голубая подсветка для найденного
                    item.setBackground(QColor(135, 206, 250, 60)) 
                    
                    # Формируем тултип из первых 3 совпадений
                    examples = [m.captured(0) for m in matches[:3]]
                    tooltip_text = "Найденные совпадения:\n" + "\n".join([f"- …{ex[:100]}…" for ex in examples])
                    item.setToolTip(tooltip_text)

        self.table_results.setSortingEnabled(True)
        self.lbl_status.setText(f"Фильтр применен. Найдено совпадений: {found_count}.")
        
        # Обновляем вид, чтобы подсветка применилась к текущей выбранной главе
        self.update_comparison_view()
    
    
    def _run_project_sync_and_reload(self):
        """Запускает синхронизацию. Вся UI-логика инкапсулирована в SyncThread."""
        if not self.project_manager:
            return

        from ...utils.project_migrator import ProjectMigrator, SyncThread

        self.wait_dialog = QMessageBox(self)
        self.wait_dialog.setWindowTitle("Синхронизация")
        self.wait_dialog.setText("Идет анализ проекта…\nПожалуйста, подождите.")
        self.wait_dialog.setStandardButtons(QMessageBox.StandardButton.NoButton)
        self.wait_dialog.setModal(True)
        
        migrator = ProjectMigrator(self.translated_folder, self.original_epub_path, self.project_manager)
        
        # --- ИЗМЕНЕНИЕ: Передаем `self` в качестве родителя для QMessageBox ---
        self.sync_thread = SyncThread(migrator, parent_widget=self)
        
        # --- ИЗМЕНЕНИЕ: Подключаем только финальный сигнал ---
        self.sync_thread.finished_sync.connect(self._on_validator_sync_finished)
        
        self.sync_thread.start()
        self.wait_dialog.show()
        
    
    def _on_validator_sync_finished(self, is_project_ready, message):
        if hasattr(self, 'wait_dialog') and self.wait_dialog:
            self.wait_dialog.accept()
    
        if not is_project_ready:
            QMessageBox.warning(self, "Операция прервана", message)
            return
            
        # 1. Принудительно обновляем данные в менеджере
        self.project_manager.reload_data_from_disk()
        
        # 2. Сообщаем пользователю
        self.lbl_status.setText("Синхронизация завершена. Обновление таблицы...")
        QApplication.processEvents() 
        
        # 3. УМНАЯ ПЕРЕЗАГРУЗКА: Сохраняем данные, обновляем список файлов
        self._smart_reload_table_preserving_data()
        
        # 4. Обновляем UI кнопки анализа (она должна загореться для НОВЫХ файлов)
        self._update_analyze_button_state()
        
        QMessageBox.information(self, "Синхронизация", message)
    
    def _smart_reload_table_preserving_data(self):
        """
        Перестраивает таблицу, сохраняя результаты анализа для файлов, 
        которые не изменили свое местоположение (internal_path).
        """
        # 1. Бэкапим текущие данные: { internal_path: result_data }
        preserved_data = {}
        for data in self.results_data.values():
            internal_path = data.get('internal_html_path')
            if internal_path:
                preserved_data[internal_path] = data
        
        # 2. Очищаем всё как при обычной инициализации
        # self.table_results.setSortingEnabled(False) # Лучше отключить на время заливки
        self.table_results.setRowCount(0)
        self.results_data.clear()
        self.path_row_map.clear()
        # dirty_files НЕ очищаем полностью, а пересчитаем ниже, 
        # но для чистоты начнем с пустого и добавим туда новые + старые dirty
        old_dirty_set = self.dirty_files.copy()
        self.dirty_files.clear()
        
        if not self.project_manager: return

        # 3. Получаем актуальный список глав
        ordered_originals, _ = get_epub_chapter_order(self.original_epub_path, return_method=True)
        row_pos = 0
        
        for internal_path in ordered_originals:
            versions = self.project_manager.get_versions_for_original(internal_path)
            if not versions: continue

            is_validated_present = '_validated.html' in versions
            
            # Определяем актуальный путь
            if is_validated_present:
                target_rel_path = versions['_validated.html']
            else:
                target_rel_path = next(iter(versions.values()))
            
            full_path = os.path.join(self.translated_folder, target_rel_path)
            
            # 4. ПРОВЕРЯЕМ: Есть ли у нас старые данные для этого пути?
            if internal_path in preserved_data:
                # ВОССТАНАВЛИВАЕМ ДАННЫЕ
                # Важно обновить путь к файлу, если вдруг он поменялся (маловероятно при sync, но всё же)
                data = preserved_data[internal_path]
                data['path'] = full_path 
                data['is_validated_file'] = is_validated_present
                
                # Если файл был dirty раньше - он остается dirty
                if internal_path in old_dirty_set:
                    self.dirty_files.add(internal_path)
            else:
                # СОЗДАЕМ НОВЫЕ ДАННЫЕ (это новый файл после сверки)
                data = {
                    'path': full_path,
                    'internal_html_path': internal_path,
                    'status': 'neutral',
                    'translated_html': "", 
                    'len_orig': 0, 'len_trans': 0,
                    'is_validated_file': is_validated_present
                }
                # Новые файлы всегда требуют проверки
                self.dirty_files.add(internal_path)
            
            # 5. Рисуем строку в таблице
            self.table_results.insertRow(row_pos)
            
            display_text = f"{os.path.basename(internal_path)}"
            if is_validated_present: display_text += " [Готов]"
            else: display_text += f" -> {os.path.basename(target_rel_path)}"

            display_path_item = SortableChapterItem(display_text, internal_path)
            display_path_item.setData(Qt.ItemDataRole.UserRole, is_validated_present)
            self.table_results.setItem(row_pos, 0, display_path_item)
            
            # Восстанавливаем визуальное содержимое ячеек
            # Колонка 1 (Ошибки/Кнопка)
            if 'structural_errors' in data:
                details_button = QPushButton("См. детали…")
                errors = data['structural_errors']
                details_button.clicked.connect(lambda checked=False, e=errors: self.show_structure_details(e))
                self.table_results.setCellWidget(row_pos, 1, details_button)
            else:
                self.table_results.setItem(row_pos, 1, QTableWidgetItem("")) # Placeholder, текст заполнится в reapply_filters

            # Колонка 2 (Длина)
            len_text = f"{data.get('len_orig', 0)} | {data.get('len_trans', 0)}" if data.get('len_orig') else "- | -"
            self.table_results.setItem(row_pos, 2, NumericTableWidgetItem(len_text))
            
            # Колонка 3 (Статус)
            self.table_results.setItem(row_pos, 3, QTableWidgetItem("...")) # Заполнится в reapply_filters

            # Регистрируем
            self.results_data[row_pos] = data
            self.path_row_map[internal_path] = row_pos
            
            # Скрываем, если нужно
            if is_validated_present and not self.check_revalidate_ok.isChecked():
                self.table_results.setRowHidden(row_pos, True)
            
            row_pos += 1
        
        # 6. Применяем фильтры и раскраску (восстановит цвета и тексты ошибок)
        self.reapply_filters()
        
        # 7. Явно обновляем статистику недопереводов прямо сейчас
        self._recalc_untranslated_stats_ui()
        
        self.lbl_status.setText(f"Таблица обновлена. Требуют проверки: {len(self.dirty_files)}")

    def _recalc_untranslated_stats_ui(self):
        """Пересчитывает общее количество недопереводов по всем текущим данным и обновляет кнопку."""
        self.untranslated_found_count = 0
        for data in self.results_data.values():
            if 'untranslated_words' in data:
                self.untranslated_found_count += 1
        
        if self.untranslated_found_count > 0:
            self.btn_fix_untranslated.setText(f"Недоперевод ({self.untranslated_found_count})")
            self.btn_fix_untranslated.setEnabled(True)
        else:
            self.btn_fix_untranslated.setText("Недоперевод (лат./иер.)")
            self.btn_fix_untranslated.setEnabled(False)
            
    def _clear_custom_filter(self, clear_text=True):
        """
        Полностью сбрасывает состояние пользовательского фильтра,
        возвращая таблицу к виду до поиска.
        """
        # 1. Очищаем поле ввода и данные поиска
        if clear_text:
            self.regex_edit.clear()
        
        # 2. Проходим по всем строкам и сбрасываем их ВИЗУАЛЬНОЕ состояние от поиска
        for row in range(self.table_results.rowCount()):
            # Удаляем сохраненные совпадения из данных
            if row in self.results_data:
                self.results_data[row].pop('regex_matches', None)
            
            # ЛЕКАРСТВО: Убираем синюю подсветку с самой строки таблицы
            item = self.table_results.item(row, 0)
            if item:
                item.setBackground(QBrush(Qt.GlobalColor.transparent))
                item.setToolTip("")

        # 3. Обновляем окно сравнения, чтобы убрать синюю подсветку из текста
        # Так как поле ввода уже пустое, подсветка для regex не применится
        self.update_comparison_view()

        # 4. ЛЕКАРСТВО: Принудительно переприменяем ОСНОВНЫЕ фильтры
        # Это вернет правильные цвета статусов и скроет ненужные строки.
        self.reapply_filters()
        
        if clear_text:
            self.lbl_status.setText("Фильтр сброшен. Показаны результаты основного анализа.")


    def on_compare_toggle(self, checked):
        """Переключает режим сравнения между оригиналом и готовой версией."""
        self.is_comparing_validated = checked
        if checked:
            self.btn_toggle_compare.setText("Сравнить с Оригиналом")
        else:
            self.btn_toggle_compare.setText("Сравнить с Готовой версией")
        
        # Просто вызываем обновление, оно само разберется, что показывать
        self.update_comparison_view()




    def _update_analyze_button_state(self):
        """
        Проверяет, есть ли файлы, которые НУЖНО проверить прямо сейчас.
        Критерий: Файл находится в self.dirty_files И (он не validated ИЛИ галочка validated включена).
        """
        # Определяем файлы, которые реально доступны для анализа
        files_to_scan_count = 0
        include_validated = self.check_revalidate_ok.isChecked()
        
        for internal_path in self.dirty_files:
            # Находим данные этого файла
            row = self.path_row_map.get(internal_path)
            if row is None: continue
            
            data = self.results_data[row]
            is_validated = data.get('is_validated_file', False)
            
            # Если файл обычный - считаем.
            # Если файл готовый - считаем только если включена галочка.
            if not is_validated or include_validated:
                files_to_scan_count += 1

        if files_to_scan_count > 0:
            # Оранжевый стиль - "Требуется обновление"
            style = """
                QPushButton {
                    background-color: rgba(255, 140, 0, 40);
                    border: 1px solid #FF8C00;
                    font-weight: bold;
                }
            """
            self.btn_analyze.setStyleSheet(style)
            self.btn_analyze.setText(f"🚀 Проверить ({files_to_scan_count})")
            self.btn_analyze.setEnabled(True)
        else:
            # Обычный стиль - "Всё актуально"
            self.btn_analyze.setStyleSheet("")
            self.btn_analyze.setText("✅ Анализ актуален")
            # Можно оставить активной для принудительного перескана, или выключить
            self.btn_analyze.setEnabled(True)

    def start_analysis(self, specific_targets=None): # Аргумент оставляем для совместимости, но используем логику dirty
        if self.project_manager:
            self.project_manager.reload_data_from_disk()
        
        # 1. Вычисляем цели: Пересечение Dirty Files и Видимости
        targets = []
        include_validated = self.check_revalidate_ok.isChecked()
        
        # Если specific_targets не передан (обычное нажатие кнопки), берем из dirty_files
        candidates = specific_targets if specific_targets else self.dirty_files
        
        for internal_path in list(candidates): # list() для безопасной итерации
            row = self.path_row_map.get(internal_path)
            if row is None: continue
            
            is_validated = self.results_data[row].get('is_validated_file', False)
            
            # Добавляем в задачи, если это не готовый файл ИЛИ (готовый и включена галочка)
            if not is_validated or include_validated:
                targets.append(internal_path)

        if not targets:
            QMessageBox.information(self, "Анализ не требуется", "Нет файлов, требующих обновления анализа согласно текущим настройкам.")
            self._update_analyze_button_state() # Сбросить стиль на всякий случай
            return
        
        # --- КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ: "Вычитаем" старые результаты только для тех файлов, что будут перепроверены ---
        for internal_path in targets:
            row = self.path_row_map.get(internal_path)
            if row is not None and row in self.results_data:
                # Если у файла был флаг недоперевода, убираем его вклад в общий счетчик
                if 'untranslated_words' in self.results_data[row]:
                    self.untranslated_found_count = max(0, self.untranslated_found_count - 1)
        # --- КОНЕЦ ИСПРАВЛЕНИЯ ---


        # UI блокировка
        self.btn_analyze.setText("⏳ Анализирую...")
        self.btn_analyze.setEnabled(False)
        self.btn_exceptions_manager.setEnabled(False)
        
        # Загрузка исключений (код сокращен, он у вас есть в оригинале)
        if self.settings_manager:
            exceptions_text = self.settings_manager.get_last_word_exceptions_text()
            if not exceptions_text.strip(): exceptions_text = api_config.default_word_exceptions()
        else:
            exceptions_text = api_config.default_word_exceptions()
        exceptions_set = {line.strip().lower() for line in exceptions_text.splitlines() if line.strip() and not line.strip().startswith('#')}
        
        # --- НОВАЯ ЛОГИКА: Добавляем авторизованные латинские термины из Глоссария ---
        if self.project_manager and self.project_manager.project_folder:
            glossary_path = os.path.join(self.project_manager.project_folder, "project_glossary.json")
            if os.path.exists(glossary_path):
                try:
                    with open(glossary_path, 'r', encoding='utf-8') as f:
                        glossary_data = json.load(f)
                    
                    # Паттерны для очистки (вычитания)
                    # 1. Удаляем всю кириллицу
                    cyrillic_pattern = re.compile(r'[а-яА-ЯёЁ]+')
                    # 2. Удаляем цифры и спецсимволы, оставляя только слова (латиницу и прочее)
                    cleanup_pattern = re.compile(r'[\W\d_]+')
                    
                    glossary_exceptions_count = 0
                    
                    # Если формат - словарь
                    iterator = glossary_data if isinstance(glossary_data, list) else glossary_data.values()
                    
                    for entry in iterator:
                        # В разных форматах глоссария entry может быть dict или value в dict
                        # Нормализуем доступ
                        rus = ''
                        if isinstance(entry, dict):
                            # --- FIX: Поддержка разных ключей для перевода (rus, translation, target) ---
                            rus = entry.get('rus') or entry.get('translation') or entry.get('target') or ''
                        
                        if not rus: continue
                        
                        # --- АЛГОРИТМ ВЫЧИТАНИЯ (как в Residue Analyzer) ---
                        # Шаг 1: Убираем русскую речь
                        no_cyrillic_str = cyrillic_pattern.sub(' ', rus)
                        
                        # Шаг 2: Убираем пунктуацию и цифры
                        pure_residue_str = cleanup_pattern.sub(' ', no_cyrillic_str)
                        
                        # Шаг 3: Разбиваем на слова
                        found_latins = pure_residue_str.strip().split()
                        
                        for word in found_latins:
                            w_lower = word.lower()
                            # Игнорируем совсем короткий мусор (1 буква), если это не CJK
                            # (для простоты считаем все < 2 букв мусором, если это латиница)
                            if len(w_lower) < 2: 
                                continue
                                
                            if w_lower not in exceptions_set:
                                exceptions_set.add(w_lower)
                                glossary_exceptions_count += 1
                                
                    if glossary_exceptions_count > 0:
                        print(f"[Validator] Автоматически добавлено {glossary_exceptions_count} не-русских слов из Глоссария в исключения.")

                except Exception as e:
                    print(f"[Validator WARN] Не удалось прочитать глоссарий для исключений: {e}")
        # -----------------------------------------------------------------------------
        
        config = {
            'check_structure': True, 
            'check_length_ratio': True,
            'show_all': self.check_show_all.isChecked(),
            'revalidate_ok': self.check_revalidate_ok.isChecked(),
            'check_simplification': True,
            'check_untranslated': True,
            'check_paragraph_size': True,
            'max_paragraph_size': self.max_paragraph_spinbox.value(),
            'simplification_threshold': self.simplification_threshold_spinbox.value() / 100.0,
            'check_repeating_chars': True,
            'repeating_chars_threshold': self.repeating_chars_spinbox.value()
        }
        
        # Запуск потока только для targets
        self.analysis_thread = ValidationThread(
            self.translated_folder, 
            self.original_epub_path, 
            config, 
            exceptions_set,
            self.project_manager,
            files_to_scan=targets
        )
        
        self.analysis_thread.result_found.connect(self.add_result)
        self.analysis_thread.progress_update.connect(self.update_status)
        self.analysis_thread.analysis_finished.connect(self.on_analysis_finished)
        self.analysis_thread.start()
    
    
    @pyqtSlot(str)
    def _jump_to_tag_in_code(self, tag_to_find):
        """
        Переходит к указанному тегу в окне с кодом перевода и выделяет его.
        """
        # 1. Убеждаемся, что мы находимся в режиме просмотра кода
        if not self.is_code_view:
            self.toggle_code_view()

        # 2. Выполняем поиск в виджете с кодом перевода
        document = self.view_translated.document()
        cursor = document.find(tag_to_find)

        if not cursor.isNull():
            # 3. Если тег найден, устанавливаем курсор и выделяем его
            self.view_translated.setTextCursor(cursor)
            self.view_translated.setFocus()
        else:
            # 4. Если по какой-то причине тег не найден, сообщаем об этом
            QMessageBox.information(self, "Не найдено", f"Не удалось найти тег {tag_to_find.replace('<', '&lt;')} в коде.")

    # --- НОВЫЙ МЕТОД ---
    def show_structure_details(self, errors_dict):
        """Создает и показывает диалог с деталями, подключая сигнал для поиска."""
        dialog = StructureErrorsDialog(errors_dict, self)
        # Подключаем сигнал из дочернего окна к слоту в этом (родительском) окне
        dialog.find_tag_in_code_requested.connect(self._jump_to_tag_in_code)
        dialog.exec()
    
# --- ВСТАВЬТЕ ЭТИ ДВА МЕТОДА В КЛАСС TranslationValidatorDialog ---
    def _go_to_previous_item(self):
        """Выбирает предыдущую строку в таблице."""
        current_row = self.table_results.currentRow()
        target_row = current_row - 1
        if target_row >= 0:
            self.table_results.selectRow(target_row)

    def _go_to_next_item(self):
        """Выбирает следующую строку в таблице."""
        current_row = self.table_results.currentRow()
        target_row = current_row + 1
        if target_row < self.table_results.rowCount():
            self.table_results.selectRow(target_row)

    def _update_highlighters(self):
        """Включает/выключает подсветку синтаксиса в зависимости от режима."""
        is_code = self.is_code_view
        
        # Сначала отключаем все "раскрасчики"
        self.html_highlighter_orig.setDocument(None)
        self.html_highlighter_trans.setDocument(None)
        self.punctuation_highlighter_orig.setDocument(None)
        self.punctuation_highlighter_trans.setDocument(None)
    
        # Затем включаем нужные
        if is_code:
            self.html_highlighter_orig.setDocument(self.view_original.document())
            self.html_highlighter_trans.setDocument(self.view_translated.document())
        else:
            self.punctuation_highlighter_orig.setDocument(self.view_original.document())
            self.punctuation_highlighter_trans.setDocument(self.view_translated.document())
        
        # Принудительно перерисовываем подсветку
        self.html_highlighter_orig.rehighlight()
        self.html_highlighter_trans.rehighlight()
        self.punctuation_highlighter_orig.rehighlight()
        self.punctuation_highlighter_trans.rehighlight()
    
    
    def _perform_initial_cjk_scan(self):
        """
        Выполняет быстрый анализ нескольких глав EPUB на наличие CJK символов
        и устанавливает соответствующий пресет в ComboBox.
        """
        if not self.original_epub_path or not os.path.exists(self.original_epub_path):
            return

        try:
            with zipfile.ZipFile(open(self.original_epub_path, 'rb'), 'r') as epub_zip:
                html_files = [name for name in epub_zip.namelist() if name.lower().endswith(('.html', '.xhtml')) and not name.startswith('__MACOSX')]
                if not html_files:
                    return

                chapters_to_scan = html_files[:5] # Проверяем до 5 глав для скорости
                cjk_char_count = 0
                
                for chapter_path in chapters_to_scan:
                    content = epub_zip.read(chapter_path).decode('utf-8', 'ignore')
                    # Используем регулярку для быстрого подсчета всех CJK символов
                    cjk_chars_in_chapter = re.findall(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]', content)
                    cjk_char_count += len(cjk_chars_in_chapter)
                    
                    if cjk_char_count >= 100:
                        break # Нашли достаточно, выходим из цикла
                
                # Если найдено достаточно иероглифов, переключаем ComboBox
                if cjk_char_count >= 100:
                    cjk_preset_index = self.ratio_presets_combo.findText("Иероглифический (象 -> A)")
                    if cjk_preset_index != -1:
                        self.ratio_presets_combo.setCurrentIndex(cjk_preset_index)
        except Exception as e:
            # В случае ошибки просто ничего не делаем, чтобы не сломать запуск диалога
            print(f"Ошибка при предварительном сканировании на CJK: {e}")


    @pyqtSlot(dict)
    def add_result(self, result):
        internal_path = result.get('internal_html_path')
        if not internal_path or internal_path not in self.path_row_map: return

        if internal_path in self.dirty_files:
            self.dirty_files.remove(internal_path)

        row_pos = self.path_row_map[internal_path]
        
        # Сохранение флагов
        was_validated = self.results_data[row_pos].get('is_validated_file', False)
        result['is_validated_file'] = was_validated
        was_edited = self.results_data[row_pos].get('is_edited', False)
        result['is_edited'] = was_edited

        self.results_data[row_pos] = result
        
        if 'untranslated_words' in result:
            self.untranslated_found_count += 1
            
        # --- UI Обновление ТОЛЬКО ОДНОЙ строки ---
        
        # 1. Кнопка деталей
        if 'structural_errors' in result:
            details_button = QPushButton("См. детали…")
            errors = result['structural_errors']
            details_button.clicked.connect(lambda checked=False, e=errors: self.show_structure_details(e))
            self.table_results.setCellWidget(row_pos, 1, details_button)
        else:
            self.table_results.removeCellWidget(row_pos, 1)
            self.table_results.setItem(row_pos, 1, QTableWidgetItem(""))

        # 2. Длина
        self.table_results.item(row_pos, 2).setText(f"{result['len_orig']} | {result['len_trans']}")
        
        # 3. Расчет статуса (Локально!)
        current_reasons, visual_status = self._calculate_status_for_data(result)
        
        if result.get('status') not in ['ok', 'delete', 'retry', 'edited']:
            result['status'] = visual_status
            
        # 4. Применение текста и цвета
        status_map = {'problem': "Проблема", 'neutral': "Проблем нет", 'ok': "Готов", 'delete': "На удаление", 'retry': "К переотправке", 'edited': "Редакт."}
        
        reason_item = self.table_results.item(row_pos, 1)
        # Если там кнопка, текст не ставим, иначе ставим причины
        if not self.table_results.cellWidget(row_pos, 1):
             reason_item.setText(", ".join(current_reasons))
             
        self.table_results.item(row_pos, 3).setText(status_map.get(visual_status, visual_status))
        self.update_row_color(row_pos, visual_status)
        
        # 5. Видимость
        # Если "Показать все" выключено И статус "Нейтральный" -> скрываем. Иначе показываем.
        show_all = self.check_show_all.isChecked()
        should_hide = (visual_status == 'neutral') and (not show_all)
        self.table_results.setRowHidden(row_pos, should_hide)
    
    def _update_data_from_view(self):
        selected_items = self.table_results.selectedItems()
        if not selected_items:
            return
        row = selected_items[0].row()
        
        if row in self.results_data:
            # Получаем текущий контент в зависимости от активного режима
            if self.is_code_view:
                current_content = self.view_translated.toPlainText()
            else:
                # Преобразуем HTML-контент в 'очищенный' текст для хранения
                current_content = self.view_translated.toHtml() 
                
            self.results_data[row]['translated_html'] = current_content
    
    
# --- НАЧАЛО КОДА ДЛЯ ЗАМЕНЫ (два метода в классе TranslationValidatorDialog) ---

    def _are_any_translated_files_left(self):
        """
        Проверяет, остались ли в основной папке перевода какие-либо файлы
        для проверки (с любым суффиксом).
        """
        try:
            # --- ИЗМЕНЕНИЕ: Используем универсальный список суффиксов ---
            from ...api import config as api_config
            
            for f in os.listdir(self.translated_folder):
                for suffix in api_config.all_translated_suffixes():
                    if f.endswith(suffix):
                        return True # Нашли хотя бы один, выходим
            return False
            
        except FileNotFoundError:
            print(f"Ошибка проверки: Папка {self.translated_folder} не найдена.")
            return False

    def auto_process_good_files(self):
        """
        Находит все "хорошие" файлы и просто переименовывает их, добавляя суффикс _validated.html.
        """
        self.lbl_status.setText("Авто-обработка 'хороших' файлов…")
        QApplication.processEvents()

        from ...api import config as api_config
        VALIDATED_SUFFIX = "_validated.html"
        
        known_problem_internal_paths = {data['internal_html_path'] for data in self.results_data.values()}
        processed_count = 0
        errors = []

        all_originals = self.project_manager.get_all_originals()
        
        for internal_path in all_originals:
            if internal_path in known_problem_internal_paths:
                continue

            versions = self.project_manager.get_versions_for_original(internal_path)
            # Ищем любую не-валидированную версию
            unvalidated_version = next(((suffix, rel_path) for suffix, rel_path in versions.items() if suffix != VALIDATED_SUFFIX), None)

            if unvalidated_version:
                old_suffix, old_rel_path = unvalidated_version
                source_path = os.path.join(self.translated_folder, old_rel_path)
                
                if os.path.exists(source_path):
                    try:
                        base_name = source_path[:-len(old_suffix)]
                        dest_path = base_name + VALIDATED_SUFFIX
                        
                        shutil.move(source_path, dest_path)
                        
                        # Атомарно обновляем карту
                        self.project_manager.remove_translation(internal_path, old_suffix)
                        new_rel_path = os.path.relpath(dest_path, self.translated_folder)
                        self.project_manager.register_translation(internal_path, VALIDATED_SUFFIX, new_rel_path)
                        
                        processed_count += 1
                    except Exception as e:
                        errors.append(f"Не удалось переименовать {os.path.basename(source_path)}: {e}")

        if errors:
            QMessageBox.warning(self, "Завершено с ошибками", f"Переименовано файлов: {processed_count}.\n\nОшибки:\n" + "\n".join(errors))
        else:
            QMessageBox.information(self, "Завершено", f"Успешно помечено 'Готовыми' файлов: {processed_count}.")
        
        self.lbl_status.setText("Готов к проверке.")
        # Перезапускаем анализ, чтобы показать пустой список
        self.start_analysis()
       
    # Новый вспомогательный метод для обновления данных в памяти
    def _update_in_memory_data(self):
        selected_items = self.table_results.selectedItems()
        if not selected_items:
            return
        
        # Получаем индекс строки из текущего выделения
        # Важно не использовать старый сохраненный индекс, так как выделение могло измениться
        row = self.table_results.row(selected_items[0])
        
        if row in self.results_data:
            # Получаем контент напрямую из виджета, где находятся самые свежие изменения
            # Мы сохраняем только "сырой" текст, так как это основной источник правок
            current_content = self.view_translated.toPlainText()
            
            # Обновляем HTML-представление в нашем словаре
            self.results_data[row]['translated_html'] = current_content
    
    def on_text_edited(self):
        """
        Срабатывает при каждом изменении в редакторе. Немедленно сохраняет
        изменения в буфер (self.results_data) и устанавливает флаг 'is_edited'.
        """
        # Этот слот должен работать только в режиме редактирования кода
        if not self.is_code_view:
            return

        selected_rows = list(set(item.row() for item in self.table_results.selectedItems()))
        if not selected_rows:
            return
    
        row = selected_rows[0]
    
        if row in self.results_data:
            # 1. Сохраняем "истинный" код из редактора в наш буфер
            self.results_data[row]['translated_html'] = self.view_translated.toPlainText()

            # 2. Если файл еще не был помечен как измененный, помечаем его
            if not self.results_data[row].get('is_edited', False):
                self.results_data[row]['is_edited'] = True
                # Обновляем статус в таблице, чтобы было видно
                self.table_results.item(row, 3).setText("Редакт.")
                self.update_row_color(row, 'edited')
            
            # 3. Активируем кнопку сохранения, так как есть несохраненные изменения
            self.btn_save_changes.setEnabled(True)

    # --- ДОБАВЬТЕ ЭТОТ НОВЫЙ МЕТОД ---
    def select_all_rows(self):
        """Выделяет все строки в таблице результатов."""
        self.table_results.selectAll()
        
    def toggle_code_view(self):
        selected_items = self.table_results.selectedItems()
        if not selected_items: return
            
        row = selected_items[0].row()
        if row not in self.results_data: return

        # --- УПРОЩЕНИЕ: Больше не сохраняем здесь ---
        # if self.is_code_view:
        #     … self.results_data[row]['translated_html'] = …
            
        self.is_code_view = not self.is_code_view
        
        self.view_translated.blockSignals(True)
        self.update_comparison_view() # Просто обновляем вид
        self._update_highlighters()
        self.view_translated.blockSignals(False)
    
        self.btn_toggle_code_view.setText("Скрыть код" if self.is_code_view else "Показать код")
    


    def _apply_highlighting(self, text_edit_widget, row_index, words_to_highlight=None, regex_pattern_str=None):
        """
        Универсальная подсветка через ExtraSelections.
        ЛЕКАРСТВО: Для Regex ищет по сырому HTML, а подсвечивает видимый текст.
        """
        selections = []
        document = text_edit_widget.document()
        
        # --- Блок 1: Недоперевод (простой поиск по видимому тексту) ---
        if words_to_highlight:
            highlight_format = QTextCharFormat()
            highlight_format.setBackground(QColor(255, 165, 0, 100))
            highlight_format.setFontWeight(QFont.Weight.Bold)

            for word in words_to_highlight:
                pattern_str = f"(?<![a-zA-Z]){re.escape(word)}(?![a-zA-Z])" if re.fullmatch(r'[a-zA-Z]+', word) else re.escape(word)
                q_regex = QRegularExpression(pattern_str, QRegularExpression.PatternOption.CaseInsensitiveOption)
                
                cursor = document.find(q_regex)
                while not cursor.isNull():
                    selection = QTextEdit.ExtraSelection(); selection.format = highlight_format; selection.cursor = cursor
                    selections.append(selection)
                    cursor = document.find(q_regex, cursor)

        # --- Блок 2: Пользовательский Regex (сложный двухэтапный ритуал) ---
        if regex_pattern_str and row_index in self.results_data:
            regex_format = QTextCharFormat()
            regex_format.setBackground(QColor(0, 191, 255, 100))
            
            try:
                # Определяем флаги для поиска
                flags = re.DOTALL
                if not self.check_case_sensitive.isChecked():
                    flags |= re.IGNORECASE
                
                python_regex = re.compile(regex_pattern_str, flags)
                
                # ШАГ 1: Ищем по сырому HTML из наших данных
                raw_html = self.results_data[row_index]['translated_html']
                matches = list(python_regex.finditer(raw_html))
                
                # Используем курсор для последовательного поиска, чтобы избежать путаницы
                # с одинаковыми фрагментами текста
                search_cursor = QTextCursor(document)
                
                for match in matches:
                    # ШАГ 2: Извлекаем видимый текст из найденного HTML-фрагмента
                    matched_html_fragment = match.group(0)
                    visible_text = BeautifulSoup(matched_html_fragment, 'html.parser').get_text().strip()
                    
                    # Если в совпадении нет видимого текста (например, пустой тег), пропускаем
                    if not visible_text:
                        continue
                        
                    # ШАГ 3: Ищем этот видимый текст в документе и подсвечиваем
                    # Начинаем поиск с позиции последнего найденного совпадения
                    found_cursor = document.find(visible_text, search_cursor)
                    
                    if not found_cursor.isNull():
                        selection = QTextEdit.ExtraSelection(); selection.format = regex_format; selection.cursor = found_cursor
                        selections.append(selection)
                        # Сдвигаем курсор, чтобы следующий поиск начался после текущего найденного фрагмента
                        search_cursor = found_cursor
                        
            except re.error as e:
                # Если регулярка невалидна, ничего не делаем
                print(f"Regex error in highlighter: {e}")

        text_edit_widget.setExtraSelections(selections)

    def update_comparison_view(self):
        selected_items = self.table_results.selectedItems()
        if not selected_items:
            self.btn_toggle_code_view.setEnabled(False)
            self.btn_toggle_compare.setEnabled(False)
            self.view_translated.setReadOnly(True)
            self.view_original.clear()
            self.view_translated.clear()
            return
        
        self.btn_toggle_code_view.setEnabled(True)
        row = selected_items[0].row()

        if row in self.results_data:
            data = self.results_data[row]
            
            original_html_safe = data.get('original_html', '<p style="color:red;">Ошибка: нет оригинала.</p>')
            left_content_raw = data.get('validated_content') if self.is_comparing_validated and data.get('validated_content') else original_html_safe
            translated_content_raw = data.get('translated_html', '')

            words_to_highlight = data.get('untranslated_words', [])
            regex_pattern = self.regex_edit.text() if self.regex_edit.text() else None
            
            # --- Загрузка контента ---
            if self.is_code_view:
                self.view_original.setPlainText(left_content_raw)
                self.view_translated.setPlainText(translated_content_raw)
            else:
                self.view_original.setHtml(left_content_raw)
                self.view_translated.setHtml(translated_content_raw)
            
            # --- Наложение цветов ---
            # Передаем индекс строки, чтобы иметь доступ к сырому HTML
            self._apply_highlighting(self.view_original, row, words_to_highlight, regex_pattern)
            self._apply_highlighting(self.view_translated, row, words_to_highlight, regex_pattern)
            
            self.view_translated.setReadOnly(not self.is_code_view)


    def _inject_highlights_into_html(self, html_content, words_to_highlight=None, regex_matches=None):
        """
        Создает временную копию HTML и "внедряет" в нее теги подсветки.
        Использует умные границы для слов, чтобы находить 'Word' внутри 'Word123'.
        """
        modified_html = html_content
        tag_regex = re.compile(r"(<[^>]+>)", re.DOTALL)

        # --- ЭТАП 1: Подсветка недоперевода ---
        if words_to_highlight:
            try:
                # Сортируем по длине, чтобы сначала подсвечивать длинные фразы
                sorted_words = sorted(words_to_highlight, key=len, reverse=True)
                patterns = []
                for w in sorted_words:
                    if re.fullmatch(r'[a-zA-Z]+', w):
                        # ЛЕКАРСТВО: Вместо \b используем lookaround. 
                        # Ищем слово, перед которым и после которого НЕТ букв.
                        # Это позволит найти "Level" внутри "Level5" или "Item_1".
                        patterns.append(f"(?<![a-zA-Z]){re.escape(w)}(?![a-zA-Z])")
                    else:
                        patterns.append(re.escape(w))
                
                if patterns:
                    giant_regex = re.compile(f"({'|'.join(patterns)})", re.IGNORECASE)
                    
                    def untranslated_replacer(match):
                        return f'<span style="background-color: rgba(255, 140, 0, 0.5); border: 1px solid orange;">{match.group(0)}</span>'

                    parts = tag_regex.split(modified_html)
                    for i in range(0, len(parts), 2):
                        # Пропускаем пустые части
                        if not parts[i]: continue
                        parts[i] = giant_regex.sub(untranslated_replacer, parts[i])
                    modified_html = "".join(parts)
            except re.error as e:
                print(f"[Highlighter Error] Untranslated words regex failed: {e}")
        
        # --- ЭТАП 2: Подсветка Regex-поиска ---
        if regex_matches:
            # Итерируем по совпадениям в обратном порядке
            for match in sorted(regex_matches, key=lambda m: m.capturedStart(0), reverse=True):
                start, end = match.capturedStart(0), match.capturedEnd(0)
                
                matched_block = modified_html[start:end]
                
                parts = tag_regex.split(matched_block)
                for i in range(0, len(parts), 2):
                    if parts[i]: 
                        parts[i] = f'<span style="background-color: rgba(0, 191, 255, 0.4);">{parts[i]}</span>'
                
                highlighted_block = "".join(parts)
                modified_html = modified_html[:start] + highlighted_block + modified_html[end:]

        return modified_html
        
    def _update_in_memory_data(self):
        selected_items = self.table_results.selectedItems()
        if not selected_items:
            return
        row = selected_items[0].row()
        
        if row in self.results_data:
            # Получаем контент в зависимости от текущего режима
            if self.is_code_view:
                current_content = self.view_translated.toPlainText()
            else:
                current_content = self.view_translated.toHtml()
            
            self.results_data[row]['translated_html'] = current_content
    

    @pyqtSlot()
    def on_selection_changed(self):
        # --- УПРОЩЕНИЕ: Убираем логику сохранения отсюда ---
        # if self.is_code_view and old_row != -1 …
        
        selected_rows = list(set(item.row() for item in self.table_results.selectedItems()))

        # … (остальной код метода без изменений) …
        can_navigate = len(selected_rows) == 1
        self.btn_prev_item.setEnabled(can_navigate and selected_rows[0] > 0)
        self.btn_next_item.setEnabled(can_navigate and selected_rows[0] < self.table_results.rowCount() - 1)

        if not selected_rows:
            self.view_original.clear(); self.view_translated.clear()
            self.btn_toggle_code_view.setEnabled(False); self.btn_toggle_compare.setVisible(False)
            self.view_translated.setReadOnly(True)
            # Кнопка сохранения НЕ деактивируется, так как могут быть другие измененные файлы
            return

        row = selected_rows[0]
        
        # --- НАЧАЛО КЛЮЧЕВОГО ИЗМЕНЕНИЯ ---
        data = self.results_data.get(row, {})
        has_validated_version = 'validated_content' in data
        # Проверяем, не является ли текущий файл сам по себе "готовым"
        is_current_file_the_validated_one = data.get('path', '').endswith('_validated.html')
        
        # Кнопку показываем, только если есть готовая версия И мы смотрим НЕ на нее
        should_show_button = has_validated_version and not is_current_file_the_validated_one
        self.btn_toggle_compare.setVisible(should_show_button)
        
        # Если кнопка скрыта, сбрасываем режим сравнения
        if not should_show_button and self.is_comparing_validated:
            self.btn_toggle_compare.setChecked(False)
        # --- КОНЕЦ КЛЮЧЕВОГО ИЗМЕНЕНИЯ ---
        
        self.view_translated.blockSignals(True)
        self.update_comparison_view()
        self.view_translated.blockSignals(False)

        # Кнопка сохранения остается активной, если есть *любые* несохраненные изменения
        if not any(data.get('is_edited', False) for data in self.results_data.values()):
            self.btn_save_changes.setEnabled(False)
        else:
            self.btn_save_changes.setEnabled(True)

    def mark_selected_rows(self, status):
        selected_rows = sorted(list(set(item.row() for item in self.table_results.selectedItems())))
        
        status_map = {
            'delete': ("На удаление", "delete"),
            'mark_ok': ("Готов", "ok"),
            'retry': ("К переотправке", "retry")
        }
        
        if status not in status_map: return
        
        display_text, internal_status = status_map[status]

        for row in selected_rows:
            if row in self.results_data:
                # 1. Обновляем наши внутренние данные
                self.results_data[row]['status'] = internal_status
                
                # 2. Обновляем текст в ячейке статуса (колонка 3)
                status_item = self.table_results.item(row, 3)
                if status_item: # Проверяем, что ячейка существует
                    status_item.setText(display_text)
                
                # 3. Вызываем обновление цвета для всей строки
                self.update_row_color(row, internal_status)

    def update_row_color(self, row, status):
        alpha = 85 
        color = QColor("transparent") # Нейтральный цвет по умолчанию

        if status == 'delete': color = QColor(90, 58, 58, alpha)
        elif status == 'ok': color = QColor(46, 75, 62, alpha)
        elif status == 'retry': color = QColor(88, 68, 46, alpha)
        elif status == 'problem': color = QColor(93, 72, 53, alpha)
        elif status == 'edited': color = QColor(58, 75, 95, alpha)
        # Для 'neutral' мы просто оставляем прозрачный цвет по умолчанию

        brush = QBrush(color)
        for col in range(self.table_results.columnCount()):
            item = self.table_results.item(row, col)
            if item:
                item.setBackground(brush)

    @pyqtSlot()
    def save_changes(self):
        files_to_save = []
        for row, data in self.results_data.items():
            if data.get('is_edited', False):
                files_to_save.append((row, data))

        if not files_to_save: return

        saved_count = 0
        for row, data in files_to_save:
            filepath = data['path']
            try:
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(data['translated_html'])
                
                data['is_edited'] = False
                # Сбрасываем статус на нейтральный перед проверкой
                self.table_results.item(row, 3).setText("Изменен (ждет анализа)")
                self.update_row_color(row, 'neutral')
                
                # --- ВАЖНО: Помечаем файл как грязный ---
                self.dirty_files.add(data['internal_html_path'])
                
                saved_count += 1
            except Exception as e:
                QMessageBox.critical(self, "Ошибка", f"Не удалось сохранить: {e}")
                break
        
        if saved_count > 0:
            self.lbl_status.setText(f"Сохранено файлов: {saved_count}. Требуется перепроверка.")
            # Автоматического запуска НЕТ. Только обновление кнопки.
            self._update_analyze_button_state()

        if not any(d.get('is_edited', False) for d in self.results_data.values()):
            self.btn_save_changes.setEnabled(False)
    
    @pyqtSlot()
    def apply_changes(self):
        if not self.project_manager:
            QMessageBox.warning(self, "Критическая ошибка", "Менеджер проекта не инициализирован.")
            return

        VALIDATED_SUFFIX = "_validated.html"
        from ...api import config as api_config
        
        # 1. Собираем ID (пути) файлов, которые нужно обработать.
        paths_to_process = set()
        actions_map = {} # path -> status
        
        for data in self.results_data.values():
            status = data.get('status')
            if status in ['delete', 'ok']:
                internal_path = data['internal_html_path']
                paths_to_process.add(internal_path)
                actions_map[internal_path] = status

        if not paths_to_process:
            QMessageBox.information(self, "Нет действий", "Не было файлов, помеченных для удаления или как 'готовые'.")
            return

        # Создаем временный справочник данных
        data_lookup = {d['internal_html_path']: d for d in self.results_data.values()}
        processed_count = 0
        
        # Список суффиксов для поиска текущего файла
        all_suffixes = api_config.all_translated_suffixes() + [VALIDATED_SUFFIX]

        try:
            # 2. Единый цикл обработки (Файловая система + Менеджер проекта + Очистка списков Валидатора)
            for internal_path in list(paths_to_process):
                if internal_path not in data_lookup: continue
                
                data = data_lookup[internal_path]
                status = actions_map[internal_path]
                source_path = data['path']
                
                # Находим текущий суффикс, чтобы корректно удалить из менеджера
                old_suffix = next((s for s in all_suffixes if source_path.endswith(s)), None)
                
                # --- ЛОГИКА УДАЛЕНИЯ ---
                if status == 'delete':
                    # А. Удаляем физически
                    if os.path.exists(source_path):
                        os.remove(source_path)
                    
                    # Б. Удаляем из менеджера проекта
                    if old_suffix:
                        self.project_manager.remove_translation(internal_path, old_suffix)
                    
                    # В. ГЛАВНОЕ ИСПРАВЛЕНИЕ: Удаляем из списка "на проверку" в Валидаторе
                    # Чтобы кнопка "Анализ" не пыталась потом искать этот файл
                    self.dirty_files.discard(internal_path)
                    
                    processed_count += 1
                
                # --- ЛОГИКА ПРИНЯТИЯ (MARK OK) ---
                elif status == 'ok':
                    if not os.path.exists(source_path):
                        continue

                    # Формируем новое имя
                    base_name = source_path
                    if old_suffix:
                        base_name = source_path[:-len(old_suffix)]
                    destination_path = base_name + VALIDATED_SUFFIX
                    
                    # Перемещаем файл
                    if source_path != destination_path:
                        shutil.move(source_path, destination_path)
                    
                    # Обновляем менеджер проекта
                    if old_suffix:
                        self.project_manager.remove_translation(internal_path, old_suffix)
                    
                    new_relative_path = os.path.relpath(destination_path, self.translated_folder)
                    self.project_manager.register_translation(internal_path, VALIDATED_SUFFIX, new_relative_path)
                    
                    # Файл остается в dirty_files (так как он существует), но теперь он validated.
                    # Кнопка "Анализ" сама решит, проверять его или нет, в зависимости от галочки "Включить готовые".
                    processed_count += 1

            # 3. Принудительно сохраняем изменения структуры проекта на диск
            # Это предотвращает "воскрешение" файлов при перезапуске
            if hasattr(self.project_manager, 'save_project_structure'):
                self.project_manager.save_project_structure()

        except (OSError, shutil.Error) as e:
            QMessageBox.critical(self, "Ошибка файловой операции", f"Произошла ошибка:\n{e}\n\nОперация прервана.")
            # Если упали - лучше перезагрузить таблицу целиком, чтобы отразить реальность
            self._populate_initial_table()
            return

        # 4. Удаляем строки из таблицы (ВИЗУАЛЬНО)
        rows_to_remove = []
        for row in range(self.table_results.rowCount()):
            item = self.table_results.item(row, 0)
            if isinstance(item, SortableChapterItem):
                if item.internal_path in paths_to_process:
                    rows_to_remove.append(row)
        
        rows_to_remove.sort(reverse=True)
        for row in rows_to_remove:
            self.table_results.removeRow(row)
        
        # 5. Синхронизируем данные (перепривязываем row index к данным)
        self._sync_data_with_visual_order()

        # 6. Пересчет счетчика недопереводов (для кнопки в интерфейсе)
        self.untranslated_found_count = 0
        for data in self.results_data.values():
            if 'untranslated_words' in data:
                self.untranslated_found_count += 1
        
        if self.untranslated_found_count > 0:
            self.btn_fix_untranslated.setText(f"Недоперевод ({self.untranslated_found_count})")
            self.btn_fix_untranslated.setEnabled(True)
        else:
            self.btn_fix_untranslated.setText("Недоперевод (лат./иер.)")
            self.btn_fix_untranslated.setEnabled(False)

        # 7. Обновляем состояние кнопки анализа
        # Теперь, так как dirty_files почищен, кнопка должна погаснуть (или уменьшить счетчик)
        self._update_analyze_button_state()

        QMessageBox.information(self, "Завершено", f"Действия применены. Обработано файлов: {processed_count}.")

        # Проверка на оставшиеся файлы
        if self.table_results.rowCount() == 0 and not self.check_show_all.isChecked() and self._are_any_translated_files_left():
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Проблемные файлы обработаны"); msg_box.setText("Что делать с оставшимися 'хорошими' файлами?")
            msg_box.setIcon(QMessageBox.Icon.Question)
            btn_show = msg_box.addButton("Показать для проверки", QMessageBox.ButtonRole.AcceptRole)
            btn_auto = msg_box.addButton("Автоматически пометить 'Готовыми'", QMessageBox.ButtonRole.ActionRole)
            btn_cancel = msg_box.addButton("Ничего не делать", QMessageBox.ButtonRole.RejectRole)
            msg_box.exec()
            if msg_box.clickedButton() == btn_show:
                self.check_show_all.setChecked(True); self.start_analysis()
            elif msg_box.clickedButton() == btn_auto:
                self.auto_process_good_files()



    @pyqtSlot(QTableWidgetItem)
    def open_file_external(self, item):
        row = item.row()
        if row in self.results_data:
            filepath = self.results_data[row]['path']
            QDesktopServices.openUrl(QUrl.fromLocalFile(filepath))

    @pyqtSlot(str, int, int)
    def update_status(self, filename, current, total):
        self.lbl_status.setText(f"Проверка ({current}/{total}): {filename}")

    @pyqtSlot(int, int)
    def on_analysis_finished(self, total_scanned, suspicious_found):
        self.lbl_status.setText(f"Проверка завершена. Проверено глав: {total_scanned}. Найдено проблем: {suspicious_found}.")
        self.btn_analyze.setEnabled(True)
        self.btn_exceptions_manager.setEnabled(True) # Разблокируем кнопку
        self._update_analyze_button_state()
        self.table_results.setSortingEnabled(True)
        if self.table_results.rowCount() > 0:
            self.table_results.selectRow(0)

        
        if self.untranslated_found_count > 0:
            self.btn_fix_untranslated.setEnabled(True)
            self.btn_fix_untranslated.setText(f"Недоперевод ({self.untranslated_found_count})")
        else:
            self.btn_fix_untranslated.setText("Недоперевод (лат./иер.)")
        
        # Добавляем новую проверку: self._are_any_translated_files_left()
        if self.table_results.rowCount() == 0 and not self.check_show_all.isChecked() and self._are_any_translated_files_left():
        
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Проблем не найдено")
            msg_box.setText("Первичная проверка не нашла проблемных файлов. Что вы хотите сделать?")
            msg_box.setIcon(QMessageBox.Icon.Question)

            btn_show = msg_box.addButton("Показать все для ручной проверки", QMessageBox.ButtonRole.AcceptRole)
            btn_auto = msg_box.addButton("Считать все 'Готовыми' и переместить", QMessageBox.ButtonRole.ActionRole)
            btn_cancel = msg_box.addButton("Ничего не делать", QMessageBox.ButtonRole.RejectRole)

            msg_box.exec()

            if msg_box.clickedButton() == btn_show:
                self.check_show_all.setChecked(True)
                self.start_analysis()
            elif msg_box.clickedButton() == btn_auto:
                self.auto_process_good_files()


    def _open_untranslated_fixer(self):
        """
        Собирает данные о недопереводах, ГРУППИРУЕТ одинаковые контексты 
        и открывает диалог для их исправления.
        ВЕРСИЯ 3.1 (С сохранением HTML): Теперь передаем innerHTML, чтобы не терять ссылки.
        """
        # Словарь для группировки: Key = Context String, Value = Data Dict
        grouped_data_map = {}
        soup_cache = {}
        processed_containers_ids = set()

        # 1. ТЕГИ-ОБЕРТКИ ИНЛАЙН (поднимаемся из них наверх)
        INLINE_TAGS = {
            'span', 'a', 'strong', 'em', 'b', 'i', 'u', 'font', 
            'small', 'big', 'sub', 'sup', 'strike', 'code', 'var', 'cite'
        }
        
        # 2. БЕЗОПАСНЫЕ БЛОКИ (из них МОЖНО брать весь текст целиком)
        SAFE_BLOCKS = {
            'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 
            'li', 'dt', 'dd', 'blockquote', 'pre', 'caption', 'figcaption', 'td', 'th', 'label'
        }
    
        # 3. КОРНЕВЫЕ ТЕГИ (из них НИКОГДА нельзя брать текст целиком)
        DANGEROUS_ROOTS = {'body', 'html', 'main', '[document]'}

        for row_index, result_data in self.results_data.items():
            if 'untranslated_words' in result_data:
                html_content = result_data['translated_html']
                
                if row_index not in soup_cache:
                    soup_cache[row_index] = BeautifulSoup(html_content, 'html.parser')
                soup = soup_cache[row_index]
    
                for term in result_data['untranslated_words']:
                    term_pattern = re.compile(re.escape(term), re.IGNORECASE)
                    text_nodes = soup.find_all(string=term_pattern)

                    for node in text_nodes:
                        if not node.parent: continue

                        # --- ФИЛЬТР "ВИДИМОСТИ" ---
                        if isinstance(node, (ProcessingInstruction, Comment, Declaration)):
                            continue
                        
                        if node.find_parent(['head', 'script', 'style', 'title']):
                            continue

                        # --- ШАГ 1: ПОДЪЕМ ПО ДЕРЕВУ (LIFTING) ---
                        effective_container = node.parent
                        while effective_container and effective_container.name in INLINE_TAGS:
                            if effective_container.parent:
                                effective_container = effective_container.parent
                            else:
                                break
                        
                        container_name = effective_container.name
                        
                        # --- ШАГ 2: ОПРЕДЕЛЕНИЕ СТРАТЕГИИ ---
                        use_orphan_mode = False 

                        if container_name in DANGEROUS_ROOTS:
                            use_orphan_mode = True
                        elif container_name in SAFE_BLOCKS:
                            use_orphan_mode = False
                        else:
                            has_block_children = False
                            for child in effective_container.children:
                                if getattr(child, 'name', None) in SAFE_BLOCKS.union({'div', 'section', 'article', 'table', 'ul', 'ol'}):
                                    has_block_children = True
                                    break
                            use_orphan_mode = has_block_children

                        # --- ШАГ 3: ФОРМИРОВАНИЕ ДАННЫХ ---
                        if use_orphan_mode:
                            # РЕЖИМ СИРОТЫ: Берем только сам найденный текст
                            target_object = node 
                            context_text = str(node).strip()
                            location_desc = f"Текст-сирота (в <{container_name}>)"
                            is_orphan_flag = True
                        else:
                            # РЕЖИМ БЛОКА: ВАЖНОЕ ИЗМЕНЕНИЕ!
                            # Мы берем не get_text() (который убивает теги), а собираем innerHTML.
                            # Это сохранит <a href="...">, <b> и прочее для отправки в AI.
                            target_object = effective_container
                            context_text = "".join(str(child) for child in effective_container.contents).strip()
                            location_desc = f"Тег <{container_name}>"
                            is_orphan_flag = False
                        
                        if not context_text: continue
                        
                        if len(context_text) > 2000:
                            target_object = node
                            context_text = str(node).strip()
                            if len(context_text) > 2000: context_text = context_text[:100] + "..."
                            location_desc = f"Текст-сирота (Слишком большой блок <{container_name}>)"
                            is_orphan_flag = True
                            if not context_text: continue

                        group_key = context_text
                        
                        unique_id = id(target_object)
                        if unique_id in processed_containers_ids: continue
                        processed_containers_ids.add(unique_id)

                        if context_text not in grouped_data_map:
                            grouped_data_map[group_key] = {
                                'term': term,
                                'context': context_text,
                                'location_info': location_desc,
                                'occurrences': [] 
                            }
                        
                        grouped_data_map[group_key]['occurrences'].append({
                            'target': target_object,
                            'is_orphan': is_orphan_flag,
                            'row_index': row_index,
                            'soup_ref': soup
                        })
    
        if not grouped_data_map:
            QMessageBox.information(self, "Все чисто", "Не найдено контекстов для исправления.")
            return
        
        data_for_dialog = list(grouped_data_map.values())
    
        dialog = UntranslatedFixerDialog(data_for_dialog, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            changes = dialog.get_changes()
            if not changes:
                return
    
            affected_rows = set()
            total_replacements = 0
    
            for change in changes:
                new_text = change['new_context']
                occurrences = change['occurrences']
                
                # Парсим новый текст как HTML фрагмент
                temp_soup = BeautifulSoup(new_text, 'html.parser')
                content_to_insert = temp_soup.body if temp_soup.body else temp_soup

                for occ in occurrences:
                    target = occ['target']
                    is_orphan = occ.get('is_orphan', False)
                    row_index = occ['row_index']
                    
                    nodes_to_inject = [node.__copy__() for node in content_to_insert.contents]

                    if is_orphan:
                        try:
                            target.replace_with(*nodes_to_inject)
                        except TypeError:
                            if nodes_to_inject:
                                first = nodes_to_inject[0]
                                target.replace_with(first)
                                current = first
                                for extra_node in nodes_to_inject[1:]:
                                    current.insert_after(extra_node)
                                    current = extra_node
                    else:
                        target.clear()
                        if hasattr(target, 'extend'):
                            target.extend(nodes_to_inject)
                        else:
                            for node in nodes_to_inject:
                                target.append(node)
                    
                    affected_rows.add(row_index)
                    total_replacements += 1
            
            for row_idx in affected_rows:
                if row_idx in soup_cache:
                    self.results_data[row_idx]['translated_html'] = str(soup_cache[row_idx])
                    if not self.results_data[row_idx].get('is_edited', False):
                        self.results_data[row_idx]['is_edited'] = True
                        self.table_results.item(row_idx, 3).setText("Редакт.")
                        self.update_row_color(row_idx, 'edited')
            
            self.btn_save_changes.setEnabled(True)
            self.update_comparison_view()
            
            if dialog.should_save_immediately():
                self.save_changes()
                QMessageBox.information(self, "Готово", f"Применено и сохранено {total_replacements} исправлений.")
            else:
                QMessageBox.information(self, "Изменения применены (в памяти)", 
                                        f"Обработано групп: {len(changes)}.\n"
                                        f"Замен: {total_replacements}.\n"
                                        "Не забудьте нажать 'Сохранить изменения'.")


    def request_retry_translation(self):
        files_to_retry = []
        original_epub_path = self.original_epub_path

        for data in self.results_data.values():
            if data['status'] == 'retry':
                files_to_retry.append(data['internal_html_path'])
        
        if not files_to_retry:
            QMessageBox.warning(self, "Ничего не выбрано", "Сначала пометьте файлы 'К переотправке'.")
            return

        # --- НАЧАЛО ИСПРАВЛЕНИЙ ---
        event = {
            'event': 'tasks_for_retry_ready',
            'source': 'TranslationValidator',
            'data': {
                'epub_path': self.original_epub_path,
                'chapter_paths': files_to_retry
            }
        }
        
        app = QtWidgets.QApplication.instance()
        if app and hasattr(app, 'event_bus'):
            # Используем правильный сигнал 'event_posted'
            app.event_bus.event_posted.emit(event)
            # Закрываем окно валидатора после успешной отправки
            self.accept() 
        # --- КОНЕЦ ИСПРАВЛЕНИЙ ---

    def closeEvent(self, event):
        """
        Перехватывает событие закрытия окна.
        1. Проверяет активный поток анализа.
        2. Если retry_enabled=False (автономный режим), спрашивает о выходе в меню.
        """
        # 1. Проверка потока
        if self.analysis_thread and self.analysis_thread.isRunning():
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle('Выход')
            msg_box.setText("Проверка еще не завершена. Прервать и выйти?")
            msg_box.setIcon(QMessageBox.Icon.Question)
            yes_button = msg_box.addButton("Да, прервать", QMessageBox.ButtonRole.YesRole)
            no_button = msg_box.addButton("Нет", QMessageBox.ButtonRole.NoRole)
            msg_box.setDefaultButton(no_button)
            msg_box.exec()
            
            if msg_box.clickedButton() != yes_button:
                event.ignore()
                return
            
            self.analysis_thread.stop()
            if not self.analysis_thread.wait(1000):
                self.analysis_thread.terminate()

        # 2. Логика выхода в меню (только если retry недоступен, т.е. автономный режим)
        if not self.retry_is_available:
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Завершение работы")
            msg_box.setText("Вы хотите закрыть приложение или вернуться в главное меню?")
            msg_box.setIcon(QMessageBox.Icon.Question)
            
            btn_menu = msg_box.addButton("Вернуться в меню", QMessageBox.ButtonRole.ActionRole)
            btn_exit = msg_box.addButton("Выйти из программы", QMessageBox.ButtonRole.DestructiveRole)
            btn_cancel = msg_box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
            
            msg_box.exec()
            clicked = msg_box.clickedButton()
            
            if clicked == btn_cancel:
                event.ignore()
                return
            elif clicked == btn_menu:
                # Устанавливаем спецкод для перезагрузки цикла в main.py
                QApplication.exit(2000) # EXIT_CODE_REBOOT
                event.accept()
            else:
                # Обычный выход
                event.accept()
        else:
            # Обычное поведение для дочернего окна
            event.accept()