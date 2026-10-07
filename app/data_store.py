"""
خوندن فایل‌های شارژ فروشگاه‌ها و کش کردنشون توی حافظه برای جستجوی سریع.

هیچ شیت یا ستونی توی کد قفل نشده: هر فایلی که آپلود بشه خودش بررسی میشه و برای
هر شیت، ستون «کد فروشگاه»، «شماره سیم‌کارت»، «اپراتور» و «محل نصب» به‌صورت خودکار
تشخیص داده می‌شن (اول از روی اسم ستون، بعد از روی محتوای سلول‌ها) و *همه‌ی* ردیف‌ها
و *همه‌ی* ستون‌های هر شیت ایندکس می‌شن. نتیجه‌ی این بررسی (چیدمان هر شیت) توی
data/store_schema.json ذخیره میشه تا پنل و ربات ازش استفاده کنن.

هر بار که فایل (یا فایل‌های) جدیدی آپلود بشه reload_data() صدا زده میشه؛ به‌صورت
خودکار هم mtime فایل‌ها چک میشه و اگه تغییر کرده باشه ری‌لود انجام می‌شه.
"""
import hashlib
import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

import pandas as pd

from app import config

_lock = threading.RLock()

# ایندکس نهایی: کد فروشگاه (نرمال‌شده) -> لیست رکوردها
_index: dict[str, list[dict]] = {}
# نگاشت «کد بدون صفر ابتدا» به کد اصلی، برای اینکه «۰۰۱» هم کد ۱ رو پیدا کنه
_alias: dict[str, str] = {}
# گزارش کامل تشخیص (همون چیزی که توی data/store_schema.json ذخیره میشه)
_schema: dict = {}
_last_error: str | None = None
_signature: tuple = ()

# با هر تغییر در نحوه‌ی نرمال‌سازی مقادیر باید بالا بره تا snapshotهای قدیمی
# (که با قواعد پیشین ساخته شدن) دور ریخته و از خود فایل‌ها بازسازی بشن.
SNAPSHOT_VERSION = 2

# ستون‌های متا که توی داده‌ی واقعی فروشگاه نیستن
META_KEYS = ("_file", "_sheet", "_row", "source", "code", "mobile", "operator", "location")

EXCEL_SUFFIXES = {".xlsx", ".xlsm"}
CSV_SUFFIXES = {".csv", ".txt"}
SUPPORTED_SUFFIXES = EXCEL_SUFFIXES | CSV_SUFFIXES

_PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
_ARABIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
_ARABIC_YEH_KAF = {"ي": "ی", "ك": "ک", "ى": "ی", "‌": " ", "ً": "", "ٌ": "", "ٍ": "", "َ": "",
                   "ُ": "", "ِ": "", "ّ": "", "ْ": "", "ـ": ""}

_KNOWN_OPERATORS = ("همراه اول", "ایرانسل", "رایتل", "شاتل", "مخابرات", "ایرانسل/", "تی‌دادیس", "شهروند", "rightel", "hamrah", "irancell", "mci")


# ---------------------------------------------------------------- نرمال‌سازی پایه

def _to_english_digits(text: str) -> str:
    """اعداد فارسی/عربی رو به انگلیسی تبدیل می‌کنه."""
    out = []
    for ch in text:
        idx = _PERSIAN_DIGITS.find(ch)
        if idx == -1:
            idx = _ARABIC_DIGITS.find(ch)
        out.append(str(idx) if idx != -1 else ch)
    return "".join(out)


def _is_missing(value) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and pd.isna(value):
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return str(value).strip() == ""


def _format_value(value) -> str:
    """مقدار هر سلول رو به یه رشته‌ی تمیز و قابل نمایش تبدیل می‌کنه."""
    if _is_missing(value):
        return ""
    if isinstance(value, pd.Timestamp):
        if value.hour or value.minute or value.second:
            return value.strftime("%Y/%m/%d %H:%M")
        return value.strftime("%Y/%m/%d")
    if pd.api.types.is_float(value):
        number = float(value)
        if number.is_integer():
            return str(int(number))
        # گِرد کردن تا ۱۵ رقم معنادار تا خطای نمایش اعشاری اکسل
        # (مثل ۳۵.۶۸۸۹۱۷۰۰۰۰۰۰۰۰۰۴ در برابر ۳۵.۶۸۸۹۱۷) روی مقایسه اثر نذاره
        text = f"{number:.15g}"
        if "e" in text or "E" in text:
            text = f"{number:.10f}"
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text
    if isinstance(value, int):
        return str(value)
    return str(value).strip()


def _normalize_code(code) -> str:
    """
    کد فروشگاه رو یکدست می‌کنه: فاصله‌های اضافه، اعداد فارسی/عربی به انگلیسی،
    جداکننده‌ها و تبدیل به رشته (چون ممکنه توی اکسل به‌صورت عدد ذخیره شده باشه).
    خروجی فقط شامل رقم‌هاست.
    """
    text = _to_english_digits(_format_value(code))
    match = re.fullmatch(r"(\d+)\.0+", text.strip())
    if match:
        text = match.group(1)
    return re.sub(r"\D", "", text)


def _col_key(name: str) -> str:
    """اسم ستون رو برای مقایسه‌ی شباهت‌سنجی نرمال می‌کنه (فاصله/نیم‌فاصله/علائم حذف، ی/ک یکسان)."""
    text = str(name)
    for src, dst in _ARABIC_YEH_KAF.items():
        text = text.replace(src, dst)
    text = re.sub(r"[\s_\-–—/\\.,()\[\]{}:;\"'#*+]+", "", text)
    return text.lower()


