"""SQLite 스키마와 접근 함수.

brief 의 3개 테이블(posts, users, naver_drafts)을 그대로 만들고,
글의 원문 발표 시각을 사실대로 보존하기 위해 posts 에 published_at 컬럼을 추가로 둔다.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    category        TEXT NOT NULL,
    source_name     TEXT NOT NULL,
    source_grade    TEXT NOT NULL,
    source_url      TEXT NOT NULL,
    source_hash     TEXT NOT NULL UNIQUE,
    verified_at     TEXT NOT NULL,
    summary_lines   TEXT NOT NULL,
    market_impact   TEXT NOT NULL,
    tags            TEXT NOT NULL,
    image_path      TEXT,
    is_member_early INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    published_at    TEXT,
    topic           TEXT,
    status          TEXT NOT NULL DEFAULT 'published'
);

CREATE INDEX IF NOT EXISTS idx_posts_category ON posts(category);
CREATE INDEX IF NOT EXISTS idx_posts_created  ON posts(created_at DESC);

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT NOT NULL UNIQUE,
    name            TEXT,
    provider        TEXT NOT NULL DEFAULT 'email',
    watchlist       TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    phone           TEXT,
    email_verified  INTEGER NOT NULL DEFAULT 0,
    phone_verified  INTEGER NOT NULL DEFAULT 0,
    marketing_agreed    INTEGER NOT NULL DEFAULT 0,
    marketing_agreed_at TEXT
);

-- 본인인증 OTP (이메일·휴대폰). 코드는 해시로만 저장한다.
CREATE TABLE IF NOT EXISTS auth_otp (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    channel       TEXT NOT NULL,
    target        TEXT NOT NULL,
    email         TEXT,
    code_hash     TEXT NOT NULL,
    delivery_mode TEXT NOT NULL DEFAULT 'unconfigured',
    sent_at       TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    consumed_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_auth_otp_target ON auth_otp(channel, target, sent_at DESC);

-- 가입 직전 인증 상태 (이메일 1건당 1행)
CREATE TABLE IF NOT EXISTS signup_verification (
    email             TEXT PRIMARY KEY,
    phone             TEXT,
    email_verified_at TEXT,
    phone_verified_at TEXT,
    updated_at        TEXT NOT NULL
);

-- 네이버 블로그 발행 이력
CREATE TABLE IF NOT EXISTS naver_publish (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id    INTEGER NOT NULL REFERENCES naver_drafts(id) ON DELETE CASCADE,
    post_id     INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'pending',
    url         TEXT,
    message     TEXT,
    attempted_at TEXT NOT NULL,
    published_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_naver_publish_draft ON naver_publish(draft_id);

CREATE TABLE IF NOT EXISTS naver_drafts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id      INTEGER NOT NULL UNIQUE REFERENCES posts(id) ON DELETE CASCADE,
    seo_title    TEXT NOT NULL,
    keywords     TEXT NOT NULL,
    body_html    TEXT NOT NULL,
    body_text    TEXT NOT NULL,
    telegram_cta TEXT NOT NULL,
    web_cta      TEXT NOT NULL,
    image_path   TEXT,
    created_at   TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def session():
    """with db.session() as conn: ... — 블록이 끝나면 반드시 close."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def init_db(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or connect()
    try:
        conn.executescript(SCHEMA)
        # 이전 버전 DB 호환: topic 컬럼이 없으면 추가
        cols = {r[1] for r in conn.execute("PRAGMA table_info(posts)")}
        if "topic" not in cols:
            conn.execute("ALTER TABLE posts ADD COLUMN topic TEXT")
        # 운영자 콘텐츠 관리: 공개/숨김 상태 (기존 글은 모두 공개)
        if "status" not in cols:
            conn.execute("ALTER TABLE posts ADD COLUMN status TEXT NOT NULL DEFAULT 'published'")
        user_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if "status" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        # 본인인증 가입: 휴대폰·인증 여부·마케팅 동의 (기존 회원은 미인증 상태로 보존)
        for column, ddl in (
            ("phone", "ALTER TABLE users ADD COLUMN phone TEXT"),
            ("email_verified", "ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0"),
            ("phone_verified", "ALTER TABLE users ADD COLUMN phone_verified INTEGER NOT NULL DEFAULT 0"),
            ("marketing_agreed", "ALTER TABLE users ADD COLUMN marketing_agreed INTEGER NOT NULL DEFAULT 0"),
            ("marketing_agreed_at", "ALTER TABLE users ADD COLUMN marketing_agreed_at TEXT"),
        ):
            if column not in user_cols:
                conn.execute(ddl)
        conn.commit()
    finally:
        if own:
            conn.close()


