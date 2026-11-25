"""هندلرهای تلگرام (دستورها + دانلود)."""

from __future__ import annotations

import time
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes
import asyncio
import logging
import uuid
from pathlib import Path
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError
import utils as kb
import utils as msg
from config import Settings
from downloader import (
    DownloadCancelled,
    DownloadError,
    DownloadErrorKind,
    Downloader,
    InvalidLink,
    UnsupportedSite,
    available_heights,
    parse_link,
)
from utils import (
    Job,
    JobRegistry,
    ProgressTracker,
    human_duration,
    human_size,
    progress_bar,
    remove_tree,
)

# ==================================================================
# commands.py
# ==================================================================
# زمان شروع پروسه — برای نمایش «مدت فعالیت»
STARTED_AT = time.time()


def _uptime() -> str:
    seconds = int(time.time() - STARTED_AT)
    hours, rest = divmod(seconds, 3600)
    minutes, _ = divmod(rest, 60)
    if hours:
        return f"{hours} ساعت و {minutes} دقیقه"
    return f"{minutes} دقیقه"


def _menu(settings: Settings):
    """منوی اصلی با دکمه‌ی Mini App اگر آدرس تنظیم شده باشد."""
    return kb.main_menu(settings.webapp_url)


def _welcome(settings: Settings) -> str:
    """متن خوش‌آمد بر اساس وجود Mini App."""
    return msg.WELCOME_WEBAPP if settings.has_webapp else msg.WELCOME


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    await update.effective_message.reply_text(
        _welcome(settings),
        parse_mode=ParseMode.HTML,
        reply_markup=_menu(settings),
        disable_web_page_preview=True,
    )


