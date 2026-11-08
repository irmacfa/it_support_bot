"""
اپ FastAPI: پنل مدیریت (آپلود اکسل، کاربران، مسئولین شارژ).
اجرا: uvicorn app.main:app --host 0.0.0.0 --port 8001
احراز هویت: یه بار رمز عبور وارد میشه، بعدش با کوکی سشن همه‌ی بخش‌ها در دسترسن.
"""
import json
import os
import logging
import re
import secrets
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import jdatetime

from fastapi import FastAPI, Request, UploadFile, File, Form, Body
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from app import config, data_store, info_store, registration, charge_admins, reset_admins, groups, group_members, bale_bot, schedule_store, schedule_times, usage_stats
from app.atomic_io import atomic_exchange_paths
from app import xlsx_sheet, region_experts, dataset_router, presence, separated_experts

app_logger = logging.getLogger("it_support_panel")

if not config.ADMIN_PASSWORD or config.ADMIN_PASSWORD.strip() in {"changeme", "change_this_password"}:
    raise RuntimeError("ADMIN_PASSWORD must be configured in the .env file before starting the admin panel.")

app = FastAPI(
    title="IT Support Lookup Bot - Admin Panel",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
# صفحه‌ی /data چند مگابایت HTML برمی‌گردونه؛ بدون فشرده‌سازی، بارگذاریش کند می‌شه.
app.add_middleware(GZipMiddleware, minimum_size=1024)
templates = Jinja2Templates(directory=str(config.BASE_DIR / "templates"))

# فایل‌های استاتیک (فونت وزیرمتن، آیکون‌ها) — self-host شده تا به CDN خارجی نیاز نباشد.
STATIC_DIR = config.BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

SESSION_COOKIE = "panel_session"
SESSION_TTL = timedelta(hours=12)
_sessions: dict[str, datetime] = {}

# گروهی که اطلاع‌رسانی کارشناسان به آن ارسال می‌شود.
ANNOUNCE_GROUP_TITLE = "انفورماتیک اوشانک"


def _create_session() -> str:
    token = secrets.token_hex(24)
    _sessions[token] = datetime.now() + SESSION_TTL
    return token


def _is_authed(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE)
    if not token or token not in _sessions:
        return False
    if datetime.now() > _sessions[token]:
        del _sessions[token]
        return False
    return True


# محدودسازی تلاش‌های ناموفق ورود؛ پنل با یک Worker اجرا می‌شود.
LOGIN_RATE_LIMIT = 5
LOGIN_RATE_WINDOW_SECONDS = 15 * 60
_login_failures: dict[str, list[float]] = {}
_login_rate_lock = threading.Lock()


def _login_client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _login_retry_after(client_key: str) -> int:
    now = time.monotonic()
    with _login_rate_lock:
        attempts = [t for t in _login_failures.get(client_key, [])
                    if now - t < LOGIN_RATE_WINDOW_SECONDS]
        if attempts:
            _login_failures[client_key] = attempts
        else:
            _login_failures.pop(client_key, None)
        if len(attempts) < LOGIN_RATE_LIMIT:
            return 0
        return max(1, int(LOGIN_RATE_WINDOW_SECONDS - (now - attempts[0])))


def _record_login_failure(client_key: str) -> None:
    now = time.monotonic()
    with _login_rate_lock:
        # پاک‌سازی کلیدهای منقضی برای جلوگیری از رشد بی‌نهایت حافظه.
        for key, values in list(_login_failures.items()):
            active = [t for t in values if now - t < LOGIN_RATE_WINDOW_SECONDS]
            if active:
                _login_failures[key] = active
            else:
                _login_failures.pop(key, None)
        _login_failures.setdefault(client_key, []).append(now)


def _clear_login_failures(client_key: str) -> None:
    with _login_rate_lock:
        _login_failures.pop(client_key, None)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    """محافظت مرکزی API و بررسی مبدأ درخواست‌های تغییردهنده."""
    if request.url.path.startswith("/api/") and not _is_authed(request):
        return JSONResponse(
            {"ok": False, "error": "احراز هویت لازم است."},
            status_code=401,
        )

    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        source = origin or referer
        host = request.headers.get("host", "").casefold()
        try:
            parsed = urlsplit(source) if source else None
        except ValueError:
            parsed = None
        if (not parsed or parsed.scheme != request.url.scheme
                or parsed.netloc.casefold() != host):
            return JSONResponse(
                {"ok": False, "error": "مبدأ درخواست معتبر نیست."},
                status_code=403,
            )

    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if _is_authed(request):
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "message": None})


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, password: str = Form(...)):
    client_key = _login_client_key(request)
    retry_after = _login_retry_after(client_key)
    if retry_after:
        response = templates.TemplateResponse(
            "login.html",
            {"request": request, "message": "تعداد تلاش‌های ورود زیاد است؛ کمی بعد دوباره امتحان کنید."},
            status_code=429,
        )
        response.headers["Retry-After"] = str(retry_after)
        return response

    if password != config.ADMIN_PASSWORD:
        _record_login_failure(client_key)
        return templates.TemplateResponse(
            "login.html", {"request": request, "message": "رمز عبور اشتباهه."}
        )

    _clear_login_failures(client_key)
    token = _create_session()
    resp = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax",
        max_age=int(SESSION_TTL.total_seconds()),
    )
    return resp


@app.post("/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        _sessions.pop(token, None)
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    """صفحه‌ی پیش‌فرض پنل: داشبورد."""
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/dashboard", status_code=302)


@app.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request):
    """صفحه‌ی آپلود فایل شارژ و اطلاعات."""
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("upload.html", _upload_ctx(request))


# ---------------------------------------------------------------- مجموعه‌داده‌ها
# هر دو جریان «شارژ» و «اطلاعات» از یک مسیر مشترک رد می‌شن: یک صفحه، یک پیش‌نمایش،
# یک دکمه‌ی اعمال. تفاوت‌ها فقط داخل هر مجموعه‌داده تعریف شده‌اند.
DATASET_ORDER = ("charge", "info")

DATASET_META = {
    "charge": {
        "key": "charge", "label": "شارژ", "emoji": "⚡", "field_type": "stores",
        "desc": "شماره‌ها، وضعیت و اطلاعات شارژ فروشگاه‌ها",
        "example": "Modem-Lte.xlsx",
    },
    "info": {
        "key": "info", "label": "اطلاعات", "emoji": "🏪", "field_type": "info",
        "desc": "مشخصات، آدرس، منطقه و کارشناس فروشگاه‌ها",
        "example": "Info.xlsx",
    },
}


def _engine_for(key: str):
    return data_store if key == "charge" else info_store


def _active_name(path: Path) -> str:
    """پسوندِ ``__charge``/``__info`` که تفکیک خودکار اضافه کرده را حذف می‌کند."""
    for tag in ("__charge", "__info"):
        if path.stem.endswith(tag):
            return path.stem[: -len(tag)] + path.suffix.lower()
    return path.name


def _active_info_path() -> Path:
    """مسیر فایل فعال اطلاعات، با پسوند واقعی."""
    if config.INFO_EXCEL_PATH.exists():
        return config.INFO_EXCEL_PATH
    for suffix in (".csv", ".xls", ".xlsx"):
        alt = config.INFO_EXCEL_PATH.with_suffix(suffix)
        if alt.exists():
            return alt
    return config.INFO_EXCEL_PATH


def _dataset_card(key: str) -> dict:
    """کارت وضعیت/گزارش/پشتیبانِ یک مجموعه‌داده."""
    meta = DATASET_META[key]
    mod = _engine_for(key)
    status = mod.get_status()
    return {
        **meta,
        "status": status,
        "loaded": status.get("loaded_stores") or status.get("loaded_info_stores") or 0,
        "total_records": status.get("total_records", 0),
        "files": status.get("files", []),
        "report": mod.get_schema(),
        "backups": _list_backups() if key == "charge" else _list_info_backups(),
        "route": None,
        "preview": None,
        "applied_diff": None,
    }


def _dataset_cards(overrides: dict | None = None) -> list[dict]:
    cards = {key: _dataset_card(key) for key in DATASET_ORDER}
    for key, patch in (overrides or {}).items():
        if key in cards and patch:
            cards[key] = {**cards[key], **patch}
    return [cards[key] for key in DATASET_ORDER]


def _upload_ctx(request: Request, flash: dict | None = None, cards: dict | None = None, **extra) -> dict:
    """کانتکست مشترک صفحه‌ی آپلود (هر دو مجموعه‌داده با هم)."""
    ctx = {
        "request": request,
        "flash": flash or {"message": None, "success": None},
        "schedule_message": None,
        "schedule_success": None,
        "token": None,
        "route": None,
        "datasets": _dataset_cards(cards),
        "loaded_days": schedule_store.get_status()["loaded_days"],
        "monitor_kpis": _build_monitor_kpis(),
        "expert_unset_label": info_store.EXPERT_UNSET,
        "active": "upload",
    }
    ctx.update(extra)
    return ctx


def _route_cards(route_result: dict) -> dict[str, dict]:
    """خلاصه‌ی مسیریابی به تفکیک هر مجموعه‌داده، برای نمایش داخل کارت‌ها."""
    cards = {key: {"files": [], "skipped": list(route_result.get("skipped", []))}
             for key in ("charge", "info")}
    for info in route_result.get("verdicts", []):
        name = info["path"].name
        for key in ("charge", "info"):
            cards[key]["files"].extend(f"{name} › {sheet}" for sheet in info.get(key, []))
    return cards


def _safe_filename(name: str) -> str:
    """اسم فایل آپلودی رو پاک‌سازی می‌کنه (جلوگیری از path traversal)."""
    name = Path(name or "file").name
    name = re.sub(r"[^\w.\-؀-ۿ ]+", "_", name, flags=re.UNICODE).strip()
    return name or "file"


def _list_backups() -> list[dict]:
    """نسخه‌های پشتیبان فایل‌های شارژ (جدیدترین اول)."""
    root = config.STORE_BACKUP_DIR
    if not root.exists():
        return []
    items = []
    for folder in sorted(root.iterdir(), reverse=True):
        if not folder.is_dir():
            continue
        files = [f for f in folder.iterdir() if f.is_file()]
        if not files:
            continue
        items.append({
            "name": folder.name,
            "files": [f.name for f in files],
            "size_kb": round(sum(f.stat().st_size for f in files) / 1024, 1),
        })
    return items


def _make_backup(protect: str = "") -> str | None:
    """از فایل‌های فعلی یه نسخه‌ی پشتیبان می‌سازه (آخرین ۳ نسخه نگه داشته میشه).

    ‌`protect` اسم نسخه‌ایه که داره بازگردانی می‌شه؛ اون هیچ‌وقت پاک نمی‌شه
    (وگرنه وسط همین درخواست پاک می‌شد و بازگردانی با خطای ۵۰۰ می‌شکست).
    """
    files = data_store.source_files()
    if not files:
        return None
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    target = config.STORE_BACKUP_DIR / stamp
    try:
        target.mkdir(parents=True, exist_ok=True)
        for path in files:
            shutil.copy2(path, target / path.name)
    except Exception as exc:
        shutil.rmtree(target, ignore_errors=True)
        app_logger.exception("ساخت نسخه‌ی پشتیبان فایل‌های شارژ ناموفق بود.")
        raise RuntimeError("ساخت نسخه‌ی پشتیبان فایل‌های شارژ ناموفق بود؛ هیچ فایلی تغییر نکرد.") from exc

    backups = sorted((d for d in config.STORE_BACKUP_DIR.iterdir() if d.is_dir()), reverse=True)
    for index, old in enumerate(backups):
        if old.name == protect or index < 3:
            continue
        shutil.rmtree(old, ignore_errors=True)
    return stamp


def _commit_store_files(staged: list[Path]) -> Path | None:
    """Build a complete replacement directory, then swap it into place."""
    destination = config.STORE_FILES_DIR
    destination.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(8)
    incoming = destination.with_name(f".{destination.name}.incoming-{token}")
    previous = destination.with_name(f".{destination.name}.previous-{token}")
    incoming.mkdir()
    try:
        # Preserve existing subdirectories, matching the previous behavior.
        if destination.is_dir():
            for child in destination.iterdir():
                if child.is_dir():
                    shutil.copytree(child, incoming / child.name)
        for path in staged:
            shutil.move(str(path), str(incoming / _active_name(path)))
        if destination.exists():
            if atomic_exchange_paths(incoming, destination):
                # The new directory is active and the old one now lives at incoming.
                previous = incoming
            else:
                os.replace(destination, previous)
                try:
                    os.replace(incoming, destination)
                except Exception:
                    if previous.exists() and not destination.exists():
                        os.replace(previous, destination)
                    raise
        else:
            os.replace(incoming, destination)
        return previous if previous.exists() else None
    except Exception:
        shutil.rmtree(incoming, ignore_errors=True)
        raise


def _finish_store_commit(previous: Path | None, success: bool) -> None:
    """Discard the old directory only after the index and snapshots are committed."""
    destination = config.STORE_FILES_DIR
    if success:
        if previous:
            if previous.is_dir():
                shutil.rmtree(previous, ignore_errors=True)
            else:
                previous.unlink(missing_ok=True)
        return
    if destination.exists():
        if destination.is_dir():
            shutil.rmtree(destination, ignore_errors=True)
        else:
            destination.unlink(missing_ok=True)
    if previous and previous.exists():
        os.replace(previous, destination)


def _clean_staging(max_age_hours: int = 6) -> None:
    """پوشه‌های آپلودِ تأییدنشده‌ی قدیمی رو پاک می‌کنه."""
    root = config.UPLOAD_STAGING_DIR
    if not root.exists():
        return
    limit = time.time() - max_age_hours * 3600
    for folder in root.iterdir():
        if folder.is_dir() and folder.stat().st_mtime < limit:
            shutil.rmtree(folder, ignore_errors=True)


MAX_UPLOAD_FILE_BYTES = 100 * 1024 * 1024
MAX_UPLOAD_TOTAL_BYTES = 200 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024


