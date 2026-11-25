"""وب‌سرور Mini App: احراز هویت + سرویس دانلود + مسیرهای HTTP."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl
import asyncio
import logging
import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable
from aiohttp import web
from config import Settings
from downloader import (
    DownloadCancelled,
    DownloadError,
    DownloadErrorKind,
    Downloader,
    DownloadResult,
    InvalidLink,
    UnsupportedSite,
    available_heights,
    parse_link,
)
from utils import human_duration, human_size, remove_tree, ProgressTracker

# ==================================================================
# auth.py
# ==================================================================
# حداکثر عمر initData (ثانیه). تلگرام خودش هم تاریخ می‌دهد؛ ما سخت‌گیرانه‌تریم.
MAX_AUTH_AGE = 24 * 60 * 60


class InitDataError(ValueError):
    """initData نامعتبر، منقضی یا دست‌کاری‌شده است."""


@dataclass(frozen=True)
class TelegramUser:
    id: int
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    language_code: str = ""
    is_premium: bool = False

    @property
    def display_name(self) -> str:
        full = f"{self.first_name} {self.last_name}".strip()
        return full or self.username or str(self.id)


@dataclass(frozen=True)
class InitData:
    user: TelegramUser
    auth_date: int
    query_id: str = ""
    raw: dict[str, str] | None = None


# ----------------------------------------------------------------------
# امضا
# ----------------------------------------------------------------------
def _secret_key(bot_token: str) -> bytes:
    """secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)"""
    return hmac.new(
        b"WebAppData",
        bot_token.encode(),
        hashlib.sha256,
    ).digest()


def compute_hash(data_check_string: str, bot_token: str) -> str:
    """هش موردانتظار را برای رشته‌ی مرتب‌شده حساب می‌کند."""
    return hmac.new(
        _secret_key(bot_token),
        data_check_string.encode(),
        hashlib.sha256,
    ).hexdigest()


def _data_check_string(pairs: list[tuple[str, str]]) -> str:
    """همه‌ی جفت‌ها به‌جز hash، مرتب‌شده بر اساس کلید، با \\n."""
    return "\n".join(f"{k}={v}" for k, v in sorted(pairs) if k != "hash")


# ----------------------------------------------------------------------
# تجزیه
# ----------------------------------------------------------------------
def parse_init_data(raw: str) -> dict[str, str]:
    """رشته‌ی initData را به دیکشنری تبدیل می‌کند (بدون بررسی امضا)."""
    if not raw or not raw.strip():
        raise InitDataError("initData خالی است")

    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=False)
    except ValueError as exc:
        raise InitDataError("initData قابل تجزیه نیست") from exc

    if not pairs:
        raise InitDataError("initData هیچ فیلدی ندارد")

    return dict(pairs)


def _parse_user(raw_user: str) -> TelegramUser:
    try:
        payload = json.loads(raw_user)
    except (TypeError, ValueError) as exc:
        raise InitDataError("فیلد user معتبر نیست") from exc

    if not isinstance(payload, dict) or "id" not in payload:
        raise InitDataError("فیلد user شناسه ندارد")

    try:
        user_id = int(payload["id"])
    except (TypeError, ValueError) as exc:
        raise InitDataError("شناسه‌ی کاربر عدد نیست") from exc

    return TelegramUser(
        id=user_id,
        first_name=str(payload.get("first_name") or ""),
        last_name=str(payload.get("last_name") or ""),
        username=str(payload.get("username") or ""),
        language_code=str(payload.get("language_code") or ""),
        is_premium=bool(payload.get("is_premium") or False),
    )


# ----------------------------------------------------------------------
# اعتبارسنجی کامل
# ----------------------------------------------------------------------
def validate_init_data(
    raw: str,
    bot_token: str,
    *,
    max_age: int = MAX_AUTH_AGE,
    now: int | None = None,
) -> InitData:
    """initData را بررسی می‌کند و کاربر را برمی‌گرداند.

    خطاها:
        InitDataError — امضا نامعتبر، منقضی، یا فیلدهای ناقص.
    """
    if not bot_token:
        raise InitDataError("توکن ربات تنظیم نشده است")

    fields = parse_init_data(raw)

    received_hash = fields.get("hash", "")
    if not received_hash:
        raise InitDataError("فیلد hash وجود ندارد")

    # --- امضا ---
    pairs = list(fields.items())
    check_string = _data_check_string(pairs)
    expected = compute_hash(check_string, bot_token)

    # مقایسه‌ی زمان‌ثابت تا از حمله‌ی زمان‌سنجی جلوگیری شود
    if not hmac.compare_digest(expected, received_hash):
        raise InitDataError("امضای initData نامعتبر است")

    # --- تازگی ---
    raw_auth_date = fields.get("auth_date", "")
    try:
        auth_date = int(raw_auth_date)
    except (TypeError, ValueError) as exc:
        raise InitDataError("auth_date معتبر نیست") from exc

    current = int(now if now is not None else time.time())
    if auth_date > current + 60:
        raise InitDataError("auth_date در آینده است")
    if max_age > 0 and (current - auth_date) > max_age:
        raise InitDataError("initData منقضی شده است")

    # --- کاربر ---
    raw_user = fields.get("user", "")
    if not raw_user:
        raise InitDataError("فیلد user وجود ندارد")

    return InitData(
        user=_parse_user(raw_user),
        auth_date=auth_date,
        query_id=fields.get("query_id", ""),
        raw=fields,
    )


# ==================================================================
# service.py
# ==================================================================
logger = logging.getLogger(__name__)

# کارها بعد از این مدت منقضی می‌شوند (ثانیه)
WEB_JOB_TTL = 30 * 60

# امضای فرستنده: (chat_id, result) -> None
Sender = Callable[[int, DownloadResult], Awaitable[None]]


class JobState(str, Enum):
    QUEUED = "queued"
    PROBING = "probing"
    READY = "ready"          # اطلاعات آماده است، منتظر انتخاب کیفیت
    DOWNLOADING = "downloading"
    UPLOADING = "uploading"
    DONE = "done"
    CANCELLED = "cancelled"
    ERROR = "error"


# وضعیت‌هایی که دیگر تغییر نمی‌کنند
TERMINAL = {JobState.DONE, JobState.CANCELLED, JobState.ERROR}


@dataclass
class WebJob:
    job_id: str
    chat_id: int
    user_id: int
    url: str = ""
    platform: str = ""
    title: str = ""
    duration: int | None = None
    heights: list[int] = field(default_factory=list)
    thumbnail: str = ""
    state: JobState = JobState.QUEUED
    error_kind: str = ""
    error_text: str = ""
    percent: float = 0.0
    downloaded: int = 0
    total: int = 0
    speed: float = 0.0
    filesize: int = 0
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    # کنترل اجرا
    # threading.Event است نه asyncio.Event: yt-dlp در ترد جدا اجرا می‌شود و
    # باید همان پرچم را ببیند. ست کردنش از حلقه‌ی asyncio هم امن است.
    cancel_event: threading.Event = field(default_factory=threading.Event)
    task: asyncio.Task | None = None

    @property
    def expired(self) -> bool:
        return (time.time() - self.created_at) > WEB_JOB_TTL

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def cancel(self) -> None:
        self.cancel_event.set()

    def to_dict(self) -> dict:
        """خروجی JSON برای فرانت‌اند."""
        return {
            "job_id": self.job_id,
            "state": self.state.value,
            "url": self.url,
            "platform": self.platform,
            "title": self.title,
            "duration": self.duration,
            "duration_text": human_duration(self.duration),
            "heights": self.heights,
            "thumbnail": self.thumbnail,
            "percent": round(self.percent, 1),
            "downloaded": self.downloaded,
            "downloaded_text": human_size(self.downloaded),
            "total": self.total,
            "total_text": human_size(self.total) if self.total else "",
            "speed": int(self.speed),
            "speed_text": f"{human_size(int(self.speed))}/s" if self.speed else "",
            "filesize": self.filesize,
            "filesize_text": human_size(self.filesize) if self.filesize else "",
            "error_kind": self.error_kind,
            "error_text": self.error_text,
            "cancellable": self.state in {
                JobState.QUEUED,
                JobState.PROBING,
                JobState.DOWNLOADING,
                JobState.UPLOADING,
            },
        }


class ServiceError(Exception):
    """خطای قابل نمایش به فرانت‌اند."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class DownloadService:
    """نگه‌دارنده‌ی کارها + اجرای دانلود در پس‌زمینه."""

    def __init__(
        self,
        settings: Settings,
        downloader: Downloader,
        sender: Sender | None = None,
    ) -> None:
        self.settings = settings
        self.downloader = downloader
        self.sender = sender
        self._jobs: dict[str, WebJob] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # مدیریت کارها
    # ------------------------------------------------------------------
    def _purge(self) -> None:
        stale = [k for k, j in self._jobs.items() if j.expired and j.state in TERMINAL]
        for key in stale:
            self._jobs.pop(key, None)

    async def create_job(self, chat_id: int, user_id: int) -> WebJob:
        async with self._lock:
            self._purge()
            job = WebJob(
                job_id=uuid.uuid4().hex[:12],
                chat_id=chat_id,
                user_id=user_id,
            )
            self._jobs[job.job_id] = job
            return job

    def get(self, job_id: str) -> WebJob | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if job.expired and job.state in TERMINAL:
            self._jobs.pop(job_id, None)
            return None
        return job

    def get_for_user(self, job_id: str, user_id: int) -> WebJob:
        """کار را می‌گیرد و مطمئن می‌شود مال همین کاربر است."""
        job = self.get(job_id)
        if job is None:
            raise ServiceError("job_not_found", "این کار پیدا نشد یا منقضی شده است.", 404)
        if job.user_id != user_id:
            raise ServiceError("forbidden", "این کار به شما تعلق ندارد.", 403)
        return job

    def active_count(self) -> int:
        return sum(
            1
            for j in self._jobs.values()
            if j.state in {JobState.DOWNLOADING, JobState.UPLOADING}
        )

    # ------------------------------------------------------------------
    # ۱) بررسی لینک
    # ------------------------------------------------------------------
    async def probe(self, job: WebJob, url: str) -> WebJob:
        """لینک را بررسی می‌کند و اطلاعات ویدیو را در کار می‌گذارد."""
        try:
            link = parse_link(url)
        except InvalidLink as exc:
            raise ServiceError("invalid_link", "لینک معتبر نیست. آدرس کامل ویدیو را بفرست.", 400) from exc
        except UnsupportedSite as exc:
            raise ServiceError(
                "unsupported_site",
                "این سایت پشتیبانی نمی‌شود. فقط یوتیوب، اینستاگرام و تیک‌تاک.",
                400,
            ) from exc

        job.url = link.url
        job.platform = link.platform
        job.state = JobState.PROBING

        try:
            info = await self.downloader.probe(link.url)
        except DownloadError as exc:
            job.state = JobState.ERROR
            job.error_kind = exc.kind.value
            job.error_text = _error_text(exc.kind)
            raise ServiceError("probe_failed", job.error_text, 422) from exc

        if info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            info = entries[0] if entries else {}

        if not info:
            job.state = JobState.ERROR
            job.error_kind = DownloadErrorKind.NOT_FOUND.value
            job.error_text = _error_text(DownloadErrorKind.NOT_FOUND)
            raise ServiceError("not_found", job.error_text, 404)

        # --- محدودیت مدت ---
        duration = info.get("duration")
        if self.settings.max_duration_sec and duration and duration > self.settings.max_duration_sec:
            job.state = JobState.ERROR
            job.error_kind = DownloadErrorKind.TOO_LONG.value
            job.error_text = (
                f"ویدیو طولانی‌تر از حد مجاز است "
                f"(حداکثر {human_duration(self.settings.max_duration_sec)})."
            )
            raise ServiceError("too_long", job.error_text, 422)

        # --- محدودیت حجم تقریبی ---
        approx = info.get("filesize") or info.get("filesize_approx")
        if approx and approx > self.settings.upload_limit:
            job.state = JobState.ERROR
            job.error_kind = DownloadErrorKind.TOO_LARGE.value
            job.error_text = _too_large_text(approx, self.settings)
            raise ServiceError("too_large", job.error_text, 422)

        job.title = (info.get("title") or "ویدیو").strip()[:200]
        job.duration = duration
        job.heights = available_heights(info)
        job.thumbnail = info.get("thumbnail") or ""
        job.state = JobState.READY
        return job

    # ------------------------------------------------------------------
    # ۲) شروع دانلود
    # ------------------------------------------------------------------
    async def start_download(
        self,
        job: WebJob,
        *,
        height: int | None = None,
        audio_only: bool = False,
    ) -> WebJob:
        if job.state in {JobState.DOWNLOADING, JobState.UPLOADING}:
            raise ServiceError("busy", "این دانلود در حال اجراست.", 409)
        if job.state in TERMINAL:
            raise ServiceError("finished", "این کار تمام شده است. لینک جدیدی بفرست.", 409)
        if not job.url:
            raise ServiceError("no_url", "اول لینک را بررسی کن.", 400)

        job.cancel_event.clear()
        job.state = JobState.DOWNLOADING
        job.percent = 0.0
        job.downloaded = 0
        job.total = 0
        job.speed = 0.0
        job.error_kind = ""
        job.error_text = ""

        job.task = asyncio.create_task(
            self._run(job, height=height, audio_only=audio_only)
        )
        return job

    async def _run(
        self,
        job: WebJob,
        *,
        height: int | None,
        audio_only: bool,
    ) -> None:
        """اجرای واقعی: دانلود، سپس ارسال به تلگرام."""
        job_dir = self.settings.download_dir / f"web-{job.job_id}"
        job_dir.mkdir(parents=True, exist_ok=True)

        tracker = ProgressTracker()
        progress_task = asyncio.create_task(self._progress_loop(job, tracker))

        try:
            result = await self.downloader.download(
                job.url,
                job_dir,
                tracker,
                height=height,
                audio_only=audio_only,
                cancel_event=job.cancel_event,
            )
        except DownloadCancelled:
            job.state = JobState.CANCELLED
            job.error_text = "دانلود لغو شد."
            return
        except DownloadError as exc:
            job.state = JobState.ERROR
            job.error_kind = exc.kind.value
            job.error_text = _error_text(exc.kind)
            return
        except Exception:  # noqa: BLE001
            logger.exception("خطای پیش‌بینی‌نشده در دانلود وب")
            job.state = JobState.ERROR
            job.error_kind = DownloadErrorKind.UNKNOWN.value
            job.error_text = _error_text(DownloadErrorKind.UNKNOWN)
            return
        finally:
            progress_task.cancel()
            try:
                await progress_task
            except asyncio.CancelledError:
                pass

        # --- حجم واقعی ---
        if result.filesize > self.settings.upload_limit:
            job.state = JobState.ERROR
            job.error_kind = DownloadErrorKind.TOO_LARGE.value
            job.error_text = _too_large_text(result.filesize, self.settings)
            remove_tree(job_dir)
            return

        job.filesize = result.filesize
        job.percent = 100.0
        job.state = JobState.UPLOADING

        # --- ارسال به چت ---
        if self.sender is None:
            job.state = JobState.ERROR
            job.error_text = "ارسال به تلگرام تنظیم نشده است."
            remove_tree(job_dir)
            return

        try:
            await self.sender(job.chat_id, result)
        except Exception:  # noqa: BLE001
            logger.exception("ارسال فایل به تلگرام ناموفق بود")
            job.state = JobState.ERROR
            job.error_kind = "send_failed"
            job.error_text = "ارسال فایل به تلگرام ناموفق بود."
            return
        finally:
            remove_tree(job_dir)

        job.state = JobState.DONE
        job.finished_at = time.time()

    async def _progress_loop(self, job: WebJob, tracker: ProgressTracker) -> None:
        """وضعیت پیشرفت را از ترد yt-dlp به کار منتقل می‌کند."""
        while True:
            await asyncio.sleep(0.5)
            state = tracker.snapshot()
            job.downloaded = state.downloaded
            job.total = state.total
            job.speed = state.speed
            job.percent = state.percent
            if state.status == "finished":
                job.percent = 100.0

    # ------------------------------------------------------------------
    # ۳) لغو
    # ------------------------------------------------------------------
    async def cancel(self, job: WebJob) -> WebJob:
        if job.state in TERMINAL:
            raise ServiceError("finished", "این کار تمام شده است.", 409)
        job.cancel()
        if job.task is not None and not job.task.done():
            # به دانلود فرصت می‌دهیم تمیز متوقف شود
            try:
                await asyncio.wait_for(asyncio.shield(job.task), timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        if job.state not in TERMINAL:
            job.state = JobState.CANCELLED
            job.error_text = "دانلود لغو شد."
        return job


# ----------------------------------------------------------------------
# ابزارها
# ----------------------------------------------------------------------
def _error_text(kind: DownloadErrorKind) -> str:
    return {
        DownloadErrorKind.PRIVATE: "این ویدیو خصوصی است یا دسترسی به آن بسته شده.",
        DownloadErrorKind.UNAVAILABLE: "ویدیو در دسترس نیست — حذف شده یا در کشور شما محدود شده.",
        DownloadErrorKind.LOGIN_REQUIRED: "این محتوا نیاز به ورود دارد. برای اینستاگرام، ادمین باید کوکی تنظیم کند.",
        DownloadErrorKind.BOT_CHECK: "یوتیوب موقتاً درخواست‌های سرور را محدود کرده. چند دقیقه بعد امتحان کن.",
        DownloadErrorKind.NOT_FOUND: "محتوایی پیدا نشد. لینک را بررسی کن.",
        DownloadErrorKind.NETWORK: "خطا در اتصال. اتصال سرور را بررسی کن.",
        DownloadErrorKind.TOO_LONG: "ویدیو طولانی‌تر از حد مجاز است.",
        DownloadErrorKind.TOO_LARGE: "حجم فایل زیاد است.",
        DownloadErrorKind.UNSUPPORTED: "این سایت پشتیبانی نمی‌شود.",
    }.get(kind, "خطای پیش‌بینی‌نشده. دوباره امتحان کن.")


def _too_large_text(size: int, settings: Settings) -> str:
    limit = human_size(settings.upload_limit)
    if settings.uses_local_api:
        return (
            f"حجم فایل {human_size(size)} است و از سقف {limit} بیشتر است. "
            "کیفیت پایین‌تر را انتخاب کن."
        )
    return (
        f"حجم فایل {human_size(size)} است و از سقف {limit} بیشتر است. "
        "تلگرام برای ربات‌ها سقف ۵۰ مگابایت دارد؛ برای فایل‌های بزرگ‌تر ادمین باید "
        "سرور محلی Bot API را فعال کند."
    )


# ==================================================================
# server.py
# ==================================================================

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

# فقط همین فایل‌ها سرو می‌شوند (تا فایل‌های پایتون/.env هرگز لو نروند)
_ALLOWED_ASSETS = {"index.html", "app.css", "app.js"}


def _find_asset(name: str) -> Path | None:
    """فایل را در static/ و در صورت نبودن، کنار webapp.py پیدا می‌کند.

    اگر فایل‌ها هنگام آپلود به گیت‌هاب تغییر نام داده باشند
    (مثل app-1.js یا index-2.html) هم پیدا می‌شوند.
    """
    if name not in _ALLOWED_ASSETS:
        return None
    stem, dot, ext = name.rpartition(".")
    for folder in (STATIC_DIR, BASE_DIR):
        exact = folder / name
        if exact.is_file():
            return exact
        for alt in sorted(folder.glob(f"{stem}-*.{ext}")):
            if alt.is_file():
                return alt
    return None


async def static_file(request: web.Request) -> web.StreamResponse:
    path = _find_asset(request.match_info["name"])
    if path is None:
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "no-cache"})