def _clean_header(raw, index: int) -> str:
    """اسم ستون رو تمیز می‌کنه؛ اگه خالی بود، اسم ستون اکسل رو می‌ذاره (مثلاً «ستون W»)."""
    text = re.sub(r"\s+", " ", str(raw or "").replace("\ufeff", "")).strip()
    text = text.replace("\u200c", " ")
    text = re.sub(r"^unnamed:?\s*\d*$", "", text, flags=re.IGNORECASE).strip()
    if not text:
        return f"ستون {_excel_letter(index)}"
    return text


def _excel_letter(index: int) -> str:
    """۰ -> A، ۲۵ -> Z، ۲۶ -> AA"""
    letters = ""
    n = index
    while True:
        letters = chr(ord("A") + n % 26) + letters
        n = n // 26 - 1
        if n < 0:
            break
    return letters


# ---------------------------------------------------------------- خوندن فایل‌ها

def _read_csv(path: Path) -> pd.DataFrame:
    """CSV رو با تشخیص کدگذاری (utf-8، utf-8-sig، cp1256 و ...) می‌خونه."""
    raw = path.read_bytes()
    encoding = "utf-8-sig"
    try:
        from charset_normalizer import from_bytes
        guess = from_bytes(raw[:200_000]).best()
        if guess and guess.encoding:
            encoding = guess.encoding
    except Exception:
        pass

    last_error = None
    for enc in dict.fromkeys([encoding, "utf-8-sig", "utf-8", "cp1256", "latin-1"]):
        try:
            return pd.read_csv(path, header=None, dtype=object, encoding=enc, sep=None, engine="python")
        except Exception as e:  # noqa: PERF203
            last_error = e
    raise ValueError(f"خوندن CSV ممکن نشد: {last_error}")


def read_tables(path: Path) -> list[tuple[str, pd.DataFrame]]:
    """
    فایل رو می‌خونه و لیستی از (نام شیت، دیتافریم خام) برمی‌گردونه.
    دیتافریم خام یعنی بدون هدر (header=None) تا بتونیم سطر هدر رو خودمون پیدا کنیم.
    """
    suffix = path.suffix.lower()
    if suffix in EXCEL_SUFFIXES:
        try:
            frames = pd.read_excel(path, sheet_name=None, header=None, dtype=object)
        except ImportError as e:
            raise ValueError(
                "برای خوندن این فرمت کتابخانه‌ی مربوطه نصب نیست؛ فایل رو با اکسل به .xlsx ذخیره کنید."
            ) from e
        except Exception as e:
            raise ValueError(f"باز کردن فایل اکسل ممکن نشد: {e}") from e
        return [(str(name).strip() or "sheet1", df) for name, df in frames.items()]

    if suffix in CSV_SUFFIXES:
        return [(path.stem, _read_csv(path))]

    if suffix == ".xls":
        raise ValueError("فرمت قدیمی .xls پشتیبانی نمی‌شه؛ فایل رو با اکسل به .xlsx ذخیره کنید.")

    raise ValueError(f"فرمت «{suffix}» پشتیبانی نمی‌شه. فرمت‌های مجاز: {', '.join(sorted(SUPPORTED_SUFFIXES))}")


def source_files() -> list[Path]:
    """لیست فایل‌هایی که داده‌ی شارژ ازشون خونده میشه (پوشه‌ی آپلود + فایل قدیمی)."""
    files: list[Path] = []
    if config.STORE_FILES_DIR.exists():
        files.extend(
            sorted(p for p in config.STORE_FILES_DIR.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES)
        )
    if not files and config.EXCEL_PATH.exists() and config.EXCEL_PATH.suffix.lower() in SUPPORTED_SUFFIXES:
        files.append(config.EXCEL_PATH)
    return files


# ---------------------------------------------------------------- تشخیص ستون‌ها

def _detect_header_row(df: pd.DataFrame, max_scan: int = 15) -> int:
    """
    سطر هدر رو پیدا می‌کنه. بعضی فایل‌ها یکی‌دو سطر عنوان قبل از هدر دارن؛
    سطری که بیشترین سلول متنیِ یکتا رو داشته باشه هدر فرض میشه.
    """
    best_idx, best_score = 0, float("-inf")
    limit = min(max_scan, len(df))
    for i in range(limit):
        cells = [_format_value(v) for v in df.iloc[i].tolist()]
        non_empty = [c for c in cells if c]
        if len(non_empty) < 2:
            continue
        unique = len(set(non_empty))
        numeric = sum(1 for c in non_empty if re.fullmatch(r"[\d.,]+", c))
        long_text = sum(1 for c in non_empty if len(c) > 40)
        score = unique + 0.6 * len(non_empty) - 1.5 * numeric - 3.0 * long_text - 0.3 * i
        if score > best_score:
            best_score, best_idx = score, i
    return best_idx


def _prepare_sheet(df: pd.DataFrame) -> tuple[list[str], pd.DataFrame, int]:
    """سطر هدر و نام ستون‌ها رو مشخص می‌کنه و ستون‌های کاملاً خالی رو حذف می‌کنه."""
    header_row = _detect_header_row(df)
    header_values = df.iloc[header_row].tolist() if header_row < len(df) else []
    n_cols = max(len(df.columns), len(header_values))
    header_values = list(header_values) + [""] * (n_cols - len(header_values))

    columns: list[str] = []
    used: dict[str, int] = {}
    for i, raw in enumerate(header_values):
        name = _clean_header(raw, i)
        if name in used:
            used[name] += 1
            name = f"{name} ({used[name]})"
        else:
            used[name] = 1
        columns.append(name)

    data = df.iloc[header_row + 1:].reset_index(drop=True)
    data.columns = range(n_cols)

    # ستون‌هایی که توی کل شیت هیچ داده‌ای ندارن (ستون‌های اضافه‌ی اکسل) حذف می‌شن
    keep = [i for i in range(n_cols) if any(not _is_missing(v) for v in data.iloc[:, i].tolist())] if len(data) else list(range(n_cols))
    data = data.iloc[:, keep]
    columns = [columns[i] for i in keep]
    return columns, data, header_row


