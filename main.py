# -*- coding: utf-8 -*-
import sys
import os
import os_patch
import builtins
import argparse
import traceback
import asyncio
import sqlite3
import atexit
import base64
from PyQt6 import QtGui
from PyQt6 import QtWidgets, QtCore
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication
from gemini_translator.api.managers import ApiKeyManager
from gemini_translator.utils.glossary_tools import ContextManager
from gemini_translator.ui.dialogs.setup import InitialSetupDialog
from gemini_translator.ui.dialogs.misc import StartupToolDialog
from gemini_translator.ui.dialogs.glossary import MainWindow as GlossaryToolWindow
from gemini_translator.ui.dialogs.validation import TranslationValidatorDialog
from gemini_translator.utils.settings import SettingsManager
from gemini_translator.utils.project_manager import TranslationProjectManager
from gemini_translator.core.translation_engine import TranslationEngine
from gemini_translator.api import config as api_config
from gemini_translator.core.task_manager import ChapterQueueManager
from gemini_translator.utils.proxy_tool import GlobalProxyController
from gemini_translator.utils.server_manager import ServerManager
from gemini_translator.ui.dialogs.proxy import ProxySettingsDialog
from gemini_translator.ui.themes import DARK_STYLESHEET  # <-- ИМПОРТИРУЙТЕ ТЕМУ


RESTART_INFO = {
    "is_restarting": False,
    "epub_path": None,
    "chapters": None,
}

# --------------------------------------------------------------------------
# Gemini EPUB Translator - Точка входа в приложение
APP_VERSION = "V 11"  # <-- ОПРЕДЕЛЕНИЕ ВЕРСИИ ЗДЕСЬ
# ---------------------------------------------------------------------------
# Этот файл отвечает за запуск приложения, обработку аргументов командной
# строки и выбор режима работы (автоматический, параллельный, гибридный).
# Вся основная логика, классы окон и утилиты импортируются из пакета
# 'gemini_translator'.
# ---------------------------------------------------------------------------

# --- БЛОК: АВАРИЙНЫЙ ПРОСМОТРЩИК ОШИБОК ---

def run_emergency_viewer():
    """
    Запускает минималистичный, самодостаточный диалог для отображения
    критической ошибки, когда основное приложение не отвечает.
    """
    # Минимальная темная тема, чтобы окно не выглядело чужеродно
    FALLBACK_DARK_QSS = """
        QDialog, QWidget { background-color: #2c313c; color: #f0f0f0; }
        QTextEdit { background-color: #1e222a; border: 1px solid #4d5666; }
        QPushButton { background-color: #4d5666; border: none; padding: 8px; border-radius: 4px; }
        QPushButton:hover { background-color: #5a6475; }
    """

    app = QtWidgets.QApplication(sys.argv)
    app.setStyleSheet(FALLBACK_DARK_QSS)

    error_text = "Тестовое сообщение об ошибке.\n\nАргументы командной строки не были предоставлены."
    # Ошибка передается как второй аргумент (первый -- --emergency-viewer)
    if len(sys.argv) > 2:
        try:
            encoded_message = sys.argv[2]
            decoded_bytes = base64.b64decode(encoded_message)
            error_text = decoded_bytes.decode('utf-8')
        except Exception as e:
            error_text = f"Не удалось декодировать сообщение об ошибке: {e}\n\nИсходные данные:\n{sys.argv[2]}"

    # Создаем диалог напрямую, без доп. классов
    dialog = QtWidgets.QDialog()
    dialog.setWindowTitle("Аварийный Отчет об Ошибке")
    dialog.setMinimumSize(700, 500)

    layout = QtWidgets.QVBoxLayout(dialog)

    info_label = QtWidgets.QLabel(
        "Произошла критическая ошибка, которая привела к зависанию основного приложения.\n"
        "Это аварийное окно было запущено для отображения информации о сбое."
    )
    info_label.setWordWrap(True)
    info_label.setStyleSheet(
        "padding: 5px; background-color: #c0392b; color: white; border-radius: 4px;")
    layout.addWidget(info_label)

    details_view = QtWidgets.QTextEdit()
    details_view.setReadOnly(True)
    details_view.setFont(QtGui.QFont("Consolas", 10))
    details_view.setText(error_text)
    layout.addWidget(details_view)

    button_layout = QtWidgets.QHBoxLayout()
    copy_button = QtWidgets.QPushButton("Скопировать ошибку")

    def copy_action():
        QtWidgets.QApplication.clipboard().setText(error_text)
        copy_button.setText("Скопировано!")
        copy_button.setEnabled(False)
        QtCore.QTimer.singleShot(2000, lambda: (
            copy_button.setText("Скопировать ошибку"),
            copy_button.setEnabled(True)
        ))

    copy_button.clicked.connect(copy_action)

    close_button = QtWidgets.QPushButton("Закрыть")
    close_button.clicked.connect(dialog.accept)

    button_layout.addWidget(copy_button)
    button_layout.addStretch()
    button_layout.addWidget(close_button)
    layout.addLayout(button_layout)

    dialog.exec()
    sys.exit(0)  # Завершаем аварийный процесс


