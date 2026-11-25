"""ابزارهای کمکی: فایل، پیشرفت، کارها، پیام‌ها و کیبوردها."""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
import threading
from dataclasses import dataclass, field
import secrets
from typing import Any
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

# ==================================================================
# files.py
# ==================================================================
logger = logging.getLogger(__name__)


def human_size(num_bytes: int) -> str:
    """۱۲۳۴۵۶۷ → «1.2 MB»"""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def human_duration(seconds: int | None) -> str:
    """۳۷۵۰ → «1:02:30»"""
    if not seconds or seconds <= 0:
        return "نامشخص"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def progress_bar(percent: float, width: int = 12) -> str:
    """نوار پیشرفت متنی: ██████░░░░░░ 50٪"""
    percent = max(0.0, min(100.0, percent))
    filled = int(width * percent / 100)
    bar = "█" * filled + "░" * (width - filled)
    return f"{bar} {percent:.0f}٪"


def remove_tree(path: Path) -> None:
    """حذف پوشه‌ی موقت بدون پرتاب خطا."""
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # pragma: no cover - فقط برای اطمینان
        logger.warning("پاک‌سازی ناموفق: %s", path, exc_info=True)


def purge_old_files(directory: Path, max_age_seconds: int = 3600) -> int:
    """حذف پوشه‌های موقت قدیمی‌تر از max_age_seconds.

    برای وقتی که ربات وسط کار کرش کرده و فایل‌ها جا مانده‌اند.
    """
    if not directory.is_dir():
        return 0

    now = time.time()
    removed = 0
    for entry in directory.iterdir():
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age > max_age_seconds:
            if entry.is_dir():
                remove_tree(entry)
            else:
                entry.unlink(missing_ok=True)
            removed += 1

    if removed:
        logger.info("%d فایل/پوشه‌ی قدیمی پاک شد.", removed)
    return removed


# ==================================================================
# progress.py
# ==================================================================
@dataclass
class ProgressState:
    status: str = "starting"  # starting | downloading | finished | error
    downloaded: int = 0
    total: int = 0
    speed: float = 0.0
    eta: int = 0

    @property
    def percent(self) -> float:
        if self.total <= 0:
            return 0.0
        return min(100.0, self.downloaded * 100.0 / self.total)


class ProgressTracker:
    """yt-dlp در یک ترد جدا اجرا می‌شود؛ این کلاس وضعیت را
    با قفل به حلقه‌ی اصلی منتقل می‌کند تا race condition نباشد.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = ProgressState()

    def update(self, **fields: object) -> None:
        with self._lock:
            for key, value in fields.items():
                if hasattr(self._state, key):
                    setattr(self._state, key, value)

    def snapshot(self) -> ProgressState:
        with self._lock:
            return ProgressState(
                status=self._state.status,
                downloaded=self._state.downloaded,
                total=self._state.total,
                speed=self._state.speed,
                eta=self._state.eta,
            )

    # --- سازگار با progress_hooks یت‌دی‌ال‌پی ---
    def hook(self, data: dict) -> None:
        status = data.get("status")
        if status == "downloading":
            self.update(
                status="downloading",
                downloaded=int(data.get("downloaded_bytes") or 0),
                total=int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0),
                speed=float(data.get("speed") or 0.0),
                eta=int(data.get("eta") or 0),
            )
        elif status == "finished":
            self.update(status="finished")


# ==================================================================
# jobs.py
# ==================================================================
# کارها بعد از این مدت منقضی می‌شوند (ثانیه) — جلوگیری از رشد حافظه
JOB_TTL = 30 * 60


@dataclass
class Job:
    job_id: str
    url: str
    platform: str
    title: str
    duration: int | None
    heights: list[int]
    info: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    # --- کنترل اجرا ---
    cancel_event: threading.Event = field(default_factory=threading.Event)
    running: bool = False

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def cancel(self) -> None:
        self.cancel_event.set()

    @property
    def expired(self) -> bool:
        return (time.time() - self.created_at) > JOB_TTL


class JobRegistry:
    """نگه‌دارنده‌ی کارها با قفل — چند کاربر همزمان امن است."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}

    # ------------------------------------------------------------------
    def create(
        self,
        *,
        url: str,
        platform: str,
        title: str,
        duration: int | None,
        heights: list[int],
        info: dict[str, Any] | None = None,
    ) -> Job:
        job_id = secrets.token_hex(4)  # ۸ کاراکتر — داخل ۶۴ بایت جا می‌شود
        job = Job(
            job_id=job_id,
            url=url,
            platform=platform,
            title=title,
            duration=duration,
            heights=heights,
            info=info or {},
        )
        with self._lock:
            self._purge_locked()
            self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.expired:
                self._jobs.pop(job_id, None)
                return None
            return job

    def drop(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.running)

    def _purge_locked(self) -> None:
        stale = [k for k, j in self._jobs.items() if j.expired]
        for key in stale:
            self._jobs.pop(key, None)


