# Импорт перечислений для классификации ошибок и команд
from gemini_translator.api.errors import ErrorType, WorkerAction

# Импорт конкретных классов исключений для их анализа
from gemini_translator.api.errors import (
    ContentFilterError,
    LocationBlockedError,
    ModelNotFoundError,
    RateLimitExceededError,
    TemporaryRateLimitError,
    NetworkError,
    OperationCancelledError,
    ValidationFailedError,
    PartialGenerationError
)


class ErrorAnalyzer:
    
    # --- Конфигурация правил отказов ---
    FAILURE_RULES = {
        ErrorType.PARTIAL_GENERATION:   {'max_attempts': 3, 'allows_chunking': False},
        ErrorType.VALIDATION:           {'max_attempts': 3, 'allows_chunking': True},
        ErrorType.NETWORK:              {'max_attempts': 2, 'allows_chunking': False},
        ErrorType.CONTENT_FILTER:       {'max_attempts': 2, 'allows_chunking': False},
        ErrorType.API_ERROR:            {'max_attempts': 2, 'allows_chunking': True},
        ErrorType.CANCEL:               {'max_attempts': 0, 'allows_chunking': False},
    }
    # Общий лимит РАЗНЫХ типов ошибок
    TOTAL_ATTEMPTS_LIMIT = 4

    
    def __init__(self, worker_instance):
        self.worker = worker_instance
        self.task_manager = self.worker.task_manager
        self.network_warnings = 0
        
    def analyze_and_act(self, exc, task_info: tuple, task_history: dict):
        task_id, task_payload = task_info
        task_name = self.task_manager._get_task_display_name(task_payload)

        # --- Шаг 1: Определяем исходный тип ошибки и тип для записи в историю ---
        error_for_rules = self._classify_exception(exc)
        error_for_history = error_for_rules
        # --- Шаг 2: "Умная" переклассификация и эскалация ---
        if error_for_rules == ErrorType.PARTIAL_GENERATION:
            partial_text = getattr(exc, 'partial_text', '')
            reason = getattr(exc, 'reason', 'OTHER').upper()
            is_first_attempt = (task_payload[0] == 'epub') or (task_payload[0] == 'epub_chunk' and len(task_payload) <= 8)

            if not partial_text.strip() and is_first_attempt:
                # ЭСКАЛАЦИЯ: Пустой хвост на первой попытке. Перезаписываем ошибку для правил.
                new_error = ErrorType.CONTENT_FILTER if reason in ["SAFETY", "PROHIBITED_CONTENT"] else ErrorType.API_ERROR
                error_for_rules = new_error
                error_for_history = new_error
            elif reason in ["SAFETY", "PROHIBITED_CONTENT"]:
                # ПЕРЕКЛАССИФИКАЦИЯ ДЛЯ ИСТОРИИ: Хвост есть, но причина - фильтр.
                error_for_history = ErrorType.CONTENT_FILTER
        
        # --- Шаг 3: Получаем правила для error_for_rules ---
        rule = self.FAILURE_RULES.get(error_for_rules, {'max_attempts': 1})
        max_attempts = rule['max_attempts']

        # --- Шаг 4: Обработка не-счетных и фатальных ошибок (используем error_for_rules) ---
        if error_for_rules in [ErrorType.GEOBLOCK, ErrorType.QUOTA_EXCEEDED, ErrorType.MODEL_NOT_FOUND]:
            payload = {"type": error_for_rules.name.lower(), "model_id": self.worker.model_id, "exception": exc}
            self.worker._post_event('fatal_error', {'payload': payload})
            return WorkerAction.ABORT_WORKER, error_for_rules, exc

        if error_for_rules == ErrorType.TEMPORARY_LIMIT:
            delay = getattr(exc, 'delay_seconds', 61)
            current_rpm = self.worker.rpm_limiter.get_rpm()
            self.worker.rpm_limiter.decrease_rpm(percentage=25)
            new_rpm = self.worker.rpm_limiter.get_rpm()
            self.worker._post_event('temporary_limit_warning_received', {'delay_seconds': delay, 'original_exception': exc, "model_id": self.worker.model_id})
            self.worker.rpm_limiter.update_last_request_time(delay)
            log_message = (f"🟡 API запросил паузу для ключа …{self.worker.worker_id[-4:]} на {delay} секунд.")
            if current_rpm > new_rpm:
                log_message += (f"\n    ➡️ Действие: RPM автоматически снижен с {current_rpm} до {new_rpm}.")
            self.worker._post_event('log_message', {'message': log_message})
            return WorkerAction.RETRY_NON_COUNTABLE, error_for_rules, exc
        
        if error_for_rules == ErrorType.NETWORK:
            self.network_warnings = self.network_warnings + 1
            delay = getattr(exc, 'delay_seconds', 30) * (self.network_warnings)
            # Подкручиваем таймер: следующий запрос будет разрешен только через delay секунд.
            # RPM лимит при этом НЕ снижаем.
            self.worker.rpm_limiter.update_last_request_time(delay)
            self.worker._post_event('temporary_limit_warning_received', {'delay_seconds': delay, 'original_exception': exc, "model_id": self.worker.model_id})
            self.worker._post_event('log_message', {'message': f"🌐 Сетевой сбой. Пауза {delay} сек. перед повтором..."})
        
        if error_for_rules in [ErrorType.VALIDATION, ErrorType.CONTENT_FILTER, ErrorType.PARTIAL_GENERATION]:
            self.network_warnings = 0
            self.worker._post_event('api_connection_healthy')

        if error_for_rules == ErrorType.CANCEL:
            self._record_and_log_failure(task_info, error_for_history)
            return WorkerAction.RETRY_NON_COUNTABLE, error_for_rules, exc

        # --- Шаг 5: Принятие решения на основе СЧЕТНЫХ ошибок ---
        # Считаем попытки по error_for_rules.
        # Если error_for_history отличается, СУММИРУЕМ их счетчики,
        # чтобы учесть оба случая в общем лимите для текущего типа ошибки.
        current_type_count = task_history.get('errors', {}).get(error_for_rules.name, 0)
        if error_for_rules.name != error_for_history.name:
            current_type_count += task_history.get('errors', {}).get(error_for_history.name, 0)
            
        total_attempts_count = task_history.get('total_count', 0)

        if (current_type_count + 1 >= max_attempts) or \
           (total_attempts_count + 1 >= self.TOTAL_ATTEMPTS_LIMIT):
            
            if total_attempts_count + 1 >= self.TOTAL_ATTEMPTS_LIMIT:
                self.worker._post_event('log_message', {'message': f"[ANALYZER] Превышен общий лимит ({self.TOTAL_ATTEMPTS_LIMIT}) попыток для задачи '{task_name}'."})
            
            # Записываем в историю финальный, самый точный тип ошибки
            self._record_and_log_failure(task_info, error_for_history)
            return self._decide_final_action(task_name, task_payload, task_history, exc, error_for_history)
        
        # --- Шаг 6: Если все лимиты в норме, даем команду на повтор ---
        self._record_and_log_failure(task_info, error_for_history)
        return WorkerAction.RETRY_COUNTABLE, error_for_history, exc

    def _record_and_log_failure(self, task_info: tuple, error_type: ErrorType):
        """Атомарно записывает ошибку в БД и выводит в лог красивое сообщение."""
        if error_type == ErrorType.CANCEL or error_type == ErrorType.GEOBLOCK or error_type == ErrorType.MODEL_NOT_FOUND:
            return
        # 1. Записываем в БД
        self.task_manager.record_failure(task_info, error_type.name)
        
        # 2. Готовим красивый лог
        task_name = self.task_manager._get_task_display_name(task_info[1])
        history = self.task_manager.get_failure_history(task_info)
        total_count = history.get('total_count', 0)

        log_message = ""
        if error_type == ErrorType.CONTENT_FILTER:
            log_message = f"🛡️ ФИЛЬТР: Зарегистрирована блокировка контента для '{task_name}' (всего: {total_count})."
        elif error_type == ErrorType.VALIDATION:
            log_message = f"📋 ВАЛИДАЦИЯ: Зарегистрирована ошибка структуры ответа для '{task_name}' (всего: {total_count})."
        else:
            # Стандартное сообщение для всех остальных ошибок
            log_message = f"[TASK] Зарегистрирована ошибка для '{task_name}' (тип: {error_type.name}, всего: {total_count})."
        
        self.worker._post_event('log_message', {'message': log_message})

    def _decide_final_action(self, task_name, task_payload: tuple, task_history: dict, last_exc, final_error_type: ErrorType):
        """
        ФИНАЛЬНАЯ ВЕРСИЯ. Правильно определяет чанки и запрещает для них "План Б".
        Безопасно получает chunk_on_error.
        Версия 11.0: Расформировывает проваленные пакеты.
        """
        task_type = task_payload[0]
    
        chunk_on_error_enabled = getattr(self.worker, 'chunk_on_error', False)
        
        is_chunkable_task_type = (task_type == 'epub')
        was_force_chunked = task_history.get('force_chunked', False)
    
        if not (is_chunkable_task_type and chunk_on_error_enabled) or was_force_chunked:
            return self._log_and_fail_permanently(task_name, final_error_type, last_exc)
    
        error_names_in_history = task_history.get('errors', {}).keys()
        
        can_try_chunking = False
        if error_names_in_history:
            for error_name in error_names_in_history:
                try:
                    rule = self.FAILURE_RULES.get(ErrorType[error_name])
                    if rule and rule.get('allows_chunking', False):
                        can_try_chunking = True
                        break # Достаточно одного разрешения
                except (KeyError, AttributeError): pass
        
        if can_try_chunking:
            log_message = (
                f"❗️ПРОВАЛ ПОПЫТОК для '{task_name}'.\n"
                f"    Последняя ошибка: ({self._classify_exception(last_exc).name}): {str(last_exc)}\n"
                f"    ➡️ Действие: Запуск ПЛАНА Б (принудительное разделение на части)."
            )
            self.worker._post_event('log_message', {'message': log_message})
            return WorkerAction.FAIL_AND_ATTEMPT_CHUNK, self._classify_exception(last_exc), last_exc
        else:
            return self._log_and_fail_permanently(task_name, final_error_type, last_exc)

    def _log_and_fail_permanently(self, task_name, error_type, exc):
        """Вспомогательная функция для логирования и возврата окончательного провала."""
        # --- Лаконичные сообщения для конкретных ошибок ---

        if error_type == ErrorType.CONTENT_FILTER:
            log_message = f"🛡️ ФИЛЬТР: Задача '{task_name}' окончательно заблокирована политикой безопасности."
        elif error_type == ErrorType.VALIDATION:
            log_message = f"📋 ВАЛИДАЦИЯ: Задача '{task_name}' провалена из-за структурных ошибок в ответе API: {str(exc)}"
        else:
            # Стандартное подробное сообщение для всех остальных ошибок
            log_message = (
                f"❌ ОКОНЧАТЕЛЬНЫЙ ПРОВАЛ ЗАДАЧИ: '{task_name}'\n"
                f"    Последняя ошибка ({error_type.name}): {str(exc)}"
            )
        if error_type == ErrorType.CANCEL or error_type == ErrorType.GEOBLOCK or error_type == ErrorType.MODEL_NOT_FOUND:
            return WorkerAction.RETRY_NON_COUNTABLE, error_type, exc


        self.worker._post_event('log_message', {'message': log_message})
        return WorkerAction.FAIL_PERMANENTLY, error_type, exc

    def _classify_exception(self, exc) -> ErrorType:
        if isinstance(exc, NetworkError):
            return ErrorType.NETWORK
        if isinstance(exc, ContentFilterError):
            return ErrorType.CONTENT_FILTER
        if isinstance(exc, LocationBlockedError):
            return ErrorType.GEOBLOCK
        if isinstance(exc, RateLimitExceededError):
            return ErrorType.QUOTA_EXCEEDED
        if isinstance(exc, PartialGenerationError):
            return ErrorType.PARTIAL_GENERATION
        if isinstance(exc, TemporaryRateLimitError):
            return ErrorType.TEMPORARY_LIMIT
        if isinstance(exc, ModelNotFoundError):
            return ErrorType.MODEL_NOT_FOUND
        if isinstance(exc, ValidationFailedError):
            return ErrorType.VALIDATION
        if isinstance(exc, OperationCancelledError):
            return ErrorType.CANCEL

        # ----------------------------------------------

        return ErrorType.API_ERROR