class LoadingDialog(QtWidgets.QDialog):
    """Простой диалог-заставка, который показывается во время инициализации."""

    def __init__(self, parent=None):
        super().__init__(parent)
        # Убираем рамку окна, делаем его похожим на заставку
        self.setWindowFlags(Qt.WindowType.SplashScreen |
                            Qt.WindowType.FramelessWindowHint)
        self.setModal(True)  # Блокируем другие окна, пока это видимо

        layout = QtWidgets.QVBoxLayout(self)
        self.label = QtWidgets.QLabel("Инициализация приложения…")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setStyleSheet("font-size: 12pt; padding: 20px;")
        layout.addWidget(self.label)
        self.setFixedSize(300, 100)


class ValidatorStartupDialog(QtWidgets.QDialog):
    """Новый диалог для выбора способа запуска Валидатора."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Запуск инструмента проверки")
        self.setMinimumWidth(400)
        self.output_folder = None
        self.original_epub_path = None
        self.settings_manager = SettingsManager()

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel(
            "Выберите, как запустить инструмент проверки:"))

        history_btn = QtWidgets.QPushButton("📂 Загрузить проект из истории")
        history_btn.clicked.connect(self.load_from_history)
        layout.addWidget(history_btn)

        manual_btn = QtWidgets.QPushButton("✍️ Выбрать папку и файл вручную")
        manual_btn.clicked.connect(self.select_manually)
        layout.addWidget(manual_btn)

        cancel_btn = QtWidgets.QPushButton("Отмена")
        cancel_btn.clicked.connect(self.reject)
        layout.addWidget(cancel_btn)

    def load_from_history(self):
        history = self.settings_manager.load_project_history()
        if not history:
            QtWidgets.QMessageBox.information(
                self, "История пуста", "Вы еще не запускали ни одного перевода.")
            return
        from gemini_translator.ui.dialogs.setup import ProjectHistoryDialog
        dialog = ProjectHistoryDialog(history, self)
        if dialog.exec():
            project = dialog.get_selected_project()
            if project:
                self.output_folder = project.get("output_folder")
                self.original_epub_path = project.get("epub_path")
                if os.path.isdir(self.output_folder) and os.path.exists(self.original_epub_path):
                    self.accept()
                else:
                    QtWidgets.QMessageBox.warning(
                        self, "Ошибка", "Пути в выбранном проекте недействительны.")

    def select_manually(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Выберите папку с переводами")
        if not folder:
            return

        epub, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Выберите исходный EPUB", "", "*.epub")
        if not epub:
            return

        self.output_folder = folder
        self.original_epub_path = epub
        self.accept()


def restart_with_new_files(epub_path, chapters):
    """Готовит приложение к перезапуску с новым набором файлов."""
    print("Подготовка к перезапуску с новыми файлами…")
    RESTART_INFO["is_restarting"] = True
    RESTART_INFO["epub_path"] = epub_path
    RESTART_INFO["chapters"] = chapters

    app = QtWidgets.QApplication.instance()
    if app:
        # Просто выходим из текущего цикла событий, чтобы вернуться в main()
        app.quit()


def global_excepthook(exc_type, exc_value, exc_tb):
    """
    Обрабатывает все неперехваченные исключения.
    Если приложение отвечает, показывает встроенное окно.
    Если приложение зависло, пытается грациозно завершить фоновые потоки
    и только потом запускает аварийный режим.
    """
    tb_list = traceback.format_exception(exc_type, exc_value, exc_tb)
    tb_str = "".join(tb_list)
    error_message = (
        f"Произошла неперехваченная ошибка: {exc_type.__name__}\n\n"
        f"--- Полный Traceback ---\n{tb_str}"
    )
    print(f"КРИТИЧЕСКАЯ ОШИБКА (Unhandled Exception):\n{error_message}")

    app = QtWidgets.QApplication.instance()

    # Сценарий 1: Приложение "живо" и может показать окно само.
    if app:
        try:
            # Даем приложению 100мс на обработку события перед показом окна
            # Это может помочь, если ошибка произошла в момент отрисовки
            QtCore.QTimer.singleShot(100, lambda: (
                QtWidgets.QMessageBox.critical(
                    None, "Критическая Ошибка Приложения", error_message
                ),
                # Запрашиваем штатное завершение, которое вызовет все aboutToQuit сигналы
                QtCore.QTimer.singleShot(0, app.quit)
            ))
            return
        except Exception as e:
            print(
                f"[CRITICAL] Не удалось показать QMessageBox, даже при живом app: {e}")
            # Если даже QMessageBox падает, переходим к плану "Б".

    # --- НОВЫЙ БЛОК: Попытка грациозного завершения ---
    # Это выполняется, только если приложение не отвечает.
    print("[CRITICAL] Приложение Qt не отвечает. Попытка принудительной, но грациозной остановки...")
    if app and hasattr(app, 'engine') and hasattr(app, 'engine_thread'):
        try:
            # Отправляем команду на очистку в поток движка и ждем его завершения
            # с таймаутом, чтобы не зависнуть здесь навсегда.
            print("[CRITICAL] Отправка команды cleanup в движок...")
            app.engine.cancel_translation("Аварийное завершение по ошибке")

            # Ждем завершения потока движка (он должен сам себя остановить)
            if app.engine_thread.wait(5000):  # Ждем до 5 секунд
                print("[CRITICAL] Фоновые потоки успешно завершены.")
            else:
                print(
                    "[CRITICAL] Таймаут ожидания фоновых потоков. Возможны 'зомби'.")
        except Exception as e:
            print(
                f"[CRITICAL] Ошибка во время попытки грациозного завершения: {e}")
    # --- КОНЕЦ НОВОГО БЛОКА ---

    # Сценарий 2: Запускаем "Спасательную шлюпку".
    print("[CRITICAL] Запуск аварийного просмотрщика ошибок...")
    try:
        import subprocess

        encoded_message = base64.b64encode(
            error_message.encode('utf-8')).decode('ascii')

        # Запускаем самих себя со специальным флагом.
        command = [sys.executable, sys.argv[0],
                   '--emergency-viewer', encoded_message]

        # --- ГЛАВНОЕ ИЗМЕНЕНИЕ ---
        # Запускаем дочерний процесс в "чистом" системном окружении,
        # чтобы он не наследовал пути к временной папке умирающего родителя.
        subprocess.Popen(command, env=os.environ.copy())
        # --- КОНЕЦ ИЗМЕНЕНИЯ ---

    except Exception as e:
        print(
            f"[ULTRA-CRITICAL] Не удалось запустить аварийный просмотрщик: {e}")

    # Принудительно завершаем зависший процесс, как и раньше.
    os._exit(1)


class ApplicationWithContext(QtWidgets.QApplication):
    """
    Расширенный класс QApplication для управления активным контекстом настроек.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._true_global_settings_manager = None
        self._active_settings_manager = None

    def initialize_managers(self):
        """Инициализирует менеджеры после создания основного объекта."""
        # Этот менеджер - константа. Он всегда работает с файлом ~/.epub_translator/settings.json
        self._true_global_settings_manager = SettingsManager(
            event_bus=self.event_bus)
        # По умолчанию активный менеджер - это глобальный
        self._active_settings_manager = self._true_global_settings_manager

    def get_settings_manager(self) -> SettingsManager:
        """
        ЕДИНСТВЕННЫЙ правильный способ получить текущий менеджер настроек.
        Все виджеты должны использовать этот метод.
        """
        return self._active_settings_manager

    def get_server_manager(self):
        """Возвращает менеджер сервера Perplexity."""
        return self.server_manager


