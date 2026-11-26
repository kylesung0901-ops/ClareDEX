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
# 공식 일정은 저장소에 커밋해 버전 관리한다(검증된 원문만).
# 예전 배포본이 쓰던 data/calendar.json 은 읽기 폴백으로만 사용한다.
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
COMPLIANCE_LINE = "웨일파트너스는 유사투자자문업 신고를 준비 중입니다."

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

# ---------------------------------------------------------------- 중요도 등급
# S(긴급) · A(주요) · B(일반). 화면에서는 S 를 빨간 배지로 강조한다.
IMPORTANCE_LEVELS = {
    "S": {"label": "긴급", "order": 0, "color": "#B3261E"},
    "A": {"label": "주요", "order": 1, "color": "#8C6B2F"},
    "B": {"label": "일반", "order": 2, "color": "#5D5A50"},
}
IMPORTANCE_DEFAULT = "B"


# ---------------------------------------------------------------- 회원 플랜
# 가격은 아직 확정하지 않았다. 화면·API 어디에도 추측 값을 쓰지 않는다.
PLANS = {
    "free": {
        "label": "무료 회원",
        "status": "이용 가능",
        "tagline": "휴대폰 인증 한 번으로 관심자산 속보를 받습니다.",
        "features": [
            "내 관심자산 실시간 알림 (최대 3개)",
            "3초 요약 전문 열람",
            "S등급 긴급 속보 알림",
            "이번 주 공식 일정 캘린더",
        ],
    },
    "pro": {
        "label": "Pro",
        "status": "출시 준비 중",
        "tagline": "발표를 기다리지 않고 먼저 받는 플랜입니다.",
        "features": [
            "무지연 속보 (수집 즉시)",
            "공시 즉시 분석 (SEC 8-K·10-Q · DART)",
            "무제한 관심자산",
            "개인 텔레그램 봇 · 카카오 알림톡 연동",
        ],
    },
}


# ---------------------------------------------------------------- 휴대폰 본인인증
PHONE_CODE_TTL = 300          # 인증번호 유효 시간(초)
PHONE_TOKEN_TTL = 1800        # 인증 완료 토큰 유효 시간(초)
PHONE_MAX_ATTEMPTS = 5        # 인증번호 입력 시도 제한
PHONE_RESEND_WINDOW = 600     # 재발송 제한 구간(초)
PHONE_MAX_SENDS = 3           # 같은 번호로 구간 내 최대 발송 횟수


def sms_configured() -> bool:
    """실제 SMS 발송 경로가 설정되어 있는지. 없으면 개발/데모 모드로 동작한다."""
    return bool((os.environ.get("SMS_WEBHOOK_URL") or "").strip())


def sms_mode() -> str:
    return "live" if sms_configured() else "demo"


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
EVENT_REQUIRED_FIELDS = ("id", "title", "org", "datetime_kst", "local_time", "source_url")


def normalize_calendar(data: dict | None) -> dict:
    """공식 일정 JSON 을 화면·API 공통 형태로 정리한다.

    - 필수 값이 비어 있는 일정은 버린다(화면에 빈 칸 `—` 가 나가지 않도록).
    - KST 시각을 ISO 로 통일하고 중요도 기본값을 채운다.
    - 시작 시각 오름차순으로 정렬한다.
    """
    data = data if isinstance(data, dict) else {}
    events: list[dict] = []
    seen: set[str] = set()
    for raw in data.get("events") or []:
        if not isinstance(raw, dict):
            continue
        if any(not str(raw.get(key) or "").strip() for key in EVENT_REQUIRED_FIELDS):
            continue
        try:
            when = parse_kst(str(raw["datetime_kst"]))
        except Exception:
            continue
        event = dict(raw)
        event["datetime_kst"] = iso_kst(when)
        event["id"] = str(event["id"]).strip()
        if event["id"] in seen:
            continue
        seen.add(event["id"])
        level = str(event.get("importance") or "A").upper()
        if level not in IMPORTANCE_LEVELS:
            level = "A"
        event["importance"] = level
        event["importance_label"] = IMPORTANCE_LEVELS[level]["label"]
        event["note"] = str(event.get("note") or f"{event['org']} 공식 일정 페이지에서 확인한 발표 예정 시각입니다.").strip()
        event["grade"] = str(event.get("grade") or SOURCE_GRADES["primary"])
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
