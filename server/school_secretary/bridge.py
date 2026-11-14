"""Userbot bridge over PyMax: READ-ONLY access to group chats.

The bridge never sends anything from the personal account — writing is
the official bot's job. Sessions persist under profiles/<name>/.

Lifecycle (per PyMax docs and reference projects):
- one-time login: client with sms_code_provider / qr_provider, run start()
  until the on_start callback fires, then stop(); session lands on disk;
- runtime: client without providers, start() as a background task, wait
  for the ready event, then call fetch_chats/fetch_history.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from pymax import Client, ConsolePasswordProvider, ConsoleQrHandler, ConsoleSmsCodeProvider, WebClient

from .config import Config
from .store import Store

log = logging.getLogger("bridge")

GROUP_TYPES = {"chat", "group", "channel"}
SESSION_NAME = "session.db"


def _is_school_chat(title: str, cfg: Config) -> bool:
    if not cfg.school_filter:
        return True
    t = (title or "").lower()
    return any(f in t for f in cfg.school_filter)


def _profile_dir(cfg: Config, profile: str):
    d = cfg.profiles_dir / profile
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_runtime_client(cfg: Config, profile: str = "default") -> Client:
    """Client for an already-logged-in profile (session on disk)."""
    return Client(
        phone=cfg.bridge_phone,
        work_dir=str(_profile_dir(cfg, profile)),
        session_name=SESSION_NAME,
    )


async def login_sms(cfg: Config, profile: str = "default") -> None:
    """One-time interactive SMS login (code and 2FA password in console)."""
    if not cfg.bridge_phone:
        raise SystemExit("BRIDGE_PHONE не задан в server/.env")
    client = Client(
        phone=cfg.bridge_phone,
        work_dir=str(_profile_dir(cfg, profile)),
        session_name=SESSION_NAME,
        sms_code_provider=ConsoleSmsCodeProvider(),
        password_provider=ConsolePasswordProvider(),
    )
    await _run_login(client)


async def login_qr(cfg: Config, profile: str = "default") -> None:
    """One-time QR login: console shows the QR, scan it with the MAX app."""
    client = WebClient(
        work_dir=str(_profile_dir(cfg, profile)),
        session_name=SESSION_NAME,
        qr_provider=ConsoleQrHandler(),
    )
    await _run_login(client)


async def _run_login(client: Any) -> None:
    logged_in = asyncio.Event()

    @client.on_start()
    async def _ready(current: Any) -> None:
        me = getattr(current, "me", None)
        contact = getattr(me, "contact", me)
        print(f"Вход выполнен: {getattr(contact, 'first_name', '?')} "
              f"(id={getattr(contact, 'id', getattr(me, 'user_id', '?'))})")
        logged_in.set()
        await current.stop()

    try:
        await client.start()
    except asyncio.CancelledError:
        # PyMax may cancel its reader on stop(); fine only after login confirmed.
        if not logged_in.is_set():
            raise
    if not logged_in.is_set():
        raise RuntimeError("Клиент MAX завершился до подтверждения входа")


async def start_client(client: Any, timeout: float = 30.0) -> asyncio.Task:
    """Start a runtime client as a background task; wait until ready."""
    ready = asyncio.Event()

    @client.on_start()
    async def _on_ready(_client: Any) -> None:
        ready.set()

    task = asyncio.create_task(client.start(), name="pymax-client")
    try:
        await asyncio.wait_for(ready.wait(), timeout=timeout)
    except TimeoutError as e:
        await _stop_client(client, task)
        raise RuntimeError(f"Клиент MAX не стал готов за {timeout:g} с") from e
    return task


async def _stop_client(client: Any, task: asyncio.Task) -> None:
    await asyncio.gather(client.stop(), return_exceptions=True)
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def sync_chats(client: Any, store: Store, cfg: Config) -> int:
    """Fetch chat list + recent history for school chats into the store."""
    synced = 0
    marker: Optional[int] = None
    chats: list[Any] = []
    while True:
        page = await client.fetch_chats(marker=marker) or []
        if not page:
            break
        chats.extend(page)
        new_marker = getattr(page[-1], "marker", None)
        if new_marker is None or new_marker == marker:
            break
        marker = new_marker

    for chat in chats:
        ctype_raw = getattr(chat, "chat_type", None) or getattr(chat, "type", "")
        ctype = str(getattr(ctype_raw, "value", ctype_raw) or "").lower()
        title = getattr(chat, "title", None) or getattr(chat, "name", "") or "(без названия)"
        members = getattr(chat, "participants_count", None) or getattr(chat, "members_count", 0) or 0
        is_group = ctype in GROUP_TYPES or bool(getattr(chat, "is_group", False))
        if not is_group or not _is_school_chat(title, cfg):
            continue
        store.upsert_chat(chat.chat_id, title, ctype, int(members))
        history = await client.fetch_history(chat.chat_id, backward=cfg.history_depth)
        count = 0
        for m in history:
            sender = getattr(m, "sender", None)
            uid = getattr(sender, "user_id", None) or getattr(m, "sender_id", 0) or 0
            uname = (getattr(sender, "name", None) or getattr(sender, "username", None)
                     or getattr(m, "sender_name", "") or "?")
            text = getattr(m, "text", None) or ""
            mid = getattr(m, "message_id", None) or getattr(m, "id", 0)
            created = int(getattr(m, "created_at", 0) or 0)
            if text and mid:
                store.upsert_message(chat.chat_id, int(mid), int(uid or 0), str(uname), text, created)
                count += 1
        store.commit_messages()
        store.mark_synced(chat.chat_id)
        synced += 1
        log.info("синхронизирован чат «%s» (+%d сообщений)", title, count)
    return synced