def _column_values(data: pd.DataFrame, col_idx: int, limit: int = 300) -> list[str]:
    if col_idx >= len(data.columns):
        return []
    values = []
    for v in data.iloc[:, col_idx].tolist():
        if _is_missing(v):
            continue
        text = _format_value(v)
        if text:
            values.append(text)
        if len(values) >= limit:
            break
    return values


def _is_clean_code(value: str) -> bool:
    return bool(re.fullmatch(r"\d{1,7}", _to_english_digits(value).strip()))


def _is_mobile(value: str) -> bool:
    digits = re.sub(r"\D", "", _to_english_digits(value))
    if len(digits) == 10 and digits.startswith("9"):
        return True
    if len(digits) == 11 and digits.startswith("09"):
        return True
    if len(digits) == 12 and digits.startswith("98"):
        return True
    return False


def _ratio(values: list[str], predicate) -> float:
    if not values:
        return 0.0
    return sum(1 for v in values if predicate(v)) / len(values)


# ستون‌هایی که اسمشون شبیه «کد» هست ولی ربطی به کد فروشگاه ندارن (کد کالا، بارکد، شماره اموال و…)
_NON_STORE_CODE_TOKENS = ("کالا", "بارکد", "انبار", "محصول", "رنگ", "ثبت", "دارایی", "قیمت", "مبلغ",
                          "تعداد", "وزن", "sku", "barcode", "inventory", "serial", "imei")


def _score_code_column(key: str, values: list[str]) -> float:
    score = 0.0
    if any(t in key for t in _NON_STORE_CODE_TOKENS):
        return -100.0
    if "کدفروشگاه" in key or "کدفروش" in key:
        score += 130
    if key in ("storecode", "storeid", "code", "shopcode", "branchcode"):
        score += 120
    if "محلاستقرار" in key:
        score += 105
    if "کد" in key:
        score += 60
    if "استقرار" in key:
        score += 45
    if "فروشگاه" in key or "شعبه" in key:
        score += 35
    if "محل" in key:
        score += 15
    for token, penalty in (("شماره", 70), ("اموال", 60), ("imei", 85), ("سری", 55), ("سریال", 55),
                           ("ردیف", 45), ("توضیح", 30), ("مدل", 25), ("برند", 20), ("پنل", 40), ("سیم", 30)):
        if token in key:
            score -= penalty

    clean_ratio = _ratio(values, _is_clean_code)
    mobile_ratio = _ratio(values, _is_mobile)
    score += clean_ratio * 90
    score -= mobile_ratio * 90
    if values and clean_ratio < 0.5:
        score -= (0.5 - clean_ratio) * 110
    return score


def _score_mobile_column(key: str, values: list[str]) -> float:
    score = 0.0
    if "شمارهسیمکارت" in key or "سیمکارت" in key:
        score += 130
    elif ("شمارهسیم" in key) or ("سیم" in key and "شماره" in key):
        score += 120
    if "شمارهپنل" in key or "پنل" in key:
        score += 115
    if any(t in key for t in ("شمارهموبایل", "موبایل", "تلفن", "همراه")):
        score += 110
    if any(t in key for t in ("sim", "mobile", "msisdn", "phone")):
        score += 110
    if "شماره" in key:
        score += 45
    for token, penalty in (("اموال", 95), ("imei", 100), ("سری", 70), ("سریال", 70), ("محل", 35),
                           ("استقرار", 35), ("کد", 30), ("ردیف", 40), ("توضیح", 30)):
        if token in key:
            score -= penalty

    score += _ratio(values, _is_mobile) * 100
    return score


def _score_named_column(key: str, hints: tuple, penalties: tuple = ()) -> float:
    score = 0.0
    for token, weight in hints:
        if token in key:
            score += weight
    for token, weight in penalties:
        if token in key:
            score -= weight
    return score