async def _stage_uploads(files: list[UploadFile], mode: str, drop_missing: bool) -> Path:
    """فایل‌ها را با سقف حجم و به‌صورت chunk در پوشه‌ی موقت ذخیره می‌کند."""
    _clean_staging()
    config.UPLOAD_STAGING_DIR.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(12)
    folder = config.UPLOAD_STAGING_DIR / token
    folder.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    try:
        for i, upload in enumerate(files):
            name = _safe_filename(upload.filename)
            if Path(name).suffix.lower() not in data_store.SUPPORTED_SUFFIXES:
                continue
            sub_dir = folder / str(i)
            sub_dir.mkdir(parents=True, exist_ok=True)
            file_bytes = 0
            with open(sub_dir / name, "wb") as target:
                while True:
                    chunk = await upload.read(UPLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    file_bytes += len(chunk)
                    total_bytes += len(chunk)
                    if file_bytes > MAX_UPLOAD_FILE_BYTES:
                        raise ValueError(f"حجم فایل «{name}» از سقف ۱۰۰ مگابایت بیشتر است.")
                    if total_bytes > MAX_UPLOAD_TOTAL_BYTES:
                        raise ValueError("حجم مجموع فایل‌های این درخواست از سقف ۲۰۰ مگابایت بیشتر است.")
                    target.write(chunk)
            await upload.close()
        (folder / "_upload.json").write_text(
            json.dumps({"mode": mode, "drop_missing": drop_missing,
                        "at": datetime.now().isoformat(timespec="seconds")},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        return folder
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        for upload in files:
            try:
                await upload.close()
            except Exception:
                pass
        raise


def _staged_paths(folder: Path) -> list[Path]:
    """فایل‌های آپلودیِ داخل پوشه‌ی موقت (فایل‌های داخلی مثل _upload.json کنار گذاشته می‌شن)."""
    if not folder.is_dir():
        return []
    return [p for p in sorted(folder.rglob("*")) if p.is_file() and not p.name.startswith("_")]


# شیت‌هایی که هرگز نباید از فایل قبلی به فایل جدید منتقل بشن
NEVER_CARRY_SHEETS = set()


def carry_over_info_sheets(path: Path) -> list[str]:
    """شیت‌هایی که توی فایل فعلی هست ولی توی فایل آپلودی نیست رو نگه می‌داره.

    وقتی چارت سازمانی جدید آپلود می‌شه، فایل کامل جایگزین می‌شه؛ اگه این
    کار انجام نشه، شیت‌هایی مثل «کارشناس انفورماتیک» یا «فروشگاههای بسته شده»
    که فقط توی فایل ما بودن از بین می‌رن. برای همین قبل از اعمال (و همون اول
    پیش‌نمایش) شیت‌های جاافتاده از فایل فعلی به فایل جدید منتقل می‌شن.
    """
    current = config.INFO_EXCEL_PATH
    if not current.exists():
        return []
    if current.resolve() == Path(path).resolve():
        return []
    if not str(path).lower().endswith(".xlsx"):
        # انتقال شیت فقط با دستکاری مستقیم فایل xlsx ممکنه؛ برای csv شیت‌های
        # فعلی از بین می‌رن، پس صریح هشدار می‌دیم.
        app_logger.warning("فایل اطلاعات csv است؛ شیت‌های فعلی قابل انتقال نیستند: %s", path)
        return []

    current_sheets = xlsx_sheet.sheet_names(current)
    have = set(xlsx_sheet.sheet_names(path))

    # فایل آپلودی باید حداقل یه شیت مشترک با فایل فعلی داشته باشه. وگرنه فایل
    # ربطی به اکسل اطلاعات نیست (مثلاً فایل دیگه‌ای که اشتباهی انتخاب شده) و
    # نباید شیت‌های فعلی روش کپی بشن؛ همون بهتره بررسی‌های معمول ردش کنن.
    if not (have & set(current_sheets)):
        app_logger.warning(
            "انتقال شیت انجام نشد: فایل آپلودی (%s) هیچ شیت مشترکی با فایل فعلی ندارد.",
            ", ".join(sorted(have)) or "بدون شیت")
        return []

    carried: list[str] = []
    for sheet in current_sheets:
        if sheet in have or sheet in NEVER_CARRY_SHEETS:
            continue
        rows = xlsx_sheet.read_sheet(current, sheet)
        if not rows or not any(any(c for c in row) for row in rows):
            continue
        try:
            xlsx_sheet.add_sheet(path, sheet, rows)
        except Exception as exc:  # noqa: BLE001 - نباید آپلود رو از کار بندازه
            app_logger.warning("انتقال شیت «%s» ناموفق بود: %s", sheet, exc)
            continue
        carried.append(sheet)
    return carried


def carry_over_charge_sheets(paths: list[Path]) -> list[str]:
    """شیت‌های شارژیِ جاافتاده را از فایل‌های فعال به فایل جدید منتقل می‌کند.

    با تفکیک خودکارِ فایل چندمنظوره، فایلِ شارژ ممکن است فقط بخشی از شیت‌ها را
    داشته باشد. اگر همان را جایگزین کنیم، بقیه‌ی داده‌ی شارژ پاک می‌شود؛ برای همین
    قبل از اعمال (و همون اولِ پیش‌نمایش) شیت‌های نبوده از فایل فعال منتقل می‌شن.
    """
    if not paths:
        return []
    base = next((p for p in paths if p.suffix.lower() in (".xlsx", ".xlsm")), None)
    if base is None:
        app_logger.warning("فایل شارژ csv است؛ شیت‌های فعلی قابل انتقال نیستند.")
        return []

    have = set()
    for path in paths:
        if path.suffix.lower() in (".xlsx", ".xlsm"):
            have |= set(xlsx_sheet.sheet_names(path))

    carried: list[str] = []
    for current in data_store.source_files():
        if current.resolve() == base.resolve():
            continue
        for sheet in xlsx_sheet.sheet_names(current):
            if sheet in have:
                continue
            rows = xlsx_sheet.read_sheet(current, sheet)
            if not rows or not any(any(c for c in row) for row in rows):
                continue
            try:
                xlsx_sheet.add_sheet(base, sheet, rows)
            except Exception as exc:  # noqa: BLE001 - نباید آپلود رو از کار بندازه
                app_logger.warning("انتقال شیت شارژ «%s» ناموفق بود: %s", sheet, exc)
                continue
            have.add(sheet)
            carried.append(sheet)
    return carried


def _list_info_backups() -> list[dict]:
    """نسخه‌های پشتیبان فایل اطلاعات فروشگاه‌ها (جدیدترین اول)."""
    root = config.INFO_BACKUP_DIR
    if not root.exists():
        return []
    items = []
    for path in sorted(root.iterdir(), reverse=True):
        if path.is_file():
            items.append({"name": path.name, "size_kb": round(path.stat().st_size / 1024, 1)})
    return items


def _make_info_backup(protect: str = "") -> str | None:
    """از فایل فعلی اطلاعات نسخه‌ی پشتیبان می‌سازه (آخرین ۳ نسخه نگه داشته می‌شه).

    فایل‌هایی که اسمشون با `baseline-` شروع بشه نسخه‌های مرجع‌اند (نسخه‌ی اصلی
    قبل از هر تغییری) و هیچ‌وقت پاک نمی‌شن. `protect` اسم نسخه‌ایه که داره
    بازگردانی می‌شه تا موقع پاک‌سازی حذف نشه.
    """
    source = _active_info_path()
    if not source.exists():
        return None
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_path = config.INFO_BACKUP_DIR / f"{stamp}{source.suffix.lower()}"
    try:
        config.INFO_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, backup_path)
    except Exception as exc:
        try:
            backup_path.unlink(missing_ok=True)
        except OSError:
            pass
        app_logger.exception("ساخت نسخه‌ی پشتیبان فایل اطلاعات ناموفق بود.")
        raise RuntimeError("ساخت نسخه‌ی پشتیبان فایل اطلاعات ناموفق بود؛ هیچ فایلی تغییر نکرد.") from exc

    backups = sorted((p for p in config.INFO_BACKUP_DIR.iterdir()
                      if p.is_file() and not p.name.startswith("baseline-")),
                     reverse=True)
    for index, old in enumerate(backups):
        if old.name == protect or index < 3:
            continue
        old.unlink(missing_ok=True)
    return stamp


def _commit_info_files(staged: list[Path]) -> tuple[Path, list[str], Path | None, Path | None]:
    """Prepare the incoming workbook and atomically replace the active file."""
    base = staged[0]
    # اگر چند فایل اطلاعاتی از یک آپلود بیرون آمده بود، شیت‌هایشان را روی هم جمع می‌کنیم.
    for extra in staged[1:]:
        have = set(xlsx_sheet.sheet_names(base))
        for sheet in xlsx_sheet.sheet_names(extra):
            if sheet in have:
                continue
            rows = xlsx_sheet.read_sheet(extra, sheet)
            if rows and any(any(c for c in row) for row in rows):
                try:
                    xlsx_sheet.add_sheet(base, sheet, rows)
                except Exception as exc:  # noqa: BLE001
                    app_logger.warning("ادغام شیت «%s» ناموفق بود: %s", sheet, exc)

    carried = carry_over_info_sheets(base)
    # مسیر مقصد پسوندِ واقعیِ فایل جدید را نگه می‌دارد تا بقیه‌ی کد به اسم فایل وابسته نباشد
    destination = (config.INFO_EXCEL_PATH if base.suffix.lower() in data_store.EXCEL_SUFFIXES
                   else config.INFO_EXCEL_PATH.with_suffix(base.suffix.lower()))
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Invalidate the old snapshot before changing the source file; if invalidation
    # fails, the currently active workbook is still untouched.
    config.INFO_SNAPSHOT_PATH.unlink(missing_ok=True)
    active_before = _active_info_path()
    previous = active_before.with_name(f".{active_before.name}.previous-{secrets.token_hex(8)}")
    had_previous = active_before.exists()
    if had_previous:
        # Keep the old source readable until the new file has been atomically installed.
        shutil.copy2(active_before, previous)
    try:
        os.replace(base, destination)
        if had_previous and active_before != destination:
            active_before.unlink()
    except Exception:
        if destination.exists() and destination != active_before:
            destination.unlink(missing_ok=True)
        if had_previous and previous.exists():
            if not active_before.exists():
                os.replace(previous, active_before)
            else:
                previous.unlink(missing_ok=True)
        raise
    return destination, carried, (previous if had_previous else None), (active_before if had_previous else None)


def _finish_info_commit(destination: Path, previous: Path | None,
                        previous_original: Path | None, success: bool) -> None:
    if success:
        if previous:
            previous.unlink(missing_ok=True)
        return
    destination.unlink(missing_ok=True)
    if previous and previous.exists() and previous_original:
        os.replace(previous, previous_original)


def _apply_dataset(key: str, staged: list[Path], mode: str, drop_missing: bool) -> tuple[bool, str, dict]:
    """Replace source files transactionally with the matching in-memory index."""
    mod = _engine_for(key)
    if key == "charge":
        _make_backup()
        previous = _commit_store_files(staged)
        active = [config.STORE_FILES_DIR / _active_name(p) for p in staged]
        try:
            result = mod.commit_upload(active, mode, drop_missing)
            _finish_store_commit(previous, bool(result[0]))
            if not result[0]:
                mod.reload_data()
            return result
        except Exception:
            _finish_store_commit(previous, False)
            mod.reload_data()
            raise

    _make_info_backup()
    destination, _carried, previous, previous_original = _commit_info_files(staged)
    try:
        result = mod.commit_upload([destination], mode, drop_missing)
        _finish_info_commit(destination, previous, previous_original, bool(result[0]))
        if not result[0]:
            mod.reload_info()
        return result
    except Exception:
        _finish_info_commit(destination, previous, previous_original, False)
        mod.reload_info()
        raise


@app.post("/upload", response_class=HTMLResponse)
@app.post("/", response_class=HTMLResponse)
@app.post("/stores/upload", response_class=HTMLResponse)
@app.post("/info/upload", response_class=HTMLResponse)
async def upload_file(request: Request,
                      files: list[UploadFile] | None = File(None),
                      info_file: UploadFile | None = File(None),
                      mode: str = Form("auto"),
                      drop_missing: str = Form("")):
    """مرحله‌ی اول: فایل‌ها گرفته و به‌صورت خودکار دسته‌بندی می‌شوند، بعد تغییرات نشان داده می‌شود.

    هر شیت مستقل تشخیص داده می‌شود، پس یک فایل چندمنظوره هم درست کار می‌کند:
    شیت‌های شارژ و اطلاعات از هم جدا می‌شوند و هر کدام به موتور خودش می‌روند.
    هنوز چیزی اعمال نمی‌شود.
    """
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)

    if mode not in ("auto", "merge", "replace"):
        mode = "auto"
    drop = drop_missing == "1"

    chosen = [f for f in (list(files or []) + ([info_file] if info_file else [])) if f is not None]
    allowed = data_store.SUPPORTED_SUFFIXES
    bad = [f.filename for f in chosen if Path(f.filename or "").suffix.lower() not in allowed]
    if bad:
        return templates.TemplateResponse(
            "upload.html",
            _upload_ctx(request, flash={"message": "فرمت این فایل‌ها پشتیبانی نمی‌شه: " + "، ".join(bad)
                                        + f" (فرمت‌های مجاز: {', '.join(sorted(allowed))})",
                                        "success": False}),
        )

    try:
        folder = await _stage_uploads(chosen, mode, drop)
    except ValueError as exc:
        return templates.TemplateResponse(
            "upload.html",
            _upload_ctx(request, flash={"message": str(exc), "success": False}),
        )
    except Exception:
        app_logger.exception("ذخیره‌ی موقت فایل آپلودشده ناموفق بود.")
        return templates.TemplateResponse(
            "upload.html",
            _upload_ctx(request, flash={"message": "ذخیره‌ی فایل آپلودشده ناموفق بود؛ فایل‌های قبلی تغییری نکردند.",
                                        "success": False}),
        )
    staged = _staged_paths(folder)
    if not staged:
        shutil.rmtree(folder, ignore_errors=True)
        return templates.TemplateResponse(
            "upload.html", _upload_ctx(request, flash={"message": "هیچ فایل قابل‌خوندنی انتخاب نشده.",
                                                       "success": False}),
        )

    # --- مسیریابی هوشمند: هر فایل/شیت به مجموعه‌داده‌ی درست خودش می‌ره
    work_dir = config.UPLOAD_STAGING_DIR / (folder.name + "-routed")
    routed = dataset_router.route(staged, work_dir)
    routed["charge"] = [p for p in routed.get("charge", []) if p.exists()]
    routed["info"] = [p for p in routed.get("info", []) if p.exists()]
    per_dataset = _route_cards(routed)

    meta: dict = {"mode": mode, "drop_missing": drop,
                  "at": datetime.now().isoformat(timespec="seconds"),
                  "routes": {k: [str(p) for p in routed[k]] for k in DATASET_ORDER if routed[k]},
                  "carried": {}}
    cards: dict[str, dict] = {}
    notes: list[str] = []
    for key in DATASET_ORDER:
        paths = routed.get(key) or []
        if not paths:
            continue
        # شیت‌هایی که توی فایل فعلی هست ولی توی فایل جدید نیست، حفظ می‌شن؛ وگرنه
        # یک آپلودِ ناقص یا تفکیک‌شده، بقیه‌ی داده‌ی فعلی رو پاک می‌کنه.
        if key == "charge":
            carried = carry_over_charge_sheets(paths)
        else:
            carried = carry_over_info_sheets(paths[0])
        if carried:
            meta["carried"][str(paths[0])] = carried
        ok, msg, payload = _engine_for(key).preview_upload(paths, mode, drop)
        if carried:
            msg += "\n🧩 شیت‌هایی که از فایل قبلی حفظ شدند: " + "، ".join(carried)
        cards[key] = {
            "route": per_dataset[key],
            "preview": {"ok": ok, "message": msg, "report": payload.get("report"),
                        "diff": payload.get("diff")},
        }
        notes.append(f"{DATASET_META[key]['emoji']} {DATASET_META[key]['label']}: {msg}")

    (folder / "_upload.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    header = dataset_router.summary(routed)

    if not notes:
        shutil.rmtree(folder, ignore_errors=True)
        shutil.rmtree(work_dir, ignore_errors=True)
        return templates.TemplateResponse(
            "upload.html",
            _upload_ctx(request, route=header,
                        flash={"message": header + "\n\nهیچ شیت قابل‌استفاده‌ای پیدا نشد.",
                               "success": False}),
        )

    return templates.TemplateResponse(
        "upload.html",
        _upload_ctx(request, token=folder.name, route=header, cards=cards,
                    flash={"message": header + "\n\n" + "\n\n".join(notes), "success": None}),
    )


@app.post("/upload/apply", response_class=HTMLResponse)
@app.post("/stores/apply", response_class=HTMLResponse)
@app.post("/info/apply", response_class=HTMLResponse)
def upload_apply(request: Request, token: str = Form(...)):
    """مرحله‌ی دوم: تغییراتی که کاربر دیده و تأیید کرده اعمال می‌شود."""
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)

    safe = _safe_filename(token)
    folder = config.UPLOAD_STAGING_DIR / safe
    work_dir = config.UPLOAD_STAGING_DIR / (safe + "-routed")
    if not folder.is_dir():
        return templates.TemplateResponse(
            "upload.html", _upload_ctx(request, flash={"message": "فایل آپلودشده پیدا نشد (منقضی شده). "
                                                          "دوباره آپلود کن.", "success": False}),
        )

    mode, drop = "merge", False
    meta: dict = {}
    meta_file = folder / "_upload.json"
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            mode = meta.get("mode", "merge")
            drop = bool(meta.get("drop_missing"))
        except Exception:  # noqa: BLE001 - متادیتای خراب نباید جلوی اعمال را بگیرد
            pass

    routes = {k: [Path(p) for p in v] for k, v in (meta.get("routes") or {}).items()}
    cards: dict[str, dict] = {}
    lines: list[str] = []
    ok_all = True
    try:
        for key in DATASET_ORDER:
            staged = [p for p in (routes.get(key) or []) if p.exists()]
            if not staged:
                continue
            carried = (meta.get("carried") or {}).get(str(staged[0]), [])
            before = _get_excel_columns(_active_info_path()) if key == "info" else None
            try:
                ok, msg, payload = _apply_dataset(key, staged, mode, drop)
            except Exception as exc:  # noqa: BLE001 - خطای یک مجموعه‌داده کل درخواست را نباید بکشد
                ok, msg, payload = False, f"خطا در اعمال: {exc}", {}
            ok_all = ok_all and ok
            cards[key] = {"applied_diff": payload.get("diff"), "report": payload.get("report")}
            text = f"{DATASET_META[key]['emoji']} {DATASET_META[key]['label']}: {msg}"
            if carried:
                text += "\n🧩 شیت‌هایی که از فایل قبلی حفظ شدند: " + "، ".join(carried)
            lines.append(text)
            if ok:
                files = _engine_for(key).get_status().get("files") or []
                if files:
                    lines.append(f"📁 فایل‌های فعال: {', '.join(files)}")
                if key == "info" and before and _tables_unchanged(before, _get_excel_columns(_active_info_path())):
                    _notify_file_updated("اطلاعات")
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        shutil.rmtree(work_dir, ignore_errors=True)

    if ok_all and lines and config.ADMIN_CHAT_ID:
        bale_bot.send_message(config.ADMIN_CHAT_ID,
                              ("📄 فایل‌های فروشگاه آپدیت شد.\n" + "\n".join(lines))[:3000])

    return templates.TemplateResponse(
        "upload.html", _upload_ctx(request, cards=cards,
                                   flash={"message": "\n\n".join(lines), "success": ok_all}),
    )


@app.post("/upload/cancel", response_class=HTMLResponse)
@app.post("/stores/cancel", response_class=HTMLResponse)
@app.post("/info/cancel", response_class=HTMLResponse)
def upload_cancel(request: Request, token: str = Form(...)):
    """آپلودِ تأییدنشده را دور می‌ریزد."""
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    safe = _safe_filename(token)
    shutil.rmtree(config.UPLOAD_STAGING_DIR / safe, ignore_errors=True)
    shutil.rmtree(config.UPLOAD_STAGING_DIR / (safe + "-routed"), ignore_errors=True)
    return templates.TemplateResponse(
        "upload.html", _upload_ctx(request, flash={"message": "آپلود لغو شد؛ هیچ تغییری اعمال نشد.",
                                                   "success": False}),
    )


def _rescan(request: Request, keys: tuple[str, ...]):
    """فقط فایل‌های فعلی دوباره خوانده می‌شوند (بدون آپلود جدید)."""
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    lines, ok_all = [], True
    for key in keys:
        mod = _engine_for(key)
        ok, msg = mod.reload_data() if key == "charge" else mod.reload_info()
        ok_all = ok_all and ok
        lines.append(f"{DATASET_META[key]['emoji']} {DATASET_META[key]['label']}: {msg}")
    return templates.TemplateResponse(
        "upload.html", _upload_ctx(request, flash={"message": "\n".join(lines), "success": ok_all}),
    )


@app.post("/upload/rescan", response_class=HTMLResponse)
def upload_rescan(request: Request):
    return _rescan(request, DATASET_ORDER)


@app.post("/stores/rescan", response_class=HTMLResponse)
def stores_rescan(request: Request):
    return _rescan(request, ("charge",))


@app.post("/info/rescan", response_class=HTMLResponse)
def info_rescan(request: Request):
    return _rescan(request, ("info",))


@app.get("/info/rescan", response_class=HTMLResponse)
def info_rescan_page(request: Request):
    return RedirectResponse("/upload", status_code=302)


def _restore(request: Request, key: str, backup: str):
    """بازگرداندن یکی از نسخه‌های پشتیبانِ مجموعه‌داده."""
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    meta = DATASET_META[key]
    if key == "charge":
        folder = config.STORE_BACKUP_DIR / _safe_filename(backup)
        if not folder.is_dir() or not any(folder.iterdir()):
            return templates.TemplateResponse(
                "upload.html", _upload_ctx(request, flash={"message": "نسخه‌ی پشتیبان پیدا نشد.",
                                                          "success": False}),
            )
        _make_backup(protect=folder.name)
        config.STORE_FILES_DIR.mkdir(parents=True, exist_ok=True)
        for old in config.STORE_FILES_DIR.iterdir():
            if old.is_file():
                old.unlink()
        for path in folder.iterdir():
            if path.is_file():
                shutil.copy2(path, config.STORE_FILES_DIR / _active_name(path))
        ok, msg = data_store.reload_data()
    else:
        source = config.INFO_BACKUP_DIR / _safe_filename(backup)
        if not source.is_file():
            return templates.TemplateResponse(
                "upload.html", _upload_ctx(request, flash={"message": "نسخه‌ی پشتیبان پیدا نشد.",
                                                          "success": False}),
            )
        _make_info_backup(protect=source.name)
        shutil.copy2(source, _active_info_path())
        config.INFO_SNAPSHOT_PATH.unlink(missing_ok=True)
        ok, msg = info_store.reload_info()
    return templates.TemplateResponse(
        "upload.html",
        _upload_ctx(request,
                    flash={"message": f"{meta['emoji']} {meta['label']}: بازگردانی انجام شد.\n" + msg,
                           "success": ok}),
    )


@app.post("/upload/restore", response_class=HTMLResponse)
def upload_restore(request: Request, kind: str = Form("charge"), backup: str = Form(...)):
    """بازگرداندن پشتیبانِ مجموعه‌داده‌ی انتخابی (`kind`: charge یا info)."""
    return _restore(request, kind if kind in DATASET_ORDER else "charge", backup)


@app.post("/stores/restore", response_class=HTMLResponse)
def stores_restore(request: Request, backup: str = Form(...)):
    return _restore(request, "charge", backup)


@app.post("/info/restore", response_class=HTMLResponse)
def info_restore(request: Request, backup: str = Form(...)):
    return _restore(request, "info", backup)


@app.post("/schedule/upload", response_class=HTMLResponse)
async def upload_schedule(request: Request, schedule_file: UploadFile = File(...)):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)

    # اگه کاربر فایل اکسل/CSV فروشگاه‌ها رو اشتباهی توی فرم شیفت داده، خودکار می‌فرستیمش
    # به بخش فایل فروشگاه‌ها، به‌جای اینکه بگیم «شیفت نامعتبر».
    if Path(_safe_filename(schedule_file.filename or "")).suffix.lower() in data_store.SUPPORTED_SUFFIXES:
        return await upload_file(request=request, files=[schedule_file], mode="merge", drop_missing="")

    ctx = _upload_ctx(request)

    if not schedule_file.filename.endswith(".pdf"):
        return templates.TemplateResponse(
            "upload.html",
            {**ctx, "schedule_message": "برنامه‌ی شیفت باید PDF باشه. اگه منظورت فایل اطلاعات "
                                        "فروشگاه‌هاست، همون فرم بالا رو استفاده کن.", "schedule_success": False},
        )

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    contents = await schedule_file.read()
    with open(schedule_store.PDF_PATH, "wb") as f:
        f.write(contents)

    ok, msg = schedule_store.reload_schedule()

    if ok and config.ADMIN_CHAT_ID:
        bale_bot.send_message(config.ADMIN_CHAT_ID, "📄 فایل شیفت آپدیت شد.")

    return templates.TemplateResponse(
        "upload.html",
        {**ctx, "schedule_message": msg, "schedule_success": ok},
    )


