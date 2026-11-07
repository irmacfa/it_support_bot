"""
کارشناس منطقه: نگاشت «منطقه → کارشناس انفورماتیک» و متن اطلاع‌رسانی گروه.

این نگاشت مستقل از شیت «کارشناس انفورماتیک» توی فایل اکسل نگه‌داری می‌شه
(توی `data/region_experts.json`) چون یک تصمیم سازمانیه، نه تخصیص فروشگاهی.
خروجی توابع `render_*` متن آماده‌ی پیام گروه رو می‌سازه؛ خودِ ارسال در
`app/main.py` و با تأیید کاربر انجام می‌شه.
"""
import json
import threading
from app import config
from app.atomic_io import atomic_write_json

_lock = threading.Lock()
STORE_PATH = config.DATA_DIR / "region_experts.json"

# پیام‌ها بلندتر از ۴۰۹۶ کاراکتر پشتیبانی نمی‌شن، پس تکه‌تکه فرستاده می‌شن.
MAX_MESSAGE_CHARS = 4000

_map: dict[str, str] = {}


def _load() -> None:
    global _map
    if STORE_PATH.exists():
        try:
            with open(STORE_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _map = {str(k).strip(): str(v).strip()
                        for k, v in data.items() if str(k).strip()}
        except Exception:
            _map = {}


def _save() -> None:
    atomic_write_json(STORE_PATH, _map)


def get_all() -> dict[str, str]:
    with _lock:
        _load()
        return dict(_map)


def set_many(pairs: dict[str, str]) -> dict:
    """نگاشت منطقه→کارشناس را ذخیره می‌کند. مقدار خالی یعنی «تعیین نشده»."""
    cleaned = {str(k).strip(): str(v).strip()
               for k, v in (pairs or {}).items() if str(k).strip()}
    with _lock:
        _load()
        changed = 0
        for region, expert in cleaned.items():
            if _map.get(region, "") != expert:
                changed += 1
            _map[region] = expert
        _save()
    return {"changed": changed, "total": len(_map)}


def regions() -> list[dict]:
    """مناطق به‌همراه تعداد فروشگاه، مدیر منطقه و کارشناسِ ثبت‌شده."""
    from app import info_store as info

    saved = get_all()
    rows, _cols = info.get_directory_rows()

    counts: dict[str, int] = {}
    managers: dict[str, set[str]] = {}
    for row in rows:
        code = str(row.get(info.INFO_CODE_COL) or "").strip()
        if code and code in info.closed_codes():
            continue
        region = str(row.get(info.EXPERT_REGION_COL)
                     or row.get("منطقه فروش") or "").strip()
        if not region:
            continue
        counts[region] = counts.get(region, 0) + 1
        manager = str(row.get("مدیر منطقه") or "").strip()
        if manager:
            managers.setdefault(region, set()).add(manager)

    out = [{"region": r, "count": counts[r],
            "manager": "، ".join(sorted(managers.get(r, ()))),
            "expert": saved.get(r, "")}
           for r in sorted(counts)]
    out.sort(key=lambda x: (not x["expert"], -x["count"], x["region"]))
    return out


def unassigned_stores() -> list[dict]:
    """فروشگاه‌های بازی که هنوز کارشناس ندارند."""
    from app import info_store as info

    groups = info.get_expert_groups()
    unset = next((g for g in groups if g["expert"] == info.EXPERT_UNSET), None)
    stores = list(unset["stores"]) if unset else []
    stores.sort(key=lambda s: (not s["region"], s["region"], s["code"]))
    return stores


def _split(text: str) -> list[str]:
    """متن بلند را بدون شکستن خط، تکه‌تکه می‌کند."""
    chunks, current = [], ""
    for line in text.splitlines():
        if current and len(current) + len(line) + 1 > MAX_MESSAGE_CHARS:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def render_region_experts(group_title: str = "انفورماتیک اوشانک") -> str:
    rows = regions()
    lines = [f"📋 *کارشناس انفورماتیک مناطق — {group_title}*", ""]
    assigned = [r for r in rows if r["expert"]]
    missing = [r for r in rows if not r["expert"]]
    for r in assigned:
        lines.append(f"🗺 {r['region']}: {r['expert']}"
                     + (f"  ({r['count']} فروشگاه)" if r["count"] else ""))
    if missing:
        lines.append("")
        lines.append("⏳ هنوز تعیین نشده:")
        for r in missing:
            lines.append(f"• {r['region']} ({r['count']} فروشگاه)")
    lines.append("")
    lines.append(f"جمع: {len(assigned)} منطقه تعیین‌شده از {len(rows)} منطقه"
                 if missing else f"جمع: هر {len(rows)} منطقه تعیین شده است.")
    return "\n".join(lines)


def render_unassigned(group_title: str = "انفورماتیک اوشانک") -> str:
    stores = unassigned_stores()
    if not stores:
        return "✅ همه‌ی فروشگاه‌های فعال کارشناس انفورماتیک دارند. وضعیت کامل است."
    lines = [f"⚠️ *فروشگاه‌های بدون کارشناس انفورماتیک — {group_title}*", "",
             f"تعداد: {len(stores)} فروشگاه", ""]
    by_region: dict[str, list[dict]] = {}
    for s in stores:
        by_region.setdefault(s["region"] or "بدون منطقه", []).append(s)
    for region in sorted(by_region):
        lines.append(f"🗺 {region}")
        for s in by_region[region]:
            lines.append(f"   • {s['code']} — {s['name']}")
        lines.append("")
    lines.append("لطفاً برای این فروشگاه‌ها کارشناس تعیین شود. 🙏")
    return "\n".join(lines).rstrip()


def render(kind: str, group_title: str = "انفورماتیک اوشانک") -> str:
    if kind == "regions":
        return render_region_experts(group_title)
    if kind == "unassigned":
        return render_unassigned(group_title)
    raise ValueError("نوع اطلاع‌رسانی نامعتبره")


_load()