# نمونه‌ی سراسری — در bot_data هم گذاشته می‌شود تا تست‌پذیر بماند
registry = JobRegistry()


# ==================================================================
# messages.py
# ==================================================================
# ----------------------------------------------------------------------
# هویت بصری
# ----------------------------------------------------------------------
BRAND = "🎬 <b>KING Downloader</b>"
DIVIDER = "━━━━━━━━━━━━━━━━━━"
THIN = "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄"

# ----------------------------------------------------------------------
# برچسب دکمه‌ها
# ----------------------------------------------------------------------
BTN_HELP = "📖 راهنما"
BTN_PLATFORMS = "🌐 پلتفرم‌ها"
BTN_STATUS = "📊 وضعیت"
BTN_CANCEL = "✖️ لغو"
BTN_BACK = "◀️ بازگشت"
BTN_MAIN = "🏠 منوی اصلی"
BTN_AGAIN = "🔁 دانلود دیگر"
BTN_BEST = "⭐ بهترین کیفیت"
BTN_AUDIO = "🎵 فقط صدا (MP3)"
BTN_QUALITY = "🎞 {height}p"
BTN_YT = "▶️ یوتیوب"
BTN_IG = "📸 اینستاگرام"
BTN_TT = "🎵 تیک‌تاک"
BTN_WEBAPP = "✨ باز کردن Mini App"

# ----------------------------------------------------------------------
# خوش‌آمد و راهنما
# ----------------------------------------------------------------------
WELCOME = (
    f"{BRAND}\n"
    f"{DIVIDER}\n"
    "<blockquote>سلام 👋\n"
    "من ویدیو را از لینک دانلود می‌کنم و همین‌جا برایت می‌فرستم.</blockquote>\n\n"
    "🔗 <b>لینک را بفرست</b> — یا از دکمه‌های زیر استفاده کن."
)

# وقتی Mini App تنظیم شده، این نسخه نمایش داده می‌شود
WELCOME_WEBAPP = (
    f"{BRAND}\n"
    f"{DIVIDER}\n"
    "<blockquote>سلام 👋\n"
    "دو راه داری: لینک را همین‌جا بفرست، یا Mini App را باز کن و با "
    "ظاهر شیشه‌ای و انتخاب کیفیت دانلود کن.</blockquote>\n\n"
    "✨ <b>Mini App</b> — تجربه‌ی کامل با دکمه‌های شیشه‌ای\n"
    "🔗 <b>یا لینک را بفرست</b> — سریع‌ترین راه"
)

