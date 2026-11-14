"""Question-answering agent: free-form questions over chat history.

Classic tool-calling loop: the model picks search tools over the local
store, we execute them and feed results back, up to a few rounds.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from .llm import LLM
from .store import Store

log = logging.getLogger("agent")

SYSTEM = (
    "Ты — школьный секретарь семьи. Отвечаешь на вопросы родителей по истории "
    "школьных чатов MAX, которые хранятся в локальной базе. "
    "Правила:\n"
    "- сначала ищи данные инструментами (можно несколько раз, разными формулировками), "
    "и только потом отвечай;\n"
    "- отвечай по-русски, кратко и по делу, с указанием чата и даты факта "
    "(например: «4Э · Родители, 12.11»);\n"
    "- если данных нет в базе — так и скажи и предложи /sync; ничего не выдумывай;\n"
    "- дедлайны и решения формулируй как список действий."
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_messages",
            "description": "Поиск по истории всех школьных чатов (регистр не важен). Возвращает сообщения с чатом, датой и автором.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "ключевое слово или фраза, например: экскурсия, собрание, деньги"},
                    "limit": {"type": "integer", "description": "сколько результатов (по умолчанию 10)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recent_messages",
            "description": "Последние сообщения всех чатов за последние N часов (по умолчанию 48).",
            "parameters": {
                "type": "object",
                "properties": {"hours": {"type": "integer"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_chats",
            "description": "Список школьных чатов в базе.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

MAX_ROUNDS = 4


def _fmt_msgs(rows: list[dict[str, Any]], limit: int = 15) -> str:
    if not rows:
        return "ничего не найдено"
    lines = []
    for m in rows[:limit]:
        t = datetime.fromtimestamp(m["created_at"] / 1000).strftime("%d.%m %H:%M")
        chat = m.get("chat_title") or "?"
        lines.append(f"[{t}] {chat} | {m['user_name']}: {(m['text'] or '')[:200]}")
    if len(rows) > limit:
        lines.append(f"(и ещё {len(rows) - limit})")
    return "\n".join(lines)


class Agent:
    def __init__(self, llm: LLM, store: Store):
        self.llm = llm
        self.store = store

    async def answer(self, question: str) -> str:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": question},
        ]
        for _ in range(MAX_ROUNDS):
            msg = await self.llm.chat_raw(messages, tools=TOOLS)
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                return (msg.get("content") or "").strip() or "Не смог найти ответ."
            messages.append({"role": "assistant", "content": msg.get("content") or "",
                             "tool_calls": tool_calls})
            for tc in tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except ValueError:
                    args = {}
                result = self._run_tool(name, args)
                messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                 "content": result})
        # exhausted rounds — ask for a final answer without tools
        messages.append({"role": "user",
                         "content": "Инструментов больше не вызывай. Ответь по имеющимся данным."})
        msg = await self.llm.chat_raw(messages)
        return (msg.get("content") or "").strip()

    def _run_tool(self, name: str, args: dict[str, Any]) -> str:
        if name == "search_messages":
            rows = self.store.search_messages(str(args.get("query", "")),
                                              limit=int(args.get("limit", 10)))
            return _fmt_msgs(rows)
        if name == "recent_messages":
            hours = int(args.get("hours", 48))
            since = datetime.now() - timedelta(hours=hours)
            rows = self.store.messages_since(int(since.timestamp() * 1000))
            return _fmt_msgs(rows, limit=30)
        if name == "list_chats":
            chats = self.store.list_chats()
            return "\n".join(f"- {c['title']} (сообщений: {c['msg_count']})" for c in chats) or "база пуста"
        return f"неизвестный инструмент {name}"
