FROM python:3.12-slim

# Tesseract(로컬 OCR, kor+eng)·시간대 데이터·ffmpeg(음악 스트리밍, 의존성으로 libopus 포함).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-kor \
        tzdata \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Seoul

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests

# 비루트 실행
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /srv
USER appuser

CMD ["python", "-m", "app.main"]
