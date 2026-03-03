# -*- coding: utf-8 -*-
"""
ConsistencyEngine v2 — Движок для проверки согласованности текста.
Управляет процессом анализа чанков текста с помощью ИИ, накапливает глоссарий сессии.
"""

import json
import logging
import re
import random
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from PyQt6.QtCore import QObject, pyqtSignal

from ..api.factory import get_api_handler_class
from ..api.config import _load_providers_config
from ..utils.text import repair_json_string

logger = logging.getLogger(__name__)


@dataclass
class GlossarySession:
    """
    Накопительный глоссарий сессии — хранит информацию о персонажах, терминах и сюжетных линиях,
    обнаруженных в процессе анализа чанков.
    """
    characters: List[Dict[str, Any]] = field(default_factory=list)
    terms: List[Dict[str, Any]] = field(default_factory=list)
    processed_chapters: List[str] = field(default_factory=list)
    important_events: List[str] = field(default_factory=list)
    next_chunk_focus: List[str] = field(default_factory=list)

    def update_from_response(self, glossary_update: Dict[str, Any], context_summary: Dict[str, Any]):
        """Обновляет глоссарий на основе ответа модели."""
        if glossary_update:
            # Добавляем персонажей (с дедупликацией по имени)
            for char in glossary_update.get('characters', []):
                if char and not any(c.get('name') == char.get('name') for c in self.characters):
                    self.characters.append(char)
            
            # Добавляем термины (с дедупликацией)
            for term in glossary_update.get('terms', []):
                if term and not any(t.get('term') == term.get('term') for t in self.terms):
                    self.terms.append(term)
        
        if context_summary:
            # Обновляем обработанные главы
            for ch in context_summary.get('processed_chapters', []):
                if ch and ch not in self.processed_chapters:
                    self.processed_chapters.append(ch)
            
            # Обновляем важные события
            for event in context_summary.get('important_events', []):
                if event and event not in self.important_events:
                    self.important_events.append(event)
            
            # Заменяем фокус на следующий чанк (не накапливаем)
            self.next_chunk_focus = context_summary.get('next_chunk_focus', [])

    def to_dict(self) -> Dict[str, Any]:
        """Возвращает глоссарий как словарь для передачи в промт."""
        return {
            'characters': self.characters,
            'terms': self.terms,
            'processed_chapters': self.processed_chapters,
            'important_events': self.important_events,
            'next_chunk_focus': self.next_chunk_focus
        }

    def clear(self):
        """Очищает глоссарий для новой сессии."""
        self.characters.clear()
        self.terms.clear()
        self.processed_chapters.clear()
        self.important_events.clear()
        self.next_chunk_focus.clear()


