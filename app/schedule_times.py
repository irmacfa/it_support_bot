"""
ساعت‌های ارسال خودکار «اطلاعیه شیفت فردا»؛ از پنل وب (/schedule-times) قابل افزودن و حذفه.
پیش‌فرض: 15:00 و 23:00.
"""
import json
import re
import threading
from app import config

_lock = threading.Lock()
STORE_PATH = config.DATA_DIR / "schedule_times.json"

_DEFAULT_TIMES = ["15:00", "23:00"]
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

_times: list[str] = []


def _load() -> None:
    global _times
    if STORE_PATH.exists():
        try:
            with open(STORE_PATH, encoding="utf-8") as f:
                _times = json.load(f)
        except Exception:
            _times = list(_DEFAULT_TIMES)
    else:
        _times = list(_DEFAULT_TIMES)


def _save() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(_times, f, ensure_ascii=False, indent=2)


def list_all() -> list[str]:
    with _lock:
        _load()
        return sorted(_times)


def add(time_str: str) -> tuple[bool, str]:
    time_str = time_str.strip()
    if not _TIME_RE.match(time_str):
        return False, "فرمت ساعت باید به شکل HH:MM باشه (مثلاً 21:30)."
    with _lock:
        _load()
        if time_str in _times:
            return False, "این ساعت قبلاً اضافه شده."
        _times.append(time_str)
        _save()
    return True, f"ساعت {time_str} اضافه شد."


def remove(time_str: str) -> None:
    with _lock:
        _load()
        if time_str in _times:
            _times.remove(time_str)
            _save()


_load()