@app.get("/schedule", response_class=HTMLResponse)
def schedule_list_page(request: Request):
    """صفحه‌ی اصلی «برنامه شیفت»: همه‌ی روزها با جستجو، مرتب‌سازی و دکمه‌ی ویرایش کنار هر روز."""
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(
        "schedule_list.html",
        {
            "request": request,
            "days": schedule_store.list_all(),
            "today_jalali": jdatetime.date.today().strftime("%Y/%m/%d"),
            "active": "schedule",
        },
    )


@app.post("/api/schedule/day")
async def schedule_day_update(request: Request, payload: dict = Body(...)):
    """ویرایش یک روز از برنامه شیفت از داخل مودال صفحه‌ی /schedule."""
    if not _is_authed(request):
        return JSONResponse({"ok": False, "message": "نشست شما منقضی شده، دوباره وارد پنل بشید."}, status_code=401)

    date = str(payload.get("date") or "").strip()
    record = schedule_store.update_day(
        date,
        desk1=str(payload.get("desk1") or "").strip(),
        desk2=str(payload.get("desk2") or "").strip(),
        night_primary=str(payload.get("night_primary") or "").strip(),
        night_secondary=str(payload.get("night_secondary") or "").strip(),
        morning_shift=str(payload.get("morning_shift") or "").strip(),
        evening_shift=str(payload.get("evening_shift") or "").strip(),
    )
    if record is None:
        return JSONResponse({"ok": False, "message": "این تاریخ توی برنامه شیفت پیدا نشد."}, status_code=404)

    bale_bot.notify_schedule_edited(record)
    return JSONResponse({"ok": True, "message": "✅ شیفت این روز بروزرسانی شد.", "record": record})


@app.post("/info/upload", response_class=HTMLResponse)
@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(
        "users.html",
        {
            "request": request,
            "users": registration.list_all(),
            "pending_users": registration.list_pending(),
            "active": "users",
        },
    )


@app.post("/api/users/{user_id}")
def update_user(user_id: str, request: dict):
    """اطلاعات کاربر رو به‌روز می‌کنه."""
    full_name = request.get("full_name")
    first_name = request.get("first_name")
    last_name = request.get("last_name")
    result = registration.update(user_id, full_name=full_name, first_name=first_name, last_name=last_name)
    if result is None:
        return JSONResponse({"error": "کاربر پیدا نشد."}, status_code=404)
    return {"ok": True, "message": "اطلاعات کاربر به‌روز شد.", "user": result}


