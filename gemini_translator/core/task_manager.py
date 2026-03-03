# ======================================== Файл: .\gemini_translator\core\task_manager.py (ФИНАЛЬНАЯ ВЕРСИЯ) ========================================

import threading

try:
    import os_patch
    PatientLock = os_patch.PatientLock
except (ImportError, AttributeError):
    print("[TaskManager WARN] PatientLock не найден. Используется стандартный RLock.")
    from threading import RLock as PatientLock

import os
import re
import uuid
import io
import zipfile
import json
import time
import sqlite3
import hashlib
from collections import Counter
import contextlib

from PyQt6.QtCore import pyqtSlot, pyqtSignal, QObject, QThread, QTimer
from PyQt6 import QtWidgets
from ..api.config import SHARED_DB_URI

def tuple_serializer(obj):
    if isinstance(obj, tuple): return {'__tuple__': True, 'items': list(obj)}
    if isinstance(obj, uuid.UUID): return str(obj)
    raise TypeError(f'Object of type {obj.__class__.__name__} is not JSON serializable')

def tuple_deserializer(dct):
    if '__tuple__' in dct: return tuple(dct['items'])
    return dct

class ChapterQueueManager(QObject):
    """
    Менеджер очереди задач на базе In-Memory SQLite.
    
    ARCHITECTURE NOTE: IN-MEMORY STATE MANAGEMENT
    ---------------------------------------------
    SQLite здесь используется НЕ для персистентного хранения на диске, а как
    высокопроизводительная структура данных в оперативной памяти (MVCC).
    
    * Write: Короткие транзакции в shared-memory (`file:...?mode=memory&cache=shared`).
    * Read: Мгновенные снапшоты (backup) в локальную память потока для UI,
      что обеспечивает неблокирующее чтение без конкуренции с воркерами.
    """
    
    _ui_update_requested = pyqtSignal()
    def __init__(self, event_bus=None):
        super().__init__()
        app = QtWidgets.QApplication.instance()
        
        if not hasattr(app, 'main_db_connection'):
            raise RuntimeError("Главное подключение к БД не найдено в QApplication!")
        
        main_conn = app.main_db_connection
        self._create_schema(main_conn)
        
        self.bus = event_bus
        if self.bus is None:
            if not hasattr(app, 'event_bus'): raise RuntimeError("EventBus не найден.")
            self.bus = app.event_bus
        
        self.session_id = None
        self.bus.event_posted.connect(self.on_event)

        self.master_uri = SHARED_DB_URI

        self._chancellor_lock = PatientLock()
        self._ui_state_list_cache = []
        self._is_updating_cache = False
        self._cache_update_worker = None
        
        # Воркер для фоновой очистки при завершении сессии
        self._cleanup_worker = None
        # Особый воркер для глоссария, чтобы не блокировать основной поток
        self._glossary_cleanup_worker = None 
       
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(100) # Задержка для сбора нескольких быстрых запросов в один
        self._update_timer.timeout.connect(self._trigger_cache_update)
        self._ui_update_requested.connect(self._notify_ui_of_change)
        
    def _get_conn(self):
        conn = sqlite3.connect(
            SHARED_DB_URI, 
            uri=True, 
            check_same_thread=False,
            timeout=10.0
        )
        conn.row_factory = sqlite3.Row
        return conn
    
    @contextlib.contextmanager
    def _get_write_conn(self):
        """
        Контекстный менеджер, выдающий эксклюзивное право на запись.
        Версия 2.0: Использует стандартные транзакции вместо клонирования.
        """
        self._chancellor_lock.acquire()
        conn = None
        try:
            conn = self._get_conn() 
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            if conn: conn.rollback()
            raise
        finally:
            if conn: conn.close()
            self._chancellor_lock.release()

    # В файле core/task_manager.py

    def _get_read_only_conn(self):
        """
        Создает легкое подключение для чтения.
        ОПТИМИЗАЦИЯ: Убрано клонирование (backup).
        Благодаря WAL-режиму (включенному в main.py), читатели не блокируют писателей.
        """
        conn = sqlite3.connect(
            self.master_uri, 
            uri=True, 
            check_same_thread=False,
            timeout=10.0
        )
        conn.row_factory = sqlite3.Row
        # Защита от случайной записи
        conn.execute("PRAGMA query_only = 1;")
        return conn

    def _create_schema(self, conn: sqlite3.Connection):
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    worker_id TEXT,
                    sequence INTEGER,
                    priority INTEGER DEFAULT 0 NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status_seq ON tasks (status, priority DESC, sequence ASC);")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_errors (
                    error_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    error_type TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks (task_id) ON DELETE CASCADE
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_task_errors_task_id ON task_errors (task_id);")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chunk_results (
                    task_id TEXT PRIMARY KEY,
                    translated_content TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks (task_id) ON DELETE CASCADE
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS glossary_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    chapters_json TEXT,
                    original TEXT NOT NULL,
                    rus TEXT,
                    note TEXT
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_glossary_original ON glossary_results (original);")
    
    def clear_glossary_results(self):
        with self._get_write_conn() as conn:
            conn.execute("DELETE FROM glossary_results")
    
    def _log(self, message):
        self._post_event('log_message', {'message': message})
    
    @pyqtSlot(dict)
    def on_event(self, event_data: dict):
        """
        Обработчик событий.
        Для 'session_finished' запускает фоновый поток очистки,
        чтобы не блокировать UI ожиданием замка.
        """
        if event_data.get('event') == 'session_finished':
            # Запускаем "спасательную операцию" в отдельном потоке.
            if self._cleanup_worker and self._cleanup_worker.isRunning():
                pass
            else:
                self._cleanup_worker = TaskDBWorker(self._handle_session_finished_background)
                self._cleanup_worker.start()
    
    def _post_event(self, name: str, data: dict = None):
        event = {
            'event': name, 'source': 'ChapterQueueManager', 'session_id': self.session_id, 'data': data or {}
        }
        self.bus.event_posted.emit(event)
    
    def _handle_session_finished_background(self):
        """
        Фоновая версия обработчика завершения сессии.
        Выполняется в отдельном потоке TaskDBWorker.
        """
        rescued_tasks = []
        
        # --- ЭТАП 1: БЫСТРАЯ ТРАНЗАКЦИЯ ---
        # Заходим, быстро читаем и обновляем, и мгновенно выходим, освобождая замок.
        with self._get_write_conn() as conn:
            cursor = conn.execute("SELECT task_id, payload FROM tasks WHERE status = 'in_progress'")
            rescued_tasks = cursor.fetchall()
            
            if rescued_tasks:
                rescued_ids = [row['task_id'] for row in rescued_tasks]
                placeholders = ','.join('?' for _ in rescued_ids)
                conn.execute(f"UPDATE tasks SET status = 'pending', priority = 1, worker_id = NULL WHERE task_id IN ({placeholders})", rescued_ids)
        
        # --- ЭТАП 2: ЛОГИРОВАНИЕ И UI (БЕЗ БЛОКИРОВОК) ---
        if not rescued_tasks: 
            return
        self._log(f"[TASK MANAGER RESCUE] Сессия завершена. Спасено {len(rescued_tasks)} зависших задач.")
        self._safe_request_ui_update()

    def rescue_task_by_worker_id(self, worker_id: str):
        rescued_count = 0
        with self._get_write_conn() as conn:
            cursor = conn.execute("SELECT task_id, payload FROM tasks WHERE worker_id = ? AND status = 'in_progress'", (worker_id,))
            tasks_to_rescue = cursor.fetchall()
            if tasks_to_rescue:
                # ВЕРСИЯ 2.0: Отправляем в начало очереди (seq < min), но с обычным приоритетом (0)
                update_cursor = conn.execute(
                    """
                    UPDATE tasks 
                    SET status = 'pending', 
                        priority = 0, 
                        worker_id = NULL,
                        sequence = (SELECT COALESCE(MIN(sequence), 0) - 1 FROM tasks)
                    WHERE worker_id = ? AND status = 'in_progress'
                    """, 
                    (worker_id,)
                )
                rescued_count = update_cursor.rowcount
        
        if rescued_count > 0:
            self._log(f"[TASK MANAGER RESCUE] 💥 Обнаружено {rescued_count} зависших задач от воркера …{worker_id[-4:]}. Начинаю спасение:")
            for task_row in tasks_to_rescue:
                try:
                    payload = json.loads(task_row['payload'], object_hook=tuple_deserializer)
                    task_name = self._get_task_display_name(payload)
                    self._log(f"    - Задача '{task_name}' спасена и возвращена в НАЧАЛО очереди.")
                except (json.JSONDecodeError, IndexError):
                    self._log(f"    - Задача с ID {task_row['task_id']} спасена.")
            self._safe_request_ui_update()
            return True
        
        return False
    
    def _normalize_payload(self, payload: tuple) -> tuple:
        if len(payload) <= 1: return payload
        file_data = payload[1]
        virtual_path = None
        try:
            if isinstance(file_data, str): virtual_path = os.copy_to_mem(file_data)
            elif isinstance(file_data, io.BytesIO):
                file_data.seek(0)
                virtual_path = os.write_bytes_to_mem(file_data.getvalue(), ".tmp")
            if virtual_path: return (payload[0], virtual_path) + payload[2:]
        except AttributeError:
            self._log("[TaskManager WARN] Патч 'os' не применен. Файлы не будут виртуализированы.")
        return payload

    def add_priority_tasks(self, tasks: list, parent_history: dict = None):
        """Добавляет задачи в НАЧАЛО очереди (высокий priority)."""
        # --- ЭТАП 1: Подготовка данных (вне транзакции) ---
        tasks_to_insert = []
        all_errors_to_insert = [] # <-- Единый список для ВСЕХ ошибок

        for task in tasks:
            task_id_str = str(uuid.uuid4())
            payload = self._normalize_payload(task)
            tasks_to_insert.append((
                task_id_str, 
                json.dumps(payload, default=tuple_serializer), 
                'pending', 
                1, # priority
                time.time() # sequence
            ))
            
            if parent_history:
                # Добавляем ошибки для ЭТОЙ задачи в ОБЩИЙ список
                for error_type, count in parent_history.get('errors', {}).items():
                    for _ in range(count):
                        all_errors_to_insert.append((task_id_str, error_type, time.time()))
        
        # --- ЭТАП 2: Атомарная запись в БД (внутри транзакции) ---
        if tasks_to_insert: # Проверяем, есть ли вообще что добавлять
            with self._get_write_conn() as conn:
                # Сначала вставляем родительские задачи
                conn.executemany(
                    "INSERT OR IGNORE INTO tasks (task_id, payload, status, priority, sequence) VALUES (?, ?, ?, ?, ?)",
                    tasks_to_insert
                )
                # Затем вставляем всю историю ошибок
                if all_errors_to_insert:
                    conn.executemany(
                        "INSERT INTO task_errors (task_id, error_type, timestamp) VALUES (?, ?, ?)",
                        all_errors_to_insert
                    )
        
        # --- ЭТАП 3: Уведомление UI (вне транзакции) ---
        self._safe_request_ui_update()
        
    def has_held_tasks(self) -> bool:
        """Проверяет, есть ли в очереди 'замороженные' задачи."""
        with self._get_read_only_conn() as conn:
            cursor = conn.execute("SELECT 1 FROM tasks WHERE status = 'held' LIMIT 1")
            return cursor.fetchone() is not None
    
    def peek_next_held_task(self) -> tuple | None:
        """
        Возвращает (id, payload) следующей 'замороженной' задачи, НЕ меняя ее статус.
        """
        with self._get_read_only_conn() as conn:
            cursor = conn.execute(
                "SELECT task_id, payload FROM tasks WHERE status = 'held' ORDER BY sequence ASC LIMIT 1"
            )
            row = cursor.fetchone()
            if row:
                task_id = uuid.UUID(row['task_id'])
                payload = json.loads(row['payload'], object_hook=tuple_deserializer)
                return (task_id, payload)
        return None

    def promote_held_task(self, task_id: uuid.UUID, new_payload: tuple):
        task_infos = self.update_task(task_id, new_status='pending', new_payload=new_payload, new_priority=1)
        self._safe_request_ui_update()
        return task_infos
        
    def add_pending_tasks(self, tasks: list):
        with self._get_write_conn() as conn:
            cursor = conn.execute("SELECT MAX(sequence) FROM tasks WHERE status = 'pending'")
            max_seq_row = cursor.fetchone()
            max_seq = max_seq_row[0] if max_seq_row else None
            start_seq = 0 if max_seq is None else max_seq + 1
            tasks_to_insert = []
            for i, task in enumerate(tasks):
                task_id = uuid.uuid4()
                payload = self._normalize_payload(task)
                tasks_to_insert.append((str(task_id), json.dumps(payload, default=tuple_serializer), 'pending', start_seq + i))
            conn.executemany("INSERT OR IGNORE INTO tasks (task_id, payload, status, sequence) VALUES (?, ?, ?, ?)", tasks_to_insert)
        self._safe_request_ui_update()
    
    def get_next_task(self, worker_id: str) -> tuple | None:
        task_for_work = self.update_task(task_id=None, worker_id=worker_id, new_status='in_progress')
        if task_for_work:
            self._log(f"[TASK] →→ Задача '{self._get_task_display_name(task_for_work[1])}' отдана ключу …{worker_id[-4:]} в работу.")
        self._safe_request_ui_update()    
        return task_for_work
    
    def update_task(self, task_id: uuid.UUID = None, worker_id: str = None, new_status: str = None, new_payload: tuple = None, new_priority: int = None, new_sequence: int = None, unsafe_mode=False, **conditions) -> tuple | None:
        """
        Центральный метод обновления. Версия 14.0: Сочетает вашу структуру
        с правильной обработкой unsafe_mode через contextlib.
        """
        # --- ЭТАП 1: Подготовка данных (вне транзакции) ---
        updates, params = [], []
        if worker_id is not None: updates.append("worker_id = ?"); params.append(worker_id)
        if new_status is not None: updates.append("status = ?"); params.append(new_status)
        if new_payload is not None: updates.append("payload = ?"); params.append(json.dumps(new_payload, default=tuple_serializer))
        if new_priority is not None: updates.append("priority = ?"); params.append(new_priority)
        if new_sequence is not None: updates.append("sequence = ?"); params.append(new_sequence)

        # --- ЭТАП 2: Выбор контекста ---
        # Если в unsafe_mode передано соединение, nullcontext просто "пропустит" его в with-блок.
        # Если unsafe_mode=False, будет использован наш _get_write_conn().
        context = contextlib.nullcontext(unsafe_mode) if unsafe_mode else self._get_write_conn()
        
        # --- ЭТАП 3: Атомарные операции с БД (внутри транзакции) ---
        with context as conn:
            target_task_id_str = str(task_id) if task_id else None
    
            # Определение ID цели, если он не задан
            if not target_task_id_str:
                cursor = conn.execute("SELECT task_id FROM tasks WHERE status = 'pending' ORDER BY priority DESC, sequence ASC LIMIT 1")
                row = cursor.fetchone()
                if not row: 
                    return None # Нет задач для обновления
                target_task_id_str = row['task_id']
            
            # Получение состояния "до"
            cursor = conn.execute("SELECT payload FROM tasks WHERE task_id = ?", (target_task_id_str,))
            row_before_update = cursor.fetchone()
            if not row_before_update: 
                return None # Задача исчезла

            # Выполнение UPDATE, если он нужен
            if updates:
                where_clauses = ["task_id = ?"]; where_params = [target_task_id_str]
                if 'current_worker_id' in conditions:
                    where_clauses.append("worker_id = ?"); where_params.append(conditions['current_worker_id'])
    
                query = f"UPDATE tasks SET {', '.join(updates)} WHERE {' AND '.join(where_clauses)}"
                final_params = tuple(params + where_params)
                cursor = conn.execute(query, final_params)
                
                # Проверка на состояние гонки
                if cursor.rowcount == 0 and conditions:
                    return None
            # --- ЭТАП 4: Возвращаем результат (все еще внутри транзакции) ---
            # Это безопасно, так как json.loads - быстрая операция
            
            payload = json.loads(row_before_update['payload'], object_hook=tuple_deserializer)

            return (uuid.UUID(target_task_id_str), payload)

    def task_done(self, worker_id: str, task_info: tuple):
        info = self.update_task(task_info[0], worker_id=worker_id, new_status='completed')
        if info:
            self._log(f"[TASK] ✅ Задача '{self._get_task_display_name(info[1])}' выполнена.")
            self._safe_request_ui_update()

    def task_done_with_content(self, worker_id: str, task_info: tuple, translated_content: str, provider_id: str):
        """
        Атомарно помечает задачу как выполненную и сохраняет ее результат.
        ВЕРСИЯ 3.0 (FIXED): Полностью разделяет транзакцию успеха и запуск обработки провала.
        """
        task_id_str = str(task_info[0])
        success = False
        try:
            with self._get_write_conn() as conn:
                # 1. Вызываем update_task в "небезопасном" режиме, передавая ему наше соединение.
                self.update_task(task_info[0], worker_id=worker_id, new_status='completed', unsafe_mode=conn)
                
                # 2. Выполняем вторую операцию в той же транзакции.
                conn.execute(
                    "INSERT OR REPLACE INTO chunk_results (task_id, translated_content, provider_id) VALUES (?, ?, ?)",
                    (task_id_str, translated_content, provider_id)
                )
            
            # Логируем только после успешного коммита (замок здесь уже отпущен!)
            success = True

        except Exception as e:
            # with-блок сам сделает rollback, замок отпущен.
            self._log(f"[DB ERROR] Ошибка при сохранении результата для задачи {task_id_str}: {e}")

        # Вызываем task_failed_permanently ТОЛЬКО если транзакция провалилась,
        # и делаем это ВНЕ блока try/with, чтобы гарантировать чистоту контекста.
        if not success:
            self.task_failed_permanently(worker_id, task_info)
        else:
            self._log(f"[TASK] ✅ Задача '{self._get_task_display_name(task_info[1])}' ({provider_id}) выполнена и результат сохранен.")
            self._safe_request_ui_update()
    
    def replace_batch_with_results(self, original_batch_task_id: str, epub_path: str, successful_chapters: list, failed_chapters: list):
        """
        Атомарно обновляет пакет: сохраняет успехи и обновляет/удаляет исходную задачу.
        Также логирует успех для каждой отдельной главы после фиксации транзакции.
        """
        with self._get_write_conn() as conn:
            # 1. Сохраняем успешные главы как 'completed'
            if successful_chapters:
                completed_tasks_to_insert = [
                    (str(uuid.uuid4()), json.dumps(('epub', epub_path, chapter), default=tuple_serializer), 'completed')
                    for chapter in successful_chapters
                ]
                conn.executemany("INSERT INTO tasks (task_id, payload, status) VALUES (?, ?, ?)", completed_tasks_to_insert)

            # 2. Обновляем исходную задачу
            if not failed_chapters:
                # Если хвостов нет -> удаляем задачу
                conn.execute("DELETE FROM tasks WHERE task_id = ?", (original_batch_task_id,))
            else:
                # Если хвосты есть -> обновляем payload
                # Статус и worker_id НЕ трогаем (задача остается у воркера)
                if len(failed_chapters) == 1:
                    new_payload = ('epub', epub_path, failed_chapters[0])
                else:
                    new_payload = ('epub_batch', epub_path, tuple(failed_chapters))
                
                conn.execute(
                    "UPDATE tasks SET payload = ? WHERE task_id = ?", 
                    (json.dumps(new_payload, default=tuple_serializer), original_batch_task_id)
                )
        
        # 3. Логируем успешные задачи (вне транзакции, чтобы не держать базу)
        if successful_chapters:
            for chapter in successful_chapters:
                # Генерируем имя задачи на лету для красивого лога, имитируя отдельную задачу 'epub'
                display_name = self._get_task_display_name(('epub', epub_path, chapter))
                self._log(f"[TASK] ✅ Задача '{display_name}' выполнена.")

        self._safe_request_ui_update()
    
    def replace_chunks_with_chapter(self, chunk_task_ids: list, epub_path: str, original_chapter_path: str):
        if not chunk_task_ids: return
        with self._get_write_conn() as conn:
            placeholders = ','.join('?' for _ in chunk_task_ids)
            conn.execute(f"DELETE FROM tasks WHERE task_id IN ({placeholders})", chunk_task_ids)
            new_task_id = str(uuid.uuid4())
            new_payload = ('epub', epub_path, original_chapter_path)
            cursor = conn.execute("SELECT MAX(sequence) FROM tasks WHERE status = 'completed'")
            max_seq_row = cursor.fetchone()
            max_seq = max_seq_row[0] if max_seq_row else None
            new_seq = 0 if max_seq is None else max_seq + 1
            conn.execute("INSERT INTO tasks (task_id, payload, status, sequence) VALUES (?, ?, ?, ?)", (new_task_id, json.dumps(new_payload, default=tuple_serializer), 'completed', new_seq))
        self._log(f"[TASK MANAGER] {len(chunk_task_ids)} чанков для '{os.path.basename(original_chapter_path)}' успешно собраны и заменены одной задачей.")
        self._safe_request_ui_update()

    def task_failed_permanently(self, worker_id: str, task_info: tuple):
        """Помечает задачу как 'failed' атомарно и логирует результат после."""
        # update_task сам управляет своей транзакцией.
        updated = self.update_task(
            task_info[0], 
            worker_id=worker_id, 
            new_status='failed', 
            current_worker_id=worker_id
        )
        if updated:
            self._log(f"[TASK] ❌ Задача '{self._get_task_display_name(task_info[1])}' провалена.")
            self._safe_request_ui_update()

    def task_requeued(self, worker_id: str, task_info: tuple):
        """
        Возвращает задачу в начало очереди (но с обычным приоритетом).
        """
        done = False 
        with self._get_write_conn() as conn:
            # Вычисляем sequence, чтобы встать ПЕРЕД всеми (меньше минимума)
            cursor = conn.execute("SELECT MIN(sequence) FROM tasks")
            min_seq_row = cursor.fetchone()
            min_seq = min_seq_row[0] if min_seq_row and min_seq_row[0] is not None else 0
            new_seq = min_seq - 1
            
            if self.update_task(task_info[0], worker_id=worker_id, new_status='pending', 
                               new_sequence=new_seq, new_priority=0, 
                               current_worker_id=worker_id, unsafe_mode=conn):
                done = True
            else:
                done = False

        if done:
            self._log(f"[TASK] 🔄 Задача '{self._get_task_display_name(task_info[1])}' возвращена в НАЧАЛО очереди.")
            self._safe_request_ui_update()
    
    def save_glossary_batch(self, task_id: str, timestamp: float, chapters_json: str, glossary_list: list) -> dict:
        """
        Сохраняет пакет терминов.
        Этап 1 (Write): Мгновенная запись в БД (блокировка минимальна).
        Этап 2 (Read): Создание клона и аналитика "что мы добавили" (без блокировки остальных).
        """
        if not glossary_list:
            return {'new': 0, 'updated': 0, 'total': 0}

        # --- ПОДГОТОВКА ДАННЫХ ---
        data_to_insert = [
            (str(task_id), timestamp, chapters_json, item['original'], item['rus'], item['note'])
            for item in glossary_list
        ]
        total_inserted = len(data_to_insert)
        
        # Список нормализованных оригиналов для аналитики
        batch_originals = list({item['original'].strip().lower() for item in glossary_list})
        
        # --- ЭТАП 1: БЫСТРАЯ ЗАПИСЬ ---
        # Заходим, вставляем, выходим. Блокировка снимается сразу после выхода из with.
        with self._get_write_conn() as conn:
            conn.executemany(
                """INSERT INTO glossary_results 
                   (task_id, timestamp, chapters_json, original, rus, note) 
                   VALUES (?, ?, ?, ?, ?, ?)""",
                data_to_insert
            )

        # --- ЭТАП 2: АНАЛИТИКА НА КЛОНЕ ---
        # Теперь, когда данные записаны и блокировка снята, мы спокойно берем
        # соединение для чтения (которое создает snapshot/клон в памяти) и считаем статистику.
        updated_count = 0 
        
        try:
            with self._get_read_only_conn() as conn:
                chunk_size = 900 # Лимит переменных SQLite
                for i in range(0, len(batch_originals), chunk_size):
                    chunk = batch_originals[i:i + chunk_size]
                    placeholders = ','.join('?' for _ in chunk)
                    
                    # Логика: Мы ищем, сколько из наших терминов УЖЕ встречались в ДРУГИХ задачах.
                    # Т.е. если original есть в базе, но task_id НЕ равен текущему - это дубль/обновление.
                    query = f"""
                        SELECT COUNT(DISTINCT LOWER(TRIM(original)))
                        FROM glossary_results
                        WHERE task_id != ? 
                        AND LOWER(TRIM(original)) IN ({placeholders})
                    """
                    
                    params = [str(task_id)] + chunk
                    cursor = conn.execute(query, params)
                    count = cursor.fetchone()[0]
                    updated_count += count
        except Exception as e:
            self._log(f"[DB STATS WARNING] Не удалось посчитать статистику глоссария: {e}")
            # В случае ошибки считаем, что все новые, чтобы не крашить процесс
            updated_count = 0

        new_count = total_inserted - updated_count
            
        return {'new': new_count, 'updated': updated_count, 'total': total_inserted}
    
    def task_requeued_for_retry(self, worker_id: str, task_info: tuple):
        """
        Возвращает задачу в начало очереди для повтора.
        Версия 3.0 (Smart Batch): 
        - Для 'epub_batch' НЕ обновляет payload, доверяя состоянию в БД (т.к. пакет мог мутировать).
        - Для остальных задач обновляет payload (сохраняя возможную обрезку хвоста от PartialGenerationError).
        """
        payload = task_info[1]
        task_type = payload[0]
        
        # ГЛАВНОЕ ИЗМЕНЕНИЕ:
        # Если это пакет -> new_payload=None (не трогаем базу, там актуальнее).
        # Если это файл/чанк -> new_payload=payload (сохраняем обрезку из памяти).
        payload_to_update = None if task_type == 'epub_batch' else payload

        # update_task сам управляет своей транзакцией.
        done = self.update_task(
            task_info[0], 
            worker_id=worker_id, 
            new_status='pending', 
            new_payload=payload_to_update,  # <-- Передаем либо None, либо данные
            new_priority=1, 
            current_worker_id=worker_id
        )
                
        if done:
            self._log(f"[TASK] 🔄 Задача '{self._get_task_display_name(payload)}' возвращена для повтора.")
            self._safe_request_ui_update()

    def remove_tasks(self, task_ids: list) -> bool:
        """Атомарно удаляет список задач из очереди."""
        if not task_ids:
            return False

        # 1. Подготовка данных (вне транзакции) - как вы и сделали.
        task_id_strs = [str(tid) for tid in task_ids]
        placeholders = ','.join('?' for _ in task_id_strs)
        
        rowcount = 0
        # 2. Атомарное удаление (внутри транзакции)
        with self._get_write_conn() as conn:
            cursor = conn.execute(f"DELETE FROM tasks WHERE task_id IN ({placeholders})", task_id_strs)
            rowcount = cursor.rowcount
        
        # 3. Уведомление UI (после транзакции)
        if rowcount > 0:
            self._safe_request_ui_update()
            
        return rowcount > 0

    def clear_all_queues(self):
        with self._get_write_conn() as conn:
            conn.execute("DELETE FROM tasks")
        self._safe_request_ui_update()
    
    def is_finished(self) -> bool:
        """
        Главный критерий завершения сессии.
        Версия 5.0 ("Умный Судья"):
        Проверяет и БД (активные задачи), и Память (флаг управляемой сессии).
        Возвращает True, только если работы нет НИГДЕ.
        """
        # 1. Проверка флага управляемой сессии (в памяти)
        is_managed_active = False
        if self.bus and hasattr(self.bus, '_data_store'):
            for key in self.bus._data_store.keys():
                if key.startswith('managed_session_active_') and self.bus.get_data(key) is True:
                    is_managed_active = True
                    break
        
        # Если мы в управляемом режиме — мы НЕ закончили, пока флаг висит.
        # Даже если в базе пусто (оркестратор готовит следующую задачу).
        if is_managed_active:
            return False

        # 2. Проверка базы данных (только если флага нет)
        # Игнорируем 'held', так как в обычном режиме это остатки Dry Run,
        # а в управляемом мы бы вышли выше по флагу.
        with self._get_read_only_conn() as conn:
            cursor = conn.execute("SELECT 1 FROM tasks WHERE status IN ('pending', 'in_progress') LIMIT 1")
            has_active_tasks = cursor.fetchone() is not None
            
        return not has_active_tasks

    # --- НАЧАЛО ВОССТАНОВЛЕННОГО БЛОКА КЭШИРОВАНИЯ ---
    @pyqtSlot()
    def _notify_ui_of_change(self):
        """
        СЛОТ, который выполняется в ГЛАВНОМ потоке.
        Безопасно запускает таймер для отложенного обновления кэша.
        """
        self._update_timer.start()
    
    def _safe_request_ui_update(self):
        """
        Безопасный метод для запроса обновления UI из ЛЮБОГО потока.
        Он просто испускает сигнал.
        """
        self._ui_update_requested.emit()
    
    def _trigger_cache_update(self):
        """Этот метод вызывается таймером и запускает фоновое обновление."""
        if self._is_updating_cache:
            return
        self._is_updating_cache = True
        worker = TaskDBWorker(self._get_ui_state_list_background)
        worker.finished.connect(lambda: self._on_cache_updated(worker))
        self._cache_update_worker = worker
        worker.start()

    def get_ui_state_list(self) -> list:
        """Основной метод для UI. Возвращает кэш."""
        return self._ui_state_list_cache

    def _on_cache_updated(self, worker):
        """Слот, который вызывается по завершении фонового обновления кэша."""
        if hasattr(worker, 'result') and worker.result is not None:
            new_state = worker.result
            if new_state != self._ui_state_list_cache:
                self._ui_state_list_cache = new_state
                self._post_event('task_state_changed', {'full_state': self._ui_state_list_cache})
        self._is_updating_cache = False
        self._cache_update_worker = None
    
    def _get_ui_state_list_background(self):
        """
        Этот метод выполняется в отдельном потоке!
        Он атомарно читает данные из in-memory клона.
        """
        ui_state_list = []
        # Получаем соединение с клоном, которое будет жить только внутри этого метода
        with self._get_read_only_conn() as conn:
            try:
                # Все операции чтения теперь происходят внутри одной транзакции на клоне
                cursor = conn.execute("""
                    SELECT task_id, payload, status FROM tasks
                    ORDER BY CASE status WHEN 'in_progress' THEN 1 WHEN 'pending' THEN 2 WHEN 'held' THEN 3 WHEN 'completed' THEN 4 WHEN 'failed' THEN 5 ELSE 6 END, priority DESC, sequence ASC
                """)
                all_rows = cursor.fetchall()
                
                failed_task_ids = [row['task_id'] for row in all_rows if row['status'] == 'failed']
                error_histories = {}
                if failed_task_ids:
                    placeholders = ','.join('?' for _ in failed_task_ids)
                    error_cursor = conn.execute(
                        f"SELECT task_id, error_type, COUNT(*) as count FROM task_errors WHERE task_id IN ({placeholders}) GROUP BY task_id, error_type",
                        failed_task_ids
                    )
                    for row in error_cursor:
                        task_id = row['task_id']
                        if task_id not in error_histories:
                            error_histories[task_id] = {'total_count': 0, 'errors': {}}
                        error_histories[task_id]['errors'][row['error_type']] = row['count']
                        error_histories[task_id]['total_count'] += row['count']
    
            except Exception as e:
                print(f"[CRITICAL DB WORKER] Ошибка в _get_ui_state_list_background: {e}")
                return None
        # Обработка данных происходит уже после закрытия соединения с клоном
        for row in all_rows:
            task_id_str, payload_json, status = row['task_id'], row['payload'], row['status']
            payload = json.loads(payload_json, object_hook=tuple_deserializer)
            ui_status = {'completed': 'success', 'failed': 'error'}.get(status, status)
            task_tuple_for_ui = (uuid.UUID(task_id_str), payload)
            details = error_histories.get(task_id_str, {})
            ui_state_list.append((task_tuple_for_ui, ui_status, details))
        return ui_state_list
    
    def get_all_tasks_for_rebuild(self) -> list[tuple]:
        """
        Возвращает АБСОЛЮТНО ВСЕ задачи из базы, независимо от их статуса,
        сохраняя при этом логический порядок. Используется для полной
        пересборки очереди. Формат: [(<uuid>, <payload>), ...].
        """
        with self._get_read_only_conn() as conn:
            cursor = conn.execute(
                """
                SELECT task_id, payload FROM tasks 
                ORDER BY priority DESC, sequence ASC
                """
            )
            tasks = []
            for row in cursor.fetchall():
                task_id = uuid.UUID(row['task_id'])
                payload = json.loads(row['payload'], object_hook=tuple_deserializer)
                tasks.append((task_id, payload))
            return tasks

    def record_failure(self, task_info: tuple, error_type: str):
        try:
            with self._get_write_conn() as conn:
                conn.execute("INSERT INTO task_errors (task_id, error_type, timestamp) VALUES (?, ?, ?)", (str(task_info[0]), error_type, time.time()))
        except sqlite3.IntegrityError:
            # [FIX] Игнорируем ошибку, если задача была удалена из базы
            # до того, как воркер успел сообщить о проблеме.
            pass
        
        self._safe_request_ui_update()
        
    def _get_task_display_name(self, task_payload: tuple) -> str:
        if not task_payload: return "Неизвестная задача"
        task_type = task_payload[0]
        try:
            if task_type == 'epub_chunk': return f"Часть #{task_payload[4]+1} из '{os.path.basename(str(task_payload[2]))}'"
            elif task_type == 'epub_batch':
                chapters = task_payload[2]
                if not chapters: return "Пустой пакет"
                def _extract_and_format_numbers(path_str: str) -> str:
                    filename = os.path.basename(str(path_str))
                    numbers = re.findall(r'\d+', filename)
                    return "_".join(numbers) if numbers else os.path.splitext(filename)[0]
                first_num_str = _extract_and_format_numbers(chapters[0])
                if len(chapters) > 1:
                    last_num_str = _extract_and_format_numbers(chapters[-1])
                    if first_num_str != last_num_str: return f'Пакет ("{first_num_str}" – "{last_num_str}")'
                return f'Пакет ("{first_num_str}")'
            elif task_type == 'epub': return f"Глава '{os.path.basename(str(task_payload[2]))}'"
            elif task_type == 'raw_text_translation': return f"✨ {task_payload[3]}" if len(task_payload) > 3 and task_payload[3] else "Прямой перевод"
            elif task_type == 'glossary_batch_task': return f"Пакет глоссария из {len(task_payload[2])} глав"
        except (IndexError, TypeError): pass
        return f"Задача типа '{task_type}'"

    def release_held_tasks(self):
        """Возвращает ВСЕ 'замороженные' задачи обратно в 'pending'."""
        rowcount = 0
        with self._get_write_conn() as conn:
            cursor = conn.execute("UPDATE tasks SET status = 'pending' WHERE status = 'held'")
            # --- ЗАХВАТЫВАЕМ РЕЗУЛЬТАТ ВНУТРИ БЛОКА ---
            rowcount = cursor.rowcount
        
        # --- ИСПОЛЬЗУЕМ СОХРАНЕННОЕ ЗНАЧЕНИЕ СНАРУЖИ ---
        if rowcount > 0:
            self._log(f"[TASK] 'Разморожено' {rowcount} задач.")
            self._safe_request_ui_update()
    
    def hold_all_pending_tasks(self):
        """Перемещает все ожидающие задачи в 'held'."""
        rowcount = 0
        with self._get_write_conn() as conn:
            cursor = conn.execute("UPDATE tasks SET status = 'held' WHERE status = 'pending'")
            # --- ЗАХВАТЫВАЕМ РЕЗУЛЬТАТ ВНУТРИ БЛОКА ---
            rowcount = cursor.rowcount
        
        # --- ИСПОЛЬЗУЕМ СОХРАНЕННОЕ ЗНАЧЕНИЕ СНАРУЖИ ---
        if rowcount > 0:
            self._log(f"[TASK] స్త 'Заморожено' {rowcount} задач.")
            self._safe_request_ui_update()
            
        return rowcount
    
    def reanimate_tasks(self, task_ids: list[uuid.UUID]):
        if not task_ids: return False
        with self._get_write_conn() as conn:
            task_id_strs = [str(tid) for tid in task_ids]
            placeholders = ','.join('?' for _ in task_id_strs)
            conn.execute(f"DELETE FROM task_errors WHERE task_id IN ({placeholders})", task_id_strs)
            
            cursor = conn.execute(f"SELECT task_id FROM tasks WHERE task_id IN ({placeholders}) AND status = 'failed'", task_id_strs)
            failed_ids_to_requeue = [row['task_id'] for row in cursor.fetchall()]
            
            if failed_ids_to_requeue:
                # Находим текущий минимум, чтобы вставить ПЕРЕД ним
                cursor_seq = conn.execute("SELECT MIN(sequence) FROM tasks")
                min_seq_row = cursor_seq.fetchone()
                min_seq = min_seq_row[0] if min_seq_row and min_seq_row[0] is not None else 0
                
                # Присваиваем уменьшающиеся индексы: min-1, min-2...
                # Это гарантирует, что они будут в топе, выше обычных задач
                for i, task_id in enumerate(failed_ids_to_requeue):
                    conn.execute(
                        "UPDATE tasks SET status = 'pending', priority = 0, worker_id = NULL, sequence = ? WHERE task_id = ?", 
                        (min_seq - 1 - i, task_id)
                    )
            
            requeued_count = len(failed_ids_to_requeue)
            cleared_count = len(task_id_strs) - requeued_count
            log_parts = []
            if cleared_count > 0: log_parts.append(f"Очищена история ошибок для {cleared_count} задач.")
            if requeued_count > 0: log_parts.append(f"Возвращено в НАЧАЛО очереди {requeued_count} проваленных задач.")
            if log_parts: self._log(f"[TASK MANAGER] Реанимация: {' '.join(log_parts)}")
        
        self._safe_request_ui_update()
        return True
    
    def get_failure_history(self, task_info: tuple) -> dict:
        """
        Получает историю ошибок для одной задачи.
        Версия 2.0: Захватывает данные внутри транзакции, обрабатывает снаружи.
        """
        task_id_str = str(task_info[0])
        history = {'total_count': 0, 'errors': {}}
        
        failes = [] # Инициализируем пустой список
        with self._get_read_only_conn() as conn:
            cursor = conn.execute(
                "SELECT error_type, COUNT(*) as count FROM task_errors WHERE task_id = ? GROUP BY error_type",
                (task_id_str,)
            )
            # --- ЗАХВАТЫВАЕМ ДАННЫЕ В ПРОСТОЙ СПИСОК ---
            failes = cursor.fetchall()
        
        # --- ОБРАБАТЫВАЕМ ДАННЫЕ ПОСЛЕ ЗАКРЫТИЯ СОЕДИНЕНИЯ ---
        for row in failes:
            # row['error_type'] и row['count'] теперь доступны безопасно
            history['errors'][row['error_type']] = row['count']
            history['total_count'] += row['count']
        
        return history

    def reorder_tasks(self, action: str, task_ids: list[uuid.UUID]):
        if not task_ids: return False
        with self._get_write_conn() as conn:
            cursor = conn.execute("""
                SELECT task_id, sequence 
                FROM tasks 
                WHERE status = 'pending' 
                ORDER BY priority DESC, sequence ASC
            """)
            all_pending = cursor.fetchall()
            if not all_pending: return False
            task_id_strs = [str(tid) for tid in task_ids]
            if action in ('top', 'bottom'):
                moved_tasks = [r for r in all_pending if r['task_id'] in task_id_strs]
                if not moved_tasks: return False
                remaining_tasks = [r for r in all_pending if r['task_id'] not in task_id_strs]
                new_order = moved_tasks + remaining_tasks if action == 'top' else remaining_tasks + moved_tasks
                for i, task_row in enumerate(new_order):
                    conn.execute("UPDATE tasks SET sequence = ?, priority = 0 WHERE task_id = ?", (i, task_row['task_id']))
            elif action in ('up', 'down'):
                all_pending_ids = [r['task_id'] for r in all_pending]
                valid_task_id_strs = [tid for tid in task_id_strs if tid in all_pending_ids]
                if not valid_task_id_strs: return False
                if action == 'up':
                    indices = sorted([all_pending_ids.index(tid) for tid in valid_task_id_strs])
                    for i in indices:
                        if i > 0: all_pending_ids.insert(i-1, all_pending_ids.pop(i))
                else:
                    indices = sorted([all_pending_ids.index(tid) for tid in valid_task_id_strs], reverse=True)
                    for i in indices:
                        if i < len(all_pending_ids) - 1: all_pending_ids.insert(i+1, all_pending_ids.pop(i))
                for i, task_id in enumerate(all_pending_ids):
                    conn.execute("UPDATE tasks SET sequence = ?, priority = 0 WHERE task_id = ?", (i, task_id))
            else: return False
        self._safe_request_ui_update()
        return True
    
    def duplicate_tasks(self, task_ids: list) -> bool:
        """
        Дублирует выбранные задачи, вставляя копии сразу после
        последнего из выбранных оригиналов. Версия 3.0 (прагматичная).
        """
        if not task_ids:
            return False

        # Подготовка данных, не требующих доступа к БД
        task_id_strs = [str(tid) for tid in task_ids]
        placeholders = ','.join('?' for _ in task_id_strs)

        with self._get_write_conn() as conn:
            # === НАЧАЛО ЕДИНОЙ АТОМАРНОЙ ОПЕРАЦИИ ===
            
            # 1. Получаем данные для дублирования
            cursor = conn.execute(
                f"SELECT payload, sequence FROM tasks WHERE task_id IN ({placeholders}) ORDER BY sequence ASC",
                task_id_strs
            )
            originals_to_duplicate = cursor.fetchall()

            if not originals_to_duplicate:
                # Если ничего не нашли, просто выходим. Транзакция закроется.
                return False

            # 2. Выполняем быстрые вычисления внутри транзакции
            num_duplicates = len(originals_to_duplicate)
            last_original_sequence = originals_to_duplicate[-1]['sequence']

            # 3. Готовим данные для вставки (тоже быстрая операция)
            tasks_to_insert = [
                (str(uuid.uuid4()), original_row['payload'], 'pending', last_original_sequence + 1 + i)
                for i, original_row in enumerate(originals_to_duplicate)
            ]

            # 4. Выполняем обе операции записи
            conn.execute(
                "UPDATE tasks SET sequence = sequence + ? WHERE sequence > ?",
                (num_duplicates, last_original_sequence)
            )
            conn.executemany(
                "INSERT INTO tasks (task_id, payload, status, sequence) VALUES (?, ?, ?, ?)",
                tasks_to_insert
            )
            
            # === КОНЕЦ АТОМАРНОЙ ОПЕРАЦИИ ===
        
        # Уведомляем UI только после успешного коммита
        self._safe_request_ui_update()
        return True
    
    def update_many(self, task_ids: list[uuid.UUID], new_status: str = None, new_priority: int = None):
        if not task_ids: return
        task_id_strs = [str(tid) for tid in task_ids]
        updates, params = [], []
        if new_status is not None: updates.append("status = ?"); params.append(new_status)
        if new_priority is not None: updates.append("priority = ?"); params.append(new_priority)
        if not updates: return
        placeholders = ','.join('?' for _ in task_id_strs)
        query = f"UPDATE tasks SET {', '.join(updates)} WHERE task_id IN ({placeholders})"
        final_params = tuple(params + task_id_strs)
        
        with self._get_write_conn() as conn:
            conn.execute(query, final_params)
        
        self._safe_request_ui_update()

    def has_pending_tasks(self) -> bool:
        """
        Проверяет, есть ли задачи в очереди или активна ли управляемая сессия.
        """
        # Сначала проверяем наличие задач со статусом 'pending' в самой БД
        with self._get_read_only_conn() as conn:
            cursor = conn.execute("SELECT 1 FROM tasks WHERE status = 'pending' LIMIT 1")
            if cursor.fetchone():
                return True
        
        # Если в БД задач нет, проверяем флаг управляемой сессии в шине событий
        if self.bus and hasattr(self.bus, '_data_store'):
            # Ищем любой ключ, начинающийся с 'managed_session_active_'
            for key in self.bus._data_store.keys():
                if key.startswith('managed_session_active_') and self.bus.get_data(key) is True:
                    return True
        
        return False
    
    def get_first_pending_task_payload(self) -> tuple | None:
        """
        Возвращает payload ПЕРВОЙ ожидающей задачи без извлечения ее из очереди.
        Возвращает None, если очередь пуста.
        """
        payload_json = None
        with self._get_read_only_conn() as conn:
            cursor = conn.execute(
                """
                SELECT payload FROM tasks
                WHERE status = 'pending'
                ORDER BY priority DESC, sequence ASC
                LIMIT 1
                """
            )
            row = cursor.fetchone()
            if row:
                # --- ЗАХВАТЫВАЕМ сырые данные ---
                payload_json = row['payload']
        
        # --- ОБРАБАТЫВАЕМ снаружи ---
        if payload_json:
            payload = json.loads(payload_json, object_hook=tuple_deserializer)
            return payload
            
        return None
    
    def hold_all_except_first(self):
        """
        Перемещает все ожидающие задачи в 'held', кроме самой первой.
        Используется для режима "пробного запуска".
        """
        updated_count = 0
        with self._get_write_conn() as conn: # Атомарная транзакция
            # 1. Находим ID первой задачи в очереди
            cursor = conn.execute(
                """
                SELECT task_id FROM tasks
                WHERE status = 'pending'
                ORDER BY priority DESC, sequence ASC
                LIMIT 1
                """
            )
            first_task_row = cursor.fetchone()
    
            if first_task_row:
                first_task_id = first_task_row['task_id']
                # 2. "Замораживаем" все остальные ожидающие задачи
                update_cursor = conn.execute(
                    "UPDATE tasks SET status = 'held' WHERE status = 'pending' AND task_id != ?",
                    (first_task_id,)
                )
                updated_count = update_cursor.rowcount
        
        # 3. Отправляем сигнал и лог ТОЛЬКО если что-то изменилось
        if updated_count > 0:
            self._log(f"[TASK] స్త 'Заморожено' {updated_count} задач для пробного запуска.")
            self._safe_request_ui_update()
    
    def get_all_pending_tasks(self) -> list[tuple]:
        """
        Возвращает полный, упорядоченный список всех ожидающих задач.
        Формат: [(<uuid>, <payload>), ...].
        Включает задачи со статусами 'pending' и 'held'.
        """
        raw_rows = []
        with self._get_read_only_conn() as conn:
            cursor = conn.execute(
                """
                SELECT task_id, payload FROM tasks 
                WHERE status IN ('pending', 'held') 
                ORDER BY priority DESC, sequence ASC
                """
            )
            # --- ЗАХВАТ СЫРЫХ ДАННЫХ ---
            raw_rows = cursor.fetchall()

        # --- ОБРАБОТКА СНАРУЖИ ---
        tasks = []
        for row in raw_rows:
            try:
                task_id = uuid.UUID(row['task_id'])
                payload = json.loads(row['payload'], object_hook=tuple_deserializer)
                tasks.append((task_id, payload))
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                # Добавим защиту на случай поврежденных данных в БД
                print(f"[TaskManager WARN] Не удалось обработать строку задачи из БД: {row}. Ошибка: {e}")
                
        return tasks

    def set_pending_tasks(self, tasks_payloads: list, initial_history: dict = None):
        """
        Полностью ЗАМЕНЯЕТ ВСЕ задачи в менеджере на новый список.
        Версия 2.1: Поддерживает initial_history для инъекции ошибок (например, для смены режима апи с самого начала).
        """
        # --- ЭТАП 1: Подготовка данных (вне транзакции) ---
        tasks_to_insert = []
        errors_to_insert = []
        
        if tasks_payloads: # Выполняем подготовку, только если есть что готовить
            for i, task_payload in enumerate(tasks_payloads):
                task_id = uuid.uuid4()
                task_id_str = str(task_id)
                payload = self._normalize_payload(task_payload)
                
                tasks_to_insert.append((
                    task_id_str, json.dumps(payload, default=tuple_serializer), 'pending', i
                ))
                
                # Если передана история, дублируем её для КАЖДОЙ новой задачи
                if initial_history:
                    timestamp = time.time()
                    for error_type, count in initial_history.get('errors', {}).items():
                        for _ in range(count):
                            errors_to_insert.append((task_id_str, error_type, timestamp))
        
        # --- ЭТАП 2: Атомарная запись в БД (внутри транзакции) ---
        with self._get_write_conn() as conn:
            # Сначала всегда полная очистка задач и их ошибок (каскадно, но для надежности чистим всё)
            conn.execute("DELETE FROM tasks")
            conn.execute("DELETE FROM task_errors") # Явная очистка ошибок при сбросе очереди
            
            # Затем вставка новых данных
            if tasks_to_insert:
                conn.executemany(
                    "INSERT INTO tasks (task_id, payload, status, sequence) VALUES (?, ?, ?, ?)",
                    tasks_to_insert
                )
            
            # Вставка инъекцированных ошибок
            if errors_to_insert:
                conn.executemany(
                    "INSERT INTO task_errors (task_id, error_type, timestamp) VALUES (?, ?, ?)",
                    errors_to_insert
                )
        
        # --- ЭТАП 3: Уведомление UI (вне транзакции) ---
        self._safe_request_ui_update()


    def fetch_and_clean_glossary(self, mode: str = 'supplement', cleanup_threshold: float = 0.3, min_count: int = 500, return_raw: bool = False) -> list:
        """
        Центральный метод доступа к глоссарию.
        
        Логика:
        1. Если mode='accumulate' И return_raw=True -> Возвращает ВСЮ историю (для просмотра в UI).
        2. Во всех остальных случаях -> Возвращает дедуплицированный 'чистый' список (для AI или чистого UI).
        3. Если mode != 'accumulate' -> Может запустить фоновую очистку базы от мусора.
        """
        
        clean_data = []
        should_cleanup = False
        stats = (0, 0)

        # --- СЦЕНАРИЙ 1: "ДАЙ МНЕ ВСЁ" (Только для режима Накопления) ---
        # Если мы в режиме обновления/дополнения, raw-дамп не имеет смысла, 
        # поэтому мы проваливаемся в Сценарий 2, чтобы показать результат логики слияния.
        if return_raw and mode == 'accumulate':
            sql_query = "SELECT original, rus, note, timestamp FROM glossary_results ORDER BY timestamp ASC"
            try:
                with self._get_read_only_conn() as conn:
                    cursor = conn.execute(sql_query)
                    # Возвращаем "как есть", база выступает просто архивом
                    return [dict(row) for row in cursor.fetchall()]
            except Exception as e:
                self._log(f"[TaskManager] Ошибка raw-чтения глоссария: {e}")
                return []

        # --- СЦЕНАРИЙ 2: "УМНАЯ ДЕДУПЛИКАЦИЯ" (Для AI или режимов Update/Supplement) ---
        
        if mode == 'supplement':
            order_direction = 'ASC'  # Старые важнее (First write wins)
        else:
            order_direction = 'DESC' # Новые важнее (Last write wins) или accumulate для AI

        # SQL с дедупликацией "на лету" через оконные функции
        sql_query = f"""
        WITH RankedTerms AS (
            SELECT 
                id, original, rus, note, timestamp,
                ROW_NUMBER() OVER (
                    PARTITION BY LOWER(TRIM(original)) 
                    ORDER BY timestamp {order_direction}, id {order_direction}
                ) as rn
            FROM glossary_results
        )
        SELECT original, rus, note, timestamp FROM RankedTerms WHERE rn = 1 ORDER BY timestamp ASC;
        """

        try:
            with self._get_read_only_conn() as conn:
                cursor = conn.execute(sql_query)
                clean_data = [dict(row) for row in cursor.fetchall()]
                
                # Логика проверки на мусор нужна ТОЛЬКО если мы НЕ в режиме накопления
                if mode != 'accumulate':
                    unique_count = len(clean_data)
                    
                    if unique_count > min_count:
                        total_cursor = conn.execute("SELECT COUNT(*) FROM glossary_results")
                        total_count = total_cursor.fetchone()[0]
                        
                        trash_count = total_count - unique_count
                        if trash_count > 0 and (trash_count / unique_count) > cleanup_threshold:
                            should_cleanup = True
                            stats = (total_count, unique_count)

        except Exception as e:
            self._log(f"[TaskManager] Ошибка smart-чтения глоссария: {e}")
            return []

        # Запуск фоновой очистки (строго запрещена для accumulate)
        if should_cleanup:
            if self._glossary_cleanup_worker is None or not self._glossary_cleanup_worker.isRunning():
                self._log(f"[DB MONITOR] 🚨 Мусора > {cleanup_threshold:.0%}. Запуск фоновой очистки.")
                self._glossary_cleanup_worker = TaskDBWorker(
                    self._surgical_cleanup,
                    order_direction=order_direction,
                    stats=stats
                )
                self._glossary_cleanup_worker.start()
        
        return clean_data
    
    def _surgical_cleanup(self, order_direction: str, stats: tuple):
        """
        Исполнительный метод для воркера. 
        Выполняет ТОЛЬКО удаление через защищенное соединение на запись.
        """
        total_before, unique_should_be = stats
        
        # Запрос на удаление проигравших
        delete_query = f"""
        DELETE FROM glossary_results 
        WHERE id NOT IN (
            SELECT id FROM (
                SELECT id, 
                       ROW_NUMBER() OVER (
                           PARTITION BY LOWER(TRIM(original)) 
                           ORDER BY timestamp {order_direction}, id {order_direction}
                       ) as rn
                FROM glossary_results
            ) WHERE rn = 1
        )
        """
        
        # _get_write_conn встанет в очередь PatientLock и выполнит транзакцию
        try:
            with self._get_write_conn() as conn:
                conn.execute(delete_query)
            self._log(f"[DB CLEANER] 🧹 Очистка завершена. Удалено ~{total_before - unique_should_be} дубликатов.")
        except Exception as e:
            self._log(f"[DB CLEANER ERROR] Ошибка при очистке: {e}")
    
    
    def _get_epub_signature(self, filepath: str) -> str:
        """
        Создает уникальный цифровой отпечаток файла (размер + хеш начала и конца).
        """
        if not os.path.exists(filepath):
            return "FILE_NOT_FOUND"
        
        stat = os.stat(filepath)
        size = stat.st_size
        
        # MD5 достаточно для проверки целостности/идентичности файла в этом контексте
        h = hashlib.md5()
        h.update(str(size).encode('utf-8'))
        
        try:
            with open(filepath, 'rb') as f:
                # Хешируем первые 8кб
                h.update(f.read(8192))
                # И последние 8кб (если файл достаточно большой)
                if size > 16384:
                    f.seek(-8192, 2)
                    h.update(f.read(8192))
        except Exception:
            return "READ_ERROR"
        
        return h.hexdigest()



    def save_queue_snapshot(self, snapshot_path: str, current_epub_path: str) -> bool:
        """
        Сохраняет состояние очереди на диск.
        ИСПРАВЛЕНО: Убран Deadlock, вызванный конфликтом транзакции и backup API.
        """
        signature = self._get_epub_signature(current_epub_path)
        
        # Переменная для соединения, чтобы гарантированно закрыть его в finally
        snapshot_conn = None
        
        try:
            # 1. Получаем приватный клон базы.
            # ВАЖНО: Не используем 'with snapshot_conn:', так как это откроет транзакцию,
            # которая может помешать бэкапу.
            snapshot_conn = self._get_read_only_conn()
                
            # 2. Модифицируем НАШ КЛОН (добавляем метку безопасности)
            snapshot_conn.execute("CREATE TABLE IF NOT EXISTS meta_info (key TEXT PRIMARY KEY, value TEXT)")
            snapshot_conn.execute("INSERT OR REPLACE INTO meta_info VALUES ('epub_sig', ?)", (signature,))
            snapshot_conn.execute("INSERT OR REPLACE INTO meta_info VALUES ('epub_path', ?)", (current_epub_path,))
            
            # ВАЖНО: Фиксируем изменения в клоне перед отправкой
            snapshot_conn.commit()
            
            # 3. Сбрасываем модифицированный клон на диск
            if os.path.exists(snapshot_path):
                try:
                    os.remove(snapshot_path)
                except OSError:
                    pass # Если файл занят, connect ниже выбросит ошибку, это ок
            
            # Подключаемся к файлу на диске
            disk_conn = sqlite3.connect(snapshot_path)
            
            try:
                # ВАЖНО: Выполняем backup БЕЗ обертки 'with disk_conn'.
                # API бэкапа само управляет блокировками.
                snapshot_conn.backup(disk_conn)
            finally:
                disk_conn.close()
                
            self._log(f"[DB] 💾 Очередь задач сохранена в '{os.path.basename(snapshot_path)}'.")
            return True
            
        except Exception as e:
            self._log(f"[DB ERROR] Не удалось сохранить очередь: {e}")
            return False
        finally:
            if snapshot_conn:
                snapshot_conn.close()


    def load_queue_snapshot(self, snapshot_path: str, current_epub_path: str) -> list | None:
        """
        Загружает базу с диска.
        ИСПРАВЛЕНО: Ручное управление соединением без транзакции для backup().
        """
        if not os.path.exists(snapshot_path):
            return None
            
        current_sig = self._get_epub_signature(current_epub_path)
        
        # Переменные для корректного закрытия в finally
        disk_conn = None
        mem_conn = None
        
        try:
            # 1. Проверка безопасности (Читаем диск)
            disk_conn = sqlite3.connect(snapshot_path)
            disk_conn.row_factory = sqlite3.Row
            
            try:
                # Проверяем наличие таблицы и хеша
                cursor = disk_conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='meta_info'")
                if not cursor.fetchone():
                    raise ValueError("Файл устарел или поврежден (нет метаданных).")

                cursor = disk_conn.execute("SELECT value FROM meta_info WHERE key='epub_sig'")
                row = cursor.fetchone()
                saved_sig = row['value'] if row else None
                
                if saved_sig != current_sig:
                    print(f"[DB SECURITY] Хеш не совпал! Файл: {saved_sig} vs Текущий: {current_sig}")
                    raise ValueError("Файл очереди не соответствует выбранной книге (хеш не совпал).")
                    
            except sqlite3.DatabaseError:
                raise ValueError("Файл базы данных поврежден.")
            
            # 2. Атомарное восстановление (ВЛИВАНИЕ)
            
            # ШАГ А: Блокируем доступ другим потокам Python
            self._chancellor_lock.acquire()
            try:
                # ШАГ Б: Получаем "голое" соединение к общей памяти.
                # ВАЖНО: Не используем _get_write_conn, чтобы не стартовать транзакцию (BEGIN).
                # backup() требует, чтобы целевая база была в состоянии autocommit (idle).
                mem_conn = self._get_conn()
                
                # ШАГ В: Выполняем подмену.
                # disk_conn (source) -> mem_conn (dest)
                disk_conn.backup(mem_conn)
                
            finally:
                if mem_conn: mem_conn.close()
                self._chancellor_lock.release()
            
            self._log(f"[DB] 📂 Очередь задач успешно восстановлена из диска.")
            
            # 3. Извлекаем список глав для синхронизации UI
            # (делаем это уже с новой, загруженной базой)
            extracted_chapters = []
            seen_chapters = set()
            
            with self._get_read_only_conn() as conn:
                cursor = conn.execute("SELECT payload FROM tasks ORDER BY priority DESC, sequence ASC")
                for row in cursor.fetchall():
                    try:
                        payload = json.loads(row['payload'], object_hook=tuple_deserializer)
                        task_type = payload[0]
                        chapters_in_task = []
                        
                        if task_type in ('epub', 'epub_chunk'):
                            chapters_in_task = [payload[2]]
                        elif task_type == 'epub_batch':
                            chapters_in_task = payload[2]
                        
                        for ch in chapters_in_task:
                            if ch not in seen_chapters:
                                extracted_chapters.append(ch)
                                seen_chapters.add(ch)
                    except Exception:
                        continue
            
            return extracted_chapters

        except Exception as e:
            self._log(f"[DB LOAD ERROR] {e}")
            # Если хеш не совпал, можно удалить файл, чтобы не мешался
            if "хеш не совпал" in str(e) and os.path.exists(snapshot_path):
                try:
                    os.remove(snapshot_path)
                    self._log(f"[DB SECURITY] Файл '{os.path.basename(snapshot_path)}' удален.")
                except OSError: pass
            raise e
        finally:
            if disk_conn: disk_conn.close()

class TaskDBWorker(QThread):
    finished = pyqtSignal()

    def __init__(self, target_func, *args, **kwargs):
        super().__init__()
        self.target_func = target_func
        self.args = args
        self.kwargs = kwargs
        self.result = None

    def run(self):
        try:
            self.result = self.target_func(*self.args, **self.kwargs)
        except Exception as e:
            print(f"[CRITICAL DB WORKER ERROR] Ошибка в фоновой задаче: {e}")
            self.result = None
        finally:
            self.finished.emit()