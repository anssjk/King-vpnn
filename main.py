"""نقطه‌ی ورود واحد — ربات تلگرام + وب‌سرور Mini App در یک پروسه.

مناسب Railway (هر سرویس = یک پروسه). هر دو بخش یک event loop، یک
Downloader و یک پوشه‌ی دانلود را شریک‌اند.

اجرا:
    python main.py
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import signal
import sys

from aiohttp import web
from telegram import Bot, BotCommand
from telegram.error import InvalidToken, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

import utils as kb
from config import ConfigError, Settings, get_settings
from downloader import Downloader, DownloadResult, ffmpeg_available
from handlers import (
    cmd_help,
    cmd_start,
    handle_link,
    on_audio,
    on_cancel,
    on_error,
    on_help,
    on_menu,
    on_platforms,
    on_quality,
    on_status,
    send_media,
)
from utils import JobRegistry, human_size, purge_old_files
from webapp import DownloadService, create_app

logger = logging.getLogger(__name__)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        level=getattr(logging, level, logging.INFO),
        stream=sys.stdout,
    )
    # httpx در هر درخواست لاگ زیادی می‌نویسد
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)


def build_application(settings: Settings) -> Application:
    """اپلیکیشن را می‌سازد. جدا شده تا در تست هم قابل استفاده باشد."""

    # تایم‌اوت‌های بلند: آپلود ویدیوی چند صد مگابایتی زمان می‌برد
    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30.0,
        read_timeout=300.0,
        write_timeout=300.0,
        pool_timeout=30.0,
    )

    builder = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .request(request)
        .get_updates_request(
            HTTPXRequest(connection_pool_size=4, read_timeout=60.0)
        )
        .concurrent_updates(True)
    )

    # سرور محلی Bot API برای فایل‌های بزرگ
    if settings.local_api_url:
        builder = builder.base_url(
            f"{settings.local_api_url.rstrip('/')}/bot"
        )
        builder = builder.base_file_url(
            f"{settings.local_api_url.rstrip('/')}/file/bot"
        )
        logger.info("استفاده از سرور محلی Bot API: %s", settings.local_api_url)

    application = builder.build()

    # --- وابستگی‌ها ---
    application.bot_data["settings"] = settings
    application.bot_data["downloader"] = Downloader(settings)
    application.bot_data["jobs"] = JobRegistry()

    # --- دستورها ---
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))

    # --- دکمه‌های اینلاین ---
    # هر عمل یک الگوی regex دارد تا callback_data ناخواسته به هندلر غلط نرود
    application.add_handler(
        CallbackQueryHandler(on_menu, pattern=rf"^{kb.ACT_MENU}:")
    )
    application.add_handler(
        CallbackQueryHandler(on_help, pattern=rf"^{kb.ACT_HELP}:")
    )
    application.add_handler(
        CallbackQueryHandler(on_platforms, pattern=rf"^{kb.ACT_PLATFORMS}:")
    )
    application.add_handler(
        CallbackQueryHandler(on_status, pattern=rf"^{kb.ACT_STATUS}:")
    )
    application.add_handler(
        CallbackQueryHandler(on_quality, pattern=rf"^{kb.ACT_QUALITY}:")
    )
    application.add_handler(
        CallbackQueryHandler(on_audio, pattern=rf"^{kb.ACT_AUDIO}:")
    )
    application.add_handler(
        CallbackQueryHandler(on_cancel, pattern=rf"^{kb.ACT_CANCEL}:")
    )

    # --- لینک ---
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link)
    )
    application.add_error_handler(on_error)

    return application


async def _post_init(application: Application) -> None:
    """کارهای بعد از اتصال: منوی دستورها + پاک‌سازی فایل‌های قدیمی."""
    settings: Settings = application.bot_data["settings"]

    await application.bot.set_my_commands(
        [
            BotCommand("start", "شروع"),
            BotCommand("help", "راهنما"),
        ]
    )

    # --- دکمه‌ی منوی چت (کنار فیلد تایپ) که Mini App را باز می‌کند ---
    if settings.has_webapp:
        try:
            from telegram import MenuButtonWebApp, WebAppInfo

            await application.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Mini App",
                    web_app=WebAppInfo(url=settings.webapp_url),
                )
            )
            logger.info("دکمه‌ی منوی Mini App ثبت شد: %s", settings.webapp_url)
        except Exception:  # noqa: BLE001 - نسخه‌های قدیمی یا آدرس نامعتبر
            logger.warning("ثبت دکمه‌ی منوی Mini App ناموفق بود", exc_info=True)

    settings.download_dir.mkdir(parents=True, exist_ok=True)
    purge_old_files(settings.download_dir, max_age_seconds=3600)

    me = await application.bot.get_me()
    logger.info("ربات @%s آماده است.", me.username)


def make_sender(bot: Bot, settings: Settings):
    """تابع ارسال فایل به چت — همان چیزی که سرویس Mini App انتظار دارد.

    منطق ارسال با ربات مشترک است (handlers.send_media).
    """

    async def send(chat_id: int, result: DownloadResult) -> None:
        await send_media(bot, chat_id, result, result.platform or "")

    return send


def log_environment() -> None:
    """نسخه‌ی ابزارها را در لاگ می‌نویسد؛ برای عیب‌یابی دانلود خیلی مفید است."""
    try:
        import yt_dlp

        ytdlp_version = yt_dlp.version.__version__
    except Exception:  # noqa: BLE001
        ytdlp_version = "؟"

    js_runtime = next(
        (name for name in ("deno", "node", "bun") if shutil.which(name)), None
    )
    logger.info(
        "yt-dlp=%s | ffmpeg=%s | JS runtime=%s",
        ytdlp_version,
        "هست" if ffmpeg_available() else "نیست",
        js_runtime or "نیست",
    )
    if js_runtime is None:
        logger.warning(
            "هیچ JS runtime (deno/node) نصب نیست؛ یوتیوب بدون آن معمولاً "
            "فرمت‌ها را نشان نمی‌دهد. Dockerfile جدید را استفاده کنید."
        )


async def _purge_loop(settings: Settings) -> None:
    """هر نیم‌ساعت پوشه‌های جامانده از دانلودهای قدیمی را پاک می‌کند."""
    while True:
        await asyncio.sleep(30 * 60)
        try:
            purge_old_files(settings.download_dir, max_age_seconds=3600)
        except Exception:  # noqa: BLE001
            logger.debug("پاک‌سازی دوره‌ای ناموفق بود", exc_info=True)


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """SIGTERM (که Railway موقع دیپلوی می‌فرستد) و SIGINT را می‌گیرد."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # ویندوز
            signal.signal(sig, lambda *_: stop.set())


