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
import socket
import subprocess
import sys
import time
from datetime import timedelta
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import config

config.load_env()

import content  # noqa: E402
import db  # noqa: E402
import server  # noqa: E402  (운영자 비밀번호·세션 규칙을 한 곳에서 가져온다)

RESULTS: list[tuple[bool, str, str]] = []


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
    conn = db.connect()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("posts", "users", "naver_drafts"):
        check(t in tables, f"테이블 존재: {t}")

    post_cols = {r[1] for r in conn.execute("PRAGMA table_info(posts)")}
    required = {"id", "title", "category", "source_name", "source_grade", "source_url", "source_hash",
                "verified_at", "summary_lines", "market_impact", "tags", "image_path",
                "is_member_early", "created_at", "importance"}
    check(required <= post_cols, "posts 필수 컬럼", ", ".join(sorted(required - post_cols)) or "모두 존재")
    draft_cols = {r[1] for r in conn.execute("PRAGMA table_info(naver_drafts)")}
    check({"post_id", "seo_title", "keywords", "body_html", "body_text", "telegram_cta", "web_cta",
           "image_path"} <= draft_cols, "naver_drafts 필수 컬럼")
    user_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    user_required = {"email", "name", "provider", "watchlist", "phone", "phone_verified", "plan", "alerts_enabled"}
    check(user_required <= user_cols, "users 필수 컬럼 (휴대폰·요금제)",
          ", ".join(sorted(user_required - user_cols)) or "모두 존재")
    check("phone_verifications" in tables, "테이블 존재: phone_verifications")

    posts = db.list_posts(conn)
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

    levels = {p["importance"] for p in posts}
    check(levels <= set(config.IMPORTANCE_LEVELS), "중요도 값 S/A/B",
          ", ".join(sorted(levels)) or "없음")
    check(all(p.get("importance_label") for p in posts), "중요도 라벨 표시 값")
    check(not any("세부 내용과 수치는 원문에서 확인하세요" in line for p in posts for line in p["summary_lines"]),
          "천편일률적 안내 문구 제거", "요약 3줄에 반복 문구 없음")
    rules = [
        (content.fed_importance("fomc-statement", "", "FOMC statement") == "S", "FOMC 성명 → S"),
        (content.fed_importance("fomc-minutes", "", "Minutes of the FOMC") == "S", "FOMC 의사록 → S"),
        (content.fed_importance("regulation", "",
                                "Agencies announce dollar thresholds for smaller loan exemption from appraisal requirements") == "B",
         "모기지·면제 기준 행정 발표 → B"),
        (content.sec_importance("8-K", ["2.02"]) == "S", "8-K 2.02 실적 → S"),
        (content.sec_importance("10-Q", []) == "A", "10-Q → A"),
        (content.sec_importance("8-K", ["7.01"]) == "B", "8-K 7.01 → B"),
        (content.dart_importance("주요사항보고서(유상증자결정)") == "A", "DART 유상증자 → A"),
    ]
    check(all(ok for ok, _ in rules), "중요도 분류 규칙",
          ", ".join(name for ok, name in rules if not ok) or "7개 규칙 통과")

    stats = db.today_stats(conn)
    check(stats["date"] == config.now_kst().strftime("%Y-%m-%d"), "오늘(KST) 기준일", stats["date"])
    check(0 <= stats["count"] <= stats["total"], "오늘 확인 건수 ≤ 전체", f"{stats['count']}/{stats['total']}")
    check(stats["count"] > 0 or stats["recent"] is not None or stats["total"] == 0,
          "오늘 0건이면 최근 확인일 제공", str(stats["recent"]))
    future = db.today_stats(conn, now=config.now_kst() + timedelta(days=1))
    check(future["count"] == 0 and (future["recent"] or future["total"] == 0),
          "다음 날 기준: 오늘 0건 · 최근 확인 건수 계산",
          f"0건 · 최근 {future['recent']}")

    banned = ("매수하세요", "매도하세요", "수익 보장", "반드시 오릅니다", "목표가")
    dirty = [p["id"] for p in posts if any(b in p["market_impact"] for b in banned)]
    check(not dirty, "컴플라이언스 금지 표현 없음", f"위반 {dirty}" if dirty else "매수·매도 권유 표현 없음")

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
        if config.COMPLIANCE_LINE not in d["body_html"]:
            issues.append((d["id"], "disclaimer"))
        if "<img" not in d["body_html"] or "t.me/Clare_PerpDEX" not in d["body_html"]:
            issues.append((d["id"], "image"))
        if len(d["seo_title"]) > 50:
            issues.append((d["id"], "seo_title_length"))
    check(not issues, "초안: 키워드 5~8 · 텔레그램/웹 링크 · 고지 · 이미지 링크 · 제목 50자", f"{issues}" if issues else "전체 정상")

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
        # --- TUPL 스타일 단일 컬럼 UI (히어로 → 다크 카드 → 캐러셀 → 최신 속보 → 일정 → 가입 안내)
        check("OFFICIAL BREAKING" in html and "공식 원문으로 읽는 오늘의 시장" in html
              and "오늘의 시장, 공식 원문 3초 결론" in html,
              "히어로: 라벨 → 헤드라인 → 골드 구분선 → 서브카피")
        check(html.count("<h1") == 1, "h1은 히어로 한 곳뿐", f"h1 {html.count('<h1')}개")
        check('class="col"' in html and 'class="side"' not in html and 'class="layout"' not in html,
              "단일 컬럼 레이아웃 (사이드바·2단 그리드 없음)")
        check('id="featured"' in html and 'id="featuredTitle"' in html and 'id="featuredBody"' in html
              and 'featured-label' in html,
              "다크 네이비 '오늘의 한 장' 카드 1장")

        cta_tag = ""
        if 'id="featuredCta"' in html:
            start = html.rfind("<", 0, html.index('id="featuredCta"'))
            end = html.index(">", html.index('id="featuredCta"')) + 1
            cta_tag = html[start:end]
        check(bool(cta_tag) and "signup.html" in cta_tag and "aria-disabled" not in cta_tag,
              "피처드 CTA → signup.html 활성 (aria-disabled 없음)", cta_tag[:120])
        check('id="carousel"' in html and 'id="carouselTrack"' in html and 'id="carouselDots"' in html,
              "주요 속보 캐러셀 + 점 인디케이터")
        check(all(label in html for label in ("S 긴급", "미국주식", "국내주식", "매크로")),
              "속보 필터 칩 (전체·S 긴급·미국주식·국내주식·코인·매크로)")
        check('id="feed"' in html and 'class="feed-item' in html and "공식 원문 ↗" in html,
              "최신 속보 카드: 라벨·제목·요약·시각+원문 링크")
        check("badge-imp" in html and "badge-s" in html and "id=\"eventList\"" in html
              and 'id="cdClock"' in html,
              "중요도 라벨 · 카운트다운 · 공식 일정 목록 마크업")
        check("id=\"plans\"" in html and "PRO" in html and "무지연 속보" in html,
              "무료 회원 혜택 · Pro 플랜 안내")
        check("id=\"countLine\"" in html and "오늘 확인" in html, "오늘 확인 건수 표기")

        # --- 차별화 기능 1: 관심자산 1초 미리보기 → 휴대폰 인증 퍼널
        check('id="watchChips"' in html and all(f'data-watch="{a}"' in html
                                                for a in ("BTC", "NVDA", "AAPL", "연준·금리")),
              "관심자산 1초 미리보기 칩 (BTC·NVDA·AAPL·연준·금리)")
        check('id="convertBar"' in html and "선택하신" in html and "휴대폰으로 받기" in html
              and "signup.html?watch=" in html and 'id="convertText"' in html,
              "선택 시 1줄 전환 바 → signup.html?watch=<자산>")
        check("matchWatch" in html and "WATCHES" in html and "URLSearchParams" in html,
              "피드 필터: 태그 기반 관심자산 필터 + watch 파라미터 복원")

        # --- 차별화 기능 2: 공식 일정 체크포인트 + 발표 알림 링크
        check('id="cdCheck"' in html and "이 일정 발표 알림 받기" in html
              and "ev-check" in html and 'id="cdWatch"' in html,
              "공식 일정: 1줄 체크포인트 + 발표 알림 링크")

        # --- 차별화 기능 3: 원문 출처 + 확인 시각(KST) 1줄 메타
        check("src-meta" in html and "출처 " in html and "KST</time>" in html
              and "story-time" in html,
              "속보 카드 메타: 출처 + 확인 시각(KST) 1줄")

        status, headers, raw = http(base + "/signup.html")
        signup_html = raw.decode("utf-8", "replace")
        check(status == 200 and "휴대폰 인증" in signup_html and "인증번호 받기" in signup_html,
              "GET /signup.html → 200 (휴대폰 인증 흐름)", f"HTTP {status}")
        check('id="phone"' in signup_html and 'id="code"' in signup_html and "관심자산" in signup_html,
              "회원가입: 번호 입력 → 인증번호 → 관심자산 단계")
        check('id="requestNote"' in signup_html and "URLSearchParams" in signup_html
              and "resolveWatch" in signup_html and "FOMC" in signup_html,
              "회원가입: watch 파라미터 → 관심자산 미리 체크 + 일정 알림 안내")
        check(all(a in signup_html for a in ('"AAPL"', '"연준·금리"', '"NVDA"')),
              "회원가입 관심자산: AAPL·연준·금리 포함")
        check('class="col"' in signup_html and "auth-card" in signup_html and 'class="layout"' not in signup_html,
              "회원가입도 오프화이트 단일 컬럼 (auth-card)")
        status, _, raw = http(base + "/signup")
        check(status == 200 and "휴대폰" in raw.decode("utf-8", "replace"), "GET /signup (기존 404 경로) → 200")

        status, headers, raw = http(base + "/admin")
        admin_html = raw.decode("utf-8", "replace")
        check(status == 200 and "운영자 로그인" in admin_html and "운영자 비밀번호" in admin_html,
              "GET /admin → 200 (운영자 로그인 게이트)", f"HTTP {status}")
        check(headers.get("Cache-Control") == "no-store", "운영자 화면 Cache-Control: no-store")
        status, _, raw = http(base + "/admin/")
        check(status == 200 and "운영자 비밀번호" in raw.decode("utf-8", "replace"),
              "GET /admin/ → 200", f"HTTP {status}")
        check("정적 프리뷰에서는 보안을 위해 내부 초안 데이터를 비공개로 둡니다" in admin_html,
              "운영자 화면에 정적 프리뷰 비공개 안내 포함")
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
        check(all(k in first for k in ("title", "summary_lines", "market_impact", "tags", "source_url", "verified_at",
                                        "importance", "importance_label")),
              "글 응답 필드 완전성 (중요도 포함)")

        status, _, raw = http(base + "/api/stats")
        api_stats = json.loads(raw)
        with db.session() as conn:
            expected = db.today_stats(conn)
        check(status == 200 and api_stats.get("today", {}).get("date") == expected["date"]
              and api_stats.get("today", {}).get("count") == expected["count"],
              "GET /api/stats → 오늘(KST) 확인 건수", f"{api_stats.get('today', {}).get('count')}건")

        status, _, raw = http(base + "/api/plans")
        plans = json.loads(raw)
        check(status == 200 and {"free", "pro"} <= set(plans.get("plans", {}))
              and plans.get("sms_mode") in ("demo", "live"),
              "GET /api/plans → 무료/Pro 플랜·SMS 모드", str(plans.get("sms_mode")))

        status, _, raw = http(base + "/api/posts?category=macro")
        macro = json.loads(raw)
        check(status == 200 and macro["posts"] and all(p["category"] == "macro" for p in macro["posts"]),
              "GET /api/posts?category=macro 필터")

        status, _, raw = http(base + "/api/posts?q=" + urllib.parse.quote("연준"))
        q = json.loads(raw)
        check(status == 200 and q.get("count", 0) >= 1, "GET /api/posts?q=연준 검색", f"{q.get('count')}건")

        status, _, raw = http(base + "/api/admin/session")
        sess = json.loads(raw)
        check(status == 200 and sess.get("authenticated") is False,
              "미로그인 GET /api/admin/session → authenticated:false", f"HTTP {status}")

        status, _, raw = http(base + "/api/naver-drafts")
        drafts_denied = json.loads(raw)
        check(status == 401 and "로그인" in drafts_denied.get("error", ""),
              "미로그인 GET /api/naver-drafts → 401", f"HTTP {status}")

        status, _, raw = http(base + "/api/admin/login", "POST", {"password": "wrong-password-for-verify"})
        check(status == 401 and json.loads(raw).get("ok") is False,
              "틀린 비밀번호 POST /api/admin/login → 401", f"HTTP {status}")

        status, headers, raw = http(base + "/api/admin/login", "POST", {"password": server.admin_password()})
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
        check(status == 200 and json.loads(raw).get("authenticated") is True,
              "로그인 후 GET /api/admin/session → authenticated:true")

        with db.session() as conn:
            expected_drafts = db.count_drafts(conn)
        status, _, raw = http(base + "/api/naver-drafts", cookie=admin_cookie)
        drafts = json.loads(raw)
        check(status == 200 and drafts.get("count") == expected_drafts,
              f"로그인 후 GET /api/naver-drafts → 200 · {expected_drafts}건",
              f"HTTP {status} · {drafts.get('count')}건")
        d0 = (drafts.get("drafts") or [{}])[0]
        check("t.me/Clare_PerpDEX" in d0.get("body_html", "") and d0.get("seo_title"),
              "초안 응답: 본문·이미지 링크 포함")

        status, headers, raw = http(base + "/api/admin/logout", "POST", {}, cookie=admin_cookie)
        check(status == 200 and "Max-Age=0" in headers.get("Set-Cookie", ""),
              "POST /api/admin/logout → 200 + 쿠키 만료")
        status, _, raw = http(base + "/api/naver-drafts", cookie=admin_cookie)
        check(status == 401, "로그아웃 후 GET /api/naver-drafts → 401 (세션 폐기)", f"HTTP {status}")

        status, _, raw = http(base + "/api/calendar")
        cal = json.loads(raw)
        evs = cal.get("events", [])
        check(status == 200 and len(evs) >= 3, "GET /api/calendar → 공식 일정 3건 이상", f"{len(evs)}건")
        required_ev = ("id", "title", "org", "datetime_kst", "local_time", "source_url", "note", "importance")
        blank = [e.get("id") for e in evs if any(not str(e.get(k) or "").strip() for k in required_ev)]
        check(not blank, "공식 일정 빈 값(—) 없음", str(blank) if blank else "모든 항목 채워짐")
        check(all("+09:00" in e["datetime_kst"] for e in evs), "공식 일정 KST 시각")
        check(evs == sorted(evs, key=lambda e: e["datetime_kst"]), "공식 일정 시간순 정렬")
        check(all(str(e.get("checkpoint") or "").strip() for e in evs),
              "공식 일정 '발표 전 체크포인트' 1줄",
              ", ".join(e["id"] for e in evs if not str(e.get("checkpoint") or "").strip()) or "전체 기입")
        check(all(len(str(e.get("checkpoint") or "")) <= 80 for e in evs),
              "체크포인트 80자 이내 (1줄 유지)",
              f"최대 {max((len(str(e.get('checkpoint') or '')) for e in evs), default=0)}자")
        banned_cp = ("매수하세요", "매도하세요", "수익 보장", "반드시 오릅니다", "목표가")
        dirty_cp = [e["id"] for e in evs if any(b in str(e.get("checkpoint") or "") for b in banned_cp)]
        check(not dirty_cp, "체크포인트: 매수·매도 권유 표현 없음", f"위반 {dirty_cp}" if dirty_cp else "객관적 관전 포인트")
        allowed_watch = set(config.WATCHLIST_ASSETS) | set(config.EVENT_WATCH_ALIASES)
        unknown_watch = sorted({"".join(str(e.get("watch") or "").split()) for e in evs} - allowed_watch - {""})
        check(all(str(e.get("watch") or "").strip() for e in evs) and not unknown_watch,
              "일정 알림 키 → 가입 관심자산 매핑 가능",
              f"모르는 키 {unknown_watch}" if unknown_watch else ", ".join(sorted({str(e.get("watch")) for e in evs})))

        email = f"verify-{int(time.time())}@claredex.test"
        status, _, raw = http(base + "/api/signup", "POST",
                              {"email": email, "name": "검증", "provider": "email", "watchlist": ["BTC", "ETH"]})
        signup = json.loads(raw)
        check(status == 201 and signup.get("ok") and signup["user"]["email"] == email,
              "POST /api/signup → 201", f"HTTP {status}")

        status, _, raw = http(base + "/api/watchlist", "POST",
                              {"email": email, "watchlist": ["BTC", "ETH", "나스닥", "NVDA", "AAPL", "연준·금리"]})
        wl = json.loads(raw)
        check(status == 200 and wl.get("ok") and len(wl["user"]["watchlist"]) == 6,
              "POST /api/watchlist → 200 저장 (AAPL·연준·금리 포함)", f"HTTP {status}")

        status, _, raw = http(base + "/api/signup", "POST", {"email": email, "provider": "email", "watchlist": []})
        again = json.loads(raw)
        check(status == 201 and len(again["user"]["watchlist"]) == 6,
              "같은 이메일 재가입 시 관심자산 보존", str(again["user"]["watchlist"]))

        with db.session() as conn:
            row = conn.execute("SELECT watchlist FROM users WHERE email = ?", (email,)).fetchone()
            stored = json.loads(row[0]) if row else None
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.commit()
        check(stored == ["BTC", "ETH", "나스닥", "NVDA", "AAPL", "연준·금리"], "관심자산 SQLite 저장 확인", str(stored))

        # ---------------------------------------------------------- 휴대폰 인증 가입
        phone = "010" + str(int(time.time()))[-8:]
        status, _, raw = http(base + "/api/phone/send-code", "POST", {"phone": phone})
        sent = json.loads(raw)
        dev_code = str(sent.get("dev_code") or "")
        check(status == 200 and sent.get("demo") is True and len(dev_code) == 6,
              "POST /api/phone/send-code → 데모 모드 인증번호 발급",
              f"HTTP {status} · demo={sent.get('demo')}")
        check(sent.get("notice") and sent.get("sms_mode") == "demo",
              "데모 모드 안내 문구 포함", str(sent.get("sms_mode")))

        status, headers, raw = http(base + "/api/phone/send-code", "POST", {"phone": "02-123-4567"})
        check(status == 400, "잘못된 휴대폰 번호 → 400", f"HTTP {status}")

        status, _, raw = http(base + "/api/phone/verify-code", "POST",
                              {"phone": phone, "code": "000000"})
        check(status == 400 and "올바르지 않습니다" in json.loads(raw).get("error", ""),
              "틀린 인증번호 → 400", f"HTTP {status}")

        status, _, raw = http(base + "/api/phone/verify-code", "POST", {"phone": phone, "code": dev_code})
        verified = json.loads(raw)
        token = str(verified.get("verification_token") or "")
        check(status == 200 and len(token) >= 20, "POST /api/phone/verify-code → 인증 토큰", f"HTTP {status}")

        status, _, raw = http(base + "/api/signup", "POST",
                              {"phone": phone, "watchlist": ["BTC"], "provider": "phone"})
        check(status == 400 and json.loads(raw).get("need") == "phone_verification",
              "인증 토큰 없는 휴대폰 가입 → 400", f"HTTP {status}")

        status, _, raw = http(base + "/api/signup", "POST",
                              {"phone": phone, "name": "인증검증", "provider": "phone",
                               "verification_token": token, "watchlist": ["BTC", "ETH", "NVDA"]})
        ph_user = json.loads(raw)
        check(status == 201 and ph_user.get("user", {}).get("phone_verified") is True
              and ph_user.get("user", {}).get("plan") == "free",
              "POST /api/signup (휴대폰 인증) → 201 · phone_verified · plan:free", f"HTTP {status}")
        check(ph_user.get("user", {}).get("watchlist") == ["BTC", "ETH", "NVDA"],
              "휴대폰 가입 관심자산 저장", str(ph_user.get("user", {}).get("watchlist")))

        status, _, raw = http(base + "/api/signup", "POST",
                              {"phone": phone, "verification_token": token, "watchlist": []})
        check(status == 401, "인증 토큰 재사용 → 401", f"HTTP {status}")

        with db.session() as conn:
            row = conn.execute("SELECT phone_verified, plan, alerts_enabled, watchlist FROM users WHERE phone = ?",
                               (phone,)).fetchone()
            stored_phone = dict(row) if row else None
            conn.execute("DELETE FROM users WHERE phone = ?", (phone,))
            conn.execute("DELETE FROM phone_verifications WHERE phone = ?", (phone,))
            conn.commit()
        check(bool(stored_phone) and stored_phone["phone_verified"] == 1 and stored_phone["plan"] == "free"
              and stored_phone["alerts_enabled"] == 1,
              "SQLite: phone_verified·plan·alerts_enabled 저장", str(stored_phone))

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
        stats_path = config.PUBLIC_DIR / "data" / "stats.json"
        check(stats_path.exists(), "public/data/stats.json 정상 생성")
        if stats_path.exists():
            snap = json.loads(stats_path.read_text(encoding="utf-8"))
            check(snap.get("today", {}).get("date") and "count" in snap.get("today", {}),
                  "stats.json: 오늘(KST) 확인 건수")
        posts_path = config.PUBLIC_DIR / "data" / "posts.json"
        if posts_path.exists():
            snap_posts = json.loads(posts_path.read_text(encoding="utf-8")).get("posts") or []
            check(bool(snap_posts) and all(p.get("importance") in config.IMPORTANCE_LEVELS for p in snap_posts),
                  "posts.json: 중요도 S/A/B 포함")
        cal_path = config.PUBLIC_DIR / "data" / "calendar.json"
        if cal_path.exists():
            snap_cal = json.loads(cal_path.read_text(encoding="utf-8"))
            evs = snap_cal.get("events") or []
            blank = [e.get("id") for e in evs if any(not str(e.get(k) or "").strip()
                                                      for k in ("org", "local_time", "note", "source_url"))]
            check(len(evs) >= 3 and not blank, "calendar.json: 3건 이상 · 빈 값 없음",
                  f"{len(evs)}건" + (f" · 빈 값 {blank}" if blank else ""))
            missing_cp = [e.get("id") for e in evs if not str(e.get("checkpoint") or "").strip()
                          or not str(e.get("watch") or "").strip()]
            check(not missing_cp, "calendar.json: 체크포인트·알림 키 포함",
                  str(missing_cp) if missing_cp else f"{len(evs)}건")

    index_html = (config.PUBLIC_DIR / "index.html").read_text(encoding="utf-8")
    signup_html = (config.PUBLIC_DIR / "signup.html").read_text(encoding="utf-8")
    check("admin-blog" not in index_html and "admin-blog" not in signup_html,
          "메인·회원가입에 admin-blog 링크 없음")
    check(not (config.PUBLIC_DIR / "admin-blog.html").exists(), "public/admin-blog.html 삭제됨")
    check('id="watchChips"' in index_html and 'id="convertBar"' in index_html
          and "signup.html?watch=" in index_html,
          "메인: 관심자산 미리보기·1줄 전환 바")
    check('id="requestNote"' in signup_html and "URLSearchParams" in signup_html
          and "EVENT_ALIAS" in signup_html,
          "회원가입: watch 파라미터 사전 선택 마크업")


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
