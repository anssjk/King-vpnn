"""ارائه‌دهنده‌های دانلود مبتنی بر API (Cobalt / TikWM / Piped / Invidious).

هر ارائه‌دهنده فقط «لینک مستقیم فایل» را برمی‌گرداند؛ دانلود واقعی را
`http_download` انجام می‌دهد (با پیشرفت، لغو و سقف حجم).

مهم: ساختار پاسخ این سرویس‌ها از روی مستندات است و روی شبکه‌ی واقعی
تست نشده؛ هر ارائه‌دهنده‌ای که خطا بدهد، موتور خودکار به بعدی می‌رود.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from config import Settings
from downloader import DownloadCancelled, DownloadError, DownloadErrorKind
from utils import ProgressTracker

logger = logging.getLogger(__name__)

API_TIMEOUT = aiohttp.ClientTimeout(total=25, sock_connect=10)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass
class Resolved:
    url: str
    title: str = ""
    ext: str = ""            # با نقطه، مثل ".mp4"؛ خالی = از Content-Type حدس بزن
    is_audio: bool = False
    provider: str = ""


# ------------------------------------------------------------------
# کمکی‌ها
# ------------------------------------------------------------------
_YT_ID_RE = re.compile(
    r"(?:youtu\.be/|youtube(?:-nocookie)?\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/|live/|v/))"
    r"([\w-]{11})"
)


def youtube_id(url: str) -> str | None:
    match = _YT_ID_RE.search(url)
    return match.group(1) if match else None


def _title_from_filename(name: str) -> str:
    """«My Video (1080p, h264, youtube).mp4» → «My Video»"""
    base = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", name or "")
    base = re.sub(r"\s*\([^)]*\)\s*$", "", base)
    return base.strip()


def _absolute(base: str, url: str) -> str:
    if url.startswith(("http://", "https://")):
        return url
    return base.rstrip("/") + "/" + url.lstrip("/")


def _pick_by_height(items: list[dict], height: int, key: str) -> dict | None:
    """بهترین گزینه با کیفیت ≤ height؛ اگر نبود، کمترین."""
    parsed = []
    for item in items:
        match = re.search(r"(\d{3,4})", str(item.get(key) or ""))
        if item.get("url") and match:
            parsed.append((int(match.group(1)), item))
    if not parsed:
        return None
    ok = [p for p in parsed if p[0] <= height]
    if ok:
        return max(ok, key=lambda p: p[0])[1]
    return min(parsed, key=lambda p: p[0])[1]


def _map_cobalt_error(code: str) -> DownloadErrorKind:
    code = (code or "").lower()
    if "private" in code:
        return DownloadErrorKind.PRIVATE
    if any(x in code for x in ("login", "age", "auth", "fetch.empty", "jwt")):
        return DownloadErrorKind.LOGIN_REQUIRED
    if any(x in code for x in ("rate", "limit", "captcha", "bot", "youtube.api")):
        return DownloadErrorKind.BOT_CHECK
    if any(x in code for x in ("unavailable", "not_found", "notfound", "fetch.fail")):
        return DownloadErrorKind.UNAVAILABLE
    if "too_long" in code or "length" in code:
        return DownloadErrorKind.TOO_LONG
    if "live" in code:
        return DownloadErrorKind.LIVE
    if "unsupported" in code or "link.invalid" in code:
        return DownloadErrorKind.NOT_FOUND
    return DownloadErrorKind.UNKNOWN


# ------------------------------------------------------------------
# Cobalt
# ------------------------------------------------------------------
async def cobalt(
    session: aiohttp.ClientSession,
    base: str,
    api_key: str,
    url: str,
    height: int,
    audio_only: bool,
) -> Resolved:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Api-Key {api_key}"

    body = {
        "url": url,
        "videoQuality": str(height),
        "downloadMode": "audio" if audio_only else "auto",
        "audioFormat": "mp3",
        "filenameStyle": "basic",
        "youtubeVideoCodec": "h264",
        "allowH265": False,
    }
    async with session.post(base + "/", json=body, headers=headers) as resp:
        try:
            data = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            raise DownloadError(
                DownloadErrorKind.NETWORK, f"cobalt HTTP {resp.status}"
            ) from exc

    status = data.get("status")
    if status in ("tunnel", "redirect"):
        return Resolved(
            url=data["url"],
            title=_title_from_filename(data.get("filename", "")),
            ext=Path(data.get("filename", "")).suffix,
            is_audio=audio_only,
            provider="cobalt",
        )
    if status == "picker":
        for item in data.get("picker") or []:
            if item.get("type") in ("video", "gif") and item.get("url"):
                return Resolved(url=item["url"], provider="cobalt")
        if audio_only and data.get("audio"):
            return Resolved(url=data["audio"], is_audio=True, provider="cobalt")
        raise DownloadError(DownloadErrorKind.NO_VIDEO, "cobalt: picker without video")
    if status == "error":
        code = (data.get("error") or {}).get("code", "")
        raise DownloadError(_map_cobalt_error(code), f"cobalt: {code}")
    raise DownloadError(DownloadErrorKind.UNKNOWN, f"cobalt: status={status}")


# ------------------------------------------------------------------
# TikWM (فقط تیک‌تاک)
# ------------------------------------------------------------------
async def tikwm(
    session: aiohttp.ClientSession, url: str, height: int, audio_only: bool
) -> Resolved:
    base = "https://www.tikwm.com"
    data: dict = {}
    for attempt in range(2):
        async with session.get(
            f"{base}/api/", params={"url": url, "hd": "1"}
        ) as resp:
            data = await resp.json(content_type=None)
        msg = str(data.get("msg") or "")
        if data.get("code") == -1 and "limit" in msg.lower() and attempt == 0:
            await asyncio.sleep(1.5)  # «۱ درخواست در ثانیه»
            continue
        break

    if data.get("code") != 0 or not data.get("data"):
        msg = str(data.get("msg") or "")
        kind = (
            DownloadErrorKind.BOT_CHECK if "limit" in msg.lower()
            else DownloadErrorKind.NOT_FOUND
        )
        raise DownloadError(kind, f"tikwm: {msg}")

    info = data["data"]
    title = (info.get("title") or "").strip()

    if audio_only:
        music = info.get("music")
        if not music:
            raise DownloadError(DownloadErrorKind.NO_VIDEO, "tikwm: no audio")
        return Resolved(url=_absolute(base, music), title=title, ext=".mp3",
                        is_audio=True, provider="tikwm")

    video = info.get("hdplay") or info.get("play")
    if not video:
        raise DownloadError(DownloadErrorKind.NO_VIDEO, "tikwm: slideshow/no video")
    return Resolved(url=_absolute(base, video), title=title, ext=".mp4", provider="tikwm")


# ------------------------------------------------------------------
# Piped (فقط یوتیوب)
# ------------------------------------------------------------------
async def piped(
    session: aiohttp.ClientSession, base: str, url: str, height: int, audio_only: bool
) -> Resolved:
    vid = youtube_id(url)
    if not vid:
        raise DownloadError(DownloadErrorKind.NOT_FOUND, "piped: no youtube id")

    async with session.get(f"{base}/streams/{vid}") as resp:
        if resp.status >= 400:
            raise DownloadError(DownloadErrorKind.NETWORK, f"piped HTTP {resp.status}")
        data = await resp.json(content_type=None)

    if data.get("error"):
        raise DownloadError(DownloadErrorKind.UNAVAILABLE, f"piped: {data['error']}"[:150])

    title = data.get("title") or ""
    if audio_only:
        audios = [a for a in data.get("audioStreams") or [] if a.get("url")]
        if not audios:
            raise DownloadError(DownloadErrorKind.NO_VIDEO, "piped: no audio")
        best = max(audios, key=lambda a: a.get("bitrate") or 0)
        ext = ".m4a" if "mp4" in str(best.get("mimeType")) else ".webm"
        return Resolved(url=best["url"], title=title, ext=ext, is_audio=True, provider="piped")

    # فقط فایل‌های «ویدیو+صدا» (progressive)؛ معمولاً تا ۷۲۰p
    progressive = [
        v for v in data.get("videoStreams") or []
        if not v.get("videoOnly") and "mp4" in str(v.get("mimeType", "")).lower()
    ]
    chosen = _pick_by_height(progressive, height, "quality")
    if not chosen:
        raise DownloadError(DownloadErrorKind.FORMAT, "piped: no progressive stream")
    return Resolved(url=chosen["url"], title=title, ext=".mp4", provider="piped")


# ------------------------------------------------------------------
# Invidious (فقط یوتیوب)
# ------------------------------------------------------------------
async def invidious(
    session: aiohttp.ClientSession, base: str, url: str, height: int, audio_only: bool
) -> Resolved:
    vid = youtube_id(url)
    if not vid:
        raise DownloadError(DownloadErrorKind.NOT_FOUND, "invidious: no youtube id")

    async with session.get(
        f"{base}/api/v1/videos/{vid}",
        params={"local": "true", "fields": "title,formatStreams,adaptiveFormats,error"},
    ) as resp:
        if resp.status >= 400:
            raise DownloadError(DownloadErrorKind.NETWORK, f"invidious HTTP {resp.status}")
        data = await resp.json(content_type=None)

    if data.get("error"):
        raise DownloadError(DownloadErrorKind.UNAVAILABLE, f"invidious: {data['error']}"[:150])

    title = data.get("title") or ""
    if audio_only:
        audios = [
            f for f in data.get("adaptiveFormats") or []
            if str(f.get("type", "")).startswith("audio/mp4") and f.get("url")
        ]
        if not audios:
            raise DownloadError(DownloadErrorKind.NO_VIDEO, "invidious: no audio")
        best = max(audios, key=lambda f: int(f.get("bitrate") or 0))
        return Resolved(url=_absolute(base, best["url"]), title=title, ext=".m4a",
                        is_audio=True, provider="invidious")

    streams = [
        f for f in data.get("formatStreams") or []
        if str(f.get("container", "")) == "mp4" or "mp4" in str(f.get("type", ""))
    ]
    chosen = _pick_by_height(streams, height, "qualityLabel")
    if not chosen:
        raise DownloadError(DownloadErrorKind.FORMAT, "invidious: no progressive stream")
    return Resolved(url=_absolute(base, chosen["url"]), title=title, ext=".mp4",
                    provider="invidious")


# ------------------------------------------------------------------
# فهرست تلاش‌ها: [(نام نمایشی, تابع resolve)]
# ------------------------------------------------------------------
def build_attempts(settings: Settings, platform: str):
    """برای این پلتفرم، به ترتیب PROVIDERS لیست تلاش‌ها را می‌سازد."""
    attempts = []
    for name in settings.providers:
        if name == "cobalt":
            for base in settings.cobalt_urls:
                attempts.append(
                    (f"cobalt@{urlparse(base).hostname}",
                     lambda s, u, h, a, b=base: cobalt(s, b, settings.cobalt_api_key, u, h, a))
                )
        elif name == "tikwm" and platform == "tiktok":
            attempts.append(("tikwm", lambda s, u, h, a: tikwm(s, u, h, a)))
        elif name == "piped" and platform == "youtube":
            for base in settings.piped_urls:
                attempts.append(
                    (f"piped@{urlparse(base).hostname}",
                     lambda s, u, h, a, b=base: piped(s, b, u, h, a))
                )
        elif name == "invidious" and platform == "youtube":
            for base in settings.invidious_urls:
                attempts.append(
                    (f"invidious@{urlparse(base).hostname}",
                     lambda s, u, h, a, b=base: invidious(s, b, u, h, a))
                )
    return attempts


def new_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        timeout=API_TIMEOUT, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    )


# ------------------------------------------------------------------
# دانلود مستقیم فایل
# ------------------------------------------------------------------
_CT_EXT = {
    "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
    "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
    "audio/webm": ".webm", "audio/ogg": ".ogg", "audio/aac": ".aac",
}


async def http_download(
    session: aiohttp.ClientSession,
    resolved: Resolved,
    out_dir: Path,
    tracker: ProgressTracker,
    cancel_event: threading.Event | None,
    max_bytes: int,
) -> Path:
    """فایل را استریم می‌کند. حجم بیش از max_bytes → TOO_LARGE (قبل یا حین دانلود)."""
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=60)
    async with session.get(resolved.url, timeout=timeout, allow_redirects=True) as resp:
        if resp.status >= 400:
            raise DownloadError(DownloadErrorKind.NETWORK, f"file HTTP {resp.status}")

        total = int(resp.headers.get("Content-Length") or 0)
        if total and total > max_bytes:
            raise DownloadError(DownloadErrorKind.TOO_LARGE, f"{total} bytes")

        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype.startswith("text/") or ctype == "application/json":
            raise DownloadError(DownloadErrorKind.FORMAT, f"unexpected {ctype}")

        ext = resolved.ext or _CT_EXT.get(ctype) or (".mp3" if resolved.is_audio else ".mp4")
        path = out_dir / f"file{ext}"

        done = 0
        loop = asyncio.get_running_loop()
        started = loop.time()
        tracker.update(status="downloading", downloaded=0, total=total)
        try:
            with path.open("wb") as fh:
                async for chunk in resp.content.iter_chunked(256 * 1024):
                    if cancel_event is not None and cancel_event.is_set():
                        raise DownloadCancelled("cancelled by user")
                    fh.write(chunk)
                    done += len(chunk)
                    if done > max_bytes:
                        raise DownloadError(DownloadErrorKind.TOO_LARGE, f">{max_bytes}")
                    elapsed = max(loop.time() - started, 0.001)
                    tracker.update(downloaded=done, speed=done / elapsed,
                                   total=total or done)
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    if done == 0:
        path.unlink(missing_ok=True)
        raise DownloadError(DownloadErrorKind.NETWORK, "empty file")
    tracker.update(status="processing")
    return path