async def run(settings: Settings) -> None:
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    log_environment()
    if not settings.has_cookies:
        logger.info(
            "کوکی تنظیم نشده (COOKIES_B64). اینستاگرام و بعضی ویدیوهای "
            "یوتیوب بدون کوکی ممکن است دانلود نشوند."
        )

    application = build_application(settings)

    # وب‌سرور از همان Bot و همان Downloader ربات استفاده می‌کند
    service = DownloadService(
        settings,
        application.bot_data["downloader"],
        sender=make_sender(application.bot, settings),
    )
    web_app = create_app(settings, service)
    runner = web.AppRunner(web_app)

    stop = asyncio.Event()
    _install_signal_handlers(stop)

    # ۱) اول وب‌سرور: healthcheck ریلوی باید زود جواب بگیرد
    await runner.setup()
    site = web.TCPSite(runner, settings.web_host, settings.web_port)
    await site.start()
    logger.info(
        "وب‌سرور روی %s:%d بالا آمد (سقف ارسال: %s)",
        settings.web_host,
        settings.web_port,
        human_size(settings.upload_limit),
    )

    # ۲) بعد ربات
    purge_task = asyncio.create_task(_purge_loop(settings))
    try:
        await application.initialize()
        await _post_init(application)
        await application.updater.start_polling(
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True,
        )
        await application.start()
        logger.info("در حال گوش دادن به تلگرام...")

        await stop.wait()
        logger.info("سیگنال خاموشی دریافت شد؛ در حال بستن...")
    finally:
        purge_task.cancel()
        for step in (
            application.updater.stop,
            application.stop,
            application.shutdown,
        ):
            try:
                await step()
            except (TelegramError, RuntimeError):
                logger.debug("خطا هنگام خاموش کردن %s", step, exc_info=True)
        await runner.cleanup()


def main() -> int:
    try:
        settings = get_settings()
    except ConfigError as exc:
        print(f"❌ خطای تنظیمات:\n{exc}", file=sys.stderr)
        return 1

    setup_logging(settings.log_level)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    logger.info(
        "سقف ارسال فایل: %s | حالت: %s | Mini App: %s",
        human_size(settings.upload_limit),
        "سرور محلی" if settings.uses_local_api else "api.telegram.org",
        settings.webapp_url or "غیرفعال (WEBAPP_URL خالی است)",
    )

    try:
        asyncio.run(run(settings))
    except InvalidToken:
        print(
            "❌ BOT_TOKEN نامعتبر است (تلگرام قبولش نکرد).\n"
            "توکن را دوباره از BotFather کپی کنید و در Railway ← Variables بگذارید.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
