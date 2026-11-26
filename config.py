"""خواندن و اعتبارسنجی تنظیمات از متغیرهای محیطی."""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# سقف آپلود تلگرام از طریق api.telegram.org
CLOUD_UPLOAD_LIMIT = 50 * 1024 * 1024
# سقف آپلود با سرور محلی Bot API
LOCAL_UPLOAD_LIMIT = 2000 * 1024 * 1024


class ConfigError(RuntimeError):
    """تنظیمات ناقص یا نامعتبر است."""


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"مقدار {name} باید عدد باشد، نه «{raw}»") from exc


@dataclass(frozen=True)
class Settings:
    bot_token: str
    local_api_url: str | None
    download_dir: Path
    max_filesize_mb: int
    max_duration_sec: int
    max_height: int
    cookies_file: Path | None
    log_level: str
    # حداکثر دانلود همزمان (Railway رایگان/Trial رم کمی دارد)
    max_concurrent: int = 2
    # پروکسی اختیاری برای دور زدن بلاک یوتیوب/اینستاگرام روی IP دیتاسنتر
    proxy_url: str | None = None
    # --- ارائه‌دهنده‌های دانلود (به همین ترتیب امتحان می‌شوند) ---
    providers: tuple[str, ...] = ("cobalt", "tikwm", "piped", "invidious", "ytdlp")
    cobalt_urls: tuple[str, ...] = ()
    cobalt_api_key: str = ""
    piped_urls: tuple[str, ...] = ()
    invidious_urls: tuple[str, ...] = ()
    # --- Mini App ---
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    webapp_url: str = ""

    @property
    def upload_limit(self) -> int:
        """سقف واقعی ارسال فایل بر اساس حالت اجرا."""
        if self.local_api_url:
            return min(self.max_filesize_mb * 1024 * 1024, LOCAL_UPLOAD_LIMIT)
        return min(self.max_filesize_mb * 1024 * 1024, CLOUD_UPLOAD_LIMIT)

    @property
    def has_cookies(self) -> bool:
        return self.cookies_file is not None

    @property
    def has_api_providers(self) -> bool:
        """آیا غیر از yt-dlp، ارائه‌دهنده‌ی دیگری فعال است؟"""
        return any(p != "ytdlp" and self.provider_enabled(p) for p in self.providers)

    def provider_enabled(self, name: str) -> bool:
        if name == "cobalt":
            return bool(self.cobalt_urls)
        if name == "piped":
            return bool(self.piped_urls)
        if name == "invidious":
            return bool(self.invidious_urls)
        return name in ("tikwm", "ytdlp")

    @property
    def uses_local_api(self) -> bool:
        return bool(self.local_api_url)

    @property
    def has_webapp(self) -> bool:
        """آیا Mini App تنظیم شده است؟"""
        return bool(self.webapp_url)


KNOWN_PROVIDERS = ("cobalt", "tikwm", "piped", "invidious", "ytdlp")


def _list(name: str, default: str = "") -> tuple[str, ...]:
    """لیست جداشده با ویرگول از متغیر محیطی (آدرس‌ها بدون «/» انتهایی)."""
    raw = os.getenv(name)
    raw = default if raw is None else raw
    items = [x.strip().rstrip("/") for x in raw.replace("\n", ",").split(",")]
    return tuple(x for x in items if x)


def _providers() -> tuple[str, ...]:
    raw = os.getenv("PROVIDERS", "").strip()
    if not raw:
        return KNOWN_PROVIDERS
    names = tuple(x.strip().lower() for x in raw.split(",") if x.strip())
    unknown = [n for n in names if n not in KNOWN_PROVIDERS]
    if unknown:
        raise ConfigError(
            f"PROVIDERS شامل نام ناشناخته است: {', '.join(unknown)}\n"
            f"نام‌های مجاز: {', '.join(KNOWN_PROVIDERS)}"
        )
    return names