class ConsistencyEngine(QObject):
    """
    Движок для проверки согласованности текста (Consistency Checker v2).
    Управляет процессом анализа чанков текста с помощью ИИ.
    """

    # Сигналы для UI
    progress_updated = pyqtSignal(int, int)       # current, total
    chunk_analyzed = pyqtSignal(dict)             # результат анализа чанка
    error_occurred = pyqtSignal(str)
    finished = pyqtSignal(list)                   # список всех найденных проблем
    fix_progress = pyqtSignal(int, int, str)      # current, total, chapter_name (для массового исправления)
    fix_completed = pyqtSignal(str, str)          # chapter_path, new_content

    def __init__(self, settings_manager, parent=None):
        super().__init__(parent)
        self.settings_manager = settings_manager
        self.glossary_session = GlossarySession()
        self.all_problems = []
        self.is_cancelled = False
        
        # Кэш для связи проблем с главами
        self.chapter_problems_map: Dict[str, List[Dict[str, Any]]] = {}
        
        # Индекс текущего ключа для ротации
        self._current_key_index = 0

    def cancel(self):
        """Отменяет текущую операцию."""
        self.is_cancelled = True

    def reset_session(self):
        """Сбрасывает сессию для нового анализа."""
        self.glossary_session.clear()
        self.all_problems.clear()
        self.chapter_problems_map.clear()
        self.is_cancelled = False
        self._current_key_index = 0

    def analyze_chapters(self, chapters: List[Dict[str, Any]], config: Dict[str, Any], 
                        active_keys: List[str], 
                        mode: str = 'standard'):
        """
        Основной метод анализа глав.
        
        Args:
            chapters: список словарей {'name': str, 'content': str, 'path': str}
            config: настройки (provider, model, chunk_size, temperature, etc.)
            active_keys: список активных API ключей для использования
            mode: 'standard' или 'glossary_first' (двухпроходный режим)
        """
        self.reset_session()

        if not active_keys:
            self.error_occurred.emit("Нет активных ключей для анализа")
            self.finished.emit([])
            return

        # 1. Разбиение на чанки
        chunks = self._split_into_chunks(chapters, config.get('chunk_size', 3))
        total_chunks = len(chunks)
        
        # Двухпроходный режим: сначала собираем глоссарий
        if mode == 'glossary_first':
            logger.info("Запуск двухпроходного режима: проход 1 - сбор глоссария")
            for i, chunk in enumerate(chunks):
                if self.is_cancelled:
                    break
                    
                self.progress_updated.emit(i + 1, total_chunks * 2)  # *2 для двух проходов
                
                try:
                    api_key = self._get_next_key(active_keys)
                    prompt = self._build_glossary_collection_prompt(chunk, config)
                    response_text = self._call_api(prompt, config, api_key)
                    
                    result = self._parse_ai_response(response_text)
                    if result:
                        # Обновляем только глоссарий, игнорируя проблемы
                        self.glossary_session.update_from_response(
                            result.get('glossary_update', {}),
                            result.get('context_summary', {})
                        )
                        # Эмитим результат для отображения прогресса в UI
                        self.chunk_analyzed.emit({
                            'problems': [],
                            'glossary_update': result.get('glossary_update', {}),
                            'context_summary': result.get('context_summary', {}),
                            'phase': 'glossary_collection'
                        })
                        
                except Exception as e:
                    logger.error(f"Error collecting glossary for chunk {i}: {e}")
                    self.error_occurred.emit(f"Ошибка сбора глоссария (чанк {i}): {e}")
            
            logger.info("Двухпроходный режим: проход 2 - поиск проблем с глоссарием")

        # Основной проход: поиск проблем
        for i, chunk in enumerate(chunks):
            if self.is_cancelled:
                break

            if mode == 'glossary_first':
                self.progress_updated.emit(total_chunks + i + 1, total_chunks * 2)
            else:
                self.progress_updated.emit(i + 1, total_chunks)

            # 2. Формирование промпта
            prompt = self._build_analysis_prompt(chunk, config)

            # 3. Вызов API с ротацией ключей
            try:
                api_key = self._get_next_key(active_keys)
                response_text = self._call_api(prompt, config, api_key)

                # 4. Валидация и парсинг JSON
                analysis_result = self._parse_ai_response(response_text)

                if analysis_result:
                    # Накапливаем проблемы
                    chunk_problems = analysis_result.get('problems', [])
                    for prob in chunk_problems:
                        prob['chunk_index'] = i
                        # Привязываем проблему к главе
                        chapter_name = prob.get('chapter', '')
                        if chapter_name not in self.chapter_problems_map:
                            self.chapter_problems_map[chapter_name] = []
                        self.chapter_problems_map[chapter_name].append(prob)
                    
                    self.all_problems.extend(chunk_problems)

                    # Обновляем глоссарий сессии (если не двухпроходный, или добавляем новое)
                    self.glossary_session.update_from_response(
                        analysis_result.get('glossary_update', {}),
                        analysis_result.get('context_summary', {})
                    )

                    self.chunk_analyzed.emit(analysis_result)

            except Exception as e:
                logger.error(f"Error analyzing chunk {i}: {e}")
                self.error_occurred.emit(str(e))

        self.finished.emit(self.all_problems)

    def _get_next_key(self, active_keys: List[str]) -> str:
        """Получает следующий ключ с ротацией."""
        if not active_keys:
            raise ValueError("Нет доступных ключей")
        
        key = active_keys[self._current_key_index % len(active_keys)]
        self._current_key_index += 1
        return key

    def _split_into_chunks(self, chapters: List[Dict[str, Any]], chunk_size: int) -> List[List[Dict[str, Any]]]:
        """Разбивает список глав на группы (чанки)."""
        return [chapters[i:i + chunk_size] for i in range(0, len(chapters), chunk_size)]

    def _filter_glossary_for_text(self, text: str, extra_text: str = "") -> Dict[str, Any]:
        """
        Фильтрует глоссарий, оставляя только термины, встречающиеся в тексте.
        Использует нечеткий поиск (упрощенная лемматизация хвостов).
        """
        full_text = (text + " " + extra_text).lower()
        
        filtered_chars = []
        filtered_terms = []
        
        # 1. Фильтруем персонажей
        for char in self.glossary_session.characters:
            name = char.get('name', '').strip()
            aliases = char.get('aliases', [])
            
            # Проверяем имя и алиасы
            found = False
            to_check = [name] + aliases
            
            for word in to_check:
                if not word: continue
                word_lower = word.lower()
                
                # Эвристика: если слово длинное (>4), ищем основу без окончания
                if len(word_lower) > 4:
                    root = word_lower[:-1]
                else:
                    root = word_lower
                    
                if root in full_text:
                    found = True
                    break
            
            if found:
                filtered_chars.append(char)
                
        # 2. Фильтруем термины
        for term_obj in self.glossary_session.terms:
            term = term_obj.get('term', '').strip()
            if not term: continue
            
            term_lower = term.lower()
            if len(term_lower) > 4:
                root = term_lower[:-1]
            else:
                root = term_lower
                
            if root in full_text:
                filtered_terms.append(term_obj)
                
        return {
            'characters': filtered_chars,
            'terms': filtered_terms,
            'processed_chapters': self.glossary_session.processed_chapters,
            'important_events': self.glossary_session.important_events,
            'next_chunk_focus': self.glossary_session.next_chunk_focus
        }

    def _build_analysis_prompt(self, chunk: List[Dict[str, Any]], config: Dict[str, Any]) -> str:
        """Формирует промпт для анализа."""
        chapters_text = ""
        for ch in chunk:
            chapters_text += f"\n--- CHAPTER: {ch['name']} ---\n{ch['content']}\n"

        # Умная фильтрация глоссария
        filtered_glossary = self._filter_glossary_for_text(chapters_text)
        
        context_json = json.dumps(
            filtered_glossary, ensure_ascii=False, indent=2)

        # Загружаем промпт из файла
        from ..api.config import get_resource_path
        prompts_file = get_resource_path("config/consistency_prompts.json")
        system_prompt = ""
        
        if prompts_file.exists():
            try:
                with open(prompts_file, 'r', encoding='utf-8') as f:
                    prompts_data = json.load(f)
                    system_prompt = "\n".join(
                        prompts_data.get("consistency_analysis", []))
            except Exception as e:
                logger.error(f"Failed to load consistency prompts: {e}")

        if not system_prompt:
            system_prompt = config.get(
                'system_prompt', "You are a professional literary editor.")

        # Подставляем переменные в промпт
        prompt = system_prompt.replace('{context_json}', context_json)
        prompt = prompt.replace('{chapters_text}', chapters_text)

        return prompt

    def _build_glossary_collection_prompt(self, chunk: List[Dict[str, Any]], config: Dict[str, Any]) -> str:
        """Формирует промпт для сбора глоссария (первый проход двухпроходного режима)."""
        chapters_text = ""
        for ch in chunk:
            chapters_text += f"\n--- CHAPTER: {ch['name']} ---\n{ch['content']}\n"

        # Умная фильтрация глоссария
        filtered_glossary = self._filter_glossary_for_text(chapters_text)
        
        context_json = json.dumps(
            filtered_glossary, ensure_ascii=False, indent=2)

        # Загружаем промпт для сбора глоссария
        from ..api.config import get_resource_path
        prompts_file = get_resource_path("config/consistency_prompts.json")
        system_prompt = ""
        
        if prompts_file.exists():
            try:
                with open(prompts_file, 'r', encoding='utf-8') as f:
                    prompts_data = json.load(f)
                    system_prompt = "\n".join(
                        prompts_data.get("glossary_collection", []))
            except Exception as e:
                logger.error(f"Failed to load glossary collection prompts: {e}")

        if not system_prompt:
            # Fallback: используем обычный промпт, но попросим не искать проблемы
            system_prompt = (
                "Analyze the following text and extract:\n"
                "1. Characters: name, gender, role, aliases\n"
                "2. Terms: unique terms, skills, items, locations\n"
                "3. Plot points: active storylines\n"
                "Do NOT look for problems in this pass.\n\n"
                "CURRENT CONTEXT:\n{context_json}\n\n"
                "TEXT TO ANALYZE:\n{chapters_text}\n\n"
                "Return JSON with glossary_update and context_summary only."
            )

        prompt = system_prompt.replace('{context_json}', context_json)
        prompt = prompt.replace('{chapters_text}', chapters_text)

        return prompt

    def fix_chapter(self, chapter_content: str, problems: List[Dict[str, Any]], 
                    config: Dict[str, Any], active_keys: List[str],
                    batch_mode: bool = False) -> str:
        """
        Исправляет конкретную главу на основе списка проблем.
        
        Args:
            chapter_content: текст главы для исправления
            problems: список проблем для исправления
            config: настройки API
            active_keys: список активных API ключей
            batch_mode: использовать batch-промпт для нескольких ошибок
            
        Returns:
            Исправленный текст главы
        """
        from ..api.config import get_resource_path
        prompts_file = get_resource_path("config/consistency_prompts.json")
        
        if batch_mode and len(problems) > 1:
            # Batch-режим для нескольких ошибок
            prompt_template = ""
            if prompts_file.exists():
                try:
                    with open(prompts_file, 'r', encoding='utf-8') as f:
                        prompts_data = json.load(f)
                        prompt_template = "\n".join(
                            prompts_data.get("batch_chapter_fix", []))
                except Exception as e:
                    logger.error(f"Failed to load batch fix prompt: {e}")
            
            if not prompt_template:
                prompt_template = "Fix the following errors in the chapter:\n{errors_list}\n\nChapter:\n{chapter_content}"
            
            # Формируем список ошибок
            errors_list = []
            for i, prob in enumerate(problems, 1):
                errors_list.append(
                    f"{i}. [{prob.get('type', 'error')}] {prob.get('description', '')}\n"
                    f"   Цитата: \"{prob.get('quote', '')}\"\n"
                    f"   Исправить: {prob.get('suggestion', '')}"
                )
            
            prompt = prompt_template.replace('{errors_list}', "\n".join(errors_list))
            
            # Для множественных ошибок берем полный текст описаний
            extra_context = "\n".join([p.get('description', '') + " " + p.get('quote', '') for p in problems])
            filtered_glossary = self._filter_glossary_for_text(chapter_content, extra_context)
            
            prompt = prompt.replace('{glossary_json}', json.dumps(filtered_glossary, ensure_ascii=False, indent=2))
            prompt = prompt.replace('{chapter_content}', chapter_content)
            
        else:
            # Одиночное исправление
            prompt_template = ""
            if prompts_file.exists():
                try:
                    with open(prompts_file, 'r', encoding='utf-8') as f:
                        prompts_data = json.load(f)
                        prompt_template = "\n".join(
                            prompts_data.get("consistency_correction", []))
                except Exception as e:
                    logger.error(f"Failed to load correction prompt: {e}")
            
            if not prompt_template:
                prompt_template = "Fix this error: {error_description}\n\nChapter:\n{chapter_content}"
            
            prob = problems[0] if problems else {}
            prompt = prompt_template.replace('{error_type}', prob.get('type', 'error'))
            prompt = prompt_template.replace('{error_description}', prob.get('description', ''))
            prompt = prompt.replace('{quote}', prob.get('quote', ''))
            prompt = prompt.replace('{suggestion}', prob.get('suggestion', ''))
            
            # Фильтрация для одиночной ошибки
            extra_context = prob.get('description', '') + " " + prob.get('quote', '')
            filtered_glossary = self._filter_glossary_for_text(chapter_content, extra_context)
            
            prompt = prompt.replace('{glossary_json}', json.dumps(filtered_glossary, ensure_ascii=False, indent=2))
            prompt = prompt.replace('{chapter_content}', chapter_content)

        api_key = self._get_next_key(active_keys)
        return self._call_api(prompt, config, api_key)

    def fix_all_chapters(self, chapters: List[Dict[str, Any]], config: Dict[str, Any],
                         active_keys: List[str]) -> Dict[str, str]:
        """
        Массово исправляет все главы с найденными проблемами.
        
        Args:
            chapters: список глав {'name': str, 'content': str, 'path': str}
            config: настройки API
            active_keys: список активных API ключей
            
        Returns:
            Словарь {path: new_content} с исправленными главами
        """
        results = {}
        chapters_with_problems = [
            ch for ch in chapters 
            if ch['name'] in self.chapter_problems_map and self.chapter_problems_map[ch['name']]
        ]
        
        total = len(chapters_with_problems)
        
        for i, chapter in enumerate(chapters_with_problems):
            if self.is_cancelled:
                break
                
            chapter_name = chapter['name']
            problems = self.chapter_problems_map.get(chapter_name, [])
            
            self.fix_progress.emit(i + 1, total, chapter_name)
            
            try:
                fixed_content = self.fix_chapter(
                    chapter['content'], 
                    problems, 
                    config,
                    active_keys,
                    batch_mode=len(problems) > 1
                )
                results[chapter['path']] = fixed_content
                self.fix_completed.emit(chapter['path'], fixed_content)
                
            except Exception as e:
                logger.error(f"Error fixing chapter {chapter_name}: {e}")
                self.error_occurred.emit(f"Ошибка при исправлении {chapter_name}: {e}")
        
        return results

    def _call_api(self, prompt: str, config: Dict[str, Any], api_key: str) -> str:
        """
        Вызывает API через существующую инфраструктуру.
        
        Args:
            prompt: текст промпта
            config: настройки (provider, model, temperature, etc.)
            api_key: API ключ для использования
        """
        provider_name = config.get('provider', 'google')
        model_name = config.get('model', 'gemini-2.0-flash-exp')

        providers_config = _load_providers_config()
        provider_info = providers_config.get(provider_name)

        if not provider_info:
            raise ValueError(f"Provider {provider_name} not found in config")

        handler_class_name = provider_info.get('handler_class')
        handler_class = get_api_handler_class(handler_class_name)

        model_config = provider_info.get('models', {}).get(model_name, {})
        
        # Добавляем id модели в model_config если его нет
        if 'id' not in model_config:
            model_config['id'] = model_name
        
        model_id = model_config.get('id', model_name)
        
        # RPD tracking: проверяем, не исчерпан ли лимит ключа для этой модели
        key_info = {'key': api_key, 'provider': provider_name}
        if self.settings_manager.is_key_limit_active(key_info, model_id):
            raise ValueError(f"Ключ {api_key[:8]}... исчерпал лимит для модели {model_id}")

        # Создаём mock-worker для совместимости с handler API
        class MockWorker:
            def __init__(self, provider_config, model_config, config, api_key_value):
                self.provider_config = provider_config
                self.model_config = model_config
                self.is_cancelled = False
                self.session_id = "consistency_check"
                self.temperature = config.get('temperature', 0.3)
                self.thinking_enabled = config.get('thinking_enabled', False)
                self.thinking_budget = config.get('thinking_budget', 0)
                self.thinking_level = config.get('thinking_level', 'minimal')
                self.api_key = api_key_value
                self.worker_id = api_key_value
                self.model_id = model_config.get('id', model_name)
                
                # Mock PromptBuilder
                class MockPromptBuilder:
                    def __init__(self):
                        self.system_instruction = config.get('system_prompt')
                        
                self.prompt_builder = MockPromptBuilder()

            def check_cancellation(self):
                if self.is_cancelled:
                    from ..api.errors import OperationCancelledError
                    raise OperationCancelledError("Cancelled by user")

        mock_worker = MockWorker(provider_info, model_config, config, api_key)
        handler = handler_class(mock_worker)

        # Создаём объект с api_key для setup_client
        class KeyHolder:
            def __init__(self, key):
                self.api_key = key
                self.worker_id = key
        
        key_holder = KeyHolder(api_key)
        handler.setup_client(key_holder)

        # Вызываем API
        from ..api.base import get_worker_loop
        loop = get_worker_loop()

        async def run_call():
            response = await handler.call_api(prompt, "[Consistency]", use_stream=False)
            return response

        response = loop.run_until_complete(run_call())
        
        # RPD tracking: инкрементируем счётчик запросов после успешного вызова
        self.settings_manager.increment_request_count(api_key, model_id)
        
        return response

    def _parse_ai_response(self, text: str) -> Optional[Dict[str, Any]]:
        """
        Парсит и валидирует JSON от ИИ.
        Обрабатывает различные форматы ответа (чистый JSON, markdown-блоки).
        """
        if not text:
            return None
            
        # Убираем markdown-блоки если есть
        text = text.strip()
        
        # Паттерн для извлечения JSON из markdown блока
        json_block_pattern = r'```(?:json)?\s*([\s\S]*?)\s*```'
        match = re.search(json_block_pattern, text)
        if match:
            text = match.group(1).strip()
        
        # Ищем первую { и последнюю }
        start_idx = text.find('{')
        end_idx = text.rfind('}')
        
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            text = text[start_idx:end_idx + 1]
        
        try:
            # Пробуем распарсить напрямую
            data = json.loads(text)
            return self._validate_response(data)
        except json.JSONDecodeError:
            # Пробуем восстановить битый JSON
            try:
                repaired_json = repair_json_string(text)
                data = json.loads(repaired_json)
                return self._validate_response(data)
            except Exception as e:
                logger.error(f"Failed to parse AI response: {e}\nOriginal text: {text[:500]}...")
                return None

    def _validate_response(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Валидирует структуру ответа."""
        if not isinstance(data, dict):
            return None
        
        # Проверяем обязательное поле problems
        if 'problems' not in data:
            data['problems'] = []
        
        # Валидируем каждую проблему
        valid_problems = []
        for prob in data.get('problems', []):
            if isinstance(prob, dict) and prob.get('type'):
                # Добавляем дефолтные значения
                prob.setdefault('id', len(valid_problems) + 1)
                prob.setdefault('confidence', 'medium')
                prob.setdefault('chapter', 'Unknown')
                valid_problems.append(prob)
        
        data['problems'] = valid_problems
        
        # Добавляем пустые структуры если их нет
        data.setdefault('glossary_update', {'characters': [], 'terms': [], 'plots': []})
        data.setdefault('context_summary', {'processed_chapters': [], 'important_events': [], 'next_chunk_focus': []})
        
        return data

    def get_problems_for_chapter(self, chapter_name: str) -> List[Dict[str, Any]]:
        """Возвращает список проблем для конкретной главы."""
        return self.chapter_problems_map.get(chapter_name, [])

    def get_glossary_summary(self) -> Dict[str, Any]:
        """Возвращает текущее состояние глоссария сессии."""
        return self.glossary_session.to_dict()

    def get_glossary_token_count(self) -> int:
        """Возвращает приблизительное количество токенов в глоссарии сессии."""
        glossary_json = json.dumps(self.glossary_session.to_dict(), ensure_ascii=False)
        return len(glossary_json) // 4  # ~4 символа на токен
