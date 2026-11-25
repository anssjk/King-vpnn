# --- تصویر پایه ---
FROM python:3.11-slim

# ffmpeg برای merge کردن ویدیو+صدا لازم است
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# اول وابستگی‌ها تا لایه‌ی کش بهتر کار کند
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# کاربر غیر root
RUN useradd -m -u 1000 botuser \
    && mkdir -p /app/downloads \
    && chown -R botuser:botuser /app
USER botuser

ENV PYTHONUNBUFFERED=1 \
    DOWNLOAD_DIR=/app/downloads

CMD ["python", "main.py"]
