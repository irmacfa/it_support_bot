"""
پایش آنلاین‌بودن و فعالیت کاربرها (بچه‌ها).

«آنلاین» یعنی چند دقیقه پیش پیامی فرستاده. این ماژول برای هر پیام ورودی ربات
یه رکورد به‌روز می‌کنه و آخرین حضور، تعداد پیام و منبعش (خصوصی/گروه) رو نگه
می‌داره. برخلاف usage_stats که فقط «استفاده از قابلیت‌ها» رو می‌شماره، اینجا
هر پیامی—even یه «سلام» بی‌ربط—حضور رو ثبت می‌کنه، پس وضعیت آنلاین دقیقه‌ای
درمیاد.

خروجی توی یه فایل JSON دائمی ذخیره می‌شه تا با ری‌استارت ربات از بین نره.
"""
import json
import threading
from datetime import datetime, timedelta

from app import config

_lock = threading.Lock()
STORE_PATH = config.DATA_DIR / "presence.json"

# آستانه‌های وضعیت (دقیقه)
ONLINE_MINUTES = 15
ACTIVE_HOURS = 6

_presence: dict = {}


def _load() -> None:
    global _presence
    if STORE_PATH.exists():
        try:
            with open(STORE_PATH, encoding="utf-8") as f:
                _presence = json.load(f)
        except Exception:
            _presence = {}
    if not isinstance(_presence, dict):
        _presence = {}


def _save() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(_presence, f, ensure_ascii=False, indent=2)


def _backfill_from_groups() -> int:
    """یک‌بار: آخرین حضورِ ذخیره‌شده در گروه‌ها رو می‌آره توی presence.

    قبل از این ماژول، فقط اعضای گروه «last_seen» داشتن. بدون این migrate،
    مانیتورینگ تا اولین پیامِ جدید همه رو «بدون حضور» نشون می‌داد.
    """
    from app import groups, group_members

    seeded = 0
    with _lock:
        _load()
        for g in groups.list_all():
            for member in group_members.list_for_chat(g["chat_id"]):
                uid = str(member.get("user_id") or "")
                seen = member.get("last_seen")
                if not uid or not seen:
                    continue
                entry = _presence.get(uid)
                if entry and entry.get("last_seen") and entry["last_seen"] >= seen:
                    continue
                _presence[uid] = {
                    **(entry or {}),
                    "messages": (entry or {}).get("messages", 0),
                    "first_seen": (entry or {}).get("first_seen", seen),
                    "last_seen": (entry or {}).get("last_seen") or seen,
                    "days": (entry or {}).get("days", {}),
                    "sources": {**(entry or {}).get("sources", {}), "group": 1},
                    "display_name": member.get("display_name") or "",
                    "username": member.get("username") or "",
                    "backfilled": True,
                }
                seeded += 1
        if seeded:
            _save()
    return seeded


def record(user_id, display_name: str = "", username: str = "", source: str = "") -> None:
    """هر پیام ورودی یه بار صدا زده می‌شه تا آخرین حضور کاربر به‌روز بشه."""
    if not user_id:
        return
    key = str(user_id)
    now = datetime.now().isoformat(timespec="seconds")
    today = datetime.now().strftime("%Y-%m-%d")
    with _lock:
        _load()
        entry = _presence.setdefault(key, {
            "messages": 0,
            "first_seen": now,
            "last_seen": now,
            "days": {},
            "sources": {},
            "display_name": "",
            "username": "",
        })
        entry["last_seen"] = now
        entry["messages"] = entry.get("messages", 0) + 1
        entry["days"][today] = entry["days"].get(today, 0) + 1
        if source:
            entry["sources"][source] = entry["sources"].get(source, 0) + 1
        if display_name:
            entry["display_name"] = display_name
        if username:
            entry["username"] = username
        _save()


def _minutes_since(iso: str | None) -> float | None:
    """چند دقیقه از آخرین حضور گذشته (None اگه تا حالا دیده نشده)."""
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if then.tzinfo is not None:
        then = then.replace(tzinfo=None)
    return (datetime.now() - then).total_seconds() / 60.0


def _state(minutes: float | None) -> str:
    """online | recent | idle | unseen"""
    if minutes is None:
        return "unseen"
    if minutes <= ONLINE_MINUTES:
        return "online"
    if minutes <= ACTIVE_HOURS * 60:
        return "recent"
    return "idle"


def snapshot() -> dict:
    """کل وضعیت حضور، به‌همراه وضعیت محاسبه‌شده و آمار امروز/هفته."""
    import jdatetime

    with _lock:
        _load()
        rows = {k: dict(v) for k, v in _presence.items()}

    today = datetime.now().strftime("%Y-%m-%d")
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    result = {}
    for key, entry in rows.items():
        minutes = _minutes_since(entry.get("last_seen"))
        days = entry.get("days") or {}
        result[key] = {
            **entry,
            "minutes_since": minutes,
            "state": _state(minutes),
            "today": days.get(today, 0),
            "week": sum(v for d, v in days.items() if d >= week_ago),
            "today_jalali": jdatetime.date.today().strftime("%Y/%m/%d"),
        }
    return result


def state_counts() -> dict:
    """شمارنده‌ی وضعیت‌ها برای KPIها."""
    counts = {"online": 0, "recent": 0, "idle": 0, "unseen": 0}
    for row in snapshot().values():
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    return counts


_load()
_backfill_from_groups()