@app.post("/api/users/{user_id}/approve")
def approve_user(user_id: str):
    result = registration.approve(user_id)
    if result is None:
        return JSONResponse({"error": "کاربر پیدا نشد."}, status_code=404)
    return {"ok": True, "message": "کاربر تایید شد.", "user": result}


@app.post("/api/users/{user_id}/reject")
def reject_user(user_id: str):
    result = registration.reject(user_id)
    if result is None:
        return JSONResponse({"error": "کاربر پیدا نشد."}, status_code=404)
    return {"ok": True, "message": "تایید کاربر لغو شد.", "user": result}


def _lookup_registered_name(user_id: str) -> str:
    for u in registration.list_all():
        if str(u.get("user_id")) == str(user_id):
            return u.get("full_name") or ""
    return ""


def _access_user_candidates() -> list[dict]:
    """کاربرهای شناخته‌شده (ثبت‌نام‌شده + دیده‌شده توی گروه) برای لیست کشویی بخش‌های دسترسی."""
    candidates: dict[str, dict] = {}

    for u in registration.list_all():
        uid = str(u.get("user_id") or "").strip()
        if not uid:
            continue
        name = (u.get("full_name") or "").strip() or " ".join(
            p for p in [u.get("first_name"), u.get("last_name")] if p
        ).strip()
        candidates[uid] = {"user_id": uid, "name": name, "username": (u.get("username") or "").strip()}

    for m in group_members.list_all():
        uid = str(m.get("user_id") or "").strip()
        if not uid:
            continue
        name = (m.get("display_name") or "").strip()
        username = (m.get("username") or "").strip()
        if uid in candidates:
            if not candidates[uid]["name"]:
                candidates[uid]["name"] = name
            if not candidates[uid]["username"]:
                candidates[uid]["username"] = username
        else:
            candidates[uid] = {"user_id": uid, "name": name, "username": username}

    result = []
    for c in candidates.values():
        if c["name"] and c["username"]:
            label = f"{c['name']} (@{c['username']})"
        elif c["name"]:
            label = c["name"]
        elif c["username"]:
            label = f"@{c['username']}"
        else:
            label = c["user_id"]
        c["label"] = label
        result.append(c)
    result.sort(key=lambda c: c["label"])
    return result


def _lookup_access_name(user_id: str) -> str:
    for c in _access_user_candidates():
        if c["user_id"] == str(user_id):
            return c["name"] or (f"@{c['username']}" if c["username"] else "")
    return ""


@app.get("/charge-admins", response_class=HTMLResponse)
def charge_admins_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(
        "charge_admins.html",
        {"request": request, "message": None, "admins": charge_admins.list_all(), "users": _access_user_candidates(), "active": "charge_admins"},
    )


@app.post("/charge-admins/add", response_class=HTMLResponse)
def charge_admins_add(request: Request, user_id: str = Form(...), name: str = Form("")):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)

    user_id = user_id.strip()
    if not user_id.isdigit():
        message = "❌ آیدی باید فقط عدد باشه."
    else:
        display_name = name.strip() or _lookup_access_name(user_id)
        charge_admins.add(user_id, display_name)
        message = f"✅ آیدی {user_id} به لیست مسئولین شارژ اضافه شد."

    return templates.TemplateResponse(
        "charge_admins.html",
        {"request": request, "message": message, "admins": charge_admins.list_all(), "users": _access_user_candidates(), "active": "charge_admins"},
    )


@app.post("/charge-admins/remove", response_class=HTMLResponse)
def charge_admins_remove(request: Request, user_id: str = Form(...)):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)

    charge_admins.remove(user_id)
    return templates.TemplateResponse(
        "charge_admins.html",
        {"request": request, "message": f"🗑 آیدی {user_id} حذف شد.", "admins": charge_admins.list_all(), "users": _access_user_candidates(), "active": "charge_admins"},
    )


@app.get("/reset-admins", response_class=HTMLResponse)
def reset_admins_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(
        "reset_admins.html",
        {"request": request, "message": None, "admins": reset_admins.list_all(), "users": _access_user_candidates(), "active": "reset_admins"},
    )


@app.post("/reset-admins/add", response_class=HTMLResponse)
def reset_admins_add(request: Request, user_id: str = Form(...), name: str = Form("")):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)

    user_id = user_id.strip()
    if not user_id.isdigit():
        message = "❌ آیدی باید فقط عدد باشه."
    else:
        display_name = name.strip() or _lookup_access_name(user_id)
        reset_admins.add(user_id, display_name)
        message = f"✅ آیدی {user_id} به لیست مسئولین ریست رمز اضافه شد."

    return templates.TemplateResponse(
        "reset_admins.html",
        {"request": request, "message": message, "admins": reset_admins.list_all(), "users": _access_user_candidates(), "active": "reset_admins"},
    )


@app.post("/reset-admins/remove", response_class=HTMLResponse)
def reset_admins_remove(request: Request, user_id: str = Form(...)):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)

    reset_admins.remove(user_id)
    return templates.TemplateResponse(
        "reset_admins.html",
        {"request": request, "message": f"🗑 آیدی {user_id} حذف شد.", "admins": reset_admins.list_all(), "users": _access_user_candidates(), "active": "reset_admins"},
    )


def _format_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} ثانیه"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} دقیقه"
    hours = minutes // 60
    minutes = minutes % 60
    return f"{hours} ساعت و {minutes} دقیقه" if minutes else f"{hours} ساعت"


def _build_usage_data():
    user_stats = usage_stats.all_user_stats()
    users = []
    for u in registration.list_all():
        stats = user_stats.get(str(u.get("user_id")), {})
        users.append({
            **u,
            "total": stats.get("total", 0),
            "actions": stats.get("actions", {}),
            "last_action_at": stats.get("last_action_at"),
        })
    users.sort(key=lambda u: (-u["total"], u.get("full_name") or ""))
    active_users = [u for u in users if u["total"] > 0]
    inactive_users = [u for u in users if u["total"] == 0]

    admin_stats = usage_stats.all_charge_admin_stats()
    admins = []
    for a in charge_admins.list_all():
        stats = admin_stats.get(str(a.get("user_id")), {})
        responded = stats.get("responded_count", 0)
        avg_seconds = (stats.get("total_response_seconds", 0.0) / responded) if responded else 0
        admins.append({
            **a,
            "responded_count": responded,
            "done_count": stats.get("done_count", 0),
            "noissue_count": stats.get("noissue_count", 0),
            "avg_response": _format_duration(avg_seconds) if responded else "-",
            "last_response_at": stats.get("last_response_at"),
        })
    admins.sort(key=lambda a: -a["responded_count"])
    top_admin_id = admins[0]["user_id"] if admins and admins[0]["responded_count"] > 0 else None

    reset_admin_stats = usage_stats.all_reset_admin_stats()
    reset_admins_list = []
    for a in reset_admins.list_all():
        stats = reset_admin_stats.get(str(a.get("user_id")), {})
        responded = stats.get("responded_count", 0)
        avg_seconds = (stats.get("total_response_seconds", 0.0) / responded) if responded else 0
        reset_admins_list.append({
            **a,
            "responded_count": responded,
            "done_count": stats.get("done_count", 0),
            "wrong_count": stats.get("wrong_count", 0),
            "avg_response": _format_duration(avg_seconds) if responded else "-",
            "last_response_at": stats.get("last_response_at"),
        })
    reset_admins_list.sort(key=lambda a: -a["responded_count"])
    top_reset_admin_id = reset_admins_list[0]["user_id"] if reset_admins_list and reset_admins_list[0]["responded_count"] > 0 else None

    return active_users, inactive_users, admins, top_admin_id, reset_admins_list, top_reset_admin_id


STATE_LABELS = {
    "online": ("آنلاین", "🟢"),
    "recent": ("اخیراً فعال", "🟡"),
    "idle": ("آفلاین", "⚪"),
    "unseen": ("بدون حضور", "⚫"),
}


def _humanize_minutes(minutes: float | None) -> str:
    """«۳ دقیقه پیش» / «۲ ساعت پیش» / «۵ روز پیش» به فارسی."""
    if minutes is None:
        return "تا حالا دیده نشده"
    m = int(minutes)
    if m < 1:
        return "همین الان"
    if m < 60:
        return f"{m} دقیقه پیش"
    hours = m // 60
    if hours < 24:
        return f"{hours} ساعت پیش"
    days = hours // 24
    if days < 30:
        return f"{days} روز پیش"
    return f"{days // 30} ماه پیش"


def _humanize_iso(iso: str | None) -> str:
    """تاریخ جلالی «۱۴۰۳/۰۵/۱۲ ۱۴:۳۰» از ISO."""
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return "—"
    return jdatetime.datetime.fromgregorian(datetime=dt).strftime("%Y/%m/%d %H:%M")


def _to_jalali(value) -> str:
    """هر تاریخ/ساعت ذخیره‌شده (ISO یا «YYYY/MM/DD HH:MM:SS») رو به شمسی تبدیل می‌کنه."""
    if value in (None, ""):
        return ""
    s = str(value).strip()
    dt = None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
        "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
        "%Y-%m-%d", "%Y/%m/%d",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return s
    jd = jdatetime.datetime.fromgregorian(datetime=dt)
    return jd.strftime("%Y/%m/%d %H:%M") if ":" in s else jd.strftime("%Y/%m/%d")


templates.env.filters["shamsi"] = _to_jalali


def _build_monitor_users() -> list[dict]:
    """ردیف مانیتورینگ هر کاربر: حضور آنلاین + عملکرد (امروز، هفته، کل)."""
    presence_by_id = presence.snapshot()
    usage_by_id = usage_stats.all_user_stats()
    today = jdatetime.date.today().strftime("%Y/%m/%d")
    week_from, week_to = _default_week_range()
    range_by_id = usage_stats.range_user_stats(week_from, week_to)
    today_by_id = usage_stats.range_user_stats(today, today)

    known = {str(u.get("user_id")): u for u in registration.list_all()}
    # کاربرایی که فقط توی presence هستن (ثبت‌نام نکرده/پاک شدن) هم دیده بشن
    for uid in presence_by_id:
        known.setdefault(uid, {"user_id": uid, "full_name": "", "approved": False})

    rows = []
    for uid, user in known.items():
        pr = presence_by_id.get(uid, {})
        us = usage_by_id.get(uid, {})
        wk = range_by_id.get(uid, {})
        td = today_by_id.get(uid, {})
        minutes = pr.get("minutes_since")
        if minutes is None and us.get("last_action_at"):
            try:
                last = datetime.fromisoformat(us["last_action_at"])
                minutes = (datetime.now() - last).total_seconds() / 60.0
            except (TypeError, ValueError):
                minutes = None
        state = pr.get("state") or ("unseen" if minutes is None else "idle")
        label, dot = STATE_LABELS.get(state, ("—", "⚪"))
        rows.append({
            "user_id": uid,
            "name": user.get("full_name") or pr.get("display_name") or f"کاربر {uid}",
            "username": user.get("username") or pr.get("username") or "",
            "approved": bool(user.get("approved")),
            "state": state,
            "state_label": label,
            "state_dot": dot,
            "last_seen": pr.get("last_seen") or us.get("last_action_at"),
            "last_seen_human": _humanize_minutes(minutes),
            "last_seen_jalali": _humanize_iso(pr.get("last_seen") or us.get("last_action_at")),
            "messages": pr.get("messages", 0),
            "messages_today": pr.get("today", 0),
            "actions": us.get("actions", {}),
            "total": us.get("total", 0),
            "charge": us.get("actions", {}).get("charge", 0),
            "info": us.get("actions", {}).get("info", 0),
            "shift": us.get("actions", {}).get("shift", 0),
            "reset": us.get("actions", {}).get("reset", 0),
            "week_total": wk.get("total", 0),
            "usage_today": td.get("total", 0),
            "online_rank": 0,
        })

    rows.sort(key=lambda r: ({"online": 0, "recent": 1, "idle": 2, "unseen": 3}[r["state"]],
                            -r["messages_today"], -r["total"], r["name"]))
    for i, r in enumerate(rows, 1):
        r["online_rank"] = i
    return rows


def _build_monitor_stores() -> dict:
    """آمار کارشناس‌ها: بی‌کارشناس‌ها، منفک‌ها و پراکندگی منطقه‌ای."""
    dir_rows, _cols = info_store.get_directory_rows()
    closed = info_store.closed_codes()
    unset = info_store.EXPERT_UNSET

    active = [r for r in dir_rows
              if str(r.get(info_store.INFO_CODE_COL) or "").strip() not in closed]

    plain_unassigned = []
    separated = []
    per_expert: dict[str, int] = {}
    region_unassigned: dict[str, int] = {}
    region_total: dict[str, int] = {}

    for r in active:
        code = str(r.get(info_store.INFO_CODE_COL) or "").strip()
        expert = str(r.get(info_store.EXPERT_COLUMN) or "").strip()
        region = str(r.get("منطقه نهائی") or r.get("منطقه فروش") or "").strip() or "نامشخص"
        name = str(r.get("فروشگاه") or r.get("نام فروشگاه") or "").strip()
        region_total[region] = region_total.get(region, 0) + 1

        if not expert or expert == unset:
            plain_unassigned.append({"code": code, "name": name, "region": region})
            region_unassigned[region] = region_unassigned.get(region, 0) + 1
            continue

        rec = separated_experts.match(expert)
        if rec:
            separated.append({
                "code": code,
                "name": name,
                "region": region,
                "file_expert": expert,
                "expert": rec.get("name") or expert,
                "reason": rec.get("reason") or "",
                "since": rec.get("since") or "",
            })
            region_unassigned[region] = region_unassigned.get(region, 0) + 1
            continue

        per_expert[expert] = per_expert.get(expert, 0) + 1

    effective_unassigned = len(plain_unassigned) + len(separated)

    by_region = [{
        "region": name,
        "unassigned": cnt,
        "total": region_total.get(name, cnt),
        "pct": round(100 * cnt / region_total[name]) if region_total.get(name) else 0,
    } for name, cnt in region_unassigned.items()]
    by_region.sort(key=lambda r: -r["unassigned"])

    separated_by_expert: dict[str, dict] = {}
    for s in separated:
        g = separated_by_expert.setdefault(s["expert"], {
            "expert": s["expert"],
            "count": 0,
            "reason": s["reason"],
            "since": s["since"],
            "stores": [],
        })
        g["count"] += 1
        g["stores"].append({"code": s["code"], "name": s["name"], "region": s["region"]})
    separated_groups = sorted(separated_by_expert.values(), key=lambda g: -g["count"])

    return {
        "total": len(active),
        "closed": len(closed),
        "assigned": len(per_expert),
        "plain_unassigned": plain_unassigned,
        "plain_unassigned_count": len(plain_unassigned),
        "separated": separated,
        "separated_count": len(separated),
        "separated_groups": separated_groups,
        "effective_unassigned": effective_unassigned,
        "effective_pct": round(100 * effective_unassigned / len(active), 1) if active else 0.0,
        "by_region": by_region,
        "per_expert": sorted(per_expert.items(), key=lambda kv: -kv[1]),
        "expert_count": len(per_expert),
    }


