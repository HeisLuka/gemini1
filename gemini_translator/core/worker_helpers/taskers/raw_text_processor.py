# -*- coding: utf-8 -*-

from .base_processor import BaseTaskProcessor
from gemini_translator.api.errors import ValidationFailedError, PartialGenerationError
from gemini_translator.utils.text import clean_html_content, safe_format


class RawTextProcessor(BaseTaskProcessor):
    async def execute(self, task_info, use_stream=True):
        """Обрабатывает задачу по прямому переводу текста."""
        task_id, task_payload = task_info
        
        try:
            text_content_to_translate = task_payload[1]
            custom_prompt_str = task_payload[2]
        except IndexError:
            raise ValueError(f"Некорректный формат задачи raw_text_translation: {task_payload}")

        log_prefix = f"Прямой перевод (ключ …{self.worker.api_key[-4:]})"
        
        if custom_prompt_str:
            user_prompt = safe_format(custom_prompt_str, text=text_content_to_translate)
            self.worker.prompt_builder.system_instruction = None
        else:
            user_prompt, _, _ = self.worker.prompt_builder.prepare_for_api(
                text_content_to_translate, 
                self.worker.system_instruction,
                current_chapters_list=None
            )

        cleaned_response = ""
        try:
            raw_translated_text = await self.worker.api_handler_instance.execute_api_call(user_prompt, log_prefix, use_stream=use_stream, allow_incomplete=True)
            cleaned_response = clean_html_content(raw_translated_text, is_html=True)
        except PartialGenerationError as e:
            if e.partial_text:
                temp_cleaned = clean_html_content(e.partial_text, is_html=False)
                last_closing_tag_pos = temp_cleaned.rfind('</p>')
                if last_closing_tag_pos != -1:
                    cleaned_response = temp_cleaned[:last_closing_tag_pos + 4]
                    self.worker._post_event('log_message', {'message': f"✂️ [RAW] Ответ обрезан по </p>. (Причина: {e.reason})"})
                else:
                    raise ValidationFailedError(f"API вернуло обрывок без </p> ({e.reason}).")
            else:
                raise ValidationFailedError(f"API вернуло пустой обрывок ({e.reason}).")

        if not cleaned_response:
            raise ValidationFailedError("API вернуло пустой ответ после очистки.")

        result_payload = (task_info, cleaned_response)
        return result_payload, True, 'SUCCESS', "Прямой перевод успешно завершен."