def _resolve_cookies(download_dir: Path) -> Path | None:
    """مسیر فایل کوکی را برمی‌گرداند.

    دو راه:
      - COOKIES_FILE: مسیر فایل روی دیسک (اجرای محلی / Docker با volume)
      - COOKIES_B64: محتوای cookies.txt به‌صورت base64 (Railway — فایل
        نمی‌شود mount کرد، پس متن را در متغیر می‌گذاریم)
    """
    b64 = "".join(os.getenv("COOKIES_B64", "").split())
    if b64:
        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ConfigError("COOKIES_B64 base64 معتبر نیست.") from exc
        target = download_dir.parent / ".cookies.txt"
        try:
            target.write_bytes(data)
            target.chmod(0o600)
        except OSError:
            # اگر کنار پوشه‌ی دانلود قابل نوشتن نبود، در /tmp می‌نویسیم
            target = Path("/tmp/cookies.txt")
            target.write_bytes(data)
            target.chmod(0o600)
        return target

    raw = os.getenv("COOKIES_FILE", "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_file():
        raise ConfigError(f"فایل کوکی پیدا نشد: {path}")
    return path


def get_settings() -> Settings:
    load_dotenv()

    token = ""
    for key in ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN", "TOKEN"):
        # فاصله، «"» و «'» اضافه‌ای که موقع کپی/پیست وارد می‌شود حذف می‌شود
        token = os.getenv(key, "").strip().strip("\"'").strip()
        if token:
            break
    if not token:
        raise ConfigError(
            "BOT_TOKEN تنظیم نشده است.\n"
            "روی Railway: سرویس را باز کنید ← تب Variables ← New Variable\n"
            "نام: BOT_TOKEN  و مقدار: توکن BotFather (مثل 123456789:AAE...)\n"
            "بعد از ذخیره، Deploy را دوباره انجام دهید.\n"
            "اجرای محلی: فایل .env بسازید و توکن را در آن بگذارید."
        )
    if ":" not in token:
        raise ConfigError("BOT_TOKEN شکل درستی ندارد (باید شامل «:» باشد).")

    local_api = os.getenv("LOCAL_API_URL", "").strip() or None
    if local_api and not local_api.startswith(("http://", "https://")):
        raise ConfigError("LOCAL_API_URL باید با http:// یا https:// شروع شود.")

    download_dir = Path(os.getenv("DOWNLOAD_DIR", "./downloads")).expanduser()

    cookies = _resolve_cookies(download_dir)

    webapp_url = os.getenv("WEBAPP_URL", "").strip()
    if not webapp_url:
        # روی Railway دامنه‌ی عمومی خودکار تنظیم می‌شود
        railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
        if railway_domain:
            webapp_url = f"https://{railway_domain}"
    if webapp_url and not webapp_url.startswith("https://"):
        raise ConfigError(
            "WEBAPP_URL باید با https:// شروع شود — تلگرام فقط آدرس HTTPS را "
            "برای Mini App می‌پذیرد."
        )

    return Settings(
        bot_token=token,
        local_api_url=local_api,
        download_dir=download_dir,
        max_filesize_mb=_int("MAX_FILESIZE_MB", 50),
        max_duration_sec=_int("MAX_DURATION_SEC", 1800),
        max_height=_int("MAX_HEIGHT", 1080),
        cookies_file=cookies,
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        max_concurrent=max(1, _int("MAX_CONCURRENT", 2)),
        proxy_url=os.getenv("PROXY_URL", "").strip() or None,
        providers=_providers(),
        cobalt_urls=_list("COBALT_API_URL"),
        cobalt_api_key=os.getenv("COBALT_API_KEY", "").strip(),
        # نمونه‌های عمومی؛ ممکن است از کار افتاده باشند. با متغیر خودتان عوضشان کنید.
        piped_urls=_list("PIPED_URLS", "https://pipedapi.kavin.rocks"),
        invidious_urls=_list("INVIDIOUS_URLS", "https://inv.nadeko.net,https://yewtu.be"),
        web_host=os.getenv("WEB_HOST", "0.0.0.0").strip() or "0.0.0.0",
        # Railway پورت را در PORT می‌دهد؛ WEB_PORT برای اجرای محلی است
        web_port=_int("PORT", _int("WEB_PORT", 8080)),
        webapp_url=webapp_url.rstrip("/"),
    )
