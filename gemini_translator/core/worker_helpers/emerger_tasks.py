from gemini_translator.api.errors import PartialGenerationError
import zipfile
from collections import Counter
from gemini_translator.utils.text import brute_force_split

class EmergencyTask:
    
    def __init__(self, worker):
        self.worker = worker
    
    def _mutate_task_for_completion(self, task_info: tuple, exc):
        """
        Проверяет, является ли ошибка PartialGenerationError с непустым хвостом.
        Если да - мутирует payload задачи для догенерации.
        В противном случае - возвращает исходный task_info.
        """
        if not isinstance(exc, PartialGenerationError) or not getattr(exc, 'partial_text', ''):
            return task_info

        task_id, task_payload = task_info
        untrimmed_partial_text = exc.partial_text
        
        # --- УМНАЯ ОБРЕЗКА ХВОСТА ---
        split_markers = ["</p>", "</div>", "</h1>", "</h2>", "</h3>", "</h4>", "</h5>", "</h6>", "</li>", "</blockquote>", "<br>", "\n"]
        best_split_pos = -1
        for marker in split_markers:
            pos = untrimmed_partial_text.rfind(marker)
            if pos > best_split_pos:
                best_split_pos = pos + len(marker)
        
        partial_text = untrimmed_partial_text[:best_split_pos].rstrip() if best_split_pos != -1 else untrimmed_partial_text
        if partial_text != untrimmed_partial_text and list(task_payload)[0] == 'epub':
            self.worker._post_event('log_message', {'message': "[INFO] Ответ AI оборван. 'Хвост' обрезан до последнего разделителя для чистого доперевода."})

        # --- МУТАЦИЯ PAYLOAD ---
        base_payload_list = list(task_payload)
        
        if base_payload_list[0] == 'epub':
            _, epub_path, chapter_path = base_payload_list
            try:
                # zipfile.ZipFile работает прозрачно благодаря os_patch, даже если epub_path в RAM.
                with open(epub_path, 'rb') as f:
                    with zipfile.ZipFile(f, "r") as zf:
                        original_content = zf.read(chapter_path).decode("utf-8", "ignore")
                
                prefix, body_content, suffix = "", original_content, ""
                content_lower = original_content.lower()
                start_body_tag_pos, end_body_tag_pos = content_lower.find('<body'), content_lower.rfind('</body>')
                
                if start_body_tag_pos != -1 and end_body_tag_pos != -1:
                    start_body_content_pos = content_lower.find('>', start_body_tag_pos) + 1
                    prefix, body_content, suffix = original_content[:start_body_content_pos], original_content[start_body_content_pos:end_body_tag_pos], original_content[end_body_tag_pos:]
                
                base_payload_list = ['epub_chunk', epub_path, chapter_path, body_content, 0, 1, prefix, suffix]
                self.worker._post_event('log_message', {'message': f"[INFO] Задача 'epub' преобразована в 'epub_chunk' для доперевода."})
            except Exception as e:
                # В случае ошибки возвращаем исходную задачу, она провалится на следующем этапе
                return task_info

        elif base_payload_list[0] == 'epub_chunk' and len(base_payload_list) > 8:
            base_payload_list = base_payload_list[:-1]

        new_payload = tuple((*base_payload_list, partial_text))
        return (task_id, new_payload)
        
    def _handle_chunk_split(self, task_info, task_history):
        """
        Логика разделения большой задачи на чанки при критической ошибке (например, Context Overflow).
        """
        try:
            # Распаковка данных задачи (предполагаем формат epub: payload[1]=epub_path, payload[2]=chapter_path)
            _, epub_path, chapter_path, *_ = task_info[1]
            
            with open(epub_path, 'rb') as f:
                with zipfile.ZipFile(f, "r") as zf:
                    content = zf.read(chapter_path).decode("utf-8", "ignore")
            
            prefix, chunks, suffix = brute_force_split(content)
            
            new_tasks = []
            for i, chunk_content in enumerate(chunks):
                # Формируем payload для типа 'epub_chunk'
                task_data = ('epub_chunk', epub_path, chapter_path, chunk_content, i, len(chunks), prefix, suffix)
                new_tasks.append(task_data)
            
            if new_tasks:
                # Наследование истории ошибок для предотвращения бесконечных циклов в чанках
                smart_history_to_pass = None
                parent_errors = task_history.get('errors', {})
                if parent_errors:
                    most_common_error = Counter(parent_errors).most_common(1)[0][0]
                    smart_history_to_pass = {'errors': {most_common_error: 1}}
                
                self.worker.task_manager.add_priority_tasks(new_tasks, parent_history=smart_history_to_pass)
                self.worker._post_event('tasks_added', {'count': len(new_tasks)})

            return (task_info, False, 'SPLIT_FOR_RETRY', f"Разделено на {len(chunks)} частей")

        except Exception as split_exc:
            self.worker._post_event('log_message', {'message': f"[ERROR] Не удалось разделить задачу: {split_exc}"})
            return (task_info, False, 'CHUNK_ERROR', f"Ошибка разделения: {split_exc}")