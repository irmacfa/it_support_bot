"""
اعضای دیده‌شده‌ی هر گروه (کسایی که حداقل یه‌بار توی گروه پیام دادن).
API ربات بله لیست کامل اعضای گروه رو نمی‌ده، پس فقط کسایی که پیام فرستادن
قابل شناسایی‌ان. توی یه فایل JSON دائمی ذخیره می‌شه.
"""
import json
import threading
from datetime import datetime
from app import config

_lock = threading.Lock()
STORE_PATH = config.DATA_DIR / "group_members.json"

# chat_id(str) -> {user_id(str): {...}}
_members: dict = {}


def _load() -> None:
    global _members
    if STORE_PATH.exists():
        try:
            with open(STORE_PATH, encoding="utf-8") as f:
                _members = json.load(f)
        except Exception:
            _members = {}


def _save() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(_members, f, ensure_ascii=False, indent=2)


def record(chat_id, user_id, display_name: str, username: str = "") -> None:
    if not chat_id or not user_id:
        return
    ckey = str(chat_id)
    ukey = str(user_id)
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        _load()
        chat_members = _members.setdefault(ckey, {})
        chat_members[ukey] = {
            "user_id": user_id,
            "display_name": display_name,
            "username": username,
            "last_seen": now,
        }
        _save()


def list_for_chat(chat_id) -> list[dict]:
    with _lock:
        _load()
        return list(_members.get(str(chat_id), {}).values())


_load()
