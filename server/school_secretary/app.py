"""Main application: bot polling + periodic chat sync.

Commands (stage 1 — group chat queries, deterministic, no LLM yet):
  /start  — привязка (первый пользователь = владелец)
  /chats  — список школьных чатов
  /chat <id|название> — последние сообщения чата
  /find <текст> — поиск по истории всех чатов
  /sync   — внеплановая синхронизация (владелец)
  /status — состояние моста и базы
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from .bridge import build_runtime_client, start_client, sync_chats
from .config import Config
from .maxbot import MaxBot
from .store import Store
from .telegram import TelegramBot

log = logging.getLogger("app")

HELP = (
    "Команды школьного секретаря:\n"
    "/chats — список школьных чатов\n"
    "/chat <номер или название> — последние сообщения\n"
    "/find <текст> — поиск по истории чатов\n"
    "/sync — обновить данные из MAX (владелец)\n"
    "/status — состояние системы"
)


def _fmt_time(ts: int) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts / 1000 if ts > 10**11 else ts).strftime("%d.%m %H:%M")


class Application:
    def __init__(self) -> None:
        self.cfg = Config()
        self.cfg.ensure_dirs()
        self.store = Store(self.cfg.db_path)
        # Transport: Telegram (interim) or MAX bot — by which token is set.
        if self.cfg.telegram_token:
            self.bot = TelegramBot(self.cfg.telegram_token)
        elif self.cfg.bot_token:
            self.bot = MaxBot(self.cfg.bot_token)
        else:
            raise SystemExit(
                "Ни один интерфейс не настроен: впишите TELEGRAM_BOT_TOKEN "
                "(или MAX_BOT_TOKEN) в server/.env"
            )
        self._client = None
        self._client_task = None
        self._last_sync_ok: float = 0.0
        self._last_sync_error: str = ""

    # ------------------------------------------------------------ bridge --
    async def _get_client(self):
        if self._client is None:
            self._client = build_runtime_client(self.cfg)
            self._client_task = await start_client(self._client)
        return self._client

    async def do_sync(self) -> str:
        try:
            client = await self._get_client()
            n = await sync_chats(client, self.store, self.cfg)
            self._last_sync_ok = time.time()
            self._last_sync_error = ""
            return f"Синхронизировано чатов: {n}. В базе: {self.store.stats()}."
        except Exception as e:  # noqa: BLE001
            self._last_sync_error = str(e)
            log.exception("sync failed")
            return f"Синхронизация не удалась: {e}"

    async def sync_loop(self) -> None:
        while True:
            await self.do_sync()
            await asyncio.sleep(600)  # every 10 minutes

    # -------------------------------------------------------------- bot --
    async def handle_message(self, msg: dict[str, Any]) -> None:
        chat_id, user_id, user_name, text = (
            msg["chat_id"], msg["user_id"], msg["user_name"], msg["text"].strip()
        )
        if not text:
            return

        if text.startswith("/start"):
            role = self.store.bind_owner(user_id, user_name)
            hi = ("Вы — владелец секретаря. " if role == "owner"
                  else "Ждите подтверждения доступа от владельца. ")
            await self.bot.send_text(chat_id, f"Здравствуйте, {user_name}! {hi}\n\n{HELP}")
            return
        if text.startswith("/help"):
            await self.bot.send_text(chat_id, HELP)
            return
        if not self.store.is_owner(user_id):
            await self.bot.send_text(chat_id, "Доступ только у владельца. Скажите ему, чтобы подключил вас.")
            return

        if text.startswith("/chats"):
            chats = self.store.list_chats()
            if not chats:
                await self.bot.send_text(chat_id, "Чатов пока нет — запустите /sync после входа моста.")
                return
            lines = [f"{i}. {c['title']} (участников: {c['members_count'] or '?'}), "
                     f"сообщений: {c['msg_count']}, обновлён: {_fmt_time(int(c['last_synced'] or 0))}"
                     for i, c in enumerate(chats, 1)]
            await self.bot.send_text(chat_id, "Школьные чаты:\n" + "\n".join(lines))
            return

        if text.startswith("/chat "):
            query = text[6:].strip()
            chat = self.store.find_chat(query)
            if chat is None:
                titles = self.store.chat_titles_ambiguous(query)
                hint = ("несколько совпадений: " + "; ".join(titles)) if titles else "чат не найден, см. /chats"
                await self.bot.send_text(chat_id, f"Не понял: {hint}")
                return
            msgs = self.store.last_messages(chat["chat_id"], limit=15)
            if not msgs:
                await self.bot.send_text(chat_id, f"«{chat['title']}»: сообщений в базе нет, попробуйте /sync")
                return
            body = "\n".join(f"[{_fmt_time(m['created_at'])}] {m['user_name']}: {m['text'][:200]}"
                             for m in msgs)
            await self.bot.send_text(chat_id, f"«{chat['title']}», последние сообщения:\n\n{body}")
            return

        if text.startswith("/find "):
            query = text[6:].strip()
            found = self.store.search_messages(query, limit=10)
            if not found:
                await self.bot.send_text(chat_id, f"По «{query}» ничего не найдено.")
                return
            body = "\n".join(f"[{_fmt_time(m['created_at'])}] {m['chat_title']} — {m['user_name']}: {m['text'][:150]}"
                             for m in found)
            await self.bot.send_text(chat_id, f"Найдено по «{query}»:\n\n{body}")
            return

        if text.startswith("/sync"):
            await self.bot.send_text(chat_id, "Синхронизирую...")
            await self.bot.send_text(chat_id, await self.do_sync())
            return

        if text.startswith("/status"):
            ok = _fmt_time(int(self._last_sync_ok)) if self._last_sync_ok else "никогда"
            err = f"\nПоследняя ошибка: {self._last_sync_error}" if self._last_sync_error else ""
            await self.bot.send_text(chat_id, f"База: {self.store.stats()}\nПоследняя синхронизация: {ok}{err}")
            return

        # free-form text: hint at commands (LLM routing comes at stage 3)
        await self.bot.send_text(chat_id, "Пока понимаю только команды.\n\n" + HELP)

    # ------------------------------------------------------------- main --
    async def run(self) -> None:
        name = await self.bot.check()
        log.info("интерфейс %s готов", name)
        await asyncio.gather(
            self.bot.poll_updates(self.handle_message),
            self.sync_loop(),
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    app = Application()
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
