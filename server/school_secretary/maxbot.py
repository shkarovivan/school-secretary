"""Official MAX Bot API client: long polling + sending messages.

Only outbound + updates here; the unofficial userbot lives in bridge.py.
Endpoint: https://platform-api2.max.ru (per dev.max.ru docs).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

import aiohttp

log = logging.getLogger("maxbot")

API_BASE = "https://platform-api2.max.ru"


class MaxBot:
    def __init__(self, token: str):
        self.token = token
        self._session: Optional[aiohttp.ClientSession] = None
        self._marker: Optional[int] = None
        self._name: str = "?"

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def check(self) -> str:
        """Validate the token; returns bot name. Raises on bad token."""
        http = await self._http()
        async with http.get(f"{API_BASE}/me", params={"access_token": self.token}) as r:
            body = await r.json()
            if r.status != 200 or "name" not in body:
                raise RuntimeError(f"проверка токена бота не прошла: HTTP {r.status} {body}")
            self._name = body.get("name") or body.get("username") or "?"
            return self._name

    async def send_text(self, chat_id: int, text: str) -> None:
        http = await self._http()
        async with http.post(
            f"{API_BASE}/messages",
            params={"access_token": self.token, "chat_id": str(chat_id), "text": text},
        ) as r:
            if r.status not in (200, 201):
                log.error("send failed: HTTP %s %s", r.status, await r.text())

    async def poll_updates(self, handler: Callable[[dict[str, Any]], asyncio.Future]) -> None:
        """Long polling loop; calls handler(update) for every update."""
        http = await self._http()
        log.info("бот «%s»: опрос обновлений запущен", self._name)
        while True:
            try:
                params: dict[str, str] = {"access_token": self.token, "timeout": "30"}
                if self._marker is not None:
                    params["marker"] = str(self._marker)
                async with http.get(f"{API_BASE}/updates", params=params,
                                    timeout=aiohttp.ClientTimeout(total=40)) as r:
                    body = await r.json()
                for update in body.get("updates", []):
                    self._marker = update.get("update_id", self._marker)
                    try:
                        await handler(update)
                    except Exception:  # noqa: BLE001 - handler bugs must not kill polling
                        log.exception("обработка update упала")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - network hiccups: back off and retry
                log.exception("опрос обновлений: ошибка, повтор через 5 с")
                await asyncio.sleep(5)
