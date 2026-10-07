"""
ثبت‌نام کاربرهایی که برای اولین‌بار پیوی ربات رو پیام می‌دن.
اطلاعات‌شون توی یه فایل JSON روی دیسک ذخیره میشه تا با ری‌استارت ربات از بین نره.
کاربران جدید بعد از ثبت‌نام باید توسط ادمین تایید بشن.
"""
import json
import threading
from datetime import datetime
from app import config

_lock = threading.Lock()
REGISTRY_PATH = config.DATA_DIR / "registered_users.json"

_registered: dict[str, dict] = {}


def _load() -> None:
    global _registered
    if REGISTRY_PATH.exists():
        try:
            with open(REGISTRY_PATH, encoding="utf-8") as f:
                _registered = json.load(f)
        except Exception:
            _registered = {}


def _save() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(_registered, f, ensure_ascii=False, indent=2)


def is_registered(user_id) -> bool:
    with _lock:
        _load()
        return str(user_id) in _registered


def is_approved(user_id) -> bool:
    """آیا کاربر ثبت‌نام کرده و تایید شده؟"""
    with _lock:
        _load()
        key = str(user_id)
        user = _registered.get(key)
        if not user:
            return False
        return user.get("approved", False)


def register(user_id, full_name: str, from_user: dict) -> dict:
    """کاربر رو ثبت می‌کنه (تایید نشده) و رکورد کامل رو برمی‌گردونه."""
    record = {
        "user_id": user_id,
        "full_name": full_name,
        "username": from_user.get("username") or "",
        "first_name": from_user.get("first_name") or "",
        "last_name": from_user.get("last_name") or "",
        "registered_at": datetime.now().isoformat(timespec="seconds"),
        "approved": False,
    }
    with _lock:
        _registered[str(user_id)] = record
        _save()
    return record


def approve(user_id) -> dict | None:
    """کاربر رو تایید می‌کنه. رکورد آپدیت‌شده رو برمی‌گردونه."""
    with _lock:
        _load()
        key = str(user_id)
        if key not in _registered:
            return None
        _registered[key]["approved"] = True
        _save()
        return _registered[key]


def reject(user_id) -> dict | None:
    """تایید کاربر رو لغو می‌کنه."""
    with _lock:
        _load()
        key = str(user_id)
        if key not in _registered:
            return None
        _registered[key]["approved"] = False
        _save()
        return _registered[key]


def list_all() -> list[dict]:
    with _lock:
        _load()
        return list(_registered.values())


def list_pending() -> list[dict]:
    """کاربرانی که ثبت‌نام کردن ولی هنوز تایید نشدن."""
    with _lock:
        _load()
        return [u for u in _registered.values() if not u.get("approved", False)]


def update(user_id, full_name: str = None, first_name: str = None, last_name: str = None) -> dict | None:
    """اطلاعات کاربر رو به‌روز می‌کنه. فقط فیلدهای non-None آپدیت می‌شن."""
    with _lock:
        _load()
        key = str(user_id)
        if key not in _registered:
            return None
        if full_name is not None:
            _registered[key]["full_name"] = full_name
        if first_name is not None:
            _registered[key]["first_name"] = first_name
        if last_name is not None:
            _registered[key]["last_name"] = last_name
        _save()
        return _registered[key]


def approve_all_existing() -> int:
    """همه کاربران قبلی (که فیلد approved ندارن) رو تایید می‌کنه. تعداد تایید شده‌ها رو برمی‌گردونه."""
    count = 0
    with _lock:
        _load()
        for key, user in _registered.items():
            if "approved" not in user:
                user["approved"] = True
                count += 1
        if count > 0:
            _save()
    return count


_load()
# تایید خودکار کاربران قبلی (که قبل از سیستم تایید ثبت‌نام شده بودن)
approve_all_existing()
