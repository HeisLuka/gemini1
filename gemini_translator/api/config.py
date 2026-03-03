# -*- coding: utf-8 -*-

import json
from pathlib import Path
import sys
import os # <-- Добавляем импорт os


# [ARCH] URI для общей базы данных в оперативной памяти.
# mode=memory: данные живут только в RAM.
# cache=shared: позволяет разным потокам видеть одну и ту же базу данных.
SESSION_ID = os.path.basename(os.getcwd()).replace(" ", "_").replace(".", "_")
SHARED_DB_URI = f'file:{SESSION_ID}_vfm_session?mode=memory&cache=shared'
# --- ЭТАП 1: УНИВЕРСАЛЬНЫЕ ФУНКЦИИ ДЛЯ РАБОТЫ С ПУТЯМИ ---

def get_executable_dir() -> Path | None:
    """Возвращает путь к папке с .exe файлом, если приложение скомпилировано."""
    if getattr(sys, 'frozen', False):
        return Path(os.path.dirname(sys.executable))
    return None

def get_internal_resource_dir() -> Path | None:
    """Возвращает путь к временной папке _MEIPASS, если это one-file сборка."""
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return Path(sys._MEIPASS)
    return None

def get_dev_project_root() -> Path:
    """Возвращает корень проекта в режиме разработки."""
    return Path(__file__).resolve().parents[2]

def get_resource_path(relative_path: str) -> Path:
    """
    Универсальная функция поиска ресурсов с приоритетом для гибридной сборки.
    1. Ищет ресурс рядом с .exe.
    2. Если не находит, ищет внутри .exe (во временной папке).
    3. Если это режим разработки, ищет относительно корня проекта.
    """
    # Сценарий 1: Приложение скомпилировано
    if getattr(sys, 'frozen', False):
        executable_dir = get_executable_dir()
        
        # Приоритет №1: Внешний файл (для гибридного режима)
        external_path = executable_dir / relative_path
        if external_path.exists():
            return external_path
            
        # Приоритет №2: Внутренний файл (для портативного режима)
        internal_dir = get_internal_resource_dir()
        if internal_dir:
            internal_path = internal_dir / relative_path
            if internal_path.exists():
                return internal_path
        
        # Если ничего не найдено, все равно возвращаем путь к внешнему файлу.
        # Вызывающий код должен будет обработать ошибку FileNotFoundError.
        return external_path

    # Сценарий 2: Режим разработки
    else:
        project_root = get_dev_project_root()
        return project_root / relative_path

_PROVIDERS_FILE = get_resource_path("config/api_providers.json")
_PROMPT_FILE = get_resource_path("config/default_prompt.txt")
_GLOSSARY_PROMPT_FILE = get_resource_path("config/default_glossary_prompt.txt")
_CORRECTION_PROMPT_FILE = get_resource_path("config/default_correction_prompt.txt")
_UNTRANSLATED_PROMPT_FILE = get_resource_path("config/default_untranslated_prompt.txt")
_WORD_EXCEPTIONS_FILE = get_resource_path("config/default_word_exceptions.txt")
_INTERNAL_PROMPTS_FILE = get_resource_path("config/internal_prompts.json")