def row_to_post(row: sqlite3.Row) -> dict[str, Any]:
    post = dict(row)
    post["summary_lines"] = json.loads(post["summary_lines"] or "[]")
    post["tags"] = json.loads(post["tags"] or "[]")
    post["is_member_early"] = bool(post["is_member_early"])
    return post


def insert_post(conn: sqlite3.Connection, post: dict[str, Any]) -> tuple[int | None, bool]:
    """source_hash 기준 중복이면 (None, False). 새로 넣었으면 (id, True)."""
    cur = conn.execute(
        """
        INSERT INTO posts
            (title, category, source_name, source_grade, source_url, source_hash,
             verified_at, summary_lines, market_impact, tags, image_path,
             is_member_early, created_at, published_at, topic)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source_hash) DO NOTHING
        """,
        (
            post["title"],
            post["category"],
            post["source_name"],
            post["source_grade"],
            post["source_url"],
            post["source_hash"],
            post["verified_at"],
            json.dumps(post["summary_lines"], ensure_ascii=False),
            post["market_impact"],
            json.dumps(post["tags"], ensure_ascii=False),
            post.get("image_path"),
            1 if post.get("is_member_early") else 0,
            post["created_at"],
            post.get("published_at"),
            post.get("topic"),
        ),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None, False
    return int(cur.lastrowid), True


def get_post(conn: sqlite3.Connection, post_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    return row_to_post(row) if row else None


POST_STATUSES = ("published", "hidden")


def list_posts(
    conn: sqlite3.Connection,
    category: str | None = None,
    tag: str | None = None,
    q: str | None = None,
    limit: int | None = None,
    include_hidden: bool = False,
    status: str | None = None,
    source: str | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM posts WHERE 1=1"
    params: list[Any] = []
    if status in POST_STATUSES:
        sql += " AND COALESCE(status, 'published') = ?"
        params.append(status)
    elif not include_hidden:
        # 공개 API 기본값: 숨김 처리한 글은 절대 내보내지 않는다.
        sql += " AND COALESCE(status, 'published') = 'published'"
    if source:
        sql += " AND source_name = ?"
        params.append(source)
    if category and category not in ("all", "전체"):
        sql += " AND category = ?"
        params.append(category)
    if tag:
        # tags 는 JSON 배열 문자열. 정확한 항목 일치를 위해 따옴표 포함 검색.
        sql += " AND tags LIKE ?"
        params.append(f'%"{tag}"%')
    if q:
        sql += " AND (title LIKE ? OR summary_lines LIKE ? OR market_impact LIKE ? OR tags LIKE ?)"
        like = f"%{q}%"
        params.extend([like, like, like, like])
    sql += " ORDER BY COALESCE(published_at, verified_at) DESC, id DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
        if offset:
            sql += " OFFSET ?"
            params.append(int(offset))
    return [row_to_post(r) for r in conn.execute(sql, params).fetchall()]


def count_posts(conn: sqlite3.Connection, status: str | None = None) -> int:
    if status in POST_STATUSES:
        return int(conn.execute(
            "SELECT COUNT(*) FROM posts WHERE COALESCE(status, 'published') = ?", (status,)
        ).fetchone()[0])
    return int(conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0])


def count_posts_since(conn: sqlite3.Connection, day: str) -> int:
    """해당 KST 날짜(YYYY-MM-DD)에 들어온 글 수."""
    return int(conn.execute(
        "SELECT COUNT(*) FROM posts WHERE substr(created_at, 1, 10) = ?", (day,)
    ).fetchone()[0])


def count_posts_matching(
    conn: sqlite3.Connection,
    category: str | None = None,
    source: str | None = None,
    q: str | None = None,
    status: str | None = None,
) -> int:
    """운영자 콘텐츠 목록과 같은 조건의 전체 건수(페이지네이션용)."""
    sql = "SELECT COUNT(*) FROM posts WHERE 1=1"
    params: list[Any] = []
    if status in POST_STATUSES:
        sql += " AND COALESCE(status, 'published') = ?"
        params.append(status)
    if category and category not in ("all", "전체"):
        sql += " AND category = ?"
        params.append(category)
    if source:
        sql += " AND source_name = ?"
        params.append(source)
    if q:
        sql += " AND (title LIKE ? OR summary_lines LIKE ? OR market_impact LIKE ? OR tags LIKE ?)"
        like = f"%{q}%"
        params.extend([like, like, like, like])
    return int(conn.execute(sql, params).fetchone()[0])


def set_post_status(conn: sqlite3.Connection, post_id: int, status: str) -> dict[str, Any] | None:
    """공개(published) / 숨김(hidden) 전환. 없는 글이면 None."""
    if status not in POST_STATUSES:
        raise ValueError(f"unknown status: {status}")
    cur = conn.execute("UPDATE posts SET status = ? WHERE id = ?", (status, int(post_id)))
    conn.commit()
    if cur.rowcount == 0:
        return None
    return get_post(conn, int(post_id))


def delete_post(conn: sqlite3.Connection, post_id: int) -> bool:
    """글 삭제. 연결된 네이버 초안도 함께 지운다."""
    conn.execute("DELETE FROM naver_drafts WHERE post_id = ?", (int(post_id),))
    cur = conn.execute("DELETE FROM posts WHERE id = ?", (int(post_id),))
    conn.commit()
    return cur.rowcount > 0


def list_sources(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT DISTINCT source_name FROM posts ORDER BY source_name").fetchall()
    return [r[0] for r in rows if r[0]]


def post_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN COALESCE(status,'published') = 'published' THEN 1 ELSE 0 END) AS published,
                  SUM(CASE WHEN status = 'hidden' THEN 1 ELSE 0 END) AS hidden,
                  MAX(COALESCE(published_at, verified_at)) AS latest
           FROM posts"""
    ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "published": int(row["published"] or 0),
        "hidden": int(row["hidden"] or 0),
        "latest": row["latest"],
    }


def category_counts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT category AS key, COUNT(*) AS n
           FROM posts GROUP BY category ORDER BY n DESC, key"""
    ).fetchall()
    return [{"key": r["key"], "count": int(r["n"])} for r in rows]


def source_counts(conn: sqlite3.Connection, limit: int = 8) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT source_name AS name, COUNT(*) AS n
           FROM posts GROUP BY source_name ORDER BY n DESC, name LIMIT ?""", (int(limit),)
    ).fetchall()
    return [{"name": r["name"], "count": int(r["n"])} for r in rows]


def posts_without_drafts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT p.* FROM posts p
           LEFT JOIN naver_drafts d ON d.post_id = p.id
           WHERE d.id IS NULL
           ORDER BY p.id ASC"""
    ).fetchall()
    return [row_to_post(r) for r in rows]


