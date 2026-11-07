"""
ربات بله - حالت polling.

دو حالت کار می‌کنه:
- پیوی: کد فروشگاه رو مستقیم می‌فرستی، شماره موبایلش برمی‌گرده (رفتار قدیمی، بدون تغییر).
- گروه: عضو با تایپ کلمه‌ی «شارژ» جستجو رو شروع می‌کنه، ربات کد فروشگاه رو می‌پرسه
  (اعداد فارسی یا انگلیسی، حداکثر ۴ رقم؛ فارسی خودکار به انگلیسی تبدیل میشه).
  بعد از جواب دادن، پیام «شارژ»، پیام کد و هر تلاش نامعتبر پاک می‌شن؛ فقط
  نتیجه‌ی نهایی توی گروه باقی می‌مونه.

اجرا: python -m app.bale_bot
(بهتره به‌عنوان یه سرویس systemd جدا از پنل وب اجرا بشه)
"""
import json
import re
import secrets
import threading
import time
import logging
import html
from datetime import timedelta

import jdatetime
import requests

from app import config, data_store, info_store, registration, charge_admins, reset_admins, groups, group_members, schedule_store, schedule_times, usage_stats, presence, region_experts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bale_bot")


class _SecretRedactionFilter(logging.Filter):
    """توکن بله را از پیام خطا و tracebackها قبل از ثبت در لاگ حذف می‌کند."""

    def filter(self, record: logging.LogRecord) -> bool:
        token = config.BALE_BOT_TOKEN
        if not token:
            return True
        message = record.getMessage()
        if token in message:
            record.msg = message.replace(token, "[REDACTED]")
            record.args = ()
        if record.exc_info:
            formatted = logging.Formatter().formatException(record.exc_info)
            record.exc_text = formatted.replace(token, "[REDACTED]")
            record.exc_info = None
        return True


log.addFilter(_SecretRedactionFilter())

# --- لینک دعوت به ربات ---
_BOT_USERNAME: str | None = None
_BOT_ID: int | None = None


def _fetch_bot_identity() -> None:
    """اطلاعات خود ربات (id و username) رو یه بار از API بله می‌گیره."""
    global _BOT_USERNAME, _BOT_ID
    if _BOT_USERNAME is not None and _BOT_ID is not None:
        return
    try:
        resp = requests.get(f"{config.BALE_API_BASE_URL}/getMe", timeout=10)
        data = resp.json()
        if data.get("ok"):
            _BOT_USERNAME = data["result"].get("username", "")
            _BOT_ID = data["result"].get("id")
    except Exception:
        pass


def _get_bot_username() -> str:
    """یوزرنیم ربات رو از API بله می‌گیره (فقط یه بار)."""
    _fetch_bot_identity()
    return _BOT_USERNAME or ""


def _bot_deep_link() -> str:
    """لینک دعوت به ربات بله برمی‌گردونه."""
    username = _get_bot_username()
    if username:
        return f"https://ble.ir/{username}"
    return ""


def _auto_delete_message(chat_id, message_id: int, delay_seconds: int = 10) -> None:
    """پیام رو بعد از چند ثانیه خودکار پاک می‌کنه."""
    def _delete():
        time.sleep(delay_seconds)
        delete_message(chat_id, message_id)
    threading.Thread(target=_delete, daemon=True).start()

WELCOME_TEXT = (
    "سلام 👋\n"
    "یکی از گزینه‌های زیر رو انتخاب کن:"
)

REGISTRATION_PROMPT_TEXT = "🔐 نام و نام خانوادگی‌تون رو بفرستید تا ثبت‌نام شوید:\n\n(مثال: علی محمدی)"
REGISTRATION_DONE_TEXT = "✅ ثبت‌نام شما تکمیل شد.\n⏳ درخواست شما برای ادمین ارسال شد، لطفاً منتظر تایید باشید."
NOT_APPROVED_TEXT = "⛔ برای استفاده باید احراز هویت شوید.\nاستفاده برای کاربران غیر احراز مجاز نیست.\n\nروی دکمه زیر کلیک کنید و نام و نام خانوادگی‌تون رو بفرستید."
PENDING_APPROVAL_TEXT = "⏳ ثبت‌نام شما انجام شد ولی هنوز توسط ادمین تایید نشده‌اید. لطفاً منتظر باشید."

# --- راهنما ---
HELP_TRIGGER_WORDS = ("راهنما", "کمک", "/help")
HELP_TEXT = (
    "📖 راهنمای استفاده از ربات\n"
    "\n"
    "🔹 تو پیوی (چت خصوصی) ربات:\n"
    "1️⃣ دستور /start یا کلمه‌ی «شروع» رو بفرست تا منو باز شه.\n"
    "2️⃣ یکی از گزینه‌های منو رو بزن:\n"
    "   🔍 جستجوی شارژ ← کد فروشگاه رو بفرست، شماره سیم‌کارتش میاد.\n"
    "   📋 اطلاعات فروشگاه ← کد فروشگاه رو بفرست، اطلاعاتش میاد.\n"
    "   📆 شیفت امروز ← برنامه‌ی شیفت امروز رو نشون می‌ده.\n"
    "3️⃣ برای برگشت به منو: بنویس «بازگشت»\n"
    "4️⃣ برای لغو کاری که وسطشی: بنویس «انصراف»\n"
    "\n"
    "🔹 تو گروه:\n"
    "- بنویس «شارژ» و بعدش کد فروشگاه رو بفرست.\n"
    "- بنویس «اطلاعات» و بعدش کد فروشگاه رو بفرست.\n"
    "- بنویس «شیفت» تا برنامه‌ی شیفت دیروز/امروز/فردا رو ببینی.\n"
    "- بنویس «رمز» تا درخواست ریست رمز ثبت بشه:\n"
    "   ۱️⃣ نوعش رو انتخاب کن (🏪 فروشگاه یا 🏢 ستادی).\n"
    "   ۲️⃣ اگه فروشگاه: کد فروشگاه رو بفرست (حداکثر ۴ رقم).\n"
    "   ۳️⃣ اگه ستادی: نام کاربری رو بفرست.\n"
    "   مسئول ریست رمز پیوی مطلع می‌شه و رمز پیش‌فرض جدید رو اعلام می‌کنه.\n"
    "   ⚠️ کد بیشتر از ۴ رقم یا فروشگاه بسته/جمع‌آوری‌شده پذیرفته نمی‌شه.\n"
    "\n"
    "⚠️ اگه هنوز ثبت‌نام نکردی: تو پیوی ربات نام و نام‌خانوادگیت رو بفرست و منتظر تایید ادمین بمون.\n"
    "\n"
    "❓ هر وقت یادت رفت، کافیه بنویسی «راهنما» تا این پیام دوباره بیاد."
)

# --- شارژ (گروه) ---
GROUP_TRIGGER_WORD = "شارژ"
GROUP_PROMPT_TEXT = "🔍 کد فروشگاه رو تایپ کن تا شماره موبایلش رو پیدا کنم (حداکثر ۴ رقم)."
GROUP_INVALID_TEXT = "❗ فقط عدد و حداکثر ۴ رقم مجازه. دوباره امتحان کن."
GROUP_PENDING_TIMEOUT_SECONDS = 180

# --- اطلاعات فروشگاه (گروه) ---
INFO_TRIGGER_WORD = "اطلاعات"
INFO_PROMPT_TEXT = "📋 کد فروشگاه رو تایپ کن تا اطلاعاتش رو بهت بدم (حداکثر ۴ رقم)."
INFO_INVALID_TEXT = "❗ فقط عدد و حداکثر ۴ رقم مجازه. دوباره امتحان کن."
INFO_PENDING_TIMEOUT_SECONDS = 180

CODE_PATTERN = re.compile(r"^[0-9]{1,4}$")
GROUP_CHAT_TYPES = {"group", "supergroup"}

# --- منو و state management پیوی ---
PRIVATE_MENU_TEXT = "یکی از گزینه‌های زیر رو انتخاب کن:"
PRIVATE_PROMPT_CHARGE = "🔍 کد فروشگاه رو بفرست تا شماره موبایلش رو پیدا کنم (حداکثر ۴ رقم)."
PRIVATE_PROMPT_INFO = "📋 کد فروشگاه رو بفرست تا اطلاعاتش رو بهت بدم (حداکثر ۴ رقم)."
PRIVATE_BACK_TEXT = "🔙 برگشت به منوی اصلی."
PRIVATE_INVALID_TEXT = "❗ فقط عدد و حداکثر ۴ رقم مجازه. دوباره امتحان کن."
PRIVATE_NOT_FOUND_TEXT = "کد «{}» توی فایل پیدا نشد. لطفاً کد رو دوباره چک کن."

# state پیوی هر کاربر: "menu" | "charge" | "info"
_private_state: dict[int, str] = {}

# وضعیت «منتظر کد فروشگاه» برای هر (chat_id, user_id) توی گروه
# مقدار: {"trigger_id", "prompt_id", "extra_ids": [...], "created_at", "display_name"}
_pending: dict[tuple, dict] = {}

# user_id هایی که پرامپت «نام و نام خانوادگی» براشون فرستاده شده و منتظر جوابشونیم
_pending_registration: set = set()

# وضعیت «منتظر کد فروشگاه» برای جستجوی اطلاعات در گروه
# کلید: (chat_id, user_id) — مقدار: {"trigger_id", "prompt_id", "extra_ids": [...], "created_at", "display_name"}
_pending_info: dict[tuple, dict] = {}

CHARGE_BUTTON_NOISSUE = "🔋 شارژ داره"
CHARGE_BUTTON_DONE = "✅ شارژ انجام شد"

# درخواست‌های شارژ درحال‌انتظار برای تایید ادمین، کلید: request_id
# مقدار: {"group_chat_id", "answer_message_id", "code", "requester_name", "resolved",
#          "admin_message_ids" (admin_id -> [message_id, ...]), "last_reminded_at"}
_charge_requests: dict[str, dict] = {}

# درخواست‌های شارژ توی یه فایل JSON ماندگار می‌شن تا با ری‌استارت ربات از بین نرن.
_charge_requests_path = config.DATA_DIR / "charge_requests.json"


def _save_charge_requests() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _charge_requests_path.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_charge_requests, f, ensure_ascii=False)
        tmp.replace(_charge_requests_path)
    except Exception as e:
        log.warning("ذخیره‌ی درخواست‌های شارژ ناموفق بود: %s", e)