# کلیدهایی که در request ذخیره می‌شوند
KEY_USER = "tg_user"
KEY_INIT = "tg_init"


# ----------------------------------------------------------------------
# میان‌افزار احراز هویت
# ----------------------------------------------------------------------
def _extract_init_data(request: web.Request) -> str:
    """initData را از هدر یا بدنه می‌گیرد.

    هدر X-Init-Data روش اصلی است؛ اگر نبود، از query هم می‌پذیریم
    (برای دیباگ با curl).
    """
    raw = request.headers.get("X-Init-Data", "").strip()
    if raw:
        return raw
    return request.query.get("init_data", "").strip()


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """مسیرهای /api را محافظت می‌کند."""
    if not request.path.startswith("/api/"):
        return await handler(request)

    settings: Settings = request.app["settings"]
    raw = _extract_init_data(request)

    if not raw:
        return web.json_response(
            {"error": "unauthorized", "message": "initData ارسال نشده است."},
            status=401,
        )

    try:
        init = validate_init_data(raw, settings.bot_token)
    except InitDataError as exc:
        logger.warning("initData رد شد: %s", exc)
        return web.json_response(
            {"error": "unauthorized", "message": "اعتبارسنجی تلگرام ناموفق بود."},
            status=401,
        )

    request[KEY_USER] = init.user
    request[KEY_INIT] = init
    return await handler(request)