class EventBus(QtCore.QObject):
    import threading
    event_posted = QtCore.pyqtSignal(dict)
    # Новые атрибуты и методы для "шины с инерцией"
    # Сигнал, который передает ключ измененных данных
    data_changed = QtCore.pyqtSignal(str)
    _data_store = {}
    _lock = threading.Lock()

    def set_data(self, key: str, value):
        """Потокобезопасно сохраняет данные и испускает сигнал."""
        with self._lock:
            self._data_store[key] = value
        self.data_changed.emit(key)

    def pop_data(self, key: str, default=None):
        """Потокобезопасно извлекает (и удаляет) данные."""
        with self._lock:
            return self._data_store.pop(key, default)

    def get_data(self, key: str, default=None):
        """Потокобезопасно читает данные, не удаляя их."""
        with self._lock:
            return self._data_store.get(key, default)


def initialize_global_resources(app: QApplication):
    """
    Создает ПУСТЫЕ глобальные ресурсы (БД, диск) и "вешает" их на QApplication.
    Не создает никаких таблиц!
    """
    print("--- Инициализация глобальных ресурсов... ---")
    try:
        # 1. Создаем и удерживаем "якорное" подключение к пустой БД
        # WAL (Write-Ahead Logging) позволяет нескольким потокам читать,
        # пока один поток пишет, что предотвращает deadlock'и.
        main_db_conn = sqlite3.connect(
            api_config.SHARED_DB_URI, uri=True, check_same_thread=False)
        main_db_conn.row_factory = sqlite3.Row

        # --- Включаем WAL на главном соединении ---
        main_db_conn.execute("PRAGMA journal_mode=WAL;")
        # Ждать до 5 секунд
        main_db_conn.execute("PRAGMA busy_timeout = 5000;")

        atexit.register(lambda: main_db_conn.close())
        app.main_db_connection = main_db_conn
        print("--- Общая in-memory база данных активна и удерживается. ---")

    except Exception as e:
        raise RuntimeError(
            f"КРИТИЧЕСКАЯ ОШИБКА при инициализации глобальных ресурсов: {e}")


