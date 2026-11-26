"""ClareDEX v2 자체 검증 스크립트.

1) DB 스키마·글·초안·썸네일 무결성
2) 실제 서버를 임시 포트로 띄워 HTTP 200 / JSON / HTML / POST 동작 확인 후 정상 종료
3) 운영자(/admin) 경로·인증·정적 프리뷰 비공개 확인

사용: python3 verify.py [--port 8765]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import config

config.load_env()

# 검증 전용 운영자 비밀번호·해시 파일 (실제 운영 비밀번호/파일은 건드리지 않는다)
VERIFY_ADMIN_PASSWORD = "verify-admin-password-2026"
VERIFY_NEW_PASSWORD = "verify-admin-new-password-2026"
VERIFY_TMP = Path(tempfile.mkdtemp(prefix="claredex-verify-"))
os.environ["ADMIN_PASSWORD"] = VERIFY_ADMIN_PASSWORD
os.environ["CLAREDEX_ADMIN_AUTH_FILE"] = str(VERIFY_TMP / "admin_auth.json")
os.environ["CLAREDEX_ADMIN_AUDIT_FILE"] = str(VERIFY_TMP / "admin_audit.jsonl")

import db  # noqa: E402
import server  # noqa: E402  (운영자 비밀번호·세션 규칙을 한 곳에서 가져온다)

# 수집기 실행 테스트는 실제 네트워크 대신 스텁 단계로 검사한다 (작업 파일도 임시 경로)
server.COLLECT_STATE_PATH = VERIFY_TMP / "collector_state.json"

RESULTS: list[tuple[bool, str, str]] = []

# 블로그 초안에 다시는 들어가면 안 되는 문구 (신고 상태를 단정하는 표현)
COMPLIANCE_BANNED = ("유사투자자문", "신고를 준비", "신고 준비 중", "신고 예정")


def check(ok: bool, name: str, detail: str = "") -> bool:
    RESULTS.append((bool(ok), name, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f"  — {detail}" if detail else ""))
    return bool(ok)


def http(url: str, method: str = "GET", body: dict | None = None, timeout: int = 15, cookie: str | None = None):
    data = None
    headers = {"Accept": "*/*"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, dict(resp.headers), raw
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def db_checks() -> None:
    print("\n── 1. DB · 콘텐츠 무결성 ─────────────────────────────────")
    if not config.DB_PATH.exists():
        check(False, "DB 파일", str(config.DB_PATH))
        return
    db.init_db()  # 서버 기동과 같은 스키마·마이그레이션을 먼저 적용해 검사한다
    conn = db.connect()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("posts", "users", "naver_drafts", "auth_otp", "signup_verification", "phone_auth_state",
              "naver_publish", "alert_rules", "alert_logs"):
        check(t in tables, f"테이블 존재: {t}")

    post_cols = {r[1] for r in conn.execute("PRAGMA table_info(posts)")}
    required = {"id", "title", "category", "source_name", "source_grade", "source_url", "source_hash",
                "verified_at", "summary_lines", "market_impact", "tags", "image_path",
                "is_member_early", "created_at"}
    check(required <= post_cols, "posts 필수 컬럼", ", ".join(sorted(required - post_cols)) or "모두 존재")
    draft_cols = {r[1] for r in conn.execute("PRAGMA table_info(naver_drafts)")}
    check({"post_id", "seo_title", "keywords", "body_html", "body_text", "telegram_cta", "web_cta",
           "image_path"} <= draft_cols, "naver_drafts 필수 컬럼")
    user_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    required_user = {"email", "name", "provider", "watchlist", "phone",
                     "email_verified", "phone_verified", "marketing_agreed", "marketing_agreed_at"}
    check(required_user <= user_cols, "users 필수 컴럼 (연락처·인증·마케팅 동의)",
          ", ".join(sorted(required_user - user_cols)) or "모두 존재")
    otp_cols = {r[1] for r in conn.execute("PRAGMA table_info(auth_otp)")}
    check({"channel", "target", "code_hash", "sent_at", "expires_at", "attempts", "consumed_at"} <= otp_cols,
          "auth_otp 컴럼 (코드 해시·만료·시도 횟수)")
    email_info = [r for r in conn.execute("PRAGMA table_info(users)") if r[1] == "email"]
    check(bool(email_info) and int(email_info[0][3]) == 0,
          "users.email: 휴대폰 단독 가입용 NULL 허용 (UNIQUE 유지)",
          f"notnull={email_info[0][3] if email_info else '?'}")
    phone_cols = {r[1] for r in conn.execute("PRAGMA table_info(phone_auth_state)")}
    check({"phone", "verified_at", "updated_at"} <= phone_cols,
          "phone_auth_state 컴럼 (번호 기준 인증 상태)")
    publish_cols = {r[1] for r in conn.execute("PRAGMA table_info(naver_publish)")}
    check({"draft_id", "post_id", "status", "url", "published_at"} <= publish_cols,
          "naver_publish 컴럼 (발행 이력)")
    alert_cols = {r[1] for r in conn.execute("PRAGMA table_info(alert_rules)")}
    required_alert = {"id", "user_id", "phone", "metric", "operator", "threshold", "label",
                      "cooldown_minutes", "last_triggered_at", "active", "created_at"}
    check(required_alert <= alert_cols, "alert_rules 필수 컴럼 (지표·연산자·임계치·쿨다운·활성)",
          ", ".join(sorted(required_alert - alert_cols)) or "모두 존재")
    alert_log_cols = {r[1] for r in conn.execute("PRAGMA table_info(alert_logs)")}
    required_alert_log = {"id", "rule_id", "phone", "metric", "observed_value", "message",
                          "status", "created_at"}
    check(required_alert_log <= alert_log_cols, "alert_logs 필수 컴럼 (관측값·메시지·발송 상태)",
          ", ".join(sorted(required_alert_log - alert_log_cols)) or "모두 존재")

    posts = db.list_posts(conn, include_hidden=True)
    check(len(posts) > 0, "수집된 글 존재", f"{len(posts)}건")
    hashes = set()
    bad = []
    for p in posts:
        h = hashlib.sha256(p["source_url"].encode("utf-8")).hexdigest()
        if h != p["source_hash"] or p["source_hash"] in hashes or not p["source_url"].startswith("http"):
            bad.append(p["id"])
        hashes.add(p["source_hash"])
        if len(p["summary_lines"]) != 3 or not p["tags"] or not p["market_impact"]:
            bad.append(p["id"])
        if "+09:00" not in p["verified_at"]:
            bad.append(p["id"])
    check(not bad, "source_hash = sha256(source_url) · 3줄 요약 · 태그 · KST 확인시각", f"이상 {sorted(set(bad))}" if bad else "전체 정상")

    banned = ("매수하세요", "매도하세요", "수익 보장", "반드시 오릅니다", "목표가")
    dirty = [p["id"] for p in posts if any(b in p["market_impact"] for b in banned)]
    check(not dirty, "컴플라이언스 금지 표현 없음", f"위반 {dirty}" if dirty else "매수·매도 권유 표현 없음")

    # ---- 중요도(S/A/B) 분류 · 저장 경로
    check("importance" in post_cols, "posts.importance 컴럼 (S/A/B)")
    levels = {str(p.get("importance") or "") for p in posts}
    check(bool(levels) and levels <= set(config.IMPORTANCE_LEVELS),
          "모든 글에 유효한 중요도(S/A/B)", str(sorted(levels)))

    import content  # noqa: E402  (수집 글 생성과 같은 경로로 검사)

    fixture = content.fed_post(
        {"title": "Federal Reserve issues FOMC statement",
         "link": f"https://www.federalreserve.gov/newsevents/pressreleases/verify-fomc-{int(time.time())}.htm"},
        config.iso_kst(),
    )
    check(fixture.get("importance") in config.IMPORTANCE_LEVELS,
          "수집 글 생성에 중요도 포함 (content.fed_post)", str(fixture.get("importance")))
    fixture["created_at"] = config.iso_kst()
    fixture["is_member_early"] = False
    post_id, created = db.insert_post(conn, fixture)
    stored_level = None
    if post_id:
        stored_level = conn.execute("SELECT importance FROM posts WHERE id = ?", (post_id,)).fetchone()[0]
        conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        conn.commit()
    check(created and stored_level == fixture["importance"],
          "posts.importance 저장·재조회", f"#{post_id} → {stored_level}")

    # ---- 공식 일정(calendar.json) 정규화
    check(config.CALENDAR_PATH == config.BASE_DIR / "calendar.json" and config.CALENDAR_PATH.exists(),
          "공식 일정은 저장소 루트 calendar.json (버전 관리)", str(config.CALENDAR_PATH))
    calendar = config.load_calendar()
    cal_events = calendar.get("events") or []
    check(len(cal_events) >= 5, "공식 일정 로드", f"{len(cal_events)}건")
    bad_cal = [e.get("id") for e in cal_events
               if e.get("importance") not in config.IMPORTANCE_LEVELS
               or not e.get("importance_label") or not e.get("org")
               or not str(e.get("source_url") or "").startswith("http")]
    check(not bad_cal, "일정 정규화: 중요도·라벨·기관·공식 원문 링크",
          str(bad_cal[:5]) if bad_cal else f"{len(cal_events)}건 정상")
    unmapped = [e.get("id") for e in cal_events
                if e.get("watch") and config.EVENT_WATCH_ALIASES.get(e["watch"]) not in config.WATCHLIST_ASSETS]
    check(not unmapped, "일정 watch 값이 관심자산으로 모두 매핑", str(unmapped) if unmapped else "모두 매핑됨")

    img_bad = []
    for p in posts:
        path = config.PUBLIC_DIR / (p["image_path"] or "").lstrip("/")
        if not p["image_path"] or not path.exists():
            img_bad.append((p["id"], "missing"))
            continue
        from PIL import Image

        try:
            with Image.open(path) as im:
                if im.format != "PNG" or im.size != (1200, 630):
                    img_bad.append((p["id"], f"{im.format} {im.size}"))
        except Exception as exc:  # noqa: BLE001
            img_bad.append((p["id"], str(exc)))
    check(not img_bad, "글마다 1200x630 PNG 썸네일", f"{img_bad}" if img_bad else f"{len(posts)}개 확인")

    drafts = db.list_drafts(conn)
    check(len(drafts) == len(posts), "글 1건당 초안 1건", f"글 {len(posts)} · 초안 {len(drafts)}")
    issues = []
    for d in drafts:
        kw = d["keywords"]
        if not (5 <= len(kw) <= 8):
            issues.append((d["id"], "keywords"))
        if config.TELEGRAM_URL not in d["body_html"] or "claredex.com" not in d["body_html"]:
            issues.append((d["id"], "cta-link"))
        if config.DISCLAIMER not in d["body_html"]:
            issues.append((d["id"], "disclaimer"))
        if any(bad in d["body_html"] or bad in d["body_text"] for bad in COMPLIANCE_BANNED):
            issues.append((d["id"], "compliance-banned-phrase"))
        if "<img" not in d["body_html"] or "t.me/Clare_PerpDEX" not in d["body_html"]:
            issues.append((d["id"], "image"))
        if len(d["seo_title"]) > 50:
            issues.append((d["id"], "seo_title_length"))
    check(not issues, "초안: 키워드 5~8 · 텔레그램/웹 링크 · 고지 · 금지 문구 없음 · 이미지 링크 · 제목 50자",
          f"{issues}" if issues else "전체 정상")

    # CTA 유사도 (자기 앞 10건과 비교)
    import difflib

    worst = 0.0
    worst_pair = None
    norm = lambda s: "".join(s.split())
    for i, d in enumerate(drafts):
        blob = norm(d["telegram_cta"] + " " + d["web_cta"])
        for prev in drafts[max(0, i - 10):i]:
            ratio = difflib.SequenceMatcher(None, blob, norm(prev["telegram_cta"] + " " + prev["web_cta"])).ratio()
            if ratio > worst:
                worst, worst_pair = ratio, (d["id"], prev["id"])
    check(worst < 0.85, "CTA 문구 최근 10건 대비 반복 없음", f"최대 유사도 {worst:.2f} {worst_pair or ''}")
    conn.close()


def server_checks(port: int) -> None:
    print("\n── 2. HTTP 서버 동작 ─────────────────────────────────────")
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--port", str(port), "--host", "127.0.0.1"],
        cwd=str(config.BASE_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        ready = False
        for _ in range(60):
            if proc.poll() is not None:
                break
            try:
                status, _, raw = http(base + "/api/health", timeout=2)
                if status == 200:
                    ready = True
                    break
            except Exception:
                time.sleep(0.25)
        if not check(ready, "서버 기동 (/api/health)"):
            out = ""
            if proc.stdout:
                proc.stdout.close()
            return

        status, headers, raw = http(base + "/")
        html = raw.decode("utf-8", "replace")
        check(status == 200 and "text/html" in headers.get("Content-Type", ""), "GET / → 200 text/html", f"HTTP {status}")
        check("공식 원문으로 읽는 오늘의 시장" in html and "무료 가입" in html,
              "메인 히어로·가입 CTA 문구 포함")
        check("최신 속보" in html and "공식 원문 ↗" in html, "타임라인(최신 속보) 섹션 마크업 포함")
        check('class="featured"' in html and 'id="featured"' in html and "오늘의 한 장" in html,
              "메인 '오늘의 한 장' 카드")
        check('id="featuredCta"' in html and "function watchKeyForPost" in html
              and "signup.html?from=featured&watch=" in html,
              "오늘의 한 장 CTA → signup.html?watch= 관심자산 연결")
        watch_keys = re.findall(r'\{ key: "([^"]+)", tags:', html)
        check(watch_keys == ["BTC", "NVDA", "AAPL", "연준·금리"]
              and all(k in config.WATCHLIST_ASSETS for k in watch_keys),
              "관심자산 칩 = BTC·NVDA·AAPL·연준·금리 (config.WATCHLIST_ASSETS 포함)", str(watch_keys))
        css = (config.PUBLIC_DIR / "assets" / "style.css").read_text(encoding="utf-8")
        check("--paper: #faf8f4" in css or "--paper:#faf8f4" in css,
              "TUPL 톤 오프화이트 배경 (#FAF8F4)")
        check(".auth-card" in css and "max-width: 540px" in css,
              "가입 화면 540px 단일 컬럼 카드")
        check(".feed-item.is-s" in css and ".badge-s" in css,
              "S등급 강조 스타일 (is-s · badge-s)")
        ticker_pos = html.find('class="ticker"')
        hero_pos = html.find('class="hero"')
        featured_pos = html.find('id="featured"')
        latest_pos = html.find('id="breaking"')
        check(0 < ticker_pos < hero_pos < featured_pos < latest_pos,
              "첫 화면 순서: 시장지표 → 히어로 → 오늘의 한 장 → 최신 속보",
              f"ticker={ticker_pos} hero={hero_pos} featured={featured_pos} latest={latest_pos}")
        check("S 긴급" in html and 'imp(p) === "S"' in html,
              "긴급(S) 속보 우선 노출 (오늘의 한 장 선택 기준)")
        check("내 종목 야간 S등급 비상벨 켜기" in html
              and "고른 자산 S등급 급변 시 문자로 바로 받기" in html
              and "광고 전화 0건" not in html
              and "30초 만에 무료 비상벨 켜기" in html,
              "메인 CTA 카피: 야간 비상벨 톤 (Featured·전환바·Free 카드)")

        status, headers, raw = http(base + "/signup.html")
        signup_html = raw.decode("utf-8", "replace")
        check(status == 200 and "비상벨 받을 번호" in signup_html
              and "id=\"smsCode\"" in signup_html and "인증번호 6자리" in signup_html,
              "GET /signup.html → 200 · 비상벨 받을 번호 + 인증번호 6자리 한 단계", f"HTTP {status}")
        check("data-social" not in signup_html and "준비 중" not in signup_html
              and "카카오로 시작하기" not in signup_html and "Google로 시작하기" not in signup_html,
              "가입 화면: 카카오·구글 (준비 중) 버튼 제거")
        check("성함 또는 닉네임" in signup_html and 'id="name"' in signup_html
              and 'name="name"' in signup_html,
              "가입 화면: 성함/닉네임 입력란 노출")
        check("이메일로도 받기 (선택)" not in signup_html
              and "optional-email" not in signup_html and "emailToggle" not in signup_html
              and "sendEmailBtn" not in signup_html and "emailCode" not in signup_html
              and "/api/auth/send-email-otp" not in signup_html
              and "/api/auth/verify-email-otp" not in signup_html,
              "이메일: 접기 토글·이메일 인증번호 발송/확인 UI·로직 완전 제거")
        m_email = re.search(r'<input[^>]*id="email"[^>]*>', signup_html)
        m_name = re.search(r'<input[^>]*id="name"[^>]*>', signup_html)
        form_pos = signup_html.find('id="bellForm"')
        submit_pos = signup_html.find('id="signupBtn"')
        check(m_email is not None and m_name is not None and 0 < form_pos < submit_pos
              and form_pos < m_email.start() < submit_pos and form_pos < m_name.start() < submit_pos
              and "hidden" not in m_email.group(0) and "hidden" not in m_name.group(0)
              and "인증번호는 보내지 않습니다" in signup_html
              and "you@gmail.com" in m_email.group(0),
              "이메일·성함/닉네임 입력란이 가입 폼에 바로 노출 (접힘·숨김 없음 · 이메일 형식 안내)")
        check("EMAIL_RE" in signup_html and "이메일 형식이 올바르지 않습니다" in signup_html
              and "email: email || undefined" in signup_html
              and "name: name || undefined" in signup_html,
              "가입 화면: 이메일 실시간 형식 검증 + 이메일·성함을 /api/signup 으로 전송")
        check("가입 전 미리 보는 내 종목 3초 브리핑" in signup_html
              and "/api/posts?limit=60" in signup_html and "/api/calendar" in signup_html,
              "1단계: 관심자산 선택 → 최근 공시 · S등급 일정 즉시 미리보기 연동")
        check("잠든 사이 내 종목 S등급 기습 공시" in signup_html and "3초 결론" in signup_html
              and "리딩방" not in signup_html and "광고 전화 0건" not in signup_html
              and "trust-badges" not in signup_html,
              "2단계: 야간 기습 공시 비상벨 카피 (스팸 보증 배지 제거)")

        # --- 2단계 카피: 방어적 변명 제거 → '받는 곳' 중심
        check("<label for=\"phone\">비상벨 받을 번호</label>" in signup_html
              and 'data-default-label="번호 확인"' in signup_html
              and ">번호 확인</button>" in signup_html
              and "내가 고른 조건으로 비상벨 켜기" in signup_html
              and "휴대폰 인증을 마치면 비상벨이 켜집니다" not in signup_html
              and "광고 전화나 제3자 판매에는 쓰지 않습니다" not in signup_html
              and "앱 설치 없이 문자로만 받으며, 원할 때 언제든 바로 끌 수 있습니다." in signup_html,
              "2단계 카피: 비상벨 받을 번호 · 번호 확인 · 내가 고른 조건으로 비상벨 켜기 (방어적 문구 제거)")
        check("커뮤니티·뉴스가 돌기 전, 내가 정한 기준선만 조용히 짚어드립니다." in signup_html
              and "잠든 사이 내 종목" in signup_html,
              "STEP 2 서브카피: 잠든 사이 · 커뮤니티·뉴스가 돌기 전 기준선")

        # --- 내 폰에 도착할 문자 미리보기 (관심자산 + 타점 조건 실시간 반영)
        form_at = signup_html.find('id="bellForm"')
        preview_at = signup_html.find('id="smsPreview"')
        phone_at = signup_html.find('id="phone"')
        check(0 < form_at < preview_at < phone_at,
              "문자 미리보기 카드가 번호 입력란 바로 위 (가입 폼 안)",
              f"form={form_at} preview={preview_at} phone={phone_at}")
        check("📩 내 폰에 도착할 비상벨 예시" in signup_html and "방금 전" in signup_html
              and "[클레어덱스]" in signup_html
              and "앱 설치도, 텔레그램 무음에 묻힐 걱정도 없이" in signup_html,
              "문자 미리보기: 제목 · 최근 시각 · [클레어덱스] 본문 · 부연 한 줄")
        check("function renderSmsPreview" in signup_html and "function smsAssetLabel" in signup_html
              and signup_html.count("renderSmsPreview()") >= 3
              and "SMS_VALUE_PLACEHOLDER" in signup_html
              and "alertRule.metric" in signup_html and "Array.from(selected)" in signup_html,
              "문자 미리보기: 관심자산·타점 조건을 바꾸면 바로 갱신되는 JS",
              f"renderSmsPreview 호출 {signup_html.count('renderSmsPreview()')}곳")
        check("smsLine1" in signup_html and "smsLine2" in signup_html
              and "도착 보장" not in signup_html and "몇 분 전 도착" not in signup_html,
              "문자 미리보기: 도착 보장·과장 수치 없이 예시로 표기")
        check(".sms-preview" in css and ".sms-bubble" in css
              and "border-radius: 14px 14px 14px 4px" in css,
              "문자 미리보기: 문자 말풍선 카드 스타일 (공용 CSS)")
        check("12월 S등급 일정" in signup_html and "고용보고서" in signup_html
              and "FOMC 회의 결과 발표" in signup_html and "소비자물가지수(CPI)" in signup_html,
              "가입 완료 화면: 내 관심자산 + 12월 S등급 일정(고용보고서·FOMC·CPI) 카드")
        endpoints = ("/api/auth/send-sms-otp", "/api/auth/verify-sms-otp", "/api/signup")
        missing = [e for e in endpoints if e not in signup_html]
        check(not missing and "watchlist: Array.from(selected)" in signup_html
              and "name: name || undefined" in signup_html
              and "email: email || undefined" in signup_html,
              "가입 화면이 실제 휴대폰 인증·가입 API 와 연동 (관심자산·이메일·성함 동시 전송)",
              ", ".join(missing) or "3개 엔드포인트 + watchlist/email/name")
        signup_assets = re.findall(r'\{ key: "([^"]+)", sub:', signup_html)
        check(len(signup_assets) == len(set(signup_assets))
              and set(signup_assets) == set(config.WATCHLIST_ASSETS),
              "가입 화면 관심자산 목록 = config.WATCHLIST_ASSETS", str(signup_assets))
        m = re.search(r"const EVENT_ALIAS = \{(.*?)\n  \};", signup_html, re.S)
        alias_block = m.group(1) if m else ""
        alias_map = dict(re.findall(r'"?([^"\s:,]+)"?\s*:\s*\{ asset: "([^"]+)"', alias_block))
        check(alias_map == config.EVENT_WATCH_ALIASES,
              "가입 화면 일정→관심자산 매핑 = config.EVENT_WATCH_ALIASES",
              str(alias_map) if alias_map != config.EVENT_WATCH_ALIASES else "6개 일정 매핑 일치")
        check("REQUEST.asset" in signup_html and "selected.add(REQUEST.asset)" in signup_html
              and "requestNote" in signup_html,
              "signup.html?watch= 관심자산 자동 체크·안내")
        check('class="auth-grid"' not in signup_html and "auth-shell" in signup_html,
              "가입 화면 단일 컬럼 (auth-grid 2단 제거)")
        consent_pos = signup_html.find("마케팅 활용 동의")
        check(consent_pos > 0 and "[보기]" in signup_html
              and "광고·마케팅 활용 및 제3자 제공 동의" in signup_html,
              "마케팅 활용 동의 + [보기] 모달")
        check("제3자 제공" in signup_html and "수집·이용 항목" in signup_html
              and "동의 거부 권리" in signup_html and "보유 및 이용 기간" in signup_html,
              "동의 모달: 수집·이용·제3자 제공·보유기간·거부 권리 포함")
        status, _, raw = http(base + "/signup")
        check(status == 200 and "야간 S등급 비상벨" in raw.decode("utf-8", "replace"),
              "GET /signup (기존 404 경로) → 200 · 새 가입 퍼널 화면")

        status, headers, raw = http(base + "/admin")
        admin_html = raw.decode("utf-8", "replace")
        check(status == 200 and "운영자 로그인" in admin_html and "운영자 비밀번호" in admin_html,
              "GET /admin → 200 (관리자 콘솔 로그인 게이트)", f"HTTP {status}")
        check(headers.get("Cache-Control") == "no-store"
              and headers.get("X-Robots-Tag") == "noindex, nofollow",
              "관리자 화면 Cache-Control: no-store · X-Robots-Tag: noindex, nofollow")
        check('name="robots" content="noindex, nofollow"' in admin_html,
              "관리자 화면 meta robots noindex, nofollow")
        status, _, raw = http(base + "/admin/")
        check(status == 200 and "운영자 비밀번호" in raw.decode("utf-8", "replace"),
              "GET /admin/ → 200", f"HTTP {status}")
        for asset, needle in (("/admin/admin.css", ".kpi-grid"), ("/admin/app.js", "admin/overview")):
            status, headers, raw = http(base + asset)
            check(status == 200 and needle in raw.decode("utf-8", "replace"),
                  f"GET {asset} → 200 (콘솔 자산)", f"HTTP {status}")
        check("정적 프리뷰" in admin_html and "noindex" in admin_html,
              "관리자 로그인 화면: 정적 프리뷰 안내 · noindex")
        for legacy in ("/admin-blog.html", "/admin-blog"):
            status, _, _ = http(base + legacy)
            check(status == 404, f"GET {legacy} → 404 (공개 노출 제거)", f"HTTP {status}")

        status, headers, raw = http(base + "/assets/style.css")
        check(status == 200 and "text/css" in headers.get("Content-Type", ""), "GET /assets/style.css → 200")

        status, headers, raw = http(base + "/images/posts/1.png")
        check(status == 200 and headers.get("Content-Type") == "image/png", "GET /images/posts/1.png → 200 image/png")

        status, headers, raw = http(base + "/api/ticker")
        ticker = json.loads(raw)
        keys = [i["key"] for i in ticker.get("items", [])]
        check(status == 200 and "application/json" in headers.get("Content-Type", ""), "GET /api/ticker → 200 JSON")
        check(len(keys) >= 4 and "btc_krw" in keys and "kimchi_premium" in keys,
              "티커: BTC KRW·김프·공포탐욕·롱숏", ", ".join(keys))
        check(all(i.get("source") for i in ticker.get("items", [])), "티커: 항목별 출처 표기")

        status, _, raw = http(base + "/api/posts")
        posts = json.loads(raw)
        check(status == 200 and posts.get("count", 0) > 0, "GET /api/posts → 200 + 글 목록", f"{posts.get('count')}건")
        first = (posts.get("posts") or [{}])[0]
        check(all(k in first for k in ("title", "summary_lines", "market_impact", "tags", "source_url", "verified_at")),
              "글 응답 필드 완전성")
        levels = [p.get("importance") for p in posts.get("posts") or []]
        check(all(lv in config.IMPORTANCE_LEVELS for lv in levels)
              and all(p.get("importance_label") for p in posts.get("posts") or []),
              "모든 글에 importance(S/A/B) + 라벨", str(sorted(set(levels)) or "없음"))

        status, _, raw = http(base + "/api/posts?category=macro")
        macro = json.loads(raw)
        check(status == 200 and macro["posts"] and all(p["category"] == "macro" for p in macro["posts"]),
              "GET /api/posts?category=macro 필터")

        status, _, raw = http(base + "/api/posts?importance=S")
        s_only = json.loads(raw)
        check(status == 200 and s_only.get("count", 0) >= 1
              and all(p.get("importance") == "S" for p in s_only.get("posts") or []),
              "GET /api/posts?importance=S 필터", f"{s_only.get('count')}건")

        # 검색어는 실제 데이터에 있는 태그로 검증한다(시드 데이터에 따라 달라지지 않게).
        needle = (first.get("tags") or ["연준"])[0]
        status, _, raw = http(base + "/api/posts?q=" + urllib.parse.quote(str(needle)))
        q = json.loads(raw)
        check(status == 200 and q.get("count", 0) >= 1, f"GET /api/posts?q={needle} 검색", f"{q.get('count')}건")
        status, _, raw = http(base + "/api/posts?q=" + urllib.parse.quote("zzz-no-such-term"))
        none = json.loads(raw)
        check(status == 200 and none.get("count") == 0, "GET /api/posts?q=없는말 → 0건", f"{none.get('count')}건")

        status, _, raw = http(base + "/api/stats")
        stats = json.loads(raw)
        check(status == 200 and isinstance(stats.get("today"), dict) and isinstance(stats.get("recent"), dict)
              and isinstance(stats.get("today", {}).get("count"), int)
              and isinstance(stats.get("recent", {}).get("count"), int)
              and stats.get("total") == posts.get("count"),
              "GET /api/stats → 오늘·최근 확인 건수",
              f"오늘 {stats.get('today', {}).get('count')}건 · 전체 {stats.get('total')}건")

        status, _, raw = http(base + "/api/admin/session")
        sess = json.loads(raw)
        check(status == 200 and sess.get("authenticated") is False,
              "미로그인 GET /api/admin/session → authenticated:false", f"HTTP {status}")

        # --- 운영자 API 전체 미로그인 401
        for path in ("/api/admin/overview", "/api/admin/users", "/api/admin/posts",
                     "/api/admin/collector", "/api/admin/security", "/api/naver-drafts"):
            status, _, raw = http(base + path)
            check(status == 401, f"미로그인 GET {path} → 401", f"HTTP {status}")
        for path, body in (
            ("/api/admin/collect", {}),
            ("/api/admin/posts/status", {"id": 1, "status": "hidden"}),
            ("/api/admin/posts/delete", {"id": 1}),
            ("/api/admin/change-password", {"current_password": "x", "new_password": "y", "confirm_password": "y"}),
        ):
            status, _, _ = http(base + path, "POST", body)
            check(status == 401, f"미로그인 POST {path} → 401", f"HTTP {status}")

        status, _, raw = http(base + "/api/naver-drafts")
        drafts_denied = json.loads(raw)
        check(status == 401 and "로그인" in drafts_denied.get("error", ""),
              "미로그인 GET /api/naver-drafts → 401", f"HTTP {status}")

        status, _, raw = http(base + "/api/admin/login", "POST", {"password": "wrong-password-for-verify"})
        check(status == 401 and json.loads(raw).get("ok") is False,
              "틀린 비밀번호 POST /api/admin/login → 401", f"HTTP {status}")

        status, headers, raw = http(base + "/api/admin/login", "POST", {"password": VERIFY_ADMIN_PASSWORD})
        login = json.loads(raw)
        set_cookie = headers.get("Set-Cookie", "")
        check(status == 200 and login.get("authenticated") is True,
              "POST /api/admin/login → 200 + 세션 발급", f"HTTP {status}")
        check("HttpOnly" in set_cookie and "SameSite=Lax" in set_cookie,
              "세션 쿠키 HttpOnly · SameSite=Lax", set_cookie.split(";", 1)[0])
        admin_cookie = set_cookie.split(";", 1)[0]
        check(admin_cookie.startswith(server.ADMIN_COOKIE + "=") and len(admin_cookie) > 30,
              f"세션 쿠키 이름: {server.ADMIN_COOKIE}")

        status, _, raw = http(base + "/api/admin/session", cookie=admin_cookie)
        params = json.loads(raw)
        check(status == 200 and params.get("authenticated") is True
              and params.get("password_source") in ("env", "file", "default"),
              "로그인 후 GET /api/admin/session → authenticated:true · 비밀번호 저장 방식",
              str(params.get("password_source")))

        # --- 대시보드 개요
        status, _, raw = http(base + "/api/admin/overview", cookie=admin_cookie)
        ov = json.loads(raw)
        kpis = ov.get("kpis") or {}
        check(status == 200 and all(k in kpis for k in (
            "users_total", "users_today", "users_7d", "posts_total", "posts_published",
            "posts_hidden", "drafts_total", "collector")),
              "GET /api/admin/overview → KPI 6종 + 부가 지표", f"HTTP {status}")
        trend = ov.get("trend") or []
        check(len(trend) == 14 and all("date" in t and "count" in t for t in trend),
              "가입 추이 14일 시계열 (빈 날 0 포함)", f"{len(trend)}일")
        check(isinstance(ov.get("categories"), list) and isinstance(ov.get("sources"), list),
              "카테고리·출처 분포 포함")
        check(kpis.get("posts_total", 0) >= 1 and kpis.get("drafts_total", 0) >= 1,
              "KPI 값 유효 (글·초안)", f"글 {kpis.get('posts_total')} · 초안 {kpis.get('drafts_total')}")
        check(isinstance(ov.get("recent_users"), list) and isinstance(ov.get("recent_posts"), list),
              "최근 가입·최근 글 목록 포함")

        # --- 회원 관리
        with db.session() as conn:
            expected_drafts = db.count_drafts(conn)
            local_users = db.count_users(conn)
        status, _, raw = http(base + "/api/admin/users?limit=10", cookie=admin_cookie)
        user_list = json.loads(raw)
        check(status == 200 and user_list.get("total") == local_users
              and isinstance(user_list.get("items"), list),
              "GET /api/admin/users → 회원 목록·전체 건수", f"{user_list.get('total')}명")

        # --- 콘텐츠 관리: 상태 전환 → 공개 API 제외
        status, _, raw = http(base + "/api/admin/posts?status=all&limit=50", cookie=admin_cookie)
        post_list = json.loads(raw)
        check(status == 200 and post_list.get("total", 0) >= 1
              and all("status" in p for p in post_list.get("items", [])),
              "GET /api/admin/posts → 상태 포함 목록", f"{post_list.get('total')}건")
        target = (post_list.get("items") or [{}])[0]
        status, _, raw = http(base + "/api/admin/posts/status", "POST",
                              {"id": target.get("id"), "status": "hidden"}, cookie=admin_cookie)
        hidden = json.loads(raw)
        check(status == 200 and (hidden.get("post") or {}).get("status") == "hidden",
              "POST /api/admin/posts/status → 숨김 전환", f"#{target.get('id')}")
        status, _, raw = http(base + "/api/posts")
        public_posts = json.loads(raw)
        check(all(p["id"] != target.get("id") for p in public_posts.get("posts", []))
              and public_posts.get("count") == post_list.get("total", 0) - 1,
              "숨김 글은 공개 /api/posts 에서 제외", f"{public_posts.get('count')}건")
        status, _, raw = http(base + "/api/admin/posts/status", "POST",
                              {"id": target.get("id"), "status": "published"}, cookie=admin_cookie)
        check(status == 200 and (json.loads(raw).get("post") or {}).get("status") == "published",
              "POST /api/admin/posts/status → 다시 공개")

        # --- 콘텐츠 관리: 임시 글 삭제
        dummy_url = f"https://example.com/verify/{int(time.time())}"
        with db.session() as conn:
            dummy_id, created = db.insert_post(conn, {
                "title": "검증용 임시 글", "category": "macro", "source_name": "verify",
                "source_grade": "primary", "source_url": dummy_url,
                "source_hash": hashlib.sha256(dummy_url.encode("utf-8")).hexdigest(),
                "verified_at": config.iso_kst(), "summary_lines": ["a", "b", "c"],
                "market_impact": "검증용", "tags": ["검증"], "image_path": "/images/posts/1.png",
                "is_member_early": False, "created_at": config.iso_kst(),
                "published_at": config.iso_kst(), "topic": "verify",
            })
        check(created and dummy_id, "검증용 임시 글 생성")
        status, _, raw = http(base + "/api/admin/posts/delete", "POST", {"id": dummy_id}, cookie=admin_cookie)
        check(status == 200 and json.loads(raw).get("deleted") == dummy_id,
              "POST /api/admin/posts/delete → 글 삭제")
        with db.session() as conn:
            check(db.get_post(conn, dummy_id) is None, "삭제 후 DB에서 사라짐")

        # --- 수집기 상태 · 보안 정보
        status, _, raw = http(base + "/api/admin/collector", cookie=admin_cookie)
        cstate = json.loads(raw)
        check(status == 200 and "running" in cstate and "last" in cstate,
              "GET /api/admin/collector → 수집기 상태")
        status, _, raw = http(base + "/api/admin/security", cookie=admin_cookie)
        sec = json.loads(raw)
        check(status == 200 and (sec.get("auth") or {}).get("algorithm") == "pbkdf2_sha256"
              and "policy" in sec and isinstance(sec.get("audit"), list),
              "GET /api/admin/security → 해시 방식·정책·감사 로그")

        # --- 네이버 초안 (로그인 상태)
        status, _, raw = http(base + "/api/naver-drafts", cookie=admin_cookie)
        drafts = json.loads(raw)
        check(status == 200 and drafts.get("count") == expected_drafts,
              "로그인 후 GET /api/naver-drafts → 200 · 전체 초안",
              f"HTTP {status} · {drafts.get('count')}/{expected_drafts}건")
        d0 = (drafts.get("drafts") or [{}])[0]
        check("t.me/Clare_PerpDEX" in d0.get("body_html", "") and d0.get("seo_title"),
              "초안 응답: 본문·이미지 링크 포함")

        # --- 비밀번호 변경: 검증 → 해시 파일 저장 → 이전/새 비밀번호 확인
        status, _, raw = http(base + "/api/admin/change-password", "POST",
                              {"current_password": "wrong-current", "new_password": VERIFY_NEW_PASSWORD,
                               "confirm_password": VERIFY_NEW_PASSWORD}, cookie=admin_cookie)
        check(status == 401, "비밀번호 변경: 현재 비밀번호 오류 → 401", f"HTTP {status}")
        status, _, raw = http(base + "/api/admin/change-password", "POST",
                              {"current_password": VERIFY_ADMIN_PASSWORD, "new_password": "short",
                               "confirm_password": "short"}, cookie=admin_cookie)
        check(status == 400, "비밀번호 변경: 10자 미만 → 400", f"HTTP {status}")
        status, _, raw = http(base + "/api/admin/change-password", "POST",
                              {"current_password": VERIFY_ADMIN_PASSWORD, "new_password": VERIFY_NEW_PASSWORD,
                               "confirm_password": "different-password"}, cookie=admin_cookie)
        check(status == 400, "비밀번호 변경: 확인 불일치 → 400", f"HTTP {status}")
        status, _, raw = http(base + "/api/admin/change-password", "POST",
                              {"current_password": VERIFY_ADMIN_PASSWORD, "new_password": VERIFY_NEW_PASSWORD,
                               "confirm_password": VERIFY_NEW_PASSWORD}, cookie=admin_cookie)
        changed = json.loads(raw)
        check(status == 200 and changed.get("ok") and changed.get("storage") == "file",
              "POST /api/admin/change-password → 200 · 해시 파일 저장", f"HTTP {status}")
        auth_path = Path(os.environ["CLAREDEX_ADMIN_AUTH_FILE"])
        auth_text = auth_path.read_text(encoding="utf-8") if auth_path.exists() else ""
        check(auth_path.exists() and "pbkdf2_sha256" in auth_text
              and VERIFY_NEW_PASSWORD not in auth_text and "salt" in auth_text,
              "admin_auth.json: 평문 없이 PBKDF2 해시 + salt 저장")
        check((auth_path.stat().st_mode & 0o777) == 0o600, "admin_auth.json 권한 0600")
        status, _, _ = http(base + "/api/admin/login", "POST", {"password": VERIFY_ADMIN_PASSWORD})
        check(status == 401, "변경 후 이전 비밀번호 로그인 → 401", f"HTTP {status}")
        status, headers, raw = http(base + "/api/admin/login", "POST", {"password": VERIFY_NEW_PASSWORD})
        check(status == 200, "변경 후 새 비밀번호 로그인 → 200", f"HTTP {status}")
        new_cookie = headers.get("Set-Cookie", "").split(";", 1)[0]
        check(server.verify_admin_password(VERIFY_NEW_PASSWORD)
              and not server.verify_admin_password(VERIFY_ADMIN_PASSWORD),
              "파일에서 다시 읽어도 새 비밀번호만 통과 (재시작 지속성)")
        status, _, _ = http(base + "/api/naver-drafts", cookie=admin_cookie)
        check(status == 200, "비밀번호 변경 후 현재 세션 유지", f"HTTP {status}")

        status, headers, raw = http(base + "/api/admin/logout", "POST", {}, cookie=new_cookie)
        check(status == 200 and "Max-Age=0" in headers.get("Set-Cookie", ""),
              "POST /api/admin/logout → 200 + 쿠키 만료")
        status, _, raw = http(base + "/api/naver-drafts", cookie=new_cookie)
        check(status == 401, "로그아웃 후 GET /api/naver-drafts → 401 (세션 폐기)", f"HTTP {status}")

        status, _, raw = http(base + "/api/calendar")
        cal = json.loads(raw)
        events = cal.get("events") or []
        check(status == 200 and len(events) >= 1, "GET /api/calendar → 200", f"{len(events)}건")
        bad_events = [e.get("id") for e in events
                      if e.get("importance") not in config.IMPORTANCE_LEVELS
                      or not e.get("importance_label")
                      or not str(e.get("source_url") or "").startswith("http")
                      or not e.get("datetime_kst")]
        check(not bad_events, "일정 API: 중요도·라벨·공식 원문 링크·발표 시각",
              str(bad_events[:5]) if bad_events else f"{len(events)}건 정상")
        unmapped = [e.get("id") for e in events
                    if e.get("watch") and config.EVENT_WATCH_ALIASES.get(e["watch"]) not in config.WATCHLIST_ASSETS]
        check(not unmapped, "일정 watch 값 → 관심자산 매핑", str(unmapped) if unmapped else "모두 매핑됨")
        times = [config.parse_kst(e["datetime_kst"]) for e in events if e.get("datetime_kst")]
        check(times == sorted(times), "일정 API: 발표 시각 오름차순")

        # ------------------------------------------------ 본인인증(이메일·휴대폰) 가입
        status, _, raw = http(base + "/api/auth/channels")
        ch = json.loads(raw)
        check(status == 200 and "email" in ch and "sms" in ch and isinstance(ch.get("sandbox"), bool),
              "GET /api/auth/channels → 발송 채널 상태",
              f"email={((ch.get('email') or {}).get('mode'))} sms={((ch.get('sms') or {}).get('mode'))}")

        # 실행마다 새 번호·이메일을 쓴다(이전 실행의 인증 기록과 섞이지 않게).
        stamp = int(time.time())
        email = f"verify-{stamp}@claredex.test"
        phone = f"010-9{stamp % 1000:03d}-{stamp % 10000:04d}"
        with db.session() as conn:
            conn.execute("DELETE FROM phone_auth_state WHERE phone = ?", (phone,))
            conn.execute("DELETE FROM signup_verification WHERE email = ?", (email,))
            conn.execute("DELETE FROM auth_otp WHERE target = ?", (phone,))
            conn.execute("DELETE FROM users WHERE phone = ? AND COALESCE(email, '') = ''", (phone,))
            conn.commit()

        status, _, raw = http(base + "/api/signup", "POST",
                              {"email": email, "name": "검증", "phone": phone})
        blocked = json.loads(raw)
        check(status == 403 and blocked.get("code") == "verification_required"
              and blocked.get("phone_verified") is False,
              "휴대폰 인증 없이 POST /api/signup → 403 (이메일은 인증 요구하지 않음)", f"HTTP {status}")

        status, _, raw = http(base + "/api/signup", "POST",
                              {"email": "not-an-email", "name": "검증", "phone": phone})
        check(status == 400 and "이메일" in json.loads(raw).get("error", ""),
              "잘못된 이메일 형식 → 400", f"HTTP {status}")

        status, _, raw = http(base + "/api/signup", "POST",
                              {"email": "verify@claredex", "name": "검증", "phone": phone})
        check(status == 400 and "이메일" in json.loads(raw).get("error", ""),
              "도메인 TLD 없는 이메일 → 400", f"HTTP {status}")

        status, _, raw = http(base + "/api/signup", "POST",
                              {"email": email, "name": "가" * 41, "phone": phone})
        check(status == 400 and json.loads(raw).get("code") == "name_too_long",
              "성함/닉네임 41자 이상 → 400", f"HTTP {status}")

        # 이메일 인증번호 API 는 다른 흐름용으로 남아 있다(가입에서는 요구하지 않는다).
        otp_email = f"verify-otp-{int(time.time())}@claredex.test"
        status, _, raw = http(base + "/api/auth/send-email-otp", "POST", {"email": otp_email})
        sent = json.loads(raw)
        check(status == 200 and sent.get("ok") and sent.get("delivery_mode") in ("unconfigured", "smtp", "resend"),
              "POST /api/auth/send-email-otp → 200 (API 유지)",
              f"mode={sent.get('delivery_mode')} sent={sent.get('sent')}")

        status, _, raw = http(base + "/api/auth/send-email-otp", "POST", {"email": otp_email})
        check(status == 429 and json.loads(raw).get("code") == "cooldown",
              "이메일 인증번호 재발송 30초 대기 → 429", f"HTTP {status}")

        otp_code = sent.get("preview_code")
        if otp_code:
            wrong = "000000" if otp_code != "000000" else "111111"
            status, _, raw = http(base + "/api/auth/verify-email-otp", "POST", {"email": otp_email, "code": wrong})
            check(status == 400 and json.loads(raw).get("code") == "mismatch",
                  "잘못된 이메일 인증번호 → 400", f"HTTP {status}")
            status, _, raw = http(base + "/api/auth/verify-email-otp", "POST", {"email": otp_email, "code": otp_code})
            ok_email = json.loads(raw)
            check(status == 200 and ok_email.get("ok"), "POST /api/auth/verify-email-otp → 200", f"HTTP {status}")
        else:
            check(True, "실제 SMTP 발송 모드 — 인증번호 검증은 코드 미노출로 건너뜀")

        # ① 이메일 인증 없이 이메일 + 성함/닉네임 + 휴대폰 OTP 만으로 가입 (201)
        status, _, raw = http(base + "/api/auth/send-sms-otp", "POST", {"phone": phone})
        sms = json.loads(raw)
        check(status == 200 and sms.get("ok") and sms.get("channel") == "sms",
              "이메일 인증 없이 POST /api/auth/send-sms-otp → 200", f"mode={sms.get('delivery_mode')}")
        sms_code = sms.get("preview_code")
        if sms_code:
            status, _, raw = http(base + "/api/auth/verify-sms-otp", "POST",
                                  {"phone": phone, "code": sms_code})
            ok_sms = json.loads(raw)
            check(status == 200 and ok_sms.get("ok") and ok_sms.get("verified"),
                  "POST /api/auth/verify-sms-otp → 200", f"HTTP {status}")
        else:
            check(True, "실제 문자 발송 모드 — 인증번호 검증은 코드 미노출로 건너뜀")

        status, _, raw = http(base + "/api/signup", "POST",
                              {"email": email, "name": "코인덕후", "phone": phone,
                               "provider": "email", "watchlist": ["BTC", "ETH"], "marketing_agreed": True})
        signup = json.loads(raw)
        check(status == 201 and signup.get("ok") and signup["user"]["email"] == email,
              "이메일 인증 없이 이메일+성함+휴대폰 OTP POST /api/signup → 201", f"HTTP {status}")
        user = signup.get("user") or {}
        check(user.get("name") == "코인덕후",
              "성함/닉네임이 users.name 에 저장", str(user.get("name")))
        check(user.get("phone") == phone and user.get("phone_verified") and not user.get("email_verified"),
              "회원 레코드: 휴대폰 인증 O · 이메일은 무인증(0)으로 저장",
              str((user.get("phone"), user.get("email_verified"), user.get("phone_verified"))))
        check(user.get("marketing_agreed") and user.get("marketing_agreed_at"),
              "마케팅 활용 동의 + 동의 시각 저장", str(user.get("marketing_agreed_at")))

        status, _, raw = http(base + "/api/admin/users?q=" + urllib.parse.quote(email), cookie=admin_cookie)
        admin_found = json.loads(raw)
        admin_items = admin_found.get("items") or []
        check(status == 200 and admin_found.get("total") == 1 and admin_items
              and admin_items[0].get("name") == "코인덕후"
              and admin_items[0].get("email") == email,
              "관리자 회원 목록(GET /api/admin/users)에 성함/닉네임 그대로 표시",
              f"HTTP {status} name={admin_items[0].get('name') if admin_items else None}")

        status, _, raw = http(base + "/api/watchlist", "POST",
                              {"email": email, "watchlist": ["BTC", "ETH", "나스닥", "NVDA", "AAPL", "연준·금리"]})
        wl = json.loads(raw)
        check(status == 200 and wl.get("ok") and len(wl["user"]["watchlist"]) == 6,
              "POST /api/watchlist → 200 저장 (AAPL·연준·금리 포함)", f"HTTP {status} · {wl.get('user', {}).get('watchlist')}")

        status, _, raw = http(base + "/api/signup", "POST",
                              {"email": email, "name": "코인덕후", "phone": phone, "provider": "email", "watchlist": []})
        again = json.loads(raw)
        check(status == 201 and len(again["user"]["watchlist"]) == 6,
              "같은 이메일 재가입 시 관심자산 보존 (이메일 재인증 불필요)", str(again["user"]["watchlist"]))

        # ------------------------------- 타점 비상벨: 가입과 동시에 조건 저장 (김프·공포탐욕·롱숏)
        status, _, raw = http(base + "/api/signup", "POST",
                              {"email": email, "name": "코인덕후", "phone": phone,
                               "alert_rule": {"metric": "kimp_pct", "operator": ">=", "threshold": 3.0,
                                              "label": "김프 +3.0% 이상"}})
        alert_signup = json.loads(raw)
        saved_alert = alert_signup.get("alert_rule") or {}
        check(status == 201 and saved_alert.get("metric") == "kimp_pct"
              and saved_alert.get("operator") == ">=" and float(saved_alert.get("threshold") or 0) == 3.0,
              "① POST /api/signup + alert_rule → 201 · 응답에 타점 조건",
              f"HTTP {status} · {saved_alert.get('label')}")
        with db.session() as conn:
            alert_row = conn.execute(
                "SELECT * FROM alert_rules WHERE phone = ? AND active = 1", (phone,)
            ).fetchone()
            stored_alert = dict(alert_row) if alert_row else None
        check(bool(stored_alert) and stored_alert["metric"] == "kimp_pct"
              and stored_alert["operator"] == ">=" and stored_alert["threshold"] == 3.0
              and stored_alert["cooldown_minutes"] == 360 and bool(stored_alert["user_id"]),
              "① alert_rules SQLite 저장 (지표·연산자·임계치·6시간 쿨다운·회원 연결)",
              str({k: stored_alert[k] for k in ("metric", "operator", "threshold", "cooldown_minutes")}
                  if stored_alert else "행 없음"))

        status, _, raw = http(base + "/api/signup", "POST",
                              {"phone": phone,
                               "alert_rule": {"metric": "doge_pct", "operator": ">=", "threshold": 1}})
        bad_metric = json.loads(raw)
        check(status == 400 and bad_metric.get("code") == "invalid_alert_rule",
              "지원하지 않는 지표 alert_rule → 400 invalid_alert_rule", f"HTTP {status}")

        status, _, raw = http(base + "/api/signup", "POST",
                              {"phone": phone,
                               "alert_rule": {"metric": "kimp_pct", "operator": ">=", "threshold": 999}})
        bad_range = json.loads(raw)
        check(status == 400 and bad_range.get("code") == "invalid_alert_rule",
              "범위를 벗어난 임계값 alert_rule → 400", f"HTTP {status}")

        status, headers, raw = http(base + "/api/market")
        market = json.loads(raw)
        check(status == 200 and "application/json" in headers.get("Content-Type", "")
              and set(market.get("metrics") or {}) == set(config.ALERT_METRICS)
              and isinstance(market.get("values"), dict),
              "GET /api/market → 200 · kimp_pct·fng·long_pct 지표", ", ".join(sorted(market.get("metrics") or {})))

        status, _, raw = http(base + "/api/alerts?phone=" + urllib.parse.quote(phone))
        alerts_get = json.loads(raw)
        check(status == 200 and (alerts_get.get("alert_rule") or {}).get("label") == "김프 +3.0% 이상"
              and alerts_get.get("member") is True,
              "GET /api/alerts?phone → 설정된 타점 조건 조회", f"HTTP {status}")

        status, _, raw = http(base + "/api/alerts", "POST",
                              {"phone": phone,
                               "alert_rule": {"metric": "fng", "operator": "<=", "threshold": 15}})
        alerts_post = json.loads(raw)
        with db.session() as conn:
            active_rules = db.list_alert_rules_by_phone(conn, phone)
        check(status == 200 and (alerts_post.get("alert_rule") or {}).get("metric") == "fng"
              and len(active_rules) == 1 and active_rules[0]["metric"] == "fng"
              and active_rules[0]["cooldown_minutes"] == 360,
              "POST /api/alerts → 기존 가입자 조건 변경 (활성 1개 정책)",
              f"HTTP {status} · 활성 {len(active_rules)}개")

        status, _, raw = http(base + "/api/admin/users?q=" + urllib.parse.quote(phone), cookie=admin_cookie)
        admin_alert = json.loads(raw)
        admin_alert_items = admin_alert.get("items") or []
        admin_rule_one = (admin_alert_items[0].get("alert_rule") if admin_alert_items else None) or {}
        check(status == 200 and admin_rule_one.get("metric") == "fng"
              and admin_rule_one.get("label"),
              "관리자 회원 목록에 타점 비상벨 요약 표시",
              f"HTTP {status} · {admin_rule_one.get('label')}")

        status, _, raw = http(base + "/api/alerts", "POST", {"phone": phone, "active": False})
        end_alerts = json.loads(raw)
        with db.session() as conn:
            off_rules = db.list_alert_rules_by_phone(conn, phone)
        check(status == 200 and end_alerts.get("alert_rule") is None and off_rules == [],
              "POST /api/alerts {active:false} → 타점 비상벨 끄기", f"HTTP {status}")

        # ② 이미 다른 회원이 쓰는 이메일과 충돌하면 덮어쓰지 않고 409 로 안내
        other_phone = "010-3333-4444"
        status, _, raw = http(base + "/api/auth/send-sms-otp", "POST", {"phone": other_phone})
        other_sms = json.loads(raw)
        other_code = other_sms.get("preview_code")
        if other_code:
            status, _, raw = http(base + "/api/auth/verify-sms-otp", "POST",
                                  {"phone": other_phone, "code": other_code})
            check(status == 200 and json.loads(raw).get("ok"),
                  "충돌 검증용 두 번째 번호 인증 → 200", f"HTTP {status}")
        else:
            check(True, "실제 문자 발송 모드 — 충돌 검증용 번호 인증은 코드 미노출로 건너뜀")

        status, _, raw = http(base + "/api/signup", "POST",
                              {"email": email, "name": "중복", "phone": other_phone})
        conflict = json.loads(raw)
        check(status == 409 and conflict.get("code") == "email_in_use" and "이메일" in conflict.get("error", ""),
              "다른 휴대폰 회원이 쓰는 이메일 → 409 email_in_use", f"HTTP {status}")

        status, _, raw = http(base + "/api/signup", "POST",
                              {"email": f"other-{int(time.time())}@claredex.test", "name": "중복", "phone": phone})
        conflict2 = json.loads(raw)
        check(status == 409 and conflict2.get("code") == "email_in_use",
              "이미 다른 이메일로 가입된 휴대폰 → 409 email_in_use", f"HTTP {status}")

        with db.session() as conn:
            intact = db.get_user_by_phone(conn, phone)
        check(bool(intact) and intact.get("email") == email and intact.get("name") == "코인덕후",
              "409 이후 기존 회원 이메일·성함을 덮어쓰지 않음", str((intact or {}).get("email")))

        with db.session() as conn:
            row = conn.execute(
                "SELECT name, watchlist, phone, email_verified, phone_verified, marketing_agreed "
                "FROM users WHERE email = ?", (email,)).fetchone()
            stored = json.loads(row[1]) if row else None
            stored_name = row[0] if row else None
            flags = tuple(row[2:]) if row else None
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("DELETE FROM signup_verification WHERE email IN (?, ?)", (email, otp_email))
            conn.execute("DELETE FROM auth_otp WHERE target IN (?, ?)", (phone, other_phone))
            conn.execute("DELETE FROM phone_auth_state WHERE phone IN (?, ?)", (phone, other_phone))
            conn.execute("DELETE FROM alert_rules WHERE phone IN (?, ?)", (phone, other_phone))
            conn.execute("DELETE FROM alert_logs WHERE phone IN (?, ?)", (phone, other_phone))
            conn.commit()
        check(stored == ["BTC", "ETH", "나스닥", "NVDA", "AAPL", "연준·금리"], "관심자산 SQLite 저장 확인", str(stored))
        check(stored_name == "코인덕후", "성함/닉네임 SQLite 저장 확인", str(stored_name))
        check(flags == (phone, 0, 1, 1), "SQLite: 이메일 무인증(0) · 휴대폰 인증(1) · 마케팅 동의(1)", str(flags))

        # ---------------------------------------- 1단계 휴대폰 가입 (이메일·이름 없이)
        solo_phone = "010-2222-3333"
        status, _, raw = http(base + "/api/auth/send-sms-otp", "POST", {"phone": solo_phone})
        solo_sms = json.loads(raw)
        check(status == 200 and solo_sms.get("ok") and solo_sms.get("channel") == "sms",
              "이메일 없이 POST /api/auth/send-sms-otp → 200", f"mode={solo_sms.get('delivery_mode')}")
        solo_code = solo_sms.get("preview_code")
        if solo_code:
            status, _, raw = http(base + "/api/auth/verify-sms-otp", "POST",
                                  {"phone": solo_phone, "code": solo_code})
            solo_ok = json.loads(raw)
            check(status == 200 and solo_ok.get("ok") and solo_ok.get("verified"),
                  "이메일 없이 POST /api/auth/verify-sms-otp → 200", f"HTTP {status}")
        else:
            check(True, "실제 문자 발송 모드 — 인증번호 검증은 코드 미노출로 건너뜀")

        solo_watch = ["BTC", "삼성전자", "SK하이닉스", "TSLA", "연준·금리"]
        status, _, raw = http(base + "/api/signup", "POST",
                              {"phone": solo_phone, "name": "휴대폰족", "watchlist": solo_watch})
        solo = json.loads(raw)
        check(status == 201 and solo.get("ok"),
              "이메일 없이(성함만) POST /api/signup → 201", f"HTTP {status}")
        solo_user = solo.get("user") or {}
        check(not solo_user.get("email") and solo_user.get("phone") == solo_phone
              and solo_user.get("phone_verified") and solo_user.get("provider") == "phone"
              and solo_user.get("name") == "휴대폰족",
              "휴대폰 단독 회원: 이메일 NULL · 휴대폰 인증 · provider=phone · 성함 저장",
              str((solo_user.get("email"), solo_user.get("phone"), solo_user.get("provider"), solo_user.get("name"))))
        check(solo_user.get("watchlist") == solo_watch,
              "가입과 동시에 관심자산 즉시 저장 (SK하이닉스·TSLA 포함)", str(solo_user.get("watchlist")))

        status, _, raw = http(base + "/api/watchlist", "POST",
                              {"phone": solo_phone, "watchlist": ["BTC", "NVDA"]})
        solo_wl = json.loads(raw)
        check(status == 200 and solo_wl.get("user", {}).get("watchlist") == ["BTC", "NVDA"],
              "이메일 없는 회원도 POST /api/watchlist (휴대폰 기준) → 200", f"HTTP {status}")

        status, _, raw = http(base + "/api/admin/users?q=" + urllib.parse.quote(solo_phone), cookie=admin_cookie)
        found = json.loads(raw)
        found_items = found.get("items") or []
        check(status == 200 and found.get("total") == 1 and found_items
              and found_items[0].get("phone") == solo_phone and found_items[0].get("email") is None
              and found_items[0].get("name") == "휴대폰족",
              "관리자 회원 목록: 이메일 없는 회원 검색·표시 (이름 포함, UNIQUE·목록 안 깨짐)",
              f"HTTP {status} total={found.get('total')}")

        with db.session() as conn:
            solo_row = conn.execute(
                "SELECT email, phone, provider, watchlist, phone_verified FROM users WHERE phone = ?",
                (solo_phone,)).fetchone()
            solo_stored = dict(solo_row) if solo_row else None
            conn.execute("DELETE FROM users WHERE phone = ?", (solo_phone,))
            conn.execute("DELETE FROM auth_otp WHERE target = ?", (solo_phone,))
            conn.execute("DELETE FROM phone_auth_state WHERE phone = ?", (solo_phone,))
            conn.commit()
        check(solo_stored is not None and solo_stored["email"] is None
              and json.loads(solo_stored["watchlist"]) == ["BTC", "NVDA"]
              and solo_stored["phone_verified"] == 1,
              "휴대폰 단독 회원 SQLite 저장 확인 (email NULL 허용)",
              str({k: solo_stored[k] for k in ("email", "phone", "provider")}) if solo_stored else "행 없음")

        status, _, raw = http(base + "/api/signup", "POST", {"email": "not-an-email"})
        check(status == 400 and "이메일" in json.loads(raw).get("error", ""), "잘못된 이메일 → 400")

        status, _, raw = http(base + "/api/watchlist", "POST", {"email": email, "watchlist": ["DOGE"]})
        check(status in (400, 404), "지원하지 않는 자산 → 4xx", f"HTTP {status}")

        status, _, raw = http(base + "/api/nope")
        check(status == 404, "없는 경로 → 404 JSON", f"HTTP {status}")

        status, _, raw = http(base + "/%2e%2e/config.py")
        check(status in (403, 404), "상위 경로 접근 차단", f"HTTP {status}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        check(proc.poll() is not None, "테스트 서버 정상 종료", f"exit={proc.returncode}")


def collector_job_checks() -> None:
    print("\n── 4. 수집기 백그라운드 작업 (stub) ────────────────────────")
    server.COLLECT_STEPS = (("stub-collector",
        "import json; print('__RESULT__' + json.dumps({'new': 1, 'total': 9, "
        "'source_status': {'fed': 'ok (2)', 'sec': 'ok (1)', 'market': 'ok (7)'}}))"),)
    started, message = server.start_collect("verify")
    check(started, "수집 작업 시작 (스텁 단계)", message)
    for _ in range(120):
        time.sleep(0.1)
        if not server.collector_state()["running"]:
            break
    state = server.collector_state()
    last = state.get("last") or {}
    check(not state["running"] and last.get("ok") is True,
          "수집 작업 완료 · 상태 파일 기록", f"{last.get('duration_s')}초")
    check((last.get("source_status") or {}).get("fed") == "ok (2)",
          "소스별 결과 저장", str(last.get("source_status")))
    check(last.get("new_posts") == 1, "신규 글 수 기록", str(last.get("new_posts")))
    again, message2 = server.start_collect("verify")
    check(not again and "초 뒤" in message2, "연속 실행 방지 (최소 간격)", message2)


def alert_engine_checks() -> None:
    print("\n── 5. 타점 비상벨 엔진 — 임계치·쿨다운 ──────────────────")
    import authflow  # noqa: E402
    import collector  # noqa: E402

    db.init_db()
    phone = "010-5555-6600"
    sent: list[tuple[str, str]] = []

    def stub_send(p: str, text: str) -> dict:
        sent.append((p, text))
        return {"ok": True, "sent": True, "mode": "stub", "test_mode": False}

    with db.session() as conn:
        conn.execute("DELETE FROM alert_logs WHERE phone = ?", (phone,))
        conn.execute("DELETE FROM alert_rules WHERE phone = ?", (phone,))
        conn.commit()
        try:
            rule = db.upsert_user_alert_rule(conn, phone=phone, metric="kimp_pct", operator=">=",
                                              threshold=3.0, label="김프 +3.0% 이상")
            check(rule["cooldown_minutes"] == 360 and config.ALERT_COOLDOWN_MINUTES == 360,
                  "기본 쿨다운 360분(6시간)", str(rule["cooldown_minutes"]))
            check(db.get_active_alert_rule(conn, phone) is not None
                  and len(db.list_active_alert_rules(conn)) >= 1,
                  "활성 규칙 조회 (list_active_alert_rules)")

            hit = {"values": {"kimchi_premium": 3.42, "fear_greed": 50, "long_pct": 55.0}}
            result = collector.evaluate_market_alerts(conn, hit, send_fn=stub_send)
            check(result["sent"] == 1 and result["matched"] == 1 and len(sent) == 1,
                  "② 임계치 도달 → 문자 발송 1건",
                  f"matched={result['matched']} sent={result['sent']}")
            message = sent[0][1] if sent else ""
            check(message.startswith("[클레어덱스 비상벨]") and "+3.42%" in message
                  and "+3.00%" in message and "KST" in message and "\n" not in message,
                  "경보 문자: [클레어덱스 비상벨] 한 줄 · 관측값·기준값·시각", message)
            triggered = conn.execute(
                "SELECT last_triggered_at FROM alert_rules WHERE id = ?", (rule["id"],)
            ).fetchone()
            check(bool(triggered and triggered["last_triggered_at"]),
                  "last_triggered_at 갱신", str(triggered["last_triggered_at"] if triggered else None))
            logs = conn.execute(
                "SELECT * FROM alert_logs WHERE phone = ? ORDER BY id DESC", (phone,)
            ).fetchall()
            check(len(logs) == 1 and logs[0]["status"] == "sent"
                  and abs(float(logs[0]["observed_value"]) - 3.42) < 1e-9
                  and logs[0]["message"] == message,
                  "alert_logs 기록 (관측값·메시지·상태=sent)", f"{len(logs)}건")

            again = collector.evaluate_market_alerts(conn, hit, send_fn=stub_send)
            check(again["sent"] == 0 and again["skipped_cooldown"] == 1 and len(sent) == 1,
                  "② 6시간 쿨다운 안 중복 발송 차단",
                  f"cooldown={again['skipped_cooldown']} · 누적 발송 {len(sent)}건")

            conn.execute("UPDATE alert_rules SET last_triggered_at = NULL WHERE id = ?", (rule["id"],))
            conn.commit()
            below = collector.evaluate_market_alerts(
                conn, {"values": {"kimchi_premium": 2.0, "fear_greed": 50, "long_pct": 55.0}},
                send_fn=stub_send,
            )
            check(below["matched"] == 0 and below["sent"] == 0 and len(sent) == 1,
                  "③ 임계치 미도달 → 미발송", f"matched={below['matched']} · 발송 {len(sent)}건")

            missing = collector.evaluate_market_alerts(conn, {"values": {"fear_greed": 5}}, send_fn=stub_send)
            check(missing["sent"] == 0 and "kimp_pct" in missing["missing_metrics"],
                  "지표 값이 없으면 발송하지 않음 (missing_metrics)",
                  str(missing["missing_metrics"]))

            db.upsert_user_alert_rule(conn, phone=phone, metric="fng", operator="<=", threshold=15)
            fear = collector.evaluate_market_alerts(conn, {"values": {"fear_greed": 12}}, send_fn=stub_send)
            check(fear["sent"] == 1 and "공포탐욕" in sent[-1][1] and "12" in sent[-1][1],
                  "공포탐욕 ≤ 15 조건도 같은 엔진으로 발송", sent[-1][1] if sent else "")

            def failing_send(p: str, text: str) -> dict:
                return {"ok": False, "sent": False, "error": "stub failure"}

            conn.execute("UPDATE alert_rules SET last_triggered_at = NULL WHERE phone = ?", (phone,))
            conn.commit()
            failed = collector.evaluate_market_alerts(conn, {"values": {"fear_greed": 10}},
                                                      send_fn=failing_send)
            failed_row = conn.execute(
                "SELECT last_triggered_at FROM alert_rules WHERE phone = ? AND active = 1", (phone,)
            ).fetchone()
            failed_log = conn.execute(
                "SELECT status FROM alert_logs WHERE phone = ? ORDER BY id DESC LIMIT 1", (phone,)
            ).fetchone()
            check(failed["failed"] == 1 and failed_log and failed_log["status"] == "failed"
                  and not (failed_row and failed_row["last_triggered_at"]),
                  "발송 실패는 실패로 기록하고 last_triggered_at 은 그대로 (재시도 가능)",
                  f"failed={failed['failed']}")

            db.upsert_user_alert_rule(conn, phone=phone, metric="long_pct", operator=">=", threshold=65.0)
            active_now = db.list_alert_rules_by_phone(conn, phone)
            check(len(active_now) == 1 and active_now[0]["metric"] == "long_pct",
                  "무료 회원 활성 타점 알림 1개 정책 — 새 조건이 기존을 대체",
                  f"활성 {len(active_now)}개 · {active_now[0]['metric'] if active_now else '없음'}")
        finally:
            conn.execute("DELETE FROM alert_logs WHERE phone = ?", (phone,))
            conn.execute("DELETE FROM alert_rules WHERE phone = ?", (phone,))
            conn.commit()

    # 자격증명 미설정 시 실제 발송 없이 test_mode 로 안전하게 돌려준다.
    saved_env = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("SMS_")}
    try:
        probe = authflow.send_sms_text(phone, "[클레어덱스 비상벨] 테스트")
    finally:
        os.environ.update(saved_env)
    check(probe.get("ok") is True and probe.get("sent") is False
          and probe.get("test_mode") is True and probe.get("mode") == "unconfigured",
          "SMS 자격증명 미설정 → send_sms_text test_mode 안전 반환 (실제 발송 없음)",
          str({k: probe.get(k) for k in ("mode", "sent", "test_mode")}))


def naver_publish_checks() -> None:
    print("\n── 4. 네이버 블로그 발행 파이프라인 ───────────────────────")
    import naver_publish  # noqa: E402  (네트워크·브라우저 없이 상태 로직만 검사)

    naver_publish.RESULT_PATH = VERIFY_TMP / "naver_publish_state.json"
    naver_publish.SHOT_DIR = VERIFY_TMP / "naver_shots"
    info = naver_publish.session_info()
    check(set(info) >= {"available", "path", "blog_id", "note"} and info["blog_id"] == "cryptobrothers",
          "네이버 세션 상태 점검 (blog.naver.com/cryptobrothers)", f"available={info['available']}")

    dry = naver_publish.run(limit=1, dry_run=True)
    check(dry.get("status") in ("dry_run", "nothing_pending"),
          "발행 대상 조회 (모의 실행)", f"status={dry.get('status')} pending={dry.get('pending')}")

    with db.session() as conn:
        before = db.naver_publish_stats(conn)
    real = naver_publish.run(limit=1)
    check(real.get("status") in ("no_session", "playwright_missing", "nothing_pending"),
          "세션·브라우저 없으면 발행하지 않고 상태만 기록",
          f"status={real.get('status')} · {str(real.get('message'))[:70]}")
    with db.session() as conn:
        after = db.naver_publish_stats(conn)
    check(after["published"] == before["published"],
          "발행 실패/미준비 상태에서는 성공으로 기록하지 않음",
          f"published {before['published']} → {after['published']}")
    check(naver_publish.RESULT_PATH.exists(), "data/naver_publish_state.json 기록")

    script = config.BASE_DIR / "collect.sh"
    check(script.exists(), "collect.sh 존재", str(script))
    text = script.read_text(encoding="utf-8") if script.exists() else ""
    order = [text.find("collector.py"), text.find("naver_drafts.py"), text.find("naver_publish.py")]
    check(all(i > 0 for i in order) and order == sorted(order),
          "collect.sh: collector.py → naver_drafts.py → naver_publish.py 순서")
    check("max_new" in text and "CLAREDEX_MAX_NEW_PER_RUN" in text,
          "collect.sh: 실행당 발행 수 제한 (하루 2회 × 1건 = 2건/일)")
    check("collector_state.json" in text,
          "collect.sh: cron 실행도 collector_state.json 에 기록 (/admin 수집기 탭)")


def _js_balanced(src: str) -> bool:
    """esprima 가 없을 때 쓰는 간이 검사: 문자열·주석을 건너뛰고 괄호 짝만 본다."""
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    quote = None
    escaped = False
    line_comment = False
    block_comment = False
    i = 0
    while i < len(src):
        ch = src[i]
        nxt = src[i + 1] if i + 1 < len(src) else ""
        if line_comment:
            if ch == "\n":
                line_comment = False
        elif block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 1
        elif quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
        else:
            if ch == "/" and nxt == "/":
                line_comment = True
                i += 1
            elif ch == "/" and nxt == "*":
                block_comment = True
                i += 1
            elif ch in "\"'`":
                quote = ch
            elif ch in "([{":
                stack.append(ch)
            elif ch in ")]}":
                if not stack or stack[-1] != pairs[ch]:
                    return False
                stack.pop()
        i += 1
    return not stack and quote is None


def js_syntax_check(html_text: str, name: str) -> None:
    """페이지의 인라인 스크립트 문법을 검사한다 (esprima 우선 · 없으면 괄호 짝 검사)."""
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html_text, re.S)
    if not scripts:
        check(False, f"{name}: 인라인 스크립트 존재")
        return
    try:
        import esprima  # noqa: F401
    except Exception:  # noqa: BLE001
        esprima = None
    ok_all = True
    detail = ""
    for idx, script in enumerate(scripts):
        if esprima is not None:
            try:
                esprima.parseScript(script)
            except Exception as exc:  # noqa: BLE001
                ok_all = False
                detail = f"{idx + 1}번째 블록: {exc}"
        elif not _js_balanced(script):
            ok_all = False
            detail = f"{idx + 1}번째 블록: 괄호 짝 불일치"
    check(ok_all, f"{name}: 인라인 JS 문법 검증",
          detail or f"{len(scripts)}블록 · {'esprima' if esprima is not None else 'bracket'}")


def static_checks() -> None:
    print("\n── 3. 정적 프리뷰 — 운영자 데이터 비공개 ──────────────────")
    proc = subprocess.run(
        [sys.executable, "export_static.py"],
        cwd=str(config.BASE_DIR), capture_output=True, text=True, timeout=120,
    )
    ok = check(proc.returncode == 0, "export_static.py 실행",
               (proc.stderr or proc.stdout or "")[-300:] if proc.returncode else "")
    if ok:
        drafts_snapshot = config.PUBLIC_DIR / "data" / "naver-drafts.json"
        check(not drafts_snapshot.exists(),
              "public/data/naver-drafts.json 내보내지 않음 (공개 호스팅 차단)",
              str(drafts_snapshot) if drafts_snapshot.exists() else "파일 없음")
        check((config.PUBLIC_DIR / "data" / "posts.json").exists(), "public/data/posts.json 정상 생성")
        stats_snapshot = config.PUBLIC_DIR / "data" / "stats.json"
        check(stats_snapshot.exists(), "public/data/stats.json 정상 생성 (오늘·최근 건수)", str(stats_snapshot))
        if stats_snapshot.exists():
            export_stats = json.loads(stats_snapshot.read_text(encoding="utf-8"))
            check(isinstance(export_stats.get("today"), dict) and isinstance(export_stats.get("recent"), dict),
                  "stats.json: today·recent 구조")
        cal_snap = config.PUBLIC_DIR / "data" / "calendar.json"
        check(cal_snap.exists(), "public/data/calendar.json 정상 생성")
        if cal_snap.exists():
            cal_data = json.loads(cal_snap.read_text(encoding="utf-8"))
            cal_list = cal_data.get("events") or []
            check(bool(cal_list) and all(e.get("importance") in config.IMPORTANCE_LEVELS
                                          and e.get("importance_label") for e in cal_list),
                  "calendar.json 스냅샷에 중요도(S/A/B) 포함", f"{len(cal_list)}건")

    index_html = (config.PUBLIC_DIR / "index.html").read_text(encoding="utf-8")
    signup_html = (config.PUBLIC_DIR / "signup.html").read_text(encoding="utf-8")
    check("admin-blog" not in index_html and "admin-blog" not in signup_html,
          "메인·회원가입에 admin-blog 링크 없음")
    check(not (config.PUBLIC_DIR / "admin-blog.html").exists(), "public/admin-blog.html 삭제됨")

    admin_html = (config.PUBLIC_DIR / "admin" / "index.html").read_text(encoding="utf-8")
    check("noindex, nofollow" in admin_html and "admin.css" in admin_html and "app.js" in admin_html,
          "관리자 콘솔: noindex · 전용 CSS/JS")
    check("운영자 비밀번호" in admin_html and "지금 수집 실행" in admin_html
          and "비밀번호 변경" in admin_html,
          "관리자 콘솔: 로그인·수집기·비밀번호 변경 화면 포함")
    check(not any((config.PUBLIC_DIR / "admin" / n).exists()
                  for n in ("admin_auth.json", "admin_audit.jsonl", "claredex.db")),
          "public/admin 에 비밀번호·DB 파일 없음")
    with db.session() as conn:
        published = db.count_posts(conn, "published")
    snap = json.loads((config.PUBLIC_DIR / "data" / "posts.json").read_text(encoding="utf-8"))
    check(snap.get("count") <= published, "posts.json 스냅샷에 숨김 글 미포함",
          f"{snap.get('count')}/{published}")
    snap_posts = snap.get("posts") or []
    check(all(p.get("importance") in config.IMPORTANCE_LEVELS and p.get("importance_label")
              for p in snap_posts),
          "posts.json 스냅샷에 importance·라벨 포함", f"{len(snap_posts)}건")
    check(bool(snap_posts) and all(k in snap_posts[0] for k in ("importance", "category_label")),
          "posts.json: 정적 화면용 필드(importance·category_label)")

    # ── 타점 비상벨(김프·공포탐욕·롱숟) 화면·프리셋·JS 문법
    presets = ("🔥 김프 +3.0% 이상", "❄️ 역프 -0.5% 이하", "😱 공포탐욕 15 이하",
               "🤑 공포탐욕 75 이상", "⚖️ 롱 비율 65% 돌파")
    missing_presets = [p for p in presets if p not in signup_html]
    check(not missing_presets, "가입 화면: 5가지 원클릭 타점 프리셋 칩",
          ", ".join(missing_presets) or "5/5")
    check("/api/market" in signup_html and 'id="alertNow"' in signup_html
          and all(f'data-metric="{m}"' in signup_html for m in config.ALERT_METRICS),
          "가입 화면: /api/market 으로 김프·공포탐욕·롱비율 현재 수치 표시")
    check('id="alertThreshold"' in signup_html and "직접 입력" in signup_html
          and all(op in signup_html for op in ('value=">="', 'value="<="')),
          "가입 화면: 직접 숫자 입력(지표·조건·임계값)")
    check('get("alert")' in signup_html and all(k in signup_html for k in
          ("kimp_high", "kimp_low", "fng_low", "fng_high", "long_high")),
          "가입 화면: ?alert= 파라미터로 프리셋 자동 선택")
    check("alert_rule" in signup_html and "payload.alert_rule" in signup_html,
          "가입 화면: 선택한 타점 조건을 /api/signup alert_rule 로 전송")
    check('id="alertSummary"' in signup_html and "타점 비상벨 조건" in signup_html,
          "가입 완료 화면에 설정된 타점 비상벨 조건 표시")
    check('alert=kimp_high' in index_html and "김프·공포탐욕·롱숟 1초 비상벨 설정" in index_html,
          "메인 상단/관심자산 영역에 타점 비상벨 설정 바")
    js_syntax_check(signup_html, "signup.html")
    js_syntax_check(index_html, "index.html")


def free_port(preferred: int = 8765) -> int:
    for port in (preferred, 8766, 8767, 8799, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("사용 가능한 포트를 찾지 못했습니다")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()

    db_checks()
    port = args.port or free_port()
    server_checks(port)
    collector_job_checks()
    alert_engine_checks()
    naver_publish_checks()
    static_checks()

    passed = sum(1 for ok, _, _ in RESULTS if ok)
    print("\n" + "=" * 74)
    print(f"검증 결과: {passed}/{len(RESULTS)} 통과")
    failed = [name for ok, name, _ in RESULTS if not ok]
    if failed:
        print("실패 항목:")
        for name in failed:
            print("  -", name)
    print("=" * 74)

    report = {
        "checked_at": config.iso_kst(),
        "port": port,
        "passed": passed,
        "total": len(RESULTS),
        "failed": failed,
        "results": [{"ok": ok, "name": name, "detail": detail} for ok, name, detail in RESULTS],
    }
    (config.DATA_DIR / "verify_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"리포트: {config.DATA_DIR / 'verify_report.json'}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
