"""Telegram Bot API transport (interim interface until a MAX bot exists).

Mirrors the MaxBot interface: check(), send_text(), poll_updates(handler)
with normalized messages: {chat_id, user_id, user_name, text}.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

import aiohttp

log = logging.getLogger("tgbot")

API_BASE = "https://api.telegram.org"
CHUNK = 4000  # Telegram hard limit is 4096


class TelegramBot:
    def __init__(self, token: str):
        self.token = token
        self._session: aiohttp.ClientSession | None = None
        self._offset: int | None = None
        self._name = "?"

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _url(self, method: str) -> str:
        return f"{API_BASE}/bot{self.token}/{method}"

    async def check(self) -> str:
        http = await self._http()
        async with http.get(self._url("getMe")) as r:
            body = await r.json()
            if not body.get("ok"):
                raise RuntimeError(f"проверка токена Telegram не прошла: {body}")
            me = body["result"]
            self._name = f"@{me.get('username', '?')}"
            return self._name

    async def send_text(self, chat_id: int, text: str) -> None:
        http = await self._http()
        for i in range(0, max(len(text), 1), CHUNK):
            async with http.post(self._url("sendMessage"),
                                 json={"chat_id": chat_id, "text": text[i:i + CHUNK]}) as r:
                if r.status != 200:
                    log.error("sendMessage failed: HTTP %s %s", r.status, await r.text())

    async def poll_updates(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        """Long polling; emits normalized private-chat messages."""
        http = await self._http()
        log.info("бот %s: опрос обновлений запущен", self._name)
        while True:
            try:
                params: dict[str, str] = {"timeout": "30"}
                if self._offset is not None:
                    params["offset"] = str(self._offset)
                async with http.get(self._url("getUpdates"), params=params,
                                    timeout=aiohttp.ClientTimeout(total=40)) as r:
                    body = await r.json()
                for update in body.get("result", []):
                    self._offset = update["update_id"] + 1
                    msg = update.get("message") or update.get("edited_message")
                    if not msg:
                        continue
                    chat = msg.get("chat", {})
                    if chat.get("type") != "private":
                        continue  # stage 1: private chats only
                    user = msg.get("from", {})
                    norm = {
                        "chat_id": chat.get("id"),
                        "user_id": user.get("id"),
                        "user_name": user.get("first_name") or user.get("username") or "?",
                        "text": msg.get("text") or "",
                    }
                    if norm["chat_id"] is None or norm["user_id"] is None or not norm["text"].strip():
                        continue
                    try:
                        await handler(norm)
                    except Exception:  # noqa: BLE001
                        log.exception("обработка сообщения упала")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("опрос обновлений: ошибка, повтор через 5 с")
                await asyncio.sleep(5)
