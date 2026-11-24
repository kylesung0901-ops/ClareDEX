"""ClareDEX v2 공통 설정.

- 경로, 기준 시간대(KST), 브랜드/컴플라이언스 문구
- .env 로더 (외부 패키지 없이)
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PUBLIC_DIR = BASE_DIR / "public"
IMAGE_DIR = PUBLIC_DIR / "images" / "posts"
DB_PATH = DATA_DIR / "claredex.db"
MARKET_CACHE_PATH = DATA_DIR / "market_cache.json"
CALENDAR_PATH = DATA_DIR / "calendar.json"

KST = ZoneInfo("Asia/Seoul")

BRAND = "클레어덱스"
BRAND_EN = "CLAREDEX"
OPERATOR = "웨일파트너스"
TELEGRAM_URL = "https://t.me/Clare_PerpDEX"
WEB_URL = "https://www.claredex.com/"
BLOG_URL = "https://blog.naver.com/cryptobrothers"
PUBLIC_BASE_URL = os.environ.get("CLAREDEX_PUBLIC_BASE_URL", "https://www.claredex.com").rstrip("/")

DISCLAIMER = (
    "본 정보는 투자 판단의 참고 자료이며, 특정 종목·자산의 매수·매도 권유나 "
    "수익 보장이 아닙니다. 투자 판단과 그 결과에 대한 책임은 투자자 본인에게 있습니다."
)
COMPLIANCE_LINE = "웨일파트너스는 유사투자자문업 신고를 준비 중입니다."

SEC_USER_AGENT = "ClareDEX AdminContact@claredex.com"
HTTP_USER_AGENT = "ClareDEX/1.0 (AdminContact@claredex.com)"
HTTP_TIMEOUT = 12

# 관심자산 (회원 온보딩 / API 검증)
WATCHLIST_ASSETS = ["BTC", "ETH", "나스닥", "코스피", "삼성전자", "NVDA"]

CATEGORIES = {
    "coin": {"label": "코인", "color": "#B45309"},
    "kr-stock": {"label": "국내주식", "color": "#14532D"},
    "us-stock": {"label": "미국주식", "color": "#1E3A5F"},
    "macro": {"label": "매크로·공시", "color": "#8A1C1C"},
    "disclosure": {"label": "공시", "color": "#3F3F46"},
}

SOURCE_GRADES = {
    "primary": "1차 공식",
    "filing": "전자공시",
    "exchange": "거래소",
    "market": "시장 데이터",
}


def load_env() -> None:
    """claredex-v2/.env 가 있으면 읽어 환경변수로 올린다 (기존 값 우선)."""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def now_kst() -> datetime:
    return datetime.now(tz=KST)


def iso_kst(dt: datetime | None = None) -> str:
    dt = dt or now_kst()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(KST).replace(microsecond=0).isoformat()


def parse_kst(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(KST)


def kst_from_utc_string(value: str) -> datetime:
    """RFC822(GMT) 문자열을 KST datetime 으로."""
    from email.utils import parsedate_to_datetime

    dt = parsedate_to_datetime(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KST)


def fmt_kst(dt: datetime, pattern: str = "%Y-%m-%d %H:%M") -> str:
    return dt.astimezone(KST).strftime(pattern)


def fmt_krw(value: float) -> str:
    return f"{value:,.0f}원"


def ensure_dirs() -> None:
    for path in (DATA_DIR, PUBLIC_DIR, IMAGE_DIR):
        path.mkdir(parents=True, exist_ok=True)


# 참고: 컨테이너의 UTC 오프셋 확인용 상수 (테스트에서 사용)
KST_OFFSET = timedelta(hours=9)
