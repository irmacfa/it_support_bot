"""
تنظیمات پروژه.
همه‌ی مقادیر حساس (توکن ربات، رمز پنل آپلود) از فایل .env خونده میشن.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# مسیر ریشه‌ی پروژه
BASE_DIR = Path(__file__).resolve().parent.parent

# بارگذاری فایل .env از ریشه‌ی پروژه
load_dotenv(BASE_DIR / ".env")

# --- تنظیمات ربات بله ---
BALE_BOT_TOKEN = os.getenv("BALE_BOT_TOKEN", "")
BALE_API_BASE_URL = f"https://tapi.bale.ai/bot{BALE_BOT_TOKEN}"

# --- تنظیمات پنل آپلود ---
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme")

# --- آیدی چت خصوصی مدیر (برای اطلاع‌رسانی ثبت‌نام کاربرهای جدید) ---
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")


# --- مسیر فایل اکسل ---
DATA_DIR = BASE_DIR / "data"
EXCEL_PATH = DATA_DIR / "stores.xlsx"
INFO_EXCEL_PATH = DATA_DIR / "info_stores.xlsx"

# پوشه‌ی فایل‌های شارژ فروشگاه‌ها (هر بار آپلود، محتوای این پوشه جایگزین میشه).
# اگه خالی باشه، برای سازگاری با نسخه‌ی قبلی از EXCEL_PATH استفاده میشه.
STORE_FILES_DIR = DATA_DIR / "stores"
# نسخه‌ی پشتیبان فایل‌های قبلی (آخرین ۳ نسخه نگه داشته میشه)
STORE_BACKUP_DIR = DATA_DIR / "stores_backup"
# گزارش تشخیص خودکار ستون‌ها (خروجی آخرین بارگذاری)
STORE_SCHEMA_PATH = DATA_DIR / "store_schema.json"
# وضعیت «اعمال‌شده» داده‌ها: بعد از هر آپلودِ ادغامی اینجا ذخیره میشه تا با ری‌استارت سرویس
# رکوردهای قبلی که توی فایل جدید نبودن از دست نرن.
STORE_SNAPSHOT_PATH = DATA_DIR / "store_snapshot.json"
# فایل‌های آپلودشده‌ای که هنوز تأیید نشدن (برای نمایش پیش‌نمایش تغییرات قبل از اعمال)
UPLOAD_STAGING_DIR = DATA_DIR / "upload_staging"

# --- فایل اطلاعات فروشگاه‌ها (چارت سازمانی و…) ---
# نسخه‌های پشتیبان فایل اطلاعات (آخرین ۳ نسخه نگه داشته میشه)
INFO_BACKUP_DIR = DATA_DIR / "info_backup"
# گزارش بررسی فایل اطلاعات
INFO_SCHEMA_PATH = DATA_DIR / "info_schema.json"
# وضعیت «اعمال‌شده» تا با ری‌استارت سرویس رکوردهای ادغامی از بین نرن
INFO_SNAPSHOT_PATH = DATA_DIR / "info_snapshot.json"

# --- شیت‌های فایل اکسل که باید جستجو بشن ---
# این تنظیمات اختیاریه: اگه خالی باشه، ستون‌های هر شیت خودکار تشخیص داده می‌شن
# (کد فروشگاه، شماره سیم‌کارت، اپراتور و محل نصب از روی اسم و محتوای ستون‌ها).
# برای وقتی که بخوای یه شیت رو دستی قفل کنی، می‌تونی اینجا ستون‌ها رو مشخص کنی؛
# name باید با اسم شیت توی اکسل یکی باشه (فاصله‌های ابتدا/انتهای اسم شیت نادیده گرفته میشه).
# اگه یه کد فروشگاه توی چند شیت (یا چند ردیف یه شیت) پیدا بشه، همه‌شون توی جواب نشون داده می‌شن.
SHEETS = [
    {
        "name": "Taknet Modems (LTE)",
        "code_col": "محل استقرار فعلی",
        "mobile_col": "شماره پنل",
        "operator_col": None,
        "location_col": "محل نصب در فروشگاه",
    },
    {
        "name": "Unlock Modems",
        "code_col": "محل استقرار فعلی",
        "mobile_col": "شماره سیم کارت",
        "operator_col": "اپراتور",
        "location_col": "محل نصب در فروشگاه",
    },
]

# --- فاصله‌ی هر بار polling (ثانیه) ---
POLL_INTERVAL_SECONDS = 2