def _load_charge_requests() -> None:
    global _charge_requests
    if not _charge_requests_path.exists():
        return
    try:
        with open(_charge_requests_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        log.warning("فایل درخواست‌های شارژ دوباره خوانده نشد.")
        return
    if not isinstance(data, dict):
        return
    # درخواست‌های باز همیشه نگه داشته می‌شن؛ درخواست‌های بسته‌شده هم تا ۷ روز.
    now = time.time()
    _charge_requests = {
        rid: rec
        for rid, rec in data.items()
        if not rec.get("resolved") or rec.get("created_at", 0) >= now - 7 * 86400
    }


_load_charge_requests()

# اگه بعد از این مدت هنوز کسی به درخواست شارژ جواب نداده باشه، یادآوری فرستاده می‌شه
# (و هر بار که دوباره این مدت بگذره، دوباره یادآوری می‌ره، تا وقتی resolve بشه)
CHARGE_REQUEST_REMINDER_SECONDS = 1800

# --- ریست رمز (گروه) ---
RESET_TRIGGER_WORD = "رمز"
RESET_PROMPT_TEXT = "🔑 درخواست ریست رمز\nبرای چه موردیه؟ یکی از دکمه‌ها رو بزن:"
RESET_STORE_PROMPT_TEXT = "🏪 کد فروشگاه رو تایپ کن تا ریست رمزش ثبت بشه (حداکثر ۴ رقم)."
RESET_HQ_PROMPT_TEXT = "🏢 نام کاربری ستادی رو وارد کن تا ریست رمزش ثبت بشه."
RESET_PENDING_TIMEOUT_SECONDS = 180
RESET_BUTTON_STORE = "🏪 فروشگاه"
RESET_BUTTON_HQ = "🏢 ستادی"
RESET_BUTTON_DONE = "✅ رمز ریست شد"
RESET_BUTTON_WRONG = "❌ مشخصات اشتباه است"
RESET_BUTTON_RETRY = "🔄 ارسال مجدد"
RESET_KIND_LABELS = {"store": "فروشگاه", "hq": "ستادی"}
# رمز پیش‌فرضی که در پیام تأیید ریست نمایش داده می‌شود (در صورت نیاز قابل تغییر):
RESET_DEFAULT_PASSWORD_STORE = "12345678"   # فروشگاه
RESET_DEFAULT_PASSWORD_HQ = "123456789"     # ستادی

# وضعیت «درخواست ریست رمز» برای هر (chat_id, user_id) توی گروه.
# کلید: (chat_id, user_id) — مقدار: {"trigger_id", "prompt_id", "extra_ids": [...],
#         "created_at", "display_name", "step": "choice" | "store" | "hq"}
_pending_reset: dict[tuple, dict] = {}

# درخواست‌های ریست رمز درحال‌انتظار برای تایید مسئول، کلید: request_id
# مقدار: {"group_chat_id", "answer_message_id", "kind", "target", "requester_name",
#          "resolved", "admin_message_ids" (admin_id -> [message_id, ...]), "last_reminded_at", "created_at"}
_reset_requests: dict[str, dict] = {}

# درخواست‌های ریست رمز توی یه فایل JSON ماندگار می‌شن تا با ری‌استارت ربات
# از بین نرن و چک درخواست تکراری هم سر جاش بمونه.
_reset_requests_path = config.DATA_DIR / "reset_requests.json"


def _save_reset_requests() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _reset_requests_path.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_reset_requests, f, ensure_ascii=False)
        tmp.replace(_reset_requests_path)
    except Exception as e:
        log.warning("ذخیره‌ی درخواست‌های ریست رمز ناموفق بود: %s", e)


def _load_reset_requests() -> None:
    global _reset_requests
    if not _reset_requests_path.exists():
        return
    try:
        with open(_reset_requests_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        log.warning("فایل درخواست‌های ریست رمز دوباره خوانده نشد.")
        return
    if not isinstance(data, dict):
        return
    # درخواست‌های باز همیشه نگه داشته می‌شن؛ درخواست‌های بسته‌شده هم تا ۷ روز.
    now = time.time()
    _reset_requests = {
        rid: rec
        for rid, rec in data.items()
        if not rec.get("resolved") or rec.get("created_at", 0) >= now - 7 * 86400
    }


_load_reset_requests()

# اگه بعد از این مدت هنوز کسی به درخواست ریست رمز جواب نداده باشه، یادآوری فرستاده می‌شه
RESET_REQUEST_REMINDER_SECONDS = 1800

# --- اطلاع‌رسانی روزانه‌ی شیفت فردا ---
# ساعت‌های ارسال از پنل (/schedule-times) مدیریت می‌شن؛ پیش‌فرض 15:00 و 23:00.
SCHEDULE_GROUP_TITLE_HINT = "انفورماتیک"
SCHEDULE_STATE_PATH = config.DATA_DIR / "schedule_announce_state.json"
GROUP_SHIFT_TRIGGER_WORD = "شیفت"
PERSIAN_WEEKDAYS = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه"]

# اعلام فروشگاه‌های بدون کارشناس (شامل کارشناس‌های منفک مثل شیری)
GROUP_NOEXPERT_TRIGGER_WORDS = ("بدون کارشناس", "بی کارشناس")

# --- اطلاعات فروشگاه ---
INFO_TRIGGER_WORD = "اطلاعات"
INFO_PROMPT_TEXT = "کد فروشگاه رو تایپ کن (حداکثر ۴ رقم)."
INFO_INVALID_TEXT = "❗ فقط عدد و حداکثر ۴ رقم مجازه. دوباره امتحان کن."
INFO_PENDING_TIMEOUT_SECONDS = 180


def _jalali_day_name(d: jdatetime.date) -> str:
    return PERSIAN_WEEKDAYS[d.weekday()]

_sent_schedule_slots: set = set()


def _load_sent_schedule_slots() -> None:
    global _sent_schedule_slots
    if SCHEDULE_STATE_PATH.exists():
        try:
            with open(SCHEDULE_STATE_PATH, encoding="utf-8") as f:
                _sent_schedule_slots = set(json.load(f))
        except Exception:
            _sent_schedule_slots = set()


def _save_sent_schedule_slots() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(SCHEDULE_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(_sent_schedule_slots), f, ensure_ascii=False, indent=2)


_load_sent_schedule_slots()


def _normalize_yeh_kaf(text: str) -> str:
    """حروف عربی ي/ك رو به معادل فارسی ی/ک تبدیل می‌کنه تا تشخیص کلمه‌ی «شارژ» مستقل از کیبورد باشه."""
    return text.replace("ي", "ی").replace("ك", "ک")


def _normalize_code_input(text: str) -> str:
    """اعداد فارسی/عربی رو به انگلیسی تبدیل می‌کنه (برای پذیرفتن کد فروشگاه با هر کیبوردی)."""
    return data_store._normalize_code(text)


def _store_is_collected(code: str) -> bool:
    """فروشگاهی که «جمع‌آوری شده» یا بسته باشه، از توی داده‌ها قابل تشخیصه.
    برای همچین فروشگاه‌هایی درخواست ریست رمز قبول نمی‌شه و به کاربر اطلاع می‌دیم."""
    results = data_store.lookup(code)
    if not results:
        return False
    for rec in results:
        text = " ".join(str(v) for v in rec.values() if isinstance(v, str))
        if "جمع آوری" in text or "جمع‌آوری" in text:
            return True
    return False


def get_updates(offset: int | None = None, timeout: int = 30) -> list:
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    try:
        resp = requests.get(
            f"{config.BALE_API_BASE_URL}/getUpdates",
            params=params,
            timeout=timeout + 10,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            log.error("getUpdates پاسخ ناموفق داد: %s", data)
            return []
        return data.get("result", [])
    except requests.RequestException as e:
        log.error("خطا در getUpdates: %s", e)
        return []


def send_message(
    chat_id,
    text: str,
    reply_to_message_id: int | None = None,
    parse_mode: str | None = None,
    reply_markup: dict | None = None,
) -> int | None:
    """پیام رو می‌فرسته و در صورت موفقیت، message_id رو برمی‌گردونه (برای پاک کردن بعدی لازمه)."""
    payload = {"chat_id": chat_id, "text": text}
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(
            f"{config.BALE_API_BASE_URL}/sendMessage",
            json=payload,
            timeout=15,
        )
        if resp.status_code != 200:
            log.error("خطا در ارسال پیام: %s - %s", resp.status_code, resp.text)
            return None
        data = resp.json()
        return data.get("result", {}).get("message_id")
    except requests.RequestException as e:
        log.error("خطا در sendMessage: %s", e)
        return None


# کش کوتاه‌مدت تعداد واقعی اعضای گروه، تا هر بار باز شدن پنل درخواست جدید به API بله نزنه
_chat_member_count_cache: dict[str, tuple[int, float]] = {}
_CHAT_MEMBER_COUNT_TTL = 30  # ثانیه


def get_chat_member_count(chat_id) -> int | None:
    """تعداد واقعی اعضای انسانی گروه رو از API بله می‌گیره.
    توجه: getChatMemberCount خود ربات رو هم به‌عنوان عضو حساب می‌کنه،
    پس برای «تعداد افراد» باید ۱ (خود ربات) کم بشه تا آمار «روح» غلط نشه."""
    key = str(chat_id)
    cached = _chat_member_count_cache.get(key)
    if cached and time.time() - cached[1] < _CHAT_MEMBER_COUNT_TTL:
        return cached[0]
    try:
        resp = requests.get(
            f"{config.BALE_API_BASE_URL}/getChatMemberCount",
            params={"chat_id": chat_id},
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            count = data.get("result")
            if isinstance(count, int):
                # کم‌کردن خود ربات از شمار اعضا (ربات عضو گروهه)
                count = max(count - 1, 0)
            _chat_member_count_cache[key] = (count, time.time())
            return count
        log.error("خطا در getChatMemberCount: %s", data)
    except requests.RequestException as e:
        log.error("خطا در getChatMemberCount: %s", e)
    return cached[0] if cached else None


def answer_callback_query(callback_query_id: str, text: str | None = None) -> None:
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        resp = requests.post(
            f"{config.BALE_API_BASE_URL}/answerCallbackQuery",
            json=payload,
            timeout=15,
        )
        if resp.status_code != 200:
            log.error("خطا در answerCallbackQuery: %s - %s", resp.status_code, resp.text)
    except requests.RequestException as e:
        log.error("خطا در answerCallbackQuery: %s", e)


def clear_message_buttons(chat_id, message_id: int | None) -> None:
    """دکمه‌های شیشه‌ای یه پیام رو حذف می‌کنه (بعد از اینکه یه ادمین جواب داد)."""
    if message_id is None:
        return
    payload = {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}}
    try:
        resp = requests.post(
            f"{config.BALE_API_BASE_URL}/editMessageReplyMarkup",
            json=payload,
            timeout=15,
        )
        if resp.status_code != 200:
            log.error("خطا در حذف دکمه‌های پیام %s: %s - %s", message_id, resp.status_code, resp.text)
    except requests.RequestException as e:
        log.error("خطا در editMessageReplyMarkup: %s", e)


def delete_message(chat_id, message_id: int | None) -> None:
    if message_id is None:
        return
    try:
        resp = requests.post(
            f"{config.BALE_API_BASE_URL}/deleteMessage",
            json={"chat_id": chat_id, "message_id": message_id},
            timeout=15,
        )
        if resp.status_code != 200:
            log.error("خطا در پاک کردن پیام %s: %s - %s", message_id, resp.status_code, resp.text)
    except requests.RequestException as e:
        log.error("خطا در deleteMessage: %s", e)


_MOBILE_98_RE = re.compile(r"^\+?98(\d{10})$")


def _format_mobile(mobile: str) -> str:
    """پیشوند +98/98 رو با 0 عوض می‌کنه (مثلاً 989123456789 -> 09123456789)."""
    m = _MOBILE_98_RE.match((mobile or "").strip())
    return "0" + m.group(1) if m else mobile


CORE_FIELDS = ("source", "code", "mobile", "operator", "location")
# محدودیت‌های نمایش جواب (پیام بله نباید خیلی بلند بشه)
MAX_REPLY_RECORDS = 6
MAX_REPLY_FIELDS = 18
MAX_REPLY_CHARS = 3500
# مقادیری که یعنی «دیتایی وجود نداره» و نباید توی جواب بیاد
EMPTY_VALUES = {"ندارد", "نامشخص", "-", "—", "–", "N/A", "NA", "N.A", "NULL", "NONE", "منصوب", "0", "0.0"}


def _record_lines(record: dict) -> list[str]:
    """خط‌های جواب: فیلدهای اصلی + همه‌ی فیلدهایی که توی اون شیت برای این رکورد دیتا دارن."""
    lines = [f"📌 کد فروشگاه: {record.get('code', '')}"]

    if record.get("mobile"):
        lines.append(f"📞 شماره سیم‌کارت: {_format_mobile(record['mobile'])}")
    if record.get("operator"):
        lines.append(f"📡 اپراتور: {record['operator']}")
    if record.get("location"):
        lines.append(f"📍 محل نصب: {record['location']}")

    source = record.get("source", "")
    all_cols = data_store.get_source_columns(source) or [k for k in record if k not in CORE_FIELDS]
    saved_order = data_store.get_visible_columns(source)
    hidden = data_store.get_hidden_columns(source)
    labels = data_store.get_field_labels(source)

    extras = [c for c in all_cols if c not in CORE_FIELDS]
    # ستون‌هایی که همون اطلاعات فیلدهای اصلی رو دارن دوباره نشون داده نمی‌شن
    promoted = set()
    for key in ("mobile", "operator", "location"):
        value = record.get(key)
        if value:
            for col in extras:
                if record.get(col) == value:
                    promoted.add(col)
                    break

    ordered = [c for c in saved_order if c in extras] + [c for c in extras if c not in saved_order]

    shown = 0
    for col in ordered:
        if col in hidden or col in promoted:
            continue
        value = str(record.get(col) or "").strip()
        if not value or value.upper() in EMPTY_VALUES:
            continue
        lines.append(f"  {labels.get(col, col)}: {value}")
        shown += 1
        if shown >= MAX_REPLY_FIELDS:
            lines.append("  … (بقیه‌ی فیلدها توی پنل › نمایش کامل دیتا)")
            break

    return lines


def format_reply(code: str, results: list[dict] | None) -> str:
    all_results = results or []
    if not all_results:
        return f"❌ کد «{code}» توی فایل پیدا نشد."

    results = all_results[:MAX_REPLY_RECORDS]
    if len(results) == 1:
        text = "\n".join(_record_lines(results[0]))
    else:
        lines = [f"⚠️ این کد توی {len(all_results)} مورد پیدا شد:"]
        for i, record in enumerate(results, 1):
            lines.append("")
            lines.append(f"{i}. {record.get('source', '')}")
            lines.extend(_record_lines(record))
        text = "\n".join(lines)

    if len(all_results) > len(results):
        text += f"\n… {len(all_results) - len(results)} مورد دیگه هست (توی پنل › نمایش کامل دیتا)"

    if len(text) > MAX_REPLY_CHARS:
        text = text[:MAX_REPLY_CHARS].rsplit("\n", 1)[0] + "\n… (پیام طولانی شد؛ بقیه توی پنل › نمایش کامل دیتا)"
    return text


def format_group_reply(display_name: str, code: str, results: list[dict] | None) -> str:
    header = f"👤 {display_name} درخواست بررسی شارژ فروشگاه زیر رو داره:"

    if not results:
        return header + f"\n\n❌ کد «{code}» توی فایل پیدا نشد."

    return header + "\n\n" + format_reply(code, results)


def _display_name(from_user: dict) -> str:
    return (
        from_user.get("first_name")
        or from_user.get("username")
        or "کاربر"
    )


def _send_private_menu(chat_id: int) -> None:
    """منوی اصلی پیوی رو با دکمه‌های شیشه‌ای می‌فرسته."""
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "🔍 جستجوی شارژ", "callback_data": "pm:charge"},
                {"text": "📋 اطلاعات فروشگاه", "callback_data": "pm:info"},
            ],
            [
                {"text": "📆 شیفت امروز", "callback_data": "pm:shift"},
            ],
            [
                {"text": "❓ راهنما", "callback_data": "pm:help"},
            ],
        ]
    }
    send_message(chat_id, PRIVATE_MENU_TEXT, reply_markup=keyboard)


