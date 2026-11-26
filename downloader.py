"""موتور دانلود: تشخیص لینک + طبقه‌بندی خطا + موتور yt-dlp."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

from config import Settings
from utils import ProgressTracker

logger = logging.getLogger(__name__)

# ==================================================================
# لینک‌ها
# ==================================================================
YOUTUBE_HOSTS = {
    "youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
}
INSTAGRAM_HOSTS = {"instagram.com", "instagr.am", "ddinstagram.com"}
TIKTOK_HOSTS = {"tiktok.com", "vm.tiktok.com", "vt.tiktok.com"}

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
    return match.group(0).rstrip(".,;:!؟?»«)،") if match else None


def _normalize_host(host: str) -> str:
    host = host.lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def detect_platform(url: str) -> str:
    try:
        parsed = urlparse(url)
        host = _normalize_host(parsed.hostname or "")
    except ValueError as exc:
        raise InvalidLink(url) from exc

    if parsed.scheme not in ("http", "https") or not host:
        raise InvalidLink(url)

    for hosts, name in (
        (YOUTUBE_HOSTS, "youtube"),
        (INSTAGRAM_HOSTS, "instagram"),
        (TIKTOK_HOSTS, "tiktok"),
    ):
        if host in hosts or any(host.endswith("." + h) for h in hosts):
            return name

    raise UnsupportedSite(host)


def parse_link(text: str) -> LinkInfo:
    url = extract_url(text)
    if not url:
        raise InvalidLink("no-url")
    return LinkInfo(url=url, platform=detect_platform(url))


# ==================================================================
# خطاها
# ==================================================================
class DownloadErrorKind(str, Enum):
    PRIVATE = "private"
    UNAVAILABLE = "unavailable"
    LOGIN_REQUIRED = "login_required"
    BOT_CHECK = "bot_check"
    FORMAT = "format"
    NO_VIDEO = "no_video"
    LIVE = "live"
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


class DownloadCancelled(Exception):
    """کاربر دکمه‌ی لغو را زد."""


# ترتیب مهم است: اولین گروهی که تطبیق کند برنده است.
_PATTERNS: tuple[tuple[tuple[str, ...], DownloadErrorKind], ...] = (
    (
        (
            "sign in to confirm",
            "confirm you're not a bot",
            "confirm you’re not a bot",
            "not a bot",
            "http error 429",
            "too many requests",
            "rate-limit",
            "rate limit",
        ),
        DownloadErrorKind.BOT_CHECK,
    ),
    (
        (
            "private video",
            "this video is private",
            "private account",
            "only available for followers",
            "members-only",
            "members only",
        ),
        DownloadErrorKind.PRIVATE,
    ),
    (
        (
            "login required",
            "log in",
            "sign in",
            "authentication required",
            "requires authentication",
            "confirm your age",
            "age-restricted",
            "age restricted",
            "inappropriate for some users",
            "cookies",
            "empty media response",
            "not granting access",
        ),
        DownloadErrorKind.LOGIN_REQUIRED,
    ),
    (
        (
            "live event will begin",
            "premieres in",
            "this live event",
            "is a live stream",
        ),
        DownloadErrorKind.LIVE,
    ),
    (
        (
            "no video formats found",
            "there is no video in this post",
            "no video could be found",
            "no video in this",
        ),
        DownloadErrorKind.NO_VIDEO,
    ),
    (
        (
            "requested format is not available",
            "only images are available",
            "no formats found",
            "n challenge",
            "nsig extraction failed",
            "no playable formats",
        ),
        DownloadErrorKind.FORMAT,
    ),
    (
        (
            "video unavailable",
            "video is unavailable",
            "this video is not available",
            "not available in your country",
            "blocked it in your country",
            "has been removed",
            "no longer available",
            "account has been terminated",
            "content isn't available",
            "content is not available",
            "requested content is not available",
        ),
        DownloadErrorKind.UNAVAILABLE,
    ),
    (
        (
            "http error 404",
            "not found",
            "does not exist",
            "unsupported url",
        ),
        DownloadErrorKind.NOT_FOUND,
    ),
    (
        (
            "timed out",
            "timeout",
            "connection",
            "network is unreachable",
            "temporary failure",
            "ssl error",
            "certificate",
            "getaddrinfo",
            "http error 5",
        ),
        DownloadErrorKind.NETWORK,
    ),
)


def classify(message: str) -> DownloadErrorKind:
    lowered = (message or "").lower()
    for needles, kind in _PATTERNS:
        if any(needle in lowered for needle in needles):
            return kind
    return DownloadErrorKind.UNKNOWN


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def clean_detail(message: str, limit: int = 220) -> str:
    """متن خطای خام yt-dlp را برای نمایش تمیز می‌کند."""
    text = _ANSI_RE.sub("", message or "").replace("ERROR:", "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit]


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


# ==================================================================
# کیفیت‌ها و حجم‌ها
# ==================================================================
# «کیفیت» = ضلع کوتاه‌تر تصویر؛ ویدیوی عمودی ۷۲۰×۱۲۸۰ هم «۷۲۰p» است.
STD_QUALITIES = (144, 240, 360, 480, 720, 1080, 1440, 2160)
# حاشیه‌ی اطمینان: حجم تخمینی با حجم نهایی بعد از merge کمی فرق دارد
SIZE_MARGIN = 0.92


def _is_video_fmt(fmt: dict) -> bool:
    return fmt.get("vcodec") not in (None, "none")


def _short_side(fmt: dict) -> int:
    values = [
        v for v in (fmt.get("width"), fmt.get("height")) if isinstance(v, int) and v > 0
    ]
    return min(values) if values else 0


def _snap(value: int) -> int:
    """۱۰۷۸ → ۱۰۸۰ ؛ مقدارهای دور از پله‌های استاندارد دست‌نخورده می‌مانند."""
    nearest = min(STD_QUALITIES, key=lambda s: abs(s - value))
    return nearest if abs(nearest - value) <= nearest * 0.08 else value


def is_portrait(info: dict) -> bool:
    width, height = info.get("width"), info.get("height")
    if not (isinstance(width, int) and isinstance(height, int)):
        for fmt in info.get("formats") or []:
            if isinstance(fmt, dict) and _is_video_fmt(fmt):
                width, height = fmt.get("width"), fmt.get("height")
                if isinstance(width, int) and isinstance(height, int):
                    break
    return isinstance(width, int) and isinstance(height, int) and height > width


def _fmt_size(fmt: dict, duration: int | float | None) -> int:
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    if isinstance(size, (int, float)) and size > 0:
        return int(size)
    tbr = fmt.get("tbr")
    if isinstance(tbr, (int, float)) and tbr > 0 and duration:
        return int(tbr * 1000 / 8 * duration)
    return 0


def available_heights(info: dict, max_height: int = 0) -> list[int]:
    """کیفیت‌های واقعاً موجود (ضلع کوتاه)، نزولی و بدون تکرار.

    اگر max_height داده شود، کیفیت‌های بالاتر حذف می‌شوند؛ ولی اگر همه‌ی
    کیفیت‌ها بالاتر بودند، کمترین‌شان نگه داشته می‌شود.
    """
    found: set[int] = set()
    for fmt in info.get("formats") or []:
        if isinstance(fmt, dict) and _is_video_fmt(fmt):
            side = _short_side(fmt)
            if side > 0:
                found.add(_snap(side))

    if not found:
        side = _short_side(info)
        if side > 0:
            found.add(_snap(side))

    heights = sorted(found, reverse=True)
    if max_height:
        capped = [h for h in heights if h <= max_height]
        heights = capped or heights[-1:]
    return heights


def estimate_sizes(info: dict, heights: list[int]) -> dict[int, int]:
    """حجم تخمینی فایل نهایی (ویدیو + صدا) برای هر کیفیت. ۰ = نامشخص."""
    duration = info.get("duration")
    formats = [f for f in (info.get("formats") or []) if isinstance(f, dict)]

    audio_only = [
        f for f in formats
        if not _is_video_fmt(f) and f.get("acodec") not in (None, "none")
    ]
    audio_size = max((_fmt_size(f, duration) for f in audio_only), default=0)

    sizes: dict[int, int] = {}
    for height in heights:
        split: list[tuple[dict, int]] = []   # فقط ویدیو
        whole: list[tuple[dict, int]] = []   # ویدیو + صدا در یک فایل
        for fmt in formats:
            if not _is_video_fmt(fmt):
                continue
            side = _short_side(fmt)
            if side <= 0 or _snap(side) != height:
                continue
            size = _fmt_size(fmt, duration)
            if not size:
                continue
            if fmt.get("acodec") in (None, "none"):
                split.append((fmt, size))
            else:
                whole.append((fmt, size))

        total = 0
        if split:
            # h264 را ترجیح می‌دهیم چون سلکتور هم همان را برمی‌دارد
            preferred = [
                s for f, s in split
                if str(f.get("vcodec", "")).startswith(("avc1", "h264"))
            ]
            video = max(preferred) if preferred else max(s for _, s in split)
            total = video + audio_size
        elif whole:
            total = max(s for _, s in whole)
        sizes[height] = total
    return sizes


def fits(size: int, limit: int) -> bool:
    """حجم نامشخص (۰) را «جا می‌شود» حساب می‌کنیم."""
    return not size or size <= limit * SIZE_MARGIN


def pick_height(
    heights: list[int],
    sizes: dict[int, int],
    limit: int,
    requested: int | None = None,
) -> int | None:
    """بهترین کیفیتی که در سقف ارسال جا می‌شود (requested=None یعنی «بهترین»)."""
    if not heights:
        return requested
    candidates = [h for h in heights if requested is None or h <= requested]
    candidates = candidates or heights[-1:]
    for height in candidates:  # نزولی
        if fits(sizes.get(height, 0), limit):
            return height
    return candidates[-1]


@dataclass
class VideoMeta:
    title: str
    duration: int | None
    heights: list[int]
    sizes: dict[int, int]
    portrait: bool
    thumbnail: str
    webpage_url: str
    best_height: int | None
    best_size: int = 0
    fitting: list[int] = field(default_factory=list)


def normalize_info(info: dict | None) -> dict:
    """پلی‌لیست → اولین ورودی؛ None → {}."""
    if not info:
        return {}
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        return entries[0] if entries else {}
    return info


def build_meta(info: dict, settings: Settings, fallback_url: str) -> VideoMeta:
    heights = available_heights(info, settings.max_height)
    sizes = estimate_sizes(info, heights)
    limit = settings.upload_limit
    fitting = [h for h in heights if fits(sizes.get(h, 0), limit)]
    best = fitting[0] if fitting else None
    return VideoMeta(
        title=(info.get("title") or "ویدیو").strip()[:200],
        duration=info.get("duration"),
        heights=heights,
        sizes=sizes,
        portrait=is_portrait(info),
        thumbnail=info.get("thumbnail") or "",
        webpage_url=info.get("webpage_url") or fallback_url,
        best_height=best,
        best_size=sizes.get(best, 0) if best else 0,
        fitting=fitting,
    )


# ==================================================================
# موتور
# ==================================================================
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


class Downloader:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._sem = asyncio.Semaphore(settings.max_concurrent)

    # ------------------------------------------------------------------
    # گزینه‌های مشترک yt-dlp
    # ------------------------------------------------------------------
    def _common_opts(self, work_dir: Path) -> dict:
        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "socket_timeout": 30,
            "retries": 3,
            "extractor_retries": 2,
        }
        if self.settings.proxy_url:
            opts["proxy"] = self.settings.proxy_url
        if self.settings.cookies_file:
            # هر کار کپی خودش را دارد: yt-dlp موقع بستن کوکی‌ها را دوباره
            # می‌نویسد و چند کار همزمان روی یک فایل همدیگر را خراب می‌کنند.
            target = work_dir / "cookies.txt"
            try:
                shutil.copyfile(self.settings.cookies_file, target)
                opts["cookiefile"] = str(target)
            except OSError:
                logger.warning("کپی فایل کوکی ناموفق بود", exc_info=True)
                opts["cookiefile"] = str(self.settings.cookies_file)
        return opts

    # ------------------------------------------------------------------
    # probe
    # ------------------------------------------------------------------
    def _probe(self, url: str) -> dict:
        import yt_dlp

        with tempfile.TemporaryDirectory(prefix="probe-") as tmp:
            opts = self._common_opts(Path(tmp))
            opts.update(
                {
                    "skip_download": True,
                    # فرمت را خودمان انتخاب می‌کنیم؛ probe نباید به‌خاطر
                    # «Requested format is not available» شکست بخورد.
                    "ignore_no_formats_error": True,
                    "check_formats": False,
                }
            )
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

    async def probe(self, url: str) -> dict:
        try:
            info = normalize_info(await asyncio.to_thread(self._probe, url))
        except Exception as exc:  # noqa: BLE001
            raise self._to_download_error(exc) from exc

        if not info:
            raise DownloadError(DownloadErrorKind.NOT_FOUND)

        formats = info.get("formats")
        if formats is not None:
            has_media = any(
                isinstance(f, dict)
                and (_is_video_fmt(f) or f.get("acodec") not in (None, "none"))
                for f in formats
            )
            if not has_media:
                # فقط تصویر/استوری‌بورد دیده شده (معمولاً نبود JS runtime یا بلاک)
                logger.warning("هیچ فرمت ویدیو/صدایی پیدا نشد: %s", url)
                raise DownloadError(DownloadErrorKind.FORMAT, "no playable formats")
        return info

    # ------------------------------------------------------------------
    # سلکتور فرمت
    # ------------------------------------------------------------------
    @staticmethod
    def _video_selectors(height: int | None, portrait: bool, merge: bool) -> list[str]:
        """فهرست سلکتورها از دقیق به آزاد؛ اولی که کار کند استفاده می‌شود."""
        selectors: list[str] = []
        if height:
            threshold = int(height * 1.05)
            dim = "width" if portrait else "height"
            if merge:
                selectors.append(f"bv*[{dim}<={threshold}]+ba/b[{dim}<={threshold}]")
            else:
                selectors.append(f"b[{dim}<={threshold}]")
        selectors.append("bv*+ba/b" if merge else "b")
        selectors.append("b/bv*+ba")
        return selectors

    # ------------------------------------------------------------------
    # دانلود
    # ------------------------------------------------------------------
    def _run_ydl(
        self,
        url: str,
        out_dir: Path,
        tracker: ProgressTracker,
        cancel_event: threading.Event | None,
        *,
        fmt: str,
        audio_only: bool,
    ) -> dict:
        import yt_dlp

        def progress_hook(data: dict) -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled("cancelled by user")
            tracker.hook(data)

        def pp_hook(data: dict) -> None:
            if data.get("status") == "started":
                tracker.update(status="processing")

        opts = self._common_opts(out_dir)
        opts.update(
            {
                "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
                "format": fmt,
                "progress_hooks": [progress_hook],
                "postprocessor_hooks": [pp_hook],
                "fragment_retries": 5,
                "restrictfilenames": True,
                "concurrent_fragment_downloads": 4,
                "overwrites": True,
            }
        )

        if audio_only:
            if ffmpeg_available():
                opts["postprocessors"] = [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ]
        else:
            # h264/aac را ترجیح بده تا روی همه‌ی گوشی‌ها در تلگرام پخش شود
            opts["format_sort"] = ["res", "vcodec:h264", "acodec:aac"]
            if ffmpeg_available():
                opts["merge_output_format"] = "mp4"

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=True)
        except DownloadCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            # yt-dlp استثنای هوک را گاهی داخل DownloadError می‌پیچد
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled("cancelled by user") from exc
            raise

    def _download_once(
        self,
        url: str,
        out_dir: Path,
        tracker: ProgressTracker,
        height: int | None,
        portrait: bool,
        audio_only: bool,
        cancel_event: threading.Event | None,
    ) -> DownloadResult:
        merge = ffmpeg_available()
        if audio_only:
            selectors = ["ba[ext=m4a]/ba/b", "b"]
        else:
            selectors = self._video_selectors(
                height or self.settings.max_height, portrait, merge
            )

        info: dict | None = None
        last_exc: Exception | None = None
        for selector in selectors:
            try:
                info = self._run_ydl(
                    url, out_dir, tracker, cancel_event,
                    fmt=selector, audio_only=audio_only,
                )
                last_exc = None
                break
            except DownloadCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                # فقط خطای «فرمت» را با سلکتور آزادتر دوباره امتحان می‌کنیم
                if classify(str(exc)) is not DownloadErrorKind.FORMAT:
                    raise
                logger.info("سلکتور %r جواب نداد؛ سلکتور بعدی...", selector)
                self._clean_dir(out_dir)
        if info is None:
            raise last_exc or DownloadError(DownloadErrorKind.UNKNOWN, "no info")

        info = normalize_info(info)
        path = self._resolve_output_path(info, out_dir, audio_only)
        if path is None:
            raise DownloadError(DownloadErrorKind.UNKNOWN, "فایل خروجی پیدا نشد")

        # عرض/ارتفاع واقعی فایل نهایی (بعد از merge)
        req = (info.get("requested_downloads") or [{}])[0] or {}
        width = req.get("width") or info.get("width")
        real_height = req.get("height") or info.get("height")

        return DownloadResult(
            path=path,
            title=(info.get("title") or "ویدیو").strip()[:200],
            duration=int(info["duration"]) if info.get("duration") else None,
            filesize=path.stat().st_size,
            platform=(info.get("extractor_key") or "").lower(),
            width=width if isinstance(width, int) else None,
            height=real_height if isinstance(real_height, int) else None,
            is_audio=audio_only,
        )

    @staticmethod
    def _clean_dir(out_dir: Path) -> None:
        for item in out_dir.iterdir():
            if item.name == "cookies.txt":
                continue
            try:
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
            except OSError:
                pass

    def _download_sync(
        self,
        url: str,
        out_dir: Path,
        tracker: ProgressTracker,
        height: int | None,
        heights: list[int],
        portrait: bool,
        audio_only: bool,
        cancel_event: threading.Event | None,
    ) -> DownloadResult:
        """دانلود؛ اگر فایل از سقف تلگرام بزرگ‌تر شد، با کیفیت پایین‌تر تکرار می‌کند."""
        limit = self.settings.upload_limit
        current = height
        result: DownloadResult | None = None
        for attempt in range(3):
            result = self._download_once(
                url, out_dir, tracker, current, portrait, audio_only, cancel_event
            )
            if audio_only or result.filesize <= limit:
                return result

            ref = current or (heights[0] if heights else self.settings.max_height)
            lower = [h for h in heights if h < ref]
            if not lower:
                return result  # پایین‌تر چیزی نیست؛ هندلر پیام «حجم زیاد» می‌دهد
            current = lower[0]
            logger.info(
                "حجم %s از سقف %s بیشتر است؛ تکرار با %sp (تلاش %d)",
                result.filesize, limit, current, attempt + 1,
            )
            self._clean_dir(out_dir)
            tracker.reset()
        assert result is not None
        return result

    def _resolve_output_path(
        self, info: dict, out_dir: Path, audio_only: bool
    ) -> Path | None:
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
            exts = {".mp3", ".m4a", ".opus", ".webm", ".ogg", ".aac"}
        else:
            exts = {".mp4", ".mkv", ".webm", ".mov"}
        media = [
            p for p in out_dir.iterdir()
            if p.is_file() and p.suffix.lower() in exts and p.stat().st_size > 0
        ]
        return max(media, key=lambda p: p.stat().st_size) if media else None

    async def download(
        self,
        url: str,
        out_dir: Path,
        tracker: ProgressTracker,
        *,
        height: int | None = None,
        heights: list[int] | None = None,
        portrait: bool = False,
        audio_only: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> DownloadResult:
        """دانلود در ترد جدا؛ حداکثر `max_concurrent` دانلود همزمان."""
        async with self._sem:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled("cancelled before start")
            try:
                return await asyncio.to_thread(
                    self._download_sync,
                    url,
                    out_dir,
                    tracker,
                    height,
                    sorted(set(heights or []), reverse=True),
                    portrait,
                    audio_only,
                    cancel_event,
                )
            except (DownloadCancelled, DownloadError):
                raise
            except Exception as exc:  # noqa: BLE001
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelled("cancelled by user") from exc
                raise self._to_download_error(exc) from exc

    @staticmethod
    def _to_download_error(exc: Exception) -> DownloadError:
        message = clean_detail(str(exc), 400)
        kind = classify(message)
        logger.warning("خطای دانلود [%s]: %s", kind.value, message)
        return DownloadError(kind, clean_detail(message))
