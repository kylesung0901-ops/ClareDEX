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
    topic           TEXT
);

CREATE INDEX IF NOT EXISTS idx_posts_category ON posts(category);
CREATE INDEX IF NOT EXISTS idx_posts_created  ON posts(created_at DESC);

CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT NOT NULL UNIQUE,
    name       TEXT,
    provider   TEXT NOT NULL DEFAULT 'email',
    watchlist  TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

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


def list_posts(
    conn: sqlite3.Connection,
    category: str | None = None,
    tag: str | None = None,
    q: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM posts WHERE 1=1"
    params: list[Any] = []
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
    return [row_to_post(r) for r in conn.execute(sql, params).fetchall()]


def count_posts(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0])


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


def count_drafts(conn: sqlite3.Connection) -> int:
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
) -> dict[str, Any]:
    email = email.strip().lower()
    created_at = created_at or config.iso_kst()
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if row:
        if name:
            conn.execute("UPDATE users SET name = ? WHERE email = ?", (name, email))
        if provider:
            conn.execute("UPDATE users SET provider = ? WHERE email = ?", (provider, email))
        if watchlist is not None:
            conn.execute(
                "UPDATE users SET watchlist = ? WHERE email = ?",
                (json.dumps(watchlist, ensure_ascii=False), email),
            )
        conn.commit()
    else:
        conn.execute(
            "INSERT INTO users (email, name, provider, watchlist, created_at) VALUES (?,?,?,?,?)",
            (
                email,
                name,
                provider,
                json.dumps(watchlist or [], ensure_ascii=False),
                created_at,
            ),
        )
        conn.commit()
    return get_user(conn, email)  # type: ignore[return-value]


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


if __name__ == "__main__":
    init_db()
    with connect() as c:
        print(f"DB ready: {config.DB_PATH}")
        print("posts:", count_posts(c), "| drafts:", count_drafts(c))