def _send_private_code_prompt(chat_id: int, mode: str) -> None:
    """وقتی کاربر حالت شارژ یا اطلاعات رو انتخاب کرده، پرامپت کد رو با دکمه بازگشت/انصراف می‌فرسته."""
    if mode == "charge":
        prompt = PRIVATE_PROMPT_CHARGE
    else:
        prompt = PRIVATE_PROMPT_INFO

    keyboard = {
        "inline_keyboard": [
            [
                {"text": "🔙 بازگشت به منو", "callback_data": "pm:back"},
                {"text": "❌ انصراف", "callback_data": "pm:cancel"},
            ],
        ]
    }
    send_message(chat_id, prompt, reply_markup=keyboard)


def _cleanup_pending(pending_key: tuple) -> dict | None:
    """اگه وضعیت منتظر کد بودن منقضی شده باشه حذفش می‌کنه و None برمی‌گردونه."""
    for store in (_pending, _pending_info):
        entry = store.get(pending_key)
        if entry is None:
            continue
        if time.time() - entry["created_at"] > GROUP_PENDING_TIMEOUT_SECONDS:
            store.pop(pending_key, None)
            return None
        return entry
    return None


def handle_group_message(message: dict) -> None:
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    message_id = message["message_id"]
    text = (message.get("text") or "").strip()
    pending_key = (chat_id, user_id)

    # بررسی وضعیت‌های درحال‌انتظار (اطلاعات و شارژ)
    info_entry = _pending_info.get(pending_key)
    charge_entry = _pending.get(pending_key)

    # --- اگه منتظر کد فروشگاه برای اطلاعات هستیم ---
    if info_entry is not None:
        if time.time() - info_entry["created_at"] > GROUP_PENDING_TIMEOUT_SECONDS:
            _pending_info.pop(pending_key, None)
        else:
            # لغو یا بازگشت
            normalized_text = _normalize_yeh_kaf(text)
            if normalized_text in ("انصراف", "لغو", "بازگشت", "برگشت"):
                log.info("لغو جستجوی اطلاعات توسط %s در چت %s", info_entry["display_name"], chat_id)
                delete_message(chat_id, info_entry["trigger_id"])
                delete_message(chat_id, info_entry["prompt_id"])
                for extra_id in info_entry["extra_ids"]:
                    delete_message(chat_id, extra_id)
                delete_message(chat_id, message_id)
                _pending_info.pop(pending_key, None)
                return

            normalized_code = _normalize_code_input(text)
            if CODE_PATTERN.fullmatch(normalized_code):
                record = info_store.lookup_info(normalized_code)
                log.info(
                    "جستجوی اطلاعات کد «%s» از %s در چت %s -> %s",
                    normalized_code, info_entry["display_name"], chat_id,
                    "یافت شد" if record else "پیدا نشد",
                )
                if record:
                    reply_text = (
                        f"👤 درخواست: {info_entry['display_name']}\n"
                        + info_store.format_info_reply(normalized_code, record)
                    )
                else:
                    reply_text = f"❌ کد «{normalized_code}» توی فایل اطلاعات فروشگاه‌ها پیدا نشد."
                send_message(chat_id, reply_text)
                usage_stats.record_action(user_id, "info")
                usage_stats.record_code_lookup(normalized_code, "info")

                delete_message(chat_id, info_entry["trigger_id"])
                delete_message(chat_id, info_entry["prompt_id"])
                for extra_id in info_entry["extra_ids"]:
                    delete_message(chat_id, extra_id)
                delete_message(chat_id, message_id)

                _pending_info.pop(pending_key, None)
                return

            # فرمت نامعتبر
            log.info("ورودی نامعتبر «%s» از %s در چت %s (اطلاعات)", text, info_entry["display_name"], chat_id)
            error_id = send_message(chat_id, INFO_INVALID_TEXT, reply_to_message_id=message_id)
            info_entry["extra_ids"].append(message_id)
            if error_id is not None:
                info_entry["extra_ids"].append(error_id)
            return

    # --- اگه منتظر کد فروشگاه برای شارژ هستیم ---
    if charge_entry is not None:
        if time.time() - charge_entry["created_at"] > GROUP_PENDING_TIMEOUT_SECONDS:
            _pending.pop(pending_key, None)
        else:
            # لغو یا بازگشت
            normalized_text = _normalize_yeh_kaf(text)
            if normalized_text in ("انصراف", "لغو", "بازگشت", "برگشت"):
                log.info("لغو جستجوی شارژ توسط %s در چت %s", charge_entry["display_name"], chat_id)
                delete_message(chat_id, charge_entry["trigger_id"])
                delete_message(chat_id, charge_entry["prompt_id"])
                for extra_id in charge_entry["extra_ids"]:
                    delete_message(chat_id, extra_id)
                delete_message(chat_id, message_id)
                _pending.pop(pending_key, None)
                return

            normalized_code = _normalize_code_input(text)
            if CODE_PATTERN.fullmatch(normalized_code):
                results = data_store.lookup(normalized_code)
                log.info(
                    "جستجوی کد «%s» از %s در چت %s -> %s مورد",
                    normalized_code, charge_entry["display_name"], chat_id, len(results) if results else 0,
                )
                reply_text = format_group_reply(charge_entry["display_name"], normalized_code, results)
                answer_message_id = send_message(chat_id, reply_text)
                usage_stats.record_action(user_id, "charge")
                usage_stats.record_code_lookup(normalized_code, "charge")

                if results:
                    _notify_charge_admins(chat_id, answer_message_id, normalized_code, charge_entry["display_name"], results)

                delete_message(chat_id, charge_entry["trigger_id"])
                delete_message(chat_id, charge_entry["prompt_id"])
                for extra_id in charge_entry["extra_ids"]:
                    delete_message(chat_id, extra_id)
                delete_message(chat_id, message_id)

                _pending.pop(pending_key, None)
                return

            # فرمت نامعتبر
            log.info("ورودی نامعتبر «%s» از %s در چت %s", text, charge_entry["display_name"], chat_id)
            error_id = send_message(chat_id, GROUP_INVALID_TEXT, reply_to_message_id=message_id)
            charge_entry["extra_ids"].append(message_id)
            if error_id is not None:
                charge_entry["extra_ids"].append(error_id)
            return

    # --- اگه در حال ثبت درخواست ریست رمز هستیم ---
    reset_entry = _pending_reset.get(pending_key)
    if reset_entry is not None:
        if time.time() - reset_entry["created_at"] > RESET_PENDING_TIMEOUT_SECONDS:
            _pending_reset.pop(pending_key, None)
        else:
            # لغو یا بازگشت
            normalized_text = _normalize_yeh_kaf(text)
            if normalized_text in ("انصراف", "لغو", "بازگشت", "برگشت"):
                log.info("لغو درخواست ریست رمز توسط %s در چت %s", reset_entry["display_name"], chat_id)
                delete_message(chat_id, reset_entry["trigger_id"])
                delete_message(chat_id, reset_entry["prompt_id"])
                for extra_id in reset_entry["extra_ids"]:
                    delete_message(chat_id, extra_id)
                delete_message(chat_id, message_id)
                _pending_reset.pop(pending_key, None)
                return

            step = reset_entry.get("step")

            # کاربر به‌جای دکمه، متن فرستاده — دوباره انتخاب نوع رو نشون بده
            if step == "choice":
                choice_keyboard = {
                    "inline_keyboard": [[
                        {"text": RESET_BUTTON_STORE, "callback_data": "reset:store"},
                        {"text": RESET_BUTTON_HQ, "callback_data": "reset:hq"},
                    ]]
                }
                prompt_id = send_message(chat_id, RESET_PROMPT_TEXT, reply_to_message_id=message_id, reply_markup=choice_keyboard)
                reset_entry["extra_ids"].append(message_id)
                if prompt_id is not None:
                    reset_entry["extra_ids"].append(prompt_id)
                return

            if step == "store":
                normalized_code = _normalize_code_input(text)
                if not CODE_PATTERN.fullmatch(normalized_code):
                    log.info("ورودی نامعتبر «%s» از %s در چت %s (ریست رمز)", text, reset_entry["display_name"], chat_id)
                    error_text = (
                        f"❗ {reset_entry['display_name']} عزیز، کد فروشگاه فقط عدد و حداکثر ۴ رقمی‌ه.\n"
                        "چیزی که فرستادی اشتباه هست، دوباره امتحان کن."
                    )
                    error_id = send_message(chat_id, error_text, reply_to_message_id=message_id)
                    reset_entry["extra_ids"].append(message_id)
                    if error_id is not None:
                        reset_entry["extra_ids"].append(error_id)
                    return
                if _store_is_collected(normalized_code):
                    log.info("فروشگاه جمع‌آوری‌شده «%s» (از طرف %s) — درخواست ریست رد شد", normalized_code, reset_entry["display_name"])
                    reply_text = (
                        f"❌ {reset_entry['display_name']} عزیز، فروشگاه {normalized_code} جمع آوری شده است.\n"
                        f"مشخصاتش اشتباه هست، لطفاً کد درست رو بفرست."
                    )
                    retry_keyboard = {
                        "inline_keyboard": [[
                            {"text": RESET_BUTTON_RETRY, "callback_data": f"reset:retry:{user_id}"},
                        ]]
                    }
                    send_message(chat_id, reply_text, reply_to_message_id=message_id, reply_markup=retry_keyboard)
                    delete_message(chat_id, reset_entry["trigger_id"])
                    delete_message(chat_id, reset_entry["prompt_id"])
                    for extra_id in reset_entry["extra_ids"]:
                        delete_message(chat_id, extra_id)
                    delete_message(chat_id, message_id)
                    _pending_reset.pop(pending_key, None)
                    return
                kind, target = "store", normalized_code
            else:  # step == "hq"
                if not text.strip():
                    return
                kind, target = "hq", text.strip()

            # درخواست قبلی برای همین فروشگاه/کاربر هنوز رسیدگی نشده — درخواست جدید ثبت نشه
            if _find_unresolved_reset_request(chat_id, kind, target) is not None:
                log.info(
                    "درخواست تکراری ریست رمز %s «%s» از %s در چت %s رد شد (درخواست قبلی هنوز باز است)",
                    kind, target, reset_entry["display_name"], chat_id,
                )
                dup_text = (
                    f"⏳ {reset_entry['display_name']} عزیز، درخواست قبلی ریست رمز "
                    f"{RESET_KIND_LABELS[kind]} {target} هنوز رسیدگی نشده."
                )
                send_message(chat_id, dup_text, reply_to_message_id=message_id)
                delete_message(chat_id, reset_entry["trigger_id"])
                delete_message(chat_id, reset_entry["prompt_id"])
                for extra_id in reset_entry["extra_ids"]:
                    delete_message(chat_id, extra_id)
                delete_message(chat_id, message_id)
                _pending_reset.pop(pending_key, None)
                return

            log.info("ریست رمز %s «%s» از %s در چت %s", kind, target, reset_entry["display_name"], chat_id)
            answer_text = _reset_request_card(reset_entry["display_name"], kind, target)
            answer_message_id = send_message(chat_id, answer_text)
            usage_stats.record_action(user_id, "reset")
            if kind == "store":
                usage_stats.record_code_lookup(normalized_code, "reset")
            _notify_reset_admins(chat_id, answer_message_id, kind, target, reset_entry["display_name"], user_id)

            delete_message(chat_id, reset_entry["trigger_id"])
            delete_message(chat_id, reset_entry["prompt_id"])
            for extra_id in reset_entry["extra_ids"]:
                delete_message(chat_id, extra_id)
            delete_message(chat_id, message_id)

            _pending_reset.pop(pending_key, None)
            return

    # --- هیچ جستجوی درحال‌انتظاری نیست؛ کلمه‌های محرک رو بررسی کن ---
    normalized_text = _normalize_yeh_kaf(text)

    if normalized_text in HELP_TRIGGER_WORDS:
        send_message(chat_id, HELP_TEXT, reply_to_message_id=message_id)
        return

    if normalized_text == INFO_TRIGGER_WORD:
        if not registration.is_registered(user_id) or not registration.is_approved(user_id):
            link = _bot_deep_link()
            keyboard = {"inline_keyboard": [[{"text": "🔐 احراز هویت کنید", "url": link}]]} if link else None
            msg_id = send_message(chat_id, NOT_APPROVED_TEXT, reply_to_message_id=message_id, reply_markup=keyboard)
            if msg_id:
                _auto_delete_message(chat_id, msg_id, 15)
                _auto_delete_message(chat_id, message_id, 15)
            return
        display_name = _display_name(message["from"])
        log.info("تریگر «اطلاعات» از %s در چت %s", display_name, chat_id)
        prompt_id = send_message(chat_id, INFO_PROMPT_TEXT, reply_to_message_id=message_id)
        _pending_info[pending_key] = {
            "trigger_id": message_id,
            "prompt_id": prompt_id,
            "extra_ids": [],
            "created_at": time.time(),
            "display_name": display_name,
        }
    elif normalized_text == GROUP_TRIGGER_WORD:
        if not registration.is_registered(user_id) or not registration.is_approved(user_id):
            link = _bot_deep_link()
            keyboard = {"inline_keyboard": [[{"text": "🔐 احراز هویت کنید", "url": link}]]} if link else None
            msg_id = send_message(chat_id, NOT_APPROVED_TEXT, reply_to_message_id=message_id, reply_markup=keyboard)
            if msg_id:
                _auto_delete_message(chat_id, msg_id, 15)
                _auto_delete_message(chat_id, message_id, 15)
            return
        display_name = _display_name(message["from"])
        log.info("تریگر «شارژ» از %s در چت %s", display_name, chat_id)
        prompt_id = send_message(chat_id, GROUP_PROMPT_TEXT, reply_to_message_id=message_id)
        _pending[pending_key] = {
            "trigger_id": message_id,
            "prompt_id": prompt_id,
            "extra_ids": [],
            "created_at": time.time(),
            "display_name": display_name,
        }
    elif normalized_text == RESET_TRIGGER_WORD:
        if not registration.is_registered(user_id) or not registration.is_approved(user_id):
            link = _bot_deep_link()
            keyboard = {"inline_keyboard": [[{"text": "🔐 احراز هویت کنید", "url": link}]]} if link else None
            msg_id = send_message(chat_id, NOT_APPROVED_TEXT, reply_to_message_id=message_id, reply_markup=keyboard)
            if msg_id:
                _auto_delete_message(chat_id, msg_id, 15)
                _auto_delete_message(chat_id, message_id, 15)
            return
        display_name = _display_name(message["from"])
        log.info("تریگر «رمز» از %s در چت %s", display_name, chat_id)
        choice_keyboard = {
            "inline_keyboard": [[
                {"text": RESET_BUTTON_STORE, "callback_data": "reset:store"},
                {"text": RESET_BUTTON_HQ, "callback_data": "reset:hq"},
            ]]
        }
        prompt_id = send_message(chat_id, RESET_PROMPT_TEXT, reply_to_message_id=message_id, reply_markup=choice_keyboard)
        _pending_reset[pending_key] = {
            "trigger_id": message_id,
            "prompt_id": prompt_id,
            "extra_ids": [],
            "created_at": time.time(),
            "display_name": display_name,
            "step": "choice",
        }
    elif normalized_text == GROUP_SHIFT_TRIGGER_WORD:
        if not registration.is_registered(user_id) or not registration.is_approved(user_id):
            link = _bot_deep_link()
            keyboard = {"inline_keyboard": [[{"text": "🔐 احراز هویت کنید", "url": link}]]} if link else None
            msg_id = send_message(chat_id, NOT_APPROVED_TEXT, reply_to_message_id=message_id, reply_markup=keyboard)
            if msg_id:
                _auto_delete_message(chat_id, msg_id, 15)
                _auto_delete_message(chat_id, message_id, 15)
            return
        log.info("درخواست «شیفت» از %s در چت %s", _display_name(message["from"]), chat_id)
        today = jdatetime.date.today()
        yesterday = today - timedelta(days=1)
        tomorrow = today + timedelta(days=1)

        def _format_one(day_label: str, d: jdatetime.date, emoji: str) -> str:
            date_str = d.strftime("%Y/%m/%d")
            name = _jalali_day_name(d)
            record = schedule_store.lookup(date_str)
            if record is None:
                return f"{emoji} {day_label} ({name} {date_str})\n❌ یافت نشد"
            lines = [f"{emoji} {day_label} ({name} {date_str})"]
            lines.extend("  " + ln for ln in _shift_lines(record))
            return "\n".join(lines)

        parts = [
            f"📅 برنامه شیفت",
            f"درخواست: {_display_name(message['from'])}",
            "",
            _format_one("دیروز", yesterday, "⬅️"),
            "",
            _format_one("امروز", today, "🔴"),
            "",
            _format_one("فردا", tomorrow, "➡️"),
        ]
        send_message(chat_id, "\n".join(parts), reply_to_message_id=message_id)
        usage_stats.record_action(user_id, "shift")

    elif normalized_text in GROUP_NOEXPERT_TRIGGER_WORDS:
        if not registration.is_registered(user_id) or not registration.is_approved(user_id):
            link = _bot_deep_link()
            keyboard = {"inline_keyboard": [[{"text": "🔐 احراز هویت کنید", "url": link}]]} if link else None
            msg_id = send_message(chat_id, NOT_APPROVED_TEXT, reply_to_message_id=message_id, reply_markup=keyboard)
            if msg_id:
                _auto_delete_message(chat_id, msg_id, 15)
                _auto_delete_message(chat_id, message_id, 15)
            return
        display_name = _display_name(message["from"])
        log.info("درخواست «بدون کارشناس» از %s در چت %s", display_name, chat_id)
        group_title = message["chat"].get("title") or "انفورماتیک اوشانک"
        for chunk in region_experts._split(region_experts.render_unassigned(group_title=group_title)):
            send_message(chat_id, chunk)


