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
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
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
    conn = db.connect()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("posts", "users", "naver_drafts", "auth_otp", "signup_verification", "naver_publish"):
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
    publish_cols = {r[1] for r in conn.execute("PRAGMA table_info(naver_publish)")}
    check({"draft_id", "post_id", "status", "url", "published_at"} <= publish_cols,
          "naver_publish 컴럼 (발행 이력)")

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
        check("공식 원문 기반 실시간 속보" in html and "내 관심자산 속보 무료로 받기" in html,
              "메인 히어로·CTA 문구 포함")
        check("실시간 속보" in html and "공식 원문 보기" in html, "타임라인 섹션 마크업 포함")
        check("flash-card" in html and "긴급 속보" in html and "flash-title" in html,
              "메인 최상단 긴급 속보 섹션")
        check("펼치기" in html and "바로 가입하기" in html,
              "펼치기 → 바로 가입하기 버튼")
        check("mask-image" in (config.PUBLIC_DIR / "assets" / "style.css").read_text(encoding="utf-8"),
              "하단 3/5 페이드 (mask-image 그라데이션)")
        flash_pos = html.find("flash-card")
        hero_pos = html.find('class="hero"')
        check(0 < flash_pos < hero_pos, "긴급 속보가 히어로보다 위에 배치")

        status, headers, raw = http(base + "/signup.html")
        signup_html = raw.decode("utf-8", "replace")
        check(status == 200 and "성함 또는 닉네임" in signup_html, "GET /signup.html → 200", f"HTTP {status}")
        check("인증번호 발송" in signup_html and "이메일 인증번호 6자리" in signup_html
              and "문자 인증번호 6자리" in signup_html,
              "회원가입: 이메일·훈대폰 OTP 입력란")
        consent_pos = signup_html.find("마케팅 활용 동의")
        check(consent_pos > 0 and "[보기]" in signup_html
              and "광고·마케팅 활용 및 제3자 제공 동의" in signup_html,
              "마케팅 활용 동의 + [보기] 모달")
        check("제3자 제공" in signup_html and "수집·이용 항목" in signup_html
              and "동의 거부 권리" in signup_html and "보유 및 이용 기간" in signup_html,
              "동의 모달: 수집·이용·제3자 제공·보유기간·거부 권리 포함")
        status, _, raw = http(base + "/signup")
        check(status == 200 and "카카오" in raw.decode("utf-8", "replace"), "GET /signup (기존 404 경로) → 200")

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

        status, _, raw = http(base + "/api/posts?category=macro")
        macro = json.loads(raw)
        check(status == 200 and macro["posts"] and all(p["category"] == "macro" for p in macro["posts"]),
              "GET /api/posts?category=macro 필터")

        status, _, raw = http(base + "/api/posts?q=FOMC")
        q = json.loads(raw)
        check(status == 200 and q.get("count", 0) >= 1, "GET /api/posts?q=FOMC 검색", f"{q.get('count')}건")

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
        check(status == 200 and len(cal.get("events", [])) >= 1, "GET /api/calendar → 200", f"{len(cal.get('events', []))}건")

        # ------------------------------------------------ 본인인증(이메일·휴대폰) 가입
        status, _, raw = http(base + "/api/auth/channels")
        ch = json.loads(raw)
        check(status == 200 and "email" in ch and "sms" in ch and isinstance(ch.get("sandbox"), bool),
              "GET /api/auth/channels → 발송 채널 상태",
              f"email={((ch.get('email') or {}).get('mode'))} sms={((ch.get('sms') or {}).get('mode'))}")

        email = f"verify-{int(time.time())}@claredex.test"
        phone = "010-9876-5432"

        status, _, raw = http(base + "/api/signup", "POST",
                              {"email": email, "name": "검증", "phone": phone})
        blocked = json.loads(raw)
        check(status == 403 and blocked.get("code") == "verification_required",
              "인증 없이 POST /api/signup → 403", f"HTTP {status}")

        status, _, raw = http(base + "/api/signup", "POST", {"email": email, "name": "", "phone": phone})
        check(status == 400 and json.loads(raw).get("code") == "name_required",
              "성함/닉네임 비어 있으면 400", f"HTTP {status}")

        status, _, raw = http(base + "/api/auth/send-email-otp", "POST", {"email": email})
        sent = json.loads(raw)
        check(status == 200 and sent.get("ok") and sent.get("delivery_mode") in ("unconfigured", "smtp", "resend"),
              "POST /api/auth/send-email-otp → 200",
              f"mode={sent.get('delivery_mode')} sent={sent.get('sent')}")

        status, _, raw = http(base + "/api/auth/send-email-otp", "POST", {"email": email})
        check(status == 429 and json.loads(raw).get("code") == "cooldown",
              "인증번호 재발송 30초 대기 → 429", f"HTTP {status}")

        code = sent.get("preview_code")
        if code:
            wrong = "000000" if code != "000000" else "111111"
            status, _, raw = http(base + "/api/auth/verify-email-otp", "POST", {"email": email, "code": wrong})
            check(status == 400 and json.loads(raw).get("code") == "mismatch",
                  "잘못된 이메일 인증번호 → 400", f"HTTP {status}")
            status, _, raw = http(base + "/api/auth/verify-email-otp", "POST", {"email": email, "code": code})
            ok_email = json.loads(raw)
            check(status == 200 and ok_email.get("ok") and ok_email.get("verification", {}).get("email_verified"),
                  "POST /api/auth/verify-email-otp → 200", f"HTTP {status}")
        else:
            check(True, "실제 SMTP 발송 모드 — 인증번호 검증은 코드 미노출로 건너뜀")

        status, _, raw = http(base + "/api/auth/send-sms-otp", "POST", {"email": email, "phone": phone})
        sms = json.loads(raw)
        check(status == 200 and sms.get("ok") and sms.get("channel") == "sms",
              "POST /api/auth/send-sms-otp → 200", f"mode={sms.get('delivery_mode')}")
        sms_code = sms.get("preview_code")
        if sms_code:
            status, _, raw = http(base + "/api/auth/verify-sms-otp", "POST",
                                  {"email": email, "phone": phone, "code": sms_code})
            ok_sms = json.loads(raw)
            check(status == 200 and ok_sms.get("ok") and ok_sms.get("verification", {}).get("phone_verified"),
                  "POST /api/auth/verify-sms-otp → 200", f"HTTP {status}")
        else:
            check(True, "실제 문자 발송 모드 — 인증번호 검증은 코드 미노출로 건너뜀")

        status, _, raw = http(base + "/api/signup", "POST",
                              {"email": email, "name": "검증", "phone": "010-1111-2222"})
        check(status == 403 and json.loads(raw).get("code") == "phone_mismatch",
              "인증한 번호와 다른 휴대폰 → 403", f"HTTP {status}")

        status, _, raw = http(base + "/api/signup", "POST",
                              {"email": email, "name": "검증", "phone": phone,
                               "provider": "email", "watchlist": ["BTC", "ETH"], "marketing_agreed": True})
        signup = json.loads(raw)
        check(status == 201 and signup.get("ok") and signup["user"]["email"] == email,
              "인증 후 POST /api/signup → 201", f"HTTP {status}")
        user = signup.get("user") or {}
        check(user.get("phone") == phone and user.get("email_verified") and user.get("phone_verified"),
              "회원 레코드에 연락처·인증 상태 저장", str((user.get("phone"), user.get("email_verified"), user.get("phone_verified"))))
        check(user.get("marketing_agreed") and user.get("marketing_agreed_at"),
              "마케팅 활용 동의 + 동의 시각 저장", str(user.get("marketing_agreed_at")))

        status, _, raw = http(base + "/api/watchlist", "POST",
                              {"email": email, "watchlist": ["BTC", "ETH", "나스닥", "NVDA"]})
        wl = json.loads(raw)
        check(status == 200 and wl.get("ok") and len(wl["user"]["watchlist"]) == 4,
              "POST /api/watchlist → 200 저장", f"HTTP {status}")

        status, _, raw = http(base + "/api/signup", "POST",
                              {"email": email, "name": "검증", "phone": phone, "provider": "email", "watchlist": []})
        again = json.loads(raw)
        check(status == 201 and len(again["user"]["watchlist"]) == 4,
              "같은 이메일 재가입 시 관심자산 보존", str(again["user"]["watchlist"]))

        with db.session() as conn:
            row = conn.execute(
                "SELECT watchlist, phone, email_verified, phone_verified, marketing_agreed "
                "FROM users WHERE email = ?", (email,)).fetchone()
            stored = json.loads(row[0]) if row else None
            flags = tuple(row[1:]) if row else None
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.execute("DELETE FROM signup_verification WHERE email = ?", (email,))
            conn.execute("DELETE FROM auth_otp WHERE email = ?", (email,))
            conn.commit()
        check(stored == ["BTC", "ETH", "나스닥", "NVDA"], "관심자산 SQLite 저장 확인", str(stored))
        check(flags == (phone, 1, 1, 1), "본인인증·마케팅 동의 SQLite 저장 확인", str(flags))

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
