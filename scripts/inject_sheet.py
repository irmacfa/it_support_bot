"""افزودن شیت به xlsx بدون بازنویسی بقیه‌ی اجزا (سطح zip).
بقیه‌ی فایل‌ها byte-for-byte کپی می‌شن تا داده‌های کاربر آسیب نبینه."""
import re
import shutil
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

SRC = Path("/home/ali/it_support_bot/data/info_stores.xlsx")
DST = Path("/tmp/opencode/info_with_expert.xlsx")
SHEET_NAME = "کارشناس انفورماتیک"
ROWS_JSON = Path("/tmp/opencode/expert_rows.json")

CT_SHEET = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"

import json
ROWS = [("کد فروشگاه", "کارشناس انفورماتیک")] + [
    (r[0], r[1]) for r in json.loads(ROWS_JSON.read_text(encoding="utf-8"))
]


def col_letter(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def build_sheet_xml(rows) -> bytes:
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        "<sheetData>",
    ]
    for r, (code, name) in enumerate(rows, start=1):
        cells = []
        if code is not None and str(code).strip() != "":
            if isinstance(code, str):
                cells.append(f'<c r="A{r}" t="inlineStr"><is><t xml:space="preserve">'
                             f'{escape(code)}</t></is></c>')
            else:
                cells.append(f'<c r="A{r}"><v>{int(code)}</v></c>')
        if name is not None and str(name).strip() != "":
            cells.append(f'<c r="B{r}" t="inlineStr"><is><t xml:space="preserve">'
                         f'{escape(str(name))}</t></is></c>')
        parts.append(f'<row r="{r}">' + "".join(cells) + "</row>")
    parts.append("</sheetData></worksheet>")
    return "".join(parts).encode("utf-8")


with zipfile.ZipFile(SRC) as z:
    names = z.namelist()
    data = {n: z.read(n) for n in names}
    infos = {i.filename: i for i in z.infolist()}

# --- شماره‌ی بزرگ‌ترین sheet و rId موجود
wb_xml = data["xl/workbook.xml"].decode("utf-8")
sheet_nums = [int(m) for m in re.findall(r'xl/worksheets/sheet(\d+)\.xml', "\n".join(names))]
new_sheet_num = max(sheet_nums) + 1 if sheet_nums else 1
sheet_ids = [int(m) for m in re.findall(r'<sheet\b[^>]*sheetId="(\d+)"', wb_xml)]
new_sheet_id = max(sheet_ids) + 1 if sheet_ids else 1

rels_xml = data["xl/_rels/workbook.xml.rels"].decode("utf-8")
rids = [int(m) for m in re.findall(r'Id="rId(\d+)"', rels_xml)]
new_rid = max(rids) + 1 if rids else 1

sheet_part = f"xl/worksheets/sheet{new_sheet_num}.xml"

# --- ۱) ساخت شیت جدید
new_sheet_bytes = build_sheet_xml(ROWS)

# --- ۲) ثبت در workbook.xml
assert SHEET_NAME not in wb_xml, "شیت با این نام از قبل هست"
new_sheet_tag = f'<sheet name="{escape(SHEET_NAME)}" sheetId="{new_sheet_id}" r:id="rId{new_rid}"/>'
wb_new = wb_xml.replace("</sheets>", new_sheet_tag + "</sheets>")
assert wb_new != wb_xml, "درج شیت در workbook.xml ناموفق"

# --- ۳) ثبت رابطه
new_rel = (f'<Relationship Id="rId{new_rid}" '
           f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
           f'Target="worksheets/sheet{new_sheet_num}.xml"/>')
rels_new = rels_xml.replace("</Relationships>", new_rel + "</Relationships>")
assert rels_new != rels_xml

# --- ۴) ثبت در Content_Types
ct = data["[Content_Types].xml"].decode("utf-8")
ct_new = ct.replace("</Types>", f'<Override PartName="/{sheet_part}" ContentType="{CT_SHEET}"/></Types>')
assert ct_new != ct

# --- ۵) نوشتن zip: همه‌ی اجزای اصلی عیناً + اجزای تغییریافته
if DST.exists():
    DST.unlink()
with zipfile.ZipFile(DST, "w", zipfile.ZIP_DEFLATED) as out:
    for n in names:
        info = infos[n]
        payload = data[n]
        if n == "xl/workbook.xml":
            payload = wb_new.encode("utf-8")
        elif n == "xl/_rels/workbook.xml.rels":
            payload = rels_new.encode("utf-8")
        elif n == "[Content_Types].xml":
            payload = ct_new.encode("utf-8")
        zi = zipfile.ZipInfo(n, date_time=info.date_time)
        zi.compress_type = info.compress_type
        zi.external_attr = info.external_attr
        out.writestr(zi, payload)
    zi = zipfile.ZipInfo(sheet_part, date_time=infos["xl/workbook.xml"].date_time)
    zi.compress_type = zipfile.ZIP_DEFLATED
    out.writestr(zi, new_sheet_bytes)

print(f"ساخته شد: {DST}")
print(f"  شیت جدید: {sheet_part} (sheetId={new_sheet_id}, rId=rId{new_rid})")
print(f"  سطرها: {len(ROWS)}")