HELP = (
    f"{BRAND}\n"
    f"{DIVIDER}\n"
    "📖 <b>راهنما</b>\n\n"
    "<blockquote><b>چطور استفاده کنم؟</b>\n"
    "۱. لینک ویدیو را بفرست\n"
    "۲. کیفیت را از دکمه‌ها انتخاب کن\n"
    "۳. فایل را همین‌جا دریافت کن</blockquote>\n\n"
    "🌐 <b>سایت‌های پشتیبانی‌شده</b>\n"
    "• یوتیوب — youtube.com، youtu.be\n"
    "• اینستاگرام — instagram.com (پست، ریلز، IGTV)\n"
    "• تیک‌تاک — tiktok.com\n\n"
    f"{THIN}\n"
    "⚙️ <b>محدودیت‌ها</b>\n"
    "• حداکثر حجم: <b>{limit}</b>\n"
    "• حداکثر مدت: <b>{duration}</b>\n"
    "• ویدیوی خصوصی یا حذف‌شده دانلود نمی‌شود"
)

PLATFORMS = (
    f"{BRAND}\n"
    f"{DIVIDER}\n"
    "🌐 <b>پلتفرم‌های پشتیبانی‌شده</b>\n\n"
    "<blockquote>▶️ <b>یوتیوب</b>\n"
    "youtube.com · youtu.be · m.youtube.com\n\n"
    "📸 <b>اینستاگرام</b>\n"
    "instagram.com — پست، ریلز، IGTV\n\n"
    "🎵 <b>تیک‌تاک</b>\n"
    "tiktok.com · vm.tiktok.com</blockquote>\n\n"
    "کافی است لینک را کپی کنی و همین‌جا بفرستی."
)

STATUS = (
    f"{BRAND}\n"
    f"{DIVIDER}\n"
    "📊 <b>وضعیت ربات</b>\n\n"
    "<blockquote>⏱ مدت فعالیت: <b>{uptime}</b>\n"
    "⚡️ دانلودهای فعال: <b>{active}</b>\n"
    "📦 سقف ارسال: <b>{limit}</b>\n"
    "🖥 حالت ارسال: <b>{mode}</b></blockquote>\n\n"
    "همه‌چیز آماده است ✅"
)

# ----------------------------------------------------------------------
# کارت ویدیو
# ----------------------------------------------------------------------
CARD = (
    f"{BRAND}\n"
    f"{DIVIDER}\n"
    "<blockquote>🎞 <a href=\"{url}\">{title}</a>\n"
    "🌐 {platform}  ·  ⏱ {duration}  ·  📦 {size}</blockquote>\n\n"
    "🎚 <b>کیفیت را انتخاب کن:</b>"
)

CARD_NO_SIZE = (
    f"{BRAND}\n"
    f"{DIVIDER}\n"
    "<blockquote>🎞 <a href=\"{url}\">{title}</a>\n"
    "🌐 {platform}  ·  ⏱ {duration}</blockquote>\n\n"
    "🎚 <b>کیفیت را انتخاب کن:</b>"
)

# ----------------------------------------------------------------------
# وضعیت‌ها
# ----------------------------------------------------------------------
PROBING = f"{BRAND}\n{DIVIDER}\n🔍 در حال بررسی لینک..."

DOWNLOADING_PLAIN = (
    f"{BRAND}\n"
    f"{DIVIDER}\n"
    "⏳ <b>در حال دانلود از {platform}...</b>\n\n"
    "<blockquote>برای لغو، دکمه‌ی پایین را بزن.</blockquote>"
)

DOWNLOADING = (
    f"{BRAND}\n"
    f"{DIVIDER}\n"
    "⏳ <b>در حال دانلود از {platform}...</b>\n\n"
    "<blockquote><code>{bar}</code>\n"
    "📥 {done} از {total}  ·  ⚡️ {speed}</blockquote>"
)

PROCESSING = (
    f"{BRAND}\n"
    f"{DIVIDER}\n"
    "⚙️ <b>دانلود تمام شد</b>\n\n"
    "<blockquote>در حال آماده‌سازی فایل...</blockquote>"
)

UPLOADING = (
    f"{BRAND}\n"
    f"{DIVIDER}\n"
    "📤 <b>در حال ارسال به تلگرام...</b>\n\n"
    "<blockquote>فایل بزرگ است، کمی صبر کن.</blockquote>"
)

CANCELLING = (
    f"{BRAND}\n"
    f"{DIVIDER}\n"
    "🛑 <b>در حال لغو...</b>"
)