# Резервные встроенные конфиги на случай, если файлы не найдены
_DEFAULT_API_PROVIDERS_CONFIG = {
    "gemini": {
        "display_name": "Google Gemini (default)",
        "handler_class": "GeminiApiHandler",
        "is_async": False,
        "needs_warmup": False,
        "file_suffix": "_translated.html",
        "reset_policy": {"type": "daily", "timezone": "America/Los_Angeles", "reset_hour": 0, "reset_minute": 1},
        "models": {"Gemini 2.5 Flash Preview": {"id": "gemini-2.5-flash", "rpm": 10, "needs_chunking": True}}
    }
}
_DEFAULT_PROMPT_TEXT = """**I. РОЛЬ И ГЛАВНАЯ ЦЕЛЬ** (встроенный промпт) …"""
_DEFAULT_GLOSSARY_PROMPT_TEXT = """Проанализируй весь предоставленный текст …"""
_DEFAULT_WORD_EXCEPTIONS_TEXT = "# Пустой список исключений по умолчанию"
_DEFAULT_CORRECTION_PROMPT_TEXT = """Проанализируй представленный глоссарий…"""
def _load_providers_config():
    if _PROVIDERS_FILE.exists():
        try:
            with open(_PROVIDERS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return _DEFAULT_API_PROVIDERS_CONFIG
    return _DEFAULT_API_PROVIDERS_CONFIG

def _load_default_prompt():
    if _PROMPT_FILE.exists():
        try:
            with open(_PROMPT_FILE, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except Exception:
            return _DEFAULT_PROMPT_TEXT
    return _DEFAULT_PROMPT_TEXT

def _load_default_glossary_prompt():
    if _GLOSSARY_PROMPT_FILE.exists():
        try:
            with open(_GLOSSARY_PROMPT_FILE, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except Exception:
            return _DEFAULT_GLOSSARY_PROMPT_TEXT
    return _DEFAULT_GLOSSARY_PROMPT_TEXT

def _load_default_correction_prompt():
    if _CORRECTION_PROMPT_FILE.exists():
        try:
            with open(_CORRECTION_PROMPT_FILE, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except Exception:
            return _DEFAULT_CORRECTION_PROMPT_TEXT
    return _DEFAULT_CORRECTION_PROMPT_TEXT

def _load_default_untranslated_prompt():
    if _UNTRANSLATED_PROMPT_FILE.exists():
        try:
            with open(_UNTRANSLATED_PROMPT_FILE, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except Exception:
            return "Переведи следующий текст:"
    return "Переведи следующий текст:"
    
def _load_default_word_exceptions():
    if _WORD_EXCEPTIONS_FILE.exists():
        try:
            with open(_WORD_EXCEPTIONS_FILE, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except Exception:
            return _DEFAULT_WORD_EXCEPTIONS_TEXT
    return _DEFAULT_WORD_EXCEPTIONS_TEXT

def _load_internal_prompts():
    """
    Загружает скрытые промпты из JSON или возвращает дефолты.
    Поддерживает многострочность через списки строк в JSON.
    """
    defaults = {
        "glossary_context_simple": "--- КОНТЕКСТ ---\n",
        "glossary_context_full": "--- КОНТЕКСТ ---\n",
        "batch_instruction": "\n\n### ИНСТРУКЦИЯ\n Keep all `<!-- i -->` including the last one. ###\n```html\n{full_text_for_api}\n```\n",
        "glossary_output_examples": {"base": ["  \"Arthur\": { \"rus\": \"Артур\", \"note\": \"Персонаж; Мужчина; Имя склоняется (позвал Артура)\" }"]},
        "glossary_tag_explanation": {
            "_INTRO_TEXT_": "GLOSSARY GUIDE\nThe `i` (info) field contains critical commands. Decode them as follows:",
	        "HOMONYM/ОМОНИМ": "Context Switch. Choose the translation that matches the current condition.",
	        "GENDER INTRIGUE/ГЕНДЕРНАЯ ИНТРИГА": "Complex Gender Protocol. Follow sub-tags based on chapter context."
        },
        "translation_output_examples": {
            "base": [
                "Src: <p>\"Hello,\" he said.</p>\nTgt: <p>─ Привет, ─ сказал он.</p>",
                "Src: <p>'Thinking,' he thought.</p>\nTgt: <p>«Мысли», – подумал он.</p>",
                "Src: <p>[System: Alert]</p>\nTgt: <p>[Система: Тревога]</p>"
            ]
        },
        "completion_instruction": "\n---\nПродолжи прерванную работу: {partial_translation}",
        "correction_prompts": {
            "intro": "Данные в блоках:",
            "block_descriptions": {
                "context": "Контекст.",
                "conflicts": "Конфликты.",
                "overlaps": "Наложения.",
                "patterns": "Паттерны.",
                "hidden": "Скрытые."
            },
            "format_instructions": {
                "json_intro": "Формат JSON:",
                "schemas": {
                    "input_with_notes": "`\"Оригинал\": { \"rus\": \"Перевод\", \"note\": \"Примечание\" }`",
                    "output_with_notes": "`{ \"Оригинал\": { \"rus\": \"Исправленный Перевод\", \"note\": \"Исправленное Примечание\" } }`",
                    "input_simple": "`\"Оригинал\": { \"rus\": \"Перевод\" }`",
                    "output_simple": "`{ \"Оригинал\": { \"rus\": \"Исправленный Перевод\" } }`"
                },
                "warning_hint": "Исправь WARNING.",
                "note_policy": "Сохраняй грамматику примечаний",
                "task_goal": "Верни JSON:"
            },
            "examples": {
                "with_notes": "{\"A\": {\"rus\": \"B\", \"note\": \"C\"}}",
                "simple": "{\"A\": {\"rus\": \"B\"}}"
            }
        }
    }
    
    loaded_data = {}
    if _INTERNAL_PROMPTS_FILE.exists():
        try:
            with open(_INTERNAL_PROMPTS_FILE, 'r', encoding='utf-8') as f:
                loaded_data = json.load(f)
        except Exception as e:
            print(f"####################\n####################\n[CONFIG ERROR] НЕ УДАЛОСЬ ЗАГРУЗИТЬ internal_prompts.json: {e}\n####################\n####################")
    # Объединяем загруженные данные с дефолтными
    for key, value in loaded_data.items():
        if key in defaults and isinstance(defaults[key], dict) and isinstance(value, dict):
             defaults[key].update(value)
        else:
             defaults[key] = value
    
    # --- МАГИЯ СКЛЕИВАНИЯ ---
    # Если значение — это список, превращаем его в строку
    for key, value in defaults.items():
        if isinstance(value, list):
            defaults[key] = "\n".join(value) # <--- разделитель \n
            
    return defaults


# --- ЭТАП 2: ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ-ХРАНИЛИЩА ---
_API_PROVIDERS = {}
_DEFAULT_PROMPT = ""
_DEFAULT_GLOSSARY_PROMPT = ""
_DEFAULT_WORD_EXCEPTIONS = ""
_DEFAULT_CORRECTION_PROMPT = ""
_DEFAULT_UNTRANSLATED_PROMPT = ""
_INTERNAL_PROMPTS = {}
_ALL_MODELS = {}
_PROVIDER_DISPLAY_MAP = {}
_ALL_TRANSLATED_SUFFIXES = []

# --- ПАРАМЕТРЫ РАСЧЕТА ТОКЕНОВ И РАЗМЕРОВ ---
CHARS_PER_ASCII_TOKEN = 4.0
CHARS_PER_CYRILLIC_TOKEN = 2.2
MODEL_OUTPUT_SAFETY_MARGIN = 0.95
ALPHABETIC_EXPANSION_FACTOR = 1.6
CJK_EXPANSION_FACTOR = 3.5

# --- ЭТАП 3: ГЛАВНАЯ ФУНКЦИЯ-ИНИЦИАЛИЗАТОР ---
def initialize_configs():
    global _API_PROVIDERS, _DEFAULT_PROMPT, _DEFAULT_GLOSSARY_PROMPT, _DEFAULT_CORRECTION_PROMPT, _DEFAULT_UNTRANSLATED_PROMPT, _DEFAULT_WORD_EXCEPTIONS, _ALL_MODELS, _PROVIDER_DISPLAY_MAP, _ALL_TRANSLATED_SUFFIXES, _INTERNAL_PROMPTS
    
    print("[CONFIG INFO] Централизованная инициализация конфигураций…")
    _API_PROVIDERS = _load_providers_config()
    _DEFAULT_PROMPT = _load_default_prompt()
    _DEFAULT_GLOSSARY_PROMPT = _load_default_glossary_prompt()
    _DEFAULT_WORD_EXCEPTIONS = _load_default_word_exceptions()
    _DEFAULT_CORRECTION_PROMPT = _load_default_correction_prompt()
    _DEFAULT_UNTRANSLATED_PROMPT = _load_default_untranslated_prompt()
    _INTERNAL_PROMPTS = _load_internal_prompts()

    _API_PROVIDERS['dry_run'] = {
        "display_name": "Пробный запуск",
        "visible": False,
        "handler_class": "DryRunApiHandler",
        "file_suffix": "_dry_run.html",
        "reset_policy": {"type": "rolling", "duration_hours": 999},
        "models": {"dry-run-model": {"id": "dry-run-model", "rpm": 1000}}
    }
    
    _ALL_MODELS = {
        model_name: {**model_config, 'provider': provider_id}
        for provider_id, provider_data in _API_PROVIDERS.items()
        for model_name, model_config in provider_data.get("models", {}).items()
    }
    _PROVIDER_DISPLAY_MAP = {
        p_data["display_name"]: p_id for p_id, p_data in _API_PROVIDERS.items()
    }
    _ALL_TRANSLATED_SUFFIXES = list(set(
        p.get("file_suffix", "_translated.html") for p in _API_PROVIDERS.values()
    ))
    print("[CONFIG INFO] Глобальные конфигурации успешно инициализированы.")

# --- ЭТАП 4: ПУБЛИЧНЫЕ ФУНКЦИИ-ГЕТТЕРЫ (стабильный API) ---
def api_providers(): return _API_PROVIDERS
def default_prompt(): return _DEFAULT_PROMPT
def default_glossary_prompt(): return _DEFAULT_GLOSSARY_PROMPT
def default_correction_prompt(): return _DEFAULT_CORRECTION_PROMPT
def default_untranslated_prompt(): return _DEFAULT_UNTRANSLATED_PROMPT
def internal_prompts(): return _INTERNAL_PROMPTS
def all_models(): return _ALL_MODELS
def provider_display_map(): return _PROVIDER_DISPLAY_MAP
def all_translated_suffixes(): return _ALL_TRANSLATED_SUFFIXES
def default_word_exceptions(): return _DEFAULT_WORD_EXCEPTIONS

# --- ГЕТТЕРЫ ДЛЯ СТАТИЧЕСКИХ КОНСТАНТ ---
def default_reset_policy(): return {"type": "rolling", "duration_hours": 24}
def default_model_name(): return "Gemini 2.5 Flash Preview"
def max_retries(): return 1
def retry_delay_seconds(): return 25
def rate_limit_delay_seconds(): return 60
def api_timeout_seconds(): return 600
def default_max_output_tokens(): return 8192
def chunk_target_size(): return 30000
def input_character_limit_for_chunk(): return 900_000
def chunk_search_window(): return 500
def min_chunk_size(): return 500
def min_forced_chunk_size(): return 250
def chunk_html_source(): return True