# ----------------------------------------------------------------------
# /help
# ----------------------------------------------------------------------
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    await update.effective_message.reply_text(
        msg.HELP.format(
            limit=human_size(settings.upload_limit),
            duration=human_duration(settings.max_duration_sec),
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=kb.back_menu(),
        disable_web_page_preview=True,
    )


# ----------------------------------------------------------------------
# دکمه‌های منو
# ----------------------------------------------------------------------
async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """بازگشت به منوی اصلی."""
    query = update.callback_query
    await query.answer()
    settings: Settings = context.application.bot_data["settings"]
    await _safe_edit(query.message, _welcome(settings), _menu(settings))


async def on_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    settings: Settings = context.application.bot_data["settings"]
    await _safe_edit(
        query.message,
        msg.HELP.format(
            limit=human_size(settings.upload_limit),
            duration=human_duration(settings.max_duration_sec),
        ),
        kb.back_menu(),
    )


async def on_platforms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await _safe_edit(query.message, msg.PLATFORMS, kb.back_menu())


async def on_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    settings: Settings = context.application.bot_data["settings"]
    registry: JobRegistry = context.application.bot_data["jobs"]

    await _safe_edit(
        query.message,
        msg.STATUS.format(
            uptime=_uptime(),
            active=registry.active_count(),
            limit=human_size(settings.upload_limit),
            mode="سرور محلی" if settings.uses_local_api else "api.telegram.org",
        ),
        kb.back_menu(),
    )


# ==================================================================
# download.py
# ==================================================================
logger = logging.getLogger(__name__)

# فاصله‌ی به‌روزرسانی پیام پیشرفت (ثانیه)
PROGRESS_INTERVAL = 4.0


# ----------------------------------------------------------------------
# ۱) دریافت لینک
# ----------------------------------------------------------------------
async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return

    settings: Settings = context.application.bot_data["settings"]
    downloader: Downloader = context.application.bot_data["downloader"]
    registry: JobRegistry = context.application.bot_data["jobs"]

    # --- تشخیص لینک ---
    try:
        link = parse_link(message.text)
    except InvalidLink:
        await message.reply_text(
            msg.NO_LINK,
            parse_mode=ParseMode.HTML,
            reply_markup=kb.main_menu(settings.webapp_url),
        )
        return
    except UnsupportedSite:
        await message.reply_text(
            msg.UNSUPPORTED_SITE,
            parse_mode=ParseMode.HTML,
            reply_markup=kb.main_menu(settings.webapp_url),
        )
        return

    platform_fa = msg.PLATFORM_NAMES.get(link.platform, link.platform)

    status = await message.reply_text(msg.PROBING, parse_mode=ParseMode.HTML)

    # --- probe ---
    try:
        info = await downloader.probe(link.url)
    except DownloadError as exc:
        await _edit_error(status, exc, settings)
        return

    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        info = entries[0] if entries else {}

    if not info:
        await status.edit_text(
            msg.NOT_FOUND,
            parse_mode=ParseMode.HTML,
            reply_markup=kb.main_menu(settings.webapp_url),
        )
        return

    # --- محدودیت مدت ---
    duration = info.get("duration")
    if settings.max_duration_sec and duration and duration > settings.max_duration_sec:
        await status.edit_text(
            msg.TOO_LONG.format(limit=human_duration(settings.max_duration_sec)),
            parse_mode=ParseMode.HTML,
            reply_markup=kb.main_menu(settings.webapp_url),
        )
        return

    # --- محدودیت حجم تقریبی ---
    approx = info.get("filesize") or info.get("filesize_approx")
    if approx and approx > settings.upload_limit:
        await status.edit_text(
            _too_large_text(approx, settings),
            parse_mode=ParseMode.HTML,
            reply_markup=kb.main_menu(settings.webapp_url),
        )
        return

    # --- ساخت کار و نمایش کارت ---
    heights = available_heights(info)
    job = registry.create(
        url=link.url,
        platform=link.platform,
        title=(info.get("title") or "ویدیو").strip()[:200],
        duration=duration,
        heights=heights,
        info={"webpage_url": info.get("webpage_url") or link.url},
    )

    card = _card_text(job, platform_fa, approx)
    await status.edit_text(
        card,
        parse_mode=ParseMode.HTML,
        reply_markup=kb.quality_menu(job.job_id, heights),
        disable_web_page_preview=True,
    )


def _card_text(job: Job, platform_fa: str, approx: int | None) -> str:
    url = job.info.get("webpage_url") or job.url
    title = _escape(job.title)
    duration = human_duration(job.duration)

    if approx:
        return msg.CARD.format(
            url=_escape(url),
            title=title,
            platform=platform_fa,
            duration=duration,
            size=human_size(approx),
        )
    return msg.CARD_NO_SIZE.format(
        url=_escape(url),
        title=title,
        platform=platform_fa,
        duration=duration,
    )


# ----------------------------------------------------------------------
# ۲) دکمه‌ی کیفیت
# ----------------------------------------------------------------------
async def on_quality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, job_id, value = kb.parse_callback(query.data or "")

    registry: JobRegistry = context.application.bot_data["jobs"]
    job = registry.get(job_id)

    if job is None:
        await query.answer(msg.JOB_EXPIRED, show_alert=True)
        return

    if job.running:
        await query.answer("⏳ این دانلود در حال اجراست.", show_alert=False)
        return

    await query.answer("✅ شروع شد")

    height: int | None = None
    if value not in ("best", ""):
        try:
            height = int(value)
        except ValueError:
            height = None

    await _start_download(
        context=context,
        message=query.message,
        job=job,
        height=height,
        audio_only=False,
    )


# ----------------------------------------------------------------------
# ۳) دکمه‌ی صدا
# ----------------------------------------------------------------------
async def on_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, job_id, _ = kb.parse_callback(query.data or "")

    registry: JobRegistry = context.application.bot_data["jobs"]
    job = registry.get(job_id)

    if job is None:
        await query.answer(msg.JOB_EXPIRED, show_alert=True)
        return
    if job.running:
        await query.answer("⏳ این دانلود در حال اجراست.", show_alert=False)
        return

    await query.answer("🎵 فقط صدا")

    await _start_download(
        context=context,
        message=query.message,
        job=job,
        height=None,
        audio_only=True,
    )


# ----------------------------------------------------------------------
# ۴) دکمه‌ی لغو
# ----------------------------------------------------------------------
async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, job_id, _ = kb.parse_callback(query.data or "")

    registry: JobRegistry = context.application.bot_data["jobs"]
    job = registry.get(job_id) if job_id else None

    if job is None or not job.running:
        await query.answer(msg.CANCEL_NOTHING, show_alert=False)
        return

    job.cancel()
    await query.answer("🛑 لغو شد", show_alert=False)

    try:
        await query.message.edit_text(
            msg.CANCELLING, parse_mode=ParseMode.HTML
        )
    except TelegramError:
        pass


# ----------------------------------------------------------------------
# ۵) اجرای دانلود
# ----------------------------------------------------------------------
async def _start_download(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    message,
    job: Job,
    height: int | None,
    audio_only: bool,
) -> None:
    settings: Settings = context.application.bot_data["settings"]
    downloader: Downloader = context.application.bot_data["downloader"]
    registry: JobRegistry = context.application.bot_data["jobs"]

    platform_fa = msg.PLATFORM_NAMES.get(job.platform, job.platform)

    job.running = True
    job.cancel_event.clear()

    job_dir = settings.download_dir / uuid.uuid4().hex
    job_dir.mkdir(parents=True, exist_ok=True)

    # پیام وضعیت + دکمه‌ی لغو
    try:
        await message.edit_text(
            msg.DOWNLOADING_PLAIN.format(platform=platform_fa),
            parse_mode=ParseMode.HTML,
            reply_markup=kb.downloading_menu(job.job_id),
        )
        status = message
    except TelegramError:
        status = await message.reply_text(
            msg.DOWNLOADING_PLAIN.format(platform=platform_fa),
            parse_mode=ParseMode.HTML,
            reply_markup=kb.downloading_menu(job.job_id),
        )

    tracker = ProgressTracker()
    progress_task = asyncio.create_task(
        _progress_loop(status, tracker, platform_fa, job)
    )

    try:
        result = await downloader.download(
            job.url,
            job_dir,
            tracker,
            height=height,
            audio_only=audio_only,
            cancel_event=job.cancel_event,
        )
    except DownloadCancelled:
        await _finish_progress(progress_task)
        await _safe_edit(status, msg.CANCELLED, kb.after_download_menu())
        return
    except DownloadError as exc:
        await _finish_progress(progress_task)
        await _edit_error(status, exc, settings)
        return
    finally:
        job.running = False
        remove_tree(job_dir)

    await _finish_progress(progress_task)

    # --- بررسی حجم واقعی ---
    if result.filesize > settings.upload_limit:
        await _safe_edit(
            status, _too_large_text(result.filesize, settings), kb.after_download_menu()
        )
        return

    # --- ارسال ---
    await _safe_edit(status, msg.UPLOADING)
    await _send_media(message, status, result, platform_fa)

    registry.drop(job.job_id)


async def _finish_progress(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _progress_loop(
    status, tracker: ProgressTracker, platform_fa: str, job: Job
) -> None:
    """هر چند ثانیه پیام وضعیت را به‌روز می‌کند."""
    last_text = ""
    while True:
        await asyncio.sleep(PROGRESS_INTERVAL)

        if job.cancelled:
            return

        state = tracker.snapshot()

        if state.status == "finished":
            text = msg.PROCESSING
        elif state.total > 0:
            text = msg.DOWNLOADING.format(
                platform=platform_fa,
                bar=progress_bar(state.percent),
                done=human_size(state.downloaded),
                total=human_size(state.total),
                speed=f"{human_size(int(state.speed))}/s" if state.speed else "—",
            )
        else:
            text = msg.DOWNLOADING_PLAIN.format(platform=platform_fa)

        if text == last_text:
            continue
        last_text = text

        try:
            await status.edit_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=kb.downloading_menu(job.job_id),
            )
        except BadRequest:
            # «message is not modified» یا پیام حذف شده — بی‌اهمیت
            pass
        except RetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
        except TelegramError:
            logger.debug("به‌روزرسانی پیشرفت ناموفق بود", exc_info=True)


# ----------------------------------------------------------------------
# ۶) ارسال فایل
# ----------------------------------------------------------------------
async def _send_media(message, status, result, platform_fa: str) -> None:
    """ارسال ویدیو یا صدا؛ اگر تلگرام نپذیرفت، به‌صورت فایل می‌فرستد."""
    title = _escape(result.title)
    size = human_size(result.filesize)

    if result.is_audio:
        caption = msg.CAPTION_AUDIO.format(
            title=title, platform=platform_fa, size=size
        )
        await message.chat.send_action(ChatAction.UPLOAD_VOICE)
        try:
            with result.path.open("rb") as fh:
                await message.reply_audio(
                    audio=fh,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    title=result.title[:64],
                    read_timeout=300,
                    write_timeout=300,
                    connect_timeout=30,
                )
            await _safe_delete(status)
            return
        except TelegramError as exc:
            logger.warning("ارسال صدا ناموفق: %s", exc)
            # ادامه به مسیر فایل

    caption = msg.CAPTION.format(title=title, platform=platform_fa, size=size)
    await message.chat.send_action(ChatAction.UPLOAD_VIDEO)

    try:
        with result.path.open("rb") as fh:
            await message.reply_video(
                video=fh,
                caption=caption,
                parse_mode=ParseMode.HTML,
                supports_streaming=True,
                width=result.width,
                height=result.height,
                duration=result.duration,
                read_timeout=300,
                write_timeout=300,
                connect_timeout=30,
            )
    except (BadRequest, TelegramError) as exc:
        logger.warning("ارسال به‌عنوان ویدیو ناموفق: %s", exc)
        # تلاش دوم: به‌صورت فایل (برای فرمت‌های غیرمنتظره)
        try:
            with result.path.open("rb") as fh:
                await message.reply_document(
                    document=fh,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    read_timeout=300,
                    write_timeout=300,
                )
        except TelegramError:
            logger.exception("ارسال فایل هم ناموفق بود")
            await _safe_edit(status, msg.SEND_FAILED, kb.after_download_menu())
            return

    await _safe_delete(status)


# ----------------------------------------------------------------------
# ابزارها
# ----------------------------------------------------------------------
async def _safe_edit(message, text: str, markup=None) -> None:
    try:
        await message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except TelegramError:
        pass


async def _safe_delete(message) -> None:
    try:
        await message.delete()
    except TelegramError:
        pass


async def _edit_error(status, exc: DownloadError, settings: Settings) -> None:
    kind = exc.kind

    if kind is DownloadErrorKind.PRIVATE:
        text = msg.PRIVATE_VIDEO
    elif kind is DownloadErrorKind.UNAVAILABLE:
        text = msg.VIDEO_UNAVAILABLE
    elif kind is DownloadErrorKind.LOGIN_REQUIRED:
        text = msg.LOGIN_REQUIRED
    elif kind is DownloadErrorKind.BOT_CHECK:
        text = msg.BOT_CHECK
    elif kind is DownloadErrorKind.NOT_FOUND:
        text = msg.NOT_FOUND
    elif kind is DownloadErrorKind.NETWORK:
        text = msg.NETWORK_ERROR
    elif kind is DownloadErrorKind.TOO_LARGE:
        text = _too_large_text(0, settings)
    else:
        text = msg.UNKNOWN_ERROR

    await _safe_edit(status, text, kb.after_download_menu())


def _too_large_text(size: int, settings: Settings) -> str:
    template = msg.TOO_LARGE if settings.uses_local_api else msg.TOO_LARGE_NO_LOCAL
    return template.format(
        size=human_size(size) if size else "نامشخص",
        limit=human_size(settings.upload_limit),
    )


def _escape(text: str) -> str:
    """فرار دادن کاراکترهای HTML برای کپشن و کارت."""
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ----------------------------------------------------------------------
# خطای سراسری
# ----------------------------------------------------------------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("خطای مدیریت‌نشده", exc_info=context.error)

    if isinstance(update, Update) and update.effective_message:
        try:
            settings: Settings = context.application.bot_data["settings"]
            await update.effective_message.reply_text(
                msg.UNKNOWN_ERROR,
                parse_mode=ParseMode.HTML,
                reply_markup=kb.main_menu(settings.webapp_url),
            )
        except TelegramError:
            pass
