FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Seoul \
    PORT=8000

# tzdata: config.py 의 ZoneInfo("Asia/Seoul")
# fonts-noto-cjk: thumbnail.py 한글 썸네일 (NotoSansCJK-Regular.ttc)
# chromium 실행 라이브러리: Playwright 가 설치한다 (네이버 블로그 발행용)
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata fonts-noto-cjk libatomic1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 네이버 블로그 자동 발행용 브라우저 (실패해도 앱 자체는 뜨도록 별도 레이어)
RUN python -m playwright install --with-deps chromium || echo "WARN: playwright chromium 설치 실패 — 네이버 발행은 playwright_missing 으로 기록됩니다"

COPY config.py db.py authflow.py collector.py content.py thumbnail.py naver_drafts.py naver_publish.py export_static.py server.py verify.py ./
COPY calendar.json ./calendar.json
COPY public ./public
COPY collect.sh ./collect.sh

RUN mkdir -p /app/data /app/public/images/posts

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status==200 else 1)"

CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8000"]