def _notify_charge_admins(group_chat_id, answer_message_id: int | None, code: str, requester_name: str, results: list[dict] | None) -> None:
    admin_ids = charge_admins.get_ids()
    if not admin_ids:
        log.warning("هیچ مسئول شارژی توی پنل ثبت نشده؛ درخواست شارژ برای کسی ارسال نشد.")
        return

    request_id = secrets.token_hex(4)
    _charge_requests[request_id] = {
        "group_chat_id": group_chat_id,
        "answer_message_id": answer_message_id,
        "code": code,
        "requester_name": requester_name,
        "resolved": False,
        "admin_message_ids": {},
        "group_reminder_ids": [],
        "last_reminded_at": time.time(),
        "created_at": time.time(),
    }

    mobiles: list[str] = []
    for record in results or []:
        mobile = record.get("mobile")
        if mobile:
            formatted = _format_mobile(mobile)
            if formatted and formatted not in mobiles:
                mobiles.append(formatted)

    notify_text = f"شارژ فروشگاه {code} رو انجام بده"
    if mobiles:
        phone_lines = "\n".join(f"📞 شماره سیم‌کارت: {m}" for m in mobiles)
        notify_text += f"\n\n{phone_lines}"
    keyboard = {
        "inline_keyboard": [[
            {"text": CHARGE_BUTTON_NOISSUE, "callback_data": f"charge:noissue:{request_id}"},
            {"text": CHARGE_BUTTON_DONE, "callback_data": f"charge:done:{request_id}"},
        ]]
    }
    for admin_id in admin_ids:
        msg_id = send_message(admin_id, notify_text, reply_markup=keyboard)
        if msg_id is not None:
            _charge_requests[request_id]["admin_message_ids"].setdefault(str(admin_id), []).append(msg_id)
    _save_charge_requests()