def recent_drafts(conn: sqlite3.Connection, n: int = 10) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM naver_drafts ORDER BY id DESC LIMIT ?", (int(n),)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["keywords"] = json.loads(d["keywords"] or "[]")
        out.append(d)
    return out


def insert_draft(conn: sqlite3.Connection, draft: dict[str, Any]) -> int:
    cur = conn.execute(
        """INSERT INTO naver_drafts
             (post_id, seo_title, keywords, body_html, body_text,
              telegram_cta, web_cta, image_path, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            draft["post_id"],
            draft["seo_title"],
            json.dumps(draft["keywords"], ensure_ascii=False),
            draft["body_html"],
            draft["body_text"],
            draft["telegram_cta"],
            draft["web_cta"],
            draft.get("image_path"),
            draft["created_at"],
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def count_drafts(conn: sqlite3.Connection, since_day: str | None = None) -> int:
    if since_day:
        return int(conn.execute(
            "SELECT COUNT(*) FROM naver_drafts WHERE substr(created_at, 1, 10) = ?", (since_day,)
        ).fetchone()[0])
    return int(conn.execute("SELECT COUNT(*) FROM naver_drafts").fetchone()[0])


def list_drafts(conn: sqlite3.Connection, limit: int | None = None) -> list[dict[str, Any]]:
    sql = """SELECT d.*, p.title AS post_title, p.category, p.source_name,
                    p.source_grade, p.source_url, p.verified_at, p.tags
             FROM naver_drafts d JOIN posts p ON p.id = d.post_id
             ORDER BY d.id DESC"""
    params: Iterable[Any] = ()
    if limit:
        sql += " LIMIT ?"
        params = (int(limit),)
    out = []
    for r in conn.execute(sql, params).fetchall():
        d = dict(r)
        d["keywords"] = json.loads(d["keywords"] or "[]")
        d["tags"] = json.loads(d["tags"] or "[]")
        out.append(d)
    return out


def upsert_user(
    conn: sqlite3.Connection,
    email: str,
    name: str | None = None,
    provider: str = "email",
    watchlist: list[str] | None = None,
    created_at: str | None = None,
    phone: str | None = None,
    email_verified: bool | None = None,
    phone_verified: bool | None = None,
    marketing_agreed: bool | None = None,
    marketing_agreed_at: str | None = None,
) -> dict[str, Any]:
    email = email.strip().lower()
    created_at = created_at or config.iso_kst()
    agreed_at = None
    if marketing_agreed:
        agreed_at = marketing_agreed_at or config.iso_kst()
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if row:
        sets: list[str] = []
        params: list[Any] = []
        if name:
            sets.append("name = ?")
            params.append(name)
        if provider:
            sets.append("provider = ?")
            params.append(provider)
        if watchlist is not None:
            sets.append("watchlist = ?")
            params.append(json.dumps(watchlist, ensure_ascii=False))
        if phone:
            sets.append("phone = ?")
            params.append(phone)
        if email_verified is not None:
            sets.append("email_verified = ?")
            params.append(1 if email_verified else 0)
        if phone_verified is not None:
            sets.append("phone_verified = ?")
            params.append(1 if phone_verified else 0)
        if marketing_agreed is not None:
            sets.append("marketing_agreed = ?")
            params.append(1 if marketing_agreed else 0)
            # 이미 동의한 이력은 유지하고, 처음 동의한 시각만 남긴다.
            if marketing_agreed and not row["marketing_agreed_at"]:
                sets.append("marketing_agreed_at = ?")
                params.append(agreed_at)
            elif not marketing_agreed:
                sets.append("marketing_agreed_at = NULL")
        if sets:
            params.append(email)
            conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE email = ?", params)
        conn.commit()
    else:
        conn.execute(
            """INSERT INTO users
                   (email, name, provider, watchlist, created_at,
                    phone, email_verified, phone_verified, marketing_agreed, marketing_agreed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                email,
                name,
                provider,
                json.dumps(watchlist or [], ensure_ascii=False),
                created_at,
                phone,
                1 if email_verified else 0,
                1 if phone_verified else 0,
                1 if marketing_agreed else 0,
                agreed_at,
            ),
        )
        conn.commit()
    return get_user(conn, email)  # type: ignore[return-value]