_stores_cache: dict = {"at": 0.0, "value": None}
_STORES_CACHE_TTL = 60.0
_lock = threading.Lock()


def _build_monitor_stores_cached() -> dict:
    """آمار فروشگاه‌ها با کش ۶۰ثانیه‌ای.

    خروجی از روی فایل اکسل ساخته می‌شه (حدود ۲۵۰ms) و خیلی تندتغییر نمی‌کنه؛
    بدون کش، هر بار رفرش صفحه‌ی آپلود این هزینه رو پرداخت می‌کرد.
    """
    now = time.time()
    with _lock:
        if _stores_cache["value"] is not None and now - _stores_cache["at"] < _STORES_CACHE_TTL:
            return _stores_cache["value"]
        value = _build_monitor_stores()
        _stores_cache["at"] = now
        _stores_cache["value"] = value
        return value


def _build_monitor_kpis() -> dict:
    """فقط شش عددِ نوار مانیتورینگ صفحه‌ی اول (بدون ساختن جدول کامل)."""
    states = {"online": 0, "recent": 0, "idle": 0, "unseen": 0}
    users = _build_monitor_users()
    for u in users:
        states[u["state"]] = states.get(u["state"], 0) + 1

    stores = _build_monitor_stores_cached()
    return {
        "states": states,
        "total_users": len(users),
        "active_today": _active_today_count(users),
        "never_active": sum(1 for u in users if u["total"] == 0 and u["messages"] == 0),
        "stores": {
            "total": stores["total"],
            "closed": stores["closed"],
            "assigned": stores["assigned"],
            "expert_count": stores["expert_count"],
            "effective_unassigned": stores["effective_unassigned"],
            "effective_pct": stores["effective_pct"],
            "plain_unassigned_count": stores["plain_unassigned_count"],
            "separated_count": stores["separated_count"],
            "separated_groups": [{"expert": g["expert"], "count": g["count"]}
                                 for g in stores["separated_groups"]],
        },
        "generated_jalali": jdatetime.datetime.now().strftime("%Y/%m/%d %H:%M"),
    }


def _active_today_count(users: list[dict]) -> int:
    """تعداد کاربرانی که واقعاً امروز چیزی کردن: پیام دادن یا از ربات استفاده کردن."""
    return sum(1 for u in users if u.get("messages_today") or u.get("usage_today"))


def _build_monitor() -> dict:
    users = _build_monitor_users()
    stores = _build_monitor_stores_cached()
    states = {"online": 0, "recent": 0, "idle": 0, "unseen": 0}
    for u in users:
        states[u["state"]] = states.get(u["state"], 0) + 1
    return {
        "users": users,
        "stores": stores,
        "states": states,
        "total_users": len(users),
        "active_today": _active_today_count(users),
        "never_active": sum(1 for u in users if u["total"] == 0 and u["messages"] == 0),
        "pending_users": sum(1 for u in users if not u["approved"]),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "generated_jalali": jdatetime.datetime.now().strftime("%Y/%m/%d %H:%M"),
        "separated_list": separated_experts.list_all(),
    }


def _monitor_activity_series(days: int = 7) -> list[dict]:
    """آمار فعالیتِ هر روزِ چند روز اخیر، برای نمودار ستونی مانیتورینگ."""
    today = jdatetime.date.today()
    out = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        key = d.strftime("%Y/%m/%d")
        day_stats = usage_stats.range_user_stats(key, key)
        charge = info = shift = reset = 0
        people = 0
        for u in day_stats.values():
            if u.get("total"):
                people += 1
            charge += u["actions"].get("charge", 0)
            info += u["actions"].get("info", 0)
            shift += u["actions"].get("shift", 0)
            reset += u["actions"].get("reset", 0)
        out.append({
            "date": key,
            "label": d.strftime("%d/%m"),
            "day": ["ش", "ی", "د", "س", "چ", "پ", "ج"][d.weekday()],
            "total": charge + info + shift + reset,
            "charge": charge,
            "info": info,
            "shift": shift,
            "reset": reset,
            "people": people,
        })
    return out


def _monitor_alerts(mon: dict) -> list[dict]:
    """هشدارهای خودکارِ مانیتورینگ، از مهم‌ترین به کم‌اهمیت‌ترین."""
    st = mon["stores"]
    alerts: list[dict] = []

    if st["separated_count"]:
        names = "، ".join(g["expert"] for g in st["separated_groups"])
        alerts.append({
            "level": "crit",
            "icon": "🧍",
            "title": f"{st['separated_count']} فروشگاه دستِ کارشناسِ منفک",
            "body": f"کارشناس {names} منفک شده ولی هنوز کسی مسئولیت این فروشگاه‌ها رو نگرفته.",
            "link": "/monitor#separated",
        })

    regions_100 = [r for r in st["by_region"] if r["pct"] >= 100 and r["total"] >= 5]
    if regions_100:
        alerts.append({
            "level": "crit",
            "icon": "🗺",
            "title": f"{len(regions_100)} منطقه کلاً بی‌کارشناس",
            "body": "، ".join(f"{r['region']} ({r['total']} فروشگاه)" for r in regions_100),
            "link": "/monitor#coverage",
        })

    if st["plain_unassigned_count"]:
        alerts.append({
            "level": "warn",
            "icon": "🚫",
            "title": f"{st['plain_unassigned_count']} فروشگاه «{info_store.EXPERT_UNSET}»",
            "body": f"در مجموع {st['effective_unassigned']} فروشگاه فعال ({st['effective_pct']}٪) کارشناس نداره.",
            "link": "/monitor#coverage",
        })

    if mon["states"]["online"] == 0:
        alerts.append({
            "level": "warn",
            "icon": "🟢",
            "title": "هیچ‌کس آنلاین نیست",
            "body": f"{mon['states']['recent']} نفر توی ۶ ساعت اخیر فعال بودن، ولی الان کسی زیر ۱۵ دقیقه پیام نداده.",
            "link": "/monitor#team",
        })

    ghosts = [u for u in mon["users"] if u["state"] == "unseen" and u["total"] == 0]
    if ghosts:
        alerts.append({
            "level": "warn",
            "icon": "👻",
            "title": f"{len(ghosts)} کاربر هیچ‌وقت استفاده نکرده",
            "body": "، ".join(u["name"] for u in ghosts[:6]) + (" و…" if len(ghosts) > 6 else ""),
            "link": "/monitor#team",
        })

    if mon["pending_users"]:
        alerts.append({
            "level": "info",
            "icon": "⏳",
            "title": f"{mon['pending_users']} ثبت‌نام در انتظار تایید",
            "body": "ثبت‌نام کردن ولی هنوز توسط ادمین تایید نشدن.",
            "link": "/users",
        })

    inactive = [u for u in mon["users"] if u["total"] > 0 and u["messages"] == 0]
    if inactive:
        alerts.append({
            "level": "info",
            "icon": "💤",
            "title": f"{len(inactive)} نفر اخیراً دیده نشدن",
            "body": "قبلاً کار می‌کردن ولی آخرین حضورشون ثبت نشده.",
            "link": "/monitor#team",
        })

    order = {"crit": 0, "warn": 1, "info": 2}
    alerts.sort(key=lambda a: order[a["level"]])
    return alerts


def _build_monitor_page() -> dict:
    """کانتکست کامل صفحه‌ی مانیتورینگ مستقل."""
    mon = _build_monitor()

    series = _monitor_activity_series()
    today_total = series[-1]["total"] if series else 0
    week_total = sum(s["total"] for s in series)
    week_peak = max(series, key=lambda s: s["total"]) if series else None

    _, _, admins, top_admin_id, reset_admins_list, top_reset_admin_id = _build_usage_data()
    admins = [a for a in admins if a["responded_count"] > 0]
    reset_admins_list = [a for a in reset_admins_list if a["responded_count"] > 0]

    hot_stores = _enrich_code_stats(usage_stats.all_code_stats(), limit=12)

    mon.update({
        "series": series,
        "today_total": today_total,
        "week_total": week_total,
        "week_peak": week_peak,
        "week_people": sum(s["people"] for s in series),
        "admins": admins,
        "top_admin_id": top_admin_id,
        "reset_admins": reset_admins_list,
        "top_reset_admin_id": top_reset_admin_id,
        "hot_stores": hot_stores,
        "alerts": _monitor_alerts(mon),
        "load_ms": None,
    })
    stores = mon.get("stores") or {}
    if "covered_count" not in stores:
        # مجموع فروشگاه‌های دارای کارشناس
        stores["covered_count"] = sum(c for _, c in (stores.get("per_expert") or [])) or stores.get("assigned") or 0
    mon["stores"] = stores
    return mon


def _default_week_range() -> tuple[str, str]:
    """بازه‌ی هفته‌ی جاری رو برمی‌گردونه: شنبه تا جمعه‌ی همون هفته (تاریخ جلالی)."""
    today = jdatetime.date.today()
    week_start = today - timedelta(days=today.weekday())  # weekday(): شنبه=0
    week_end = week_start + timedelta(days=6)
    return week_start.strftime("%Y/%m/%d"), week_end.strftime("%Y/%m/%d")


def _enrich_code_stats(code_stats: dict, limit: int = 15) -> list[dict]:
    """آمار جستجوی کدهای فروشگاه رو با محل نصب (اگه پیدا بشه) غنی می‌کنه و نزولی مرتب می‌کنه."""
    result = []
    for code, stats in code_stats.items():
        records = data_store.lookup(code)
        location = records[0].get("location") if records else ""
        result.append({
            "code": code,
            "total": stats["total"],
            "actions": stats.get("actions", {}),
            "location": location,
            "last_requested_at": stats.get("last_requested_at"),
        })
    result.sort(key=lambda r: -r["total"])
    return result[:limit]


def _build_range_usage_data(from_date: str, to_date: str) -> tuple[list[dict], dict | None, dict | None, list[dict]]:
    """آمار استفاده‌ی کاربرها و پرتقاضاترین فروشگاه‌ها رو فقط برای بازه‌ی [from_date, to_date] می‌سازه
    (مرتب‌شده نزولی)، و پرتقاضاترین «شارژ» همون بازه رو هم برمی‌گردونه."""
    range_stats = usage_stats.range_user_stats(from_date, to_date)
    reg_by_id = {str(u.get("user_id")): u for u in registration.list_all()}

    range_users = []
    for uid, stats in range_stats.items():
        reg = reg_by_id.get(uid)
        range_users.append({
            "user_id": uid,
            "full_name": reg.get("full_name") if reg else None,
            "total": stats["total"],
            "actions": stats["actions"],
        })
    range_users.sort(key=lambda u: -u["total"])

    top_charge_requester = None
    charge_ranked = [u for u in range_users if u["actions"].get("charge")]
    charge_ranked.sort(key=lambda u: -u["actions"]["charge"])
    if charge_ranked:
        top_charge_requester = charge_ranked[0]

    top_reset_requester = None
    reset_ranked = [u for u in range_users if u["actions"].get("reset")]
    reset_ranked.sort(key=lambda u: -u["actions"]["reset"])
    if reset_ranked:
        top_reset_requester = reset_ranked[0]

    range_codes = _enrich_code_stats(usage_stats.range_code_stats(from_date, to_date))

    return range_users, top_charge_requester, top_reset_requester, range_codes


def _build_usage_overview(from_date: str, to_date: str) -> dict:
    """آمار دوره‌ای کاربران و حضور پیام‌رسان را برای صفحه‌ی گزارش آماده می‌کند."""
    period_stats = usage_stats.range_user_stats(from_date, to_date)
    all_stats = usage_stats.all_user_stats()
    users = []
    for user in registration.list_all():
        uid = str(user.get("user_id") or "")
        period = period_stats.get(uid, {})
        lifetime = all_stats.get(uid, {})
        users.append({
            **user,
            "user_id": uid,
            "total": period.get("total", 0),
            "actions": period.get("actions", {}),
            "last_action_at": lifetime.get("last_action_at"),
            "lifetime_total": lifetime.get("total", 0),
        })

    active = [user for user in users if user["total"] > 0]
    inactive = [user for user in users if user["total"] == 0]
    active.sort(key=lambda user: (-user["total"], user.get("full_name") or ""))
    least_active = sorted(active, key=lambda user: (user["total"], user.get("full_name") or ""))

    action_totals = {"charge": 0, "info": 0, "reset": 0, "shift": 0}
    for user in users:
        for action, count in (user.get("actions") or {}).items():
            action_totals[action] = action_totals.get(action, 0) + count

    registered_by_id = {user["user_id"]: user for user in users}
    presence_rows = []
    for uid, row in presence.snapshot().items():
        registered = registered_by_id.get(str(uid), {})
        presence_rows.append({
            "user_id": str(uid),
            "full_name": registered.get("full_name") or row.get("display_name") or f"کاربر {uid}",
            "username": registered.get("username") or row.get("username") or "",
            "messages": row.get("messages", 0),
            "today_messages": row.get("today", 0),
            "week_messages": row.get("week", 0),
            "sources": row.get("sources") or {},
            "last_seen": row.get("last_seen"),
            "state": row.get("state", "unseen"),
            "state_label": STATE_LABELS.get(row.get("state"), ("نامشخص", "⚪"))[0],
        })
    presence_rows.sort(key=lambda row: (-row["messages"], row["full_name"]))

    return {
        "period_top_users": active[:10],
        "period_low_users": least_active[:10],
        "period_inactive_count": len(inactive),
        "period_active_count": len(active),
        "period_total_actions": sum(action_totals.values()),
        "period_action_totals": action_totals,
        "registered_count": len(users),
        "approved_count": sum(1 for user in users if user.get("approved")),
        "presence_rows": presence_rows[:15],
        "presence_total_messages": sum(row["messages"] for row in presence_rows),
        "presence_users_count": len(presence_rows),
        "presence_counts": presence.state_counts(),
    }


