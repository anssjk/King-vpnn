"""هندلرهای تلگرام (دستورها + دانلود + ارسال فایل)."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from telegram import Bot, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import ContextTypes

import utils as kb
import utils as msg
from config import Settings
from downloader import (
    DownloadCancelled,
    DownloadError,
    DownloadErrorKind,
    DownloadResult,
    Downloader,
    InvalidLink,
    UnsupportedSite,
    build_meta,
    clean_detail,
    parse_link,
    pick_height,
)
from utils import (
    Job,
    JobRegistry,
    ProgressTracker,
    human_duration,
    human_size,
    progress_bar,
    remove_tree,
    safe_filename,
)

logger = logging.getLogger(__name__)

# زمان شروع پروسه — برای نمایش «مدت فعالیت»
STARTED_AT = time.time()

# فاصله‌ی به‌روزرسانی پیام پیشرفت (ثانیه)
PROGRESS_INTERVAL = 4.0


def _uptime() -> str:
    seconds = int(time.time() - STARTED_AT)
    hours, rest = divmod(seconds, 3600)
    minutes, _ = divmod(rest, 60)
    if hours:
        return f"{hours} ساعت و {minutes} دقیقه"
    return f"{minutes} دقیقه"


def _menu(settings: Settings):
    return kb.main_menu(settings.webapp_url)


def _welcome(settings: Settings) -> str:
    return msg.WELCOME_WEBAPP if settings.has_webapp else msg.WELCOME


def _help_text(settings: Settings) -> str:
    return msg.HELP.format(
        limit=human_size(settings.upload_limit),
        duration=human_duration(settings.max_duration_sec),
    )


# ==================================================================
# دستورها و منو
# ==================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    await update.effective_message.reply_text(
        _welcome(settings),
        parse_mode=ParseMode.HTML,
        reply_markup=_menu(settings),
        disable_web_page_preview=True,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    await update.effective_message.reply_text(
        _help_text(settings),
        parse_mode=ParseMode.HTML,
        reply_markup=kb.back_menu(),
        disable_web_page_preview=True,
    )


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    settings: Settings = context.application.bot_data["settings"]
    await _safe_edit(query.message, _welcome(settings), _menu(settings))


async def on_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    settings: Settings = context.application.bot_data["settings"]
    await _safe_edit(query.message, _help_text(settings), kb.back_menu())


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
# ۱) دریافت لینک
# ==================================================================
async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return

    settings: Settings = context.application.bot_data["settings"]
    downloader: Downloader = context.application.bot_data["downloader"]
    registry: JobRegistry = context.application.bot_data["jobs"]
    main_kb = kb.main_menu(settings.webapp_url)

    try:
        link = parse_link(message.text)
    except InvalidLink:
        await message.reply_text(msg.NO_LINK, parse_mode=ParseMode.HTML, reply_markup=main_kb)
        return
    except UnsupportedSite:
        await message.reply_text(
            msg.UNSUPPORTED_SITE, parse_mode=ParseMode.HTML, reply_markup=main_kb
        )
        return

    platform_fa = msg.PLATFORM_NAMES.get(link.platform, link.platform)
    status = await message.reply_text(msg.PROBING, parse_mode=ParseMode.HTML)

    try:
        info = await downloader.probe(link.url)
    except DownloadError as exc:
        await _edit_error(status, exc, settings, link.platform)
        return
    except Exception:  # noqa: BLE001
        logger.exception("خطای پیش‌بینی‌نشده در probe")
        await _safe_edit(status, msg.UNKNOWN_ERROR, kb.after_download_menu())
        return

    meta = build_meta(info, settings, link.url)

    # --- محدودیت مدت ---
    if (
        settings.max_duration_sec
        and meta.duration
        and meta.duration > settings.max_duration_sec
    ):
        await _safe_edit(
            status,
            msg.TOO_LONG.format(limit=human_duration(settings.max_duration_sec)),
            main_kb,
        )
        return

    job = registry.create(
        url=link.url,
        platform=link.platform,
        title=meta.title,
        duration=meta.duration,
        heights=meta.heights,
        sizes=meta.sizes,
        portrait=meta.portrait,
        info={"webpage_url": meta.webpage_url},
    )

    card = _card_text(job, platform_fa, meta.best_size)
    if meta.heights and not meta.fitting:
        card += msg.ALL_TOO_LARGE

    await _safe_edit(
        status,
        card,
        kb.quality_menu(
            job.job_id,
            meta.heights,
            sizes=meta.sizes,
            limit=settings.upload_limit,
            best=meta.best_height,
        ),
    )


def _card_text(job: Job, platform_fa: str, approx: int | None) -> str:
    url = job.info.get("webpage_url") or job.url
    fields = dict(
        url=_escape(url),
        title=_escape(job.title),
        platform=platform_fa,
        duration=human_duration(job.duration),
    )
    if approx:
        return msg.CARD.format(size=human_size(approx), **fields)
    return msg.CARD_NO_SIZE.format(**fields)


# ==================================================================
# ۲) دکمه‌های کیفیت / صدا / لغو
# ==================================================================
async def on_quality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, job_id, value = kb.parse_callback(query.data or "")

    registry: JobRegistry = context.application.bot_data["jobs"]
    settings: Settings = context.application.bot_data["settings"]
    job = registry.get(job_id)

    if job is None:
        await query.answer(msg.JOB_EXPIRED, show_alert=True)
        return
    if job.running:
        await query.answer("⏳ این دانلود در حال اجراست.")
        return

    await query.answer("✅ شروع شد")

    requested: int | None = None
    if value not in ("best", ""):
        try:
            requested = int(value)
        except ValueError:
            requested = None

    # «بهترین» یعنی بهترین کیفیتی که در سقف ارسال جا می‌شود
    height = pick_height(job.heights, job.sizes, settings.upload_limit, requested)

    await _start_download(
        context=context, message=query.message, job=job,
        height=height, audio_only=False,
    )


async def on_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, job_id, _ = kb.parse_callback(query.data or "")

    registry: JobRegistry = context.application.bot_data["jobs"]
    job = registry.get(job_id)

    if job is None:
        await query.answer(msg.JOB_EXPIRED, show_alert=True)
        return
    if job.running:
        await query.answer("⏳ این دانلود در حال اجراست.")
        return

    await query.answer("🎵 فقط صدا")
    await _start_download(
        context=context, message=query.message, job=job,
        height=None, audio_only=True,
    )


async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, job_id, _ = kb.parse_callback(query.data or "")

    registry: JobRegistry = context.application.bot_data["jobs"]
    job = registry.get(job_id) if job_id else None

    if job is None or not job.running:
        await query.answer(msg.CANCEL_NOTHING)
        return

    job.cancel()
    await query.answer("🛑 لغو شد")
    await _safe_edit(query.message, msg.CANCELLING)


# ==================================================================
# ۳) اجرای دانلود
# ==================================================================
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

    status = message
    progress_task: asyncio.Task | None = None
    sent = False

    try:
        await _safe_edit(
            status,
            msg.DOWNLOADING_PLAIN.format(platform=platform_fa),
            kb.downloading_menu(job.job_id),
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
                heights=job.heights,
                portrait=job.portrait,
                audio_only=audio_only,
                cancel_event=job.cancel_event,
            )
        except DownloadCancelled:
            await _finish_progress(progress_task)
            await _safe_edit(status, msg.CANCELLED, kb.after_download_menu())
            return
        except DownloadError as exc:
            await _finish_progress(progress_task)
            await _edit_error(status, exc, settings, job.platform)
            return
        except Exception:  # noqa: BLE001
            logger.exception("خطای پیش‌بینی‌نشده در دانلود")
            await _finish_progress(progress_task)
            await _safe_edit(status, msg.UNKNOWN_ERROR, kb.after_download_menu())
            return

        await _finish_progress(progress_task)

        # --- بررسی حجم واقعی ---
        if result.filesize > settings.upload_limit:
            await _safe_edit(
                status,
                _too_large_text(result.filesize, settings),
                kb.after_download_menu(),
            )
            return

        # --- ارسال (پوشه‌ی موقت تا همین‌جا باید زنده بماند) ---
        await _safe_edit(status, msg.UPLOADING)
        try:
            await send_media(context.bot, message.chat_id, result, platform_fa)
            sent = True
        except TelegramError:
            logger.exception("ارسال فایل به تلگرام ناموفق بود")
            await _safe_edit(status, msg.SEND_FAILED, kb.after_download_menu())
            return

        await _safe_delete(status)
    finally:
        await _finish_progress(progress_task)
        job.running = False
        remove_tree(job_dir)
        if sent:
            registry.drop(job.job_id)


async def _finish_progress(task: asyncio.Task | None) -> None:
    if task is None or task.done():
        return
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
        if state.status == "processing":
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
            pass  # «message is not modified» یا پیام حذف شده
        except RetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
        except TelegramError:
            logger.debug("به‌روزرسانی پیشرفت ناموفق بود", exc_info=True)


# ==================================================================
# ۴) ارسال فایل — هم برای ربات، هم برای Mini App
# ==================================================================
_UPLOAD_TIMEOUTS = dict(
    read_timeout=600, write_timeout=600, connect_timeout=30, pool_timeout=60
)


async def _with_retry(factory):
    """یک‌بار تکرار اگر تلگرام گفت «صبر کن» یا تایم‌اوت شد."""
    try:
        return await factory()
    except RetryAfter as exc:
        await asyncio.sleep(exc.retry_after + 1)
        return await factory()


async def send_media(
    bot: Bot,
    chat_id: int,
    result: DownloadResult,
    platform_fa: str = "",
) -> None:
    """ویدیو/صدا را می‌فرستد؛ اگر تلگرام نپذیرفت، به‌صورت فایل می‌فرستد.

    اگر هیچ‌کدام کار نکند TelegramError پرتاب می‌شود.
    """
    title = _escape(result.title)
    size = human_size(result.filesize)
    ext = result.path.suffix or (".mp3" if result.is_audio else ".mp4")
    filename = safe_filename(result.title, ext)
    plat = platform_fa or "—"

    if result.is_audio:
        caption = msg.CAPTION_AUDIO.format(title=title, platform=plat, size=size)
        try:
            await bot.send_chat_action(chat_id, ChatAction.UPLOAD_VOICE)

            async def _send_audio():
                with result.path.open("rb") as fh:
                    return await bot.send_audio(
                        chat_id=chat_id,
                        audio=fh,
                        filename=filename,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        title=result.title[:64],
                        duration=result.duration,
                        **_UPLOAD_TIMEOUTS,
                    )

            await _with_retry(_send_audio)
            return
        except TelegramError as exc:
            logger.warning("ارسال صدا ناموفق، تلاش به‌عنوان فایل: %s", exc)
    else:
        caption = msg.CAPTION.format(title=title, platform=plat, size=size)
        try:
            await bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)

            async def _send_video():
                with result.path.open("rb") as fh:
                    return await bot.send_video(
                        chat_id=chat_id,
                        video=fh,
                        filename=filename,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        supports_streaming=True,
                        width=result.width,
                        height=result.height,
                        duration=result.duration,
                        **_UPLOAD_TIMEOUTS,
                    )

            await _with_retry(_send_video)
            return
        except TelegramError as exc:
            logger.warning("ارسال به‌عنوان ویدیو ناموفق، تلاش به‌عنوان فایل: %s", exc)

    async def _send_doc():
        with result.path.open("rb") as fh:
            return await bot.send_document(
                chat_id=chat_id,
                document=fh,
                filename=filename,
                caption=caption,
                parse_mode=ParseMode.HTML,
                **_UPLOAD_TIMEOUTS,
            )

    await _with_retry(_send_doc)


# ==================================================================
# ابزارها
# ==================================================================
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


async def _edit_error(
    status, exc: DownloadError, settings: Settings, platform: str = ""
) -> None:
    kind = exc.kind
    hint = ""

    if kind is DownloadErrorKind.PRIVATE:
        text = msg.PRIVATE_VIDEO
    elif kind is DownloadErrorKind.UNAVAILABLE:
        text = msg.VIDEO_UNAVAILABLE
    elif kind in (DownloadErrorKind.LOGIN_REQUIRED, DownloadErrorKind.BOT_CHECK):
        text = (
            msg.LOGIN_REQUIRED
            if kind is DownloadErrorKind.LOGIN_REQUIRED
            else msg.BOT_CHECK
        )
        hint = msg.HINT_BAD_COOKIES if settings.has_cookies else msg.HINT_NO_COOKIES
    elif kind is DownloadErrorKind.FORMAT:
        text = msg.FORMAT_ERROR
    elif kind is DownloadErrorKind.NO_VIDEO:
        text = msg.NO_VIDEO
    elif kind is DownloadErrorKind.LIVE:
        text = msg.LIVE_ERROR
    elif kind is DownloadErrorKind.NOT_FOUND:
        text = msg.NOT_FOUND
    elif kind is DownloadErrorKind.NETWORK:
        text = msg.NETWORK_ERROR
    elif kind is DownloadErrorKind.TOO_LARGE:
        text = _too_large_text(0, settings)
    else:
        text = msg.UNKNOWN_ERROR
        if exc.detail:
            # جزئیات فنی برای عیب‌یابی سریع توسط ادمین
            hint = f"\n\n<code>{_escape(clean_detail(exc.detail, 180))}</code>"

    await _safe_edit(status, text + hint, kb.after_download_menu())


def _too_large_text(size: int, settings: Settings) -> str:
    template = msg.TOO_LARGE if settings.uses_local_api else msg.TOO_LARGE_NO_LOCAL
    return template.format(
        size=human_size(size) if size else "نامشخص",
        limit=human_size(settings.upload_limit),
    )


def _escape(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ==================================================================
# خطای سراسری
# ==================================================================
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
