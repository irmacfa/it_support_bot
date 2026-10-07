"""
لیست گروه‌هایی که ربات توشون عضوه، برای ارسال پیام دستی از پنل.
هر بار که پیامی از یه گروه به ربات می‌رسه، اون گروه ثبت/به‌روزرسانی می‌شه.
"""
import json
import threading
from datetime import datetime
from app import config

_lock = threading.Lock()
STORE_PATH = config.DATA_DIR / "known_groups.json"

_groups: dict[str, dict] = {}


def _load() -> None:
    global _groups
    if STORE_PATH.exists():
        try:
            with open(STORE_PATH, encoding="utf-8") as f:
                _groups = json.load(f)
        except Exception:
            _groups = {}


def _save() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(_groups, f, ensure_ascii=False, indent=2)


def record(chat_id, title: str) -> None:
    key = str(chat_id)
    with _lock:
        _load()
        existing = _groups.get(key, {})
        _groups[key] = {
            "chat_id": chat_id,
            "title": title or existing.get("title") or "",
            "last_seen": datetime.now().isoformat(timespec="seconds"),
        }
        _save()


def list_all() -> list[dict]:
    with _lock:
        _load()
        return list(_groups.values())


_load()
