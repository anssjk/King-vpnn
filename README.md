# ربات دانلودر تلگرام (Railway)

۱. ریپو را روی GitHub بگذار و در Railway «Deploy from GitHub repo» بزن.
۲. در Variables فقط `BOT_TOKEN` را بگذار.
۳. Settings ← Networking ← Generate Domain، بعد Redeploy.
۴. همان آدرس را در BotFather برای Mini App بگذار.

متغیرهای اختیاری: `MAX_FILESIZE_MB`، `MAX_DURATION_SEC`، `MAX_HEIGHT`،
`COOKIES_B64` (خروجی `base64 -w0 cookies.txt`)، `LOCAL_API_URL`، `LOG_LEVEL`.
`PORT` و `WEBAPP_URL` را خود Railway/برنامه تنظیم می‌کند.
