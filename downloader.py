"""موتور دانلود و تشخیص لینک (لینک‌ها + خطاها + موتور yt-dlp)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse
from enum import Enum
import asyncio
import logging
import shutil
import threading
from pathlib import Path
from config import Settings
from utils import ProgressTracker

# ==================================================================
# links.py
# ==================================================================
# دامنه‌های هر پلتفرم (بدون www.)
YOUTUBE_HOSTS = {
    "youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
}
INSTAGRAM_HOSTS = {"instagram.com", "instagr.am", "ddinstagram.com"}
TIKTOK_HOSTS = {"tiktok.com", "vm.tiktok.com", "vt.tiktok.com"}

# الگوی لینک داخل متن (کاربر ممکن است متن + لینک بفرستد)
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


@dataclass(frozen=True)
class LinkInfo:
    url: str
    platform: str  # youtube | instagram | tiktok


class UnsupportedSite(ValueError):
    """دامنه در فهرست پشتیبانی نیست."""


class InvalidLink(ValueError):
    """لینک ساختار درستی ندارد."""


def extract_url(text: str) -> str | None:
    """اولین لینک http(s) را از متن بیرون می‌کشد."""
    match = URL_RE.search(text or "")
    return match.group(0).rstrip(".,;:!؟?»«)") if match else None


def _normalize_host(host: str) -> str:
    host = host.lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def detect_platform(url: str) -> str:
    """نام پلتفرم را برمی‌گرداند یا UnsupportedSite می‌دهد."""
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise InvalidLink(url) from exc

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise InvalidLink(url)

    host = _normalize_host(parsed.hostname or "")

    if host in YOUTUBE_HOSTS:
        return "youtube"
    if host in INSTAGRAM_HOSTS:
        return "instagram"
    if host in TIKTOK_HOSTS:
        return "tiktok"

    # زیردامنه‌های شناخته‌شده (مثلاً m.tiktok.com)
    for known, name in (
        (YOUTUBE_HOSTS, "youtube"),
        (INSTAGRAM_HOSTS, "instagram"),
        (TIKTOK_HOSTS, "tiktok"),
    ):
        if any(host.endswith("." + h) for h in known):
            return name

    raise UnsupportedSite(host)


def parse_link(text: str) -> LinkInfo:
    """متن کاربر → LinkInfo. خطاها را با پیام مناسب پرتاب می‌کند."""
    url = extract_url(text)
    if not url:
        raise InvalidLink("no-url")
    return LinkInfo(url=url, platform=detect_platform(url))


# ==================================================================
# errors.py
# ==================================================================
class DownloadErrorKind(str, Enum):
    PRIVATE = "private"
    UNAVAILABLE = "unavailable"
    LOGIN_REQUIRED = "login_required"
    BOT_CHECK = "bot_check"
    NOT_FOUND = "not_found"
    NETWORK = "network"
    TOO_LONG = "too_long"
    TOO_LARGE = "too_large"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class DownloadError(Exception):
    """خطای قابل‌نمایش به کاربر."""

    def __init__(self, kind: DownloadErrorKind, detail: str = "") -> None:
        super().__init__(detail or kind.value)
        self.kind = kind
        self.detail = detail


# الگوهای متن خطای yt-dlp → نوع خطا
_PATTERNS: tuple[tuple[tuple[str, ...], DownloadErrorKind], ...] = (
    (
        (
            "private video",
            "this video is private",
            "private account",
            "only available for followers",
            "requested content is not available",
        ),
        DownloadErrorKind.PRIVATE,
    ),
    (
        (
            "sign in to confirm",
            "confirm you're not a bot",
            "confirm you’re not a bot",
            "cookies-from-browser",
            "login required",
            "requires authentication",
            "rate-limit reached",
            "too many requests",
            "http error 429",
        ),
        DownloadErrorKind.BOT_CHECK,
    ),
    (
        (
            "login required",
            "you need to log in",
            "authentication required",
            "cookies",
        ),
        DownloadErrorKind.LOGIN_REQUIRED,
    ),
    (
        (
            "video unavailable",
            "video is unavailable",
            "this video is not available",
            "has been removed",
            "no longer available",
            "account has been terminated",
            "content isn't available",
            "content is not available",
            "requested format is not available",
        ),
        DownloadErrorKind.UNAVAILABLE,
    ),
    (
        (
            "404",
            "not found",
            "does not exist",
            "unsupported url",
            "no video formats found",
        ),
        DownloadErrorKind.NOT_FOUND,
    ),
    (
        (
            "timed out",
            "connection",
            "network is unreachable",
            "temporary failure",
            "ssl",
            "getaddrinfo",
            "http error 5",
        ),
        DownloadErrorKind.NETWORK,
    ),
)


def classify(message: str) -> DownloadErrorKind:
    """متن خطای خام yt-dlp را به نوع خطا تبدیل می‌کند."""
    lowered = (message or "").lower()
    for needles, kind in _PATTERNS:
        if any(needle in lowered for needle in needles):
            return kind
    return DownloadErrorKind.UNKNOWN


# ==================================================================
# engine.py
# ==================================================================
logger = logging.getLogger(__name__)


class DownloadCancelled(Exception):
    """کاربر دکمه‌ی لغو را زد."""


def ffmpeg_available() -> bool:
    """آیا ffmpeg نصب است؟

    بدون ffmpeg نمی‌توان ویدیو و صدا را merge کرد، پس باید
    فرمت‌های «تک‌فایلی» (که صدا داخلشان است) را انتخاب کنیم.
    """
    return shutil.which("ffmpeg") is not None


@dataclass
class DownloadResult:
    path: Path
    title: str
    duration: int | None
    filesize: int
    platform: str
    width: int | None = None
    height: int | None = None
    is_audio: bool = False


# ----------------------------------------------------------------------
# استخراج ارتفاع‌های موجود از اطلاعات probe
# ----------------------------------------------------------------------
def available_heights(info: dict) -> list[int]:
    """ارتفاع‌های واقعاً موجود در این ویدیو، نزولی و بدون تکرار.

    yt-dlp در formats فهرست همه‌ی فرمت‌ها را می‌دهد؛ آن‌هایی که
    height دارند و ویدیویی هستند را برمی‌داریم.
    """
    heights: set[int] = set()

    for fmt in info.get("formats") or []:
        if not isinstance(fmt, dict):
            continue
        # فقط فرمت‌هایی که هم ویدیو دارند هم صدا یا ویدیوی خالص
        vcodec = fmt.get("vcodec")
        if vcodec in (None, "none"):
            continue
        height = fmt.get("height")
        if isinstance(height, int) and height > 0:
            heights.add(height)

    # اگر formats نبود، از خود info استفاده کن
    if not heights:
        height = info.get("height")
        if isinstance(height, int) and height > 0:
            heights.add(height)

    return sorted(heights, reverse=True)


class Downloader:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ------------------------------------------------------------------
    # ساخت گزینه‌های yt-dlp
    # ------------------------------------------------------------------
    def _base_opts(
        self,
        out_dir: Path,
        tracker: ProgressTracker,
        cancel_event: threading.Event | None = None,
    ) -> dict:
        opts: dict = {
            "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "progress_hooks": [self._make_hook(tracker, cancel_event)],
            "retries": 3,
            "fragment_retries": 3,
            "socket_timeout": 30,
            "restrictfilenames": True,
            "concurrent_fragment_downloads": 4,
            # فایل‌های بزرگ را از قبل رد نکن؛ خودمان بررسی می‌کنیم
            "max_filesize": None,
        }

        if self.settings.cookies_file:
            opts["cookiefile"] = str(self.settings.cookies_file)

        return opts

    @staticmethod
    def _make_hook(tracker: ProgressTracker, cancel_event: threading.Event | None):
        """هوک پیشرفت + بررسی لغو.

        yt-dlp اگر هوک استثنا پرتاب کند، دانلود را متوقف می‌کند.
        این تنها راه تمیز برای لغو وسط دانلود است.
        """

        def hook(data: dict) -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled("cancelled by user")
            tracker.hook(data)

        return hook

    # ------------------------------------------------------------------
    # انتخاب فرمت
    # ------------------------------------------------------------------
    def _format_selector(
        self,
        height: int | None = None,
        *,
        audio_only: bool = False,
    ) -> str:
        """سلکتور فرمت بر اساس کیفیت درخواستی.

        height=None یعنی «بهترین کیفیت تا سقف تنظیمات».
        audio_only=True یعنی فقط صدا (بهترین m4a، یا هر صدای موجود).
        """
        if audio_only:
            return "bestaudio[ext=m4a]/bestaudio/best"

        h = height or self.settings.max_height

        if not ffmpeg_available():
            logger.warning(
                "ffmpeg پیدا نشد — فقط فرمت‌های تک‌فایلی دانلود می‌شوند "
                "(کیفیت ممکن است پایین‌تر باشد)."
            )
            # 18 = 360p mp4 با صدا؛ کلاسیک‌ترین فرمت تک‌فایلی یوتیوب
            return (
                f"18/best[height<={h}][ext=mp4]/"
                f"best[height<={h}]/"
                f"best[ext=mp4]/"
                f"best/worst"
            )

        return (
            f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={h}]+bestaudio/"
            f"best[height<={h}][ext=mp4]/"
            f"best[height<={h}]/best"
        )

    # ------------------------------------------------------------------
    # استخراج اطلاعات بدون دانلود
    # ------------------------------------------------------------------
    def _probe(self, url: str) -> dict:
        import yt_dlp

        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "socket_timeout": 30,
        }
        if self.settings.cookies_file:
            opts["cookiefile"] = str(self.settings.cookies_file)

        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    async def probe(self, url: str) -> dict:
        """اطلاعات ویدیو را در ترد جدا می‌گیرد (بدون بلاک‌کردن ربات)."""
        try:
            return await asyncio.to_thread(self._probe, url)
        except Exception as exc:  # noqa: BLE001 - yt-dlp خطاهای متنوعی دارد
            raise self._to_download_error(exc) from exc

    # ------------------------------------------------------------------
    # دانلود
    # ------------------------------------------------------------------
    def _download_sync(
        self,
        url: str,
        out_dir: Path,
        tracker: ProgressTracker,
        height: int | None,
        audio_only: bool,
        cancel_event: threading.Event | None,
    ) -> DownloadResult:
        import yt_dlp

        opts = self._base_opts(out_dir, tracker, cancel_event)
        opts["format"] = self._format_selector(height, audio_only=audio_only)
        if not audio_only:
            opts["merge_output_format"] = "mp4"

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except DownloadCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            # اگر وسط کار لغو شده، خطای yt-dlp را نادیده بگیر
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled("cancelled by user") from exc
            raise

        if info is None:
            raise DownloadError(DownloadErrorKind.NOT_FOUND)

        # پلی‌لیست: اولین ورودی را بردار
        if info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            if not entries:
                raise DownloadError(DownloadErrorKind.NOT_FOUND)
            info = entries[0]

        path = self._resolve_output_path(info, out_dir, audio_only)
        if path is None:
            raise DownloadError(DownloadErrorKind.UNKNOWN, "فایل خروجی پیدا نشد")

        return DownloadResult(
            path=path,
            title=(info.get("title") or "ویدیو").strip()[:200],
            duration=info.get("duration"),
            filesize=path.stat().st_size,
            platform=(info.get("extractor_key") or "").lower(),
            width=info.get("width"),
            height=info.get("height"),
            is_audio=audio_only,
        )

    def _resolve_output_path(
        self, info: dict, out_dir: Path, audio_only: bool = False
    ) -> Path | None:
        """مسیر فایل نهایی را پیدا می‌کند.

        yt-dlp مسیر را در requested_downloads می‌گذارد، ولی بعد از
        merge ممکن است پسوند عوض شود؛ پس هر دو را بررسی می‌کنیم.
        """
        candidates: list[Path] = []

        for item in info.get("requested_downloads") or []:
            for key in ("filepath", "_filename"):
                if item.get(key):
                    candidates.append(Path(item[key]))

        for key in ("filepath", "_filename"):
            if info.get(key):
                candidates.append(Path(info[key]))

        for candidate in candidates:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate

        # آخرین راه: بزرگ‌ترین فایل رسانه‌ای داخل پوشه
        if audio_only:
            exts = {".m4a", ".mp3", ".opus", ".webm", ".ogg", ".aac"}
        else:
            exts = {".mp4", ".mkv", ".webm", ".mov"}

        media = [
            p for p in out_dir.iterdir() if p.is_file() and p.suffix.lower() in exts
        ]
        if media:
            return max(media, key=lambda p: p.stat().st_size)

        return None

    async def download(
        self,
        url: str,
        out_dir: Path,
        tracker: ProgressTracker,
        *,
        height: int | None = None,
        audio_only: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> DownloadResult:
        """دانلود در ترد جدا تا event loop ربات قفل نشود."""
        try:
            return await asyncio.to_thread(
                self._download_sync,
                url,
                out_dir,
                tracker,
                height,
                audio_only,
                cancel_event,
            )
        except DownloadCancelled:
            raise
        except DownloadError:
            raise
        except Exception as exc:  # noqa: BLE001
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled("cancelled by user") from exc
            raise self._to_download_error(exc) from exc

    # ------------------------------------------------------------------
    # نگاشت خطا
    # ------------------------------------------------------------------
    @staticmethod
    def _to_download_error(exc: Exception) -> DownloadError:
        message = str(exc)
        kind = classify(message)
        logger.warning("خطای دانلود [%s]: %s", kind.value, message[:300])
        return DownloadError(kind, message)