def _remind_pending_charge_request(request_id: str, record: dict) -> None:
    """برای یه درخواست شارژ که هنوز کسی جوابش رو نداده، هم به مسئولین دوباره پیام (با دکمه) می‌فرسته
    هم توی گروه، ریپلای روی همون پیام جواب، یادآوری می‌ذاره."""
    admin_ids = charge_admins.get_ids()
    reminder_text = (
        f"⏰ یادآوری: هنوز به درخواست شارژ فروشگاه {record['code']} "
        f"(از طرف {record['requester_name']}) پاسخ داده نشده."
    )
    keyboard = {
        "inline_keyboard": [[
            {"text": CHARGE_BUTTON_NOISSUE, "callback_data": f"charge:noissue:{request_id}"},
            {"text": CHARGE_BUTTON_DONE, "callback_data": f"charge:done:{request_id}"},
        ]]
    }
    for admin_id in admin_ids:
        msg_id = send_message(admin_id, reminder_text, reply_markup=keyboard)
        if msg_id is not None:
            record["admin_message_ids"].setdefault(str(admin_id), []).append(msg_id)

    gr_msg_id = send_message(
        record["group_chat_id"],
        "⏰ هنوز مسئول شارژ به این درخواست رسیدگی نکرده.",
        reply_to_message_id=record["answer_message_id"],
    )
    if gr_msg_id is not None:
        record["group_reminder_ids"].append(gr_msg_id)
    log.info("یادآوری درخواست شارژ %s (فروشگاه %s) ارسال شد.", request_id, record["code"])
    _save_charge_requests()


def _maybe_remind_pending_charge_requests() -> None:
    """هر ۳۰ دقیقه، تا وقتی یه درخواست شارژ resolve نشده، دوباره یادآوری می‌فرسته."""
    now = time.time()
    for request_id, record in list(_charge_requests.items()):
        if record["resolved"]:
            continue
        if now - record["last_reminded_at"] < CHARGE_REQUEST_REMINDER_SECONDS:
            continue
        record["last_reminded_at"] = now
        try:
            _remind_pending_charge_request(request_id, record)
        except Exception as e:
            log.exception("خطا در ارسال یادآوری درخواست شارژ %s: %s", request_id, e)


def _find_unresolved_reset_request(group_chat_id, kind: str, target: str):
    """درخواست ریست رمزِ قبلی برای همین هدف (همون فروشگاه یا کاربر) که هنوز
    رسیدگی نشده رو پیدا می‌کنه تا درخواست تکراری ثبت نشه."""
    needle = target.casefold() if kind == "hq" else target
    for record in _reset_requests.values():
        if record["resolved"] or record["kind"] != kind:
            continue
        if record["group_chat_id"] != group_chat_id:
            continue
        current = record["target"]
        if (current.casefold() if kind == "hq" else current) == needle:
            return record
    return None


def _reset_request_card(requester_name: str, kind: str, target: str) -> str:
    """پیام ثبت درخواست ریست رمز — قالب یکسان برای فروشگاه و ستادی."""
    target_line = (
        f"🏪 فروشگاه: {target}"
        if kind == "store"
        else f"🧑‍💻 کاربر: {target}"
    )
    return (
        "🔑 درخواست ریست رمز\n"
        "\n"
        f"👤 درخواست کننده: {requester_name}\n"
        f"{target_line}\n"
        "\n"
        "⏳ در انتظار رسیدگی توسط مسئول ریست رمز"
    )


def _reset_target_line(kind: str, target: str) -> str:
    return f"فروشگاه {target}" if kind == "store" else f"کاربر {target}"


def _reset_done_group_text(kind: str, target: str, admin_name: str, requester_name: str = "") -> str:
    """پیام تأیید ریست برای گروه — بدون رمز پیش‌فرض."""
    requester = f"به درخواست {requester_name}، " if requester_name else ""
    return f"⚠️ {requester}رمز {_reset_target_line(kind, target)} توسط {admin_name} ریست شد."


def _reset_done_reply_text(kind: str, target: str, admin_name: str, requester_name: str = "") -> str:
    """پیام تأیید ریست همراه با رمز پیش‌فرض (ستادی: ۹ رقم، فروشگاه: ۸ رقم) —
    این پیام فقط پیوی خود درخواست‌کننده فرستاده می‌شه."""
    password = RESET_DEFAULT_PASSWORD_STORE if kind == "store" else RESET_DEFAULT_PASSWORD_HQ
    requester = f"به درخواست {requester_name}، " if requester_name else ""
    return (
        f"⚠️ {requester}رمز {_reset_target_line(kind, target)} توسط {admin_name} ریست شد.\n"
        f"🔑 رمز پیش فرض: {password}"
    )


def _reset_wrong_reply_text(kind: str, target: str, admin_name: str) -> str:
    """پیام «مشخصات اشتباه است» با قالب HTML؛ خط «بررسی توسط» داخل یک کادر (code) می‌آید."""
    label = RESET_KIND_LABELS.get(kind, kind)
    safe_target = html.escape(str(target))
    safe_admin = html.escape(str(admin_name))
    return (
        f"❌ مشخصات ارسالی ({label} «{safe_target}») اشتباه بود.\n"
        f"لطفاً دوباره بنویسید «{RESET_TRIGGER_WORD}» و مشخصات رو درست ارسال کنید.\n\n"
        f"<code>بررسی توسط: {safe_admin}</code>"
    )


def _notify_reset_admins(group_chat_id, answer_message_id: int | None, kind: str, target: str, requester_name: str, requester_user_id=None) -> None:
    """درخواست ریست رمز رو پیوی برای مسئولین ریست (زرابی، بصیر و…) می‌فرسته."""
    admin_ids = reset_admins.get_ids()
    if not admin_ids:
        log.warning("هیچ مسئول ریست رمزی توی پنل ثبت نشده؛ درخواست ریست رمز برای کسی ارسال نشد.")
        return

    request_id = secrets.token_hex(4)
    _reset_requests[request_id] = {
        "group_chat_id": group_chat_id,
        "answer_message_id": answer_message_id,
        "kind": kind,
        "target": target,
        "requester_name": requester_name,
        "requester_user_id": requester_user_id,
        "resolved": False,
        "admin_message_ids": {},
        "group_reminder_ids": [],
        "last_reminded_at": time.time(),
        "created_at": time.time(),
    }

    notify_text = f"رمز {_reset_target_line(kind, target)} رو ریست کن"
    keyboard = {
        "inline_keyboard": [[
            {"text": RESET_BUTTON_DONE, "callback_data": f"reset:done:{request_id}"},
            {"text": RESET_BUTTON_WRONG, "callback_data": f"reset:wrong:{request_id}"},
        ]]
    }
    for admin_id in admin_ids:
        msg_id = send_message(admin_id, notify_text, reply_markup=keyboard)
        if msg_id is not None:
            _reset_requests[request_id]["admin_message_ids"].setdefault(str(admin_id), []).append(msg_id)
    _save_reset_requests()


