# --- تصویر پایه ---
FROM python:3.11-slim

# ffmpeg برای merge کردن ویدیو+صدا و تبدیل به MP3 لازم است
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Deno: یوتیوب بدون JS runtime فرمت‌ها را نشان نمی‌دهد
# (yt-dlp برای حل چالش n/sig یوتیوب به آن نیاز دارد)
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app

COPY requirements.txt .

# با هر نسخه‌ی جدید yt-dlp این فایل عوض می‌شود و لایه‌ی کش دوباره ساخته می‌شود؛
# پس هر Redeploy آخرین yt-dlp را نصب می‌کند (سایت‌ها مدام تغییر می‌کنند).
ADD https://pypi.org/pypi/yt-dlp/json /tmp/yt-dlp-latest.json

RUN pip install --no-cache-dir -U -r requirements.txt

COPY . .

# کاربر غیر root
RUN useradd -m -u 1000 botuser \
    && mkdir -p /app/downloads \
    && chown -R botuser:botuser /app
USER botuser

ENV PYTHONUNBUFFERED=1 \
    DOWNLOAD_DIR=/app/downloads \
    DENO_DIR=/home/botuser/.deno

CMD ["python", "main.py"]
