"""
آمار استفاده از قابلیت‌های ربات: کدوم کاربرها از چه امکاناتی (شارژ/اطلاعات/شیفت)
استفاده کردن و کدوم هنوز هیچ استفاده‌ای نکردن، و مسئولین شارژ چقدر و چطور به
درخواست‌ها پاسخ دادن. علاوه بر مجموع کلی، یه ریز روزانه (تاریخ جلالی) هم نگه
می‌داریم تا بشه گزارش رو برای یه بازه‌ی دلخواه (مثلاً یه هفته) هم گرفت.
توی یه فایل JSON دائمی ذخیره می‌شه.
"""
import json
import threading
from datetime import datetime
import jdatetime
from app import config

_lock = threading.Lock()
STORE_PATH = config.DATA_DIR / "usage_stats.json"

_stats: dict = {"users": {}, "charge_admins": {}, "daily": {}, "codes": {}, "daily_codes": {}}


def _load() -> None:
    global _stats
    if STORE_PATH.exists():
        try:
            with open(STORE_PATH, encoding="utf-8") as f:
                _stats = json.load(f)
        except Exception:
            _stats = {"users": {}, "charge_admins": {}, "reset_admins": {}, "daily": {}, "codes": {}, "daily_codes": {}}
    _stats.setdefault("users", {})
    _stats.setdefault("charge_admins", {})
    _stats.setdefault("reset_admins", {})
    _stats.setdefault("daily", {})
    _stats.setdefault("codes", {})
    _stats.setdefault("daily_codes", {})


def _save() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(_stats, f, ensure_ascii=False, indent=2)


def record_action(user_id, action: str) -> None:
    """یه بار استفاده‌ی واقعی از یه قابلیت (charge/info/shift) رو برای کاربر ثبت می‌کنه."""
    if not user_id:
        return
    key = str(user_id)
    now = datetime.now().isoformat(timespec="seconds")
    day_key = jdatetime.date.today().strftime("%Y/%m/%d")
    with _lock:
        _load()
        user = _stats["users"].setdefault(key, {"total": 0, "actions": {}, "last_action_at": None})
        user["total"] += 1
        user["actions"][action] = user["actions"].get(action, 0) + 1
        user["last_action_at"] = now

        day_bucket = _stats["daily"].setdefault(day_key, {})
        user_day = day_bucket.setdefault(key, {})
        user_day[action] = user_day.get(action, 0) + 1

        _save()


def record_code_lookup(code: str, action: str) -> None:
    """یه بار جستجوی یه کد فروشگاه (charge/info) رو ثبت می‌کنه، برای گزارش پرتقاضاترین فروشگاه‌ها."""
    if not code:
        return
    now = datetime.now().isoformat(timespec="seconds")
    day_key = jdatetime.date.today().strftime("%Y/%m/%d")
    with _lock:
        _load()
        entry = _stats["codes"].setdefault(code, {"total": 0, "actions": {}, "last_requested_at": None})
        entry["total"] += 1
        entry["actions"][action] = entry["actions"].get(action, 0) + 1
        entry["last_requested_at"] = now

        day_bucket = _stats["daily_codes"].setdefault(day_key, {})
        code_day = day_bucket.setdefault(code, {})
        code_day[action] = code_day.get(action, 0) + 1

        _save()


def record_charge_response(admin_id, action: str, elapsed_seconds: float) -> None:
    """پاسخ یه مسئول شارژ به یه درخواست رو ثبت می‌کنه (برای مقایسه‌ی میزان فعالیت‌شون)."""
    if not admin_id:
        return
    key = str(admin_id)
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        _load()
        admin = _stats["charge_admins"].setdefault(key, {
            "responded_count": 0,
            "done_count": 0,
            "noissue_count": 0,
            "total_response_seconds": 0.0,
            "last_response_at": None,
        })
        admin["responded_count"] += 1
        if action == "done":
            admin["done_count"] += 1
        else:
            admin["noissue_count"] += 1
        admin["total_response_seconds"] += max(elapsed_seconds, 0)
        admin["last_response_at"] = now
        _save()


def record_reset_response(admin_id, action: str, elapsed_seconds: float) -> None:
    """پاسخ مسئول ریست رمز به یه درخواست رو ثبت می‌کنه (ریست شد / مشخصات اشتباه بود)."""
    if not admin_id:
        return
    key = str(admin_id)
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        _load()
        admin = _stats["reset_admins"].setdefault(key, {
            "responded_count": 0,
            "done_count": 0,
            "wrong_count": 0,
            "total_response_seconds": 0.0,
            "last_response_at": None,
        })
        admin["responded_count"] += 1
        if action == "done":
            admin["done_count"] += 1
        else:
            admin["wrong_count"] += 1
        admin["total_response_seconds"] += max(elapsed_seconds, 0)
        admin["last_response_at"] = now
        _save()


def all_user_stats() -> dict:
    with _lock:
        _load()
        return dict(_stats["users"])


def all_charge_admin_stats() -> dict:
    with _lock:
        _load()
        return dict(_stats["charge_admins"])


def all_reset_admin_stats() -> dict:
    with _lock:
        _load()
        return dict(_stats["reset_admins"])


def range_user_stats(from_date: str, to_date: str) -> dict[str, dict]:
    """آمار استفاده‌ی هر کاربر رو فقط برای بازه‌ی [from_date, to_date] برمی‌گردونه
    (تاریخ‌های جلالی به فرمت YYYY/MM/DD، هر دو سر بازه شامل می‌شن).
    خروجی: user_id -> {"total": n, "actions": {...}}"""
    with _lock:
        _load()
        daily = _stats["daily"]
        result: dict[str, dict] = {}
        for day, day_bucket in daily.items():
            if not (from_date <= day <= to_date):
                continue
            for uid, actions in day_bucket.items():
                u = result.setdefault(uid, {"total": 0, "actions": {}})
                for action, cnt in actions.items():
                    u["actions"][action] = u["actions"].get(action, 0) + cnt
                    u["total"] += cnt
        return result


def all_code_stats() -> dict:
    with _lock:
        _load()
        return dict(_stats["codes"])


def range_code_stats(from_date: str, to_date: str) -> dict[str, dict]:
    """آمار جستجوی کد فروشگاه‌ها رو فقط برای بازه‌ی [from_date, to_date] برمی‌گردونه.
    خروجی: code -> {"total": n, "actions": {...}}"""
    with _lock:
        _load()
        daily_codes = _stats["daily_codes"]
        result: dict[str, dict] = {}
        for day, day_bucket in daily_codes.items():
            if not (from_date <= day <= to_date):
                continue
            for code, actions in day_bucket.items():
                e = result.setdefault(code, {"total": 0, "actions": {}})
                for action, cnt in actions.items():
                    e["actions"][action] = e["actions"].get(action, 0) + cnt
                    e["total"] += cnt
        return result


_load()