@app.get("/usage", response_class=HTMLResponse)
def usage_page(request: Request, from_date: str = "", to_date: str = ""):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)

    active_users, inactive_users, admins, top_admin_id, reset_admins_list, top_reset_admin_id = _build_usage_data()

    default_from, default_to = _default_week_range()
    from_date = from_date.strip() or default_from
    to_date = to_date.strip() or default_to
    range_users, top_charge_requester, top_reset_requester, range_codes = _build_range_usage_data(from_date, to_date)
    top_codes = _enrich_code_stats(usage_stats.all_code_stats())
    overview = _build_usage_overview(from_date, to_date)

    return templates.TemplateResponse(
        "usage.html",
        {
            "request": request,
            **overview,
            "active_users": active_users,
            "inactive_users": inactive_users,
            "admins": admins,
            "top_admin_id": top_admin_id,
            "reset_admins": reset_admins_list,
            "top_reset_admin_id": top_reset_admin_id,
            "groups": groups.list_all(),
            "from_date": from_date,
            "to_date": to_date,
            "default_from": default_from,
            "default_to": default_to,
            "range_users": range_users,
            "top_charge_requester": top_charge_requester,
            "top_reset_requester": top_reset_requester,
            "range_codes": range_codes,
            "top_codes": top_codes,
            "message": None,
            "active": "usage",
        },
    )


@app.post("/usage/send", response_class=HTMLResponse)
def usage_send(request: Request, chat_id: str = Form(...)):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)

    active_users, inactive_users, admins, top_admin_id, reset_admins_list, top_reset_admin_id = _build_usage_data()

    text = bale_bot.format_usage_report(active_users, inactive_users, admins, top_admin_id, reset_admins_list, top_reset_admin_id)
    target = next((g for g in groups.list_all() if str(g.get("chat_id")) == str(chat_id)), None)
    if target is None:
        message = "❌ گروه انتخاب‌شده در فهرست گروه‌های شناخته‌شده نیست."
    else:
        msg_id = bale_bot.send_message(target["chat_id"], text)
        message = "✅ گزارش به گروه ارسال شد." if msg_id is not None else "❌ ارسال گزارش ناموفق بود."

    from_date, to_date = _default_week_range()
    range_users, top_charge_requester, top_reset_requester, range_codes = _build_range_usage_data(from_date, to_date)
    top_codes = _enrich_code_stats(usage_stats.all_code_stats())
    overview = _build_usage_overview(from_date, to_date)

    return templates.TemplateResponse(
        "usage.html",
        {
            "request": request,
            **overview,
            "active_users": active_users,
            "inactive_users": inactive_users,
            "admins": admins,
            "top_admin_id": top_admin_id,
            "reset_admins": reset_admins_list,
            "top_reset_admin_id": top_reset_admin_id,
            "groups": groups.list_all(),
            "from_date": from_date,
            "to_date": to_date,
            "default_from": from_date,
            "default_to": to_date,
            "range_users": range_users,
            "top_charge_requester": top_charge_requester,
            "top_reset_requester": top_reset_requester,
            "range_codes": range_codes,
            "top_codes": top_codes,
            "message": message,
            "active": "usage",
        },
    )


def _member_status_from_record(reg: dict | None) -> tuple[str, str]:
    """وضعیت احراز هویت رو از رکورد ثبت‌نام (اگه وجود داشته باشه) برمی‌گردونه: (کلید, برچسب فارسی)."""
    if reg is None:
        return "unregistered", "⛔ ثبت‌نام نکرده"
    if reg.get("approved", False):
        return "approved", "✅ احراز شده"
    return "pending", "⏳ در انتظار تایید"


def _build_group_auth_data() -> tuple[list[dict], list[dict]]:
    """برمی‌گردونه (groups_data, unseen_registered).
    unseen_registered یعنی کاربرایی که ثبت‌نام/تایید شدن ولی از وقتی ردیابی گروه
    فعال شده (یعنی از امروز)، هنوز پیامی ازشون توی هیچ گروهی دیده نشده —
    معمولاً چون ثبت‌نامشون از قبل بوده و بعدش چیزی توی گروه ننوشتن.
    وقتی کاربر ثبت‌نام‌شده باشه، full_name/first_name/last_name هم برای ویرایش
    از پنل (همون /api/users) کنارش میاد."""
    order = {"unregistered": 0, "pending": 1, "approved": 2}
    reg_by_id = {str(u.get("user_id")): u for u in registration.list_all()}
    seen_user_ids: set[str] = set()
    result = []
    for g in groups.list_all():
        members = []
        for m in group_members.list_for_chat(g["chat_id"]):
            uid = str(m["user_id"])
            seen_user_ids.add(uid)
            reg = reg_by_id.get(uid)
            status_key, status_label = _member_status_from_record(reg)
            members.append({
                **m,
                "status_key": status_key,
                "status_label": status_label,
                "full_name": reg.get("full_name") if reg else None,
                "first_name": reg.get("first_name") if reg else None,
                "last_name": reg.get("last_name") if reg else None,
            })
        members.sort(key=lambda m: (order[m["status_key"]], m["display_name"] or ""))
        counts = {
            "approved": sum(1 for m in members if m["status_key"] == "approved"),
            "pending": sum(1 for m in members if m["status_key"] == "pending"),
            "unregistered": sum(1 for m in members if m["status_key"] == "unregistered"),
        }
        result.append({
            "chat_id": g["chat_id"],
            "title": g.get("title") or str(g["chat_id"]),
            "members": members,
            "counts": counts,
            "real_member_count": bale_bot.get_chat_member_count(g["chat_id"]),
        })

    unseen_registered = []
    for uid, reg in reg_by_id.items():
        if uid in seen_user_ids:
            continue
        status_key, status_label = _member_status_from_record(reg)
        unseen_registered.append({**reg, "status_key": status_key, "status_label": status_label})
    unseen_registered.sort(key=lambda u: (order[u["status_key"]], u.get("full_name") or ""))

    return result, unseen_registered


def _group_summary_stats() -> dict:
    """خلاصه‌ی آماری برای نوار بالای سایدبار: تعداد واقعی افراد گروه (طبق API بله)،
    تعداد احراز شده‌ها، و «روح»ها (اعضای گروه که هنوز احراز هویت نشدن).
    چون ربات فقط به یه گروه اختصاص داره، همه‌ی کاربرای تایید‌شده رو متعلق به همون گروه در نظر می‌گیریم."""
    approved_total = sum(1 for u in registration.list_all() if u.get("approved", False))

    member_total = 0
    got_real_count = False
    for g in groups.list_all():
        count = bale_bot.get_chat_member_count(g["chat_id"])
        if count is not None:
            member_total += count
            got_real_count = True

    if not got_real_count:
        # فال‌بک: اگه API بله جواب نداد، از تعداد کسایی که پیام دادن استفاده کن
        groups_data, _ = _build_group_auth_data()
        member_total = sum(len(g["members"]) for g in groups_data)

    return {
        "total_members": member_total,
        "total_approved": approved_total,
        "total_ghost": max(member_total - approved_total, 0),
    }


templates.env.globals["group_summary_stats"] = _group_summary_stats


@app.post("/group-stats/send")
def group_stats_send(request: Request):
    if not _is_authed(request):
        return JSONResponse({"ok": False, "error": "نشست شما منقضی شده، دوباره وارد پنل بشید."}, status_code=401)

    all_groups = groups.list_all()
    if not all_groups:
        return {"ok": False, "error": "هنوز هیچ گروهی شناسایی نشده."}

    text = bale_bot.format_group_stats_report(_group_summary_stats())
    sent_any = False
    for g in all_groups:
        if bale_bot.send_message(g["chat_id"], text) is not None:
            sent_any = True

    if not sent_any:
        return {"ok": False, "error": "ارسال پیام به گروه ناموفق بود."}
    return {"ok": True}


@app.get("/group-auth", response_class=HTMLResponse)
def group_auth_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    groups_data, unseen_registered = _build_group_auth_data()
    return templates.TemplateResponse(
        "group_auth.html",
        {"request": request, "groups_data": groups_data, "unseen_registered": unseen_registered, "message": None, "active": "group_auth"},
    )


@app.post("/group-auth/send", response_class=HTMLResponse)
def group_auth_send(request: Request, chat_id: str = Form(...)):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)

    groups_data, unseen_registered = _build_group_auth_data()
    target = next((g for g in groups_data if str(g["chat_id"]) == chat_id), None)
    if target is None:
        message = "❌ گروه پیدا نشد."
    else:
        text = bale_bot.format_group_auth_report(target["title"], target["members"])
        msg_id = bale_bot.send_message(int(chat_id), text)
        message = "✅ گزارش به گروه ارسال شد." if msg_id is not None else "❌ ارسال گزارش ناموفق بود."

    return templates.TemplateResponse(
        "group_auth.html",
        {"request": request, "groups_data": groups_data, "unseen_registered": unseen_registered, "message": message, "active": "group_auth"},
    )


@app.post("/group-auth/remind", response_class=HTMLResponse)
def group_auth_remind(request: Request, chat_id: str = Form(...)):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)

    groups_data, unseen_registered = _build_group_auth_data()
    target = next((g for g in groups_data if str(g["chat_id"]) == chat_id), None)
    if target is None:
        message = "❌ گروه پیدا نشد."
    else:
        # توجه: target["counts"]["approved"] فقط شامل کسانیه که پیام‌شون تو گروه دیده شده،
        # نه کل احراز‌شده‌ها؛ برای «روح» باید از تعداد واقعی احراز‌شده‌های ثبت‌نامی استفاده کرد
        # (چون ربات فقط به همین یه گروه اختصاص داره).
        real_count = target["real_member_count"]
        if real_count is None:
            real_count = target["counts"]["approved"] + target["counts"]["pending"] + target["counts"]["unregistered"]
        approved_total = sum(1 for u in registration.list_all() if u.get("approved", False))
        ghost_count = max(real_count - approved_total, 0)
        msg_id = bale_bot.send_group_auth_reminder(int(chat_id), ghost_count)
        message = "✅ یادآوری احراز هویت به گروه ارسال شد." if msg_id is not None else "❌ ارسال یادآوری ناموفق بود."

    return templates.TemplateResponse(
        "group_auth.html",
        {"request": request, "groups_data": groups_data, "unseen_registered": unseen_registered, "message": message, "active": "group_auth"},
    )


@app.get("/closed-stores", response_class=HTMLResponse)
def closed_stores_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)

    closed = info_store.closed_codes()
    dir_rows, _ = info_store.get_directory_rows()
    items = []
    for r in dir_rows:
        code = info_store._normalize_code(r.get(info_store.INFO_CODE_COL))
        if code and code in closed:
            items.append({
                "code": code,
                "name": str(r.get("فروشگاه") or r.get("نام فروشگاه") or "").strip(),
                "region": str(r.get("منطقه نهائی") or r.get("منطقه فروش") or "").strip() or "نامشخص",
            })
    items.sort(key=lambda x: x["code"])

    return templates.TemplateResponse("closed_stores.html", {
        "request": request,
        "active": "closed_stores",
        "items": items,
        "count": len(items),
    })


