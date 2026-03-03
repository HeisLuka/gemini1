# -*- coding: utf-8 -*-

import os
import zipfile

from .base_processor import BaseTaskProcessor
from gemini_translator.api.errors import ValidationFailedError, PartialGenerationError
from gemini_translator.utils.text import (
    process_body_tag, is_content_effectively_empty, clean_html_content, validate_html_structure
)

class EpubSingleFileProcessor(BaseTaskProcessor):
    async def execute(self, task_info, use_stream=True):
        """Самодостаточно обрабатывает ОДНУ задачу типа 'epub'."""
        task_id, task_payload = task_info
        
        try:
            epub_path = task_payload[1]
            internal_chapter_path = task_payload[2]
        except IndexError:
            raise ValueError(f"Некорректный формат задачи epub: {task_payload}")
        
        log_prefix = f"{os.path.basename(internal_chapter_path)} (ключ …{self.worker.api_key[-4:]})"

        if self.worker.is_cancelled:
            return task_info, False, 'CANCELLED', "Отменено пользователем"

        with zipfile.ZipFile(open(epub_path, 'rb'), "r") as zf:
            original_content = zf.read(internal_chapter_path).decode("utf-8", "ignore")
        
        prefix_html, body_content, html_suffix = process_body_tag(original_content, return_parts=True, body_content_only=False)
        
        version_suffix = self.worker.provider_config['file_suffix']
        internal_dir = os.path.dirname(internal_chapter_path)
        chapter_basename = os.path.splitext(os.path.basename(internal_chapter_path))[0]
        new_filename = f"{chapter_basename}{version_suffix}"
        destination_dir = os.path.join(self.worker.output_folder, internal_dir)
        os.makedirs(destination_dir, exist_ok=True)
        out_path = os.path.join(destination_dir, new_filename)

        if not body_content.strip():
            self._copy_original_as_result(out_path, original_content, internal_chapter_path, version_suffix)
            return task_info, True, 'SUCCESS', "Файл пуст или с пустым <body>, скопирован."

        segmented_text = self.worker.context_manager.prepare_html_for_translation(body_content)
        content_with_placeholders = self.worker.prompt_builder._replace_media_with_placeholders(segmented_text)

        if is_content_effectively_empty(content_with_placeholders):
            self._copy_original_as_result(out_path, original_content, internal_chapter_path, version_suffix)
            return task_info, True, 'SUCCESS', "Пропущено (нет текста, только медиа/теги), оригинал скопирован."

        user_prompt, _, _ = self.worker.prompt_builder.prepare_for_api(
            body_content, 
            self.worker.system_instruction,
            current_chapters_list=[internal_chapter_path]
        )
        
        original_was_a_body = (
            body_content.strip().lower().startswith('<body') and 
            body_content.strip().lower().endswith('</body>')
        )

        raw_response = ""
        try:
            raw_response = await self.worker.api_handler_instance.execute_api_call(user_prompt, log_prefix, use_stream=use_stream)
        except PartialGenerationError as e:
            if e.partial_text:
                e.partial_text = clean_html_content(e.partial_text, is_html=False)
            raise e

        cleaned_response = clean_html_content(raw_response, is_html=original_was_a_body)
        
        original_body_with_placeholders = self.worker.prompt_builder._replace_media_with_placeholders(body_content)

        if not getattr(self.worker, "force_accept", False):
            is_valid, reason, validated_html = validate_html_structure(original_body_with_placeholders, cleaned_response)
            if not is_valid: 
                raise ValidationFailedError(f"Ответ не прошел валидацию: {reason}")
            cleaned_response = validated_html
        
        restored_body = self.worker.response_parser._restore_media_from_placeholders(
            translated_content=cleaned_response,
            original_content_for_map_building=original_content
        )

        if not restored_body or not restored_body.strip():
            raise ValidationFailedError("API вернуло пустой ответ после очистки и восстановления.")
        
        self.worker.response_parser.process_and_save_single_file(
            translated_body_content=restored_body,
            original_full_content=original_content,
            prefix_html=prefix_html,
            suffix_html=html_suffix,
            output_path=out_path,
            original_internal_path=internal_chapter_path,
            version_suffix=version_suffix
        )
        
        return task_info, True, 'SUCCESS', ""
    
    def _copy_original_as_result(self, out_path, content, internal_path, suffix):
        """Копирует оригинал на диск и регистрирует его в проекте."""
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(content)
        if self.project_manager:
            relative_path = os.path.relpath(out_path, self.project_manager.project_folder)
            self.project_manager.register_translation(
                original_internal_path=internal_path,
                version_suffix=suffix,
                translated_relative_path=relative_path
            )

