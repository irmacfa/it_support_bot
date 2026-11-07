"""
پردازش فایل PDF برنامه‌ی شیفت ماهانه (روز، تاریخ شمسی، کارشناس اول/دوم سرویس دسک، کارشناس شیفت شب).
هر ماه از پنل جایگزین می‌شه؛ داده‌ها بعد از پردازش توی یه JSON کش می‌شن.
"""
import json
import logging
import re
import threading

import pdfplumber

from app import config
from app.atomic_io import atomic_write_json

log = logging.getLogger("schedule_store")

_lock = threading.Lock()
PDF_PATH = config.DATA_DIR / "schedule.pdf"
JSON_PATH = config.DATA_DIR / "schedule.json"

_schedule: dict[str, dict] = {}

_ASCII_ONLY_RE = re.compile(r"^[0-9/\-*\s]*$")


def _reverse_fa(cell) -> str:
    """پی‌دی‌اف سلول‌های فارسی رو معکوس (حرف به حرف) استخراج می‌کنه؛ برش می‌گردونه به حالت درست.
    سلول‌های عددی/تاریخ/*** رو دست‌نخورده برمی‌گردونه چون از قبل درستن."""
    text = (cell or "").strip()
    if not text or _ASCII_ONLY_RE.match(text):
        return text
    return text[::-1]


def _load() -> None:
    global _schedule
    if JSON_PATH.exists():
        try:
            with open(JSON_PATH, encoding="utf-8") as f:
                _schedule = json.load(f)
        except Exception:
            _schedule = {}
    else:
        _schedule = {}


def _save(value: dict[str, dict] | None = None) -> None:
    atomic_write_json(JSON_PATH, _schedule if value is None else value)


def reload_schedule() -> tuple[bool, str]:
    """فایل PDF ذخیره‌شده رو پارس می‌کنه و کش JSON رو می‌سازه."""
    if not PDF_PATH.exists():
        return False, "فایل PDF برنامه شیفت پیدا نشد."

    try:
        with pdfplumber.open(PDF_PATH) as pdf:
            rows: list[list] = []
            for page in pdf.pages:
                for table in page.extract_tables():
                    rows.extend(table)
    except Exception as e:
        log.error("خطا در خوندن PDF شیفت: %s", e)
        return False, f"خطا در خوندن فایل PDF: {e}"

    new_data: dict[str, dict] = {}
    date_re = re.compile(r"^\d{4}/\d{2}/\d{2}$")

    for row in rows:
        if len(row) < 7:
            continue
        date_cell = (row[5] or "").strip()
        if not date_re.match(date_cell):
            continue  # ردیف هدر یا خالی

        record = {
            "date": date_cell,
            "day_name": _reverse_fa(row[6]),
            "desk1": _reverse_fa(row[4]),
            "desk2": _reverse_fa(row[3]),
            "night_primary": _reverse_fa(row[1]),
            "night_secondary": _reverse_fa(row[0]),
        }
        new_data[date_cell] = record

    if not new_data:
        return False, "هیچ ردیف معتبری توی PDF پیدا نشد. فرمت فایل رو چک کن."

    try:
        with _lock:
            _save(new_data)
            _schedule = new_data
    except Exception:
        log.exception("ذخیره‌ی برنامه‌ی شیفت ناموفق بود.")
        return False, "ذخیره‌ی برنامه‌ی شیفت ناموفق بود؛ اطلاعات قبلی حفظ شد."

    return True, f"برنامه شیفت با موفقیت بارگذاری شد ({len(new_data)} روز)."


def update_day(
    date_str: str,
    *,
    desk1: str | None = None,
    desk2: str | None = None,
    night_primary: str | None = None,
    night_secondary: str | None = None,
    morning_shift: str | None = None,
    evening_shift: str | None = None,
) -> dict | None:
    """یه روز خاص از برنامه شیفت رو مستقیم ویرایش می‌کنه (مثلاً جابجایی دو نفر)، بدون نیاز به آپلود دوباره PDF."""
    with _lock:
        _load()
        current = _schedule.get(date_str)
        if current is None:
            return None
        updated = {key: dict(value) for key, value in _schedule.items()}
        record = dict(current)
        if desk1 is not None:
            record["desk1"] = desk1
        if desk2 is not None:
            record["desk2"] = desk2
        if night_primary is not None:
            record["night_primary"] = night_primary
        if night_secondary is not None:
            record["night_secondary"] = night_secondary
        if morning_shift is not None:
            record["morning_shift"] = morning_shift
        if evening_shift is not None:
            record["evening_shift"] = evening_shift
        updated[date_str] = record
        _save(updated)
        _schedule = updated
        return dict(record)


def lookup(date_str: str) -> dict | None:
    with _lock:
        _load()
        return _schedule.get(date_str)


def list_all() -> list[dict]:
    with _lock:
        _load()
        return sorted(_schedule.values(), key=lambda r: r["date"])


def get_status() -> dict:
    with _lock:
        _load()
        return {"loaded_days": len(_schedule)}


_load()
