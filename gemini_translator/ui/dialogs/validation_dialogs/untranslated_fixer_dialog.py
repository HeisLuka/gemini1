# -*- coding: utf-8 -*-
import re
import math
from bs4 import BeautifulSoup
from PyQt6 import QtGui
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QTableWidget, QHeaderView, QApplication, QHBoxLayout, QLineEdit,
    QDialogButtonBox, QTableWidgetItem, QMessageBox, QGroupBox, QWidget, QListWidget, QStyle,
    QCheckBox, QLabel, QPushButton, QGridLayout, QComboBox, QFrame, QSizePolicy, QLayout, QScrollArea, QMenu
)
from PyQt6.QtCore import Qt, QSize, pyqtSignal, pyqtSlot, QRect, QPoint

# Используем тот же самый делегат, что и в менеджере глоссариев
from ..glossary_dialogs.custom_widgets import ExpandingTextEditDelegate
from ....api import config as api_config
from ..glossary_dialogs.ai_correction import CorrectionSessionDialog

from ...widgets import (
    KeyManagementWidget, ModelSettingsWidget, LogWidget, PresetWidget
)
from ...widgets.common_widgets import NoScrollSpinBox, NoScrollDoubleSpinBox, NoScrollComboBox

# Алиасы для удобства
QSpinBox = NoScrollSpinBox
QDoubleSpinBox = NoScrollDoubleSpinBox
# QComboBox = NoScrollComboBox # Здесь оставляем стандартный или переопределенный по желанию


# Паттерны
ALIEN_WORD_PATTERN = re.compile(r'[^\W\d_а-яА-ЯёЁ]+')
CJK_PATTERN = re.compile(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]')
LATIN_PATTERN = re.compile(r'[a-zA-Z]')
GREEK_PATTERN = re.compile(r'[\u0370-\u03ff\u1f00-\u1fff]')
NORMAL_CHARS_PATTERN = re.compile(r'[а-яА-ЯёЁ0-9\s\.,!?;:«»"\'\-\(\)\[\]\%№—–\/\+\*]')

# --- ВСПОМОГАТЕЛЬНЫЕ КЛАССЫ ИНТЕРФЕЙСА ---

# --- ВСПОМОГАТЕЛЬНЫЕ КЛАССЫ ИНТЕРФЕЙСА ---

class FlowLayout(QLayout):
    """Стандартный FlowLayout для размещения тегов облаком."""
    def __init__(self, parent=None, margin=0, h_spacing=5, v_spacing=5):
        super().__init__(parent)
        if parent is not None: self.setContentsMargins(margin, margin, margin, margin)
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing
        self._items = []

    def addItem(self, item): self._items.append(item)
    def count(self): return len(self._items)
    def itemAt(self, index): return self._items[index] if 0 <= index < len(self._items) else None
    def takeAt(self, index): return self._items.pop(index) if 0 <= index < len(self._items) else None
    def expandingDirections(self): return Qt.Orientation(0)
    def hasHeightForWidth(self): return True
    def heightForWidth(self, width): return self._do_layout(QRect(0, 0, width, 0), True)
    def setGeometry(self, rect): super().setGeometry(rect); self._do_layout(rect, False)
    def sizeHint(self): return self.minimumSize()
    def minimumSize(self):
        size = QSize()
        for item in self._items: size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do_layout(self, rect, test_only):
        x, y, line_height = rect.x(), rect.y(), 0
        spacing_x, spacing_y = self._h_spacing, self._v_spacing
        
        for item in self._items:
            w = item.widget()
            space_x = spacing_x
            space_y = spacing_y
            next_x = x + item.sizeHint().width() + space_x
            if next_x - space_x > rect.right() and line_height > 0:
                x = rect.x()
                y = y + line_height + space_y
                next_x = x + item.sizeHint().width() + space_x
                line_height = 0
            if not test_only: item.setGeometry(QRect(QPoint(x, y), item.sizeHint()))
            x = next_x
            line_height = max(line_height, item.sizeHint().height())
        return y + line_height - rect.y()

class FilterTagWidget(QFrame):
    """Виджет одного тега: [ (+) Термин | X ] или [ (-) Термин | X ]"""
    removed = pyqtSignal(str, str) # term, type

    def __init__(self, text, tag_type, parent=None):
        super().__init__(parent)
        self.text = text
        self.tag_type = tag_type # 'whitelist' or 'blacklist'
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 2, 5, 2)
        layout.setSpacing(5)
        
        # Визуал
        self.setFrameShape(QFrame.Shape.StyledPanel)
        
        # CSS: 
        # 1. padding: 0px для кнопки (чтобы крестик был виден).
        # 2. Спец-стиль для QLabel#separator (делаем его тоньше и полупрозрачным).
        if tag_type == 'whitelist':
            # Зеленоватый
            self.setStyleSheet("""
                QFrame { background-color: rgba(46, 204, 113, 40); border: 1px solid #2ECC71; border-radius: 4px; }
                QLabel { color: #2ECC71; font-weight: bold; border: none; background: transparent; }
                QLabel#separator { font-weight: normal; color: rgba(46, 204, 113, 0.6); }
                QPushButton { background: transparent; color: #2ECC71; font-weight: bold; border: none; padding: 0px; }
                QPushButton:hover { color: #fff; }
            """)
            prefix = "(+)"
        else:
            # Красноватый
            self.setStyleSheet("""
                QFrame { background-color: rgba(231, 76, 60, 40); border: 1px solid #E74C3C; border-radius: 4px; }
                QLabel { color: #E74C3C; font-weight: bold; border: none; background: transparent; }
                QLabel#separator { font-weight: normal; color: rgba(231, 76, 60, 0.6); }
                QPushButton { background: transparent; color: #E74C3C; font-weight: bold; border: none; padding: 0px; }
                QPushButton:hover { color: #fff; }
            """)
            prefix = "(-)"

        # 1. Текст тега
        lbl = QLabel(f"{prefix} {text}")
        layout.addWidget(lbl)
        
        # 2. Сепаратор (НОВОЕ)
        sep = QLabel("|")
        sep.setObjectName("separator") # Имя для CSS селектора выше
        layout.addWidget(sep)
        
        # 3. Кнопка удаления
        btn_close = QPushButton("×")
        btn_close.setFixedSize(16, 16)
        btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_close.clicked.connect(self._on_remove)
        layout.addWidget(btn_close)

    def _on_remove(self):
        self.removed.emit(self.text, self.tag_type)
        self.deleteLater()
   
