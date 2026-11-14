"""One-time interactive login for the userbot profile.

Usage:
  python -m school_secretary.login          # SMS: код и 2FA-пароль в консоли
  python -m school_secretary.login --qr     # QR-код в консоли, сканируете приложением MAX

Phone is taken from .env (BRIDGE_PHONE). The session then persists on disk
and survives restarts; no repeat login needed.
"""

from __future__ import annotations

import argparse
import asyncio

from .bridge import login_qr, login_sms
from .config import Config


def main() -> None:
    parser = argparse.ArgumentParser(description="Разовый вход userbot-профиля MAX")
    parser.add_argument("--qr", action="store_true", help="вход по QR-коду вместо SMS")
    args = parser.parse_args()

    cfg = Config()
    cfg.ensure_dirs()
    if args.qr:
        asyncio.run(login_qr(cfg))
    else:
        asyncio.run(login_sms(cfg))


if __name__ == "__main__":
    main()
