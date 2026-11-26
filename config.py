"""ClareDEX v2 공통 설정.

- 경로, 기준 시간대(KST), 브랜드/컴플라이언스 문구
- .env 로더 (외부 패키지 없이)
"""
from __future__ import annotations

import json
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
# 공식 일정은 저장소에 커밋해 버전 관리한다(공식 원문에서 확인한 일정만).
# 예전 배포본이 쓰던 data/calendar.json 은 읽기 폴백으로만 사용한다(운영 DB·볼륨 보존).
CALENDAR_PATH = BASE_DIR / "calendar.json"
LEGACY_CALENDAR_PATH = DATA_DIR / "calendar.json"

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

SEC_USER_AGENT = "ClareDEX AdminContact@claredex.com"
HTTP_USER_AGENT = "ClareDEX/1.0 (AdminContact@claredex.com)"
HTTP_TIMEOUT = 12

# 관심자산 (회원 온보딩 / API 검증)
WATCHLIST_ASSETS = ["BTC", "ETH", "나스닥", "코스피", "삼성전자", "NVDA", "AAPL", "연준·금리"]

# 공식 일정 알림 신청(signup.html?watch=FOMC) → 관심자산 매핑.
# 일정은 별도 구독 목록을 만들지 않고, 그 발표를 다루는 관심자산 알림으로 함께 설정한다.
# (public/signup.html 의 EVENT_ALIAS 와 같은 규칙을 유지한다)
EVENT_WATCH_ALIASES = {
    "FOMC": "연준·금리",
    "CPI": "연준·금리",
    "PCE": "연준·금리",
    "GDP": "연준·금리",
    "고용보고서": "연준·금리",
    "무역수지": "연준·금리",
}

# ---------------------------------------------------------------- 중요도 등급
# S(긴급) · A(주요) · B(일반). 화면에서는 S 를 빨간 배지로 강조한다.
IMPORTANCE_LEVELS = {
    "S": {"label": "긴급", "order": 0, "color": "#B3261E"},
    "A": {"label": "주요", "order": 1, "color": "#8C6B2F"},
    "B": {"label": "일반", "order": 2, "color": "#5D5A50"},
}
IMPORTANCE_DEFAULT = "B"

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


# ---------------------------------------------------------------- 공식 일정

def normalize_calendar(data: dict | None) -> dict:
    """calendar.json 을 화면·API 가 그대로 쓸 수 있는 형태로 정리한다.

    · 빈 칸(—·""·None)은 쓰지 않는다. 제목·발표 시각이 없는 일정은 목록에서 빼고,
      부가 문구는 기관 공식 일정 페이지 기준의 기본 문장으로 채운다.
    · importance 는 S/A/B 만 인정한다(그 외 값은 A).
    """
    data = data if isinstance(data, dict) else {}
    events: list[dict] = []
    seen: set[str] = set()
    for raw in data.get("events") or []:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        datetime_kst = str(raw.get("datetime_kst") or "").strip()
        org = str(raw.get("org") or "").strip()
        if not title or not org or not datetime_kst:
            continue
        try:
            parse_kst(datetime_kst)
        except Exception:
            continue
        event_id = str(raw.get("id") or f"{org}-{datetime_kst}").strip()
        if event_id in seen:
            continue
        seen.add(event_id)
        level = str(raw.get("importance") or "A").upper()
        if level not in IMPORTANCE_LEVELS:
            level = "A"
        event = {
            "id": event_id,
            "title": title,
            "org": org,
            "datetime_kst": datetime_kst,
            "local_time": str(raw.get("local_time") or "").strip(),
            "note": str(raw.get("note") or "").strip()
                    or f"{org} 공식 일정 페이지에서 확인한 발표 예정 시각입니다.",
            "checkpoint": str(raw.get("checkpoint") or "").strip(),
            "watch": str(raw.get("watch") or "").strip(),
            "source_url": str(raw.get("source_url") or "").strip(),
            "grade": str(raw.get("grade") or SOURCE_GRADES["primary"]).strip(),
            "importance": level,
            "importance_label": IMPORTANCE_LEVELS[level]["label"],
        }
        events.append(event)
    events.sort(key=lambda e: parse_kst(e["datetime_kst"]))
    out = {k: v for k, v in data.items() if k != "events"}
    out["ok"] = bool(data.get("ok", True))
    out["timezone"] = out.get("timezone") or "Asia/Seoul"
    out["events"] = events
    return out


def load_calendar() -> dict:
    """저장소의 calendar.json → (없으면) data/calendar.json 순서로 읽는다."""
    for path in (CALENDAR_PATH, LEGACY_CALENDAR_PATH):
        try:
            return normalize_calendar(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return normalize_calendar({"events": []})


# 참고: 컨테이너의 UTC 오프셋 확인용 상수 (테스트에서 사용)
KST_OFFSET = timedelta(hours=9)