def list_users(
    conn: sqlite3.Connection,
    q: str | None = None,
    provider: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM users WHERE 1=1"
    params: list[Any] = []
    if q:
        sql += " AND (email LIKE ? OR name LIKE ? OR watchlist LIKE ? OR COALESCE(phone, '') LIKE ?)"
        like = f"%{q}%"
        params.extend([like, like, like, like])
    if provider and provider != "all":
        sql += " AND provider = ?"
        params.append(provider)
    if status and status != "all":
        sql += " AND COALESCE(status, 'active') = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    params.extend([int(limit), int(offset)])
    out = []
    for r in conn.execute(sql, params).fetchall():
        user = dict(r)
        user["watchlist"] = json.loads(user["watchlist"] or "[]")
        out.append(user)
    return out


def count_users(
    conn: sqlite3.Connection,
    q: str | None = None,
    provider: str | None = None,
    status: str | None = None,
) -> int:
    sql = "SELECT COUNT(*) FROM users WHERE 1=1"
    params: list[Any] = []
    if q:
        sql += " AND (email LIKE ? OR name LIKE ? OR watchlist LIKE ? OR COALESCE(phone, '') LIKE ?)"
        like = f"%{q}%"
        params.extend([like, like, like, like])
    if provider and provider != "all":
        sql += " AND provider = ?"
        params.append(provider)
    if status and status != "all":
        sql += " AND COALESCE(status, 'active') = ?"
        params.append(status)
    return int(conn.execute(sql, params).fetchone()[0])


def user_stats(conn: sqlite3.Connection, days: int = 14) -> dict[str, Any]:
    """가입 KPI + 일별 추이 (KST 날짜 기준, 비어 있는 날도 0으로 채움)."""
    from datetime import date, timedelta

    today = config.now_kst().date()
    start = today - timedelta(days=int(days) - 1)
    rows = conn.execute(
        """SELECT substr(created_at, 1, 10) AS d, COUNT(*) AS n
           FROM users WHERE substr(created_at, 1, 10) >= ?
           GROUP BY d""",
        (start.isoformat(),),
    ).fetchall()
    by_day = {r["d"]: int(r["n"]) for r in rows}
    series = []
    cursor = start
    while cursor <= today:
        series.append({"date": cursor.isoformat(), "count": by_day.get(cursor.isoformat(), 0)})
        cursor += timedelta(days=1)
    week_start = (today - timedelta(days=6)).isoformat()
    month_start = (today - timedelta(days=29)).isoformat()
    total = int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
    today_n = by_day.get(today.isoformat(), 0)
    week_n = int(conn.execute(
        "SELECT COUNT(*) FROM users WHERE substr(created_at, 1, 10) >= ?", (week_start,)
    ).fetchone()[0])
    month_n = int(conn.execute(
        "SELECT COUNT(*) FROM users WHERE substr(created_at, 1, 10) >= ?", (month_start,)
    ).fetchone()[0])
    return {
        "total": total,
        "today": today_n,
        "last7": week_n,
        "last30": month_n,
        "series": series,
    }


def recent_users(conn: sqlite3.Connection, n: int = 6) -> list[dict[str, Any]]:
    out = []
    for r in conn.execute("SELECT * FROM users ORDER BY created_at DESC, id DESC LIMIT ?", (int(n),)).fetchall():
        user = dict(r)
        user["watchlist"] = json.loads(user["watchlist"] or "[]")
        out.append(user)
    return out


def provider_counts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT provider AS key, COUNT(*) AS n FROM users
           GROUP BY provider ORDER BY n DESC, key"""
    ).fetchall()
    return [{"key": r["key"], "count": int(r["n"])} for r in rows]


def get_user(conn: sqlite3.Connection, email: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM users WHERE email = ?", (email.strip().lower(),)
    ).fetchone()
    if not row:
        return None
    user = dict(row)
    user["watchlist"] = json.loads(user["watchlist"] or "[]")
    return user


def set_watchlist(conn: sqlite3.Connection, email: str, watchlist: list[str]) -> dict[str, Any]:
    email = email.strip().lower()
    conn.execute(
        "UPDATE users SET watchlist = ? WHERE email = ?",
        (json.dumps(watchlist, ensure_ascii=False), email),
    )
    conn.commit()
    return get_user(conn, email)  # type: ignore[return-value]


# ---------------------------------------------------------------- 본인인증 OTP

def insert_otp(
    conn: sqlite3.Connection,
    channel: str,
    target: str,
    code_hash: str,
    delivery_mode: str,
    sent_at: str,
    expires_at: str,
    email: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO auth_otp (channel, target, email, code_hash, delivery_mode, sent_at, expires_at)
           VALUES (?,?,?,?,?,?,?)""",
        (channel, target, email, code_hash, delivery_mode, sent_at, expires_at),
    )
    conn.commit()
    return int(cur.lastrowid)


