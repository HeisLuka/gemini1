import aiohttp
import asyncio
import json
import time
import traceback
from ..base import BaseApiHandler
from ..errors import (
    ContentFilterError, NetworkError, LocationBlockedError, 
    RateLimitExceededError, ModelNotFoundError, ValidationFailedError, 
    TemporaryRateLimitError, PartialGenerationError
)

class HuggingFaceApiHandler(BaseApiHandler):
    """
    Хендлер для Hugging Face Inference API (Router).
    Поддерживает Cold Boot (ожидание 503) и Hybrid Streaming.
    """
    
    def setup_client(self, client_override=None, proxy_settings=None):
        super().setup_client(client_override, proxy_settings)

        if not client_override:
            return False
        
        self.worker.api_key = client_override.api_key
        self.worker.model_id = self.worker.model_config.get("id", "meta-llama/Llama-3.1-8B-Instruct")
        self.base_url = "https://router.huggingface.co/v1/chat/completions"
        
        self._proactive_session_init()
        return True

    async def call_api(self, prompt, log_prefix, allow_incomplete=False, use_stream=True, debug=False, max_output_tokens=None):
        session = await self._get_or_create_session_internal()

        headers = {
            "Authorization": f"Bearer {self.worker.api_key}",
            "Content-Type": "application/json"
        }

        messages = (
            [{"role": "system", "content": self.worker.prompt_builder.system_instruction}]
            if self.worker.prompt_builder.system_instruction
            else []
        ) + [{"role": "user", "content": prompt}]

        payload = {
            "model": self.worker.model_id,
            "messages": messages,
            "temperature": self.worker.temperature,
            "stream": use_stream # Теперь мы реально запрашиваем то, что хотим
        }

        if max_output_tokens:
            payload["max_tokens"] = max_output_tokens
        elif allow_incomplete:
            payload["max_tokens"] = 8192

        # Цикл попыток на случай "Model is loading" (Cold Boot)
        max_retries = 5
        retry_count = 0

        while retry_count < max_retries:
            try:
                async with session.post(self.base_url, headers=headers, json=payload) as response:
                    
                    # --- 1. ОБРАБОТКА ОШИБОК (Статус != 200) ---
                    if response.status != 200:
                        error_text = await response.text()
                        error_json = {}
                        try:
                            error_json = json.loads(error_text)
                        except: pass
                        
                        # Ловим Cold Boot (503 + estimated_time)
                        if response.status == 503 and "estimated_time" in error_json:
                            wait_time = float(error_json["estimated_time"])
                            log_msg = f"❄️ Модель HuggingFace спит. Загрузка... Ждем {wait_time:.1f}с."
                            self.worker._post_event('log_message', {'message': log_msg})
                            
                            await asyncio.sleep(wait_time * 1.2)
                            retry_count += 1
                            continue
                        
                        # Стандартные ошибки
                        if response.status == 401:
                            raise RateLimitExceededError(f"Неверный токен (…{self.worker.api_key[-4:]}) Hugging Face.")
                        if response.status == 404:
                            # HF часто кидает 404, если модель недоступна через этот API
                            raise ModelNotFoundError(f"Модель {self.worker.model_id} недоступна.")
                        if response.status == 429:
                            raise RateLimitExceededError("Превышен лимит запросов (429).")
                        
                        # Любая другая ошибка
                        raise NetworkError(f"Ошибка HF ({response.status}): {error_text[:200]}")

                    # --- 2. ОБРАБОТКА УСПЕШНОГО ОТВЕТА (200 OK) ---
                    
                    # Ветка А: СТРИМИНГ
                    if use_stream:
                        collected_text = ""
                        finish_reason = None
                        
                        try:
                            async for line in response.content:
                                line_str = line.decode('utf-8').strip()
                                if not line_str or line_str == 'data: [DONE]': 
                                    continue
                                
                                if line_str.startswith('data: '):
                                    json_str = line_str[6:] # Убираем "data: "
                                    try:
                                        chunk = json.loads(json_str)
                                        if 'choices' in chunk and chunk['choices']:
                                            delta = chunk['choices'][0].get('delta', {})
                                            content_part = delta.get('content', '')
                                            if content_part:
                                                collected_text += content_part
                                            
                                            # Проверяем причину остановки
                                            f_reason = chunk['choices'][0].get('finish_reason')
                                            if f_reason:
                                                finish_reason = f_reason
                                    except json.JSONDecodeError:
                                        continue
                        
                        except Exception as stream_e:
                            # Если стрим оборвался, но мы что-то скачали — спасаем это!
                            if collected_text:
                                raise PartialGenerationError(
                                    f"Обрыв стрима HF: {stream_e}", 
                                    partial_text=collected_text,
                                    reason="NETWORK_ERROR"
                                )
                            raise stream_e

                        # Если стрим закончился нормально, проверяем finish_reason
                        if finish_reason == "length" and not allow_incomplete:
                             # Если оборвалось из-за лимита токенов, возвращаем как Partial
                             raise PartialGenerationError(
                                "Превышен лимит токенов (length)",
                                partial_text=collected_text,
                                reason="LENGTH"
                             )
                        
                        return collected_text

                    # Ветка Б: ОБЫЧНЫЙ ЗАПРОС (JSON)
                    else:
                        result = await response.json()
                        if 'choices' in result and result['choices']:
                            choice = result['choices'][0]
                            content = choice['message']['content']
                            
                            # Проверка на обрыв по длине
                            if choice.get('finish_reason') == "length" and not allow_incomplete:
                                raise PartialGenerationError(
                                    "Превышен лимит токенов (length)",
                                    partial_text=content,
                                    reason="LENGTH"
                                )
                                
                            return content
                        
                        raise Exception(f"Пустой ответ JSON от HF: {result}")

            except asyncio.TimeoutError:
                raise NetworkError("Таймаут соединения с Hugging Face")
            except (aiohttp.ClientError, OSError) as e:
                # Это подавит трейсбек в консоли и отправит ошибку в штатный обработчик ретраев
                error_msg = f"Сбой сети/SSL ({type(e).__name__}): {e}"
                raise NetworkError(error_msg, delay_seconds=30) from e
            except (RateLimitExceededError, ContentFilterError, NetworkError, 
                    PartialGenerationError, ModelNotFoundError, LocationBlockedError, 
                    ValidationFailedError, TemporaryRateLimitError) as e:
                raise e
            
            except Exception as e:
                traceback.print_exc()
                raise Exception(f"Критическая ошибка HF: {e}")
        
        raise NetworkError("Не удалось дождаться загрузки модели Hugging Face (Retry Limit).", delay_seconds=30)