# ============================================================================
# ОСНОВНАЯ ТОЧКА ВХОДА
# ============================================================================
if len(sys.argv) > 1 and sys.argv[1] == '--emergency-viewer':
    run_emergency_viewer()

# Специальный код возврата для перезагрузки приложения (возврат в меню)
EXIT_CODE_REBOOT = 2000

if __name__ == "__main__":
    import threading
    # --- РЕГИСТРАЦИЯ ГЛАВНОГО ПОТОКА ---
    main_id = threading.get_ident()
    print(f"\n[SYSTEM] 🟢 MAIN UI THREAD ID: {main_id}\n")
    # Регистрируем его как VIP
    os_patch.PatientLock.register_vip_thread(main_id)

    sys.excepthook = global_excepthook
    app = ApplicationWithContext(sys.argv)
    app.setStyleSheet(DARK_STYLESHEET)

    # Инициализация ресурсов (один раз при старте процесса)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    initialize_global_resources(app)

    # [ARCH] Активация виртуальной файловой системы.
    # С этого момента функции open(), os.path.* и zipfile умеют работать с путями 'mem://'.
    os_patch.apply()
    api_config.initialize_configs()

    print("[INFO] Инициализация основных сервисов приложения…")

    app.event_bus = EventBus()
    app.initialize_managers()
    app.settings_manager = app.get_settings_manager()
    app.task_manager = ChapterQueueManager(event_bus=app.event_bus)
    app.global_version = APP_VERSION
    app.proxy_controller = GlobalProxyController(app.event_bus)
    proxy_settings = app.settings_manager.load_proxy_settings()

    temp_folder = os.path.join(
        os.path.expanduser("~"), ".epub_translator_temp")
    os.makedirs(temp_folder, exist_ok=True)
    app.context_manager = ContextManager(temp_folder)
    app.server_manager = ServerManager(app.event_bus)
    print("[INFO] Инициализация TranslationEngine…")

    app.engine = TranslationEngine(task_manager=app.task_manager)
    app.engine_thread = QtCore.QThread(app)
    app.engine.moveToThread(app.engine_thread)

    # Убираем автоматическую остановку потока по aboutToQuit,
    # чтобы движок переживал перезагрузку интерфейса (код 2000).
    # Ручная остановка выполняется в самом конце файла.

    app.engine_thread.finished.connect(app.engine.deleteLater)

    app.engine_thread.start()

    print("[OK] TranslationEngine запущен в фоновом потоке.")
    QtCore.QMetaObject.invokeMethod(
        app.engine,
        "log_thread_identity",
        QtCore.Qt.ConnectionType.QueuedConnection
    )

    try:
        import jieba
        print("[INFO] Warming up jieba dictionary…")
        jieba.lcut("прогрев", cut_all=False)
    except (ImportError, Exception) as e:
        print(f"[WARN] Could not warm up jieba dictionary: {e}")

    # --- ГЛАВНЫЙ ЦИКЛ ПРИЛОЖЕНИЯ ---
    while True:
        main_window_to_run = None
        loading_dialog = LoadingDialog()

        try:
            # Диалог выбора инструмента
            tool_dialog = StartupToolDialog(app_version=APP_VERSION)
            if tool_dialog.exec():
                selected_tool = tool_dialog.selected_tool
                if selected_tool == 'translator':
                    main_window_to_run = InitialSetupDialog()
                elif selected_tool == 'validator':
                    startup_dialog = ValidatorStartupDialog()
                    if startup_dialog.exec():
                        output_folder = startup_dialog.output_folder
                        original_epub = startup_dialog.original_epub_path
                        project_manager = TranslationProjectManager(
                            output_folder)
                        # retry_enabled=False означает автономный режим
                        main_window_to_run = TranslationValidatorDialog(
                            output_folder,
                            original_epub,
                            retry_enabled=False,
                            project_manager=project_manager
                        )
                elif selected_tool == 'glossary':
                    # Импортируем диалог запуска (он теперь внутри модуля)
                    from gemini_translator.ui.dialogs.glossary import GlossaryStartupDialog

                    startup_dialog = GlossaryStartupDialog()
                    if startup_dialog.exec():
                        # project_path может быть путем или None (если выбран пустой режим)
                        project_path = startup_dialog.project_path
                        main_window_to_run = GlossaryToolWindow(
                            mode='standalone',
                            project_path=project_path
                        )
            else:
                # Пользователь закрыл меню — выход
                break

        except Exception as e:
            if loading_dialog.isVisible():
                loading_dialog.close()

            tb_str = "".join(traceback.format_exception(
                type(e), e, e.__traceback__))
            error_message = (
                f"Произошла критическая ошибка при инициализации окна: {type(e).__name__}\n\n"
                f"--- Полный Traceback ---\n{tb_str}"
            )
            print(f"[CRITICAL STARTUP ERROR]\n{error_message}")
            QtWidgets.QMessageBox.critical(
                None, "Ошибка запуска", error_message)
            main_window_to_run = None

        # Запуск выбранного окна
        if main_window_to_run:
            main_window_to_run.show()
            exit_code = app.exec()

            # Если код возврата равен коду перезагрузки, цикл while повторится
            if exit_code != EXIT_CODE_REBOOT:
                break
        else:
            break

    # --- ЗАВЕРШЕНИЕ ---
    print(f"[INFO] Приложение завершает работу.")
    if hasattr(app, 'engine_thread') and app.engine_thread.isRunning():
        app.engine_thread.quit()
        app.engine_thread.wait()
    sys.exit(0)
