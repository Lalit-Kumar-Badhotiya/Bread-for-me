"""Notifications: console + log file + Windows toast (plyer) + optional Telegram.

Telegram token/chat_id come from .env or settings notify block.
"""
import datetime as dt
import os
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "data" / "logs"


class Notifier:
    def __init__(self, settings: dict):
        n = settings.get("notify", {}) or {}
        self.tg_token = os.getenv("TELEGRAM_BOT_TOKEN") or n.get("telegram_bot_token") or ""
        self.tg_chat = os.getenv("TELEGRAM_CHAT_ID") or n.get("telegram_chat_id") or ""
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.log_path = LOG_DIR / "notify.log"

    def send(self, title: str, message: str = "", toast: bool = False):
        line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {title} {message}".strip()
        print(line)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if toast:
            self._toast(title, message)
        if self.tg_token and self.tg_chat:
            self._telegram(f"{title}\n{message}")

    # ---------- providers (all failures are non-fatal) ----------
    @staticmethod
    def _toast(title: str, message: str):
        try:
            from plyer import notification
            # Windows balloon tips cap strings; plyer crashes on overflow.
            notification.notify(
                title=("ApplyBot — " + title)[:60],
                message=message[:60],
                app_name="applybot",
                timeout=10,
            )
        except Exception:
            pass  # desktop notifications unavailable; console/log still have it

    def _telegram(self, text: str):
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                json={"chat_id": self.tg_chat, "text": text},
                timeout=10,
            )
        except Exception:
            pass