CANCELLED = (
    f"{BRAND}\n"
    f"{DIVIDER}\n"
    "🛑 <b>دانلود لغو شد</b>\n\n"
    "<blockquote>فایل نیمه‌کاره پاک شد. لینک جدیدی بفرست.</blockquote>"
)

CANCEL_NOTHING = "🤷 چیزی برای لغو نیست — دانلودی در جریان نیست."

# ----------------------------------------------------------------------
# خطاها
# ----------------------------------------------------------------------
NO_LINK = (
    f"{BRAND}\n"
    f"{DIVIDER}\n"
    "❌ <b>لینکی در پیام پیدا نکردم</b>\n\n"
    "<blockquote>یک لینک ویدیو بفرست، مثلاً:\n"
    "<code>https://youtu.be/xxxxxxxxxxx</code></blockquote>"
)

UNSUPPORTED_SITE = (
    f"{BRAND}\n"
    f"{DIVIDER}\n"
    "❌ <b>این سایت پشتیبانی نمی‌شود</b>\n\n"
    "<blockquote>فقط این سه سایت قبول می‌شوند:\n"
    "▶️ یوتیوب\n"
    "📸 اینستاگرام\n"
    "🎵 تیک‌تاک</blockquote>"
)

INVALID_LINK = (
    f"{BRAND}\n"
    f"{DIVIDER}\n"
    "❌ <b>این لینک معتبر نیست</b>\n\n"
    "<blockquote>آدرس کامل ویدیو را بفرست.</blockquote>"
)

PRIVATE_VIDEO = (
    f"{BRAND}\n"
    f"{DIVIDER}\n"
    "🔒 <b>ویدیوی خصوصی</b>\n\n"
    "<blockquote>این ویدیو خصوصی است یا دسترسی به آن بسته شده.\n"
    "فقط ویدیوهای عمومی قابل دانلود هستند.</blockquote>"
)

VIDEO_UNAVAILABLE = (
    f"{BRAND}\n"
    f"{DIVIDER}\n"
    "❌ <b>ویدیو در دسترس نیست</b>\n\n"
    "<blockquote>ممکن است حذف شده باشد یا در کشور شما محدود شده باشد.</blockquote>"
)

LOGIN_REQUIRED = (
    f"{BRAND}\n"
    f"{DIVIDER}\n"
    "🔑 <b>این محتوا نیاز به ورود دارد</b>\n\n"
    "<blockquote>برای اینستاگرام، ادمین ربات باید فایل کوکی را تنظیم کند.</blockquote>"
)

BOT_CHECK = (
    f"{BRAND}\n"
    f"{DIVIDER}\n"
    "🤖 <b>یوتیوب موقتاً محدود کرده</b>\n\n"
    "<blockquote>یوتیوب درخواست‌های سرور را محدود کرده است.\n"
    "چند دقیقه بعد دوباره امتحان کن.</blockquote>"
)

NOT_FOUND = (
    f"{BRAND}\n"
    f"{DIVIDER}\n"
    "❌ <b>محتوایی پیدا نشد</b>\n\n"
    "<blockquote>لینک را بررسی کن.</blockquote>"
)

NETWORK_ERROR = (
    f"{BRAND}\n"
    f"{DIVIDER}\n"
    "🌐 <b>خطا در اتصال</b>\n\n"
    "<blockquote>اتصال سرور را بررسی کن و دوباره امتحان کن.</blockquote>"
)

TOO_LONG = (
    f"{BRAND}\n"
    f"{DIVIDER}\n"
    "⏱ <b>ویدیو طولانی‌تر از حد مجاز است</b>\n\n"
    "<blockquote>حداکثر مدت: <b>{limit}</b></blockquote>"
)

TOO_LARGE = (
    f"{BRAND}\n"
    f"{DIVIDER}\n"
    "📦 <b>حجم فایل زیاد است</b>\n\n"
    "<blockquote>حجم ویدیو: <b>{size}</b>\n"
    "حداکثر مجاز: <b>{limit}</b></blockquote>\n\n"
    "💡 کیفیت پایین‌تر را انتخاب کن، یا از ادمین بخواه "
    "«سرور محلی Bot API» را فعال کند (تا ۲ گیگابایت)."
)

