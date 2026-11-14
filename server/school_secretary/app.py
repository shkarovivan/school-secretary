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
    "/today — сводка сообщений за сегодня\n"
    "/chats — список школьных чатов\n"
    "/chat <номер или название> — последние сообщения\n"
    "/find <текст> — поиск по истории чатов\n"
    "/sync — обновить данные из MAX\n"
    "/status — состояние системы\n"
    "/users — пользователи (владелец)\n"
    "/allow <id> — открыть доступ (владелец)"
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

    # ----------------------------------------------------------- digest --
    SYSTEM_SUMMARIZER = (
        "Ты — школьный секретарь семьи. Тебе дают переписку школьных чатов за день. "
        "Составь краткую сводку на русском:\n"
        "1) по каждому чату — 1–3 пункта о главном (факты, а не пересказ подряд);\n"
        "2) отдельным блоком «Дедлайны и действия» — всё, что требуют от родителей "
        "(деньги, формы, документы, даты, мероприятия);\n"
        "3) «Вопросы без ответа» — если есть.\n"
        "Пиши только на основе сообщений, ничего не выдумывай, без вводных фраз."
    )

    async def build_today_digest(self, chat_query: str = "") -> str:
        """LLM summary of today's chat messages; deterministic fallback."""
        day_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        since_ms = int(day_start.timestamp() * 1000)

        chat_id = None
        chat_title_filter = ""
        if chat_query:
            chat = self.store.find_chat(chat_query)
            if chat is None:
                titles = self.store.chat_titles_ambiguous(chat_query)
                hint = ("несколько совпадений: " + "; ".join(titles)) if titles else "чат не найден, см. /chats"
                return f"Не понял: {hint}"
            chat_id = chat["chat_id"]
            chat_title_filter = chat["title"]

        msgs = self.store.messages_since(since_ms, chat_id)
        if not msgs:
            return ("Сегодня новых сообщений " +
                    (f"в «{chat_title_filter}» " if chat_id else "") + "нет. Попробуйте /sync, если давно не обновляли.")

        llm = self.cfg.llm()
        if llm.configured():
            transcript = self._build_transcript(msgs)
            try:
                summary = await llm.complete(
                    self.SYSTEM_SUMMARIZER,
                    f"Дата: {day_start.strftime('%d.%m.%Y')}\n\nПереписка за день:\n\n{transcript}",
                )
                return f"🤖 Сводка за {day_start.strftime('%d.%m')}\n\n{summary}"
            except Exception as e:  # noqa: BLE001 - fall back to deterministic digest
                log.exception("LLM-сводка не удалась, откат на обычную")
                return (f"(LLM не ответил: {e}; показываю обычную сводку)\n\n"
                        + self._deterministic_digest(msgs, day_start, chat_title_filter))
        return self._deterministic_digest(msgs, day_start, chat_title_filter)

    @staticmethod
    def _build_transcript(msgs: list[dict[str, Any]], budget: int = 6000) -> str:
        """Compact transcript for the LLM, roughly capped by characters."""
        lines: list[str] = []
        total = 0
        for m in msgs:
            t = datetime.fromtimestamp(m["created_at"] / 1000).strftime("%H:%M")
            text = (m["text"] or "").replace("\n", " ")[:220]
            line = f"[{t}] {m['chat_title']} | {m['user_name']}: {text}"
            if total + len(line) > budget:
                lines.append("…(обрезано по бюджету)")
                break
            lines.append(line)
            total += len(line) + 1
        return "\n".join(lines)

    @staticmethod
    def _deterministic_digest(msgs: list[dict[str, Any]], day_start, chat_title_filter: str) -> str:
        by_chat: dict[int, dict[str, Any]] = {}
        for m in msgs:
            g = by_chat.setdefault(m["chat_id"], {"title": m["chat_title"], "items": []})
            g["items"].append(m)
        head = (f"📅 {day_start.strftime('%d.%m.%Y')}, сообщений за день: {len(msgs)}"
                + (f" (чат «{chat_title_filter}»)" if chat_title_filter else ""))
        SHOW_PER_CHAT = 12
        parts = [head]
        for g in by_chat.values():
            items = g["items"]
            lines = [f"\n🗞 {g['title']} ({len(items)})"]
            for m in items[-SHOW_PER_CHAT:]:
                t = datetime.fromtimestamp(m["created_at"] / 1000).strftime("%H:%M")
                text = (m["text"] or "").replace("\n", " ")[:200]
                lines.append(f"{t} {m['user_name']}: {text}")
            if len(items) > SHOW_PER_CHAT:
                lines.append(f"…и ещё {len(items) - SHOW_PER_CHAT} (см. /chat)")
            parts.append("\n".join(lines))
        return "\n".join(parts)

    # -------------------------------------------------------------- bot --
    async def handle_message(self, msg: dict[str, Any]) -> None:
        chat_id, user_id, user_name, text = (
            msg["chat_id"], msg["user_id"], msg["user_name"], msg["text"].strip()
        )
        if not text:
            return

        if text.startswith("/start"):
            role = self.store.bind_owner(user_id, user_name)
            if role == "owner":
                await self.bot.send_text(chat_id, f"Здравствуйте, {user_name}! Вы — владелец секретаря.\n\n{HELP}")
            elif role == "member":
                await self.bot.send_text(chat_id, f"Здравствуйте, {user_name}! Доступ открыт.\n\n{HELP}")
            else:
                # Guest: ask the owner to approve, with a ready-to-copy command.
                owner = self.store.owner_id()
                await self.bot.send_text(chat_id, "Вы не в списке. Запрос на доступ отправлен владельцу.")
                if owner:
                    await self.bot.send_text(
                        owner,
                        f"Запрос доступа: {user_name} (id={user_id}).\nОткрыть: /allow {user_id}",
                    )
            return
        if text.startswith("/help"):
            await self.bot.send_text(chat_id, HELP)
            return
        if text.startswith("/allow"):
            if not self.store.is_owner(user_id):
                await self.bot.send_text(chat_id, "Доступ раздаёт только владелец.")
                return
            arg = text[6:].strip().lstrip("@")
            if not arg.isdigit():
                await self.bot.send_text(chat_id, "Формат: /allow <id> (id приходит в уведомлении о запросе)")
                return
            target = int(arg)
            if not self.store.set_role(target, "member"):
                await self.bot.send_text(chat_id, f"Пользователь {target} ещё не писал боту — не могу открыть.")
                return
            await self.bot.send_text(chat_id, f"Готово: {self.store.user_name(target)} (id={target}) получил доступ.")
            await self.bot.send_text(target, f"Вам открыт доступ к школьному секретарю!\n\n{HELP}")
            return
        if text.startswith("/users"):
            if not self.store.is_owner(user_id):
                await self.bot.send_text(chat_id, "Только для владельца.")
                return
            lines = [f"{u['name']} (id={u['max_user_id']}) — {u['role']}" for u in self.store.list_users()]
            await self.bot.send_text(chat_id, "Пользователи:\n" + "\n".join(lines))
            return
        if not self.store.is_allowed(user_id):
            await self.bot.send_text(chat_id, "Доступ только у владельца и одобренных членов семьи.")
            return

        if text.startswith("/today"):
            await self.bot.send_text(chat_id, await self.build_today_digest(text[6:].strip()))
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
        set_commands = getattr(self.bot, "set_commands", None)
        if set_commands is not None:
            await set_commands([
                ("today", "сводка сообщений за сегодня"),
                ("chats", "список школьных чатов"),
                ("chat", "последние сообщения чата: /chat <название>"),
                ("find", "поиск по истории чатов: /find <текст>"),
                ("sync", "обновить данные из MAX"),
                ("status", "состояние системы"),
                ("users", "пользователи (владелец)"),
                ("allow", "открыть доступ: /allow <id> (владелец)"),
                ("help", "все команды"),
            ])
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
