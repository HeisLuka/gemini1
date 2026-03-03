import aiohttp
import asyncio
import json
import traceback
import re
from PyQt6 import QtCore, QtWidgets
from ..base import BaseApiHandler
from ..errors import (
    ContentFilterError, NetworkError, LocationBlockedError, 
    RateLimitExceededError, ModelNotFoundError, ValidationFailedError, 
    TemporaryRateLimitError
)



class DryRunApiHandler(BaseApiHandler):
    """
    Псевдо-API хендлер. Взаимодействует с GUI, поэтому требует особого
    пути выполнения.
    """
    def setup_client(self, client_override=None, proxy_settings=None):
        self.worker.api_key = client_override.api_key
        self.worker.model_id = self.worker.model_config.get("id", "dry-run-model")
        # Прокси в DryRun не нужны, но сигнатура должна совпадать
        return True

    async def execute_api_call(self, prompt, log_prefix, allow_incomplete=False, debug=False, use_stream=True, max_output_tokens=None):
        """
        Переопределяем основной метод, чтобы избежать его запуска в фоновом
        потоке.
        """
        # --- ШАГ 1: Импортируем наш новый диалог ---
        from ...ui.dialogs.setup_dialogs.dry_run_dialog import DryRunPromptDialog

        # --- ШАГ 2: Собираем полный текст промпта ---
        system_instruction = getattr(self.worker.prompt_builder, 'system_instruction', None)
        
        final_output = []
        if system_instruction:
            final_output.append("════════════════════════════════════════════════════")
            final_output.append("          SYSTEM INSTRUCTION (СИСТЕМНАЯ ИНСТРУКЦИЯ)          ")
            final_output.append("════════════════════════════════════════════════════")
            final_output.append(system_instruction.strip())
            final_output.append("\n" * 2)
            final_output.append("════════════════════════════════════════════════════")
            final_output.append("               USER PROMPT (ПРОМПТ ПОЛЬЗОВАТЕЛЯ)               ")
            final_output.append("════════════════════════════════════════════════════")
        
        final_output.append(prompt.strip())
        full_prompt_text = "\n".join(final_output)
        
        # --- ШАГ 3: Вызываем статический метод нашего диалога ---
        self.worker._post_event('log_message', {'message': "[INFO] Ожидание ручного ввода ответа для пробного запуска…"})
        
        app = QtWidgets.QApplication.instance()
        main_window = next((w for w in app.topLevelWidgets() if isinstance(w, QtWidgets.QMainWindow)), None)
        
        user_translation = DryRunPromptDialog.get_translation(main_window, full_prompt_text)

        # --- ШАГ 4: Анализируем результат ---
        if user_translation is not None:
            self.worker._post_event('log_message', {'message': "[INFO] Получен ручной перевод. Обработка как ответа API…"})
            return user_translation
        else:
            self.worker._post_event('log_message', {'message': "[INFO] Ручной ввод отменен. Симуляция ошибки 'Модель не найдена' для остановки сессии."})
            raise ModelNotFoundError("Пробный запуск отменен пользователем.")