TOO_LARGE_NO_LOCAL = (
    f"{BRAND}\n"
    f"{DIVIDER}\n"
    "📦 <b>حجم فایل زیاد است</b>\n\n"
    "<blockquote>حجم ویدیو: <b>{size}</b>\n"
    "حداکثر مجاز: <b>{limit}</b></blockquote>\n\n"
    "💡 تلگرام برای ربات‌ها سقف ۵۰ مگابایت دارد.\n"
    "برای فایل‌های بزرگ‌تر، ادمین باید سرور محلی Bot API را راه‌اندازی کند "
    "(راهنما در README)."
)

SEND_FAILED = (
    f"{BRAND}\n"
    f"{DIVIDER}\n"
    "❌ <b>ارسال فایل ناموفق بود</b>\n\n"
    "<blockquote>ممکن است فایل خراب باشد یا اتصال قطع شده باشد.</blockquote>"
)

UNKNOWN_ERROR = (
    f"{BRAND}\n"
    f"{DIVIDER}\n"
    "❌ <b>خطای پیش‌بینی‌نشده</b>\n\n"
    "<blockquote>دوباره امتحان کن. اگر تکرار شد، به ادمین بگو.</blockquote>"
)

RATE_LIMITED = "⏳ کمی صبر کن، درخواست‌های زیادی فرستاده‌ای."

JOB_EXPIRED = "⌛️ این دکمه منقضی شده. لینک را دوباره بفرست."

# ----------------------------------------------------------------------
# کپشن فایل
# ----------------------------------------------------------------------
CAPTION = (
    "🎬 <b>{title}</b>\n"
    f"{THIN}\n"
    "🌐 {platform}  ·  📦 {size}"
)

CAPTION_AUDIO = (
    "🎵 <b>{title}</b>\n"
    f"{THIN}\n"
    "🌐 {platform}  ·  📦 {size}"
)

# ----------------------------------------------------------------------
# نام پلتفرم‌ها
# ----------------------------------------------------------------------
PLATFORM_NAMES = {
    "youtube": "یوتیوب",
    "instagram": "اینستاگرام",
    "tiktok": "تیک‌تاک",
}


# ==================================================================
# keyboards.py
# ==================================================================
# ----------------------------------------------------------------------
# شناسه‌ی عمل‌ها — در هندلرها هم از همین‌ها استفاده می‌شود
# ----------------------------------------------------------------------
ACT_MENU = "menu"
ACT_HELP = "help"
ACT_PLATFORMS = "plat"
ACT_STATUS = "stat"
ACT_QUALITY = "q"
ACT_AUDIO = "a"
ACT_CANCEL = "x"
ACT_AGAIN = "again"
ACT_WEBAPP = "webapp"

# ارتفاع‌های پیشنهادی برای انتخاب کیفیت (فقط آن‌هایی که ویدیو دارد)
QUALITY_CHOICES = (360, 480, 720, 1080)


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def _webapp_btn(text: str, url: str) -> InlineKeyboardButton:
    """دکمه‌ای که Mini App را باز می‌کند.

    تلگرام فقط آدرس HTTPS را می‌پذیرد؛ اگر آدرس تنظیم نشده باشد،
    این دکمه ساخته نمی‌شود.
    """
    return InlineKeyboardButton(text, web_app=WebAppInfo(url=url))