def _detect_columns(columns: list[str], data: pd.DataFrame) -> dict:
    """ستون کد فروشگاه، شماره سیم‌کارت، اپراتور و محل نصب رو خودکار پیدا می‌کنه."""
    keys = [_col_key(c) for c in columns]
    sampled = {i: _column_values(data, i) for i in range(len(columns))}

    code_scores = [(i, _score_code_column(keys[i], sampled[i])) for i in range(len(columns))]
    code_idx, code_score = (max(code_scores, key=lambda t: t[1]) if code_scores else (None, 0.0))
    code_col = columns[code_idx] if code_idx is not None and code_score > 0 else None

    mobile_scores = [
        (i, _score_mobile_column(keys[i], sampled[i]))
        for i in range(len(columns))
        if i != code_idx
    ]
    mobile_idx, mobile_score = (max(mobile_scores, key=lambda t: t[1]) if mobile_scores else (None, 0.0))
    mobile_col = columns[mobile_idx] if mobile_idx is not None and mobile_score > 40 else None

    operator_hints = (("اپراتور", 120), ("operator", 120), ("carrier", 110), ("شبکه", 60))
    operator_idx, operator_score = (None, 0.0)
    for i, key in enumerate(keys):
        score = _score_named_column(key, operator_hints, (("اموال", 60), ("ردیف", 40), ("محل", 30), ("برند", 40)))
        if not score:
            # وقتی ستون «اپراتور» نداریم: ستونی که بیشتر خوندهاش اسم اپراتورهای شناخته‌شده‌ست
            values = sampled[i]
            hits = sum(1 for v in values if any(op in v.lower() for op in _KNOWN_OPERATORS))
            if values and hits / len(values) >= 0.6 and hits >= 2:
                score = 70
        if score > operator_score:
            operator_idx, operator_score = i, score
    operator_col = columns[operator_idx] if operator_score >= 60 else None

    location_hints = (("محلنصب", 130), ("محل‌نصب", 130), ("محل نصب", 130), ("نصبدرفروشگاه", 120),
                      ("محلاستقرار", 90), ("موقعیت", 80), ("آدرس", 75), ("نشانی", 70), ("محل", 40), ("location", 80))
    location_idx, location_score = (None, 0.0)
    for i, key in enumerate(keys):
        if i in (code_idx, mobile_idx):
            continue
        score = _score_named_column(key, location_hints, (("شماره", 60), ("اموال", 60), ("ردیف", 40)))
        if score > location_score:
            location_idx, location_score = i, score
    location_col = columns[location_idx] if location_score >= 60 else None

    return {
        "code_col": code_col,
        "mobile_col": mobile_col,
        "operator_col": operator_col,
        "location_col": location_col,
        "code_idx": code_idx if code_col else None,
        "mobile_idx": mobile_idx if mobile_col else None,
        "operator_idx": operator_idx if operator_col else None,
        "location_idx": location_idx if location_col else None,
        "scores": {
            "code": [[columns[i], round(s, 1)] for i, s in sorted(code_scores, key=lambda t: -t[1])[:3]],
            "mobile": [[columns[i], round(s, 1)] for i, s in sorted(mobile_scores, key=lambda t: -t[1])[:3]],
        },
    }


# ---------------------------------------------------------------- استخراج کد

# الگوهای «کد فروشگاه داخل متن» — فقط وقتی یه کلیدواژه یا ساختار مشخص کنار عدد باشه،
# تا «بسته ۶۰ گیگی» یا تاریخ‌هایی مثل ۱۴۰۴/۱۱/۰۲ اشتباهی کد حساب نشن.
_KEYWORD_CODE_PATTERNS = (
    re.compile(r"فروشگاه\s*[:\-–—]?\s*(\d{1,7})(?!\d)"),
    re.compile(r"جمع\s*آوری[^()]{0,25}?(\d{1,7})(?!\d)"),
    re.compile(r"کد\s*فروشگاه\s*[:\-–—]?\s*(\d{1,7})(?!\d)"),
    re.compile(r"\(\s*(\d{1,7})\s*\)"),
)
_CODE_PREFIX_RE = re.compile(r"^(\d{1,7})(?!\d)\s*[-–—:،,(]?\s+\S")
# ستون‌هایی که هیچ‌وقت نباید ازشون کد استخراج بشه (شماره سیم‌کارت/دارایی/سریال)
_SKIP_SCAN_TOKENS = ("اموال", "imei", "سریال", "سری")
# ستون‌های توضیحات/یادداشت که ممکنه داخلشون به یه فروشگاه اشاره شده باشه
_NOTES_TOKENS = ("توضیح", "شرح", "یادداشت", "ملاحظه", "comment", "note", "remark")


def _range_codes(text: str) -> list[str] | None:
    """
    بازه/لیست کدها مثل «1249-1250» یا «7015، 7016» رو می‌شناسه.
    عمداً سخت‌گیره تا شماره‌های سریال مثل «215-302-500-3349» یا «865-294-015-430-011» کد حساب نشن.
    """
    parts = re.split(r"\s*[,،;/|\-–—]\s*", text)
    if not 2 <= len(parts) <= 3:
        return None
    if not all(re.fullmatch(r"\d{3,4}", p) for p in parts):
        return None
    if len({len(p) for p in parts}) != 1:
        return None
    if sum(len(p) for p in parts) < 7:
        return None
    return parts


def _extract_from_text(value) -> tuple[list[str], str]:
    """
    از یه سلول، کد(های) فروشگاه رو درمیاره.
    خروجی: (لیست کدها، سطح اطمینان: clean | range | pattern)
    """
    text = _to_english_digits(re.sub(r"\s+", " ", _format_value(value))).strip()
    if not text:
        return [], ""

    if re.fullmatch(r"\d{1,7}", text):
        return [text], "clean"

    codes = _range_codes(text)
    if codes:
        return codes, "range"

    # «۴۰۸۱- سرقت شده…» یا «۱۰۷۱ سرقت شده به دلیل…» یا «۳۵۵۰ جمع آوری»
    match = _CODE_PREFIX_RE.match(text)
    if match:
        return [match.group(1)], "pattern"

    for pattern in _KEYWORD_CODE_PATTERNS:
        found = pattern.search(text)
        if found:
            return [found.group(1)], "pattern"

    return [], ""


def extract_codes(value) -> tuple[list[str], bool]:
    """سازگاری با نسخه‌ی قبل: کدهای یک سلول + اینکه «کد مستقیم» بوده یا نه."""
    codes, confidence = _extract_from_text(value)
    return codes, confidence in ("clean", "range")


