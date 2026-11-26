"""SQLite 스키마와 접근 함수.

brief 의 3개 테이블(posts, users, naver_drafts)을 그대로 만들고,
글의 원문 발표 시각을 사실대로 보존하기 위해 posts 에 published_at 컬럼을 추가로 둔다.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
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
    importance      TEXT
);

CREATE INDEX IF NOT EXISTS idx_posts_category ON posts(category);
CREATE INDEX IF NOT EXISTS idx_posts_created  ON posts(created_at DESC);

CREATE TABLE IF NOT EXISTS users (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    email             TEXT UNIQUE,
    name              TEXT,
    provider          TEXT NOT NULL DEFAULT 'phone',
    watchlist         TEXT NOT NULL DEFAULT '[]',
    phone             TEXT,
    phone_verified    INTEGER NOT NULL DEFAULT 0,
    phone_verified_at TEXT,
    plan              TEXT NOT NULL DEFAULT 'free',
    alerts_enabled    INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS phone_verifications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    phone        TEXT NOT NULL,
    code_hash    TEXT NOT NULL,
    salt         TEXT NOT NULL,
    purpose      TEXT NOT NULL DEFAULT 'signup',
    attempts     INTEGER NOT NULL DEFAULT 0,
    token_hash   TEXT,
    verified_at  TEXT,
    consumed_at  TEXT,
    expires_at   TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_phone_codes ON phone_verifications(phone, created_at DESC);

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


def _migrate_users_email_nullable(conn: sqlite3.Connection) -> None:
    """휴대폰 인증만으로 가입할 수 있도록 users.email 의 NOT NULL 을 푼다.

    SQLite 는 컬럼 제약을 바꾸지 못하므로 테이블을 새로 만들어 복사한다.
    기존 id·이메일·관심자산은 그대로 보존한다.
    """
    info = {r[1]: r for r in conn.execute("PRAGMA table_info(users)")}
    if "email" not in info or not info["email"][3]:  # 3 = notnull
        return
    conn.executescript(
        """
        ALTER TABLE users RENAME TO users_old_email_notnull;
        CREATE TABLE users (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            email             TEXT UNIQUE,
            name              TEXT,
            provider          TEXT NOT NULL DEFAULT 'phone',
            watchlist         TEXT NOT NULL DEFAULT '[]',
            phone             TEXT,
            phone_verified    INTEGER NOT NULL DEFAULT 0,
            phone_verified_at TEXT,
            plan              TEXT NOT NULL DEFAULT 'free',
            alerts_enabled    INTEGER NOT NULL DEFAULT 1,
            created_at        TEXT NOT NULL
        );
        INSERT INTO users (id, email, name, provider, watchlist, created_at)
            SELECT id, email, name, provider, watchlist, created_at
            FROM users_old_email_notnull;
        DROP TABLE users_old_email_notnull;
        """
    )


def init_db(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or connect()
    try:
        conn.executescript(SCHEMA)
        # 이전 버전 DB 호환: topic·importance 컬럼이 없으면 추가
        post_cols = {r[1] for r in conn.execute("PRAGMA table_info(posts)")}
        if "topic" not in post_cols:
            conn.execute("ALTER TABLE posts ADD COLUMN topic TEXT")
            post_cols.add("topic")
        if "importance" not in post_cols:
            # 기본값 없이 추가해 '아직 분류하지 않은 글'을 구분한다(collector 가 채운다).
            conn.execute("ALTER TABLE posts ADD COLUMN importance TEXT")

        # users: 휴대폰 인증·요금제 컬럼 마이그레이션
        _migrate_users_email_nullable(conn)
        user_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        additions = {
            "phone": "TEXT",
            "phone_verified": "INTEGER NOT NULL DEFAULT 0",
            "phone_verified_at": "TEXT",
            "plan": "TEXT NOT NULL DEFAULT 'free'",
            "alerts_enabled": "INTEGER NOT NULL DEFAULT 1",
        }
        for column, ddl in additions.items():
            if column not in user_cols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {column} {ddl}")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone ON users(phone) WHERE phone IS NOT NULL")
        conn.commit()
    finally:
        if own:
            conn.close()


def row_to_post(row: sqlite3.Row) -> dict[str, Any]:
    post = dict(row)
    post["summary_lines"] = json.loads(post["summary_lines"] or "[]")
    post["tags"] = json.loads(post["tags"] or "[]")
    post["is_member_early"] = bool(post["is_member_early"])
    level = str(post.get("importance") or config.IMPORTANCE_DEFAULT).upper()
    if level not in config.IMPORTANCE_LEVELS:
        level = config.IMPORTANCE_DEFAULT
    post["importance"] = level
    post["importance_label"] = config.IMPORTANCE_LEVELS[level]["label"]
    return post


def insert_post(conn: sqlite3.Connection, post: dict[str, Any]) -> tuple[int | None, bool]:
    """source_hash 기준 중복이면 (None, False). 새로 넣었으면 (id, True)."""
    cur = conn.execute(
        """
        INSERT INTO posts
            (title, category, source_name, source_grade, source_url, source_hash,
             verified_at, summary_lines, market_impact, tags, image_path,
             is_member_early, created_at, published_at, topic, importance)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            str(post.get("importance") or config.IMPORTANCE_DEFAULT).upper(),
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
    email: str | None = None,
    name: str | None = None,
    provider: str = "phone",
    watchlist: list[str] | None = None,
    created_at: str | None = None,
    phone: str | None = None,
    phone_verified: bool | None = None,
    plan: str | None = None,
    alerts_enabled: bool | None = None,
) -> dict[str, Any]:
    """가입·갱신. 이메일 없이 휴대폰 번호만으로도 가입할 수 있다.

    같은 이메일 또는 같은 휴대폰 번호의 기존 회원이면 필요한 값만 갱신한다.
    """
    email = (email or "").strip().lower() or None
    phone = (phone or "").strip() or None
    if not email and not phone:
        raise ValueError("email 또는 phone 중 하나는 필요합니다.")
    created_at = created_at or config.iso_kst()
    row = None
    if email:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if row is None and phone:
        row = conn.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()

    if row:
        user_id = int(row["id"])
        if name:
            conn.execute("UPDATE users SET name = ? WHERE id = ?", (name, user_id))
        if provider:
            conn.execute("UPDATE users SET provider = ? WHERE id = ?", (provider, user_id))
        if watchlist is not None:
            conn.execute(
                "UPDATE users SET watchlist = ? WHERE id = ?",
                (json.dumps(watchlist, ensure_ascii=False), user_id),
            )
        if email and not row["email"]:
            conn.execute("UPDATE users SET email = ? WHERE id = ?", (email, user_id))
        if phone:
            conn.execute("UPDATE users SET phone = ? WHERE id = ?", (phone, user_id))
        if phone_verified is not None:
            conn.execute(
                "UPDATE users SET phone_verified = ?, phone_verified_at = ? WHERE id = ?",
                (1 if phone_verified else 0,
                 (row["phone_verified_at"] or config.iso_kst()) if phone_verified else None,
                 user_id),
            )
        if plan in config.PLANS:
            conn.execute("UPDATE users SET plan = ? WHERE id = ?", (plan, user_id))
        if alerts_enabled is not None:
            conn.execute("UPDATE users SET alerts_enabled = ? WHERE id = ?",
                         (1 if alerts_enabled else 0, user_id))
        conn.commit()
        found = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    else:
        cur = conn.execute(
            """INSERT INTO users
                 (email, name, provider, watchlist, phone, phone_verified,
                  phone_verified_at, plan, alerts_enabled, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                email,
                name,
                provider or "phone",
                json.dumps(watchlist or [], ensure_ascii=False),
                phone,
                1 if phone_verified else 0,
                config.iso_kst() if phone_verified else None,
                plan if plan in config.PLANS else "free",
                1 if (alerts_enabled is None or alerts_enabled) else 0,
                created_at,
            ),
        )
        conn.commit()
        found = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_to_user(found)  # type: ignore[arg-type]


def _row_to_user(row: sqlite3.Row) -> dict[str, Any]:
    user = dict(row)
    user["watchlist"] = json.loads(user.get("watchlist") or "[]")
    user["phone_verified"] = bool(user.get("phone_verified"))
    user["alerts_enabled"] = bool(user.get("alerts_enabled"))
    user["plan"] = user.get("plan") if user.get("plan") in config.PLANS else "free"
    return user


def get_user(conn: sqlite3.Connection, email: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM users WHERE email = ?", ((email or "").strip().lower(),)
    ).fetchone()
    return _row_to_user(row) if row else None


def get_user_by_phone(conn: sqlite3.Connection, phone: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM users WHERE phone = ?", (phone.strip(),)).fetchone()
    return _row_to_user(row) if row else None


def set_watchlist(conn: sqlite3.Connection, email: str, watchlist: list[str]) -> dict[str, Any]:
    email = email.strip().lower()
    conn.execute(
        "UPDATE users SET watchlist = ? WHERE email = ?",
        (json.dumps(watchlist, ensure_ascii=False), email),
    )
    conn.commit()
    return get_user(conn, email)  # type: ignore[return-value]


# ---------------------------------------------------------------- 통계 · 중요도


def today_stats(conn: sqlite3.Connection, now=None) -> dict[str, Any]:
    """KST 기준 '오늘 확인 건수'와, 오늘 0건일 때 보여줄 최근 확인일을 계산한다."""
    now = now or config.now_kst()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    start_iso, end_iso = config.iso_kst(start), config.iso_kst(end)

    today_count = int(conn.execute(
        "SELECT COUNT(*) FROM posts WHERE datetime(verified_at) >= datetime(?) "
        "AND datetime(verified_at) < datetime(?)",
        (start_iso, end_iso),
    ).fetchone()[0])
    total = count_posts(conn)
    last_row = conn.execute(
        "SELECT date(datetime(verified_at), '+9 hours') AS kst_date, COUNT(*) AS n, "
        "       MAX(verified_at) AS last_at "
        "FROM posts WHERE datetime(verified_at) < datetime(?) "
        "GROUP BY kst_date ORDER BY kst_date DESC LIMIT 1",
        (start_iso,),
    ).fetchone()
    last_any = conn.execute(
        "SELECT verified_at FROM posts ORDER BY datetime(verified_at) DESC LIMIT 1"
    ).fetchone()
    recent = None
    if last_row and last_row["n"]:
        recent = {"date": last_row["kst_date"], "count": int(last_row["n"]), "last_at": last_row["last_at"]}
    return {
        "date": start.strftime("%Y-%m-%d"),
        "count": today_count,
        "total": total,
        "recent": recent,
        "last_verified_at": last_any["verified_at"] if last_any else None,
    }


def importance_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {"S": 0, "A": 0, "B": 0}
    for row in conn.execute("SELECT importance, COUNT(*) AS n FROM posts GROUP BY importance"):
        level = str(row["importance"] or config.IMPORTANCE_DEFAULT).upper()
        if level in counts:
            counts[level] += int(row["n"])
    return counts


def set_post_importance(conn: sqlite3.Connection, post_id: int, level: str) -> None:
    level = str(level or config.IMPORTANCE_DEFAULT).upper()
    if level not in config.IMPORTANCE_LEVELS:
        level = config.IMPORTANCE_DEFAULT
    conn.execute("UPDATE posts SET importance = ? WHERE id = ?", (level, post_id))


# ---------------------------------------------------------------- 휴대폰 인증


def create_phone_code(
    conn: sqlite3.Connection,
    phone: str,
    code_hash: str,
    salt: str,
    expires_at: str,
    purpose: str = "signup",
    created_at: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO phone_verifications
             (phone, code_hash, salt, purpose, expires_at, created_at)
           VALUES (?,?,?,?,?,?)""",
        (phone, code_hash, salt, purpose, expires_at, created_at or config.iso_kst()),
    )
    conn.commit()
    return int(cur.lastrowid)