def latest_otp(conn: sqlite3.Connection, channel: str, target: str) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT * FROM auth_otp WHERE channel = ? AND target = ? AND consumed_at IS NULL
           ORDER BY id DESC LIMIT 1""",
        (channel, target),
    ).fetchone()
    return dict(row) if row else None


def count_recent_otp(conn: sqlite3.Connection, channel: str, target: str, since_iso: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM auth_otp WHERE channel = ? AND target = ? AND sent_at >= ?",
        (channel, target, since_iso),
    ).fetchone()
    return int(row[0]) if row else 0


def bump_otp_attempts(conn: sqlite3.Connection, otp_id: int) -> int:
    conn.execute("UPDATE auth_otp SET attempts = attempts + 1 WHERE id = ?", (otp_id,))
    conn.commit()
    row = conn.execute("SELECT attempts FROM auth_otp WHERE id = ?", (otp_id,)).fetchone()
    return int(row[0]) if row else 0


def consume_otp(conn: sqlite3.Connection, otp_id: int, when: str) -> None:
    conn.execute("UPDATE auth_otp SET consumed_at = ? WHERE id = ?", (when, otp_id))
    conn.commit()


def set_verification(
    conn: sqlite3.Connection,
    email: str,
    phone: str | None = None,
    email_ok: bool | None = None,
    phone_ok: bool | None = None,
    when: str | None = None,
) -> dict[str, Any]:
    email = email.strip().lower()
    when = when or config.iso_kst()
    row = conn.execute("SELECT * FROM signup_verification WHERE email = ?", (email,)).fetchone()
    if row:
        sets = ["updated_at = ?"]
        params: list[Any] = [when]
        if phone:
            sets.append("phone = ?")
            params.append(phone)
            # 휴대폰 번호가 바뀌면 이전 휴대폰 인증은 무효다.
            if phone != row["phone"] and phone_ok is None:
                sets.append("phone_verified_at = NULL")
        if email_ok:
            sets.append("email_verified_at = ?")
            params.append(when)
        if phone_ok:
            sets.append("phone_verified_at = ?")
            params.append(when)
        params.append(email)
        conn.execute(f"UPDATE signup_verification SET {', '.join(sets)} WHERE email = ?", params)
    else:
        conn.execute(
            """INSERT INTO signup_verification
                   (email, phone, email_verified_at, phone_verified_at, updated_at)
               VALUES (?,?,?,?,?)""",
            (email, phone, when if email_ok else None, when if phone_ok else None, when),
        )
    conn.commit()
    return get_verification(conn, email) or {}


def get_verification(conn: sqlite3.Connection, email: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM signup_verification WHERE email = ?", (email.strip().lower(),)
    ).fetchone()
    return dict(row) if row else None


# ------------------------------------------------------------ 네이버 발행 이력

def pending_naver_drafts(conn: sqlite3.Connection, limit: int = 1) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT d.*, p.title AS post_title, p.source_url, p.verified_at, p.topic
           FROM naver_drafts d
           JOIN posts p ON p.id = d.post_id
           WHERE p.status = 'published'
             AND NOT EXISTS (
                 SELECT 1 FROM naver_publish np
                 WHERE np.draft_id = d.id AND np.status IN ('published', 'skipped')
             )
           ORDER BY d.id DESC
           LIMIT ?""",
        (int(limit),),
    ).fetchall()
    return [dict(r) for r in rows]


def record_naver_publish(
    conn: sqlite3.Connection,
    draft_id: int,
    post_id: int,
    status: str,
    url: str | None = None,
    message: str | None = None,
    attempted_at: str | None = None,
    published_at: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO naver_publish (draft_id, post_id, status, url, message, attempted_at, published_at)
           VALUES (?,?,?,?,?,?,?)""",
        (draft_id, post_id, status, url, message, attempted_at or config.iso_kst(), published_at),
    )
    conn.commit()
    return int(cur.lastrowid)


def naver_publish_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM naver_publish GROUP BY status"
    ).fetchall()
    counts = {r["status"]: int(r["n"]) for r in rows}
    last = conn.execute(
        "SELECT * FROM naver_publish ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {
        "published": counts.get("published", 0),
        "failed": counts.get("failed", 0),
        "skipped": counts.get("skipped", 0),
        "counts": counts,
        "last": dict(last) if last else None,
        "pending": len(pending_naver_drafts(conn, limit=999)),
    }


if __name__ == "__main__":
    init_db()
    with connect() as c:
        print(f"DB ready: {config.DB_PATH}")
        print("posts:", count_posts(c), "| drafts:", count_drafts(c))
