"""ClareDEX v2 웹 서버.

public/ 정적 파일 + JSON API.
  GET  /                     메인
  GET  /signup(.html)        10초 가입 + 관심자산 온보딩
  GET  /admin-blog(.html)    네이버 블로그 SEO 초안 복사 도구
  GET  /api/ticker           실시간 지표 (실패 시 data/market_cache.json 폴백)
  GET  /api/posts            ?category= &tag= &q= &limit=
  GET  /api/naver-drafts     네이버 초안 목록
  GET  /api/calendar         KST 공식 일정
  POST /api/signup           {email, name, provider, watchlist}
  POST /api/watchlist        {email, watchlist}
"""
from __future__ import annotations

import argparse
import json
import re
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
    def _send_json(self, obj: object, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
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
        if path.suffix in (".html",):
            self.send_header("Cache-Control", "no-cache")
        else:
            self.send_header("Cache-Control", "public, max-age=600")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

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
        if route == "/api/naver-drafts":
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
        if route in ("/admin-blog", "/admin-blog/"):
            self._send_file(config.PUBLIC_DIR / "admin-blog.html")
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
        body = self._read_json_body()
        if body is None:
            self._send_json({"ok": False, "error": "JSON 본문이 필요합니다."}, 400)
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