@app.get("/data", response_class=HTMLResponse)
def data_browser_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)

    store_groups = {}
    for r in data_store.list_all():
        source = r.get("source", "")
        columns = ["code"] + [c for c in data_store.get_source_columns(source)
                              if c not in ("code", "mobile", "operator", "location", "source")]
        group = store_groups.setdefault(source, {"columns": columns, "rows": []})
        if not group["rows"]:
            group["columns"] = columns
        group["rows"].append(r)

    EXPERT_COL = info_store.EXPERT_COLUMN
    UNSET = info_store.EXPERT_UNSET

    all_dir_rows, dir_columns = info_store.get_directory_rows()
    # فروشگاه‌های بسته فقط در تب مستقل خودشان می‌آیند؛ همه‌ی شمارش‌ها و
    # گروه‌بندی کارشناسان از فهرست فعال‌ها محاسبه می‌شود.
    closed_codes_set = info_store.closed_codes()
    dir_rows, closed_rows = info_store.partition_directory_rows(
        all_dir_rows, closed_codes_set
    )

    # کارشناسِ منفک‌شده (مثل محمد شیری) هم «فاقد کارشناس» حساب می‌شه،
    # هم‌طور که مانیتورینگ نشون می‌ده.
    for r in dir_rows:
        expert = str(r.get(EXPERT_COL) or "").strip()
        if expert and separated_experts.is_separated(expert):
            r[EXPERT_COL] = UNSET

    # شیت خام «کارشناس انفورماتیک» در تب اطلاعات هم همین‌طور هماهنگ شود:
    # منفک‌ها و سلول‌های خالی کارشناس → «فاقد کارشناس»
    info_sheet_groups = info_store.get_sheet_groups()
    for group in info_sheet_groups.values():
        if EXPERT_COL not in group["columns"]:
            continue
        for row in group["rows"]:
            value = str(row.get(EXPERT_COL) or "").strip()
            if not value or separated_experts.is_separated(value):
                row[EXPERT_COL] = UNSET

    unassigned = sum(1 for r in dir_rows if r.get(EXPERT_COL) == UNSET)
    expert_names = sorted({r.get(EXPERT_COL, "") for r in dir_rows
                           if r.get(EXPERT_COL) and r.get(EXPERT_COL) != UNSET})

    expert_groups = info_store.get_expert_groups()
    merged: list[dict] = []
    unset_target: dict | None = None
    for g in expert_groups:
        if g["expert"] == UNSET:
            if unset_target is None:
                unset_target = g
                merged.append(g)
            else:
                # دسته‌ی اصلیِ «فاقد کارشناس» که بعد از گروه منفک آمده
                unset_target["count"] += g["count"]
                unset_target["stores"].extend(g["stores"])
        elif separated_experts.is_separated(g["expert"]):
            # فروشگاه‌های کارشناس منفک داخل دسته‌ی «فاقد کارشناس» ادغام می‌شوند
            if unset_target is None:
                unset_target = {"expert": UNSET, "count": 0, "stores": []}
                merged.append(unset_target)
            unset_target["count"] += g["count"]
            unset_target["stores"].extend(g["stores"])
        else:
            merged.append(g)

    unassigned = sum(g["count"] for g in merged if g["expert"] == UNSET)
    expert_names = sorted({r.get(EXPERT_COL, "") for r in dir_rows
                           if r.get(EXPERT_COL) and r.get(EXPERT_COL) != UNSET})
    expert_groups = merged

    return templates.TemplateResponse(
        "data_browser.html",
        {
            "request": request,
            "store_groups": store_groups,
            "info_groups": info_sheet_groups,
            "directory_rows": dir_rows,
            "directory_columns": dir_columns,
            "closed_rows": closed_rows,
            "closed_count": len(closed_rows),
            "directory_all_columns": info_store.get_directory_columns(),
            "directory_unassigned": unassigned,
            "expert_names": expert_names,
        "expert_groups": merged,
        "expert_column": info_store.EXPERT_COLUMN,
        "expert_unset": info_store.EXPERT_UNSET,
            "region_rows": region_experts.regions(),
            "announce_group_title": ANNOUNCE_GROUP_TITLE,
            "active": "data",
        },
    )


@app.get("/monitor", response_class=HTMLResponse)
def monitor_page(request: Request):
    """پایش زنده ادغام شده با داشبورد."""
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/dashboard", status_code=302)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request):
    """داشبورد جدید با کامپوننت‌های FarsiUI."""
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)

    import time as _time
    t0 = _time.time()
    payload = _build_monitor_page()
    payload["load_ms"] = int((_time.time() - t0) * 1000)

    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "monitor": payload, "active": "dashboard"},
    )


@app.get("/api/monitor/full", response_class=JSONResponse)
def api_monitor_full(request: Request):
    """داده‌ی کامل مانیتورینگ برای تازه‌سازی زنده‌ی صفحه‌ی /monitor."""
    if not _is_authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(_build_monitor_page())


@app.get("/components", response_class=HTMLResponse)
def components_demo_page(request: Request):
    """صفحه‌ی راهنمای کامپوننت‌ها: نمونه‌ی زنده‌ی farsiui.css."""
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(
        "components_demo.html",
        {"request": request, "active": ""},
    )


@app.get("/api/monitor/stores", response_class=JSONResponse)
def api_monitor_stores(request: Request, group: str = "all"):
    """لیست فروشگاه‌ها برای دکلیک اعداد مانیتورینگ.

    group: all | unassigned | separated | assigned | closed | by_expert
    """
    if not _is_authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    closed = set(info_store.closed_codes())
    unset = info_store.EXPERT_UNSET
    dir_rows, _ = info_store.get_directory_rows()

    def _item(r: dict) -> dict:
        code = str(r.get(info_store.INFO_CODE_COL) or "").strip()
        return {
            "code": code,
            "name": str(r.get("فروشگاه") or r.get("نام فروشگاه") or "").strip(),
            "region": str(r.get("منطقه نهائی") or r.get("منطقه فروش") or "").strip() or "نامشخص",
            "expert": str(r.get(info_store.EXPERT_COLUMN) or "").strip(),
        }

    active, closed_items = [], []
    for r in dir_rows:
        item = _item(r)
        if not item["code"]:
            continue
        if item["code"] in closed:
            closed_items.append(item)
        else:
            active.append(item)

    def _status(item: dict) -> str:
        expr = item["expert"]
        if not expr or expr == unset:
            return "unassigned"
        if separated_experts.match(expr):
            return "separated"
        return "assigned"

    by_expert: dict[str, list[dict]] = {}
    for item in active:
        st = _status(item)
        item["status"] = st
        if st not in ("assigned", "separated"):
            continue
        by_expert.setdefault(item["expert"], []).append(item)

    titles = {
        "all": "همه‌ی فروشگاه‌های فعال",
        "unassigned": "فروشگاه بدون کارشناس",
        "separated": "فروشگاه کارشناسِ منفک",
        "assigned": "فروشگاه کارشناس‌دار",
        "closed": "فروشگاه‌های بسته",
    }

    if group == "all":
        items = active
    elif group == "unassigned":
        items = [i for i in active if i["status"] == "unassigned"]
    elif group == "separated":
        items = [i for i in active if i["status"] == "separated"]
    elif group == "assigned":
        items = [i for i in active if i["status"] == "assigned"]
    elif group == "closed":
        items = closed_items
    elif group == "by_expert":
        return JSONResponse({"items": [
            {"expert": name, "count": len(rows), "stores": rows}
            for name, rows in sorted(by_expert.items(), key=lambda kv: -len(kv[1]))
        ]})
    else:
        items = []

    items.sort(key=lambda i: i["code"])
    return JSONResponse({
        "group": group,
        "title": titles.get(group, group),
        "total": len(items),
        "items": items,
    })


@app.get("/api/monitor/store/{code}", response_class=JSONResponse)
def api_monitor_store(code: str, request: Request):
    """جزئیات یه فروشگاه برای دکلیک داغ‌ترین‌ها: وضعیت + آمار جستجو."""
    if not _is_authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    code = (code or "").strip()
    records = data_store.lookup(code) or []
    meta = records[0] if records else {}
    location = str(meta.get("location") or meta.get("محل نصب") or "").strip() if isinstance(meta, dict) else ""

    # وضعیت از دیتای فروش/کارشناس
    closed = code in info_store.closed_codes()
    unset = info_store.EXPERT_UNSET
    expert, region, name = "", "", ""
    dir_rows, _ = info_store.get_directory_rows()
    for r in dir_rows:
        if str(r.get(info_store.INFO_CODE_COL) or "").strip() == code:
            name = str(r.get("فروشگاه") or r.get("نام فروشگاه") or "").strip()
            expert = str(r.get(info_store.EXPERT_COLUMN) or "").strip()
            region = str(r.get("منطقه نهائی") or r.get("منطقه فروش") or "").strip() or "نامشخص"
            break

    if not expert or expert == unset:
        status = "unassigned"
    elif separated_experts.match(expert):
        status = "separated"
    else:
        status = "assigned"

    usage = usage_stats.all_code_stats().get(code, {"total": 0, "actions": {}, "last_requested_at": None})

    # سری ۷ روز اخیر
    series = []
    today = jdatetime.date.today()
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        k = d.strftime("%Y/%m/%d")
        day = usage_stats.range_code_stats(k, k).get(code, {"total": 0, "actions": {}})
        series.append({
            "label": d.strftime("%d/%m"),
            "total": day["total"],
            "actions": day["actions"],
        })

    return JSONResponse({
        "code": code,
        "name": name or location or "-",
        "location": location,
        "region": region,
        "expert": expert or "-",
        "status": status,
        "closed": closed,
        "usage": {
            "total": usage.get("total", 0),
            "actions": usage.get("actions", {}),
            "last_requested_at": usage.get("last_requested_at"),
        },
        "series": series,
        "total_lookups": len(records),
    })


@app.get("/api/requests/open", response_class=JSONResponse)
def api_requests_open(request: Request, kind: str = "all"):
    """درخواست‌های باز (پاسخ‌نشده) شارژ و ریست رمز، برای دکلیک مسئولین."""
    if not _is_authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    now = datetime.now().timestamp()
    out = []
    pools = [("charge", bale_bot._charge_requests), ("reset", bale_bot._reset_requests)]
    for kindname, pool in pools:
        if kind not in ("all", kindname):
            continue
        for rid, r in (pool or {}).items():
            if r.get("resolved"):
                continue
            created = r.get("created_at")
            out.append({
                "id": rid,
                "kind": kindname,
                "target": r.get("target") or r.get("code"),
                "display_name": r.get("display_name"),
                "created_jalali": (jdatetime.datetime.fromgregorian(
                    datetime=datetime.fromtimestamp(created)).strftime("%Y/%m/%d %H:%M")) if created else None,
                "age_seconds": round(now - created) if created else 0,
                "group_chat_id": r.get("group_chat_id"),
            })
    out.sort(key=lambda x: x.get("age_seconds", 0), reverse=True)
    return JSONResponse({"items": out, "total": len(out)})


@app.get("/broadcast", response_class=HTMLResponse)
def broadcast_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(
        "broadcast.html",
        {"request": request, "message": None, "groups": groups.list_all(), "active": "broadcast"},
    )


@app.post("/broadcast", response_class=HTMLResponse)
def broadcast_send(request: Request, chat_id: str = Form(...), text: str = Form(...)):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)

    text = text.strip()
    target = next((g for g in groups.list_all() if str(g.get("chat_id")) == str(chat_id)), None)
    if not text:
        message = "❌ متن پیام نمی‌تونه خالی باشه."
    elif target is None:
        message = "❌ گروه انتخاب‌شده در فهرست گروه‌های شناخته‌شده نیست."
    else:
        msg_id = bale_bot.send_message(target["chat_id"], text)
        message = "✅ پیام به گروه ارسال شد." if msg_id is not None else "❌ ارسال پیام ناموفق بود."

    return templates.TemplateResponse(
        "broadcast.html",
        {"request": request, "message": message, "groups": groups.list_all(), "active": "broadcast"},
    )


@app.get("/api/monitor", response_class=JSONResponse)
def api_monitor(request: Request):
    """داده‌ی زنده‌ی مانیتورینگ، برای تازه‌سازی خودکار بدون ری‌لود صفحه."""
    if not _is_authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    mon = _build_monitor()
    now = datetime.now()
    users = [{
        "user_id": u["user_id"],
        "name": u["name"],
        "username": u["username"],
        "approved": u["approved"],
        "state": u["state"],
        "state_label": u["state_label"],
        "state_dot": u["state_dot"],
        "last_seen_human": u["last_seen_human"],
        "last_seen_jalali": u["last_seen_jalali"],
        "messages": u["messages"],
        "messages_today": u["messages_today"],
        "total": u["total"],
        "charge": u["charge"],
        "info": u["info"],
        "shift": u["shift"],
        "reset": u["reset"],
        "week_total": u["week_total"],
    } for u in mon["users"]]

    return JSONResponse({
        "generated_at": mon["generated_at"],
        "generated_jalali": mon["generated_jalali"],
        "total_users": mon["total_users"],
        "states": mon["states"],
        "active_today": mon["active_today"],
        "never_active": mon["never_active"],
        "pending_users": mon["pending_users"],
        "users": users,
        "stores": {
            "total": mon["stores"]["total"],
            "closed": mon["stores"]["closed"],
            "assigned": mon["stores"]["assigned"],
            "expert_count": mon["stores"]["expert_count"],
            "effective_unassigned": mon["stores"]["effective_unassigned"],
            "effective_pct": mon["stores"]["effective_pct"],
            "plain_unassigned_count": mon["stores"]["plain_unassigned_count"],
            "separated_count": mon["stores"]["separated_count"],
        },
    })


@app.post("/api/monitor/separated", response_class=JSONResponse)
async def api_separated_add(request: Request, payload: dict = Body(...)):
    """افزودن یه کارشناس به لیست منفک‌ها."""
    if not _is_authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        rec = separated_experts.add(
            payload.get("name", ""),
            payload.get("reason", ""),
            payload.get("aliases") or [],
        )
        return JSONResponse({"ok": True, "record": rec})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/monitor/separated/remove", response_class=JSONResponse)
async def api_separated_remove(request: Request, payload: dict = Body(...)):
    """حذف یه کارشناس از لیست منفک‌ها."""
    if not _is_authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    ok = separated_experts.remove(payload.get("name", ""))
    return JSONResponse({"ok": ok})


@app.get("/schedule-times", response_class=HTMLResponse)
def schedule_times_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(
        "schedule_times.html",
        {"request": request, "message": None, "times": schedule_times.list_all(), "active": "schedule_times"},
    )


@app.post("/schedule-times/add", response_class=HTMLResponse)
def schedule_times_add(request: Request, time_value: str = Form(...)):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)

    ok, message = schedule_times.add(time_value)
    return templates.TemplateResponse(
        "schedule_times.html",
        {"request": request, "message": message, "times": schedule_times.list_all(), "active": "schedule_times"},
    )


@app.post("/schedule-times/remove", response_class=HTMLResponse)
def schedule_times_remove(request: Request, time_value: str = Form(...)):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)

    schedule_times.remove(time_value)
    return templates.TemplateResponse(
        "schedule_times.html",
        {"request": request, "message": f"🗑 ساعت {time_value} حذف شد.", "times": schedule_times.list_all(), "active": "schedule_times"},
    )


@app.get("/api/status")
def status():
    """اندپوینت ساده برای بررسی وضعیت (برای مانیتورینگ)."""
    return data_store.get_status()


# --- فیلدهای اکسل ---
FIELDS_CONFIG_PATH = config.DATA_DIR / "fields_config.json"