def extract_row_codes(values: list, scan_idxs: list[int]) -> tuple[list[str], str]:
    """
    کد فروشگاه یه ردیف رو از ستون‌های داده‌شده (به‌ترتیب اولویت) پیدا می‌کنه.
    ایندکس‌ها معمولاً شامل ستون کد فروشگاه، ستون محل نصب و ستون توضیحاته؛
    بقیه‌ی ستون‌ها (شماره سیم‌کارت، شماره اموال، سریال، شماره ردیف و…) عمداً اسکن نمی‌شن
    تا داده‌ی اونا اشتباهی کد فروشگاه حساب نشه.
    """
    for i in scan_idxs:
        if not 0 <= i < len(values):
            continue
        codes, confidence = _extract_from_text(values[i])
        if codes:
            return codes, confidence
    return [], ""


# ---------------------------------------------------------------- ساخت ایندکس

def _override_for(sheet_name: str) -> dict | None:
    """اگه ادمین برای این شیت توی config.SHEETS ستون‌های مشخصی تعیین کرده، برمی‌گردونه."""
    key = _col_key(sheet_name)
    for sheet_cfg in getattr(config, "SHEETS", []) or []:
        if _col_key(sheet_cfg.get("name", "")) == key:
            return sheet_cfg
    return None


def _index_sheet(file_name: str, sheet_name: str, raw_df: pd.DataFrame, new_index: dict[str, list[dict]],
                 seen: set) -> dict:
    """یه شیت رو می‌خونه، رکوردها رو می‌سازه و توی new_index اضافه می‌کنه."""
    label = sheet_name.strip() or sheet_name
    info: dict = {
        "file": file_name,
        "sheet": label,
        "rows": int(len(raw_df)),
        "indexed": 0,
        "inferred": 0,
        "multi": 0,
        "no_code": 0,
        "unique_codes": 0,
        "columns": [],
        "code_col": None,
        "mobile_col": None,
        "operator_col": None,
        "location_col": None,
        "skipped_reason": None,
        "override": False,
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
    override = _override_for(sheet_name)
    if override:
        index_of = {c: i for i, c in enumerate(columns)}
        for field in ("code_col", "mobile_col", "operator_col", "location_col"):
            wanted = override.get(field)
            if not wanted:
                continue
            match = next((c for c in columns if _col_key(c) == _col_key(wanted)), None)
            if match:
                detected[field] = match
                detected[field + "_idx"] = index_of[match]
        info["override"] = True

    info["code_col"] = detected["code_col"]
    info["mobile_col"] = detected["mobile_col"]
    info["operator_col"] = detected["operator_col"]
    info["location_col"] = detected["location_col"]
    info["detect_scores"] = detected["scores"]

    code_idx = detected["code_idx"]
    if code_idx is None:
        info["skipped_reason"] = "ستون کد فروشگاه پیدا نشد (هیچ ستونی شبیه کد نبود)."
        return info

    mobile_idx = detected["mobile_idx"]
    operator_idx = detected["operator_idx"]
    location_idx = detected["location_idx"]

    # ستون‌هایی که برای پیدا کردن کد فروشگاه اسکن می‌شن: اول ستون کد، بعد «محل نصب»،
    # بعد ستون‌های توضیحات. بقیه (شماره سیم‌کارت/دارایی/سریال/شماره ردیف) عمداً اسکن نمی‌شن.
    scan_idxs = [i for i in (code_idx, location_idx) if i is not None]
    for i, col in enumerate(columns):
        if i in scan_idxs or i == mobile_idx:
            continue
        key = _col_key(col)
        if any(t in key for t in _NOTES_TOKENS) and not any(t in key for t in _SKIP_SCAN_TOKENS):
            scan_idxs.append(i)

    codes_here: set[str] = set()
    for row_no, row in enumerate(data.itertuples(index=False), start=1):
        values = list(row)
        codes, confidence = extract_row_codes(values, scan_idxs)
        if not codes:
            info["no_code"] = info.get("no_code", 0) + 1
            continue
        if confidence == "pattern":
            info["inferred"] += 1
        elif confidence == "range":
            info["multi"] = info.get("multi", 0) + 1

        record = {
            "source": label,
            "_file": file_name,
            "_sheet": label,
            "_row": row_no,
            "_inferred": confidence == "pattern",
            "mobile": _format_value(values[mobile_idx]) if mobile_idx is not None else "",
            "operator": _format_value(values[operator_idx]) if operator_idx is not None else "",
            "location": _format_value(values[location_idx]) if location_idx is not None else "",
        }
        for i, col in enumerate(columns):
            record[col] = _format_value(values[i])

        base_fingerprint = tuple(sorted((k, v) for k, v in record.items() if not k.startswith("_") and k != "source"))
        for code in codes:
            codes_here.add(code)
            fingerprint = (label, code, base_fingerprint)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            new_index.setdefault(code, []).append({**record, "code": code})
            info["indexed"] += 1

    info["unique_codes"] = len(codes_here)
    if info["indexed"] == 0:
        info["skipped_reason"] = "هیچ ردیفی با کد فروشگاه معتبر پیدا نشد."
    return info


# موتور مشترک اینجا import می‌شود (توابع پایه‌ی همین ماژول تا این نقطه تعریف شده‌اند)
# و خودِ موتور هم ماژول ما را تنبلانه می‌خواند، پس حلقه‌ی import نمی‌سازد.
from app.store_engine import Engine as _Engine, Profile as _Profile


class ChargeProfile(_Profile):
    """مشخصات مجموعه‌داده‌ی «شارژ»؛ بقیه‌ی کار به موتور مشترک واگذار می‌شود."""

    key = "charge"
    label = "شارژ"
    noun = "شارژ"
    schema_path = config.STORE_SCHEMA_PATH
    snapshot_path = config.STORE_SNAPSHOT_PATH
    snapshot_version = SNAPSHOT_VERSION
    use_alias = True
    nothing_uploaded = "هنوز هیچ فایل شارژی آپلود نشده."
    nothing_found = "هیچ کد فروشگاهی"
    replace_tail = "کل داده‌ی شارژ فعلی با همین فایل ساخته می‌شه"
    replace_tail_replace = "کل داده‌ی شارژ فعلی با همین فایل ساخته می‌شه"

    def source_files(self) -> list[Path]:
        """لیست فایل‌هایی که داده‌ی شارژ ازشون خونده می‌شه (پوشه‌ی آپلود + فایل قدیمی)."""
        files: list[Path] = []
        if config.STORE_FILES_DIR.exists():
            files.extend(
                sorted(p for p in config.STORE_FILES_DIR.iterdir()
                       if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES)
            )
        if not files and config.EXCEL_PATH.exists() and config.EXCEL_PATH.suffix.lower() in SUPPORTED_SUFFIXES:
            files.append(config.EXCEL_PATH)
        return files

    def sheet_report_shell(self, file_name: str, sheet_name: str, rows: int) -> dict:
        return {
            "file": file_name,
            "sheet": sheet_name,
            "rows": rows,
            "indexed": 0,
            "inferred": 0,
            "multi": 0,
            "no_code": 0,
            "unique_codes": 0,
            "columns": [],
            "code_col": None,
            "mobile_col": None,
            "operator_col": None,
            "location_col": None,
            "skipped_reason": None,
            "override": False,
        }

    def index_sheet(self, file_name, sheet_name, raw_df, new_index, seen):
        return _index_sheet(file_name, sheet_name, raw_df, new_index, seen)

    def totals_from(self, sheets, file_count, index):
        totals = super().totals_from(sheets, file_count, index)
        totals["inferred"] = sum(s.get("inferred", 0) for s in sheets)
        return totals


ENGINE = _Engine(ChargeProfile())
_lock = ENGINE._lock


def source_files() -> list[Path]:
    """لیست فایل‌های فعال داده‌ی شارژ."""
    return ChargeProfile().source_files()


def build_from_paths(paths: list[Path]) -> tuple[dict[str, list[dict]], dict]:
    """ایندکس + گزارش کامل از روی فایل‌ها (بدون تغییر کش)."""
    return ENGINE.build_from_paths(paths)


# ---------------------------------------------------------------- تشخیص تغییر و اعمال

def _data_hash(record: dict) -> str:
    """اثر انگشت «داده‌ی» یه رکورد؛ فایل/شیت/شماره‌ی ردیف توی مقایسه دخالت ندارن."""
    payload = {k: str(v) for k, v in record.items() if not k.startswith("_") and k != "source"}
    blob = "\u241f".join(f"{k}\u241e{value}" for k, value in sorted(payload.items()))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _similarity(old: dict, new: dict) -> float:
    """چند درصد فیلدهای دو رکورد یکیه (برای تشخیص «همون رکورد، دیتا عوض شده»)."""
    keys = {k for k, v in list(old.items()) + list(new.items())
            if not k.startswith("_") and k != "source" and str(v or "").strip()}
    if not keys:
        return 0.0
    same = sum(1 for k in keys if str(old.get(k, "")).strip() == str(new.get(k, "")).strip())
    return same / len(keys)


def _pair_records(old_recs: list[dict], new_recs: list[dict]) -> list[tuple[int, int]]:
    """رکوردهای قدیم و جدید یه کد رو جفت می‌کنه تا «یکسان» از «تغییرکرده» جدا بشه."""
    used: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for ni, nr in enumerate(new_recs):
        h = _data_hash(nr)
        match = next((oi for oi, orr in enumerate(old_recs) if oi not in used and _data_hash(orr) == h), None)
        if match is not None:
            used.add(match)
            pairs.append((match, ni))

    for ni, nr in enumerate(new_recs):
        if any(pair[1] == ni for pair in pairs):
            continue
        best, best_score = None, 0.0
        for oi, orr in enumerate(old_recs):
            if oi in used:
                continue
            score = _similarity(orr, nr)
            if score > best_score:
                best, best_score = oi, score
        if best is not None and best_score >= 0.5:
            used.add(best)
            pairs.append((best, ni))

    return sorted(pairs, key=lambda pair: pair[1])


def _values_of_column(recs: list[dict], col: str) -> list[str]:
    values = [str(r.get(col, "")).strip() for r in recs]
    return sorted(v for v in values if v)


def diff_index(old_index: dict, new_index: dict) -> dict:
    """
    دو ایندکس رو مقایسه می‌کنه و می‌گه چی اضافه شده، چی عوض شده و چی دیگه توی فایل نیست.
    خروجی: {"entries": [...], "counts": {...}} — هر entry برای یه کد فروشگاهه.
    """
    entries: list[dict] = []
    unchanged = 0
    unchanged_records = 0
    codes = sorted(set(old_index) | set(new_index), key=lambda c: (len(c), c))

    for code in codes:
        new_recs = new_index.get(code) or []
        old_recs = old_index.get(code) or []

        if not old_recs:
            entries.append({"code": code, "status": "added", "records": len(new_recs),
                            "source": new_recs[0].get("source", ""), "fields": {}})
            continue
        if not new_recs:
            entries.append({"code": code, "status": "removed", "records": len(old_recs),
                            "source": old_recs[0].get("source", ""), "fields": {}})
            continue

        cols = {k for r in new_recs + old_recs for k, v in r.items()
                if not k.startswith("_") and k not in ("source", "code") and str(v or "").strip()}
        fields: dict[str, tuple[str, str]] = {}
        seen_pairs: set[tuple[str, str]] = set()
        core = {"mobile", "operator", "location"}
        # اول ستون‌های واقعی فایل، بعد فیلدهای اصلی (که معمولاً کپیِ همون ستون‌ان ⇒ تکراری نشون داده نمی‌شن)
        for col in sorted(cols - core) + sorted(cols & core):
            before, after = _values_of_column(old_recs, col), _values_of_column(new_recs, col)
            if before == after:
                continue
            if col in core and (", ".join(before) or "—", ", ".join(after) or "—") in seen_pairs:
                continue
            fields[col] = ("، ".join(before) or "—", "، ".join(after) or "—")
            seen_pairs.add(fields[col])

        paired = len(_pair_records(old_recs, new_recs))
        extra_new = max(0, len(new_recs) - paired)
        extra_old = max(0, len(old_recs) - paired)
        if fields or extra_new or extra_old:
            entries.append({
                "code": code, "status": "changed", "records": len(new_recs),
                "source": new_recs[0].get("source", ""), "fields": fields,
                "added_records": extra_new, "removed_records": extra_old,
            })
        else:
            unchanged += 1
            unchanged_records += len(new_recs)

    counts = {
        "added": sum(1 for e in entries if e["status"] == "added"),
        "changed": sum(1 for e in entries if e["status"] == "changed"),
        "removed": sum(1 for e in entries if e["status"] == "removed"),
        "unchanged": unchanged,
        "changed_fields": sum(len(e["fields"]) for e in entries),
    }
    return {"entries": entries, "counts": counts}


# اگر فایل جدید این‌درصد از کدهای فعلی را داشته باشد، «عکس کامل» حساب می‌شود
# و جایگزینی کامل پیشنهاد می‌شود؛ وگرنه ادغام تغییرات.
AUTO_REPLACE_COVERAGE = 0.90


def detect_mode(old_index: dict, new_index: dict,
                threshold: float = AUTO_REPLACE_COVERAGE) -> dict:
    """تشخیص خودکار «جایگزینی» یا «ادغام» بر اساس پوشش کد.

    اگر فایل جدید تقریباً همه‌ی کدهای فعلی را داشته باشد، یک عکس کامل است و
    باید جایگزین شود (وگرنه فروشگاه‌هایی که از آن حذف شده‌اند برای همیشه
    می‌مانند). اگر فقط بخشی از کدها را داشته باشد، یک فایل تغییرات است و باید
    ادغام شود (وگرنه بقیه‌ی داده پاک می‌شود).
    """
    old_codes = set(old_index)
    new_codes = set(new_index)
    total = len(old_codes)
    covered = len(old_codes & new_codes)

    coverage = (covered / total) if total else 0.0
    if not total:
        # داده‌ی فعلی خالیه؛ جایگزینی و ادغام فرقی نمی‌کنن، ادغام امن‌تره
        return {"mode": "merge", "reason": "داده‌ی فعلی خالیه", "coverage": 1.0,
                "covered": 0, "total": 0, "new_codes": len(new_codes),
                "missing": 0, "threshold": threshold}
    if not new_codes:
        return {"mode": "merge", "reason": "فایل جدید کدی نداره", "coverage": 0.0,
                "covered": 0, "total": total, "new_codes": 0,
                "missing": total, "threshold": threshold}

    percent = round(coverage * 100)
    if coverage >= threshold:
        return {"mode": "replace", "coverage": coverage, "covered": covered, "total": total,
                "new_codes": len(new_codes), "missing": total - covered, "threshold": threshold,
                "reason": f"{percent}٪ از کدهای فعلی ({covered} از {total}) داخل فایل جدید هست → عکس کامل"}
    return {"mode": "merge", "coverage": coverage, "covered": covered, "total": total,
            "new_codes": len(new_codes), "missing": total - covered, "threshold": threshold,
            "reason": f"فقط {percent}٪ از کدهای فعلی ({covered} از {total}) داخل فایل جدید هست → فایل تغییرات"}


def auto_mode_message(detect: dict) -> str:
    """جمله‌ی فارسی تشخیص خودکار برای نمایش بالای پیش‌نمایش."""
    if not detect:
        return ""
    if detect["mode"] == "replace":
        label = "جایگزینی کامل"
        warn = (f"⚠️ توجه: {detect['missing']} کد فعلی توی فایل جدید نیست و با جایگزینی حذف می‌شه."
                if detect["missing"] else "هیچ کدی از داده‌ی فعلی حذف نمی‌شه.")
    else:
        label = "فقط تغییرات (ادغام)"
        warn = f"{detect['missing']} کد فعلی توی فایل جدید نیست و دست‌نخورده می‌مونه."
    return f"🤖 تشخیص خودکار: {label} — {detect['reason']}\n{warn}\nاگر تشخیص سیستم را پسندیدی با «اعمال تغییرات» بزن؛ اگر نه، همین‌جا حالت را عوض کن و دوباره آپلود کن."


def merge_index(old_index: dict, new_index: dict, drop_missing: bool = False) -> dict:
    """
    ایندکس جدید و قدیم رو ادغام می‌کنه: رکوردهای جدید اضافه، رکوردهای تغییرکرده جایگزین
    و رکوردهایی که توی فایل جدید نیستن حفظ می‌شن (مگر اینکه drop_missing روشن باشه).
    """
    merged: dict[str, list[dict]] = {} if drop_missing else {
        code: [dict(r) for r in recs] for code, recs in old_index.items()
    }

    for code, new_recs in new_index.items():
        if code not in merged:
            merged[code] = [dict(r) for r in new_recs]
            continue

        old_recs = merged[code]
        pairs = _pair_records(old_recs, new_recs)
        out: list[dict] = []
        replaced_old: set[int] = set()

        for oi, ni in pairs:
            nr, orr = new_recs[ni], old_recs[oi]
            if _data_hash(nr) == _data_hash(orr):
                # دیتا یکیه؛ فقط مشخصات فایل/ردیف به‌روز می‌شه
                out.append({**orr, **{k: nr.get(k, orr.get(k)) for k in ("_file", "_sheet", "_row", "source")}})
            else:
                out.append(nr)
            replaced_old.add(oi)

        paired_new = {ni for _, ni in pairs}
        for ni, nr in enumerate(new_recs):
            if ni not in paired_new:
                out.append(nr)

        if not drop_missing:
            for oi, orr in enumerate(old_recs):
                if oi not in replaced_old:
                    out.append(orr)

        merged[code] = out

    return merged


def diff_summary(diff: dict, mode: str = "merge", drop_missing: bool = False) -> str:
    """خلاصه‌ی فارسی تغییرات برای پنل/ربات."""
    counts = diff.get("counts", {})
    if mode == "merge":
        removed_label = "کد که توی فایل جدید نبود و حذف میشه" if drop_missing else "کد که توی فایل جدید نبود (دست‌نخورده)"
    else:
        removed_label = "کد که توی فایل جدید نبود (با جایگزینی کامل حذف میشه)"
    return " | ".join([
        f"🆕 {counts.get('added', 0)} کد جدید",
        f"🔄 {counts.get('changed', 0)} کد تغییرکرده",
        f"➖ {counts.get('removed', 0)} {removed_label}",
        f"✅ {counts.get('unchanged', 0)} کد بدون تغییر",
    ])


# ---------------------------------------------------------------- کش و ری‌لود

def reload_data() -> tuple[bool, str]:
    """همه‌ی فایل‌های شارژ را از دیسک می‌خواند و کش را به‌روز می‌کند."""
    return ENGINE.reload()


def preview_upload(paths: list[Path], mode: str = "merge", drop_missing: bool = False) -> tuple[bool, str, dict]:
    """تغییرات را گزارش می‌دهد بدون آنکه چیزی اعمال شود."""
    return ENGINE.preview_upload(paths, mode, drop_missing)


def commit_upload(paths: list[Path], mode: str = "merge", drop_missing: bool = False) -> tuple[bool, str, dict]:
    """تغییرات را اعمال می‌کند."""
    return ENGINE.commit_upload(paths, mode, drop_missing)


def report_summary(report: dict, max_warnings: int = 6) -> str:
    return ENGINE.report_summary(report, max_warnings)


def _auto_reload() -> None:
    """اگر فایل‌ها عوض شده باشند، خودکار ری‌لود می‌کند."""
    ENGINE.auto_reload()


# ---------------------------------------------------------------- API عمومی

def lookup(store_code: str) -> list[dict] | None:
    """کد فروشگاه را جستجو می‌کند؛ «۰۰۱» هم کد «۱» را پیدا می‌کند."""
    return ENGINE.lookup(store_code)


def list_all() -> list[dict]:
    """همه‌ی رکوردهای شارژ را فلت‌شده برمی‌گرداند."""
    return ENGINE.list_all()


def get_schema() -> dict:
    """گزارش کامل تشخیص (همان چیزی که در store_schema.json ذخیره می‌شود)."""
    return ENGINE.get_schema()


def get_source_columns(source: str) -> list[str]:
    """ستون‌های داده‌ی یک شیت (بدون ستون‌های متا) به ترتیب فایل."""
    return ENGINE.get_source_columns(source)


def get_store_files() -> list[dict]:
    return ENGINE.get_source_files_info()


def get_status() -> dict:
    status = ENGINE.get_status()
    return {
        "loaded_stores": status["loaded_codes"],
        "total_records": status["total_records"],
        "sheets": status["sheets"],
        "skipped_sheets": status["skipped_sheets"],
        "last_error": status["last_error"],
        "excel_exists": config.EXCEL_PATH.exists(),
        "files": status["files"],
        "generated_at": status["generated_at"],
    }


# --- تنظیمات فیلدها (ترتیب/مخفی/لیبل ستون‌ها از پنل) ---
FIELDS_CONFIG_PATH = config.DATA_DIR / "fields_config.json"


def _load_fields_config() -> dict:
    if FIELDS_CONFIG_PATH.exists():
        try:
            with open(FIELDS_CONFIG_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def get_visible_columns(sheet_name: str) -> list[str]:
    """ستون‌های قابل نمایش یه شیت رو بر اساس تنظیمات پنل برمی‌گردونه (اگه تنظیمی نبود، ترتیب خود فایل)."""
    cfg = _load_fields_config()
    saved = cfg.get("stores", {}).get(sheet_name, {})
    order = saved.get("order", [])
    if order:
        return list(order)
    return get_source_columns(sheet_name)


def get_hidden_columns(sheet_name: str) -> set[str]:
    """ستون‌های مخفی یه شیت رو بر اساس تنظیمات پنل برمی‌گردونه."""
    cfg = _load_fields_config()
    saved = cfg.get("stores", {}).get(sheet_name, {})
    return set(saved.get("hidden", []))


def get_field_labels(sheet_name: str) -> dict:
    """لیبل‌های سفارشی ستون‌های یه شیت (از پنل)."""
    cfg = _load_fields_config()
    saved = cfg.get("stores", {}).get(sheet_name, {})
    return dict(saved.get("labels", {}))


def has_field_config(sheet_name: str) -> bool:
    cfg = _load_fields_config()
    return bool(cfg.get("stores", {}).get(sheet_name))


# در زمان استارت اپ، اگه فایلی از قبل هست، بارگذاریش کن
reload_data()
