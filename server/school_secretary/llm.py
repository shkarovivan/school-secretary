"""Thin OpenAI-compatible chat-completion client (GLM/Zhipu, OpenRouter,
YandexGPT-compatible gateways, local vLLM/Ollama — anything speaking
{base}/chat/completions)."""

from __future__ import annotations

import json
import logging
from typing import Optional

import aiohttp

log = logging.getLogger("llm")


class LLM:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    async def chat_raw(self, messages: list[dict], tools: list[dict] | None = None,
                       max_tokens: int = 1200, temperature: float = 0.3) -> dict:
        """One raw chat round; returns the assistant message dict
        (may contain content and/or tool_calls)."""
        url = f"{self.base_url}/chat/completions"
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        for token_param in ("max_completion_tokens", "max_tokens"):
            payload[token_param] = max_tokens
            break  # chat_raw used with gateways that accept the new name; adjust if needed
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        last_error: Exception | None = None
        for attempt in (1, 2):
            try:
                async with aiohttp.ClientSession() as http:
                    async with http.post(url, json=payload, headers=headers,
                                         timeout=aiohttp.ClientTimeout(total=self.timeout)) as r:
                        body = await r.text()
                        if r.status == 400 and "max_tokens" in body and "not supported" in body:
                            payload.pop("max_completion_tokens", None)
                            payload["max_tokens"] = max_tokens
                            continue
                        if r.status != 200:
                            raise RuntimeError(f"LLM HTTP {r.status}: {body[:300]}")
                        data = json.loads(body)
                        return data["choices"][0]["message"]
            except aiohttp.ClientConnectorError as e:
                last_error = e
                if attempt == 2:
                    raise RuntimeError(f"LLM недоступен: {e}") from e
                await asyncio.sleep(2)
        raise RuntimeError(f"LLM: {last_error}")

    async def complete(self, system: str, user: str, max_tokens: int = 900,
                       temperature: float = 0.3) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        # Some gateways/models want max_completion_tokens (new OpenAI convention),
        # older ones only accept max_tokens — try one, fall back to the other.
        for token_param in ("max_completion_tokens", "max_tokens"):
            payload = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                token_param: max_tokens,
            }
            for attempt in (1, 2):  # one retry on transient network errors
                try:
                    async with aiohttp.ClientSession() as http:
                        async with http.post(url, json=payload, headers=headers,
                                             timeout=aiohttp.ClientTimeout(total=self.timeout)) as r:
                            body = await r.text()
                            if r.status == 400 and "'max_tokens' is not supported" in body:
                                break  # retry with the other parameter name
                            if r.status != 200:
                                raise RuntimeError(f"LLM HTTP {r.status}: {body[:300]}")
                            data = json.loads(body)
                            try:
                                return data["choices"][0]["message"]["content"].strip()
                            except (KeyError, IndexError) as e:
                                raise RuntimeError(f"неожиданный ответ LLM: {body[:300]}") from e
                except aiohttp.ClientConnectorError as e:
                    if attempt == 2:
                        raise RuntimeError(f"LLM недоступен: {e}") from e
                    await __import__("asyncio").sleep(2)
        raise RuntimeError("LLM: оба варианта параметра токенов отклонены")
