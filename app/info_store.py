"""
خوندن فایل اطلاعات فروشگاه‌ها (چارت سازمانی، Data و…) و کش کردن توی حافظه برای جستجو.
کلید جستجو: کد فروشگاه.

مثل فایل شارژ، همه‌ی شیت‌ها خونده می‌شن، سطر هدر و ستون کد فروشگاه خودکار تشخیص داده
می‌شن و شیت‌هایی که ربطی به فروشگاه ندارن (مثل «مدیران» یا «سرپرستان») کنار گذاشته
می‌شن و توی گزارش دلیلشون نوشته می‌شه.
"""
import json
import os
import threading
from datetime import datetime
from pathlib import Path

from app import config
from app.atomic_io import atomic_write_json
from app import data_store
from app import separated_experts
from app.xlsx_sheet import trim_float_noise
from app.data_store import (
    SUPPORTED_SUFFIXES,
    _col_key,
    _detect_columns,
    _format_value,
    _normalize_code,
    _prepare_sheet,
    _read_csv,
    auto_mode_message,
    detect_mode,
    diff_index,
    diff_summary,
    merge_index,
    read_tables,
)

_lock = threading.RLock()

# نسخه‌ی ساخت ایندکس اطلاعات. با هر تغییر در نرمال‌سازی مقادیر (مثل حذف نویز
# اعشاری مختصات‌ها) باید بالا بره تا اسنپ‌شات‌های قدیمی نادیده گرفته بشن و از
# فایل بازخوانی بشن؛ وگرنه آپلود مجددِ سالم فایل «تغییر» گزارش می‌شه.
INFO_INDEX_VERSION = 2


def _snapshot_version() -> str:
    return f"{data_store.SNAPSHOT_VERSION}.{INFO_INDEX_VERSION}"


def _info_value(value) -> str:
    """مقدار سلول شیت اطلاعات، با نرمال‌سازی نویز اعشاری.

    فایل سازمانی مختصات‌ها رو ناهمگون ذخیره کرده: بعضی «35.662250» (متن) و
    بعضی «35.688917000000004» (عدد). اگه این‌ها یکی نشن، هر آپلود مجددِ سالم
    کل مختصات‌ها رو «تغییرکرده» گزارش می‌کنه. _format_value فقط روی عددِ واقعی
    کار می‌کنه، پس اینجا نرمال‌سازی رو برای متنِ عددی هم اعمال می‌کنیم.
    """
    text = _format_value(value)
    if not text or "." not in text:
        return text
    return trim_float_noise(text)
INFO_CODE_COL = "کد فروشگاه"

INFO_LABEL_MAP = {
    "نام فروشگاه": "🏪 نام فروشگاه",
    "فروشگاه": "🏬 فروشگاه",
    "استان": "🏗 استان",
    "شهر": "🏙 شهر",
    "منطقه فروش": "🗺 منطقه فروش",
    "منطقه نهائی": "🗺 منطقه نهایی",
    "منطقه نهايي": "🗺 منطقه نهایی",
    "مدیر منطقه": "👤 مدیر منطقه",
    "مدیر کل": "🎯 مدیر کل",
    "پشتیبان منطقه": "🛠 پشتیبان منطقه",
    "سرپرست": "🔗 سرپرست",
    "تحت پوشش کارشناس": "👨‍💻 تحت پوشش کارشناس",
    "آدرس": "📍 آدرس",
    "نشانی": "📍 نشانی",
    "کد پستی": "📮 کد پستی",
    "تلفن": "☎️ تلفن",
    "موبایل": "📱 موبایل",
    "ایمیل": "✉️ ایمیل",
    "نوع فروشگاه": "🏷 نوع فروشگاه",
    "گرید فروشگاه": "⭐ گرید فروشگاه",
    "تاریخ افتتاحیه": "📅 تاریخ افتتاحیه",
    "تاریخ استخدام": "📅 تاریخ استخدام",
    "تاریخ اتمام فعالیت": "⛔ تاریخ اتمام فعالیت",
    "Lat": "🌐 عرض جغرافیایی",
    "Long": "🌐 طول جغرافیایی",
}

# ستون‌هایی که «کد فروشگاه» هستن (کد پرسنلی، ردیف و… کد فروشگاه نیستن)
_STORE_CODE_HINTS = ("کدفروشگاه", "کدفروش", "محلاستقرار", "فروشگاه", "شعبه", "نمایندگی", "کد")
_NON_STORE_CODE_HINTS = ("پرسنلی", "کارمند", "ردیف", "استخدام", "تعداد", "شماره", "منطقه")

MIN_CLEAN_CODE_RATIO = 0.6


def _is_store_code_column(key: str, values: list[str]) -> bool:
    """آیا این ستون واقعاً کد فروشگاهه؟ (نه کد پرسنلی، نه شماره‌ی ردیف)"""
    if any(t in key for t in _NON_STORE_CODE_HINTS):
        return False
    if not any(t in key for t in _STORE_CODE_HINTS):
        return False
    if not values:
        return True
    clean = sum(1 for v in values if v.strip().isdigit() and 2 <= len(v.strip()) <= 7)
    return clean / len(values) >= MIN_CLEAN_CODE_RATIO