def _remind_pending_reset_request(request_id: str, record: dict) -> None:
    """برای یه درخواست ریست رمز جواب‌نداده، هم به مسئولین دوباره پیام (با دکمه) می‌فرسته
    هم توی گروه، ریپلای روی همون پیام درخواست، یادآوری می‌ذاره."""
    admin_ids = reset_admins.get_ids()
    label = RESET_KIND_LABELS.get(record["kind"], record["kind"])
    reminder_text = (
        f"⏰ یادآوری: هنوز به درخواست ریست رمز {label} {record['target']} "
        f"(از طرف {record['requester_name']}) پاسخ داده نشده."
    )
    keyboard = {
        "inline_keyboard": [[
            {"text": RESET_BUTTON_DONE, "callback_data": f"reset:done:{request_id}"},
            {"text": RESET_BUTTON_WRONG, "callback_data": f"reset:wrong:{request_id}"},
        ]]
    }
    for admin_id in admin_ids:
        msg_id = send_message(admin_id, reminder_text, reply_markup=keyboard)
        if msg_id is not None:
            record["admin_message_ids"].setdefault(str(admin_id), []).append(msg_id)

    gr_msg_id = send_message(
        record["group_chat_id"],
        "⏰ هنوز مسئول ریست رمز به این درخواست رسیدگی نکرده.",
        reply_to_message_id=record["answer_message_id"],
    )
    if gr_msg_id is not None:
        record["group_reminder_ids"].append(gr_msg_id)
    log.info("یادآوری درخواست ریست رمز %s (%s %s) ارسال شد.", request_id, label, record["target"])
    _save_reset_requests()


def _maybe_remind_pending_reset_requests() -> None:
    """هر ۳۰ دقیقه، تا وقتی یه درخواست ریست رمز resolve نشده، دوباره یادآوری می‌فرسته."""
    now = time.time()
    for request_id, record in list(_reset_requests.items()):
        if record["resolved"]:
            continue
        if now - record["last_reminded_at"] < RESET_REQUEST_REMINDER_SECONDS:
            continue
        record["last_reminded_at"] = now
        try:
            _remind_pending_reset_request(request_id, record)
        except Exception as e:
            log.exception("خطا در ارسال یادآوری درخواست ریست رمز %s: %s", request_id, e)


def handle_callback_query(callback_query: dict) -> None:
    cq_id = callback_query.get("id")
    data = callback_query.get("data") or ""
    clicking_admin = callback_query.get("from") or {}
    clicking_admin_id = str(clicking_admin.get("id"))
    chat_id = callback_query.get("message", {}).get("chat", {}).get("id")

    if clicking_admin.get("id"):
        presence.record(
            clicking_admin["id"],
            _display_name(clicking_admin),
            clicking_admin.get("username") or "",
            "callback",
        )

    # --- callback منوی پیوی ---
    if data.startswith("pm:"):
        if not chat_id or str(chat_id) != clicking_admin_id:
            answer_callback_query(cq_id, "این منو فقط در گفت‌وگوی خصوصی ربات قابل استفاده است.")
            return
        action = data.split(":", 1)[1]
        if action in ("charge", "info", "shift") and not registration.is_approved(clicking_admin.get("id")):
            answer_callback_query(cq_id, "برای استفاده از این قابلیت باید تایید شده باشید.")
            return
        if action == "charge":
            if chat_id:
                _private_state[chat_id] = "charge"
                _send_private_code_prompt(chat_id, "charge")
            answer_callback_query(cq_id, "جستجوی شارژ")
        elif action == "info":
            if chat_id:
                _private_state[chat_id] = "info"
                _send_private_code_prompt(chat_id, "info")
            answer_callback_query(cq_id, "اطلاعات فروشگاه")
        elif action == "shift":
            if chat_id:
                today_str = jdatetime.date.today().strftime("%Y/%m/%d")
                record = schedule_store.lookup(today_str)
                send_message(chat_id, format_today_shift(record))
                usage_stats.record_action(clicking_admin.get("id"), "shift")
            answer_callback_query(cq_id, "شیفت امروز")
        elif action == "help":
            if chat_id:
                send_message(chat_id, HELP_TEXT)
            answer_callback_query(cq_id, "راهنما")
        elif action == "back":
            if chat_id:
                _private_state[chat_id] = "menu"
                _send_private_menu(chat_id)
            answer_callback_query(cq_id, "بازگشت به منو")
        elif action == "cancel":
            if chat_id:
                _private_state.pop(chat_id, None)
                send_message(chat_id, "✅ عملیات لغو شد.")
                _send_private_menu(chat_id)
            answer_callback_query(cq_id, "انصراف")
        return

    # --- callback تایید/رد ثبت‌نام ---
    if data.startswith("reg:"):
        # تایید ثبت‌نام فقط از پیوی مدیرِ تنظیم‌شده مجاز است.
        if (not config.ADMIN_CHAT_ID
                or str(chat_id) != str(config.ADMIN_CHAT_ID)
                or clicking_admin_id != str(config.ADMIN_CHAT_ID)):
            answer_callback_query(cq_id, "شما اجازه‌ی انجام این عملیات را ندارید.")
            return
        parts = data.split(":")
        if len(parts) == 3 and parts[1] in ("approve", "reject"):
            target_user_id = parts[2]
            action = parts[1]
            admin_name = _display_name(clicking_admin)

            if action == "approve":
                result = registration.approve(target_user_id)
                if result:
                    answer_callback_query(cq_id, f"کاربر {result.get('full_name', '')} تایید شد ✅")
                    # به کاربر اطلاع بده
                    send_message(int(target_user_id), "✅ درخواست شما توسط ادمین تایید شد.\nحالا می‌تونید از ربات استفاده کنید.\nبرای شروع /start بزنید.")
                    # به ادمین بگو
                    if chat_id:
                        clear_message_buttons(chat_id, callback_query.get("message", {}).get("message_id"))
                        send_message(chat_id, f"✅ کاربر «{result.get('full_name', '')}» توسط {admin_name} تایید شد.")
                    log.info("کاربر %s توسط %s تایید شد", target_user_id, admin_name)
                else:
                    answer_callback_query(cq_id, "کاربر پیدا نشد.")
            else:
                result = registration.reject(target_user_id)
                if result:
                    answer_callback_query(cq_id, f"کاربر {result.get('full_name', '')} رد شد ❌")
                    send_message(int(target_user_id), "❌ درخواست شما توسط ادمین رد شد.\nدر صورت نیاز با مدیر تماس بگیرید.")
                    if chat_id:
                        clear_message_buttons(chat_id, callback_query.get("message", {}).get("message_id"))
                        send_message(chat_id, f"❌ کاربر «{result.get('full_name', '')}» توسط {admin_name} رد شد.")
                    log.info("کاربر %s توسط %s رد شد", target_user_id, admin_name)
                else:
                    answer_callback_query(cq_id, "کاربر پیدا نشد.")
        return

    # --- callback «ارسال مجدد» کد فروشگاه (بعد از فروشگاه جمع‌آوری‌شده) ---
    if data.startswith("reset:retry:"):
        target_uid = data.split(":", 2)[2]
        if not (isinstance(chat_id, int) and clicking_admin.get("id") and str(clicking_admin["id"]) == target_uid):
            answer_callback_query(cq_id, "این دکمه برای شما نیست.")
            return
        pending_key = (chat_id, int(clicking_admin["id"]))
        _pending_reset[pending_key] = {
            "trigger_id": None,
            "prompt_id": None,
            "extra_ids": [],
            "created_at": time.time(),
            "display_name": _display_name(clicking_admin),
            "step": "store",
        }
        prompt_id = send_message(chat_id, RESET_STORE_PROMPT_TEXT)
        _pending_reset[pending_key]["prompt_id"] = prompt_id
        clear_message_buttons(chat_id, callback_query.get("message", {}).get("message_id"))
        answer_callback_query(cq_id, "کد فروشگاه رو بفرست")
        return

    # --- callback انتخاب نوع درخواست ریست رمز (گروه) ---
    if data in ("reset:store", "reset:hq"):
        if not (isinstance(chat_id, int) and clicking_admin.get("id")):
            answer_callback_query(cq_id, "توی گروه استفاده کنید.")
            return
        pending_key = (chat_id, int(clicking_admin["id"]))
        entry = _pending_reset.get(pending_key)
        if entry is None or entry.get("step") in ("store", "hq"):
            answer_callback_query(cq_id, "این درخواست پیدا نشد یا قبلاً انتخاب شده.")
            return

        kind = "store" if data == "reset:store" else "hq"
        entry["step"] = kind

        # مثل شارژ: پیام‌های قبلی (تریگر «رمز» + پرامپت انتخاب نوع) سریع پاک بشن
        # تا گروه تمیز بمونه و کاربر فقط کد/نام رو تایپ کنه.
        delete_message(chat_id, entry.get("trigger_id"))
        delete_message(chat_id, entry.get("prompt_id"))
        for extra_id in entry.get("extra_ids", []):
            delete_message(chat_id, extra_id)

        if kind == "store":
            prompt_id = send_message(chat_id, RESET_STORE_PROMPT_TEXT)
            answer_callback_query(cq_id, "🏪 فروشگاه")
        else:
            prompt_id = send_message(chat_id, RESET_HQ_PROMPT_TEXT)
            answer_callback_query(cq_id, "🏢 ستادی")
        entry["trigger_id"] = None
        entry["prompt_id"] = prompt_id
        entry["extra_ids"] = []
        return

    # --- callback تایید انجام / اشتباه بودن درخواست ریست رمز ---
    if data.startswith("reset:done:") or data.startswith("reset:wrong:"):
        _, action, request_id = data.split(":", 2)
        record = _reset_requests.get(request_id)
        if (action not in ("done", "wrong")
                or str(chat_id) != clicking_admin_id
                or clicking_admin_id not in reset_admins.get_ids()
                or not record
                or clicking_admin_id not in record.get("admin_message_ids", {})):
            answer_callback_query(cq_id, "شما اجازه‌ی پاسخ به این درخواست را ندارید.")
            return
        if record is None:
            answer_callback_query(cq_id, "این درخواست پیدا نشد یا قبلاً پاک شده.")
            return
        if record["resolved"]:
            answer_callback_query(cq_id, "این درخواست قبلاً پاسخ داده شده.")
            return

        record["resolved"] = True
        record["resolved_at"] = time.time()
        record["resolved_action"] = action
        admin_name = _display_name(clicking_admin)
        label = RESET_KIND_LABELS.get(record["kind"], record["kind"])
        elapsed_seconds = time.time() - record.get("created_at", time.time())
        usage_stats.record_reset_response(clicking_admin_id, action, elapsed_seconds)
        if action == "wrong":
            reply_text = _reset_wrong_reply_text(record["kind"], record["target"], admin_name)
            parse_mode = "html"
            answer_callback_query(cq_id, "ثبت شد؛ مشخصات اشتباه بود ❌")
            send_message(record["group_chat_id"], reply_text, reply_to_message_id=record["answer_message_id"], parse_mode=parse_mode)
        else:
            requester_name = record.get("requester_name", "")
            reply_text = _reset_done_reply_text(record["kind"], record["target"], admin_name, requester_name)
            parse_mode = None
            answer_callback_query(cq_id, "ثبت شد ✅")
            # در گروه فقط «به درخواست چه‌کسی و توسط چه‌کسی» گفته میشه (بدون رمز)؛
            # این پیام آخر می‌مونه و پاک نمیشه، ولی کارت درخواست و یادآوری‌ها فوراً پاک میشن.
            group_text = _reset_done_group_text(record["kind"], record["target"], admin_name, requester_name)
            send_message(record["group_chat_id"], group_text)
            delete_message(record["group_chat_id"], record["answer_message_id"])
        log.info("درخواست ریست رمز %s (%s %s) توسط %s پاسخ داده شد: %s", request_id, label, record["target"], admin_name, action)

        # دکمه‌ها رو از پیام اول ارسالی به مسئولین حذف کن، پیام‌های یادآوری رو کامل حذف کن،
        # و اونایی که نزدن رو از رسیدگی خبر بده
        for admin_id, msg_ids in record["admin_message_ids"].items():
            reminder_msgs, first_msg = msg_ids[1:], msg_ids[0] if msg_ids else None
            for msg_id in reminder_msgs:
                delete_message(admin_id, msg_id)
            if first_msg is not None:
                clear_message_buttons(admin_id, first_msg)
            if admin_id == clicking_admin_id:
                send_message(admin_id, reply_text, parse_mode=parse_mode)
            else:
                send_message(admin_id, f"ℹ️ این درخواست قبلاً توسط {admin_name} پاسخ داده شد.\n{reply_text}", parse_mode=parse_mode)
        for gr_id in record.get("group_reminder_ids", []):
            delete_message(record["group_chat_id"], gr_id)
        _save_reset_requests()
        return

    # --- callback درخواست شارژ ---
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "charge":
        answer_callback_query(cq_id)
        return

    _, action, request_id = parts
    record = _charge_requests.get(request_id)

    if (action not in ("done", "noissue")
            or str(chat_id) != clicking_admin_id
            or clicking_admin_id not in charge_admins.get_ids()
            or not record
            or clicking_admin_id not in record.get("admin_message_ids", {})):
        answer_callback_query(cq_id, "شما اجازه‌ی پاسخ به این درخواست را ندارید.")
        return

    if record is None:
        answer_callback_query(cq_id, "این درخواست پیدا نشد یا قبلاً پاک شده.")
        return

    if record["resolved"]:
        answer_callback_query(cq_id, "این درخواست قبلاً پاسخ داده شده.")
        return

    record["resolved"] = True
    record["resolved_at"] = time.time()
    record["resolved_action"] = action
    admin_name = _display_name(clicking_admin)
    answer_callback_query(cq_id, "ثبت شد ✅")

    elapsed_seconds = time.time() - record.get("created_at", time.time())
    usage_stats.record_charge_response(clicking_admin_id, action, elapsed_seconds)

    if action == "done":
        reply_text = f"✅ شارژ فروشگاه {record['code']} توسط {admin_name} انجام شد."
    else:
        reply_text = (
            f"ℹ️ طبق بررسی {admin_name}:\n"
            f"\n"
            f"🏪 فروشگاه {record['code']} : 🔋 شارژ داره"
        )

    send_message(record["group_chat_id"], reply_text, reply_to_message_id=record["answer_message_id"])
    log.info("درخواست شارژ %s (فروشگاه %s) توسط %s: %s", request_id, record["code"], admin_name, action)

    # دکمه‌ها رو از پیام اول ارسالی به ادمین‌ها حذف کن، پیام‌های یادآوری رو کامل حذف کن،
    # و به اونایی که نزدن خبر بده جواب داده شده
    for admin_id, msg_ids in record["admin_message_ids"].items():
        reminder_msgs, first_msg = msg_ids[1:], msg_ids[0] if msg_ids else None
        for msg_id in reminder_msgs:
            delete_message(admin_id, msg_id)
        if first_msg is not None:
            clear_message_buttons(admin_id, first_msg)
        if admin_id != clicking_admin_id:
            send_message(admin_id, f"ℹ️ این درخواست قبلاً توسط {admin_name} پاسخ داده شد.\n{reply_text}")
    for gr_id in record.get("group_reminder_ids", []):
        delete_message(record["group_chat_id"], gr_id)
    _save_charge_requests()


