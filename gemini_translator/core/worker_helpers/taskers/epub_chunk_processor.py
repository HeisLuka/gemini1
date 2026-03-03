# -*- coding: utf-8 -*-

from .base_processor import BaseTaskProcessor
from gemini_translator.api.errors import ValidationFailedError, PartialGenerationError
from gemini_translator.utils.text import is_content_effectively_empty, clean_html_content, validate_html_structure
import os


class EpubChunkProcessor(BaseTaskProcessor):
    async def execute(self, task_info, use_stream=True):
        task_id, task_payload = task_info
        
        is_retry = len(task_payload) > 8
        base_payload = task_payload[:-1] if is_retry else task_payload
        partial_translation = task_payload[-1] if is_retry else None
        
        _, epub_path, chapter_path, chunk_content, chunk_index, total_chunks, prefix, suffix = base_payload
        log_prefix = f"{os.path.basename(chapter_path)} [{chunk_index + 1}/{total_chunks}]" + (" [Попытка 2+]" if is_retry else "")

        content_to_translate_for_api = chunk_content
        if not chunk_content.lower().strip().startswith('<body'):
            content_to_translate_for_api = "<body>" + content_to_translate_for_api
        if not chunk_content.lower().strip().endswith('</body>'):
            content_to_translate_for_api = content_to_translate_for_api + "</body>"

        segmented_text = self.worker.context_manager.prepare_html_for_translation(content_to_translate_for_api)
        content_with_placeholders = self.worker.prompt_builder._replace_media_with_placeholders(segmented_text)
        
        if is_content_effectively_empty(content_with_placeholders):
            final_restored_html = content_to_translate_for_api 
            result_payload = ((task_id, tuple(base_payload)), final_restored_html)
            return result_payload, True, 'SUCCESS', "Пропущено (нет текста, только медиа/теги)"
        
        completion_data = None
        if partial_translation:
            clean_partial_for_prompt = clean_html_content(partial_translation, is_html=False)
            completion_data = {
                'original_content': content_to_translate_for_api, 
                'partial_translation': clean_partial_for_prompt
            }

        user_prompt, _, _ = self.worker.prompt_builder.prepare_for_api(
            text_content=content_to_translate_for_api,
            system_instruction_text=self.worker.system_instruction,
            completion_data=completion_data,
            current_chapters_list=[chapter_path]
        )
        
        newly_generated_part_raw = ""
        try:
            newly_generated_part_raw = await self.worker.api_handler_instance.execute_api_call(user_prompt, log_prefix, use_stream=use_stream)
        except PartialGenerationError as e:
            if e.partial_text:
                e.partial_text = clean_html_content(e.partial_text, is_html=False)
            raise e

        cleaned_new_part_from_markdown = clean_html_content(newly_generated_part_raw, is_html=False)
        
        cleaned_partial_from_markdown = ""
        if partial_translation:
            cleaned_partial_from_markdown = clean_html_content(partial_translation, is_html=False)
            
        accumulated_raw_html = cleaned_partial_from_markdown + cleaned_new_part_from_markdown
        accumulated_text = clean_html_content(accumulated_raw_html, is_html=True)
        
        original_chunk_with_placeholders = self.worker.prompt_builder._replace_media_with_placeholders(content_to_translate_for_api)

        if not getattr(self.worker, "force_accept", False):
            is_valid, reason, validated_chunk = validate_html_structure(original_chunk_with_placeholders, accumulated_text)
            if not is_valid: 
                raise ValidationFailedError(f"Финальный текст не прошел валидацию: {reason}")
            accumulated_text = validated_chunk

        final_restored_html = self.worker.response_parser._restore_media_from_placeholders(
            translated_content=accumulated_text,
            original_content_for_map_building=content_to_translate_for_api
        )
        
        base_payload = task_payload[:-1] if len(task_payload) > 8 else task_payload
        result_payload = ((task_id, tuple(base_payload)), final_restored_html)
        return result_payload, True, 'SUCCESS', "Успешно"