@web.middleware
async def error_middleware(request: web.Request, handler):
    """ServiceError را به JSON تمیز تبدیل می‌کند."""
    try:
        return await handler(request)
    except ServiceError as exc:
        return web.json_response(
            {"error": exc.code, "message": exc.message},
            status=exc.status,
        )
    except web.HTTPException:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("خطای پیش‌بینی‌نشده در %s %s", request.method, request.path)
        return web.json_response(
            {"error": "internal", "message": "خطای داخلی سرور."},
            status=500,
        )


# ----------------------------------------------------------------------
# صفحه‌ها
# ----------------------------------------------------------------------
async def index(request: web.Request) -> web.StreamResponse:
    path = _find_asset("index.html")
    if path is None:
        return web.Response(text="index.html پیدا نشد", status=500)
    return web.FileResponse(
        path,
        headers={
            # Mini App داخل iframe تلگرام باز می‌شود
            "Cache-Control": "no-cache",
        },
    )


async def health(request: web.Request) -> web.Response:
    service: DownloadService = request.app["service"]
    return web.json_response(
        {
            "status": "ok",
            "active": service.active_count(),
        }
    )


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------
async def api_probe(request: web.Request) -> web.Response:
    """بررسی لینک و برگرداندن اطلاعات ویدیو."""
    service: DownloadService = request.app["service"]
    user = request[KEY_USER]

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        raise ServiceError("bad_json", "بدنه‌ی درخواست JSON معتبر نیست.", 400)

    url = str(payload.get("url") or "").strip()
    if not url:
        raise ServiceError("no_url", "لینک را وارد کن.", 400)

    job = await service.create_job(chat_id=user.id, user_id=user.id)
    await service.probe(job, url)
    return web.json_response(job.to_dict())