def _shift_lines(record: dict) -> list[str]:
    """فقط بخش‌هایی که واقعاً دیتا دارن رو می‌سازه."""
    desk1 = record.get("desk1")
    desk2 = record.get("desk2")
    has_desk1 = bool(desk1) and desk1 != "***"
    has_desk2 = bool(desk2) and desk2 != "***"

    lines = []
    if has_desk1 or has_desk2:
        lines.append("🖥 سرویس دسک:")
        if has_desk1:
            lines.append(f"👤 کارشناس اول: {desk1}")
        if has_desk2:
            lines.append(f"👤 کارشناس دوم: {desk2}")
        lines.append("")

    morning = (record.get("morning_shift") or "").strip()
    evening = (record.get("evening_shift") or "").strip()
    # اگه شیفت روزانه وارد شده، همیشه نشون بده (مخصوصاً روزهای تعطیل)
    if morning or evening:
        lines.append("🕖 شیفت روزانه:")
        if morning:
            # اگه فقط ساعت نوشته نشده ولی اسم هست با هم نمایش میشه
            if morning and ' ' in morning and ':' in morning:
                lines.append(f"👤 {morning}")
            elif morning:
                lines.append(f"👤 {morning}")
            else:
                pass
        if evening:
            lines.append(f"👤 {evening}")
        lines.append("")

    lines.append("🌙 شیفت شب:")
    n1 = record.get("night_primary") or ""
    if n1 and n1 != "***":
        lines.append(f"👤 {n1}")
    n2 = record.get("night_secondary") or ""
    if n2 and n2 != "***":
        lines.append(f"👤 {n2}")
    return lines


def format_schedule_edit_announcement(record: dict) -> str:
    lines = [
        "✏️ شیفت این روز ویرایش شد",
        "",
        f"🗓 {record['day_name']} | {record['date']}",
        "",
    ]
    lines.extend(_shift_lines(record))
    return "\n".join(lines)


def notify_schedule_edited(record: dict) -> None:
    """وقتی شیفت یه روز از پنل ویرایش می‌شه، به گروه اطلاع می‌ده."""
    target_chat_id = _get_schedule_target_chat_id()
    if target_chat_id is None:
        log.warning("هیچ گروهی برای اطلاع‌رسانی ویرایش شیفت شناسایی نشده.")
        return
    send_message(target_chat_id, format_schedule_edit_announcement(record))
    log.info("اطلاعیه ویرایش شیفت (%s) به گروه %s ارسال شد.", record["date"], target_chat_id)


def format_schedule_announcement(record: dict) -> str:
    lines = [
        "📢 اطلاعیه شیفت فردا",
        "",
        f"🗓 {record['day_name']} | {record['date']}",
        "",
    ]
    lines.extend(_shift_lines(record))
    return "\n".join(lines)


def format_today_shift(record: dict | None) -> str:
    today = jdatetime.date.today()
    day_name = _jalali_day_name(today)
    date_str = today.strftime("%Y/%m/%d")

    if record is None:
        return (
            f"📆 شیفت امروز\n\n🗓 {day_name} | {date_str}\n\n"
            "❌ اطلاعات شیفت امروز تو فایل بارگذاری‌شده پیدا نشد."
        )

    lines = [
        "📆 شیفت امروز",
        "",
        f"🗓 {record['day_name']} | {record['date']}",
        "",
    ]
    lines.extend(_shift_lines(record))
    return "\n".join(lines)


def format_usage_report(active_users: list, inactive_users: list, admins: list, top_admin_id, reset_admins: list | None = None, top_reset_admin_id=None, top_n: int = 5) -> str:
    """گزارش آمار استفاده از ربات رو برای ارسال به گروه فرمت می‌کنه."""
    lines = [
        "📊 گزارش استفاده از ربات",
        "",
        f"👥 کاربران فعال: {len(active_users)} نفر",
        f"⛔ کاربران بدون استفاده: {len(inactive_users)} نفر",
    ]

    if active_users:
        lines.append("")
        lines.append("🏅 پراستفاده‌ترین کاربرها:")
        for i, u in enumerate(active_users[:top_n], 1):
            lines.append(f"{i}. {u.get('full_name') or '-'} — {u['total']} بار")

    if admins:
        lines.append("")
        lines.append("🔔 مسئولین شارژ:")
        for a in admins:
            star = " 🏆" if a["user_id"] == top_admin_id and a["responded_count"] > 0 else ""
            lines.append(
                f"- {a.get('name') or a['user_id']}: {a['responded_count']} پاسخ "
                f"(میانگین زمان پاسخ: {a['avg_response']}){star}"
            )

    if reset_admins:
        lines.append("")
        lines.append("🔑 مسئولین ریست رمز:")
        for a in reset_admins:
            star = " 🏆" if a["user_id"] == top_reset_admin_id and a["responded_count"] > 0 else ""
            lines.append(
                f"- {a.get('name') or a['user_id']}: {a['responded_count']} پاسخ "
                f"(میانگین زمان پاسخ: {a['avg_response']}){star}"
            )

    return "\n".join(lines)


def format_group_stats_report(stats: dict) -> str:
    """خلاصه‌ی سه‌عددی بالای سایدبار (افراد گروه/احراز شده/روح) رو برای ارسال به گروه فرمت می‌کنه."""
    return (
        "📊 آمار گروه\n\n"
        f"👥 افراد گروه: {stats['total_members']}\n"
        f"✅ احراز شده: {stats['total_approved']}\n"
        f"👻 روح: {stats['total_ghost']}"
    )


def send_group_auth_reminder(chat_id, ghost_count: int) -> int | None:
    """یادآوری احراز هویت رو با دکمه‌ی ورود به پیوی ربات، به گروه می‌فرسته.
    چون بات نمی‌تونه به کسی که خودش پیوی ربات رو شروع نکرده مستقیم پیام بده،
    تنها راه رسوندن پیام به «روح»‌ها همینه: یادآوری تو خودِ گروه، با دکمه‌ی ورود به پیوی."""
    text = (
        "🔔 یادآوری احراز هویت\n\n"
        f"طبق آمار، حدود {ghost_count} نفر توی این گروه هنوز توی ربات احراز هویت نکردن.\n"
        "برای استفاده از قابلیت‌های ربات (شارژ، اطلاعات فروشگاه، شیفت)، لطفاً روی دکمه‌ی زیر بزنید و ثبت‌نام کنید."
    )
    link = _bot_deep_link()
    keyboard = {"inline_keyboard": [[{"text": "🔐 احراز هویت کنید", "url": link}]]} if link else None
    return send_message(chat_id, text, reply_markup=keyboard)


