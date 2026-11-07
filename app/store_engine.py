"""
موتور مشترکِ «مجموعه‌داده‌های فروشگاه».

قبلاً `data_store` (شارژ) و `info_store` (اطلاعات) هر کدام ~۴۰۰ خط از همان
منطق را جداگانه داشتند: خواندن فایل، ساخت گزارش شیت‌ها، ذخیره/خواندن اسنپ‌شات،
تشخیص حالت ادغام، محاسبه‌ی diff، پیش‌نمایش، اعمال و کش. تنها تفاوت واقعی‌شان این
بود که «یک شیت چطور به رکورد تبدیل می‌شود» و فایل‌ها کجا نگه‌داری می‌شوند.

این ماژول آن هزار خط تکراری را یک‌جا جمع می‌کند. هر مجموعه‌داده یک `Profile`
می‌سازد که فقط همان تفاوت‌های واقعی را توصیف می‌کند، و `Engine` بقیه را انجام
می‌دهد. رفتار هر دو مجموعه‌داده دقیقاً مثل قبل می‌ماند — این یک بازآرایی است،
نه تغییر رفتار.

افزودن مجموعه‌داده‌ی سوم (مثلاً فایل انبار) یعنی یک Profile دیگر، نه یک کپی
از کل این موتور.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from app import config
from app.atomic_io import atomic_write_json

# `data_store` این موتور را در انتهای فایلش import می‌کند، پس اگر اینجا
# مستقیم از آن چیزی import کنیم دورِ حلقه‌ای می‌سازیم که به ترتیب import وابسته
# می‌شود. به‌جایش تنبلانه resolve می‌کنیم تا هر دو ترتیب کار کند.
_ds_module = None


def _ds():
    """ماژول data_store را تنبلانه برمی‌گرداند (شکستن حلقه‌ی import)."""
    global _ds_module
    if _ds_module is None:
        from app import data_store as mod
        _ds_module = mod
    return _ds_module


class Profile:
    """توصیف تفاوت‌های یک مجموعه‌داده با موتور مشترک.

    پیش‌فرض‌ها برای حالت رایج (یک ایندکس کد→رکورد، اسنپ‌شات، گزارش) کافی است و
    فقط چیزهایی را پیاده‌سازی می‌کنی که واقعاً فرق دارند.
    """

    key: str = ""
    label: str = ""
    # اسمی که در پیام‌های فارسی به این مجموعه‌داده اشاره می‌کند («شارژ»/«اطلاعات»)
    noun: str = ""
    schema_path: Path = Path()
    snapshot_path: Path = Path()
    snapshot_version: str = "1"
    # پیام‌ها
    nothing_uploaded: str = "هنوز هیچ فایلی آپلود نشده."
    nothing_found: str = "هیچ کد فروشگاهی پیدا نشد"
    replace_tail: str = "کل داده‌ی فعلی با همین فایل ساخته می‌شه"
    replace_tail_replace: str = "کل اطلاعات فعلی با همین فایل ساخته می‌شه"

    # آیا جستجوی «۰۰۱» باید کد «۱» را هم پیدا کند؟
    use_alias: bool = False

    def source_files(self) -> list[Path]:
        raise NotImplementedError

    def index_sheet(self, file_name: str, sheet_name: str, raw_df, new_index: dict, seen: set) -> dict:
        raise NotImplementedError

    def sheet_report_shell(self, file_name: str, sheet_name: str, rows: int) -> dict:
        """اسکلت گزارش یک شیت؛ هر profile فیلدهای مخصوص خودش را اضافه می‌کند."""
        return {
            "file": file_name,
            "sheet": sheet_name,
            "rows": rows,
            "indexed": 0,
            "no_code": 0,
            "unique_codes": 0,
            "columns": [],
            "code_col": None,
            "skipped_reason": None,
        }

    def totals_shell(self) -> dict:
        return {"files": 0, "sheets": 0, "rows": 0, "indexed": 0, "codes": 0, "skipped": 0}

    def totals_from(self, sheets: list[dict], file_count: int, index: dict) -> dict:
        """جمع‌بندی گزارش از روی شیت‌ها و ایندکس. هر profile فیلدهای مخصوصش را
        (مثل «کد استنتاجی») اینجا اضافه می‌کند."""
        return {
            "files": file_count,
            "sheets": len(sheets),
            "rows": sum(s.get("rows", 0) for s in sheets),
            "indexed": sum(s.get("indexed", 0) for s in sheets),
            "codes": len(index),
            "skipped": sum(1 for s in sheets if s.get("skipped_reason")),
        }


class Engine:
    """کش، اسنپ‌شات، diff، پیش‌نمایش و اعمالِ یک مجموعه‌داده."""

    def __init__(self, profile: Profile):
        self.profile = profile
        self._lock = threading.RLock()
        self._index: dict[str, list[dict]] = {}
        self._alias: dict[str, str] = {}
        self._schema: dict = {}
        self._last_error: str | None = None
        self._signature: tuple = ()

    # ------------------------------------------------------------ کمکی‌ها

    def _snapshot_version(self):
        return self.profile.snapshot_version

    @staticmethod
    def files_signature(paths: list[Path]) -> tuple:
        signature = []
        for path in paths:
            try:
                st = path.stat()
                signature.append((str(path), st.st_mtime_ns, st.st_size))
            except OSError:
                signature.append((str(path), 0, 0))
        return tuple(signature)

    def _build_alias(self, index: dict) -> dict:
        alias: dict[str, str] = {}
        for code in index:
            alias.setdefault(code.lstrip("0") or "0", code)
        return alias

    def _snapshot_paths(self) -> list[Path]:
        return self.profile.source_files()

    # ------------------------------------------------------------ ساخت ایندکس

    def build_from_paths(self, paths: list[Path]) -> tuple[dict[str, list[dict]], dict]:
        """فایل‌ها را می‌خواند و ایندکس + گزارش کامل می‌سازد (بدون تغییر کش)."""
        index: dict[str, list[dict]] = {}
        seen: set = set()
        sheets: list[dict] = []
        file_infos: list[dict] = []
        errors: list[str] = []

        for path in paths:
            file_info = {"name": path.name, "size_kb": round(path.stat().st_size / 1024, 1),
                         "sheets": 0, "error": None}
            try:
                tables = _ds().read_tables(path)
            except Exception as e:
                file_info["error"] = str(e)
                file_infos.append(file_info)
                errors.append(f"{path.name}: {e}")
                continue

            for sheet_name, df in tables:
                file_info["sheets"] += 1
                label = (sheet_name or "").strip() or sheet_name
                try:
                    sheets.append(self.profile.index_sheet(path.name, label, df, index, seen))
                except Exception as e:
                    errors.append(f"{path.name} › {sheet_name}: {e}")
                    shell = self.profile.sheet_report_shell(path.name, label, 0)
                    shell["skipped_reason"] = f"خطا در خوندن شیت: {e}"
                    sheets.append(shell)
            file_infos.append(file_info)

        # اسم شیت بین چند فایل تکرار شده → برای یکی‌کردن نمایش، اسم فایل هم اضافه می‌شود.
        # رکوردهای ایندکس هم هم‌زمان بازنامی‌گذاری می‌شوند تا گزارش و ایندکس ناهم‌خوان نشوند.
        if sheets and len({s["sheet"] for s in sheets}) < len(sheets):
            counts: dict[str, int] = {}
            for s in sheets:
                counts[s["sheet"]] = counts.get(s["sheet"], 0) + 1
            renames: dict[tuple[str, str], str] = {}
            for s in sheets:
                if counts[s["sheet"]] > 1:
                    new_label = f"{Path(s['file']).stem} › {s['sheet']}"
                    renames[(s["file"], s["sheet"])] = new_label
                    s["sheet"] = new_label
            if renames:
                for recs in index.values():
                    for r in recs:
                        new_label = renames.get((r.get("_file"), r.get("source")))
                        if new_label:
                            r["source"] = new_label

        report = {
            "dataset": self.profile.key,
            "generated_at": datetime.now().strftime("%Y/%m/%d %H:%M:%S"),
            "files": file_infos,
            "sheets": sheets,
            "totals": self.profile.totals_from(sheets, len(file_infos), index),
            "errors": errors,
        }
        return index, report

    # ------------------------------------------------------------ اسنپ‌شات

    def _write_schema(self, report: dict) -> None:
        """Persist the schema or raise; silently losing it would hide a failed apply."""
        atomic_write_json(self.profile.schema_path, report)

    def _write_snapshot(self, index: dict, report: dict) -> None:
        """Persist the applied state before publishing it to the in-memory cache."""
        payload = {
            "version": self._snapshot_version(),
            "at": datetime.now().strftime("%Y/%m/%d %H:%M:%S"),
            "files": [list(sig) for sig in self.files_signature(self.profile.source_files())],
            "index": index,
            "schema": report,
        }
        atomic_write_json(self.profile.snapshot_path, payload, indent=None)

    def _read_snapshot(self, paths: list[Path]) -> dict | None:
        if not self.profile.snapshot_path.exists():
            return None
        try:
            with open(self.profile.snapshot_path, encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            return None
        if not payload.get("index") or not payload.get("schema"):
            return None
        if payload.get("version") != self._snapshot_version():
            # پردازش عوض شده؛ اسنپ‌شات قدیمی دور ریخته و از خود فایل‌ها بازسازی می‌شود
            return None
        if payload.get("files") != [list(sig) for sig in self.files_signature(paths)]:
            return None
        return payload

    def apply_index(self, index: dict, report: dict, write_snapshot: bool = True) -> None:
        # Persist first. If either write fails, retain the previous in-memory index.
        if write_snapshot:
            self._write_snapshot(index, report)
        self._write_schema(report)
        with self._lock:
            self._index = index
            self._alias = self._build_alias(index) if self.profile.use_alias else {}
            self._schema = report
            self._last_error = report.get("errors")[0] if report.get("errors") else None
            self._signature = self.files_signature(self.profile.source_files())

    # ------------------------------------------------------------ reload

    def reload(self) -> tuple[bool, str]:
        """فایل‌های فعال را از دیسک می‌خواند و کش را به‌روز می‌کند."""
        paths = self.profile.source_files()
        if not paths:
            self._last_error = self.profile.nothing_uploaded
            return False, self._last_error

        snapshot = self._read_snapshot(paths)
        if snapshot:
            try:
                self.apply_index(snapshot["index"], snapshot["schema"], write_snapshot=False)
            except Exception:
                self._last_error = "ذخیره‌ی گزارش ناموفق بود؛ ایندکس قبلی حفظ شد."
                return False, self._last_error
            return True, self.report_summary(snapshot["schema"])

        index, report = self.build_from_paths(paths)
        if not index:
            self._last_error = self.report_summary(report)
            return False, self._last_error

        try:
            self.apply_index(index, report)
        except Exception:
            self._last_error = "ذخیره‌ی وضعیت ناموفق بود؛ ایندکس قبلی حفظ شد."
            return False, self._last_error
        return True, self.report_summary(report)

    def auto_reload(self, force: bool = False) -> None:
        """اگر فایل‌ها عوض شده باشند، خودکار ری‌لود می‌کند."""
        paths = self.profile.source_files()
        if not paths:
            return
        if force or self.files_signature(paths) != self._signature:
            self.reload()

    # ------------------------------------------------------------ diff / apply

    def _current_index(self) -> dict:
        with self._lock:
            return {code: [dict(r) for r in recs] for code, recs in self._index.items()}

    def index_codes(self) -> set[str]:
        """همه‌ی کدهای ایندکس‌شده (بدون کپی کردن رکوردها)."""
        with self._lock:
            return set(self._index)

    def all_records(self) -> dict[str, list[dict]]:
        """کپیِ ایندکس فعلی."""
        return self._current_index()

    def raw_schema(self) -> dict:
        """کپیِ گزارش فعلی، بدون ری‌لود خودکار."""
        with self._lock:
            return json.loads(json.dumps(self._schema, ensure_ascii=False)) if self._schema else {}

    def _current_schema(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._schema, ensure_ascii=False)) if self._schema else {}

    def _merge_report(self, old_schema: dict, new_report: dict, merged_index: dict) -> dict:
        """گزارش شیت‌های قبلی را با ایندکس ادغام‌شده هماهنگ می‌کند تا شیت‌هایی که
        توی فایل جدید نبودند (ولی رکوردهایشان باقی مانده) گم نشوند."""
        new_sheets = {s.get("sheet") for s in new_report.get("sheets", [])}
        present = {r.get("source") for recs in merged_index.values() for r in recs}
        sheets = list(new_report.get("sheets", []))

        per_source: dict[str, int] = {}
        for recs in merged_index.values():
            for r in recs:
                per_source[r.get("source", "")] = per_source.get(r.get("source", ""), 0) + 1

        for sheet in old_schema.get("sheets", []):
            name = sheet.get("sheet")
            if name in new_sheets or name not in present:
                continue
            kept = dict(sheet)
            kept["kept_from_previous"] = True
            kept["indexed"] = per_source.get(name, 0)
            sheets.append(kept)

        report = dict(new_report)
        report["sheets"] = sheets
        totals = self.profile.totals_shell()
        totals.update({
            "files": len(new_report.get("files", [])),
            "sheets": len(sheets),
            "rows": new_report.get("totals", {}).get("rows", 0),
            "indexed": sum(len(v) for v in merged_index.values()),
            "codes": len(merged_index),
            "skipped": sum(1 for s in sheets if s.get("skipped_reason")),
        })
        if "inferred" in new_report.get("totals", {}):
            totals["inferred"] = new_report.get("totals", {}).get("inferred", 0)
        report["totals"] = totals
        report["generated_at"] = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        return report

    def preview_upload(self, paths: list[Path], mode: str = "merge",
                       drop_missing: bool = False) -> tuple[bool, str, dict]:
        """تغییرات را گزارش می‌دهد بدون آنکه چیزی اعمال شود."""
        new_index, report = self.build_from_paths(paths)
        payload = {"dataset": self.profile.key,
                   "diff": None, "report": report, "mode": mode, "drop_missing": drop_missing,
                   "new_codes": len(new_index), "auto": None}

        if not new_index:
            return False, (f"{self.profile.nothing_found} توی فایل‌های آپلودشده؛ "
                           "فایل‌های قبلی سر جای خودشون موندن.\n"
                           + self.report_summary(report)), payload

        old_index = self._current_index()
        diff = _ds().diff_index(old_index, new_index)
        payload["diff"] = diff

        detect = _ds().detect_mode(old_index, new_index)
        payload["auto"] = detect
        effective = detect["mode"] if mode == "auto" else mode
        payload["mode"] = mode
        payload["effective_mode"] = effective

        if effective == "merge":
            tail = "با «اعمال تغییرات» رکوردهای جدید اضافه و تغییرکرده‌ها جایگزین می‌شن و بقیه دست‌نخورده می‌مونن."
        else:
            tail = (f"با «جایگزینی کامل» {self.profile.replace_tail} "
                    f"({diff['counts']['removed']} کد حذف می‌شه).")
        message = (f"📋 {len(new_index)} کد توی فایل جدید خونده شد. "
                   f"{_ds().diff_summary(diff, effective, drop_missing)}\n" + tail)
        if mode == "auto":
            message = _ds().auto_mode_message(detect) + "\n" + message
        return True, message, payload

    def commit_upload(self, paths: list[Path], mode: str = "merge",
                      drop_missing: bool = False) -> tuple[bool, str, dict]:
        """تغییرات را اعمال می‌کند (فایل‌های فیزیکی را main.py جابه‌جا می‌کند)."""
        new_index, new_report = self.build_from_paths(paths)
        if not new_index:
            return False, (f"{self.profile.nothing_found} توی فایل‌های آپلودشده؛ "
                           "داده‌ی قبلی دست‌نخورده موند.\n"
                           + self.report_summary(new_report)), {"report": new_report, "diff": None}

        old_index = self._current_index()
        old_schema = self._current_schema()
        diff = _ds().diff_index(old_index, new_index)

        if mode not in ("merge", "replace"):
            mode = _ds().detect_mode(old_index, new_index)["mode"]

        if mode == "merge" and old_index:
            index = _ds().merge_index(old_index, new_index, drop_missing=drop_missing)
            report = self._merge_report(old_schema, new_report, index)
        else:
            index = new_index
            report = new_report

        report["last_change"] = {
            "at": datetime.now().strftime("%Y/%m/%d %H:%M:%S"),
            "mode": mode,
            "drop_missing": drop_missing,
            "counts": diff["counts"],
        }
        self.apply_index(index, report)

        totals = report.get("totals", {})
        message = (self.report_summary(report) + "\n\n" + _ds().diff_summary(diff, mode, drop_missing) +
                   f"\n📦 وضعیت نهایی: {totals.get('codes', len(index))} کد فروشگاه / "
                   f"{totals.get('indexed', sum(len(v) for v in index.values()))} رکورد")
        return True, message, {"report": report, "diff": diff}

    # ------------------------------------------------------------ API عمومی

    def lookup(self, store_code: str) -> list[dict] | None:
        self.auto_reload()
        code = _ds()._normalize_code(store_code)
        if not code:
            return None
        with self._lock:
            records = None
            if self.profile.use_alias:
                canonical = self._alias.get(code.lstrip("0") or "0", code)
                records = self._index.get(canonical) or self._index.get(code)
            else:
                records = self._index.get(code)
        return [dict(r) for r in records] if records else None

    def list_all(self, extra: dict | None = None) -> list[dict]:
        self.auto_reload()
        with self._lock:
            rows = []
            for code, recs in self._index.items():
                for r in recs:
                    row = dict(extra) if extra else {}
                    row["code"] = code
                    row.update({k: v for k, v in r.items() if not k.startswith("_")})
                    rows.append(row)
            return rows

    def get_schema(self) -> dict:
        self.auto_reload()
        with self._lock:
            return (json.loads(json.dumps(self._schema, ensure_ascii=False)) if self._schema
                    else {"sheets": [], "totals": {}, "files": []})

    def get_source_columns(self, source: str) -> list[str]:
        with self._lock:
            for sheet in self._schema.get("sheets", []):
                if sheet.get("sheet") == source:
                    return list(sheet.get("columns", []))
            return []

    def get_source_files_info(self) -> list[dict]:
        with self._lock:
            return list(self._schema.get("files", []))

    def get_status(self) -> dict:
        with self._lock:
            totals = self._schema.get("totals", {})
            return {
                "dataset": self.profile.key,
                "loaded_codes": len(self._index),
                "total_records": totals.get("indexed", sum(len(v) for v in self._index.values())),
                "sheets": totals.get("sheets", 0),
                "skipped_sheets": totals.get("skipped", 0),
                "last_error": self._last_error,
                "files": [p.name for p in self.profile.source_files()],
                "generated_at": self._schema.get("generated_at"),
            }

    # ------------------------------------------------------------ گزارش

    def report_summary(self, report: dict, max_warnings: int = 6) -> str:
        """خلاصه‌ی فارسی و خوانا از گزارش، برای پنل و پیام‌رسان."""
        totals = report.get("totals", {})
        parts = [
            f"✅ {totals.get('files', 0)} فایل و {totals.get('sheets', 0)} شیت خونده شد.",
            f"📊 {totals.get('indexed', 0)} رکورد از {totals.get('rows', 0)} ردیف، "
            f"برای {totals.get('codes', 0)} کد فروشگاه.",
        ]

        for sheet in report.get("sheets", []):
            if sheet.get("skipped_reason") or not sheet.get("indexed"):
                continue
            detected = f"کد: {sheet.get('code_col') or '—'}"
            if sheet.get("mobile_col"):
                detected += f" | شماره: {sheet['mobile_col']}"
            if sheet.get("operator_col"):
                detected += f" | اپراتور: {sheet['operator_col']}"
            if sheet.get("location_col"):
                detected += f" | محل: {sheet['location_col']}"
            parts.append(f"• {sheet.get('file', '')} › {sheet['sheet']}: "
                         f"{sheet['indexed']} رکورد ({detected})")

        skipped = [s for s in report.get("sheets", []) if s.get("skipped_reason")]
        if skipped:
            parts.append(f"⚠️ {len(skipped)} شیت ایندکس نشد:")
            for sheet in skipped[:max_warnings]:
                parts.append(f"   - {sheet.get('file', '')} › {sheet['sheet']}: {sheet['skipped_reason']}")
            if len(skipped) > max_warnings:
                parts.append(f"   - و {len(skipped) - max_warnings} شیت دیگر…")

        if report.get("errors"):
            parts.append("⛔ خطاها: " + " / ".join(report["errors"][:3]))
        return "\n".join(parts)
