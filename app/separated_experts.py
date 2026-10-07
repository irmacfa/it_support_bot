"""
کارشناس‌های منفک (رفته از سازمان).

وقتی یه کارشناس از همکاران جدا می‌شه، فروشگاه‌هایی که به اسمش تخصیص داده شده بود
عملاً «بدون کارشناس» می‌مونن، چون کسی مسئولیتشون رو نگرفته. ولی توی فایل اکسل
هنوز اسم اون کارشناس توی ستون «کارشناس انفورماتیک» نشسته و آمارِ بی‌کارشناس‌ها
اشتباه درمیاد.

این ماژول اون کارشناس‌ها رو نگه می‌داره تا توی مانیتورینگ، فروشگاه‌هاشون جدا
گزارش بشن و بشه گفت «این فروشگاه‌ها هنوز کارشناس ندارن».

تطبیق اسم عمداً سخاوتمندانه‌ست: «محمد شیری» و «شیری» یه نفر حساب می‌شن.
"""
import json
import re
import threading

from app import config

_lock = threading.Lock()
STORE_PATH = config.DATA_DIR / "separated_experts.json"

# نرمال‌سازی عربی/فارسی تا «شیری» و «محمد شیری» یهی بشن
_CHARS = {
    "ي": "ی",   # ي عربی
    "ى": "ی",   # ى
    "ك": "ک",   # ك عربی
    "‌": " ",        # نیم‌فاصله
    "ـ": "",         # کشیدگی
    "ـ": "",
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ؤ": "و", "ۀ": "ه", "ة": "ه",
}
_PREFIXES = ("محمد", "محمدرضا", "سید", "سیدمحمد")

_records: list[dict] = []


def _norm(text: str) -> str:
    """نرمال‌سازی اسم برای تطبیق: حذف فضا/نیم‌فاصله/کشیدگی و یکسان‌سازی ی و ک."""
    s = str(text or "").strip().lower()
    for src, dst in _CHARS.items():
        s = s.replace(src, dst)
    s = re.sub(r"[\s_.\-()\[\]]+", "", s)
    for prefix in _PREFIXES:
        if s.startswith(prefix) and len(s) > len(prefix):
            s = s[len(prefix):]
            break
    return s


def _surname(text: str) -> str:
    """آخرین بخش اسم به‌عنوان فامیلی، برای تطبیق «شیری» با «محمد شیری»."""
    parts = [p for p in re.split(r"\s+", str(text or "").strip()) if p]
    if not parts:
        return ""
    last = parts[-1]
    if last in _PREFIXES and len(parts) > 1:
        return parts[-2]
    return last


def _seed() -> list[dict]:
    return [{
        "name": "محمد شیری",
        "aliases": ["شیری"],
        "reason": "منفک شده — فروشگاه‌ها هنوز از سمت همکاران در وضعیت عدم تعیین کارشناس است.",
        "since": "",
        "added_at": "",
    }]


def _load() -> None:
    global _records
    if STORE_PATH.exists():
        try:
            with open(STORE_PATH, encoding="utf-8") as f:
                _records = json.load(f)
        except Exception:
            _records = []
    if not isinstance(_records, list):
        _records = []
    if not _records:
        _records = _seed()
        _save()


def _save() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(_records, f, ensure_ascii=False, indent=2)


def list_all() -> list[dict]:
    with _lock:
        _load()
        return [dict(r) for r in _records]


def match(expert_name: str) -> dict | None:
    """اگه این اسم کارشناس جزو منفک‌هاست، رکوردش رو برمی‌گردونه."""
    target = _norm(expert_name)
    if not target:
        return None
    for r in _records:
        candidates = [r.get("name", "")] + list(r.get("aliases") or [])
        if any(_norm(c) == target for c in candidates if c):
            return dict(r)
    surname = _norm(_surname(expert_name))
    if surname:
        for r in _records:
            candidates = [r.get("name", "")] + list(r.get("aliases") or [])
            if any(_norm(c) == surname for c in candidates if c):
                return dict(r)
    return None


def is_separated(expert_name: str) -> bool:
    return match(expert_name) is not None


def display_name(expert_name: str) -> str:
    """اسم خوانای کارشناسِ منفک (برای نمایش «محمد شیری» به‌جای «شیری»)."""
    rec = match(expert_name)
    if rec:
        return rec.get("name") or expert_name
    return expert_name


def add(name: str, reason: str = "", aliases: list[str] | None = None) -> dict:
    from datetime import datetime

    name = str(name or "").strip()
    if not name:
        raise ValueError("اسم کارشناس خالیه")
    with _lock:
        _load()
        if any(_norm(r.get("name")) == _norm(name) for r in _records):
            raise ValueError(f"«{name}» از قبل توی لیست منفک‌ها هست")
        record = {
            "name": name,
            "aliases": aliases or [],
            "reason": str(reason or "").strip(),
            "since": "",
            "added_at": datetime.now().isoformat(timespec="seconds"),
        }
        _records.append(record)
        _save()
        return dict(record)


def remove(name: str) -> bool:
    target = _norm(name)
    global _records
    with _lock:
        _load()
        before = len(_records)
        _records = [r for r in _records if _norm(r.get("name")) != target]
        if len(_records) != before:
            _save()
            return True
    return False


_load()