# ----------------------------------------------------------------------
# منوی اصلی
# ----------------------------------------------------------------------
def main_menu(webapp_url: str = "") -> InlineKeyboardMarkup:
    """منوی اصلی: دکمه‌ی Mini App (اگر تنظیم شده) + دو ردیف دو‌ستونی + یک ردیف تمام‌عرض.

    چیدمان دو‌ستونی همان چیزی است که حس «کارت شیشه‌ای» می‌دهد؛
    تلگرام دکمه‌های هم‌ردیف را با فاصله‌ی یکسان می‌چیند.
    """
    rows: list[list[InlineKeyboardButton]] = []

    # دکمه‌ی Mini App — برجسته‌ترین انتخاب، تمام‌عرض و اول
    if webapp_url:
        rows.append([_webapp_btn(BTN_WEBAPP, webapp_url)])

    rows.extend(
        [
            [
                _btn(BTN_HELP, f"{ACT_HELP}:"),
                _btn(BTN_PLATFORMS, f"{ACT_PLATFORMS}:"),
            ],
            [
                _btn(BTN_STATUS, f"{ACT_STATUS}:"),
                _btn(BTN_CANCEL, f"{ACT_CANCEL}:"),
            ],
            [_btn(BTN_MAIN, f"{ACT_MENU}:")],
        ]
    )

    return InlineKeyboardMarkup(rows)


def back_menu() -> InlineKeyboardMarkup:
    """برای صفحه‌های راهنما/پلتفرم‌ها/وضعیت."""
    return InlineKeyboardMarkup(
        [[_btn(BTN_BACK, f"{ACT_MENU}:")]]
    )


# ----------------------------------------------------------------------
# انتخاب کیفیت
# ----------------------------------------------------------------------
def quality_menu(
    job_id: str,
    heights: list[int],
    *,
    has_audio: bool = True,
) -> InlineKeyboardMarkup:
    """دکمه‌های کیفیت + صدا + لغو.

    heights: ارتفاع‌های واقعاً موجود در این ویدیو (نزولی).
    اگر خالی باشد، فقط «بهترین کیفیت» و صدا نشان داده می‌شود.
    """
    rows: list[list[InlineKeyboardButton]] = []

    # ردیف اول: بهترین کیفیت (تمام‌عرض — برجسته‌ترین انتخاب)
    rows.append([_btn(BTN_BEST, f"{ACT_QUALITY}:{job_id}:best")])

    # ردیف‌های بعدی: کیفیت‌های مشخص، دو‌ستونی
    picked = [h for h in QUALITY_CHOICES if h in heights]
    if not picked:
        picked = sorted(heights, reverse=True)[:4]

    pair: list[InlineKeyboardButton] = []
    for height in picked:
        pair.append(
            _btn(BTN_QUALITY.format(height=height), f"{ACT_QUALITY}:{job_id}:{height}")
        )
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)

    # صدا
    if has_audio:
        rows.append([_btn(BTN_AUDIO, f"{ACT_AUDIO}:{job_id}")])

    # لغو
    rows.append([_btn(BTN_CANCEL, f"{ACT_CANCEL}:{job_id}")])

    return InlineKeyboardMarkup(rows)


# ----------------------------------------------------------------------
# در حین دانلود
# ----------------------------------------------------------------------
def downloading_menu(job_id: str) -> InlineKeyboardMarkup:
    """فقط یک دکمه: لغو."""
    return InlineKeyboardMarkup(
        [[_btn(BTN_CANCEL, f"{ACT_CANCEL}:{job_id}")]]
    )


# ----------------------------------------------------------------------
# بعد از پایان
# ----------------------------------------------------------------------
def after_download_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [_btn(BTN_AGAIN, f"{ACT_MENU}:")],
            [_btn(BTN_MAIN, f"{ACT_MENU}:")],
        ]
    )


# ----------------------------------------------------------------------
# تجزیه‌ی callback_data
# ----------------------------------------------------------------------
def parse_callback(data: str) -> tuple[str, str, str]:
    """«q:ab12cd:720» → ("q", "ab12cd", "720")

    بخش‌های غایب با رشته‌ی خالی پر می‌شوند تا هندلر لازم نباشد
    None-check کند.
    """
    parts = (data or "").split(":")
    action = parts[0] if parts else ""
    job_id = parts[1] if len(parts) > 1 else ""
    value = parts[2] if len(parts) > 2 else ""
    return action, job_id, value