def latest_phone_code(conn: sqlite3.Connection, phone: str, purpose: str = "signup") -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT * FROM phone_verifications
           WHERE phone = ? AND purpose = ? AND consumed_at IS NULL
           ORDER BY id DESC LIMIT 1""",
        (phone, purpose),
    ).fetchone()
    return dict(row) if row else None


def bump_phone_attempts(conn: sqlite3.Connection, code_id: int) -> int:
    conn.execute("UPDATE phone_verifications SET attempts = attempts + 1 WHERE id = ?", (code_id,))
    conn.commit()
    row = conn.execute("SELECT attempts FROM phone_verifications WHERE id = ?", (code_id,)).fetchone()
    return int(row[0]) if row else 0


def mark_phone_verified(
    conn: sqlite3.Connection, code_id: int, token_hash: str, verified_at: str
) -> None:
    conn.execute(
        "UPDATE phone_verifications SET token_hash = ?, verified_at = ? WHERE id = ?",
        (token_hash, verified_at, code_id),
    )
    conn.commit()


def consume_phone_token(
    conn: sqlite3.Connection, phone: str, token_hash: str, now=None, purpose: str = "signup"
) -> bool:
    """인증 완료 토큰을 1회용으로 소모한다. 성공하면 True."""
    now = now or config.now_kst()
    row = conn.execute(
        """SELECT * FROM phone_verifications
           WHERE phone = ? AND purpose = ? AND token_hash = ?
             AND consumed_at IS NULL AND verified_at IS NOT NULL
           ORDER BY id DESC LIMIT 1""",
        (phone, purpose, token_hash),
    ).fetchone()
    if not row:
        return False
    try:
        if config.parse_kst(row["expires_at"]) <= now:
            return False
    except Exception:
        return False
    conn.execute(
        "UPDATE phone_verifications SET consumed_at = ? WHERE id = ?",
        (config.iso_kst(now), row["id"]),
    )
    conn.commit()
    return True


def recent_phone_sends(conn: sqlite3.Connection, phone: str, since_iso: str) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM phone_verifications WHERE phone = ? AND datetime(created_at) >= datetime(?)",
        (phone, since_iso),
    ).fetchone()[0])


if __name__ == "__main__":
    init_db()
    with connect() as c:
        print(f"DB ready: {config.DB_PATH}")
        print("posts:", count_posts(c), "| drafts:", count_drafts(c))
