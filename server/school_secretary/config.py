"""Configuration from .env (server directory)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv() -> None:
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


@dataclass
class Config:
    bot_token: str = field(default_factory=lambda: os.getenv("MAX_BOT_TOKEN", ""))
    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    bridge_phone: str = field(default_factory=lambda: os.getenv("BRIDGE_PHONE", ""))
    school_filter: list[str] = field(default_factory=lambda: [
        s.strip().lower() for s in os.getenv("SCHOOL_CHAT_FILTER", "").split(",") if s.strip()
    ])
    history_depth: int = field(default_factory=lambda: int(os.getenv("SYNC_HISTORY_DEPTH", "50")))

    # Paths
    base_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent)
    db_path: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent / "data" / "secretary.db")
    profiles_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent / "profiles")

    def validate_bot(self) -> None:
        if not self.bot_token and not self.telegram_token:
            raise SystemExit("Ни MAX_BOT_TOKEN, ни TELEGRAM_BOT_TOKEN не заданы в server/.env")

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