def _load_fields_config() -> dict:
    if FIELDS_CONFIG_PATH.exists():
        try:
            with open(FIELDS_CONFIG_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_fields_config(cfg: dict) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(FIELDS_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _get_excel_columns(excel_path, sheet_names: list[str] | None = None) -> dict[str, list[str]]:
    """ستون‌های همه شیت‌های یه فایل اکسل رو برمی‌گردونه."""
    import pandas as pd
    result = {}
    if not excel_path.exists():
        return result
    try:
        xls = pd.ExcelFile(excel_path)
    except Exception:
        return result
    for name in xls.sheet_names:
        if sheet_names and name.strip() not in [s.strip() for s in sheet_names]:
            continue
        try:
            df = pd.read_excel(xls, sheet_name=name, nrows=0, dtype=str)
            result[name.strip()] = [str(c).strip() for c in df.columns]
        except Exception:
            continue
    return result


def _tables_unchanged(old_columns: dict[str, list[str]], new_columns: dict[str, list[str]]) -> bool:
    """چک می‌کنه شیت‌ها و ستون‌های فایل جدید نسبت به نسخه قبلی فرقی نکرده."""
    if set(old_columns) != set(new_columns):
        return False
    return all(set(old_columns[sheet]) == set(new_columns[sheet]) for sheet in old_columns)


def _notify_file_updated(file_label: str) -> None:
    """وقتی فایل جدیدی با همون ساختار جدول‌های قبلی آپلود میشه، به ادمین خبر میده."""
    if not config.ADMIN_CHAT_ID:
        return
    text = f"📄 فایل {file_label} آپدیت شد.\n✅ ساختار جدول‌ها با نسخه قبلی یکیه."
    bale_bot.send_message(config.ADMIN_CHAT_ID, text)


@app.get("/api/fields/{file_type}")
def get_fields(file_type: str):
    """فیلدهای فایل رو برمی‌گردونه. file_type: 'stores' یا 'info'"""
    if file_type == "stores":
        # فایل شارژ: ستون‌ها از روی فایلی که واقعاً آپلود شده (همه‌ی شیت‌ها) خونده می‌شن
        columns = {s["sheet"]: list(s.get("columns", [])) for s in data_store.get_schema().get("sheets", [])}
    elif file_type == "info":
        # فایل اطلاعات: ستون‌ها از روی شیت‌هایی که واقعاً ایندکس شدن
        columns = {s["sheet"]: list(s.get("columns", [])) for s in info_store.get_schema().get("sheets", [])
                   if s.get("indexed")}
    else:
        return JSONResponse({"error": "نوع فایل نامعتبر"}, status_code=400)

    cfg = _load_fields_config()

    # ترکیب ستون‌ها با تنظیمات ذخیره‌شده
    result = {}

    labels_map = {}
    if file_type == "info":
        labels_map = dict(info_store.INFO_LABEL_MAP)

    for sheet, cols in columns.items():
        saved = cfg.get(file_type, {}).get(sheet, {})
        saved_order = saved.get("order", cols)
        hidden = saved.get("hidden", [])
        saved_labels = saved.get("labels", {})

        # حفظ ترتیب ذخیره‌شده، ستون‌های جدید رو اضافه کن
        ordered = [c for c in saved_order if c in cols]
        for c in cols:
            if c not in ordered:
                ordered.append(c)

        # لیبل‌های ذخیره‌شده رو با پیش‌فرض ادغام کن
        merged_labels = dict(labels_map)
        merged_labels.update(saved_labels)
        for c in ordered:
            if c not in merged_labels:
                merged_labels[c] = c

        result[sheet] = {
            "columns": ordered,
            "hidden": [c for c in hidden if c in cols],
            "labels": {c: merged_labels.get(c, c) for c in ordered},
        }

    return result


@app.post("/api/fields/{file_type}")
def save_fields(file_type: str, request: dict):
    """ترتیب و وضعیت نمایش فیلدها رو ذخیره می‌کنه."""
    cfg = _load_fields_config()
    if file_type not in cfg:
        cfg[file_type] = {}

    for sheet_name, sheet_data in request.items():
        cfg[file_type][sheet_name] = {
            "order": sheet_data.get("order", []),
            "hidden": sheet_data.get("hidden", []),
            "labels": sheet_data.get("labels", {}),
        }

    _save_fields_config(cfg)
    return {"ok": True, "message": "تنظیمات فیلدها ذخیره شد."}


@app.get("/api/store-list/columns")
def store_list_columns():
    """ستون‌های فهرست یکپارچه‌ی فروشگاه‌ها به‌همراه وضعیت نمایششون."""
    return {"columns": info_store.get_directory_columns()}


def _split_message(text: str) -> list[str]:
    return region_experts._split(text)


def _announce_kind_ok(kind: str) -> str | None:
    if kind not in ("regions", "unassigned"):
        return "نوع اطلاع‌رسانی نامعتبره"
    return None


def _apply_expert_change(action, *args) -> dict:
    """تغییر کارشناس (تکی یا گروهی): اول بکاپ، بعد اعمال، و در صورت خطا برگردان."""
    backup = _make_info_backup()
    try:
        result = action(*args)
    except Exception as exc:  # noqa: BLE001
        if backup:
            shutil.copy2(config.INFO_BACKUP_DIR / f"{backup}.xlsx", config.INFO_EXCEL_PATH)
            info_store.reload_info()
        return {"ok": False, "message": f"خطا: {exc}"}
    result["ok"] = True
    result["backup"] = backup
    return result


@app.post("/api/expert/store")
async def set_store_expert(request: dict):
    """کارشناسِ یک فروشگاه را از تب «لیست کارشناسان» عوض می‌کند."""
    code = str(request.get("code") or "").strip()
    if not code:
        return JSONResponse({"ok": False, "message": "کد فروشگاه رو وارد کن."}, status_code=400)
    result = _apply_expert_change(info_store.set_store_expert, code, request.get("expert"))
    if result.get("ok"):
        result["message"] = f"✅ {code} → {result['expert']}"
    return JSONResponse(result)


@app.post("/api/expert/bulk-store")
async def bulk_store_expert(request: dict):
    """ذخیره‌ی گروهیِ dropdownهای تب «لیست کارشناسان».

    به‌جای اینکه با هر انتخاب یک درخواست و رفرش بفرسته، همه‌ی انتخاب‌های
    عوض‌شده در یک درخواست و یک بار بازنویسیِ شیت ذخیره می‌شن.
    """
    changes = request.get("changes")
    if not isinstance(changes, list) or not changes:
        return JSONResponse({"ok": False, "message": "هیچ تغییری برای ذخیره نبود."}, status_code=400)

    pairs: list[tuple[str, str]] = []
    for item in changes:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        expert = item.get("expert")
        if code and expert is not None:
            pairs.append((code, str(expert)))
    if not pairs:
        return JSONResponse({"ok": False, "message": "تغییر معتبری نبود."}, status_code=400)

    result = _apply_expert_change(info_store.set_store_experts_bulk, pairs)
    if result.get("ok"):
        n = result.get("changed", 0)
        result["message"] = (f"✅ {n} فروشگاه ذخیره شد."
                             if n else "تغییری اعمال نشد (مقادیر با قبلی یکی بود).")
    return JSONResponse(result)


@app.post("/api/region-experts")
async def save_region_experts(request: dict):
    """ذخیره‌ی نگاشت منطقه → کارشناس انفورماتیک."""
    pairs = request.get("regions")
    if not isinstance(pairs, dict) or not pairs:
        return JSONResponse({"ok": False, "message": "هیچ منطقه‌ای برای ذخیره نبود."}, status_code=400)
    result = region_experts.set_many(pairs)
    result["ok"] = True
    result["message"] = f"✅ {result['changed']} منطقه ذخیره شد."
    return JSONResponse(result)


@app.post("/api/announce/preview")
async def announce_preview(request: dict):
    """متن پیام اطلاع‌رسانی را می‌سازد ولی چیزی نمی‌فرستد."""
    kind = str(request.get("kind") or "").strip()
    bad = _announce_kind_ok(kind)
    if bad:
        return JSONResponse({"ok": False, "message": bad}, status_code=400)
    try:
        text = region_experts.render(kind)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": f"خطا: {exc}"}, status_code=400)
    return JSONResponse({"ok": True, "kind": kind, "text": text})


@app.post("/api/announce/send")
async def announce_send(request: dict):
    """ارسال اطلاع‌رسانی به گروه. متن از سمت سرور ساخته می‌شود.

    عمداً متنِ فرستاده‌شده از کلاینت گرفته نمی‌شود تا پیامی که می‌رود همیشه
    با وضعیت فعلی داده‌ها بخواند.
    """
    kind = str(request.get("kind") or "").strip()
    bad = _announce_kind_ok(kind)
    if bad:
        return JSONResponse({"ok": False, "message": bad}, status_code=400)
    try:
        text = region_experts.render(kind)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": f"خطا: {exc}"}, status_code=400)

    # فقط گروه «انفورماتیک اوشانک». عمداً fallback نداریم: اگه این گروه پیدا
    # نشه، خطا می‌دهیم تا پیام اشتباهی به بقیه‌ی گروه‌ها نره.
    targets = [g for g in groups.list_all() if g.get("title") == ANNOUNCE_GROUP_TITLE]
    if not targets:
        return JSONResponse(
            {"ok": False, "message": f"گروه «{ANNOUNCE_GROUP_TITLE}» پیدا نشد؛ چیزی ارسال نشد."},
            status_code=400)

    sent, failed = 0, 0
    for group in targets:
        for chunk in _split_message(text):
            if bale_bot.send_message(int(group["chat_id"]), chunk) is None:
                failed += 1
            else:
                sent += 1
    if not sent:
        return JSONResponse({"ok": False, "message": "❌ ارسال به هیچ گروهی موفق نبود."}, status_code=502)
    note = f" (⚠️ {failed} بخش ناموفق)" if failed else ""
    return JSONResponse({"ok": True, "sent": sent,
                         "message": f"✅ به {len(targets)} گروه ارسال شد ({sent} پیام{note})."})


@app.post("/api/expert/rename")
async def rename_expert(request: dict):
    """اسم یک کارشناس را روی همه‌ی فروشگاه‌هایش عوض می‌کند."""
    old = str(request.get("from") or "").strip()
    new = str(request.get("to") or "").strip()
    if not old or not new:
        return JSONResponse({"ok": False, "message": "اسم فعلی و جدید رو وارد کن."}, status_code=400)
    result = _apply_expert_change(info_store.rename_expert, old, new)
    if result.get("ok"):
        n = result.get("changed", 0)
        result["message"] = (f"✅ {n} فروشگاه از «{old}» به «{new}» تغییر کرد."
                             if n else f"فروشگاهی با نام «{old}» پیدا نشد.")
    return JSONResponse(result)


@app.post("/api/expert/bulk")
async def bulk_expert(request: dict):
    """ویرایش گروهی کارشناس انفورماتیک بر اساس محدوده‌ی کد فروشگاه.

    دو حالت داره: `preview` (هیچ تغییری نمی‌ده، فقط می‌گه چند کد و کدوم کارشناس
    الان دارن) و `apply` (شیت «کارشناس انفورماتیک» رو بازنویسی می‌کنه). قبل از
    اعمال حتماً بکاپ گرفته می‌شه.
    """
    mode = "preview" if request.get("preview") else "apply"
    code_from = str(request.get("code_from") or "").strip()
    code_to = str(request.get("code_to") or "").strip() or code_from
    only_empty = bool(request.get("only_empty", True))
    if not code_from:
        return JSONResponse({"ok": False, "message": "محدوده‌ی کد رو وارد کن."}, status_code=400)

    if mode == "preview":
        try:
            data = info_store.preview_expert_bulk(code_from, code_to, only_empty)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "message": f"خطا: {exc}"}, status_code=400)
        data["ok"] = True
        return data

    expert = str(request.get("expert") or "").strip()
    if not expert:
        return JSONResponse({"ok": False, "message": "نام کارشناس رو وارد کن."}, status_code=400)

    backup = _make_info_backup()
    try:
        result = info_store.bulk_assign_expert(code_from, code_to, expert, only_empty)
    except Exception as exc:  # noqa: BLE001
        # اگه نوشتن شکست خورد، نسخه‌ی قبلی برگردونه
        if backup:
            shutil.copy2(config.INFO_BACKUP_DIR / f"{backup}.xlsx", config.INFO_EXCEL_PATH)
            info_store.reload_info()
        return JSONResponse({"ok": False, "message": f"خطا: {exc}"}, status_code=400)

    result["ok"] = True
    result["backup"] = backup
    if not result.get("changed"):
        result["message"] = "هیچ فروشگاهی با این محدوده و شرط‌ها پیدا نشد."
    elif result.get("overwritten"):
        result["message"] = (f"✅ {result['changed']} فروشگاه به «{expert}» تخصیص یافت "
                             f"({result['filled']} خالی بود، {result['overwritten']} مورد بازنویسی شد).")
    else:
        result["message"] = f"✅ {result['changed']} فروشگاه به «{expert}» تخصیص یافت."
    return result


@app.post("/api/store-list/columns")
def save_store_list_columns(request: dict):
    """ذخیره‌ی انتخاب ستون‌های فهرست فروشگاه‌ها."""
    hidden = request.get("hidden")
    if not isinstance(hidden, list):
        return JSONResponse({"ok": False, "message": "ورودی نامعتبر"}, status_code=400)
    info_store.save_directory_columns([str(c) for c in hidden])
    return {"ok": True, "message": "ستون‌های فهرست فروشگاه‌ها ذخیره شد."}


@app.get("/api/sheet-titles/info")
def get_info_sheet_titles():
    """عنوان نمایشی و وضعیت نمایش تیتر هر شیتِ اطلاعات فروشگاه‌ها."""
    return info_store.get_sheet_display_map()


@app.post("/api/sheet-titles/info")
def save_info_sheet_titles(request: dict):
    """ذخیره‌ی عنوان سفارشی/حذف تیتر شیت‌های اطلاعات فروشگاه‌ها."""
    info_store.save_sheet_display(request)
    return {"ok": True, "message": "عنوان شیت‌ها ذخیره شد."}