# --- КЛАСС: Расширенный диалог фильтрации (Облако тегов) ---
class AdvancedTagFilterDialog(QDialog):
    
    def __init__(self, whitelist_set, blacklist_set, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Фильтр терминов (Теги)")
        self.resize(600, 450)
        
        # Копируем сеты, чтобы изменения применились только по кнопке ОК
        self.whitelist = set(whitelist_set)
        self.blacklist = set(blacklist_set)
        
        layout = QVBoxLayout(self)
        
        # --- Блок добавления ---
        input_group = QGroupBox("Новый фильтр")
        input_layout = QHBoxLayout(input_group)
        
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("Введите слово или часть слова...")
        self.input_edit.returnPressed.connect(self._add_to_whitelist)
        
        self.btn_add_white = QPushButton("Требовать (+)")
        self.btn_add_white.setStyleSheet("background-color: #2ECC71; color: white; font-weight: bold;")
        self.btn_add_white.clicked.connect(self._add_to_whitelist)
        
        self.btn_add_black = QPushButton("Исключать (-)")
        self.btn_add_black.setStyleSheet("background-color: #E74C3C; color: white; font-weight: bold;")
        self.btn_add_black.clicked.connect(self._add_to_blacklist)
        
        input_layout.addWidget(self.input_edit)
        input_layout.addWidget(self.btn_add_white)
        input_layout.addWidget(self.btn_add_black)
        
        layout.addWidget(input_group)
        
        # --- Область тегов ---
        layout.addWidget(QLabel("<b>Активные фильтры (нажмите ✖ для удаления):</b>"))
        
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        # Серый фон для контраста тегов
        scroll.setStyleSheet("QScrollArea { background-color: #2b2b2b; border-radius: 4px; }") 
        
        self.container = QWidget()
        self.container.setStyleSheet("background: transparent;")
        self.flow_layout = FlowLayout(self.container, margin=15, h_spacing=10, v_spacing=10)
        scroll.setWidget(self.container)
        
        layout.addWidget(scroll)
        
        # --- Кнопки диалога (РУСИФИКАЦИЯ) ---
        bbox = QDialogButtonBox()
        # Добавляем свои кнопки вместо стандартных флагов
        apply_btn = bbox.addButton("Применить фильтры", QDialogButtonBox.ButtonRole.AcceptRole)
        cancel_btn = bbox.addButton("Отмена", QDialogButtonBox.ButtonRole.RejectRole)
        
        bbox.accepted.connect(self.accept)
        bbox.rejected.connect(self.reject)
        layout.addWidget(bbox)
        
        self._render_tags()


    def _render_tags(self):
        # Очистка лейаута
        while self.flow_layout.count():
            item = self.flow_layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()

        # Рендер White
        for term in sorted(list(self.whitelist)):
            self._create_tag(term, 'whitelist')
            
        # Рендер Black
        for term in sorted(list(self.blacklist)):
            self._create_tag(term, 'blacklist')

    def _create_tag(self, term, t_type):
        tag = FilterTagWidget(term, t_type)
        tag.removed.connect(self._on_tag_removed)
        self.flow_layout.addWidget(tag)

    def _on_tag_removed(self, term, t_type):
        if t_type == 'whitelist': self.whitelist.discard(term)
        else: self.blacklist.discard(term)
        # Удаление происходит внутри виджета, нам надо только обновить данные
        # Но для красивой перекомпоновки FlowLayout можно вызвать update, но он сам справится

    def _add_to_whitelist(self):
        text = self.input_edit.text().strip().lower()
        if text and text not in self.whitelist and text not in self.blacklist:
            self.whitelist.add(text)
            self._create_tag(text, 'whitelist')
            self.input_edit.clear()

    def _add_to_blacklist(self):
        text = self.input_edit.text().strip().lower()
        if text and text not in self.whitelist and text not in self.blacklist:
            self.blacklist.add(text)
            self._create_tag(text, 'blacklist')
            self.input_edit.clear()
            
    def get_lists(self):
        return self.whitelist, self.blacklist
        
# --- ОСНОВНОЙ КЛАСС ---
class UntranslatedFixerDialog(QDialog):
    """
    Диалог для пакетного исправления найденных недопереводов.
    Версия 6.0: Пагинация и сохранение выделения.
    """
    def __init__(self, data_list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Помощник исправления недопереводов")
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
        
        
        self.original_data = data_list
        self.changes = []
        
        # --- ДВА СПИСКА ---
        self.blacklist_set = set() # Бывший temp_ignore_set
        self.whitelist_set = set()
        
        self.filtered_indices = []
        self.selected_indices = set()
        
        self.current_page = 0
        self.page_size = 50
        
        self._pre_analyze_data()
        self.init_ui()
        self.apply_filters()
        
        self.selected_indices = set(self.filtered_indices)
        self.update_table_view()

    def _pre_analyze_data(self):
        for item in self.original_data:
            term = item['term']
            raw_context = item['context']
            
            if CJK_PATTERN.search(term): item['lang_tag'] = 'cjk'
            elif LATIN_PATTERN.search(term): item['lang_tag'] = 'latin'
            elif GREEK_PATTERN.search(term): item['lang_tag'] = 'greek'
            else: item['lang_tag'] = 'other'

            try:
                clean_text = BeautifulSoup(raw_context, 'html.parser').get_text()
            except:
                clean_text = raw_context

            total_len = len(clean_text)
            alien_chars = len([c for c in clean_text if not NORMAL_CHARS_PATTERN.match(c)])
            alien_ratio = (alien_chars / total_len * 100.0) if total_len > 0 else 0.0
            
            item['stats'] = (total_len, alien_chars, alien_ratio)
            item['clean_text_cache'] = clean_text

    def init_ui(self):
        layout = QVBoxLayout(self)

        # --- ФИЛЬТРЫ ---
        filter_group = QGroupBox("Параметры фильтрации")
        filter_layout = QGridLayout(filter_group)
        filter_layout.setSpacing(10)
        
        # Ряд 1: Чекбоксы языков + Кнопка фильтров справа
        lang_layout = QHBoxLayout()
        self.chk_latin = QCheckBox("Латиница"); self.chk_latin.setChecked(True)
        self.chk_cjk = QCheckBox("Азия (CJK)"); self.chk_cjk.setChecked(True)
        self.chk_greek = QCheckBox("Греческий"); self.chk_greek.setChecked(True)
        self.chk_other = QCheckBox("Другое"); self.chk_other.setChecked(True)
        
        for chk in [self.chk_latin, self.chk_cjk, self.chk_greek, self.chk_other]:
            chk.stateChanged.connect(self.apply_filters)
            lang_layout.addWidget(chk)
        
        lang_layout.addStretch() # Пружина сдвигает всё вправо
        
        # --- БЛОК УПРАВЛЕНИЯ ТЕГАМИ (Справа) ---
        # Зеленый счетчик (WhiteList)
        self.lbl_white_count = QLabel("0")
        self.lbl_white_count.setStyleSheet("color: #2ECC71; font-weight: bold; font-size: 10pt;")
        self.lbl_white_count.setToolTip("Количество активных фильтров 'Требовать' (+)")
        
        # Разделитель
        sep_slash = QLabel("/")
        sep_slash.setStyleSheet("color: gray;")
        
        # Красный счетчик (BlackList)
        self.lbl_black_count = QLabel("0")
        self.lbl_black_count.setStyleSheet("color: #E74C3C; font-weight: bold; font-size: 10pt;")
        self.lbl_black_count.setToolTip("Количество активных фильтров 'Исключать' (-)")
        
        # Кнопка настройки
        self.btn_open_tags = QPushButton("⚙️ Фильтры")
        self.btn_open_tags.setToolTip("Настроить списки исключений и требований (Whitelist/Blacklist)")
        self.btn_open_tags.clicked.connect(self._open_tag_manager)
        
        lang_layout.addWidget(self.lbl_white_count)
        lang_layout.addWidget(sep_slash)
        lang_layout.addWidget(self.lbl_black_count)
        lang_layout.addSpacing(5)
        lang_layout.addWidget(self.btn_open_tags)
        
        filter_layout.addWidget(QLabel("<b>Типы языков:</b>"), 0, 0)
        filter_layout.addLayout(lang_layout, 0, 1)

        # Ряд 2: Числовые параметры
        numeric_row = QHBoxLayout()
        self.ratio_mode_combo = QComboBox(); self.ratio_mode_combo.addItems(["% чужеродности", "Кол-во чужих симв."])
        self.ratio_op = QComboBox(); self.ratio_op.addItems([">", "<", "="]); self.ratio_op.setCurrentText(">")
        self.ratio_spin = QDoubleSpinBox(); self.ratio_spin.setRange(0, 100); self.ratio_spin.setValue(0.0); self.ratio_spin.setSingleStep(5.0)

        numeric_row.addWidget(self.ratio_mode_combo)
        numeric_row.addWidget(self.ratio_op)
        numeric_row.addWidget(self.ratio_spin)
        numeric_row.addSpacing(30)
        
        self.len_op = QComboBox(); self.len_op.addItems([">", "<", "="]); self.len_op.setCurrentText(">")
        self.len_spin = QSpinBox(); self.len_spin.setRange(0, 50000); self.len_spin.setValue(0); self.len_spin.setSingleStep(10)

        numeric_row.addWidget(QLabel("<b>Общая длина:</b>"))
        numeric_row.addWidget(self.len_op)
        numeric_row.addWidget(self.len_spin)
        numeric_row.addStretch()

        self.ratio_mode_combo.currentIndexChanged.connect(self._on_ratio_mode_changed)
        self.ratio_mode_combo.currentIndexChanged.connect(self.apply_filters)
        for w in [self.ratio_op, self.ratio_spin, self.len_op, self.len_spin]:
            if isinstance(w, QComboBox): w.currentIndexChanged.connect(self.apply_filters)
            else: w.valueChanged.connect(self.apply_filters)

        filter_layout.addLayout(numeric_row, 1, 0, 1, 2)
        
        layout.addWidget(filter_group)
        
        # --- ТАБЛИЦА ---
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        # Меняем заголовок последней колонки
        self.table.setHorizontalHeaderLabels(["✅", "Термин / Локация", "Контекст (редактируемый)", "Инфо", "Действ."])
        
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(4, 50)

        delegate = ExpandingTextEditDelegate(self.table)
        self.table.setItemDelegateForColumn(2, delegate)
        self.table.setWordWrap(True)
        self.table.verticalHeader().setDefaultSectionSize(60)
        
        self.table.cellChanged.connect(self._on_cell_changed)
        
        layout.addWidget(self.table)

        # --- ПАНЕЛЬ УПРАВЛЕНИЯ И ПАГИНАЦИЯ ---
        control_panel = QFrame()
        cp_layout = QHBoxLayout(control_panel)
        
        self.btn_check_vis = QPushButton("☑ Все на странице")
        self.btn_uncheck_vis = QPushButton("☐ Снять на странице")
        self.btn_check_vis.clicked.connect(lambda: self.toggle_visible_selection(True))
        self.btn_uncheck_vis.clicked.connect(lambda: self.toggle_visible_selection(False))
        
        cp_layout.addWidget(self.btn_check_vis)
        cp_layout.addWidget(self.btn_uncheck_vis)
        cp_layout.addWidget(QLabel("|"))

        self.btn_prev_page = QPushButton("◀")
        self.btn_next_page = QPushButton("▶")
        self.lbl_page_info = QLabel("Страница 1 из 1")
        self.spin_page_size = QSpinBox()
        self.spin_page_size.setRange(10, 500)
        self.spin_page_size.setValue(50)
        self.spin_page_size.setSuffix(" строк/стр.")
        self.spin_page_size.setFixedWidth(120)
        
        self.btn_prev_page.clicked.connect(self.prev_page)
        self.btn_next_page.clicked.connect(self.next_page)
        self.spin_page_size.valueChanged.connect(self._on_page_size_changed)

        cp_layout.addWidget(self.btn_prev_page)
        cp_layout.addWidget(self.lbl_page_info)
        cp_layout.addWidget(self.btn_next_page)
        cp_layout.addWidget(self.spin_page_size)
        
        cp_layout.addStretch()
        
        self.total_filtered_label = QLabel("Всего: 0 (Выбрано: 0)")
        self.total_filtered_label.setStyleSheet("font-weight: bold; color: #aaa;")
        
        self.btn_clear_selected = QPushButton("🗑️ Очистить текст (Выбранное)")
        self.btn_clear_selected.setStyleSheet("background-color: #5a2d2d; color: white;")
        self.btn_clear_selected.clicked.connect(self._clear_selected_content)
        
        self.ai_translate_btn = QPushButton("🤖 Перевести выбранное...")
        self.ai_translate_btn.setStyleSheet("font-weight: bold; font-size: 11pt; padding: 5px;")
        self.ai_translate_btn.clicked.connect(self._start_ai_translation)
        
        cp_layout.addWidget(self.total_filtered_label)
        cp_layout.addWidget(self.btn_clear_selected)
        cp_layout.addWidget(self.ai_translate_btn)
        
        layout.addWidget(control_panel)

        # --- Dialog Buttons ---
        button_box = QDialogButtonBox()
        apply_button = button_box.addButton("Применить и закрыть", QDialogButtonBox.ButtonRole.AcceptRole)
        cancel_button = button_box.addButton("Отмена", QDialogButtonBox.ButtonRole.RejectRole)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)
        
        # Обновляем цифры при старте
        self._update_tags_info_label()
    # --- ПАГИНАЦИЯ ---
    def _on_page_size_changed(self):
        self.page_size = self.spin_page_size.value()
        self.current_page = 0
        self.update_table_view()

    def prev_page(self):
        if self.current_page > 0:
            self.current_page -= 1
            self.update_table_view()

    def next_page(self):
        max_page = max(0, (len(self.filtered_indices) - 1) // self.page_size)
        if self.current_page < max_page:
            self.current_page += 1
            self.update_table_view()
    
    def _open_tag_manager(self):
        dlg = AdvancedTagFilterDialog(self.whitelist_set, self.blacklist_set, self)
        if dlg.exec():
            self.whitelist_set, self.blacklist_set = dlg.get_lists()
            self._update_tags_info_label()
            self.apply_filters()

    def _update_tags_info_label(self):
        w = len(self.whitelist_set)
        b = len(self.blacklist_set)
        # Обновляем наши новые красивые лейблы
        if hasattr(self, 'lbl_white_count'):
            self.lbl_white_count.setText(str(w))
        if hasattr(self, 'lbl_black_count'):
            self.lbl_black_count.setText(str(b))
        
    # --- ЛОГИКА ---
            
    def _on_ratio_mode_changed(self):
        mode = self.ratio_mode_combo.currentIndex()
        self.ratio_spin.blockSignals(True)
        if mode == 0: 
            self.ratio_spin.setRange(0, 100); self.ratio_spin.setSuffix("%"); self.ratio_spin.setDecimals(1); self.ratio_spin.setSingleStep(5.0)
            if self.ratio_spin.value() > 100: self.ratio_spin.setValue(100)
        else:
            self.ratio_spin.setRange(0, 10000); self.ratio_spin.setSuffix(" шт."); self.ratio_spin.setDecimals(0); self.ratio_spin.setSingleStep(1.0)
        self.ratio_spin.blockSignals(False)

    def _check_numeric_condition(self, value, op, target):
        if op == ">": return value > target
        if op == "<": return value < target
        return value == target

    def apply_filters(self):
        self._save_current_view_changes()

        active_tags = set()
        if self.chk_latin.isChecked(): active_tags.add('latin')
        if self.chk_cjk.isChecked(): active_tags.add('cjk')
        if self.chk_greek.isChecked(): active_tags.add('greek')
        if self.chk_other.isChecked(): active_tags.add('other')
        
        ratio_mode = self.ratio_mode_combo.currentIndex()
        target_ratio = self.ratio_spin.value()
        op_ratio = self.ratio_op.currentText()
        target_len = self.len_spin.value()
        op_len = self.len_op.currentText()
        
        self.filtered_indices = []
        
        for i, item in enumerate(self.original_data):
            # 1. Базовый фильтр по типу языка (по основному кандидату)
            if item.get('lang_tag') not in active_tags: continue
            
            clean_text = item.get('clean_text_cache', '')
            clean_text_lower = clean_text.lower()
            
            # --- ИЗМЕНЕННАЯ ЛОГИКА ЧЕРНОГО СПИСКА ---
            
            # 1. Находим ВСЕ кандидаты в этом тексте заново
            all_candidates = ALIEN_WORD_PATTERN.findall(clean_text)
            
            # 2. Фильтруем кандидатов: оставляем только тех, кого НЕТ в черном списке
            remaining_candidates = []
            valid_alien_chars_count = 0
            
            if self.blacklist_set:
                for w in all_candidates:
                    if w.lower() not in self.blacklist_set:
                        remaining_candidates.append(w)
                        valid_alien_chars_count += len(w)
            else:
                remaining_candidates = all_candidates
                # Берем предрасчитанное, если фильтра нет (быстрее)
                valid_alien_chars_count = item['stats'][1] 

            # 3. Если после фильтрации не осталось ни одного кандидата — скрываем строку
            # (Значит, все "чужие" слова в этом абзаце — легальны)
            if not remaining_candidates: 
                continue

            # --- ЛОГИКА БЕЛОГО СПИСКА ---
            # Работает как "Поиск": требуем наличия слова в тексте
            if self.whitelist_set:
                if not any(good in clean_text_lower for good in self.whitelist_set):
                    continue

            # --- ПЕРЕСЧЕТ СТАТИСТИКИ ---
            # Пересчитываем ratio только на основе ОСТАВШИХСЯ кандидатов
            total_len = item['stats'][0]
            
            # Если мы фильтровали, пересчитываем ratio
            if self.blacklist_set:
                alien_ratio = (valid_alien_chars_count / total_len * 100.0) if total_len > 0 else 0.0
                alien_chars = valid_alien_chars_count
            else:
                # Иначе берем из кэша
                alien_chars = item['stats'][1]
                alien_ratio = item['stats'][2]

            # Обновляем данные для отображения в таблице (показываем только актуальные проблемы)
            remaining_candidates.sort(key=len, reverse=True)
            display_terms = ", ".join(remaining_candidates[:3])
            if len(remaining_candidates) > 3: display_terms += "..."
            
            item['_display_term'] = display_terms
            
            item['_all_candidates'] = remaining_candidates 
            # ------------------------------------

            # Перезаписываем статистику для отображения в колонке "Инфо"
            item['_current_stats'] = (total_len, alien_chars, alien_ratio)

            # --- ЧИСЛОВЫЕ ФИЛЬТРЫ ---
            val_to_check = alien_ratio if ratio_mode == 0 else alien_chars
            
            if not self._check_numeric_condition(val_to_check, op_ratio, target_ratio): continue
            if not self._check_numeric_condition(total_len, op_len, target_len): continue
                
            self.filtered_indices.append(i)
        
        self.current_page = 0
        filtered_set = set(self.filtered_indices)
        self.selected_indices = self.selected_indices.intersection(filtered_set)
        
        self.update_table_view()


    def update_table_view(self, save_ui=True):
        """
        Обновление таблицы с учетом пагинации.
        :param save_ui: Если True, сохраняет текущие правки из ячеек в память перед обновлением.
                        Если False, просто перерисовывает таблицу (используется после применения перевода).
        """
        if save_ui:
            self._save_current_view_changes()
        
        total_items = len(self.filtered_indices)
        max_page = max(0, (total_items - 1) // self.page_size)
        
        if self.current_page > max_page: self.current_page = max_page
        
        start_idx = self.current_page * self.page_size
        end_idx = min(start_idx + self.page_size, total_items)
        
        batch_indices = self.filtered_indices[start_idx:end_idx]
        
        # Обновляем UI элементы
        self.lbl_page_info.setText(f"Стр. {self.current_page + 1} из {max_page + 1}")
        self.btn_prev_page.setEnabled(self.current_page > 0)
        self.btn_next_page.setEnabled(self.current_page < max_page)
        
        self._update_counts_label()
        self.populate_table(batch_indices)
   
    def _update_counts_label(self):
        self.total_filtered_label.setText(f"Всего: {len(self.filtered_indices)} (Выбрано: {len(self.selected_indices)})")

    def _save_current_view_changes(self):
        """Сохраняем текст из виджетов в структуру данных."""
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 2)
            if not item: continue
            idx = item.data(Qt.ItemDataRole.UserRole)
            txt = item.text().strip()
            stored = self.original_data[idx].get('new_context', self.original_data[idx]['context'])
            if txt != stored: self.original_data[idx]['new_context'] = txt

    def populate_table(self, indices):
        self.table.blockSignals(True) 
        
        self.table.clearContents()
        self.table.setRowCount(0)

        self.table.setRowCount(len(indices))
        
        for row, idx in enumerate(indices):
            data = self.original_data[idx]
            
            # 0. Checkbox
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            state = Qt.CheckState.Checked if idx in self.selected_indices else Qt.CheckState.Unchecked
            chk.setCheckState(state)
            chk.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            chk.setData(Qt.ItemDataRole.UserRole, idx) 
            
            # 1. Term
            display_term = data.get('_display_term', data['term'])
            loc = data.get('location_info', '')
            lang = data.get('lang_tag', '?').upper()
            
            term_item = QTableWidgetItem(f"{display_term}\n[{lang}] {loc}")
            term_item.setFlags(term_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if "Текст вне абзацев" in loc: term_item.setForeground(QtGui.QColor("#E74C3C"))
            
            # 2. Context
            original_ctx = data['context']
            current_ctx = data.get('new_context', original_ctx)
            
            ctx_item = QTableWidgetItem(current_ctx)
            ctx_item.setData(Qt.ItemDataRole.UserRole, idx)
            
            if current_ctx != original_ctx:
                ctx_item.setBackground(QtGui.QColor(58, 75, 95, 120))
            
            # 3. Info
            tot, alien, ratio = data.get('_current_stats', data.get('stats', (0,0,0)))
            info_item = QTableWidgetItem(f"{tot} | {alien} | {ratio:.1f}%")
            info_item.setFlags(info_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            info_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if ratio > 40: info_item.setForeground(QtGui.QColor("#E74C3C"))
            elif ratio > 0: info_item.setForeground(QtGui.QColor("#F39C12"))
            else: info_item.setForeground(QtGui.QColor("#2ECC71"))

            # 4. Action Button (Menu)
            btn_widget = QWidget()
            btn_layout = QHBoxLayout(btn_widget)
            btn_layout.setContentsMargins(0, 0, 0, 0)
            btn_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            
            # Вместо простого игнора делаем меню действий
            action_btn = QPushButton("⚡")
            action_btn.setFixedSize(30, 24)
            action_btn.setToolTip("Меню действий: Фильтр / Игнор")
            
            # Передаем индекс данных, чтобы достать кандидатов
            action_btn.clicked.connect(lambda ch, i=idx, btn=action_btn: self._show_row_action_menu(i, btn))
            
            btn_layout.addWidget(action_btn)
            
            self.table.setItem(row, 0, chk)
            self.table.setItem(row, 1, term_item)
            self.table.setItem(row, 2, ctx_item)
            self.table.setItem(row, 3, info_item)
            self.table.setCellWidget(row, 4, btn_widget)
        
        self.table.blockSignals(False)
    
    def _show_row_action_menu(self, data_index, button_widget):
        item_data = self.original_data[data_index]
        candidates = item_data.get('_all_candidates', [])
        
        unique_candidates = []
        seen = set()
        for c in candidates:
            if c.lower() not in seen:
                seen.add(c.lower())
                unique_candidates.append(c)
        
        # Показываем больше кандидатов
        top_candidates = unique_candidates[:15]
        
        menu = QMenu(self)
        # СТИЛИ МЕНЮ ДЛЯ РЕАКЦИИ НА НАВЕДЕНИЕ
        menu.setStyleSheet("""
            QMenu {
                background-color: #2b2b2b;
                border: 1px solid #555;
            }
            QMenu::item {
                padding: 6px 25px 6px 20px;
                color: #e0e0e0;
                font-size: 10pt;
            }
            QMenu::item:selected {
                background-color: #3498db; /* Ярко-синий при наведении */
                color: #ffffff;
            }
            QMenu::separator {
                height: 1px;
                background: #555;
                margin: 5px 0;
            }
            QMenu::item:disabled {
                color: #666;
            }
        """)

        # --- Раздел White List (Требовать) ---
        if top_candidates:
            # Заголовок теперь активная кнопка "Требовать ВСЕ"
            title_white = menu.addAction("🟢 Требовать ВСЕ")
            title_white.setToolTip("Добавить все слова ниже в список обязательных")
            title_white.triggered.connect(lambda: self._add_multiple_filter_tags(top_candidates, 'white'))
            
            for term in top_candidates:
                action_text = f'   + "{term}"'
                action = menu.addAction(action_text)
                action.triggered.connect(lambda ch, t=term: self._add_filter_tag(t, 'white'))
        else:
            dummy = menu.addAction("🟢 Требовать (нет слов)")
            dummy.setEnabled(False)

        menu.addSeparator()

        # --- Раздел Black List (Скрыть) ---
        if top_candidates:
            # Заголовок теперь активная кнопка "Скрыть ВСЕ"
            title_black = menu.addAction("🔴 Скрыть ВСЕ")
            title_black.setToolTip("Добавить все слова ниже в список исключений")
            title_black.triggered.connect(lambda: self._add_multiple_filter_tags(top_candidates, 'black'))

            for term in top_candidates:
                action_text = f'   - "{term}"'
                action = menu.addAction(action_text)
                action.triggered.connect(lambda ch, t=term: self._add_filter_tag(t, 'black'))
        else:
            dummy = menu.addAction("🔴 Скрыть (нет слов)")
            dummy.setEnabled(False)

        # Показываем меню
        menu.exec(button_widget.mapToGlobal(button_widget.rect().bottomLeft()))
    
    def _add_multiple_filter_tags(self, terms, list_type):
        """Пакетное добавление списка терминов."""
        updated = False
        target_set = self.whitelist_set if list_type == 'white' else self.blacklist_set
        
        for term in terms:
            t = term.lower()
            if t not in target_set:
                target_set.add(t)
                updated = True
        
        if updated:
            self._update_tags_info_label()
            self.apply_filters()

    def _add_filter_tag(self, term, list_type):
        term = term.lower()
        if list_type == 'white':
            self.whitelist_set.add(term)
        else:
            self.blacklist_set.add(term)
            
        self._update_tags_info_label()
        self.apply_filters()
        
    def _set_whitelist_filter(self, term):
        """Устанавливает термин в поле поиска и обновляет таблицу."""
        self.whitelist_edit.setText(term)
        # apply_filters вызовется автоматически через сигнал textChanged
        
    def _on_cell_changed(self, row, col):
        """Обработка кликов по чекбоксам для обновления глобального множества."""
        if col == 0:
            item = self.table.item(row, 0)
            idx = item.data(Qt.ItemDataRole.UserRole)
            if item.checkState() == Qt.CheckState.Checked:
                self.selected_indices.add(idx)
            else:
                self.selected_indices.discard(idx)
            self._update_counts_label()

    def toggle_visible_selection(self, check):
        """Меняет состояние только для ВИДИМЫХ на странице элементов."""
        self.table.blockSignals(True)
        state = Qt.CheckState.Checked if check else Qt.CheckState.Unchecked
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            idx = item.data(Qt.ItemDataRole.UserRole)
            item.setCheckState(state)
            if check:
                self.selected_indices.add(idx)
            else:
                self.selected_indices.discard(idx)
        self.table.blockSignals(False)
        self._update_counts_label()

    def _clear_selected_content(self):
        """
        Очищает текст. Показывает диалог с выбором:
        - для ФИЗИЧЕСКИ выделенных (синим) строк
        - для всех ОТМЕЧЕННЫХ галочками строк
        """
        # Создаем диалог выбора
        msg = QMessageBox(self)
        msg.setWindowTitle("Выбор режима очистки")
        msg.setText("Какой текст вы хотите очистить?")
        msg.setIcon(QMessageBox.Icon.Question)
        
        b_selected = msg.addButton("Выделенное курсором", QMessageBox.ButtonRole.ActionRole)
        b_checked = msg.addButton("Отмеченное флажками", QMessageBox.ButtonRole.ActionRole)
        b_cancel = msg.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        
        msg.exec()
        
        clicked_button = msg.clickedButton()
        
        if clicked_button == b_cancel or clicked_button is None:
            return # Пользователь отменил

        # --- Вариант 1: Очистка выделенного курсором (старое поведение) ---
        if clicked_button == b_selected:
            selected_items = self.table.selectedItems()
            if not selected_items:
                QMessageBox.warning(self, "Ничего не выделено", "Выделите строки мышкой (через Shift/Ctrl), текст которых нужно очистить.")
                return

            rows = sorted(list(set(item.row() for item in selected_items)))
            count = 0
            for row in rows:
                ctx_item = self.table.item(row, 2)
                if not ctx_item: continue
                
                idx = ctx_item.data(Qt.ItemDataRole.UserRole)
                self.original_data[idx]['new_context'] = ""
                ctx_item.setText("")
                ctx_item.setBackground(QtGui.QColor(90, 58, 58, 100))
                count += 1
            
            if count > 0:
                QMessageBox.information(self, "Очищено", f"Очищен текст в {count} выделенных строках.")

        # --- Вариант 2: Очистка всех отмеченных флагом ---
        elif clicked_button == b_checked:
            if not self.selected_indices:
                QMessageBox.warning(self, "Ничего не отмечено", "Нет элементов, отмеченных флагом.")
                return
            
            # Сначала сохраняем любые ручные правки на текущей странице
            self._save_current_view_changes()

            count = 0
            # Проходим по всем отмеченным индексам (даже на других страницах)
            for idx in self.selected_indices:
                self.original_data[idx]['new_context'] = ""
                count += 1
                
            # Важно! Обновляем всю таблицу, так как изменения могли затронуть другие страницы
            self.update_table_view(save_ui=False)
            
            if count > 0:
                QMessageBox.information(self, "Очищено", f"Очищен текст в {count} отмеченных флагом строках.")

    def _start_ai_translation(self):
        # 1. Сначала сохраняем ручные правки, если они были до нажатия кнопки
        self._save_current_view_changes()
        
        if not self.selected_indices:
            return QMessageBox.warning(self, "Ничего не выбрано", "Нет отмеченных элементов для перевода.")
            
        sorted_indices = sorted(list(self.selected_indices))
        
        # 2. Формируем список задач
        tasks_list = []
        batch_size = self.page_size
        
        for i in range(0, len(sorted_indices), batch_size):
            batch_indices = sorted_indices[i : i + batch_size]
            html_parts = []
            for idx in batch_indices:
                data_item = self.original_data[idx]
                # Берем текущий контекст (даже если он уже правился руками)
                txt = data_item.get('new_context', data_item['context'])
                uid = f"{idx}"
                html_parts.append(f'<p data-id="{uid}">{txt}</p>')
            
            full_html = "<html><body>" + "\n".join(html_parts) + "</body></html>"
            tasks_list.append(full_html)

        # 3. Запускаем диалог
        parent = self.parent()
        if not (parent and hasattr(parent, 'settings_manager')): return
        
        dlg = AITranslationDialog(tasks_list, parent.settings_manager, self)
        
        # Если нажали "Применить" (Accepted)
        if dlg.exec():
            results = dlg.get_translated_results()
            if not results: return
            
            updated_count = 0
            
            for html_res in results:
                try:
                    soup = BeautifulSoup(html_res, 'html.parser')
                    for p in soup.find_all('p', attrs={'data-id': True}):
                        uid = p['data-id']
                        try:
                            idx = int(uid)
                            new_txt = p.decode_contents()
                            
                            # Обновляем данные в памяти
                            self.original_data[idx]['new_context'] = new_txt
                            
                            # Раньше тут снималось выделение, теперь оставляем:
                            # self.selected_indices.discard(idx) 
                            
                            updated_count += 1
                        except:
                            pass
                except Exception as e:
                    print(f"Error parsing result: {e}")
            
            # ВАЖНО: Вызываем обновление с save_ui=False, 
            # чтобы старый текст из ячеек не перезаписал только что полученный перевод
            self.update_table_view(save_ui=False)
            
            QMessageBox.information(self, "Готово", f"Успешно обновлено строк: {updated_count}")


    def accept(self):
        self._save_current_view_changes()
        self.changes.clear()
        for item in self.original_data:
            if 'new_context' in item and item['new_context'] != item['context']:
                self.changes.append(item.copy())
        
        if not self.changes: super().accept(); return

        msg = QMessageBox(self)
        msg.setWindowTitle("Применение")
        msg.setText(f"Изменений: {len(self.changes)}.")
        msg.setInformativeText("Как применить?")
        
        b_save = msg.addButton("Применить и Сохранить", QMessageBox.ButtonRole.AcceptRole)
        b_apply = msg.addButton("Только в память", QMessageBox.ButtonRole.YesRole)
        b_cancel = msg.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        
        msg.exec()
        
        if msg.clickedButton() == b_cancel or msg.clickedButton() is None: 
            return

        self._should_save_immediately = (msg.clickedButton() == b_save)
        super().accept()

    def should_save_immediately(self): return getattr(self, '_should_save_immediately', False)
    def get_changes(self): return self.changes


# --- ДИАЛОГ ПЕРЕВОДА (ОБНОВЛЕННЫЙ v6: Dark Theme UI) ---
class AITranslationDialog(QDialog):
    """
    Адаптированный диалог для сессии перевода недопереведенных фрагментов.
    Версия 6.0: 
    - Горизонтальная компоновка настроек (Label -> Spinbox).
    - Адаптация под темную тему (цвета текста, прозрачная кнопка).
    - Синхронизация с моделью.
    """
    def __init__(self, tasks_payloads, settings_manager, parent=None):
        super().__init__(parent)
        
        app = QApplication.instance()
        if not hasattr(app, 'event_bus') or not hasattr(app, 'engine'):
            raise RuntimeError("AITranslationDialog requires a global event_bus and engine.")
        self.bus = app.event_bus
        self.engine = app.engine
        self.task_manager = self.engine.task_manager
        
        self.tasks_payloads = tasks_payloads if isinstance(tasks_payloads, list) else [tasks_payloads]
        self.settings_manager = settings_manager
        
        self.translated_results = [] 
        self.is_session_active = False

        self.setWindowTitle(f"AI-ассистент перевода ({len(self.tasks_payloads)} пакетов)")
        
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
        
        self._init_ui()
        self.bus.event_posted.connect(self._on_global_event)
        
        # Первичная синхронизация настроек модели
        self._on_external_model_changed()

    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        
        # 1. Ключи
        self.key_widget = KeyManagementWidget(self.settings_manager, self)
        distribution_group = self.key_widget.findChild(QWidget, "distribution_group")
        if distribution_group: distribution_group.setVisible(False)
        main_layout.addWidget(self.key_widget)

        # 2. Модель и Лог
        middle_panel_layout = QHBoxLayout()
        self.model_settings_widget = ModelSettingsWidget(self)
        
        # Скрываем лишнее
        for name in ["rpm_row", "concurrent_row", "right_column_widget"]:
            widget_to_hide = self.model_settings_widget.findChild(QWidget, name)
            if widget_to_hide: widget_to_hide.setVisible(False)
            
        self.model_settings_widget.model_combo.currentIndexChanged.connect(self._on_external_model_changed)
        middle_panel_layout.addWidget(self.model_settings_widget, 1)

        log_group = QGroupBox("Лог выполнения")
        log_layout = QVBoxLayout(log_group)
        self.log_widget = LogWidget(self)
        log_layout.addWidget(self.log_widget)
        middle_panel_layout.addWidget(log_group, 1)
        
        main_layout.addLayout(middle_panel_layout)

        # 3. Промпт
        self.prompt_widget = PresetWidget(
            parent=self,
            preset_name="Промпт исправления",
            default_prompt_func=api_config.default_untranslated_prompt,
            load_presets_func=self.settings_manager.load_untranslated_prompts,
            save_presets_func=self.settings_manager.save_untranslated_prompts,
            get_last_text_func=self.settings_manager.get_last_untranslated_prompt_text,
            get_last_preset_func=self.settings_manager.get_last_untranslated_prompt_preset_name,
            save_last_preset_func=self.settings_manager.save_last_untranslated_prompt_preset_name
        )
        self.prompt_widget.load_last_session_state()
        main_layout.addWidget(self.prompt_widget)

        # 4. НИЖНЯЯ ПАНЕЛЬ
        bottom_frame = QFrame()
        bottom_frame.setFrameShape(QFrame.Shape.StyledPanel)
        bottom_layout = QHBoxLayout(bottom_frame)
        bottom_layout.setContentsMargins(10, 10, 10, 10)
        
        # -- Группа настроек производительности --
        perf_layout = QHBoxLayout()
        perf_layout.setSpacing(20) # Чуть больше отступа между группами
        
        # Helper: Горизонтальная компоновка [Текст] [Спинбокс]
        def create_param_widget(title, spinbox):
            w = QWidget()
            l = QHBoxLayout(w) # <-- QHBoxLayout для горизонтали
            l.setContentsMargins(0, 0, 0, 0)
            l.setSpacing(8)
            lbl = QLabel(title)
            # Цвет #ccc хорошо читается на темном фоне
            lbl.setStyleSheet("font-size: 9pt; color: #ccc;") 
            l.addWidget(lbl)
            l.addWidget(spinbox)
            return w

        # Threads
        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(1, 20); self.threads_spin.setValue(1)
        self.threads_spin.setToolTip("Количество потоков (воркеров).")
        self.threads_spin.setFixedWidth(60) # Фиксируем ширину для аккуратности
        
        # RPM
        self.rpm_spin = QSpinBox()
        self.rpm_spin.setRange(1, 1000); self.rpm_spin.setValue(10)
        self.rpm_spin.setToolTip("Лимит запросов в минуту (на один ключ).")
        self.rpm_spin.setFixedWidth(60)
        
        # Concurrent
        self.concurrent_spin = QSpinBox()
        self.concurrent_spin.setRange(1, 50); self.concurrent_spin.setValue(1)
        self.concurrent_spin.setToolTip("Параллельных запросов внутри одного потока.")
        self.concurrent_spin.setFixedWidth(60)
        
        perf_layout.addWidget(create_param_widget("Потоки:", self.threads_spin))
        perf_layout.addWidget(create_param_widget("RPM:", self.rpm_spin))
        perf_layout.addWidget(create_param_widget("Параллельно:", self.concurrent_spin))
        
        # Load info
        info_container = QWidget()
        info_layout = QVBoxLayout(info_container)
        info_layout.setContentsMargins(0,0,0,0)
        info_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self.load_info_label = QLabel("~ ? зад/поток")
        self.load_info_label.setStyleSheet("color: #888; font-size: 9pt; margin-left: 5px;")
        info_layout.addWidget(self.load_info_label)
        
        perf_layout.addWidget(info_container)
        
        bottom_layout.addLayout(perf_layout)
        bottom_layout.addStretch()
        
        # -- Кнопки --
        self.btn_apply = QPushButton("Применить полученные (0)")
        self.btn_apply.setEnabled(False)
        self.btn_apply.setMinimumWidth(180)
        # Стартовый стиль: прозрачный
        self.btn_apply.setStyleSheet("background-color: transparent; color: #777; border: 1px solid #444; border-radius: 4px; padding: 6px;")
        
        self.start_stop_btn = QPushButton(f"🚀 Начать перевод")
        self.start_stop_btn.setMinimumWidth(150)
        self.start_stop_btn.setStyleSheet("font-weight: bold; font-size: 11pt; padding: 6px;")
        
        self.cancel_close_btn = QPushButton("Закрыть")
        
        bottom_layout.addWidget(self.btn_apply)
        bottom_layout.addWidget(self.start_stop_btn)
        bottom_layout.addWidget(self.cancel_close_btn)
        
        main_layout.addWidget(bottom_frame)
        
        # Коннекторы
        self.start_stop_btn.clicked.connect(self._on_start_stop_clicked)
        self.cancel_close_btn.clicked.connect(self.reject) 
        self.btn_apply.clicked.connect(self.accept) 
        
        self.key_widget.active_keys_changed.connect(self._update_threads_limit)
        self.threads_spin.valueChanged.connect(self._update_load_info)
        
        self.key_widget.provider_combo.currentIndexChanged.emit(self.key_widget.provider_combo.currentIndex())
        self._update_threads_limit()

    def _update_apply_button(self):
        """Обновляет состояние кнопки применения."""
        count = len(self.translated_results)
        should_be_active = (count > 0) and (not self.is_session_active)

        self.btn_apply.setEnabled(should_be_active)
        self.btn_apply.setText(f"Применить полученные ({count})")

        if should_be_active:
            # Активная: Зеленая
            self.btn_apply.setStyleSheet("background-color: #2ECC71; color: white; font-weight: bold; padding: 6px; border-radius: 4px;")
        else:
            # Неактивная: Прозрачная с рамкой (под темную тему)
            self.btn_apply.setStyleSheet("background-color: transparent; color: #777; border: 1px solid #444; border-radius: 4px; padding: 6px;")

    def _on_external_model_changed(self):
        settings = self.model_settings_widget.get_settings()
        model_name = settings.get('model')
        if not model_name: return

        model_cfg = api_config.all_models().get(model_name, {})
        provider_id = model_cfg.get('provider')
        
        rec_rpm = 10
        rec_concurrent = 1
        
        if provider_id:
            prov_cfg = api_config.api_providers().get(provider_id, {})
            if prov_cfg.get('rpm'): rec_rpm = prov_cfg.get('rpm')
        
        if model_cfg.get('rpm'): rec_rpm = model_cfg.get('rpm')
        if model_cfg.get('max_concurrent_requests'): 
            rec_concurrent = model_cfg.get('max_concurrent_requests')
            
        if rec_concurrent == 0: rec_concurrent = 5 
        
        self.rpm_spin.setValue(rec_rpm)
        self.concurrent_spin.setValue(rec_concurrent)

    def _check_can_close(self):
        if self.is_session_active:
            self._on_start_stop_clicked()
            return False 

        if self.translated_results:
            reply = QMessageBox.question(
                self, 
                "Несохраненные результаты",
                f"Есть непримененные переводы ({len(self.translated_results)} шт.).\n"
                "Если вы закроете окно, они пропадут.\n\n"
                "Действительно выйти без сохранения?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            return reply == QMessageBox.StandardButton.Yes
            
        return True

    def reject(self):
        if self._check_can_close():
            super().reject()

    def closeEvent(self, event):
        if self.result() != QDialog.DialogCode.Accepted:
            if not self._check_can_close():
                event.ignore()
                return
        
        if self.bus:
            try:
                self.bus.event_posted.disconnect(self._on_global_event)
            except (TypeError, RuntimeError):
                pass
        super().closeEvent(event)

    def _update_threads_limit(self):
        keys = self.key_widget.get_active_keys()
        num_keys = len(keys)
        self.threads_spin.setMaximum(max(1, num_keys))
        if num_keys > 0:
            self.threads_spin.setValue(min(self.threads_spin.value(), num_keys))
        self._update_load_info()
        self._update_start_button_state()

    def _update_load_info(self):
        tasks = len(self.tasks_payloads)
        threads = self.threads_spin.value()
        if threads > 0:
            per_thread = math.ceil(tasks / threads)
            self.load_info_label.setText(f"~ {per_thread} зад/поток")
        else:
            self.load_info_label.setText("?")

    def _post_event(self, name: str, data: dict = None):
        if not self.bus: return
        session_id = self.engine.session_id if self.engine and self.engine.session_id else None
        event = { 'event': name, 'source': 'AITranslationDialog', 'session_id': session_id, 'data': data or {} }
        self.bus.event_posted.emit(event)

    def _update_start_button_state(self):
        can_start = not self.is_session_active and len(self.key_widget.get_active_keys()) > 0
        self.start_stop_btn.setEnabled(can_start)

    def _on_start_stop_clicked(self):
        if self.is_session_active:
            if self.engine and self.engine.session_id:
                self.start_stop_btn.setText("Остановка…")
                self.start_stop_btn.setEnabled(False)
                self._post_event('manual_stop_requested')
        else:
            self._set_ui_active(True)
            settings = self.get_settings()
            if not settings.get('api_keys'):
                QMessageBox.warning(self, "Нет ключей", "Не выбрано ни одного активного API ключа.")
                self._set_ui_active(False)
                return

            self.prompt_widget.save_last_session_state()
            self.settings_manager.save_last_untranslated_prompt_text(self.prompt_widget.get_prompt())
            
            self.translated_results = []
            self._update_apply_button()
            
            tasks_to_add = []
            prompt = self.prompt_widget.get_prompt()
            
            for i, payload in enumerate(self.tasks_payloads):
                task = ('raw_text_translation', payload, prompt, f"Пакет {i+1}/{len(self.tasks_payloads)}")
                tasks_to_add.append(task)
            
            self.task_manager.clear_all_queues()
            self.task_manager.add_pending_tasks(tasks_to_add)
            
            self._post_event('start_session_requested', {'settings': settings})

    @pyqtSlot(dict)
    def _on_global_event(self, event: dict):
        event_name = event.get('event')
        data = event.get('data', {})

        if event_name == 'session_started':
            self._set_ui_active(True)
            return

        if event_name == 'session_finished':
            self.task_manager.clear_all_queues()
            self._set_ui_active(False)
            
            if not self.translated_results:
                QMessageBox.warning(self, "Сессия завершена", f"Сессия завершилась без результатов. {data.get('reason', '')}")
            else:
                QMessageBox.information(self, "Готово", f"Сессия завершена. Получено ответов: {len(self.translated_results)}. Не забудьте нажать 'Применить'!")
            return

        if event_name == 'task_finished':
            if data.get('success'):
                task_info = data.get('task_info')
                if task_info and task_info[1] and task_info[1][0] == 'raw_text_translation':
                    res_html = data.get('result_data')
                    if res_html:
                        self.translated_results.append(res_html)
                        self._update_apply_button()

    def _set_ui_active(self, active: bool):
        self.is_session_active = active
        self.key_widget.setEnabled(not active)
        self.model_settings_widget.setEnabled(not active)
        self.prompt_widget.setEnabled(not active)
        self.threads_spin.setEnabled(not active)
        self.rpm_spin.setEnabled(not active)
        self.concurrent_spin.setEnabled(not active)

        if active:
            self.start_stop_btn.setText("❌ Стоп")
            self.start_stop_btn.setStyleSheet("background-color: #C0392B; color: #ffffff; font-weight: bold; padding: 6px;")
            self.start_stop_btn.setEnabled(True)
            self.cancel_close_btn.setText("Прервать")
        else:
            self.start_stop_btn.setText(f"🚀 Начать перевод")
            self.start_stop_btn.setStyleSheet("font-weight: bold; font-size: 11pt; padding: 6px;")
            self.cancel_close_btn.setText("Закрыть")
            self._update_start_button_state()
            
        self._update_apply_button()

    def get_settings(self):
        settings = self.model_settings_widget.get_settings()
        settings['provider'] = self.key_widget.get_selected_provider()
        settings['api_keys'] = self.key_widget.get_active_keys()
        settings['rpm_limit'] = self.rpm_spin.value()
        settings['max_concurrent_requests'] = self.concurrent_spin.value()
        settings['num_instances'] = self.threads_spin.value()
        
        model_name = settings.get('model')
        settings['model_config'] = api_config.all_models().get(model_name, {}).copy()
        settings['force_accept'] = True
        settings['custom_prompt'] = api_config.default_prompt()
        return settings

    def get_translated_results(self):
        return self.translated_results
####