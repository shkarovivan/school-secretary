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

# PyMax ChatType values: DIALOG (private), CHAT (group), CHANNEL
GROUP_TYPES = {"chat", "channel"}
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
    kept_ids: set[int] = set()
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
        ctype_raw = getattr(chat, "type", "")
        ctype = str(getattr(ctype_raw, "value", ctype_raw) or "").lower()
        title = getattr(chat, "title", None) or "(без названия)"
        members = getattr(chat, "participants_count", 0) or 0
        if ctype not in GROUP_TYPES or not _is_school_chat(title, cfg):
            continue
        kept_ids.add(chat.id)
        store.upsert_chat(chat.id, title, ctype, int(members))

        # Sender names: chat participants if present, else a members request.
        # PyMax model: Member.contact (User) -> .id and .names[] (Name.name).
        names: dict[int, str] = {}

        def _collect_members(members: list[Any]) -> None:
            for mem in members or []:
                contact = getattr(mem, "contact", None)
                uid = getattr(contact, "id", None)
                nm = None
                for n in (getattr(contact, "names", None) or []):
                    nm = getattr(n, "name", None) or getattr(n, "first_name", None)
                    if nm:
                        break
                if uid and nm:
                    names[int(uid)] = str(nm)

        _collect_members(getattr(chat, "participants", None))
        if not names:
            try:
                _collect_members(await client.get_chat_members(chat.id, count=100))
            except Exception:  # noqa: BLE001 - names are nice-to-have, not critical
                log.debug("не удалось получить участников %s", title)
        if names:
            store.update_sender_names(chat.id, names)

        history = await client.fetch_history(chat.id, backward=cfg.history_depth)
        count = 0
        for m in history:
            uid = int(getattr(m, "sender", None) or 0)
            uname = names.get(uid) or (f"#{uid}" if uid else "?")
            text = getattr(m, "text", None) or ""
            mid = getattr(m, "id", None)
            created = int(getattr(m, "time", 0) or 0)
            if text and mid:
                store.upsert_message(chat.id, int(mid), uid, uname, text, created)
                count += 1
        store.commit_messages()
        store.mark_synced(chat.id)
        synced += 1
        log.info("синхронизирован чат «%s» (+%d сообщений)", title, count)
    removed = store.purge_except(kept_ids)
    if removed:
        log.info("удалено чатов вне фильтра: %d", removed)
    return synced
