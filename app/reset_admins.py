"""
مدیریت لیست مسئولین ریست رمز (کسایی که درخواست ریست رمز فروشگاه/ستادی رو پیوی دریافت می‌کنن).
از طریق پنل وب (/reset-admins) قابل افزودن و حذفه؛ توی یه فایل JSON دائمی ذخیره می‌شه.
"""
import json
import threading
from datetime import datetime
from app import config

_lock = threading.Lock()
STORE_PATH = config.DATA_DIR / "reset_admins.json"

_admins: dict[str, dict] = {}


def _load() -> None:
    global _admins
    if STORE_PATH.exists():
        try:
            with open(STORE_PATH, encoding="utf-8") as f:
                _admins = json.load(f)
        except Exception:
            _admins = {}


def _save() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(_admins, f, ensure_ascii=False, indent=2)


def list_all() -> list[dict]:
    with _lock:
        _load()
        return list(_admins.values())


def get_ids() -> list[str]:
    with _lock:
        _load()
        return list(_admins.keys())


def add(user_id, name: str = "") -> dict:
    user_id = str(user_id).strip()
    record = {
        "user_id": user_id,
        "name": name,
        "added_at": datetime.now().isoformat(timespec="seconds"),
    }
    with _lock:
        _admins[user_id] = record
        _save()
    return record


def remove(user_id) -> None:
    user_id = str(user_id).strip()
    with _lock:
        _admins.pop(user_id, None)
        _save()


_load()