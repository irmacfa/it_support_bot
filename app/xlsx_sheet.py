"""افزودن شیت به فایل xlsx در سطح zip، بدون بازنویسی بقیه‌ی اجزای فایل.

چرا؟ `openpyxl` روی فایل‌های این پروژه بخشی از رشته‌ها را پاک می‌کند
(روی info_stores.xlsx حدود ۱۲٬۸۰۰ سلول از بین رفت). اینجا هر جزء غیر از
سه جزءی که واقعاً باید عوض بشه، byte-for-byte از فایل مبدأ کپی می‌شه.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape, unescape

import pandas as pd

CT_SHEET = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
REL_WORKSHEET = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"

# اگه مقداری شبیه «۱۱۶۵.۰» بود (سلول اعشاری ذخیره شده) به «۱۱۶۵» تبدیل می‌شه
_TRAILING_ZERO = re.compile(r"^(\d+)\.0$")


def trim_float_noise(text) -> str:
    """اعشار اضافه‌ی اکسل/پانداس رو از یه مقدار متنیِ عددی حذف می‌کنه.

    اکسل گاهی 35.688917 رو به شکل 35.688917000000004 و گاهی 35.66225 رو به شکل
    35.662250 ذخیره می‌کنه. اگه این ناهمگنی دست‌نخورده بمونه، هر بار که فایل از
    اکسل دوباره آپلود بشه کل مختصات‌ها «تغییرکرده» گزارش می‌شن. فقط مقادیری که
    واقعاً عدد اعشاری‌ان کوتاه می‌شن تا شماره‌هایی مثل 09123456789 یا کدهایی مثل
    0351234567 دست‌نخورده بمونن.
    """
    candidate = str(text).strip()
    if "." not in candidate:
        return str(text)
    head, _, tail = candidate.lstrip("-").partition(".")
    if not head.isdigit() or not tail.isdigit():
        return str(text)
    try:
        number = float(candidate)
    except ValueError:
        return str(text)
    if number == int(number) and abs(number) < 1e15:
        return str(int(number))
    short = f"{number:.15g}"
    if "e" in short or "E" in short:
        return str(text)
    return short


def _clean(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)) or value is pd.NaT:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value)
    m = _TRAILING_ZERO.match(text)
    if m:
        return m.group(1)
    return trim_float_noise(text)


def sheet_names(path: Path) -> list[str]:
    """اسم شیت‌های یک xlsx، به همان ترتیبی که توی فایل هست."""
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("xl/workbook.xml").decode("utf-8", "replace")
    except (OSError, KeyError, zipfile.BadZipFile):
        return []
    return re.findall(r'<sheet\b[^>]*\bname="([^"]*)"', xml)


def read_sheet(path: Path, sheet: str) -> list[list[str]]:
    """خواندن محتوای یک شیت به‌صورت رشته (همه‌ی سطرها و ستون‌ها)."""
    try:
        df = pd.read_excel(path, sheet_name=sheet, header=None, dtype=str)
    except Exception:
        return []
    rows = [[_clean(v) for v in row] for row in df.values.tolist()]
    width = max((len(r) for r in rows), default=0)
    return [r + [""] * (width - len(r)) for r in rows]


def _col_letter(n: int) -> str:
    letters = ""
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _build_sheet_xml(rows: list[list[str]]) -> bytes:
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        "<sheetData>",
    ]
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, value in enumerate(row, start=1):
            if value == "":
                continue
            ref = f"{_col_letter(c)}{r}"
            cells.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">'
                         f"{escape(value)}</t></is></c>")
        parts.append(f'<row r="{r}">' + "".join(cells) + "</row>")
    parts.append("</sheetData></worksheet>")
    return "".join(parts).encode("utf-8")


def _sheet_part_by_name(payload: dict[str, bytes], wb_xml: str, sheet_name: str) -> str | None:
    """مسیر داخلیِ part یک شیت، از روی اسمش توی workbook.xml."""
    target_name = unescape(sheet_name)
    for element in re.findall(r"<sheet\b[^>]*/?>", wb_xml):
        name_m = re.search(r'\bname="([^"]*)"', element)
        if not name_m or unescape(name_m.group(1)) != target_name:
            continue
        rid_m = re.search(r'r:id="([^"]*)"', element)
        if not rid_m:
            return None
        rels_xml = payload["xl/_rels/workbook.xml.rels"].decode("utf-8")
        for rel in re.findall(r"<Relationship\b[^>]*/?>", rels_xml):
            if re.search(rf'Id="{re.escape(rid_m.group(1))}"', rel):
                tgt = re.search(r'Target="([^"]*)"', rel)
                if not tgt:
                    return None
                target = unescape(tgt.group(1)).lstrip("/")
                return target if target.startswith("xl/") else f"xl/{target}"
    return None


def replace_sheet(path: Path, sheet_name: str, rows: list[list[str]]) -> Path:
    """محتوای یه شیت موجود رو عوض می‌کنه، بقیه‌ی اجزای فایل دست‌نخورده می‌مونن.

    برای ویرایش‌های گروهی لازمه چون add_sheet اسم تکراری می‌ساخت و فایل رو خراب
    می‌کرد. اینجا فقط همون یک part بازنویسی می‌شه؛ workbook.xml، rels و
    Content_Types اصلاً دست نمی‌خورن چون اسم و ارتباط شیت عوض نمی‌شه.
    """
    path = Path(path)
    if not rows:
        raise ValueError("شیت خالیه و نوشته نمی‌شه")
    if not str(path).lower().endswith(".xlsx"):
        raise ValueError("فقط xlsx پشتیبانی می‌شه")

    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        payload = {n: z.read(n) for n in names}
        infos = {i.filename: i for i in z.infolist()}

    part = _sheet_part_by_name(payload, payload["xl/workbook.xml"].decode("utf-8"), sheet_name)
    if not part or part not in payload:
        raise ValueError(f"شیت «{sheet_name}» توی فایل پیدا نشد")

    body = _build_sheet_xml(rows)
    tmp = path.with_name(path.stem + ".__tmp__" + path.suffix)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
        for n in names:
            zi = zipfile.ZipInfo(n, date_time=infos[n].date_time)
            zi.compress_type = infos[n].compress_type
            zi.external_attr = infos[n].external_attr
            out.writestr(zi, body if n == part else payload[n])
    tmp.replace(path)
    return path


_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    f'<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument'
    f'.spreadsheetml.sheet.main+xml"/>'
    + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="{CT_SHEET}"/>'
              for i in range(1, 102))
    + "</Types>"
)

_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006'
    f'/relationships/officeDocument" Target="xl/workbook.xml"/>'
    "</Relationships>"
)

_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
    '<borders count="1"><border/></borders>'
    '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
    '<cellXfs count="1"><xf xfId="0"/></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)


def new_workbook(dst: Path, sheets: list[tuple[str, list[list[str]]]]) -> Path:
    """یک فایل xlsx کاملاً تازه می‌سازد که فقط شیت‌های داده‌شده را دارد.

    برای تفکیک فایل چندمنظوره لازم است: `add_sheet` شیت را به فایل *مبدأ* اضافه
    می‌کند، پس برای ساختن فایلی که فقط شیت‌های شارژ (یا فقط اطلاعات) را داشته
    باشد باید workbook تازه ساخته شود، نه کپیِ فایل اصلی.
    """
    dst = Path(dst)
    wanted = [(name, rows) for name, rows in sheets
              if rows and any(any(str(c).strip() for c in row) for row in rows)]
    if not wanted:
        raise ValueError("هیچ شیتِ پری برای ساخت فایل نیست")

    wb_sheets, rels = [], []
    for i, (name, _rows) in enumerate(wanted, start=1):
        wb_sheets.append(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>')
        rels.append(f'<Relationship Id="rId{i}" Type="{REL_WORKSHEET}" '
                    f'Target="worksheets/sheet{i}.xml"/>')
    styles_rid = len(wanted) + 1
    rels.append(f'<Relationship Id="rId{styles_rid}" Type="http://schemas.openxmlformats.org'
                f'/officeDocument/2006/relationships/styles" Target="styles.xml"/>')

    payload: dict[str, bytes] = {
        "[Content_Types].xml": _CONTENT_TYPES.encode("utf-8"),
        "_rels/.rels": _ROOT_RELS.encode("utf-8"),
        "xl/styles.xml": _STYLES.encode("utf-8"),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets>{"".join(wb_sheets)}</sheets></workbook>').encode("utf-8"),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'{"".join(rels)}</Relationships>').encode("utf-8"),
    }
    for i, (_name, rows) in enumerate(wanted, start=1):
        payload[f"xl/worksheets/sheet{i}.xml"] = _build_sheet_xml(
            [[("" if c is None else str(c)) for c in row] for row in rows])

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.stem + ".__new__" + dst.suffix)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
        for name, body in payload.items():
            out.writestr(name, body)
    tmp.replace(dst)
    return dst


def add_sheet(src: Path, sheet_name: str, rows: list[list[str]], dst: Path | None = None) -> Path:
    """یه شیت به فایل xlsx اضافه می‌کنه. بقیه‌ی اجزای فایل دست‌نخورده می‌مونن."""
    src, dst = Path(src), Path(dst or src)
    if not rows or not any(any(c for c in row) for row in rows):
        raise ValueError("شیت خالیه و اضافه نمی‌شه")
    if not str(src).lower().endswith(".xlsx"):
        raise ValueError("فقط xlsx پشتیبانی می‌شه")

    with zipfile.ZipFile(src) as z:
        names = z.namelist()
        payload = {n: z.read(n) for n in names}
        infos = {i.filename: i for i in z.infolist()}

    wb_xml = payload["xl/workbook.xml"].decode("utf-8")
    sheet_nums = [int(m) for m in re.findall(r"xl/worksheets/sheet(\d+)\.xml", "\n".join(names))]
    new_num = max(sheet_nums) + 1 if sheet_nums else 1
    ids = [int(m) for m in re.findall(r'<sheet\b[^>]*sheetId="(\d+)"', wb_xml)]
    new_id = max(ids) + 1 if ids else 1

    rels_xml = payload["xl/_rels/workbook.xml.rels"].decode("utf-8")
    rids = [int(m) for m in re.findall(r'Id="rId(\d+)"', rels_xml)]
    new_rid = max(rids) + 1 if rids else 1

    part = f"xl/worksheets/sheet{new_num}.xml"
    tag = f'<sheet name="{escape(sheet_name)}" sheetId="{new_id}" r:id="rId{new_rid}"/>'
    wb_new = wb_xml.replace("</sheets>", tag + "</sheets>")
    if wb_new == wb_xml:
        raise ValueError("درج شیت در workbook.xml ناموفق بود")
    rel = (f'<Relationship Id="rId{new_rid}" Type="{REL_WORKSHEET}" '
           f'Target="worksheets/sheet{new_num}.xml"/>')
    rels_new = rels_xml.replace("</Relationships>", rel + "</Relationships>")
    if rels_new == rels_xml:
        raise ValueError("درج رابطه ناموفق بود")
    ct = payload["[Content_Types].xml"].decode("utf-8")
    ct_new = ct.replace("</Types>",
                        f'<Override PartName="/{part}" ContentType="{CT_SHEET}"/></Types>')
    if ct_new == ct:
        raise ValueError("درج Content-Type ناموفق بود")

    tmp = dst.with_name(dst.stem + ".__tmp__" + dst.suffix)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
        for n in names:
            info = infos[n]
            body = payload[n]
            if n == "xl/workbook.xml":
                body = wb_new.encode("utf-8")
            elif n == "xl/_rels/workbook.xml.rels":
                body = rels_new.encode("utf-8")
            elif n == "[Content_Types].xml":
                body = ct_new.encode("utf-8")
            zi = zipfile.ZipInfo(n, date_time=info.date_time)
            zi.compress_type = info.compress_type
            zi.external_attr = info.external_attr
            out.writestr(zi, body)
        zi = zipfile.ZipInfo(part, date_time=infos["xl/workbook.xml"].date_time)
        zi.compress_type = zipfile.ZIP_DEFLATED
        out.writestr(zi, _build_sheet_xml(rows))
    tmp.replace(dst)
    return dst