async def api_download(request: web.Request) -> web.Response:
    """شروع دانلود با کیفیت انتخاب‌شده."""
    service: DownloadService = request.app["service"]
    user = request[KEY_USER]

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        raise ServiceError("bad_json", "بدنه‌ی درخواست JSON معتبر نیست.", 400)

    job_id = str(payload.get("job_id") or "").strip()
    if not job_id:
        raise ServiceError("no_job", "شناسه‌ی کار ارسال نشده.", 400)

    job = service.get_for_user(job_id, user.id)

    audio_only = bool(payload.get("audio_only") or False)
    height: int | None = None

    raw_height = payload.get("height")
    if raw_height not in (None, "", "best"):
        try:
            height = int(raw_height)
        except (TypeError, ValueError) as exc:
            raise ServiceError("bad_height", "کیفیت انتخاب‌شده معتبر نیست.", 400) from exc
        if height <= 0:
            raise ServiceError("bad_height", "کیفیت انتخاب‌شده معتبر نیست.", 400)

    await service.start_download(job, height=height, audio_only=audio_only)
    return web.json_response(job.to_dict())


async def api_status(request: web.Request) -> web.Response:
    """وضعیت پیشرفت یک کار."""
    service: DownloadService = request.app["service"]
    user = request[KEY_USER]

    job_id = request.match_info.get("job_id", "")
    job = service.get_for_user(job_id, user.id)
    return web.json_response(job.to_dict())


async def api_cancel(request: web.Request) -> web.Response:
    """لغو دانلود در جریان."""
    service: DownloadService = request.app["service"]
    user = request[KEY_USER]

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        raise ServiceError("bad_json", "بدنه‌ی درخواست JSON معتبر نیست.", 400)

    job_id = str(payload.get("job_id") or "").strip()
    if not job_id:
        raise ServiceError("no_job", "شناسه‌ی کار ارسال نشده.", 400)

    job = service.get_for_user(job_id, user.id)
    await service.cancel(job)
    return web.json_response(job.to_dict())


# ----------------------------------------------------------------------
# ساخت اپلیکیشن
# ----------------------------------------------------------------------
def create_app(settings: Settings, service: DownloadService) -> web.Application:
    app = web.Application(
        middlewares=[error_middleware, auth_middleware],
        client_max_size=1024 * 1024,  # ۱ مگابایت برای بدنه‌ی JSON کافی است
    )
    app["settings"] = settings
    app["service"] = service

    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/static/{name}", static_file)

    app.router.add_post("/api/probe", api_probe)
    app.router.add_post("/api/download", api_download)
    app.router.add_get("/api/status/{job_id}", api_status)
    app.router.add_post("/api/cancel", api_cancel)

    return app
