"""ClareDEX v2 자체 검증 스크립트.

1) DB 스키마·글·초안·썸네일 무결성
2) 실제 서버를 임시 포트로 띄워 HTTP 200 / JSON / HTML / POST 동작 확인 후 정상 종료

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
import urllib.error
import urllib.request
from pathlib import Path

import config

config.load_env()

import db  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    RESULTS.append((bool(ok), name, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f"  — {detail}" if detail else ""))
    return bool(ok)


def http(url: str, method: str = "GET", body: dict | None = None, timeout: int = 15):
    data = None
    headers = {"Accept": "*/*"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
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
                "is_member_early", "created_at"}
    check(required <= post_cols, "posts 필수 컬럼", ", ".join(sorted(required - post_cols)) or "모두 존재")
    draft_cols = {r[1] for r in conn.execute("PRAGMA table_info(naver_drafts)")}
    check({"post_id", "seo_title", "keywords", "body_html", "body_text", "telegram_cta", "web_cta",
           "image_path"} <= draft_cols, "naver_drafts 필수 컬럼")
    user_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    check({"email", "name", "provider", "watchlist"} <= user_cols, "users 필수 컬럼")

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
        check("공식 원문 기반 실시간 속보" in html and "내 관심자산 속보 무료로 받기" in html,
              "메인 히어로·CTA 문구 포함")
        check("실시간 속보" in html and "공식 원문 보기" in html, "타임라인 섹션 마크업 포함")

        status, headers, raw = http(base + "/signup.html")
        check(status == 200 and "10초" in raw.decode("utf-8", "replace"), "GET /signup.html → 200", f"HTTP {status}")
        status, _, raw = http(base + "/signup")
        check(status == 200 and "카카오" in raw.decode("utf-8", "replace"), "GET /signup (기존 404 경로) → 200")

        status, headers, raw = http(base + "/admin-blog.html")
        check(status == 200 and "네이버 블로그 SEO 초안" in raw.decode("utf-8", "replace"),
              "GET /admin-blog.html → 200")

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

        status, _, raw = http(base + "/api/naver-drafts")
        drafts = json.loads(raw)
        check(status == 200 and drafts.get("count", 0) > 0, "GET /api/naver-drafts → 200", f"{drafts.get('count')}건")
        d0 = drafts["drafts"][0]
        check("t.me/Clare_PerpDEX" in d0["body_html"] and d0["seo_title"], "초안 응답: 본문·이미지 링크 포함")

        status, _, raw = http(base + "/api/calendar")
        cal = json.loads(raw)
        check(status == 200 and len(cal.get("events", [])) >= 1, "GET /api/calendar → 200", f"{len(cal.get('events', []))}건")

        email = f"verify-{int(time.time())}@claredex.test"
        status, _, raw = http(base + "/api/signup", "POST",
                              {"email": email, "name": "검증", "provider": "email", "watchlist": ["BTC", "ETH"]})
        signup = json.loads(raw)
        check(status == 201 and signup.get("ok") and signup["user"]["email"] == email,
              "POST /api/signup → 201", f"HTTP {status}")

        status, _, raw = http(base + "/api/watchlist", "POST",
                              {"email": email, "watchlist": ["BTC", "ETH", "나스닥", "NVDA"]})
        wl = json.loads(raw)
        check(status == 200 and wl.get("ok") and len(wl["user"]["watchlist"]) == 4,
              "POST /api/watchlist → 200 저장", f"HTTP {status}")

        status, _, raw = http(base + "/api/signup", "POST", {"email": email, "provider": "email", "watchlist": []})
        again = json.loads(raw)
        check(status == 201 and len(again["user"]["watchlist"]) == 4,
              "같은 이메일 재가입 시 관심자산 보존", str(again["user"]["watchlist"]))

        with db.session() as conn:
            row = conn.execute("SELECT watchlist FROM users WHERE email = ?", (email,)).fetchone()
            stored = json.loads(row[0]) if row else None
            conn.execute("DELETE FROM users WHERE email = ?", (email,))
            conn.commit()
        check(stored == ["BTC", "ETH", "나스닥", "NVDA"], "관심자산 SQLite 저장 확인", str(stored))

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