def _info_index_sheet(file_name: str, sheet_name: str, raw_df, new_index: dict[str, list[dict]],
                       seen: set) -> dict:
    """یک شیت را می‌خواند و اگر شیتِ اطلاعات فروشگاه باشد، رکوردها را اضافه می‌کند."""
    label = sheet_name.strip() or sheet_name
    info: dict = {
        "file": file_name,
        "sheet": label,
        "rows": int(len(raw_df)) if raw_df is not None else 0,
        "indexed": 0,
        "no_code": 0,
        "unique_codes": 0,
        "columns": [],
        "code_col": None,
        "skipped_reason": None,
    }

    if raw_df is None or len(raw_df) == 0:
        info["skipped_reason"] = "شیت خالیه."
        return info

    columns, data, header_row = _prepare_sheet(raw_df)
    info["columns"] = columns
    info["header_row"] = header_row + 1
    if not columns:
        info["skipped_reason"] = "هیچ ستونی پیدا نشد."
        return info

    detected = _detect_columns(columns, data)
    code_col = detected.get("code_col")
    code_idx = detected.get("code_idx")

    if code_idx is None:
        info["skipped_reason"] = "ستون کد فروشگاه پیدا نشد."
        return info

    values = [str(v).strip() for v in data.iloc[:, code_idx].tolist()[:200]]
    if not _is_store_code_column(_col_key(code_col), values):
        info["skipped_reason"] = f"ستون «{code_col}» کد فروشگاه نیست (شیت مربوط به اطلاعات فروشگاه نیست)."
        return info

    info["code_col"] = code_col

    codes_here: set[str] = set()
    for row_no, row in enumerate(data.itertuples(index=False), start=1):
        values_row = list(row)
        code = _normalize_code(values_row[code_idx])
        if not code:
            info["no_code"] += 1
            continue

        record = {"source": label, "_file": file_name, "_sheet": label, "_row": row_no}
        for i, col in enumerate(columns):
            record[col] = _info_value(values_row[i])

        codes_here.add(code)
        fingerprint = (label, code, tuple(sorted((k, v) for k, v in record.items() if not k.startswith("_"))))
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        new_index.setdefault(code, []).append({**record, "code": code})
        info["indexed"] += 1

    info["unique_codes"] = len(codes_here)
    if info["indexed"] == 0:
        info["skipped_reason"] = "هیچ ردیفی با کد فروشگاه معتبر پیدا نشد."
    return info


# موتور مشترک (حلقه‌ی import را می‌شکند چون خودش ماژول ما را تنبلانه می‌خواند)
from app.store_engine import Engine as _Engine, Profile as _Profile


class InfoProfile(_Profile):
    """مشخصات مجموعه‌داده‌ی «اطلاعات فروشگاه»؛ بقیه به موتور مشترک واگذار می‌شود."""

    key = "info"
    label = "اطلاعات"
    noun = "اطلاعات"
    schema_path = config.INFO_SCHEMA_PATH
    snapshot_path = config.INFO_SNAPSHOT_PATH
    snapshot_version = _snapshot_version()
    use_alias = False
    nothing_uploaded = "هنوز فایلی برای اطلاعات فروشگاه‌ها آپلود نشده."
    nothing_found = "هیچ کد فروشگاهی"
    replace_tail = "کل اطلاعات فعلی با همین فایل ساخته می‌شه"
    replace_tail_replace = "کل اطلاعات فعلی با همین فایل ساخته می‌شه"

    def source_files(self) -> list[Path]:
        """فایل‌هایی که اطلاعات فروشگاه‌ها ازشون خونده می‌شه."""
        if config.INFO_EXCEL_PATH.exists() and config.INFO_EXCEL_PATH.suffix.lower() in SUPPORTED_SUFFIXES:
            return [config.INFO_EXCEL_PATH]
        return []

    def index_sheet(self, file_name, sheet_name, raw_df, new_index, seen):
        return _info_index_sheet(file_name, sheet_name, raw_df, new_index, seen)


ENGINE = _Engine(InfoProfile())
_lock = ENGINE._lock


def source_files() -> list[Path]:
    return InfoProfile().source_files()


def build_from_paths(paths: list[Path]) -> tuple[dict[str, list[dict]], dict]:
    """ایندکس + گزارش کامل (بدون تغییر کش)."""
    return ENGINE.build_from_paths(paths)


def report_summary(report: dict, max_warnings: int = 6) -> str:
    return ENGINE.report_summary(report, max_warnings)


def reload_info() -> tuple[bool, str]:
    """فایل اطلاعات را از دیسک می‌خواند و کش را به‌روز می‌کند."""
    return ENGINE.reload()


def _auto_reload(force: bool = False) -> None:
    """اگر فایل عوض شده باشد، خودکار ری‌لودش می‌کند."""
    ENGINE.auto_reload(force)


def preview_upload(paths: list[Path], mode: str = "merge", drop_missing: bool = False) -> tuple[bool, str, dict]:
    """تغییرات را گزارش می‌دهد (چیزی اعمال نمی‌شود)."""
    return ENGINE.preview_upload(paths, mode, drop_missing)


def commit_upload(paths: list[Path], mode: str = "merge", drop_missing: bool = False) -> tuple[bool, str, dict]:
    """تغییرات را اعمال می‌کند."""
    return ENGINE.commit_upload(paths, mode, drop_missing)


# ---------------------------------------------------------------- API عمومی