def format_group_auth_report(chat_title: str, members: list, limit: int = 15) -> str:
    """گزارش وضعیت احراز هویت اعضای دیده‌شده‌ی یه گروه رو فرمت می‌کنه."""
    approved = [m for m in members if m["status_key"] == "approved"]
    pending = [m for m in members if m["status_key"] == "pending"]
    unregistered = [m for m in members if m["status_key"] == "unregistered"]

    lines = [
        f"🔐 گزارش احراز هویت گروه «{chat_title}»",
        "",
        f"✅ احراز شده: {len(approved)} نفر",
        f"⏳ در انتظار تایید: {len(pending)} نفر",
        f"⛔ ثبت‌نام نکرده: {len(unregistered)} نفر",
    ]

    if unregistered:
        lines.append("")
        lines.append("⛔ ثبت‌نام‌نکرده‌ها:")
        for m in unregistered[:limit]:
            lines.append(f"- {m['display_name']}")
        if len(unregistered) > limit:
            lines.append(f"... و {len(unregistered) - limit} نفر دیگه")

    if pending:
        lines.append("")
        lines.append("⏳ منتظر تایید:")
        for m in pending[:limit]:
            lines.append(f"- {m['display_name']}")
        if len(pending) > limit:
            lines.append(f"... و {len(pending) - limit} نفر دیگه")

    lines.append("")
    lines.append("ℹ️ این گزارش فقط شامل کسانیه که حداقل یه‌بار توی گروه پیام فرستادن.")

    return "\n".join(lines)


def _get_schedule_target_chat_id():
    all_groups = groups.list_all()
    if not all_groups:
        return None
    for g in all_groups:
        if SCHEDULE_GROUP_TITLE_HINT in (g.get("title") or ""):
            return g["chat_id"]
    # Fail closed: never send schedule details to an arbitrary first-seen group.
    return None


def _maybe_send_schedule_announcement() -> None:
    """هر روز راس ساعت‌های تنظیم‌شده، اطلاعیه‌ی شیفتِ فردا رو به گروه انفورماتیک می‌فرسته."""
    now = time.localtime()
    hhmm = time.strftime("%H:%M", now)
    if hhmm not in schedule_times.list_all():
        return

    today_jalali = jdatetime.date.today()
    slot_key = f"{today_jalali.strftime('%Y/%m/%d')}_{hhmm}"
    if slot_key in _sent_schedule_slots:
        return

    _sent_schedule_slots.add(slot_key)
    _save_sent_schedule_slots()

    target_chat_id = _get_schedule_target_chat_id()
    if target_chat_id is None:
        log.warning("هیچ گروهی برای ارسال اطلاعیه شیفت شناسایی نشده.")
        return

    tomorrow_jalali = today_jalali + timedelta(days=1)
    date_str = tomorrow_jalali.strftime("%Y/%m/%d")
    record = schedule_store.lookup(date_str)

    if record is None:
        log.warning("داده‌ی شیفت برای تاریخ %s پیدا نشد.", date_str)
        missing_key = f"missing_{date_str}"
        if missing_key not in _sent_schedule_slots and config.ADMIN_CHAT_ID:
            _sent_schedule_slots.add(missing_key)
            _save_sent_schedule_slots()
            send_message(
                config.ADMIN_CHAT_ID,
                f"⚠️ داده‌ی برنامه شیفت برای تاریخ {date_str} پیدا نشد. لطفاً فایل ماه جدید رو از پنل آپلود کن.",
            )
        return

    send_message(target_chat_id, format_schedule_announcement(record))
    log.info("اطلاعیه شیفت فردا (%s) به گروه %s ارسال شد.", date_str, target_chat_id)


def _notify_admin_new_registration(record: dict) -> None:
    if not config.ADMIN_CHAT_ID:
        log.warning("ADMIN_CHAT_ID تنظیم نشده؛ اطلاع‌رسانی ثبت‌نام جدید ارسال نشد.")
        return

    bale_name = f"{record['first_name']} {record['last_name']}".strip() or "-"
    username = f"@{record['username']}" if record["username"] else "-"
    user_id = record["user_id"]
    lines = [
        "🆕 ثبت‌نام جدید توی ربات:",
        "",
        f"👤 نام واردشده: {record['full_name']}",
        f"🪪 نام بله: {bale_name}",
        f"🔖 یوزرنیم: {username}",
        f"🆔 آیدی عددی: {user_id}",
        "",
        "⏳ در انتظار تایید شما...",
    ]
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ تایید", "callback_data": f"reg:approve:{user_id}"},
            {"text": "❌ رد", "callback_data": f"reg:reject:{user_id}"},
        ]]
    }
    send_message(config.ADMIN_CHAT_ID, "\n".join(lines), reply_markup=keyboard)


def handle_private_message(message: dict) -> None:
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    text = (message.get("text") or "").strip()

    if not text:
        return

    # راهنما همیشه در دسترسه، حتی برای کسی که هنوز ثبت‌نام/تایید نشده،
    # و بدون اینکه به وسط فرآیند ثبت‌نامش لطمه بزنه
    if _normalize_yeh_kaf(text) in HELP_TRIGGER_WORDS:
        send_message(chat_id, HELP_TEXT)
        return

    # ثبت‌نام اجباری
    if not registration.is_registered(user_id):
        if user_id in _pending_registration:
            record = registration.register(user_id, text, message["from"])
            _pending_registration.discard(user_id)
            log.info("ثبت‌نام جدید: «%s» (user_id=%s)", text, user_id)
            send_message(chat_id, REGISTRATION_DONE_TEXT)
            _notify_admin_new_registration(record)
            return

        _pending_registration.add(user_id)
        send_message(chat_id, REGISTRATION_PROMPT_TEXT)
        return

    # تایید ادمین
    if not registration.is_approved(user_id):
        link = _bot_deep_link()
        keyboard = {"inline_keyboard": [[{"text": "🔐 احراز هویت کنید", "url": link}]]} if link else None
        send_message(chat_id, PENDING_APPROVAL_TEXT, reply_markup=keyboard)
        return

    # --- دستورات متنی (همیشه فعال) ---
    if text in ("/start", "شروع"):
        _private_state[chat_id] = "menu"
        _send_private_menu(chat_id)
        return

    if text in ("/back", "بازگشت", "برگشت"):
        _private_state[chat_id] = "menu"
        _send_private_menu(chat_id)
        return

    if text in ("/cancel", "انصراف", "لغو", "_cancel"):
        _private_state.pop(chat_id, None)
        send_message(chat_id, "✅ عملیات لغو شد.")
        _send_private_menu(chat_id)
        return

    # --- بررسی state فعلی کاربر ---
    state = _private_state.get(chat_id, "menu")

    if state == "charge":
        # اگه متن عدد نباشه، یعنی کاربر می‌خواد حالت عوض کنه
        normalized_code = _normalize_code_input(text)
        if not CODE_PATTERN.fullmatch(normalized_code):
            _private_state[chat_id] = "menu"
            _send_private_menu(chat_id)
            return
        results = data_store.lookup(normalized_code)
        if results:
            send_message(chat_id, format_reply(normalized_code, results))
        else:
            send_message(chat_id, PRIVATE_NOT_FOUND_TEXT.format(normalized_code))
        usage_stats.record_action(user_id, "charge")
        usage_stats.record_code_lookup(normalized_code, "charge")
        return

    if state == "info":
        normalized_code = _normalize_code_input(text)
        if not CODE_PATTERN.fullmatch(normalized_code):
            _private_state[chat_id] = "menu"
            _send_private_menu(chat_id)
            return
        record = info_store.lookup_info(normalized_code)
        if record:
            send_message(chat_id, info_store.format_info_reply(normalized_code, record))
        else:
            send_message(chat_id, PRIVATE_NOT_FOUND_TEXT.format(normalized_code))
        usage_stats.record_action(user_id, "info")
        usage_stats.record_code_lookup(normalized_code, "info")
        return

    # state == "menu" — هر متنی غیر از دستورات سیستمی، منو نشون بده
    _send_private_menu(chat_id)


def handle_message(message: dict) -> None:
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type")

    # حضور کاربر رو ثبت می‌کنیم حتی اگه پیامش متن یا قابل‌پردازش نباشه (عکس، استیکر، …)
    from_user = message.get("from") or {}
    if from_user.get("id"):
        presence.record(
            from_user["id"],
            _display_name(from_user),
            from_user.get("username") or "",
            "group" if chat_type in GROUP_CHAT_TYPES else "private",
        )

    if chat_id and chat_type in GROUP_CHAT_TYPES:
        groups.record(chat_id, chat.get("title") or "")
        from_user = message.get("from") or {}
        if from_user.get("id"):
            group_members.record(chat_id, from_user["id"], _display_name(from_user), from_user.get("username") or "")

    text = message.get("text")

    if not chat_id or not message.get("from") or text is None:
        return

    if chat_type in GROUP_CHAT_TYPES:
        handle_group_message(message)
    else:
        handle_private_message(message)


def run() -> None:
    if not config.BALE_BOT_TOKEN:
        log.error("BALE_BOT_TOKEN تنظیم نشده. فایل .env رو چک کن.")
        return

    log.info("ربات شروع به کار کرد (polling)...")
    offset = None

    while True:
        updates = get_updates(offset=offset, timeout=config.POLL_INTERVAL_SECONDS * 10)
        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message")
            callback_query = update.get("callback_query")
            if message:
                try:
                    handle_message(message)
                except Exception as e:
                    log.exception("خطا در پردازش پیام: %s", e)
            elif callback_query:
                try:
                    handle_callback_query(callback_query)
                except Exception as e:
                    log.exception("خطا در پردازش callback_query: %s", e)

        try:
            _maybe_send_schedule_announcement()
        except Exception as e:
            log.exception("خطا در ارسال اطلاعیه شیفت: %s", e)

        try:
            _maybe_remind_pending_charge_requests()
        except Exception as e:
            log.exception("خطا در بررسی یادآوری درخواست‌های شارژ: %s", e)

        try:
            _maybe_remind_pending_reset_requests()
        except Exception as e:
            log.exception("خطا در بررسی یادآوری درخواست‌های ریست رمز: %s", e)

        time.sleep(config.POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
