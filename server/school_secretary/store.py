"""SQLite storage: chats, messages, bot users."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Optional


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS chats(
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                title_lower TEXT,
                chat_type TEXT,
                members_count INTEGER,
                last_synced REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS messages(
                chat_id INTEGER,
                message_id INTEGER,
                user_id INTEGER,
                user_name TEXT,
                text TEXT,
                text_lower TEXT,
                created_at INTEGER,
                PRIMARY KEY(chat_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_messages_time ON messages(created_at);
            CREATE TABLE IF NOT EXISTS bot_users(
                max_user_id INTEGER PRIMARY KEY,
                name TEXT,
                role TEXT DEFAULT 'guest',
                created_at REAL
            );
            """
        )
        self.db.commit()

    # ------------------------------------------------------------ users --
    def bind_owner(self, max_user_id: int, name: str) -> str:
        """First user to /start becomes the owner; others stay guests."""
        row = self.db.execute(
            "SELECT * FROM bot_users WHERE max_user_id=?", (max_user_id,)
        ).fetchone()
        if row:
            return row["role"]
        role = "owner" if self.db.execute("SELECT COUNT(*) c FROM bot_users WHERE role='owner'").fetchone()["c"] == 0 else "guest"
        self.db.execute(
            "INSERT INTO bot_users(max_user_id, name, role, created_at) VALUES(?,?,?,?)",
            (max_user_id, name, role, time.time()),
        )
        self.db.commit()
        return role

    def is_owner(self, max_user_id: int) -> bool:
        row = self.db.execute(
            "SELECT role FROM bot_users WHERE max_user_id=?", (max_user_id,)
        ).fetchone()
        return bool(row and row["role"] == "owner")

    def is_allowed(self, max_user_id: int) -> bool:
        row = self.db.execute(
            "SELECT role FROM bot_users WHERE max_user_id=?", (max_user_id,)
        ).fetchone()
        return bool(row and row["role"] in ("owner", "member"))

    def owner_id(self) -> Optional[int]:
        row = self.db.execute("SELECT max_user_id FROM bot_users WHERE role='owner'").fetchone()
        return row["max_user_id"] if row else None

    def set_role(self, max_user_id: int, role: str) -> bool:
        cur = self.db.execute(
            "UPDATE bot_users SET role=? WHERE max_user_id=?", (role, max_user_id)
        )
        self.db.commit()
        return cur.rowcount > 0

    def user_name(self, max_user_id: int) -> str:
        row = self.db.execute(
            "SELECT name FROM bot_users WHERE max_user_id=?", (max_user_id,)
        ).fetchone()
        return row["name"] if row else "?"

    def list_users(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT max_user_id, name, role FROM bot_users ORDER BY role, name"
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ chats --
    def upsert_chat(self, chat_id: int, title: str, chat_type: str, members_count: int) -> None:
        self.db.execute(
            """INSERT INTO chats(chat_id, title, title_lower, chat_type, members_count)
               VALUES(?,?,?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET
                 title=excluded.title, title_lower=excluded.title_lower,
                 chat_type=excluded.chat_type, members_count=excluded.members_count""",
            (chat_id, title, title.lower(), chat_type, members_count),
        )
        self.db.commit()

    def mark_synced(self, chat_id: int) -> None:
        self.db.execute("UPDATE chats SET last_synced=? WHERE chat_id=?", (time.time(), chat_id))
        self.db.commit()

    def purge_except(self, keep_ids: set[int]) -> int:
        """Drop chats (and their messages) not in keep_ids; returns count removed."""
        rows = self.db.execute("SELECT chat_id FROM chats").fetchall()
        drop = [r["chat_id"] for r in rows if r["chat_id"] not in keep_ids]
        for chat_id in drop:
            self.db.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
            self.db.execute("DELETE FROM chats WHERE chat_id=?", (chat_id,))
        self.db.commit()
        return len(drop)

    def list_chats(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT c.chat_id, c.title, c.members_count, c.last_synced,
                      (SELECT COUNT(*) FROM messages m WHERE m.chat_id=c.chat_id) AS msg_count,
                      (SELECT MAX(created_at) FROM messages m WHERE m.chat_id=c.chat_id) AS last_at
               FROM chats c ORDER BY c.title"""
        ).fetchall()
        return [dict(r) for r in rows]

    def find_chat(self, query: str) -> Optional[dict[str, Any]]:
        """Chat by exact id, or by case-insensitive title substring (unique match)."""
        if query.isdigit():
            row = self.db.execute("SELECT * FROM chats WHERE chat_id=?", (int(query),)).fetchone()
            if row:
                return dict(row)
        rows = self.db.execute(
            "SELECT * FROM chats WHERE title_lower LIKE ? ORDER BY title",
            (f"%{query.lower()}%",),
        ).fetchall()
        if len(rows) == 1:
            return dict(rows[0])
        return None

    def chat_titles_ambiguous(self, query: str) -> list[str]:
        rows = self.db.execute(
            "SELECT title FROM chats WHERE title_lower LIKE ? ORDER BY title",
            (f"%{query.lower()}%",),
        ).fetchall()
        return [r["title"] for r in rows]

    # --------------------------------------------------------- messages --
    def upsert_message(self, chat_id: int, message_id: int, user_id: int,
                       user_name: str, text: str, created_at: int) -> None:
        self.db.execute(
            """INSERT INTO messages(chat_id, message_id, user_id, user_name, text, text_lower, created_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(chat_id, message_id) DO NOTHING""",
            (chat_id, message_id, user_id, user_name, text, text.lower(), created_at),
        )

    def commit_messages(self) -> None:
        self.db.commit()

    def last_messages(self, chat_id: int, limit: int = 15) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT user_name, text, created_at FROM messages
               WHERE chat_id=? AND text IS NOT NULL AND text != ''
               ORDER BY created_at DESC LIMIT ?""",
            (chat_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def messages_since(self, since_ms: int, chat_id: Optional[int] = None) -> list[dict[str, Any]]:
        """Messages newer than since_ms (milliseconds), oldest first, with chat titles."""
        q = ("""SELECT m.user_name, m.text, m.created_at, c.chat_id, c.title AS chat_title
                FROM messages m JOIN chats c ON c.chat_id=m.chat_id
                WHERE m.created_at >= ?""")
        params: list[Any] = [since_ms]
        if chat_id is not None:
            q += " AND m.chat_id=?"
            params.append(chat_id)
        q += " ORDER BY m.created_at"
        rows = self.db.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def search_messages(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT m.user_name, m.text, m.created_at, c.title AS chat_title
               FROM messages m JOIN chats c ON c.chat_id=m.chat_id
               WHERE m.text_lower LIKE ? AND m.text IS NOT NULL
               ORDER BY m.created_at DESC LIMIT ?""",
            (f"%{query.lower()}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> str:
        chats = self.db.execute("SELECT COUNT(*) c FROM chats").fetchone()["c"]
        msgs = self.db.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
        return f"чаты: {chats}, сообщения: {msgs}"
