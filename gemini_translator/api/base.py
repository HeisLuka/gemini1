import threading
import asyncio
import aiohttp
import re
from collections import Counter
from PyQt6.QtWidgets import QApplication
from ..utils.async_helpers import run_sync
from .errors import (
    OperationCancelledError, ContentFilterError, RateLimitExceededError, LocationBlockedError, SuccessSignal,
    ModelNotFoundError, ValidationFailedError, NetworkError, PartialGenerationError, TemporaryRateLimitError, GracefulShutdownInterrupt
)

_thread_local = threading.local()

try:
    import requests
    from requests.exceptions import RequestException as RequestsError
except ImportError:
    requests = None
    class RequestsError(Exception): pass

try:
    import socks
    from aiohttp_socks import ProxyConnector, ProxyType
    PROXY_ERRORS = (socks.ProxyError, socks.GeneralProxyError, socks.ProxyConnectionError)
except (ImportError, AttributeError):
    socks = None
    ProxyConnector = None
    ProxyType = None
    PROXY_ERRORS = ()

def get_worker_loop():
    """Получает или создает event loop для текущего потока воркера."""
    if not hasattr(_thread_local, "loop") or _thread_local.loop.is_closed():
        _thread_local.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_thread_local.loop)
    return _thread_local.loop

class BaseApiHandler:
    """
    Базовый класс-стратегия с умной гибридной логикой.
    Версия 5.0: Исправлена диспетчеризация асинхронных вызовов.
    """
    def __init__(self, worker):
        super().__init__()
        self.worker = worker
        self.is_async_native = self.worker.provider_config.get("is_async", False)
        self.proxy_settings = None

    def _proactive_session_init(self):
        api_timeout = self.worker.provider_config.get("base_timeout", 600)
        loop = get_worker_loop()
        loop.run_until_complete(self._get_or_create_session_internal(api_timeout))
        
    def setup_client(self, client_override=None, proxy_settings=None):
        """Базовая настройка."""
        self.proxy_settings = proxy_settings
        return True

    async def _get_or_create_session_internal(self, api_timeout=600):
        """[Внутренний] Лениво создает сессию."""
        if hasattr(_thread_local, "session") and not _thread_local.session.closed:

            return _thread_local.session
        
        connector = None
        if self.proxy_settings and self.proxy_settings.get('enabled'):
            try:
                host = self.proxy_settings.get('host')
                port = self.proxy_settings.get('port')
                p_type = self.proxy_settings.get('type', 'SOCKS5').lower()
                user = self.proxy_settings.get('user')
                pwd = self.proxy_settings.get('pass')
                
                if host and port:
                    auth = f"{user}:{pwd}@" if user and pwd else ""
                    url = f"{p_type}://{auth}{host}:{port}"
                    connector = ProxyConnector.from_url(url, rdns=True)
            except Exception as e:
                print(f"[API ERROR] Не удалось создать прокси-коннектор: {e}")

        timeout = aiohttp.ClientTimeout(total=api_timeout)
        _thread_local.session = aiohttp.ClientSession(
            loop=get_worker_loop(),
            timeout=timeout,
            connector=connector 
        )
        return _thread_local.session

    async def _close_thread_session_internal(self):
        """[Внутренний] Закрывает сессию для текущего потока."""
        if hasattr(_thread_local, "session"):
            session = getattr(_thread_local, "session")
            if session and not session.closed:
                await session.close()
            delattr(_thread_local, "session")

    
    def _force_session_reset(self):
        """
        [Внутренний] Принудительно удаляет сессию из thread_local.
        Используется при критических ошибках соединения (ServerDisconnected и т.д.),
        чтобы следующий запрос гарантированно создал чистое подключение.
        """
        if hasattr(_thread_local, "session"):
            session = getattr(_thread_local, "session")
            # Пытаемся закрыть корректно, но не блокируемся, если это невозможно синхронно
            if session and not session.closed:
                try:
                    # Создаем задачу на закрытие в текущем лупе, не дожидаясь её
                    loop = get_worker_loop()
                    if loop.is_running():
                        loop.create_task(session.close())
                except Exception:
                    pass # Игнорируем ошибки закрытия, так как мы все равно удаляем ссылку
            
            delattr(_thread_local, "session")
    
    
    
    async def execute_api_call(self, prompt, log_prefix, allow_incomplete=False, debug=False, use_stream=True, max_output_tokens=None):
        self.worker.settings_manager.increment_request_count(self.worker.api_key, self.worker.model_id)
        
        try:
            if self.is_async_native:
                # ВАЖНО: Здесь обязательно должен быть await!
                return await self._async_executor(prompt, log_prefix, allow_incomplete, use_stream, debug, max_output_tokens)
            else:
                return await self._sync_executor_wrapper(prompt, log_prefix, allow_incomplete, use_stream, debug, max_output_tokens)

        except asyncio.CancelledError:
            # ПЕРЕХВАТ СИСТЕМНОЙ ОТМЕНЫ (Критический фикс для таймаутов DNS/Aiohttp)
            
            # Сценарий 1: Это штатная отмена (пользователь нажал Стоп)
            if self.worker.is_cancelled:
                 raise OperationCancelledError("Операция отменена системой (asyncio.CancelledError)")
            
            # Сценарий 2: Это "Тихий убийца". Таймаут внутри aiohttp всплыл как CancelledError.
            # Превращаем его в NetworkError, чтобы ErrorAnalyzer увидел проблему, записал в лог и сделал ретрай.
            error_msg = "Запрос прерван (CancelledError). Вероятная причина: таймаут DNS или сброс соединения."
            self._force_session_reset() # Сбрасываем сессию, так как коннектор может быть 'битым'
            raise NetworkError(error_msg, delay_seconds=10)

        except Exception as e:
            self._process_exception_and_counters(e)

    async def _async_executor(self, prompt, log_prefix, allow_incomplete, use_stream, debug, max_output_tokens):
        """
        Обертка для асинхронных вызовов.
        """
        # 1. Создаем корутину вызова API. 
        # ВАЖНО: Это только объект, код внутри call_api еще не выполняется.
        api_coroutine = self.call_api(
            prompt, log_prefix, allow_incomplete, use_stream, debug, max_output_tokens
        )
        
        api_timeout = self.worker.provider_config.get("base_timeout", 600)

        # 2. Оборачиваем корутину в wait_for (тоже корутина)
        api_task_with_timeout = asyncio.wait_for(api_coroutine, timeout=api_timeout)

        # 3. Создаем задачи для цикла событий. Вот ТУТ они начинают выполняться.
        checker_task = asyncio.create_task(self._cancellation_checker())
        api_task = asyncio.create_task(api_task_with_timeout)
        
        try:
            done, pending = await asyncio.wait({api_task, checker_task}, return_when=asyncio.FIRST_COMPLETED)
            
            if checker_task in done:
                # Если сработала отмена
                api_task.cancel()
                # Обязательно дожидаемся отмены, чтобы избежать warning'ов
                try:
                    await api_task
                except asyncio.CancelledError:
                    pass
                raise OperationCancelledError("Отмена обнаружена во время ожидания API")
            
            if api_task in done:
                # Если API ответило (или упало с ошибкой)
                checker_task.cancel()
                return await api_task
                
        except asyncio.TimeoutError:
            checker_task.cancel()
            api_task.cancel() # Отменяем зависший запрос
            raise NetworkError(f"Глобальный таймаут API ({api_timeout}с) превышен.", delay_seconds=30)
        except Exception as e:
            # Страховка на случай непредвиденных ошибок в asyncio.wait
            checker_task.cancel()
            if not api_task.done():
                api_task.cancel()
            raise e

    async def _sync_executor_wrapper(self, prompt, log_prefix, allow_incomplete, use_stream, debug, max_output_tokens):
        """Обертка для СИНХРОННЫХ вызовов (через run_sync)."""
        api_timeout = self.worker.provider_config.get("base_timeout", 600)
        
        api_coro = run_sync(
            self.call_api,
            prompt, log_prefix, allow_incomplete, use_stream, debug, max_output_tokens,
            forget=False,
            timeout=api_timeout
        )
        
        checker_task = asyncio.create_task(self._cancellation_checker())
        api_task = asyncio.create_task(api_coro)
        
        try:
            done, pending = await asyncio.wait({api_task, checker_task}, return_when=asyncio.FIRST_COMPLETED)
            if checker_task in done:
                api_task.cancel()
                raise OperationCancelledError("Отмена обнаружена во время ожидания API")
            if api_task in done:
                checker_task.cancel()
                return await api_task
        except asyncio.TimeoutError:
            checker_task.cancel()
            raise NetworkError(f"Глобальный таймаут API ({api_timeout}с) превышен.")

    async def _cancellation_checker(self):
        """Пингует флаг отмены каждые 200мс."""
        while not self.worker.is_cancelled:
            await asyncio.sleep(0.2)
    
    def call_api(self, prompt, log_prefix, allow_incomplete=False, use_stream=True, debug=False, max_output_tokens=None):
        """
        Основной метод. Реализации должны вызывать self._get_or_create_session_internal().
        """
        raise NotImplementedError
    
    def _process_exception_and_counters(self, e: Exception):
        # 1. Специфичная логика для PartialGenerationError
        if isinstance(e, PartialGenerationError):
            # Проводим диагностику текста на зацикливание
            if self._detect_looping(e.partial_text):
                # ВМЕШАТЕЛЬСТВО: Если найден цикл, превращаем ошибку в ValidationFailedError.
                # Это заставит воркер сбросить текущий прогресс и начать генерацию заново,
                # вместо того чтобы продолжать дописывать повторяющийся бред.
                raise ValidationFailedError(f"Обнаружено зацикливание текста в прерванном ответе. Причина сброса: {e.reason}")
            
            # Если цикла нет - пробрасываем как есть (воркер попробует дописать)
            raise e
        
        
        # Стандартная обработка ошибок
        if isinstance(e, (
            OperationCancelledError, ContentFilterError, ValidationFailedError
        )):
            raise e

        self.worker.settings_manager.decrement_request_count(self.worker.api_key, self.worker.model_id)
        
        # ЛОГИКА СБРОСА СЕССИИ
        # Если это NetworkError или ошибка aiohttp, сбрасываем сессию,
        # так как коннектор может быть в "битом" состоянии.
        is_aiohttp_error = isinstance(e, (aiohttp.ClientError, asyncio.TimeoutError, OSError))
        # Проверяем также по тексту ошибки, если она завернута
        error_text = str(e).lower()
        is_disconnect = "disconnected" in error_text or "connection" in error_text or "closed" in error_text
        
        if is_aiohttp_error or is_disconnect or isinstance(e, NetworkError):
            self._force_session_reset()
        
        if ("http error" in error_text and "403" in error_text) or ("http 403" in error_text):
            raise LocationBlockedError(f"Ошибка доступа 403") from e
        
        # Далее стандартная классификация для ретраев
        if isinstance(e, (
            NetworkError, TemporaryRateLimitError, RateLimitExceededError, LocationBlockedError, ModelNotFoundError
        )):
            raise e
        
        if "сannot connect to host" in error_text or "getaddrinfo failed" in error_text:
            raise NetworkError(f"Нет связи с сервером, или нет интернета.", delay_seconds=60) from e
        
        if isinstance(e, aiohttp.ClientResponseError) and e.status == 429:
            raise TemporaryRateLimitError(f"Превышен минутный лимит (код 429)", delay_seconds=65) from e
        
        if isinstance(e, aiohttp.ClientPayloadError):
            error_msg = "Сетевой сбой: Некорректный ответ Сервера."
            raise NetworkError(error_msg, delay_seconds=30) from e
        
        if isinstance(e, aiohttp.ClientResponseError) and e.status in [401, 403]:
            raise NetworkError(f"Доступ запрещен (код {e.status}): {e.reason}", delay_seconds=30) from e
        
        if 'api key not valid' in error_text or 'permission denied' in error_text:
            raise RateLimitExceededError(f"Невалидный/заблокированный API ключ {self.worker.api_key[-4:]}: {str(e)}") from e

        if "вам включили лимиты" in error_text or "quota_exceeded" in error_text:
            raise RateLimitExceededError(str(e))
        
        if "user location is not supported" in error_text:
            raise LocationBlockedError(f"Геоблок: {str(e)}") from e
            
        if "model" in error_text and ("not found" in error_text or "is not supported" in error_text):
            raise ModelNotFoundError(f"Модель не найдена: {str(e)}") from e
        
        if isinstance(e, (aiohttp.ClientError, asyncio.TimeoutError, RequestsError, OSError) + PROXY_ERRORS):
            error_msg = f"Сетевой сбой ({type(e).__name__}): {str(e)}"
            raise NetworkError(error_msg, delay_seconds=30) from e

        raise e
        
    def _detect_looping(self, text):
        """
        [Диагностика 2.0] Статистический анализ зацикливания.
        Проверяет периодичность повторов и кластеризацию повторяющихся блоков.
        """
        if not text: return False
        
        # 1. Парсинг и очистка
        raw_blocks = re.split(r'</p>|\n\s*\n', text)
        blocks = []
        for b in raw_blocks:
            clean = re.sub(r'<[^>]+>', '', b).strip()
            if clean: blocks.append(clean)
            
        if len(blocks) < 4: return False

        # 2. Картирование: Текст -> Список индексов вхождений
        index_map = {}
        for idx, block in enumerate(blocks):
            if block not in index_map: index_map[block] = []
            index_map[block].append(idx)

        # 3. Критерий А: Одиночный периодический цикл (Oscillation)
        # "Если один абзац имеет вхождения > 3 и дистанция повторяется в 70% случаев"
        for block, indices in index_map.items():
            count = len(indices)
            if count <= 1: continue
            
            # Для коротких фраз повышаем порог, чтобы не ловить "Он кивнул."
            is_short = len(block) < 30
            required_count = 5 if is_short else 4
            
            if count >= required_count:
                # Вычисляем дистанции (шаги) между вхождениями: [2, 5, 8] -> [3, 3]
                diffs = [indices[i+1] - indices[i] for i in range(len(indices)-1)]
                
                if not diffs: continue
                
                # Анализ частоты дистанций
                dist_counts = Counter(diffs)
                most_common_dist, freq = dist_counts.most_common(1)[0]
                
                # Если одна и та же дистанция встречается в >= 70% случаев -> это ритмичный цикл
                if freq / len(diffs) >= 0.70:
                    return True

        # 4. Критерий Б: Сценарная петля (Cluster Loop)
        # "Если три абзаца рядом имеют множественные вхождения"
        consecutive_repeats = 0
        
        for idx, block in enumerate(blocks):
            # Проверяем, встречается ли этот блок где-то еще в тексте
            if len(index_map[block]) > 1:
                consecutive_repeats += 1
            else:
                consecutive_repeats = 0
            
            # Если нашли цепочку из 3 подряд идущих блоков, которые есть где-то еще
            if consecutive_repeats >= 3:
                # Доп. проверка: суммарная длина должна быть значимой (исключаем диалог "Да/Нет/Да")
                b1 = blocks[idx]
                b2 = blocks[idx-1]
                b3 = blocks[idx-2]
                if (len(b1) + len(b2) + len(b3)) > 60:
                     return True

        return False
        
        
from abc import ABC, abstractmethod


class BaseServer(ABC):
    """
    Абстрактный базовый класс для локальных серверов (стратегия).
    Определяет интерфейс управления жизненным циклом и валидации.
    """
    def __init__(self, port=None):
        self.port = port
        self._is_running = False

    @abstractmethod
    def start(self, anonymous=True):
        """Запускает сервер в отдельном потоке."""
        pass

    @abstractmethod
    def stop(self):
        """Останавливает сервер."""
        pass

    @abstractmethod
    def is_running(self) -> bool:
        """Возвращает True, если сервер активен и отвечает."""
        pass

    @abstractmethod
    def get_url(self) -> str | None:
        """Возвращает базовый URL запущенного сервера (например http://127.0.0.1:PORT)."""
        pass
    
    @abstractmethod
    def validate_token(self, token: str) -> dict:
        """
        Проверяет один токен.
        Return: {'valid': bool, 'message': str, 'email': str, 'is_pro': bool}
        """
        pass

    @abstractmethod
    def validate_batch(self, tokens: list) -> list:
        """
        Проверяет список токенов.
        Return: list[dict] (результаты validate_token + поле 'token')
        """
        pass