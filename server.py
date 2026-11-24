"""ClareDEX v2 웹 서버.

public/ 정적 파일 + JSON API.
  GET  /                     메인
  GET  /signup(.html)        10초 가입 + 관심자산 온보딩
  GET  /admin/, /admin       운영자 전용 네이버 블로그 SEO 초안 도구 (로그인 게이트)
  GET  /api/ticker           실시간 지표 (실패 시 data/market_cache.json 폴백)
  GET  /api/posts            ?category= &tag= &q= &limit=
  GET  /api/naver-drafts     네이버 초안 목록 (운영자 로그인 필요, 미로그인 401)
  GET  /api/calendar         KST 공식 일정
  GET  /api/admin/session    현재 운영자 세션 상태
  POST /api/signup           {email, name, provider, watchlist}
  POST /api/watchlist        {email, watchlist}
  POST /api/admin/login      {password} → 서명된 세션 쿠키 발급
  POST /api/admin/logout     세션 쿠키 폐기

운영자 인증
  · 비밀번호는 환경변수 ADMIN_PASSWORD (또는 .env) 에서 읽습니다.
  · 값이 없을 때만 개발용 기본 비밀번호(DEV_ADMIN_PASSWORD)로 동작하며, 기동 시 경고를 남깁니다.
  · 세션은 메모리에만 두는 무작위 토큰 + HttpOnly·SameSite=Lax 쿠키입니다(서버 재시작 시 모두 만료).
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import config

config.load_env()

import collector  # noqa: E402
import db  # noqa: E402

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
PROVIDERS = {"email", "kakao", "google", "naver"}

TICKER_TTL = 20.0
_ticker_lock = threading.Lock()
_ticker_cache: dict | None = None
_ticker_cache_at = 0.0

# ---------------------------------------------------------------- 운영자 인증
ADMIN_COOKIE = "claredex_admin"
ADMIN_SESSION_TTL = 12 * 3600  # 12시간
DEV_ADMIN_PASSWORD = "claredex-admin-2026"  # .env.example · README 에 문서화된 개발용 기본값
ADMIN_LOGIN_MAX_FAILS = 10  # 같은 IP 기준
ADMIN_LOGIN_WINDOW = 300.0  # 4xx 실패 누적 구간(초)

_admin_lock = threading.Lock()
_admin_sessions: dict[str, float] = {}  # token → 만료 시각(epoch)
_admin_login_fails: dict[str, list[float]] = {}  # client ip → 실패 시각 목록


def admin_password() -> str:
    """환경변수 ADMIN_PASSWORD 우선, 없으면 개발용 기본값."""
    return (os.environ.get("ADMIN_PASSWORD") or "").strip() or DEV_ADMIN_PASSWORD


def admin_password_is_default() -> bool:
    return not (os.environ.get("ADMIN_PASSWORD") or "").strip()


def create_admin_session() -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _admin_lock:
        for key, expires in list(_admin_sessions.items()):
            if expires <= now:
                _admin_sessions.pop(key, None)
        _admin_sessions[token] = now + ADMIN_SESSION_TTL
    return token


def drop_admin_session(token: str) -> None:
    if not token:
        return
    with _admin_lock:
        _admin_sessions.pop(token, None)


def admin_session_valid(token: str) -> bool:
    if not token:
        return False
    now = time.time()
    with _admin_lock:
        expires = _admin_sessions.get(token)
        if expires is None:
            return False
        if expires <= now:
            _admin_sessions.pop(token, None)
            return False
        return True


def _load_cache_file() -> dict | None:
    try:
        payload = json.loads(config.MARKET_CACHE_PATH.read_text(encoding="utf-8"))
        payload["stale"] = True
        payload["note"] = "실시간 조회가 지연되어 마지막으로 수집한 값을 표시합니다."
        return payload
    except Exception:
        return None


def get_ticker(force: bool = False) -> dict:
    global _ticker_cache, _ticker_cache_at
    with _ticker_lock:
        now = time.time()
        if not force and _ticker_cache and now - _ticker_cache_at < TICKER_TTL:
            return _ticker_cache
        try:
            payload = collector.collect_market()
            payload["served_from"] = "live"
            _ticker_cache = payload
            _ticker_cache_at = now
            try:
                collector.save_market_cache(payload)
            except Exception:
                pass
            return payload
        except Exception as exc:  # noqa: BLE001
            cached = _load_cache_file()
            if cached:
                cached["served_from"] = "cache"
                cached["error"] = str(exc)
                _ticker_cache = cached
                _ticker_cache_at = now - TICKER_TTL + 5
                return cached
            return {"ok": False, "served_from": "none", "items": [], "error": str(exc)}


def _post_json(post: dict) -> dict:
    out = dict(post)
    out["category_label"] = config.CATEGORIES.get(post["category"], {}).get("label", post["category"])
    out["image_url"] = f"{config.PUBLIC_BASE_URL}{post['image_path']}" if post.get("image_path") else None
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "ClareDEXv2/0.1"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ helpers
    def _send_json(self, obj: object, status: int = 200, cookies: list[str] | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for cookie in cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_file(self, path: Path, no_store: bool = False) -> None:
        if not path.is_file():
            self._send_json({"ok": False, "error": "not found"}, 404)
            return
        import mimetypes

        data = path.read_bytes()
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix == ".html":
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if no_store:
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Robots-Tag", "noindex, nofollow")
        elif path.suffix in (".html",):
            self.send_header("Cache-Control", "no-cache")
        else:
            self.send_header("Cache-Control", "public, max-age=600")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    # ------------------------------------------------------- 운영자 인증 헬퍼
    def _cookie_token(self) -> str:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == ADMIN_COOKIE:
                return value.strip()
        return ""

    def _is_admin(self) -> bool:
        return admin_session_valid(self._cookie_token())

    def _admin_cookie(self, token: str | None) -> str:
        parts = [f"{ADMIN_COOKIE}={token}" if token else f"{ADMIN_COOKIE}=",
                 "Path=/", "HttpOnly", "SameSite=Lax"]
        parts.append(f"Max-Age={ADMIN_SESSION_TTL}" if token else "Max-Age=0")
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        if proto == "https":
            parts.append("Secure")
        return "; ".join(parts)

    def _login_blocked(self) -> bool:
        ip = self.client_address[0]
        now = time.time()
        with _admin_lock:
            hits = [t for t in _admin_login_fails.get(ip, []) if now - t < ADMIN_LOGIN_WINDOW]
            if hits:
                _admin_login_fails[ip] = hits
            else:
                _admin_login_fails.pop(ip, None)
            return len(hits) >= ADMIN_LOGIN_MAX_FAILS

    def _record_login_fail(self) -> None:
        ip = self.client_address[0]
        now = time.time()
        with _admin_lock:
            hits = [t for t in _admin_login_fails.get(ip, []) if now - t < ADMIN_LOGIN_WINDOW]
            hits.append(now)
            _admin_login_fails[ip] = hits

    def _clear_login_fails(self) -> None:
        with _admin_lock:
            _admin_login_fails.pop(self.client_address[0], None)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        print(f"[web] {self.address_string()} {fmt % args}")

    def _read_json_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 200_000:
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------ GET
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = unquote(parsed.path)
        qs = parse_qs(parsed.query)

        if route == "/api/ticker":
            self._send_json(get_ticker(), 200)
            return
        if route == "/api/posts":
            category = (qs.get("category") or [None])[0]
            tag = (qs.get("tag") or [None])[0]
            q = (qs.get("q") or [None])[0]
            try:
                limit = min(int((qs.get("limit") or ["50"])[0]), 200)
            except ValueError:
                limit = 50
            with db.session() as conn:
                posts = db.list_posts(conn, category=category, tag=tag, q=q, limit=limit)
            self._send_json({"ok": True, "count": len(posts), "posts": [_post_json(p) for p in posts]})
            return
        if route == "/api/admin/session":
            self._send_json({"ok": True, "authenticated": self._is_admin(),
                             "expires_in": ADMIN_SESSION_TTL}, 200)
            return
        if route == "/api/naver-drafts":
            if not self._is_admin():
                self._send_json({"ok": False, "error": "운영자 로그인이 필요합니다.", "login": "/admin/"}, 401)
                return
            with db.session() as conn:
                drafts = db.list_drafts(conn)
            for d in drafts:
                d["image_url"] = f"{config.PUBLIC_BASE_URL}{d['image_path']}" if d.get("image_path") else None
                d["image_local"] = d.get("image_path")
                d["telegram_url"] = config.TELEGRAM_URL
                d["web_url"] = config.WEB_URL
            self._send_json({"ok": True, "count": len(drafts), "drafts": drafts})
            return
        if route == "/api/calendar":
            try:
                data = json.loads(config.CALENDAR_PATH.read_text(encoding="utf-8"))
            except Exception:
                data = {"events": []}
            self._send_json({"ok": True, **data})
            return
        if route == "/api/health":
            with db.session() as conn:
                self._send_json({"ok": True, "posts": db.count_posts(conn), "drafts": db.count_drafts(conn)})
            return

        # 정적 파일
        if route in ("/", ""):
            self._send_file(config.PUBLIC_DIR / "index.html")
            return
        if route in ("/signup", "/signup/"):
            self._send_file(config.PUBLIC_DIR / "signup.html")
            return
        if route in ("/admin", "/admin/", "/admin/index.html"):
            self._send_file(config.PUBLIC_DIR / "admin" / "index.html", no_store=True)
            return
        if route == "/favicon.ico":
            self._send_json({"ok": False, "error": "not found"}, 404)
            return

        target = (config.PUBLIC_DIR / route.lstrip("/")).resolve()
        try:
            target.relative_to(config.PUBLIC_DIR.resolve())
        except ValueError:
            self._send_json({"ok": False, "error": "forbidden"}, 403)
            return
        if target.is_dir():
            target = target / "index.html"
        self._send_file(target)

    # ------------------------------------------------------------ POST
    def do_POST(self) -> None:  # noqa: N802
        route = unquote(urlparse(self.path).path)

        if route == "/api/admin/logout":
            drop_admin_session(self._cookie_token())
            self._send_json({"ok": True, "authenticated": False}, 200, cookies=[self._admin_cookie(None)])
            return

        body = self._read_json_body()
        if body is None:
            self._send_json({"ok": False, "error": "JSON 본문이 필요합니다."}, 400)
            return

        if route == "/api/admin/login":
            if self._login_blocked():
                self._send_json({"ok": False,
                                 "error": "로그인 시도가 너무 많습니다. 잠시 후 다시 시도해 주세요."}, 429)
                return
            password = str(body.get("password") or "")
            if not password or not hmac.compare_digest(password, admin_password()):
                self._record_login_fail()
                self._send_json({"ok": False, "error": "비밀번호가 올바르지 않습니다."}, 401)
                return
            self._clear_login_fails()
            token = create_admin_session()
            self._send_json({"ok": True, "authenticated": True, "expires_in": ADMIN_SESSION_TTL},
                            200, cookies=[self._admin_cookie(token)])
            return

        if route == "/api/signup":
            email = str(body.get("email", "")).strip().lower()
            name = str(body.get("name", "")).strip() or None
            provider = str(body.get("provider", "email")).strip().lower() or "email"
            watchlist = body.get("watchlist") or []
            if not EMAIL_RE.match(email):
                self._send_json({"ok": False, "error": "이메일 형식을 확인해 주세요."}, 400)
                return
            if provider not in PROVIDERS:
                self._send_json({"ok": False, "error": f"지원하지 않는 provider 입니다: {provider}"}, 400)
                return
            if not isinstance(watchlist, list):
                self._send_json({"ok": False, "error": "watchlist 는 배열이어야 합니다."}, 400)
                return
            unknown = [a for a in watchlist if a not in config.WATCHLIST_ASSETS]
            if unknown:
                self._send_json(
                    {"ok": False, "error": "지원하지 않는 관심자산입니다.", "unknown": unknown,
                     "allowed": config.WATCHLIST_ASSETS},
                    400,
                )
                return
            with db.session() as conn:
                existing = db.get_user(conn, email)
                # 이미 가입한 이메일을 다시 진행할 때 빈 목록으로 기존 관심자산을 지우지 않는다.
                effective = watchlist if (watchlist or not existing) else None
                user = db.upsert_user(conn, email=email, name=name, provider=provider, watchlist=effective)
            self._send_json({"ok": True, "user": user, "next": "/signup?step=watchlist"}, 201)
            return

        if route == "/api/watchlist":
            email = str(body.get("email", "")).strip().lower()
            watchlist = body.get("watchlist")
            if not EMAIL_RE.match(email):
                self._send_json({"ok": False, "error": "이메일 형식을 확인해 주세요."}, 400)
                return
            if not isinstance(watchlist, list):
                self._send_json({"ok": False, "error": "watchlist 는 배열이어야 합니다."}, 400)
                return
            unknown = [a for a in watchlist if a not in config.WATCHLIST_ASSETS]
            if unknown:
                self._send_json({"ok": False, "error": "지원하지 않는 관심자산입니다.", "unknown": unknown}, 400)
                return
            with db.session() as conn:
                if not db.get_user(conn, email):
                    self._send_json({"ok": False, "error": "가입된 이메일이 아닙니다."}, 404)
                    return
                user = db.set_watchlist(conn, email, watchlist)
            self._send_json({"ok": True, "user": user})
            return

        self._send_json({"ok": False, "error": "not found"}, 404)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_PUT(self) -> None:  # noqa: N802
        self._send_json({"ok": False, "error": "method not allowed"}, 405)

    do_PATCH = do_PUT
    do_DELETE = do_PUT


def serve(port: int = 8000, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    if admin_password_is_default():
        print("[admin] ⚠ ADMIN_PASSWORD 가 설정되지 않아 개발용 기본 비밀번호를 사용합니다. "
              "운영 배포 전에 .env 의 ADMIN_PASSWORD 를 반드시 교체하세요.")
    db.init_db()
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def main() -> int:
    parser = argparse.ArgumentParser(description="ClareDEX v2 웹 서버")
    parser.add_argument("--port", type=int, default=int(__import__("os").environ.get("PORT", 8000)))
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    httpd = serve(port=args.port, host=args.host)
    print(f"ClareDEX v2 · http://localhost:{args.port}  (Ctrl+C 로 종료)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
