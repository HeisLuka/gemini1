# -*- coding: utf-8 -*-

from .base_processor import BaseTaskProcessor
from gemini_translator.api.errors import SuccessSignal, ValidationFailedError




class HelloTaskProcessor(BaseTaskProcessor):
    async def execute(self, task_info, use_stream=False):
        """Простая проверка доступности API."""
        if self.worker.api_provider_name == "dry_run":
            raise SuccessSignal(status_code="SUCCESS", message="Dry run 'hello' successful.")

        log_prefix = f"Приветствие (ключ …{self.worker.api_key[-4:]})"
        user_prompt = "Ответь одним словом: 'OK'"
        self.worker.prompt_builder.system_instruction = None

        response = await self.worker.api_handler_instance.execute_api_call(user_prompt, log_prefix, use_stream=use_stream, allow_incomplete=True)
        
        if response and "ok" in response.lower():
            raise SuccessSignal(status_code="SUCCESS", message="API 'hello' successful.")
        else:
            raise ValidationFailedError(f"Неожиданный ответ на 'hello': {response[:50]}")