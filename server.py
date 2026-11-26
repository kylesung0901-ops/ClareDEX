"""ClareDEX v2 웹 서버.

public/ 정적 파일 + JSON API.
  GET  /                     메인
  GET  /signup(.html)        10초 가입 + 관심자산 온보딩
  GET  /admin/, /admin       운영자 전용 관리자 콘솔 (로그인 게이트 · KPI · 회원 · 콘텐츠 · 수집기 · 보안)
  GET  /api/ticker           실시간 지표 (실패 시 data/market_cache.json 폴백)
  GET  /api/posts            ?category= &tag= &q= &limit=   (공개 글만)
  GET  /api/calendar         KST 공식 일정
  GET  /api/auth/channels    본인인증 발송 채널 상태 (SMTP/Resend/SMS 설정 여부·샌드박스 여부)
  POST /api/auth/send-email-otp   {email}            이메일 인증번호 발송
  POST /api/auth/verify-email-otp {email, code}      이메일 인증번호 확인
  POST /api/auth/send-sms-otp     {email, phone}     휴대폰 인증번호 발송 (이메일 인증 선행)
  POST /api/auth/verify-sms-otp   {email, phone, code}  휴대폰 인증번호 확인
  POST /api/signup           {email, name, phone, marketing_agreed, watchlist}  (이메일+휴대폰 인증 필수)
  POST /api/watchlist        {email, watchlist}

운영자 API (모두 세션 쿠키 필요 · 미로그인 401)
  GET  /api/admin/session            현재 운영자 세션 상태
  GET  /api/admin/overview           KPI · 14일 가입 추이 · 분포 · 최근 가입/글 · 수집기 상태
  GET  /api/admin/users              ?q= &provider= &limit= &offset=   회원 목록
  GET  /api/admin/posts              ?category= &source= &status= &q= &limit= &offset=   글 목록
  GET  /api/admin/collector          수집기 상태 · 최근 실행 결과 · 로그
  GET  /api/admin/security           비밀번호 저장 방식 · 보안 설정 · 최근 감사 로그
  GET  /api/naver-drafts             네이버 초안 목록
  POST /api/admin/login              {password} → 세션 쿠키 발급
  POST /api/admin/logout             세션 쿠키 폐기
  POST /api/admin/posts/status       {id, status}  공개↔숨김
  POST /api/admin/posts/delete       {id}          글 삭제(연결 초안 포함)
  POST /api/admin/collect            {trigger}     collector.py → naver_drafts.py 백그라운드 실행
  POST /api/admin/change-password    {current_password, new_password, confirm_password}

운영자 인증
  · 비밀번호 해시는 data/admin_auth.json (컨테이너 /app/data/admin_auth.json · 마운트 볼륨) 에
    PBKDF2-HMAC-SHA256 + salt 로 저장됩니다. 파일이 있으면 그것이 최우선이고, 없을 때만
    환경변수 ADMIN_PASSWORD (또는 .env), 그것도 없을 때만 개발용 기본값을 씁니다.
  · /api/admin/change-password 는 해시 파일을 원자적으로 교체하므로 컨테이너를 재시작해도 유지됩니다.
  · 세션은 메모리에만 두는 무작위 토큰 + HttpOnly·SameSite=Lax 쿠키입니다(서버 재시작 시 모두 만료).
  · 상태를 바꾸는 요청은 같은 출처(Origin)에서 온 것만 받습니다.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import config

config.load_env()

import authflow  # noqa: E402
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


# 비밀번호 해시 파일 (기본: data/admin_auth.json — docker 볼륨으로 마운트되어 재시작에도 유지)
ADMIN_AUTH_PATH = Path(
    os.environ.get("CLAREDEX_ADMIN_AUTH_FILE") or (config.DATA_DIR / "admin_auth.json")
)
ADMIN_AUDIT_PATH = Path(
    os.environ.get("CLAREDEX_ADMIN_AUDIT_FILE") or (config.DATA_DIR / "admin_audit.jsonl")
)
ADMIN_HASH_ALGO = "pbkdf2_sha256"
ADMIN_HASH_ITERATIONS = 240_000
ADMIN_PASSWORD_MIN = 10
ADMIN_PASSWORD_MAX = 200
ADMIN_AUDIT_KEEP = 400

_auth_file_lock = threading.Lock()
_auth_cache: dict | None = None
_auth_cache_mtime: float | None = None
_audit_lock = threading.Lock()


def _fallback_admin_password() -> str:
    """해시 파일이 아직 없을 때 쓰는 초기 비밀번호 (환경변수 → 개발 기본값)."""
    return (os.environ.get("ADMIN_PASSWORD") or "").strip() or DEV_ADMIN_PASSWORD


def admin_password() -> str:
    """초기(폴백) 비밀번호. 해시 파일이 있으면 그 값이 우선이므로 인증에는 쓰지 않는다."""
    return _fallback_admin_password()


def admin_password_is_default() -> bool:
    return not (os.environ.get("ADMIN_PASSWORD") or "").strip()


def _read_admin_auth(force: bool = False) -> dict | None:
    """data/admin_auth.json 을 읽는다. 파일이 없거나 형식이 잘못되면 None."""
    global _auth_cache, _auth_cache_mtime
    with _auth_file_lock:
        try:
            mtime = ADMIN_AUTH_PATH.stat().st_mtime
        except OSError:
            _auth_cache, _auth_cache_mtime = None, None
            return None
        if not force and _auth_cache is not None and _auth_cache_mtime == mtime:
            return _auth_cache
        try:
            raw = json.loads(ADMIN_AUTH_PATH.read_text(encoding="utf-8"))
            if (not isinstance(raw, dict) or raw.get("algorithm") != ADMIN_HASH_ALGO
                    or not raw.get("salt") or not raw.get("hash")):
                return None
            int(raw.get("iterations") or ADMIN_HASH_ITERATIONS)
        except Exception:  # noqa: BLE001
            return None
        _auth_cache, _auth_cache_mtime = raw, mtime
        return raw


def admin_password_source() -> str:
    """file(변경된 해시 파일) · env(.env/환경변수) · default(개발용)."""
    if _read_admin_auth() is not None:
        return "file"
    return "env" if not admin_password_is_default() else "default"


def admin_auth_info() -> dict:
    record = _read_admin_auth()
    return {
        "storage": admin_password_source(),
        "path": str(ADMIN_AUTH_PATH),
        "algorithm": ADMIN_HASH_ALGO,
        "iterations": int((record or {}).get("iterations") or ADMIN_HASH_ITERATIONS),
        "updated_at": (record or {}).get("updated_at"),
        "updated_by": (record or {}).get("updated_by"),
        "min_length": ADMIN_PASSWORD_MIN,
    }


def verify_admin_password(password: str) -> bool:
    """해시 파일 우선, 없으면 환경변수/개발 기본값과 비교 (타이밍 세이프)."""
    if not password:
        return False
    record = _read_admin_auth()
    if record is not None:
        try:
            salt = base64.b64decode(record["salt"])
            expected = base64.b64decode(record["hash"])
            iterations = int(record.get("iterations") or ADMIN_HASH_ITERATIONS)
            digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                                        iterations, dklen=len(expected))
            return hmac.compare_digest(digest, expected)
        except Exception:  # noqa: BLE001
            return False
    # 비밀번호에 한글이 올 수 있으므로 바이트로 비교한다.
    return hmac.compare_digest(password.encode("utf-8"), _fallback_admin_password().encode("utf-8"))


def _make_auth_record(password: str, updated_by: str) -> dict:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                                 ADMIN_HASH_ITERATIONS, dklen=32)
    return {
        "version": 1,
        "algorithm": ADMIN_HASH_ALGO,
        "iterations": ADMIN_HASH_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": base64.b64encode(digest).decode("ascii"),
        "updated_at": config.iso_kst(),
        "updated_by": updated_by or "admin",
    }


def set_admin_password(password: str, updated_by: str = "admin") -> dict:
    """해시 파일을 원자적으로 교체하고 메모리 캐시를 즉시 갱신한다."""
    global _auth_cache, _auth_cache_mtime
    record = _make_auth_record(password, updated_by)
    ADMIN_AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = ADMIN_AUTH_PATH.with_name(ADMIN_AUTH_PATH.name + ".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, ADMIN_AUTH_PATH)
    with _auth_file_lock:
        _auth_cache = record
        try:
            _auth_cache_mtime = ADMIN_AUTH_PATH.stat().st_mtime
        except OSError:
            _auth_cache_mtime = None
    return record


def audit(action: str, detail: str = "", ip: str = "") -> None:
    """운영자 조치 기록 (data/admin_audit.jsonl · 최근 400줄 유지)."""
    entry = {"at": config.iso_kst(), "action": action, "detail": detail, "ip": ip}
    line = json.dumps(entry, ensure_ascii=False)
    with _audit_lock:
        try:
            ADMIN_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
            with ADMIN_AUDIT_PATH.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            if ADMIN_AUDIT_PATH.stat().st_size > 400_000:
                lines = ADMIN_AUDIT_PATH.read_text(encoding="utf-8").splitlines()[-ADMIN_AUDIT_KEEP:]
                ADMIN_AUDIT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            print(f"[admin] 감사 로그 기록 실패: {exc}")


def read_audit(n: int = 20) -> list[dict]:
    try:
        lines = ADMIN_AUDIT_PATH.read_text(encoding="utf-8").splitlines()[-int(n):]
    except Exception:  # noqa: BLE001
        return []
    out = []
    for line in lines:
        try:
            item = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(item, dict):
            out.append(item)
    return list(reversed(out))


# ------------------------------------------------------- 수집기 백그라운드 실행
COLLECT_STATE_PATH = config.DATA_DIR / "collector_state.json"
COLLECT_MIN_INTERVAL = 30.0     # 연속 클릭 방지 (초)
COLLECT_STEP_TIMEOUT = 420      # 단계별 최대 실행 시간 (초)
COLLECT_LOG_KEEP = 80

_collect_lock = threading.Lock()
_collect_job: dict = {"running": False, "started_at": None, "trigger": None}

COLLECT_STEPS = (
    ("collector.py",
     "import json, os, collector\n"
     "result = collector.run(max_new=int(os.environ.get('CLAREDEX_MAX_NEW_PER_RUN', '1')))\n"
     "print('__RESULT__' + json.dumps(result, ensure_ascii=False, default=str))\n"),
    ("naver_drafts.py", "import json, naver_drafts; "
                        "print('__RESULT__' + json.dumps(naver_drafts.run(), ensure_ascii=False, default=str))"),
    ("naver_publish.py",
     "import json, os, naver_publish\n"
     "result = naver_publish.run(limit=int(os.environ.get('CLAREDEX_NAVER_PER_RUN', '1')))\n"
     "print('__RESULT__' + json.dumps(result, ensure_ascii=False, default=str))\n"),
)


def collector_last_run() -> dict | None:
    try:
        data = json.loads(COLLECT_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def collector_state() -> dict:
    with _collect_lock:
        job = dict(_collect_job)
    last = collector_last_run()
    with db.session() as conn:
        stats = db.post_stats(conn)
        naver = db.naver_publish_stats(conn)
    return {
        "ok": True,
        "running": bool(job.get("running")),
        "job": job,
        "last": last,
        "latest_post_at": stats.get("latest"),
        "naver": naver,
        "naver_channels": {"email": authflow.email_mode(), "sms": authflow.sms_mode()},
        "generated_at": config.iso_kst(),
    }


def _run_collect_job(trigger: str) -> None:
    global _collect_job
    started = time.time()
    steps: list[dict] = []
    log_lines: list[str] = []
    ok = True
    result: dict = {}
    for name, code in COLLECT_STEPS:
        step_started = time.time()
        parsed = None
        try:
            proc = subprocess.run(
                [sys.executable, "-c", code],
                cwd=str(config.BASE_DIR), capture_output=True, text=True,
                timeout=COLLECT_STEP_TIMEOUT, env=dict(os.environ),
            )
            stdout = proc.stdout or ""
            for line in stdout.splitlines():
                if line.startswith("__RESULT__"):
                    try:
                        parsed = json.loads(line[len("__RESULT__"):])
                    except Exception:  # noqa: BLE001
                        parsed = None
                elif line.strip():
                    log_lines.append(f"[{name}] {line}")
            if proc.stderr:
                for line in proc.stderr.splitlines()[-20:]:
                    if line.strip():
                        log_lines.append(f"[{name}!] {line}")
            step = {
                "name": name,
                "ok": proc.returncode == 0,
                "exit_code": proc.returncode,
                "seconds": round(time.time() - step_started, 1),
                "result": parsed,
            }
        except subprocess.TimeoutExpired:
            step = {"name": name, "ok": False, "exit_code": None,
                    "seconds": round(time.time() - step_started, 1), "result": None,
                    "error": f"{COLLECT_STEP_TIMEOUT}초 시간 초과"}
            log_lines.append(f"[{name}] 시간 초과로 중단했습니다.")
        except Exception as exc:  # noqa: BLE001
            step = {"name": name, "ok": False, "exit_code": None,
                    "seconds": round(time.time() - step_started, 1), "result": None,
                    "error": str(exc)}
            log_lines.append(f"[{name}] 실행 실패: {exc}")
        steps.append(step)
        if not step["ok"]:
            ok = False
            break
        if parsed:
            result[name] = parsed

    collector_result = (steps[0].get("result") or {}) if steps else {}
    naver_result = (result.get("naver_publish.py") or {})
    state = {
        "last_run_at": config.iso_kst(),
        "started_at": job_started_at(_collect_job.get("started_ts")),
        "trigger": trigger,
        "ok": ok,
        "duration_s": round(time.time() - started, 1),
        "steps": steps,
        "source_status": (collector_result or {}).get("source_status") or {},
        "new_posts": (collector_result or {}).get("new"),
        "total_posts": (collector_result or {}).get("total"),
        "deferred_posts": (collector_result or {}).get("deferred"),
        "drafts_created": ((result.get("naver_drafts.py") or {}).get("created")),
        "drafts_total": ((result.get("naver_drafts.py") or {}).get("total")),
        "naver": naver_result or None,
        "log": log_lines[-COLLECT_LOG_KEEP:],
    }
    try:
        COLLECT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        COLLECT_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                                      encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"[collect] 상태 저장 실패: {exc}")
    with _collect_lock:
        _collect_job = {"running": False, "started_at": _collect_job.get("started_at"),
                        "started_ts": _collect_job.get("started_ts"), "trigger": trigger,
                        "finished_at": state["last_run_at"], "ok": ok}
    print(f"[collect] 완료 · ok={ok} · {state['duration_s']}s · 신규 {state['new_posts']}건")


def job_started_at(ts: float | None) -> str | None:
    if not ts:
        return None
    from datetime import datetime
    return datetime.fromtimestamp(ts, tz=config.KST).replace(microsecond=0).isoformat()


def start_collect(trigger: str = "admin") -> tuple[bool, str]:
    global _collect_job
    now = time.time()
    with _collect_lock:
        if _collect_job.get("running"):
            return False, "이미 수집이 실행 중입니다. 끝난 뒤 다시 시도해 주세요."
        last = collector_last_run() or {}
        try:
            last_ts = config.parse_kst(last.get("last_run_at")).timestamp() if last.get("last_run_at") else 0
        except Exception:  # noqa: BLE001
            last_ts = 0
        if now - last_ts < COLLECT_MIN_INTERVAL:
            wait = int(COLLECT_MIN_INTERVAL - (now - last_ts))
            return False, f"방금 전 실행했습니다. {wait}초 뒤에 다시 시도해 주세요."
        _collect_job = {"running": True, "started_at": config.iso_kst(), "started_ts": now,
                        "trigger": trigger}
    threading.Thread(target=_run_collect_job, args=(trigger,), daemon=True).start()
    return True, "수집을 시작했습니다."


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


def drop_other_admin_sessions(keep_token: str) -> int:
    """비밀번호 변경 시 현재 세션만 남기고 나머지를 폐기한다."""
    with _admin_lock:
        drop = [t for t in _admin_sessions if t != keep_token]
        for token in drop:
            _admin_sessions.pop(token, None)
        return len(drop)


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


def _admin_post_json(post: dict) -> dict:
    out = _post_json(post)
    out["status"] = post.get("status") or "published"
    return out


def build_overview() -> dict:
    """관리자 대시보드 1화면 데이터 (KPI · 추이 · 분포 · 최근 활동 · 수집기)."""
    today = config.now_kst().date().isoformat()
    with db.session() as conn:
        users = db.user_stats(conn, 14)
        posts = db.post_stats(conn)
        cats = db.category_counts(conn)
        sources = db.source_counts(conn, 8)
        recent_users = db.recent_users(conn, 5)
        recent_posts = db.list_posts(conn, limit=5, include_hidden=True)
        drafts_total = db.count_drafts(conn)
        drafts_today = db.count_drafts(conn, since_day=today)
        providers = db.provider_counts(conn)
        posts_today = db.count_posts_since(conn, today)
    state = collector_state()
    last = state.get("last") or {}
    categories = [
        {"key": c["key"], "count": c["count"],
         "label": config.CATEGORIES.get(c["key"], {}).get("label", c["key"]),
         "color": config.CATEGORIES.get(c["key"], {}).get("color", "#6b6b6b")}
        for c in cats
    ]
    return {
        "ok": True,
        "generated_at": config.iso_kst(),
        "kpis": {
            "users_total": users["total"],
            "users_today": users["today"],
            "users_7d": users["last7"],
            "users_30d": users["last30"],
            "posts_total": posts["total"],
            "posts_published": posts["published"],
            "posts_hidden": posts["hidden"],
            "posts_today": posts_today,
            "drafts_total": drafts_total,
            "drafts_today": drafts_today,
            "collector": {
                "running": bool(state.get("running")),
                "last_run_at": last.get("last_run_at"),
                "ok": last.get("ok"),
                "new_posts": last.get("new_posts"),
                "duration_s": last.get("duration_s"),
                "trigger": last.get("trigger"),
                "latest_post_at": state.get("latest_post_at"),
            },
        },
        "trend": users["series"],
        "categories": categories,
        "sources": sources,
        "providers": providers,
        "recent_users": recent_users,
        "recent_posts": [_admin_post_json(p) for p in recent_posts],
        "collector": {k: v for k, v in state.items() if k != "ok"},
    }


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

    # ----------------------------------------------------------- 운영자 인증
    def _cookie_token(self) -> str:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == ADMIN_COOKIE:
                return value.strip()
        return ""

    def _is_admin(self) -> bool:
        return admin_session_valid(self._cookie_token())

    def _require_admin(self) -> bool:
        if self._is_admin():
            return True
        self._send_json({"ok": False, "error": "운영자 로그인이 필요합니다.", "login": "/admin/"}, 401)
        return False

    def _cross_origin(self) -> bool:
        """브라우저가 붙인 Origin 이 현재 Host 와 다르면 상태 변경 요청을 거부한다."""
        origin = (self.headers.get("Origin") or "").strip()
        if not origin or origin == "null":
            return bool(origin)
        try:
            return urlparse(origin).netloc != (self.headers.get("Host") or "").strip()
        except Exception:  # noqa: BLE001
            return True

    def _admin_cookie(self, token: str | None) -> str:
        parts = [f"{ADMIN_COOKIE}={token}" if token else f"{ADMIN_COOKIE}=",
                 "Path=/", "HttpOnly", "SameSite=Lax"]
        parts.append(f"Max-Age={ADMIN_SESSION_TTL}" if token else "Max-Age=0")
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        if proto == "https":
            parts.append("Secure")
        return "; ".join(parts)

    def _client_ip(self) -> str:
        """리버스 프록시 뒤에서도 운영자 한 명이 전체를 잠그지 않도록 XFF 첫 홉을 우선한다."""
        forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        return forwarded or self.client_address[0]

    def _login_blocked(self) -> bool:
        ip = self._client_ip()
        now = time.time()
        with _admin_lock:
            hits = [t for t in _admin_login_fails.get(ip, []) if now - t < ADMIN_LOGIN_WINDOW]
            if hits:
                _admin_login_fails[ip] = hits
            else:
                _admin_login_fails.pop(ip, None)
            return len(hits) >= ADMIN_LOGIN_MAX_FAILS

    def _record_login_fail(self) -> None:
        ip = self._client_ip()
        now = time.time()
        with _admin_lock:
            hits = [t for t in _admin_login_fails.get(ip, []) if now - t < ADMIN_LOGIN_WINDOW]
            hits.append(now)
            _admin_login_fails[ip] = hits

    def _clear_login_fails(self) -> None:
        with _admin_lock:
            _admin_login_fails.pop(self._client_ip(), None)

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

    def _handle_auth_otp(self, route: str, body: dict) -> None:
        """이메일·휴대폰 인증번호 발송/확인. 발송 채널이 없으면 숨기지 않고 그대로 알린다."""
        email = authflow.normalize_email(body.get("email"))
        phone = body.get("phone")
        code = body.get("code")
        if route == "/api/auth/send-email-otp":
            result = authflow.issue(authflow.CHANNEL_EMAIL, email)
        elif route == "/api/auth/verify-email-otp":
            result = authflow.verify(authflow.CHANNEL_EMAIL, email, code)
        elif route == "/api/auth/send-sms-otp":
            result = authflow.issue(authflow.CHANNEL_SMS, phone, email=email)
        else:
            result = authflow.verify(authflow.CHANNEL_SMS, phone, code, email=email)

        if result.get("ok"):
            if route.endswith("verify-email-otp") or route.endswith("verify-sms-otp"):
                with db.session() as conn:
                    result["verification"] = authflow.verification_state(conn, email)
            self._send_json(result, 200)
            return

        code_name = result.get("code")
        status = 429 if code_name in ("cooldown", "rate_limited", "too_many") else 400
        if code_name == "delivery_failed":
            status = 502
        self._send_json(result, status)

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
            authenticated = self._is_admin()
            payload = {"ok": True, "authenticated": authenticated, "expires_in": ADMIN_SESSION_TTL}
            if authenticated:
                info = admin_auth_info()
                payload["password_source"] = info["storage"]
                payload["password_updated_at"] = info["updated_at"]
            self._send_json(payload, 200)
            return

        # -------------------------------------------------- 운영자 콘솔 API
        if route == "/api/admin/overview":
            if not self._require_admin():
                return
            self._send_json(build_overview())
            return
        if route == "/api/admin/users":
            if not self._require_admin():
                return
            q = (qs.get("q") or [""])[0].strip()
            provider = (qs.get("provider") or ["all"])[0]
            status = (qs.get("status") or ["all"])[0]
            try:
                limit = max(1, min(int((qs.get("limit") or ["50"])[0]), 500))
                offset = max(0, int((qs.get("offset") or ["0"])[0]))
            except ValueError:
                limit, offset = 50, 0
            with db.session() as conn:
                items = db.list_users(conn, q=q or None, provider=provider, status=status,
                                      limit=limit, offset=offset)
                total = db.count_users(conn, q=q or None, provider=provider, status=status)
                providers = db.provider_counts(conn)
            self._send_json({"ok": True, "total": total, "count": len(items), "limit": limit,
                             "offset": offset, "items": items, "providers": providers})
            return
        if route == "/api/admin/posts":
            if not self._require_admin():
                return
            category = (qs.get("category") or ["all"])[0]
            source = (qs.get("source") or [""])[0].strip()
            status = (qs.get("status") or ["all"])[0]
            qtext = (qs.get("q") or [""])[0].strip()
            try:
                limit = max(1, min(int((qs.get("limit") or ["40"])[0]), 200))
                offset = max(0, int((qs.get("offset") or ["0"])[0]))
            except ValueError:
                limit, offset = 40, 0
            status_filter = status if status in db.POST_STATUSES else None
            with db.session() as conn:
                items = db.list_posts(conn, category=category, q=qtext or None, source=source or None,
                                      limit=limit, offset=offset, include_hidden=True, status=status_filter)
                total = db.count_posts_matching(conn, category=category, q=qtext or None,
                                                source=source or None, status=status_filter)
                sources = db.list_sources(conn)
                cats = db.category_counts(conn)
                stats = db.post_stats(conn)
            self._send_json({
                "ok": True, "total": total, "count": len(items), "limit": limit, "offset": offset,
                "items": [_admin_post_json(p) for p in items], "sources": sources,
                "categories": [{"key": c["key"], "count": c["count"],
                                "label": config.CATEGORIES.get(c["key"], {}).get("label", c["key"])}
                               for c in cats],
                "stats": stats,
            })
            return
        if route == "/api/admin/collector":
            if not self._require_admin():
                return
            self._send_json(collector_state())
            return
        if route == "/api/admin/security":
            if not self._require_admin():
                return
            info = admin_auth_info()
            self._send_json({
                "ok": True,
                "auth": info,
                "session_ttl": ADMIN_SESSION_TTL,
                "session_count": len(_admin_sessions),
                "server_time": config.iso_kst(),
                "public_base_url": config.PUBLIC_BASE_URL,
                "audit": read_audit(20),
                "policy": {
                    "login_max_fails": ADMIN_LOGIN_MAX_FAILS,
                    "login_window": ADMIN_LOGIN_WINDOW,
                    "cookie": "HttpOnly · SameSite=Lax" +
                              (" · Secure" if (self.headers.get("X-Forwarded-Proto") or "").startswith("https") else ""),
                    "noindex": True,
                    "api_auth": "모든 /api/admin/* · /api/naver-drafts 는 미로그인 401",
                    "hash": f"{info['algorithm']} · salt · {info['iterations']:,}회",
                },
            })
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
        if route == "/api/auth/channels":
            self._send_json({"ok": True, **authflow.delivery_status()})
            return
        if route == "/api/health":
            with db.session() as conn:
                self._send_json({"ok": True, "posts": db.count_posts(conn),
                                 "drafts": db.count_drafts(conn),
                                 "users": db.count_users(conn),
                                 "time": config.iso_kst()})
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
        if route.startswith("/admin/"):
            # 관리자 콘솔 전용 CSS/JS (noindex · no-store)
            admin_root = (config.PUBLIC_DIR / "admin").resolve()
            target = (admin_root / route[len("/admin/"):]).resolve()
            try:
                target.relative_to(admin_root)
            except ValueError:
                self._send_json({"ok": False, "error": "forbidden"}, 403)
                return
            if target.is_dir():
                target = target / "index.html"
            self._send_file(target, no_store=True)
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

        if route.startswith("/api/admin") and self._cross_origin():
            self._send_json({"ok": False, "error": "cross-origin 요청은 허용하지 않습니다."}, 403)
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
            if not password or not verify_admin_password(password):
                self._record_login_fail()
                audit("login_fail", "비밀번호 불일치", self._client_ip())
                self._send_json({"ok": False, "error": "비밀번호가 올바르지 않습니다."}, 401)
                return
            self._clear_login_fails()
            token = create_admin_session()
            audit("login", f"세션 발급 · {admin_password_source()}", self._client_ip())
            self._send_json({"ok": True, "authenticated": True, "expires_in": ADMIN_SESSION_TTL},
                            200, cookies=[self._admin_cookie(token)])
            return

        if route == "/api/admin/change-password":
            if not self._require_admin():
                return
            if self._login_blocked():
                self._send_json({"ok": False, "error": "시도가 너무 많습니다. 잠시 후 다시 시도해 주세요."}, 429)
                return
            current = str(body.get("current_password") or "")
            new = str(body.get("new_password") or "")
            confirm = str(body.get("confirm_password") or "")
            if not current or not new or not confirm:
                self._send_json({"ok": False, "error": "현재 비밀번호·새 비밀번호·확인을 모두 입력해 주세요."}, 400)
                return
            if not verify_admin_password(current):
                self._record_login_fail()
                audit("password_change_fail", "현재 비밀번호 불일치", self._client_ip())
                self._send_json({"ok": False, "error": "현재 비밀번호가 올바르지 않습니다."}, 401)
                return
            if new != confirm:
                self._send_json({"ok": False, "error": "새 비밀번호와 확인이 일치하지 않습니다."}, 400)
                return
            if len(new) < ADMIN_PASSWORD_MIN:
                self._send_json({"ok": False,
                                 "error": f"새 비밀번호는 {ADMIN_PASSWORD_MIN}자 이상이어야 합니다."}, 400)
                return
            if len(new) > ADMIN_PASSWORD_MAX:
                self._send_json({"ok": False,
                                 "error": f"새 비밀번호는 {ADMIN_PASSWORD_MAX}자 이하여야 합니다."}, 400)
                return
            if hmac.compare_digest(new.encode("utf-8"), current.encode("utf-8")):
                self._send_json({"ok": False, "error": "현재 비밀번호와 다른 비밀번호를 입력해 주세요."}, 400)
                return
            try:
                record = set_admin_password(new, updated_by=f"admin@{self._client_ip()}")
            except Exception as exc:  # noqa: BLE001
                self._send_json({"ok": False, "error": f"비밀번호를 저장하지 못했습니다: {exc}"}, 500)
                return
            self._clear_login_fails()
            dropped = drop_other_admin_sessions(self._cookie_token())
            audit("password_change", f"해시 파일 갱신 · 다른 세션 {dropped}개 종료", self._client_ip())
            self._send_json({"ok": True, "updated_at": record["updated_at"],
                             "storage": "file", "other_sessions_dropped": dropped,
                             "message": "비밀번호를 변경했습니다. 컨테이너를 재시작해도 유지됩니다."})
            return

        if route == "/api/admin/posts/status":
            if not self._require_admin():
                return
            try:
                post_id = int(body.get("id"))
            except (TypeError, ValueError):
                self._send_json({"ok": False, "error": "글 id 가 필요합니다."}, 400)
                return
            status = str(body.get("status") or "").strip()
            if status not in db.POST_STATUSES:
                self._send_json({"ok": False, "error": "status 는 published 또는 hidden 입니다."}, 400)
                return
            with db.session() as conn:
                post = db.set_post_status(conn, post_id, status)
            if not post:
                self._send_json({"ok": False, "error": "해당 글이 없습니다."}, 404)
                return
            audit("post_status", f"#{post_id} → {status} · {post['title'][:60]}", self._client_ip())
            self._send_json({"ok": True, "post": _admin_post_json(post)})
            return

        if route == "/api/admin/posts/delete":
            if not self._require_admin():
                return
            try:
                post_id = int(body.get("id"))
            except (TypeError, ValueError):
                self._send_json({"ok": False, "error": "글 id 가 필요합니다."}, 400)
                return
            with db.session() as conn:
                post = db.get_post(conn, post_id)
                deleted = db.delete_post(conn, post_id)
            if not deleted:
                self._send_json({"ok": False, "error": "해당 글이 없습니다."}, 404)
                return
            audit("post_delete", f"#{post_id} · {(post or {}).get('title', '')[:60]}", self._client_ip())
            self._send_json({"ok": True, "deleted": post_id})
            return

        if route == "/api/admin/collect":
            if not self._require_admin():
                return
            trigger = str(body.get("trigger") or "admin")[:40]
            started, message = start_collect(trigger)
            if not started:
                self._send_json({"ok": False, "running": True, "error": message}, 409)
                return
            audit("collect_start", trigger, self._client_ip())
            self._send_json({"ok": True, "running": True, "message": message,
                             "started_at": config.iso_kst()}, 202)
            return

        if route in ("/api/auth/send-email-otp", "/api/auth/verify-email-otp",
                     "/api/auth/send-sms-otp", "/api/auth/verify-sms-otp"):
            self._handle_auth_otp(route, body)
            return

        if route == "/api/signup":
            email = str(body.get("email", "")).strip().lower()
            raw_name = body.get("name")
            name = (str(raw_name).strip() if raw_name is not None else "") or None
            phone = authflow.normalize_phone(body.get("phone"))
            raw_provider = body.get("provider")
            provider = ((str(raw_provider).strip().lower() if raw_provider is not None else "email")
                        or "email")
            watchlist = body.get("watchlist") or []
            # 동의 여부를 보내지 않은 요청(관심자산만 수정 등)은 기존 동의 이력을 지우지 않는다.
            raw_marketing = body.get("marketing_agreed")
            marketing_agreed = None if raw_marketing is None else bool(raw_marketing)
            if not EMAIL_RE.match(email):
                self._send_json({"ok": False, "error": "이메일 형식을 확인해 주세요."}, 400)
                return
            if not name:
                self._send_json({"ok": False, "code": "name_required",
                                 "error": "성함 또는 닉네임을 입력해 주세요."}, 400)
                return
            if len(name) > 40:
                self._send_json({"ok": False, "code": "name_too_long",
                                 "error": "성함 또는 닉네임은 40자 이내로 입력해 주세요."}, 400)
                return
            if not phone:
                self._send_json({"ok": False, "code": "phone_required",
                                 "error": "휴대폰 번호를 정확히 입력해 주세요. 예) 010-1234-5678"},
                                400)
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
                state = authflow.verification_state(conn, email)
            if not state["email_verified"] or not state["phone_verified"]:
                self._send_json({
                    "ok": False,
                    "code": "verification_required",
                    "error": "이메일과 휴대폰 본인인증을 모두 마쳐야 가입할 수 있습니다.",
                    "email_verified": state["email_verified"],
                    "phone_verified": state["phone_verified"],
                }, 403)
                return
            if state.get("phone") and state["phone"] != phone:
                self._send_json({
                    "ok": False,
                    "code": "phone_mismatch",
                    "error": "인증한 휴대폰 번호와 입력한 번호가 다릅니다.",
                    "phone_masked": authflow.masked_phone(state["phone"]),
                }, 403)
                return
            with db.session() as conn:
                existing = db.get_user(conn, email)
                # 이미 가입한 이메일을 다시 진행할 때 빈 목록으로 기존 관심자산을 지우지 않는다.
                effective = watchlist if (watchlist or not existing) else None
                user = db.upsert_user(
                    conn, email=email, name=name, provider=provider, watchlist=effective,
                    phone=phone, email_verified=True, phone_verified=True,
                    marketing_agreed=marketing_agreed,
                )
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
    source = admin_password_source()
    if source == "default":
        print("[admin] ⚠ ADMIN_PASSWORD 가 설정되지 않아 개발용 기본 비밀번호를 사용합니다. "
              "운영 배포 전에 /admin/ 보안 탭에서 비밀번호를 변경하세요.")
    elif source == "env":
        print("[admin] 초기 비밀번호: 환경변수 ADMIN_PASSWORD 사용 중 "
              "(/admin/ 보안 탭에서 변경하면 data/admin_auth.json 해시가 우선합니다)")
    else:
        info = admin_auth_info()
        print(f"[admin] 비밀번호: {ADMIN_AUTH_PATH} 해시 사용 (갱신 {info.get('updated_at') or '—'})")
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