def lookup_info(store_code: str) -> list[dict] | None:
    """کد فروشگاه را جستجو می‌کند و رکوردهایش را از همه‌ی شیت‌ها برمی‌گرداند."""
    return ENGINE.lookup(store_code)


def list_all() -> list[dict]:
    """همه‌ی رکوردها را فلت‌شده برمی‌گرداند (کد + شیت + همه‌ی ستون‌ها)."""
    _auto_reload()
    rows = []
    for code, recs in ENGINE.all_records().items():
        for r in recs:
            row = {"code": code, "شیت": r.get("source", "")}
            row.update({k: v for k, v in r.items() if not k.startswith("_")})
            rows.append(row)
    return rows


def get_sheet_groups() -> dict[str, dict]:
    """رکوردها رو بر اساس شیت گروه‌بندی می‌کنه (ستون‌ها + ردیف‌ها) برای نمایش کامل توی پنل."""
    _auto_reload()
    groups: dict[str, dict] = {}
    index = ENGINE.all_records()
    schema = ENGINE.raw_schema()

    for code, recs in index.items():
        for r in recs:
            source = r.get("source", "")
            group = groups.setdefault(source, {"columns": [], "rows": [], "title": get_sheet_display(source)[0]})
            if not group["rows"]:
                group["columns"] = get_sheet_columns(source, schema)
            row = {"کد فروشگاه": code}
            row.update({k: v for k, v in r.items() if not k.startswith("_") and k != "source"})
            group["rows"].append(row)
    return groups


def get_sheet_columns(source: str, schema: dict | None = None) -> list[str]:
    """ستون‌های داده‌ی یه شیت (بدون ستون‌های متا) به ترتیب فایل."""
    if schema is None:
        schema = ENGINE.raw_schema()
    for sheet in (schema or {}).get("sheets", []):
        if sheet.get("sheet") == source:
            return [INFO_CODE_COL] + [c for c in sheet.get("columns", []) if c != INFO_CODE_COL]
    return [INFO_CODE_COL]


def get_status() -> dict:
    status = ENGINE.get_status()
    return {
        "loaded_info_stores": status["loaded_codes"],
        "total_records": status["total_records"],
        "sheets": status["sheets"],
        "skipped_sheets": status["skipped_sheets"],
        "info_excel_exists": config.INFO_EXCEL_PATH.exists(),
        "files": [p.name for p in source_files()],
        "last_error": status["last_error"],
        "generated_at": status["generated_at"],
    }


def get_schema() -> dict:
    return ENGINE.get_schema()


# --- تنظیمات فیلدها (ترتیب/مخفی/لیبل ستون‌ها از پنل) ---
FIELDS_CONFIG_PATH = config.DATA_DIR / "fields_config.json"
# کلید قدیمی تنظیمات (وقتی فایل فقط یه شیت داشت) — برای سازگاری به همه‌ی شیت‌ها اعمال می‌شه
_LEGACY_SHEET_KEY = "Sheet1"


