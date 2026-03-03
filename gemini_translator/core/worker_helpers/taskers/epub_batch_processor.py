# -*- coding: utf-8 -*-

import os
from collections import Counter

from .base_processor import BaseTaskProcessor
from gemini_translator.api.errors import SuccessSignal, ValidationFailedError, PartialGenerationError
from gemini_translator.utils.text import clean_html_content, prettify_html

class EpubBatchProcessor(BaseTaskProcessor):
    async def execute(self, task_info, use_stream=False):
        """Обрабатывает пакет. Версия 10.0: Разведка и принятие решений."""
        task_id, task_payload = task_info
        
        try:
            epub_path = task_payload[1]
            chapter_list = task_payload[2]
        except IndexError:
            raise ValueError(f"Некорректный формат задачи epub_batch: {task_payload}")
        
        user_prompt, _, _, original_contents = self.worker.prompt_builder.prepare_batch_for_api(epub_path, chapter_list, self.worker.system_instruction)
        raw_response = ""
        finish_reason_exc = None
        allow_incomplete = use_stream
        try:
            raw_response = await self.worker.api_handler_instance.execute_api_call(
                user_prompt, 
                f"Пакет из {len(chapter_list)} глав", 
                use_stream=use_stream, 
                allow_incomplete=allow_incomplete
            )
        except PartialGenerationError as e:
            raw_response = e.partial_text
            finish_reason_exc = e
            self.worker._post_event('log_message', {'message': f"📦 [BATCH WARN] Ответ частичный (Причина: {e.reason}). Пытаюсь извлечь готовые главы..."})

        cleaned_response = clean_html_content(raw_response, is_html=False)
        report = self.worker.response_parser.unpack_and_validate_batch(cleaned_response, chapter_list, original_contents)
        
        successful_chapters_data = report.get('successful', [])
        successful_chapters_paths = [] 
        failed_chapters_details = report.get('failed', [])
        failed_chapters_paths = [x[0] for x in failed_chapters_details]

        file_suffix = self.worker.provider_config['file_suffix']
        registrations_to_make = []
        for success_data in successful_chapters_data:
            try:
                original_path = success_data['original_path']
                final_html = success_data['final_html']
                chapter_basename = os.path.splitext(os.path.basename(original_path))[0]
                new_filename = f"{chapter_basename}{file_suffix}"
                destination_dir = os.path.join(self.worker.output_folder, os.path.dirname(original_path))
                os.makedirs(destination_dir, exist_ok=True)
                out_path = os.path.join(destination_dir, new_filename)
                use_prettify = getattr(self.worker, "use_prettify", False)
                if use_prettify:
                    final_html = prettify_html(final_html)
                
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(final_html)
                    
                relative_path = os.path.relpath(out_path, self.worker.output_folder)
                registrations_to_make.append((original_path, file_suffix, relative_path))
                successful_chapters_paths.append(original_path)
            except Exception as e:
                self.worker._post_event('log_message', {'message': f"🔻 [BATCH CRITICAL] Ошибка сохранения '{success_data.get('original_path')}': {e}"})
                if success_data.get('original_path') not in failed_chapters_paths:
                    failed_chapters_paths.append(success_data.get('original_path'))
        
        if self.worker.project_manager and registrations_to_make:
            try:
                self.worker.project_manager.register_multiple_translations(registrations_to_make)
            except Exception as e:
                self.worker._post_event('log_message', {'message': f"🔻 [BATCH CRITICAL] Ошибка пакетной регистрации в карте проекта: {e}"})
        
        self.worker.task_manager.replace_batch_with_results(
            original_batch_task_id=str(task_id),
            epub_path=epub_path,
            successful_chapters=successful_chapters_paths,
            failed_chapters=failed_chapters_paths
        )

        total_count = len(chapter_list)
        failed_count = len(failed_chapters_paths)
        success_count = len(successful_chapters_paths)
        
        if failed_count == 0:
            self.worker._post_event('log_message', {'message': f"📦 [BATCH DONE] Пакет ({total_count} глав) полностью выполнен."})
            raise SuccessSignal("Пакет завершен.")

        reasons = [x[1] for x in failed_chapters_details]
        reason_counts = Counter(reasons)
        short_summary_parts = []
        for reason, count in reason_counts.items():
            short_reason = reason.split(':')[0]
            short_summary_parts.append(f"{count}x {short_reason}")
        short_summary_str = ", ".join(short_summary_parts)
        
        log_header = f"📦 [BATCH PARTIAL] {success_count} ok, {failed_count} fail." if success_count > 0 else f"📦 [BATCH FAILED] {failed_count} fail."
        self.worker._post_event('log_message', {'message': f"{log_header} Сводка: {short_summary_str}"})

        detailed_errors = [f"[{os.path.basename(path)}]: {reason}" for path, reason in failed_chapters_details]
        full_error_details = "; ".join(detailed_errors)
        
        if finish_reason_exc:
            original_message = str(finish_reason_exc)
            enriched_message = f"{original_message}. Детали провала глав: {full_error_details}"
            if isinstance(finish_reason_exc, PartialGenerationError):
                raise ValidationFailedError(enriched_message)
            else:
                raise type(finish_reason_exc)(enriched_message)
        else:
            exception_msg = f"Провал {failed_count}/{total_count} глав. Детали: {full_error_details}"
            raise ValidationFailedError(exception_msg)

