"""
تشخیص هوشمند نوع فایل: هر شیتِ آپلودشده belonged به «شارژ» است یا «اطلاعات»؟

تا قبل، دو صفحه‌ی جداگانه در پنل بود و کاربر خودش باید حدس می‌زد فایلش کدام‌یک است
— و اگر اشتباه می‌فرستاد، یا شیت‌ها بی‌صدا ایندکس نمی‌شدند یا داده‌ی قاطی می‌شد.
این ماژول خودش از روی *محتوای* ستون‌ها تصمیم می‌گیرد:

  • ستون‌های دستگاه/سیم‌کارت/دارایی (IMEI، شماره سیم‌کارت، شماره اموال، سریال،
    برند دستگاه، اپراتور، محل نصب) → فایل «شارژ».
  • ستون‌های آدرس/شخص (آدرس، کد پستی، ایمیل، تلفن، کد پرسنلی، مدیر، سرپرست،
    نام فروشگاه، استان، شهر، Lat/Long) → فایل «اطلاعات».
  • شیتی که اصلاً ستون کد فروشگاه ندارد (مثل «مدیران» یا «کد پرسنلی») → هیچ‌کدام
    (skip)، چون ربطی به فروشگاه ندارد.

خروجی، پیشنهاد است نه تصمیم نهایی: `main.py` ازش برای مسیریابی خودکار و
بازخورد صفحه‌ی آپلود استفاده می‌کند و کاربر هر وقت خواست می‌تواند دستی بازنویسی کند.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from app import config, xlsx_sheet
from app.data_store import (
    SUPPORTED_SUFFIXES,
    _clean_header,
    _col_key,
    _is_missing,
    _prepare_sheet,
    read_tables,
)

CHARGE = "charge"
INFO = "info"
SKIP = "skip"

# نکته‌ی مهم: `_col_key` فاصله/نیم‌فاصله/علائم را حذف می‌کند، پس کلیدواژه‌ها باید
# هم به همان شکل نرمال‌شده دربیایند. به‌جای نوشتن دستی نسخه‌ی بی‌فاصله، همه‌ی
# کلیدواژه‌ها را یک‌بار از همان تابع رد می‌دهیم تا هیچ‌کدام بی‌صدا بی‌اثر نشوند.
def _norm_hints(words: tuple[str, ...]) -> tuple[str, ...]:
    out: list[str] = []
    for word in words:
        key = _col_key(word)
        if key and key not in out:
            out.append(key)
    return tuple(out)


_CHARGE_HINTS = _norm_hints((
    "imei", "imie", "شماره سیم کارت", "سیم کارت", "sim", "شماره پنل", "پنل",
    "شماره اموال", "اموال", "serial", "سریال", "سری دستگاه", "برند دستگاه",
    "مدل دستگاه", "اپراتور", "محل نصب", "unlock", "قفل", "مودم", "روتر",
    "دانگل", "آنتن", "تجهیزات",
))

_INFO_HINTS = _norm_hints((
    "کد پستی", "پستی", "آدرس", "نشانی", "استان", "شهر", "نام فروشگاه", "نوع فروشگاه",
    "گرید فروشگاه", "کد پرسنلی", "پرسنلی", "مدیر کل", "مدیر منطقه", "مدیر",
    "سرپرست", "ایمیل", "تلفن", "تاریخ استخدام", "کد انبار", "افتتاحیه", "منطقه",
    "lat", "long", "عرض جغرافیایی", "طول جغرافیایی", "توضیحات فعالیت",
))

# ستون‌هایی که توی هر دو نوع فایل هستن و به‌تنهایی هیچ سیگنالی نمی‌دن
_NEUTRAL_HINTS = _norm_hints((
    "ردیف", "nan", "توضیحات", "کد فروشگاه", "محل استقرار فعلی", "فروشگاه",
    "منطقه فروش", "منطقه نهائی", "منطقه نهایی",
))

# ستون‌هایی که هیچ‌وقت کد فروشگاه نیستن، حتی اگر محتوایشان عددی باشد
_NON_STORE_CODE_HINTS = _norm_hints((
    "ردیف", "کد پرسنلی", "پرسنلی", "کد پستی", "تلفن", "موبایل", "شماره",
    "imei", "سریال", "سری دستگاه", "تعداد", "منطقه", "کد انبار",
))

_STORE_CODE_HINTS = _norm_hints((
    "کد فروشگاه", "کد فروشگاهي", "کدفروشگاه", "shop code", "store code",
))

# ستونی که در فایل‌های شارژ نقش «کد فروشگاه» را بازی می‌کند
_CHARGE_CODE_COLUMNS = _norm_hints(("محل استقرار فعلی", "محل استقرار", "محل نصب"))


def _score_hints(columns: list[str], hints: tuple[str, ...]) -> tuple[float, list[str]]:
    """امتیاز ستون‌ها بر اساس کلیدواژه‌ها. ستون‌های خنثی حساب نمی‌شن."""
    score = 0.0
    hits: list[str] = []
    for col in columns:
        key = _col_key(col)
        if not key or any(n in key for n in _NEUTRAL_HINTS):
            continue
        matched = [h for h in hints if h in key]
        if matched:
            # ستون‌هایی که چند کلیدواژه دارن (مثل «شماره سیم کارت دانگل») سیگنال قوی‌تری دارن
            score += 1.0 + 0.35 * (len(matched) - 1)
            hits.append(col)
    return score, hits


def _has_store_code_column(columns: list[str], data: pd.DataFrame) -> tuple[bool, list[str]]:
    """آیا شیت ستونی دارد که *احتمالاً* کد فروشگاه است؟

    سه سیگنال به‌ترتیب قدرت:
      ۱) ستونی که اسمش صریحاً «کد فروشگاه» است (فایل‌های اطلاعات).
      ۲) ستونی مثل «محل استقرار فعلی» که در فایل‌های شارژ نقش کد را دارد.
      ۳) ستونی با اسم ناشناخته که محتوایش بیشترش عدد کوتاه و تمیز است
         (تعمیم به چیدمان‌های جدیدی که هنوز ندیده‌ایم).

    نام ستون‌های شناخته‌شده‌ی *غیر*کدی مثل «شماره اموال» یا «سریال دستگاه» هیچ‌وقت
    کد فروشگاه حساب نمی‌شوند، حتی اگر محتوایشان عددی باشد.
    """
    named: list[str] = []
    for col in columns:
        key = _col_key(col)
        if any(t in key for t in _STORE_CODE_HINTS):
            named.append(col)
    if named:
        return True, named

    for col in columns:
        key = _col_key(col)
        if any(t in key for t in _NON_STORE_CODE_HINTS):
            continue
        if any(t in key for t in _CHARGE_CODE_COLUMNS):
            return True, [col]

    # در نهایت: ستون ناشناخته‌ای که بیشترِ محتویاتش عدد کوتاهِ تمیز است
    for i, col in enumerate(columns):
        if i >= len(data.columns):
            continue
        key = _col_key(col)
        if any(t in key for t in _NON_STORE_CODE_HINTS):
            continue
        values = [str(v).strip() for v in data.iloc[:, i].tolist()[:200] if not _is_missing(v)]
        if not values:
            continue
        clean = sum(1 for v in values if v.isdigit() and 2 <= len(v) <= 7)
        if clean / len(values) >= 0.6:
            return True, [col]
    return False, []


def classify_sheet(columns: list[str], data: pd.DataFrame) -> dict:
    """یک شیتِ آماده‌شده (هدر جدا شده) را دسته‌بندی می‌کند.

    خروجی: {"kind": charge|info|skip, "charge": نمره, "info": نمره,
             "hits": {...}, "reason": str}
    """
    charge_score, charge_hits = _score_hints(columns, _CHARGE_HINTS)
    info_score, info_hits = _score_hints(columns, _INFO_HINTS)

    has_code, code_cols = _has_store_code_column(columns, data)
    # فایل اطلاعاتِ این مجموعه همیشه ستونی به اسم «کد فروشگاه» دارد؛ فایل شارژ
    # به‌جای آن از «محل استقرار فعلی» استفاده می‌کند. این قوی‌ترین تفکیک‌کننده است.
    named_code = any(any(t in _col_key(c) for t in _STORE_CODE_HINTS) for c in code_cols)

    if not has_code:
        return {
            "kind": SKIP, "charge": round(charge_score, 2), "info": round(info_score, 2),
            "hits": {"charge": charge_hits, "info": info_hits},
            "reason": "ستون کد فروشگاه پیدا نشد (شیت مربوط به فروشگاه نیست).",
        }

    if charge_score <= 0 and info_score <= 0:
        kind = INFO if named_code else CHARGE
        return {
            "kind": kind, "charge": 0.0, "info": 0.0,
            "hits": {"charge": [], "info": []},
            "reason": ("ستون «کد فروشگاه» دارد → فایل اطلاعات."
                       if named_code else "فقط ستون محل استقرار دارد → فایل شارژ."),
        }

    if charge_score == info_score:
        kind = INFO if named_code else CHARGE
        reason = "امتیاز دو نوع برابر بود؛ از روی نام ستون کد تصمیم گرفته شد."
    else:
        kind = CHARGE if charge_score > info_score else INFO
        reason = ("ستون‌های دستگاه/سیم‌کارت بیشتر بود." if kind == CHARGE
                  else "ستون‌های آدرس/مشخصات فروشگاه بیشتر بود.")

    return {
        "kind": kind,
        "charge": round(charge_score, 2),
        "info": round(info_score, 2),
        "margin": round(abs(charge_score - info_score), 2),
        "hits": {"charge": charge_hits, "info": info_hits},
        "code_candidates": code_cols,
        "reason": reason,
    }


def classify_file(path: Path) -> dict:
    """همه‌ی شیت‌های یک فایل را دسته‌بندی می‌کند.

    خروجی: {"path": Path, "sheets": {sheet_name: verdict}, "charge": [..], "info": [..],
             "skip": [..], "ok": bool}
    """
    result = {"path": path, "sheets": {}, "charge": [], "info": [], "skip": [], "ok": False,
              "error": None}
    try:
        tables = read_tables(path)
    except Exception as exc:  # noqa: BLE001 - گزارش خطا به پنل
        result["error"] = str(exc)
        return result

    for sheet_name, df in tables:
        label = (sheet_name or "").strip() or sheet_name
        try:
            if df is None or len(df) == 0:
                verdict = {"kind": SKIP, "charge": 0.0, "info": 0.0,
                           "hits": {"charge": [], "info": []}, "reason": "شیت خالیه."}
            else:
                columns, data, _header_row = _prepare_sheet(df)
                if not columns:
                    verdict = {"kind": SKIP, "charge": 0.0, "info": 0.0,
                               "hits": {"charge": [], "info": []}, "reason": "هیچ ستونی پیدا نشد."}
                else:
                    verdict = classify_sheet(columns, data)
        except Exception as exc:  # noqa: BLE001
            verdict = {"kind": SKIP, "charge": 0.0, "info": 0.0,
                       "hits": {"charge": [], "info": []},
                       "reason": f"خطا در بررسی شیت: {exc}"}
        result["sheets"][label] = verdict
        result[verdict["kind"]].append(label)

    result["ok"] = bool(result[CHARGE] or result[INFO])
    return result


def classify_paths(paths: list[Path]) -> list[dict]:
    return [classify_file(p) for p in paths]


def split_file(path: Path, out_dir: Path) -> dict:
    """یک فایلِ آپلودی را به دو فایلِ مجزای شارژ و اطلاعات می‌شکند.

    شیت‌های دسته‌بندی‌شده با `add_sheet` توی فایل جدید کپی می‌شوند، پس ساختار
    (هدر، فرمت‌ها) تا حد ممکن حفظ می‌شود. اگر فایل فقط یک دسته شیت داشته باشد،
    همان فایل اصلی دست‌نخورده برمی‌گردد (بدون کپی بی‌دلیل).

    خروجی: {CHARGE: Path|None, INFO: Path|None, "skipped": [نام شیت‌ها]}
    """
    info = classify_file(path)
    if not info["ok"]:
        return {CHARGE: None, INFO: None, "skipped": list(info["skip"]), "verdict": info}

    # کلیدِ «verdict» عمداً اسمِ INFO نیست: کلیدِ "info" خودش دسته‌ی اطلاعات است
    # و اگر متادیتا زیرش بنشیند، مسیریابی فایلِ اطلاعاتی را اشتباه می‌گیرد.
    out: dict = {CHARGE: None, INFO: None, "skipped": list(info["skip"]), "verdict": info}

    # فقط یک دسته → نیازی به شکستن نیست
    if not (info[CHARGE] and info[INFO]):
        single = CHARGE if info[CHARGE] else INFO
        out[single] = path
        return out

    suffix = path.suffix.lower()
    if suffix not in {".xlsx", ".xlsm"}:
        # برای CSV نمی‌شه چند شیت داشت؛ هر دسته یک CSV جدا می‌گیره
        out[CHARGE] = _write_single_csv(path, info[CHARGE], out_dir, "charge")
        out[INFO] = _write_single_csv(path, info[INFO], out_dir, "info")
        return out

    for kind, sheets in ((CHARGE, info[CHARGE]), (INFO, info[INFO])):
        if not sheets:
            continue
        target = out_dir / f"{path.stem}__{kind}{path.suffix.lower()}"
        if target.exists():
            target.unlink()
        try:
            all_rows = {name: xlsx_sheet.read_sheet(path, name) for name in sheets}
        except Exception as exc:  # noqa: BLE001
            out["verdict"]["sheets_error"] = str(exc)
            continue
        try:
            # workbook تازه: فقط شیت‌های همین دسته، بدون بقیه‌ی شیت‌های فایل اصلی
            xlsx_sheet.new_workbook(target, [(name, all_rows[name]) for name in sheets])
        except Exception as exc:  # noqa: BLE001 - برگردیم به فایل اصلی
            out["verdict"].setdefault("split_errors", []).append(f"{kind}: {exc}")
            target.unlink(missing_ok=True)
            continue
        out[kind] = target

    # اگر شکستن یک دسته شکست خورد، باید دست کم همون دسته برگرده به فایل اصلی
    for kind in (CHARGE, INFO):
        if out[kind] is None:
            other = INFO if kind == CHARGE else CHARGE
            if out[other] is not None and out[other] != path:
                # دسته‌ی دیگه جدا شده ولی این یکی نشد → کل فایل به این دسته می‌ره
                out[other].unlink(missing_ok=True)
                out[other] = None
                out[kind] = path
            elif out[other] is None:
                out[kind] = path
    return out


def _write_single_csv(path: Path, sheets: list[str], out_dir: Path, kind: str) -> Path:
    """شیت‌های یک دسته را در یک CSV (با جداکننده‌ی « » و انکد utf-8) می‌نویسد."""
    target = out_dir / f"{path.stem}__{kind}.csv"
    rows: list[str] = []
    for name in sheets:
        try:
            sheet_rows = xlsx_sheet.read_sheet(path, name)
        except Exception:  # noqa: BLE001
            continue
        if not rows:
            rows.append(f"# {name}")
        for row in sheet_rows:
            rows.append(" ".join(_clean_header(c, i) if c else "" for i, c in enumerate(row)))
    target.write_text("\n".join(rows), encoding="utf-8")
    return target


def route(paths: list[Path], work_dir: Path) -> dict:
    """لیست فایل‌های آپلودی را دسته‌بندی و (در صورت نیاز) تفکیک می‌کند.

    خروجی:
      {
        "charge": [Path, ...],   # فایل‌هایی که باید به موتور شارژ برسن
        "info":   [Path, ...],   # فایل‌هایی که باید به موتور اطلاعات برسن
        "skipped": [نام شیت‌های بی‌ربط],
        "verdicts": [گزارش هر فایل],
        "split": bool,           # آیا فایلی تفکیک شد؟
      }
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    charge_files: list[Path] = []
    info_files: list[Path] = []
    skipped: list[str] = []
    verdicts: list[dict] = []
    did_split = False

    for path in paths:
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        info = classify_file(path)
        verdicts.append(info)
        if not info["ok"]:
            continue
        result = split_file(path, work_dir)
        if isinstance(result[CHARGE], Path):
            charge_files.append(result[CHARGE])
        if isinstance(result[INFO], Path):
            info_files.append(result[INFO])
        if result[CHARGE] not in (None, path) or result[INFO] not in (None, path):
            did_split = True
        skipped.extend(f"{path.name} › {name}" for name in result["skipped"])

    # شیت‌های فایل‌های تک‌دسته هم باید گزارش بشن
    for info in verdicts:
        if not info["ok"]:
            skipped.extend(f"{info['path'].name} › {name}" for name in info["skip"])

    return {
        CHARGE: charge_files,
        INFO: info_files,
        "skipped": skipped,
        "verdicts": verdicts,
        "split": did_split,
    }


def summary(route_result: dict) -> str:
    """توضیح فارسیِ تصمیم‌های مسیریابی، برای نمایش بالای پیش‌نمایش."""
    lines: list[str] = []
    for info in route_result.get("verdicts", []):
        name = info["path"].name
        if info.get("error"):
            lines.append(f"❌ «{name}»: خوانده نشد — {info['error']}")
            continue
        parts = []
        for kind, label in ((CHARGE, "شارژ"), (INFO, "اطلاعات")):
            if info[kind]:
                parts.append(f"{label}: {'، '.join(info[kind])}")
        if info["skip"]:
            parts.append(f"نادیده: {'، '.join(info['skip'])}")
        lines.append(f"📄 «{name}» → {' | '.join(parts) or 'هیچ شیت قابل‌استفاده‌ای ندارد'}")

    if route_result.get("split"):
        lines.append("✂️ فایل چندمنظوره بود؛ شیت‌های شارژ و اطلاعات از هم جدا شدند.")
    return "\n".join(lines)