def _load_fields_config() -> dict:
    if FIELDS_CONFIG_PATH.exists():
        try:
            with open(FIELDS_CONFIG_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _sheet_settings(sheet_name: str) -> dict:
    saved = _load_fields_config().get("info", {})
    if sheet_name in saved:
        return saved[sheet_name]
    return saved.get(_LEGACY_SHEET_KEY, {})


def get_visible_columns(sheet_name: str) -> list[tuple[str, str]]:
    """ستون‌های قابل نمایش یه شیت به‌همراه لیبلشون."""
    saved = _sheet_settings(sheet_name)
    saved_labels = saved.get("labels", {})
    all_cols = get_sheet_columns(sheet_name)[1:]
    saved_order = saved.get("order", all_cols)
    hidden = set(saved.get("hidden", []))

    def _label(col: str) -> str:
        return saved_labels.get(col) or INFO_LABEL_MAP.get(col, col)

    result, seen = [], set()
    for col in list(saved_order) + all_cols:
        if col in hidden or col in seen or col not in all_cols:
            continue
        result.append((col, _label(col)))
        seen.add(col)
    return result


def get_sheet_display(sheet: str) -> tuple[str, bool]:
    """(عنوان نمایشی شیت، آیا تیترش توی نتیجه نمایش داده بشه).

    عنوان پیش‌فرض همون اسم شیته؛ با تنظیمات پنل می‌شه عوضش کرد یا کلاً حذفش کرد.
    """
    saved = _load_fields_config().get("info_sheets", {}).get(sheet, {})
    label = str(saved.get("label") or "").strip() or sheet
    return label, not bool(saved.get("hidden"))


def get_sheet_display_map() -> dict[str, dict]:
    """تنظیمات عنوان همه‌ی شیت‌های ایندکس‌شده، برای نمایش در پنل."""
    result = {}
    sheets = [s.get("sheet") for s in ENGINE.raw_schema().get("sheets", []) if s.get("indexed")]
    for sheet in sheets:
        label, show = get_sheet_display(sheet)
        result[sheet] = {"label": label, "show": show}
    return result


def get_directory_columns() -> list[dict]:
    """ستون‌های فهرست یکپارچه با وضعیت نمایش/مخفی (برای پنل)."""
    columns, _rows = get_store_directory()
    saved = _load_fields_config().get("store_list", {})
    hidden = set(saved.get("hidden", []))
    return [{"key": c, "label": _directory_label(c), "hidden": c in hidden} for c in columns]


def _directory_label(col: str) -> str:
    """لیبل ستون‌های فهرست یکپارچه (از لیبل‌های تفکیک‌شده و سفارشی شیت‌ها)."""
    if col in _directory_dup_labels:
        return _directory_dup_labels[col]
    return _raw_label(col)


def save_directory_columns(hidden: list[str] | None) -> None:
    """ذخیره‌ی انتخاب ستون‌های فهرست فروشگاه‌ها (از پنل)."""
    cfg = _load_fields_config()
    cfg["store_list"] = {"hidden": [str(c) for c in (hidden or [])]}
    atomic_write_json(FIELDS_CONFIG_PATH, cfg)


def get_directory_rows() -> tuple[list[dict], list[dict]]:
    """(سطرها، ستون‌های قابل نمایش) برای فهرست فروشگاه‌های پنل."""
    columns, rows = get_store_directory()
    hidden = set(_load_fields_config().get("store_list", {}).get("hidden", []))
    shown = [c for c in columns if c not in hidden]
    # کد و کارشناس هیچ‌وقت نباید از جدول حذف بشن
    for must in (INFO_CODE_COL, EXPERT_COLUMN):
        if must in hidden and must not in shown:
            shown.insert(0 if must == INFO_CODE_COL else 1, must)
    return rows, [{"key": c, "label": _directory_label(c)} for c in shown]


def expert_assignments() -> tuple[list[str], dict[str, str]]:
    """(سطرهای خام شیت کارشناس، نگاشت کد → کارشناس) برای ویرایش گروهی."""
    from app.xlsx_sheet import read_sheet
    path = config.INFO_EXCEL_PATH
    rows = read_sheet(path, EXPERT_SHEET) if path.exists() else []
    if not rows:
        return [], {}
    header = [str(c).strip() for c in rows[0]]
    try:
        code_pos = header.index(INFO_CODE_COL)
    except ValueError:
        code_pos = 0
    try:
        expert_pos = header.index(EXPERT_COLUMN)
    except ValueError:
        expert_pos = 1
    mapping: dict[str, str] = {}
    for row in rows[1:]:
        padded = list(row) + [""] * (expert_pos + 1 - len(row))
        code = str(padded[code_pos]).strip()
        if code:
            mapping[code] = str(padded[expert_pos]).strip()
    return rows, mapping


def preview_expert_bulk(code_from: str, code_to: str, only_empty: bool = True) -> dict:
    """پیش‌نمایش ویرایش گروهی: چه کدهایی توی محدوده‌اند و الان چه کارشناسی دارند.

    کدهایی که توی ایندکس هستن ولی شیت کارشناس براشون ردیف نداره هم شمرده می‌شن
    (`added`) چون موقع اعمال به‌صورت سطر جدید اضافه می‌شن — وگرنه عدد پیش‌نمایش
    با نتیجه‌ی اعمال نمی‌خوند.
    """
    _rows, mapping = expert_assignments()
    _auto_reload()
    known = ENGINE.index_codes()
    lo, hi = _range_bounds(code_from, code_to)
    closed = closed_codes()
    codes = sorted((c for c in mapping if _in_range(c, lo, hi) and c not in closed),
                   key=lambda c: (len(c), c))
    absent = sorted((c for c in known - set(mapping)
                     if _in_range(c, lo, hi) and c not in closed),
                    key=lambda c: (len(c), c))
    if only_empty:
        codes = [c for c in codes if not mapping.get(c)]
    assigned = {c: mapping.get(c, "") for c in codes}
    return {
        "matched": len(codes),
        "already_assigned": sum(1 for c in codes if mapping.get(c)),
        "added": len(absent),
        "sample": codes[:15],
        "codes": codes,
        "by_expert": _count_by(assigned.values()),
    }


def _count_by(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value or EXPERT_UNSET] = out.get(value or EXPERT_UNSET, 0) + 1
    return out


def _range_bounds(code_from, code_to) -> tuple[int, int]:
    def num(value) -> int:
        digits = "".join(ch for ch in str(value or "") if ch.isdigit())
        return int(digits) if digits else 0
    lo, hi = num(code_from), num(code_to) or num(code_from)
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def _in_range(code: str, lo: int, hi: int) -> bool:
    digits = "".join(ch for ch in str(code) if ch.isdigit())
    if not digits:
        return False
    value = int(digits)
    if lo == hi:
        # فقط پیشوند: ۳۵۰۰ یعنی همه‌ی کدهای ۳۵۰۰ تا ۳۵۹۹
        if len(str(lo)) <= 2:
            return lo <= value <= lo + 99
        factor = 10 ** (len(str(lo)) - 2)
        return lo <= value <= lo + factor - 1
    return lo <= value <= hi


def bulk_assign_expert(code_from: str, code_to: str, expert: str,
                       only_empty: bool = True) -> dict:
    """یه کارشناس رو به همه‌ی فروشگاه‌های یه محدوده‌ی کد تخصیص می‌ده.

    شیت «کارشناس انفورماتیک» در سطح zip بازنویسی می‌شه (نه openpyxl، چون روی این
    فایل رشته‌ها را پاک می‌کنه). بقیه‌ی شیت‌ها byte-for-byte دست‌نخورده می‌مونن.
    """
    from app.xlsx_sheet import read_sheet, replace_sheet

    expert = str(expert or "").strip()
    if not expert:
        raise ValueError("نام کارشناس خالیه")
    if expert == EXPERT_UNSET:
        raise ValueError("برای خالی‌کردن از دکمه‌ی حذف تک‌تک استفاده کن")

    path = config.INFO_EXCEL_PATH
    if not path.exists():
        raise ValueError("فایل اطلاعات پیدا نشد")

    rows = read_sheet(path, EXPERT_SHEET)
    if not rows:
        raise ValueError(f"شیت «{EXPERT_SHEET}» توی فایل نیست")
    header = [str(c).strip() for c in rows[0]]
    try:
        code_pos = header.index(INFO_CODE_COL)
    except ValueError:
        code_pos = 0
    try:
        expert_pos = header.index(EXPERT_COLUMN)
    except ValueError:
        expert_pos = 1
    width = max(len(header), expert_pos + 1)

    lo, hi = _range_bounds(code_from, code_to)
    closed = closed_codes()
    new_rows = [list(header) + [""] * (width - len(header))]
    changed, filled, overwritten, skipped = 0, 0, 0, 0
    seen: set[str] = set()

    for row in rows[1:]:
        padded = list(row) + [""] * (width - len(row))
        code = str(padded[code_pos]).strip()
        if not code:
            new_rows.append(padded)
            continue
        current = str(padded[expert_pos]).strip()
        if _in_range(code, lo, hi) and (current != expert) and (current == "" or not only_empty):
            if code in closed:
                # فروشگاه بسته به کارشناس تخصیص نمی‌گیره؛ تخصیص قبلیش هم پاک می‌شه
                if current:
                    padded[expert_pos] = ""
                    changed += 1
                    skipped += 1
                new_rows.append(padded)
                seen.add(code)
                continue
            if current:
                overwritten += 1
            else:
                filled += 1
            padded[expert_pos] = expert
            changed += 1
        seen.add(code)
        new_rows.append(padded)

    # کدهایی که توی ایندکس هستن ولی ردیف ندارن. فروشگاه بسته‌ها اینجا ساخته
    # نمی‌شن — کدشون فقط توی شیت «تعداد فروشگاه ها» اومده و نباید وارد شیت کارشناس بشن.
    _auto_reload()
    with _lock:
        known = sorted(ENGINE.index_codes())
    added = 0
    for code in sorted((c for c in known if c not in seen), key=lambda c: (len(c), c)):
        if not _in_range(code, lo, hi):
            continue
        if code in closed:
            skipped += 1
            continue
        padded = [""] * width
        padded[code_pos] = code
        padded[expert_pos] = expert
        new_rows.append(padded)
        changed += 1
        filled += 1
        added += 1

    if not changed:
        return {"changed": 0, "filled": 0, "overwritten": 0, "added": 0,
                "skipped_closed": skipped, "expert": expert}

    replace_sheet(path, EXPERT_SHEET, new_rows)
    _auto_reload(force=True)
    return {"changed": changed, "filled": filled, "overwritten": overwritten,
            "added": added, "skipped_closed": skipped, "expert": expert}


EXPERT_STORE_NAME_COL = "فروشگاه"
EXPERT_REGION_COL = "منطقه نهائی"


def _expert_sheet_rows(path: Path) -> tuple[list[list[str]], int, int]:
    """(سطرها، اندیس ستون کد، اندیس ستون کارشناس) برای نوشتن روی شیت کارشناس."""
    from app.xlsx_sheet import read_sheet
    rows = read_sheet(path, EXPERT_SHEET)
    if not rows:
        raise ValueError(f"شیت «{EXPERT_SHEET}» توی فایل نیست")
    header = [str(c).strip() for c in rows[0]]
    try:
        code_pos = header.index(INFO_CODE_COL)
    except ValueError:
        code_pos = 0
    try:
        expert_pos = header.index(EXPERT_COLUMN)
    except ValueError:
        expert_pos = 1
    return rows, code_pos, expert_pos


CLOSED_SHEET = "فروشگاههای بسته شده"
CLOSED_CODE_COL = "کد فروشگاه"

_closed_cache: tuple[str, frozenset[str]] | None = None


def closed_codes() -> frozenset[str]:
    """کد فروشگاه‌های بسته، از شیت «فروشگاههای بسته شده».

    فروشگاه بسته نه کارشناس می‌گیرد و نه توی تب «لیست کارشناسان» می‌آید، تا
    آمار فروشگاه‌های فعال قاطی نشود. نتیجه بر اساس امضای فایل کش می‌شه.
    """
    global _closed_cache
    _auto_reload()
    paths = source_files()
    sig = ENGINE.files_signature(paths) if paths else ""
    if _closed_cache and _closed_cache[0] == sig:
        return _closed_cache[1]

    from app.xlsx_sheet import read_sheet
    codes: set[str] = set()
    path = config.INFO_EXCEL_PATH
    if path.exists():
        try:
            rows = read_sheet(path, CLOSED_SHEET)
            if rows:
                # سرستون‌ها معمولاً سطر دومن، ولی بعضی فایل‌ها سطر اول
                first = [str(c).strip() for c in rows[0]]
                if CLOSED_CODE_COL in first:
                    header, body = first, rows[1:]
                elif len(rows) > 1 and CLOSED_CODE_COL in [str(c).strip() for c in rows[1]]:
                    header, body = [str(c).strip() for c in rows[1]], rows[2:]
                else:
                    header, body = first, rows[1:]
                pos = header.index(CLOSED_CODE_COL) if CLOSED_CODE_COL in header else 0
                for row in body:
                    if len(row) > pos:
                        code = _normalize_code(row[pos])
                        if code:
                            codes.add(code)
        except Exception:
            pass

    result = frozenset(codes)
    _closed_cache = (sig, result)
    return result


def clear_closed_experts() -> dict:
    """کارشناسِ فروشگاه‌های بسته را خالی می‌کند.

    فروشگاه بسته نباید به کارشناسی تخصیص داشته باشد؛ اگر قبلاً گرفته شده بود
    (مثلاً از ویرایش گروهی قبلی که فیلتر بسته‌بودن نداشت) پاکش می‌کنیم.
    """
    from app.xlsx_sheet import replace_sheet

    path = config.INFO_EXCEL_PATH
    rows, code_pos, expert_pos = _expert_sheet_rows(path)
    width = max(len(rows[0]), expert_pos + 1)
    closed = closed_codes()

    cleared, kept = 0, 0
    new_rows = [list(rows[0]) + [""] * (width - len(rows[0]))]
    for row in rows[1:]:
        padded = list(row) + [""] * (width - len(row))
        code = _normalize_code(padded[code_pos])
        if code in closed and str(padded[expert_pos]).strip():
            padded[expert_pos] = ""
            cleared += 1
        elif code in closed:
            kept += 1
        new_rows.append(padded)

    if cleared:
        replace_sheet(path, EXPERT_SHEET, new_rows)
        _auto_reload(force=True)
    return {"cleared": cleared, "already_empty": kept, "closed_total": len(closed)}


def get_expert_groups() -> list[dict]:
    """فروشگاه‌ها گروه‌بندی‌شده به اسم کارشناس، برای تب «لیست کارشناسان».

    فروشگاه‌های بسته اصلاً نمی‌آیند: نه تخصیص دارند نه نمایش داده می‌شوند، تا
    قاطی آمار فروشگاه‌های فعال نشوند.
    """
    rows, _cols = get_directory_rows()
    closed = closed_codes()
    groups: dict[str, list[dict]] = {}
    for row in rows:
        code = str(row.get(INFO_CODE_COL) or "").strip()
        if code in closed:
            continue
        expert = str(row.get(EXPERT_COLUMN) or "").strip() or EXPERT_UNSET
        # کارشناسِ منفک (مثل «شیری») هم «فاقد کارشناس» حساب می‌شه تا همه‌ی
        # فروشگاه‌های بدون کارشناس یکجا دیده بشن.
        if separated_experts.is_separated(expert):
            expert = EXPERT_UNSET
        groups.setdefault(expert, []).append({
            "code": code,
            "name": row.get(EXPERT_STORE_NAME_COL) or row.get("نام فروشگاه") or "",
            "region": row.get(EXPERT_REGION_COL) or row.get("منطقه فروش") or "",
        })
    result = [{"expert": name, "count": len(items), "stores": items}
              for name, items in groups.items()]
    result.sort(key=lambda g: (g["expert"] == EXPERT_UNSET, -g["count"], g["expert"]))
    return result


def set_store_expert(code: str, expert: str) -> dict:
    """کارشناسِ یک فروشگاه را عوض می‌کند. expert خالی یعنی بی‌کارشناس."""
    from app.xlsx_sheet import replace_sheet

    code = _normalize_code(code)
    expert = str(expert or "").strip()
    if not code:
        raise ValueError("کد فروشگاه خالیه")
    if code in closed_codes():
        raise ValueError(f"فروشگاه {code} بسته شده و کارشناس نمی‌گیره")
    if expert == EXPERT_UNSET:
        expert = ""

    path = config.INFO_EXCEL_PATH
    rows, code_pos, expert_pos = _expert_sheet_rows(path)
    width = max(len(rows[0]), expert_pos + 1)

    found = False
    new_rows = [list(rows[0]) + [""] * (width - len(rows[0]))]
    for row in rows[1:]:
        padded = list(row) + [""] * (width - len(row))
        if _normalize_code(padded[code_pos]) == code:
            before = str(padded[expert_pos]).strip()
            if before != expert:
                padded[expert_pos] = expert
            found = True
        new_rows.append(padded)

    if not found:
        # فروشگاه توی شیت کارشناس نبوده → سطر تازه بساز
        padded = [""] * width
        padded[code_pos] = code
        padded[expert_pos] = expert
        new_rows.append(padded)

    replace_sheet(path, EXPERT_SHEET, new_rows)
    _auto_reload(force=True)
    return {"code": code, "expert": expert or EXPERT_UNSET}


def set_store_experts_bulk(pairs) -> dict:
    """ذخیره‌ی گروهیِ کارشناس چند فروشگاه در یک بار بازنویسی شیت.

    `pairs` فهرستی از `(کد, کارشناس)` است؛ کارشناس خالی یعنی بی‌کارشناس.
    فروشگاه بسته یا نامعتبر بی‌صدا رد می‌شود تا یک مورد خراب کل ذخیره را
    از بین نبرد، ولی در گزارش برمی‌گردد.
    """
    from app.xlsx_sheet import replace_sheet

    closed = closed_codes()
    wanted: dict[str, str] = {}
    skipped: list[str] = []
    for raw_code, raw_expert in pairs:
        code = _normalize_code(raw_code)
        if not code:
            continue
        if code in closed:
            skipped.append(f"{code} (بسته)")
            continue
        expert = str(raw_expert or "").strip()
        if expert == EXPERT_UNSET:
            expert = ""
        wanted[code] = expert

    if not wanted:
        return {"changed": 0, "skipped": skipped, "saved": 0}

    path = config.INFO_EXCEL_PATH
    rows, code_pos, expert_pos = _expert_sheet_rows(path)
    width = max(len(rows[0]), expert_pos + 1)

    new_rows = [list(rows[0]) + [""] * (width - len(rows[0]))]
    changed = 0
    for row in rows[1:]:
        padded = list(row) + [""] * (width - len(row))
        code = _normalize_code(padded[code_pos])
        if code in wanted:
            if str(padded[expert_pos]).strip() != wanted[code]:
                padded[expert_pos] = wanted[code]
                changed += 1
            wanted.pop(code)
        new_rows.append(padded)

    for code, expert in wanted.items():
        padded = [""] * width
        padded[code_pos] = code
        padded[expert_pos] = expert
        new_rows.append(padded)
        changed += 1

    if changed:
        replace_sheet(path, EXPERT_SHEET, new_rows)
        _auto_reload(force=True)
    return {"changed": changed, "skipped": skipped}


def rename_expert(old: str, new: str) -> dict:
    """اسم یک کارشناس را روی همه‌ی فروشگاه‌هایش عوض می‌کند."""
    from app.xlsx_sheet import replace_sheet

    old = str(old or "").strip()
    new = str(new or "").strip()
    if not old:
        raise ValueError("اسم فعلی کارشناس خالیه")
    if not new:
        raise ValueError("اسم جدید خالیه")
    if old == new:
        return {"changed": 0, "from": old, "to": new}
    if new == EXPERT_UNSET:
        raise ValueError("برای خالی‌کردن از «بی‌کارشناس» توی فهرست استفاده کن")

    path = config.INFO_EXCEL_PATH
    rows, _code_pos, expert_pos = _expert_sheet_rows(path)
    width = max(len(rows[0]), expert_pos + 1)
    changed = 0
    new_rows = [list(rows[0]) + [""] * (width - len(rows[0]))]
    for row in rows[1:]:
        padded = list(row) + [""] * (width - len(row))
        if str(padded[expert_pos]).strip() == old:
            padded[expert_pos] = new
            changed += 1
        new_rows.append(padded)

    if not changed:
        return {"changed": 0, "from": old, "to": new}

    replace_sheet(path, EXPERT_SHEET, new_rows)
    _auto_reload(force=True)
    return {"changed": changed, "from": old, "to": new}


def save_sheet_display(values: dict[str, dict]) -> None:
    cfg = _load_fields_config()
    saved = cfg.get("info_sheets", {})
    for sheet, data in (values or {}).items():
        if not isinstance(data, dict):
            continue
        label = str(data.get("label") or "").strip()
        entry = {"label": label, "hidden": bool(data.get("hidden"))}
        if not label and not entry["hidden"]:
            saved.pop(sheet, None)  # تنظیمی نداره → پیش‌فرض فایل
        else:
            saved[sheet] = entry
    cfg["info_sheets"] = saved
    atomic_write_json(FIELDS_CONFIG_PATH, cfg)


def format_info_reply(code: str, records: dict | list[dict] | None) -> str:
    """رکوردهای یه کد رو به‌صورت پیام، تفکیک‌شده بر اساس شیت، نشون می‌ده."""
    if isinstance(records, dict):
        records = [records]
    if not records:
        return f"🏪 اطلاعات فروشگاه {code}"

    # رکوردها رو بر اساس شیت گروه کن (ترتیب ورودی حفظ می‌شه) و ستون‌های
    # قابل نمایش هر شیت رو یه‌بار حساب کن
    grouped: dict[str, list[str]] = {}
    columns_cache: dict[str, list[tuple[str, str]]] = {}
    for record in records:
        sheet = record.get("source") or record.get("_sheet") or ""
        if sheet not in grouped:
            grouped[sheet] = []
            columns_cache[sheet] = get_visible_columns(sheet)
        for col, label in columns_cache[sheet]:
            value = str(record.get(col, "")).strip()
            if not value:
                # کارشناسِ خالی هم «نامشخص» حساب می‌شه تا با منفک‌ها یه‌دست باشه
                if col == EXPERT_COLUMN:
                    grouped[sheet].append(f"{label}: نامشخص")
                continue
            # کارشناسِ منفک (مثل «شیری») → «نامشخص»
            if col == EXPERT_COLUMN and separated_experts.is_separated(value):
                value = "نامشخص"
            grouped[sheet].append(f"{label}: {value}")

    blocks = []
    for sheet, lines in grouped.items():
        # اگه همه‌ی ستون‌های این شیت مخفی یا بی‌مقدارن، کل بخش حذف می‌شه
        # (وگرنه فقط یه تیتر خالی توی نتیجه می‌موند)
        if not lines:
            continue
        title, show_title = get_sheet_display(sheet)
        body = "\n".join(lines)
        blocks.append(f"▫️ {title}\n{body}" if show_title else body)

    if not blocks:
        return f"🏪 اطلاعات فروشگاه {code}\n\nهیچ فیلد قابل نمایشی برای این فروشگاه نیست."
    return f"🏪 اطلاعات فروشگاه {code}\n\n" + "\n\n".join(blocks)


# ستون و برچسبِ «کارشناس انفورماتیک» در فهرست یکپارچه‌ی فروشگاه‌ها
EXPERT_SHEET = "کارشناس انفورماتیک"
EXPERT_COLUMN = "کارشناس انفورماتیک"
EXPERT_UNSET = "فاقد کارشناس"

# اسم ستون‌های بی‌معنی که نباید توی فهرست یکپارچه بیاین
_JUNK_COLUMN_NAMES = {"", "nan", "none", "nat"}

# برچسب‌های تفکیک‌شده‌ی ستون‌های هم‌نام (پر می‌شه از get_store_directory)
_directory_dup_labels: dict[str, str] = {}


def _is_junk_column(key: str) -> bool:
    text = str(key or "").strip().lower()
    if text in _JUNK_COLUMN_NAMES:
        return True
    return text.startswith("unnamed:")


def _sheet_display_label(sheet: str) -> str:
    label, _show = get_sheet_display(sheet)
    return label or sheet


def _raw_label(col: str) -> str:
    for sheet in (get_schema().get("sheets") or []):
        if not sheet.get("indexed"):
            continue
        for key, label in get_visible_columns(sheet.get("sheet", "")):
            if key == col:
                return label
    return INFO_LABEL_MAP.get(col, col)


def _resolve_directory_labels(columns: list[str], key_source: dict[str, str]) -> dict[str, str]:
    """لیبل ستون‌های فهرست یکپارچه، با تفکیک موارد هم‌نام.

    دو نوع برخورد ممکنه: کلید متفاوت با لیبل یکسان (مثل «منطقه نهايی» با
    ی/ی عربی) و کلید یکسان از شیت‌های مختلف. هر دو با پسوند «· نام شیت» حل می‌شن.
    """
    overrides: dict[str, str] = {}
    used: set[str] = set()
    for col in columns:
        label = _raw_label(col)
        if label in used and col not in overrides:
            label = f"{label} · {_sheet_display_label(key_source.get(col, ''))}".strip(" ·")
        overrides.setdefault(col, label)
        used.add(label)
    return overrides


def get_store_directory() -> tuple[list[str], list[dict]]:
    """فهرست یکپارچه‌ی همه‌ی فروشگاه‌ها: هر کد یک ردیف، همه‌ی شیت‌ها ادغام‌شده.

    ستون‌های هم‌نامِ چند شیت روی هم جمع نمی‌شن: اولین شیتی که مقدار داشته باشه
    برنده‌ست (ترتیب شیت‌ها = ترتیب فایل). فروشگاهی که کارشناس نداشته باشه
    مقدار «تعیین نشده است» می‌گیره تا توی فهرست گم نشه.
    """
    _auto_reload()
    index = ENGINE.all_records()
    schema = ENGINE.raw_schema()

    rows: dict[str, dict] = {}
    order: list[str] = []
    seen_keys: set[str] = set()
    key_source: dict[str, str] = {}

    for code, recs in index.items():
        row = rows.get(code)
        if row is None:
            row = rows[code] = {INFO_CODE_COL: code}
            order.append(code)
        for r in recs:
            if r.get("source") == EXPERT_SHEET:
                expert = str(r.get(EXPERT_COLUMN) or "").strip()
                if expert:
                    row[EXPERT_COLUMN] = expert
                continue
            for key, value in r.items():
                if key.startswith("_") or key in ("source", "code", INFO_CODE_COL):
                    continue
                if _is_junk_column(key):
                    continue
                value = str(value or "").strip()
                # اولین شیتی که مقدار داره برنده‌ست؛ بقیه‌ی ستون‌های هم‌نام نادیده گرفته می‌شن
                if value and not row.get(key):
                    if key not in seen_keys:
                        seen_keys.add(key)
                        key_source[key] = str(r.get("source") or "").strip()
                    row[key] = value

    for row in rows.values():
        if not str(row.get(EXPERT_COLUMN) or "").strip():
            row[EXPERT_COLUMN] = EXPERT_UNSET

    columns = [INFO_CODE_COL] + [k for k in seen_keys if k not in (INFO_CODE_COL, EXPERT_COLUMN)]
    if EXPERT_COLUMN not in columns:
        columns.append(EXPERT_COLUMN)
    # ترتیب ستون‌ها همون ترتیب شیت‌های فایل باشه، نه ترتیب کشف
    file_order: list[str] = []
    for sheet in (schema or {}).get("sheets", []):
        if not sheet.get("indexed"):
            continue
        for col in get_sheet_columns(sheet.get("sheet", ""), schema):
            if col in columns and col not in file_order:
                file_order.append(col)
    file_order += [c for c in columns if c not in file_order]
    columns = file_order

    global _directory_dup_labels
    _directory_dup_labels = _resolve_directory_labels(columns, key_source)

    def sort_key(code: str):
        row = rows[code]
        unset = row.get(EXPERT_COLUMN) == EXPERT_UNSET
        return (unset, str(row.get(EXPERT_COLUMN) or ""), _int_or_big(row.get("منطقه فروش")),
                _int_or_big(code))

    return columns, [rows[c] for c in sorted(order, key=sort_key)]


def _int_or_big(value) -> int:
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else 10**9


reload_info()
