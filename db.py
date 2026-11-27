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
    status          TEXT NOT NULL DEFAULT 'published',
    importance      TEXT NOT NULL DEFAULT 'B'
);

CREATE INDEX IF NOT EXISTS idx_posts_category ON posts(category);
CREATE INDEX IF NOT EXISTS idx_posts_created  ON posts(created_at DESC);

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT UNIQUE,
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

CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone);

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

-- 가입 직전 인증 상태 (이메일 1건당 1행 · 이메일을 쓰지 않는 휴대폰 단독 가입에는 쓰지 않는다)
CREATE TABLE IF NOT EXISTS signup_verification (
    email             TEXT PRIMARY KEY,
    phone             TEXT,
    email_verified_at TEXT,
    phone_verified_at TEXT,
    updated_at        TEXT NOT NULL
);

-- 휴대폰 단독 가입용 인증 상태 (휴대폰 번호 1건당 1행)
CREATE TABLE IF NOT EXISTS phone_auth_state (
    phone       TEXT PRIMARY KEY,
    verified_at TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- 타점 비상벨: 회원이 정한 지표 조건(예: 김프 +3.0% 이상)을 넘으면 문자로 알린다.
-- 무료 회원 1인당 활성 규칙 1개(active=1) 기본 정책 — 새로 설정하면 기존 활성 규칙은 꺼진다.
CREATE TABLE IF NOT EXISTS alert_rules (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER,
    phone             TEXT NOT NULL,
    metric            TEXT NOT NULL,
    operator          TEXT NOT NULL,
    threshold         REAL NOT NULL,
    label             TEXT,
    cooldown_minutes  INTEGER NOT NULL DEFAULT 360,
    last_triggered_at TEXT,
    active            INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alert_rules_phone  ON alert_rules(phone, active);
CREATE INDEX IF NOT EXISTS idx_alert_rules_active ON alert_rules(active);

-- 타점 비상벨 발송 이력 (같은 조건의 중복 발송·실패를 사실대로 남긴다)
CREATE TABLE IF NOT EXISTS alert_logs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id        INTEGER,
    phone          TEXT NOT NULL,
    metric         TEXT NOT NULL,
    observed_value REAL,
    message        TEXT,
    status         TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alert_logs_rule ON alert_logs(rule_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alert_logs_phone ON alert_logs(phone, created_at DESC);

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
        # 중요도(S/A/B) 분류: 기존 글도 등급을 잃지 않도록 컬럼만 추가하고 아래에서 백필한다.
        if "importance" not in cols:
            conn.execute("ALTER TABLE posts ADD COLUMN importance TEXT NOT NULL DEFAULT 'B'")
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
        # 휴대폰 단독 가입: email 이 없어도 회원을 만들 수 있게 UNIQUE 제약은 남기고 NOT NULL 만 푼다.
        _make_users_email_nullable(conn)
        conn.commit()
        _backfill_importance(conn)
        _backfill_tags(conn)
    finally:
        if own:
            conn.close()


def _make_users_email_nullable(conn: sqlite3.Connection) -> None:
    """기존 DB 의 users.email NOT NULL 제약을 풀어 휴대폰 단독 회원(email NULL)을 허용한다.

    SQLite 는 컬럼 제약을 직접 바꾸지 못하므로 테이블을 새 스키마로 다시 만들고 값을 그대로 옮긴다.
    기존 이메일+휴대폰 회원의 값·id·가입일은 그대로 보존된다.
    """
    cols = conn.execute("PRAGMA table_info(users)").fetchall()
    email_col = next((c for c in cols if c[1] == "email"), None)
    if not email_col or int(email_col[3]) == 0:  # 이미 NULL 허용
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone)")
        return
    names = [c[1] for c in cols]
    quoted = ", ".join(f'"{n}"' for n in names)
    conn.execute("ALTER TABLE users RENAME TO users_legacy")
    conn.execute(
        """CREATE TABLE users (
               id              INTEGER PRIMARY KEY AUTOINCREMENT,
               email           TEXT UNIQUE,
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
           )"""
    )
    conn.execute(f"INSERT INTO users ({quoted}) SELECT {quoted} FROM users_legacy")
    conn.execute("DROP TABLE users_legacy")
    # 빈 문자열 이메일은 '이메일 없음'과 같게 본다(UNIQUE 충돌로 회원이 막히지 않도록).
    conn.execute("UPDATE users SET email = NULL WHERE TRIM(COALESCE(email, '')) = ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone)")
    conn.commit()


def _backfill_importance(conn: sqlite3.Connection) -> None:
    """중요도가 비어 있는 기존 글에 S/A/B 등급을 재계산해 채운다(사실 정보만 사용)."""
    import content  # 순환 임포트 방지: 백필 시점에만 가져온다

    rows = conn.execute(
        "SELECT id, title, topic, source_name, source_url, summary_lines FROM posts "
        "WHERE COALESCE(importance, '') NOT IN ('S', 'A', 'B')"
    ).fetchall()
    if not rows:
        return
    for row in rows:
        post = dict(row)
        try:
            post["summary_lines"] = json.loads(post.get("summary_lines") or "[]")
        except Exception:  # noqa: BLE001
            post["summary_lines"] = []
        level = content.classify_post_importance(post)
        conn.execute("UPDATE posts SET importance = ? WHERE id = ?", (level, post["id"]))
    conn.commit()


def _backfill_tags(conn: sqlite3.Connection) -> None:
    """예전 저장분의 태그 표기를 현재 노출 용어로 맞춘다.

    2026-11-27: '매크로' → '경제'. 저장된 태그는 네이버 키워드·관심자산 매칭에 쓰이므로
    과거 글도 같은 표기를 쓰게 한다(여러 번 돌아도 결과가 같다).
    """
    renames = {"매크로": "경제"}
    rows = conn.execute("SELECT id, tags FROM posts WHERE tags LIKE '%매크로%'").fetchall()
    for row in rows:
        try:
            tags = json.loads(row["tags"] or "[]")
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(tags, list) or not renames.keys() & set(tags):
            continue
        fixed = list(dict.fromkeys(renames.get(tag, tag) for tag in tags))
        conn.execute(
            "UPDATE posts SET tags = ? WHERE id = ?",
            (json.dumps(fixed, ensure_ascii=False), row["id"]),
        )
    conn.commit()


def row_to_post(row: sqlite3.Row) -> dict[str, Any]:
    post = dict(row)
    post["summary_lines"] = json.loads(post["summary_lines"] or "[]")
    post["tags"] = json.loads(post["tags"] or "[]")
    post["is_member_early"] = bool(post["is_member_early"])
    # importance 컬럼이 없는 이전 DB(읽기 전용 경로)에서도 화면이 깨지지 않도록 기본값을 채운다.
    if str(post.get("importance") or "").upper() not in config.IMPORTANCE_LEVELS:
        post["importance"] = config.IMPORTANCE_DEFAULT
    else:
        post["importance"] = str(post["importance"]).upper()
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
    importance: str | None = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM posts WHERE 1=1"
    params: list[Any] = []
    if importance in config.IMPORTANCE_LEVELS:
        sql += " AND UPPER(COALESCE(importance, ?)) = ?"
        params.extend([config.IMPORTANCE_DEFAULT, importance.upper()])
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


def confirmation_stats(conn: sqlite3.Connection, day: str | None = None) -> dict[str, Any]:
    """KST 자정 기준 '오늘 확인 건수'와, 0건일 때 함께 보여줄 최근 확인 건수.

    기준 시각은 글의 확인 시각(verified_at, +09:00)의 날짜다. 서버 시각대와 무관하게
    KST 달력 기준으로 세므로 자정 직후에도 전날 건수가 섞이지 않는다.
    """
    day = day or config.now_kst().date().isoformat()
    published = "COALESCE(status, 'published') = 'published'"
    today = int(conn.execute(
        f"SELECT COUNT(*) FROM posts WHERE {published} AND substr(verified_at, 1, 10) = ?", (day,)
    ).fetchone()[0])
    last = conn.execute(
        f"SELECT substr(verified_at, 1, 10) AS d, COUNT(*) AS n FROM posts "
        f"WHERE {published} AND substr(verified_at, 1, 10) < ? "
        f"GROUP BY d ORDER BY d DESC LIMIT 1",
        (day,),
    ).fetchone()
    total = count_posts(conn, "published")
    return {
        "date": day,
        "today": today,
        "recent_date": last["d"] if last else None,
        "recent_count": int(last["n"]) if last else 0,
        "total": total,
    }


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
    email: str | None,
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
    """회원을 만들거나 갱신한다.

    휴대폰 단독 가입을 위해 email 은 None 일 수 있다. 같은 이메일이 있으면 그 회원을,
    이메일이 없으면 같은 휴대폰 번호의 회원을 갱신해 중복 회원을 만들지 않는다.
    """
    email = (email or "").strip().lower() or None
    phone = (phone or "").strip() or None
    created_at = created_at or config.iso_kst()
    agreed_at = None
    if marketing_agreed:
        agreed_at = marketing_agreed_at or config.iso_kst()
    row = None
    if email:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if row is None and phone:
        row = conn.execute(
            "SELECT * FROM users WHERE phone = ? ORDER BY id DESC LIMIT 1", (phone,)
        ).fetchone()
    if row:
        sets: list[str] = []
        params: list[Any] = []
        # 이메일 없이 가입한 회원이 나중에 이메일을 더하면 같은 회원에 붙인다.
        if email and not (row["email"] or "").strip():
            sets.append("email = ?")
            params.append(email)
            if email_verified is None:
                sets.append("email_verified = ?")
                params.append(0)
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
            params.append(row["id"])
            conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()
        return get_user_by_id(conn, int(row["id"]))  # type: ignore[return-value]
    cur = conn.execute(
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
    return get_user_by_id(conn, int(cur.lastrowid))  # type: ignore[return-value]


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
        sql += " AND (COALESCE(email, '') LIKE ? OR COALESCE(name, '') LIKE ? OR watchlist LIKE ? OR COALESCE(phone, '') LIKE ?)"
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
        sql += " AND (COALESCE(email, '') LIKE ? OR COALESCE(name, '') LIKE ? OR watchlist LIKE ? OR COALESCE(phone, '') LIKE ?)"
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


def get_user(conn: sqlite3.Connection, email: str | None) -> dict[str, Any] | None:
    email = (email or "").strip().lower()
    if not email:
        return None
    row = conn.execute(
        "SELECT * FROM users WHERE email = ?", (email,)
    ).fetchone()
    if not row:
        return None
    user = dict(row)
    user["watchlist"] = json.loads(user["watchlist"] or "[]")
    return user


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone()
    if not row:
        return None
    user = dict(row)
    user["watchlist"] = json.loads(user["watchlist"] or "[]")
    return user


def get_user_by_phone(conn: sqlite3.Connection, phone: str | None) -> dict[str, Any] | None:
    phone = (phone or "").strip()
    if not phone:
        return None
    row = conn.execute(
        "SELECT * FROM users WHERE phone = ? ORDER BY id DESC LIMIT 1", (phone,)
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


def set_watchlist_by_id(conn: sqlite3.Connection, user_id: int, watchlist: list[str]) -> dict[str, Any]:
    """이메일 없이 가입한 회원(휴대폰 단독)의 관심자산도 저장한다."""
    conn.execute(
        "UPDATE users SET watchlist = ? WHERE id = ?",
        (json.dumps(watchlist, ensure_ascii=False), int(user_id)),
    )
    conn.commit()
    return get_user_by_id(conn, int(user_id))  # type: ignore[return-value]


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


def set_phone_verification(conn: sqlite3.Connection, phone: str, when: str | None = None) -> dict[str, Any]:
    """휴대폰 단독 가입용 인증 기록. 같은 번호는 1행만 유지하고 인증 시각을 갱신한다."""
    phone = (phone or "").strip()
    when = when or config.iso_kst()
    if not phone:
        return {}
    row = conn.execute("SELECT * FROM phone_auth_state WHERE phone = ?", (phone,)).fetchone()
    if row:
        conn.execute(
            "UPDATE phone_auth_state SET verified_at = ?, updated_at = ? WHERE phone = ?",
            (when, when, phone),
        )
    else:
        conn.execute(
            "INSERT INTO phone_auth_state (phone, verified_at, updated_at) VALUES (?,?,?)",
            (phone, when, when),
        )
    conn.commit()
    return get_phone_verification(conn, phone) or {}


def get_phone_verification(conn: sqlite3.Connection, phone: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM phone_auth_state WHERE phone = ?", ((phone or "").strip(),)
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


# ------------------------------------------------------------ 타점 비상벨

def _alert_rule_dict(row: sqlite3.Row) -> dict[str, Any]:
    rule = dict(row)
    rule["threshold"] = float(rule["threshold"])
    rule["cooldown_minutes"] = int(rule["cooldown_minutes"] or config.ALERT_COOLDOWN_MINUTES)
    rule["active"] = bool(rule["active"])
    return rule


def validate_alert_rule(metric: str, operator: str, threshold: Any) -> float:
    """지원 지표·연산자·임계치 범위를 확인하고 threshold 를 float 로 돌려준다."""
    metric = str(metric or "").strip()
    operator = str(operator or "").strip()
    if metric not in config.ALERT_METRICS:
        raise ValueError(f"지원하지 않는 지표입니다: {metric}")
    if operator not in config.ALERT_OPERATORS:
        raise ValueError("연산자는 '>=' 또는 '<=' 만 쓸 수 있습니다.")
    try:
        value = float(threshold)
    except (TypeError, ValueError):
        raise ValueError("임계값은 숫자로 입력해 주세요.") from None
    meta = config.ALERT_METRICS[metric]
    if value != value or value in (float("inf"), float("-inf")):  # NaN·inf 차단
        raise ValueError("임계값은 숫자로 입력해 주세요.")
    if value < float(meta["min"]) or value > float(meta["max"]):
        raise ValueError(
            f"{meta['label']} 임계값은 {meta['min']:g}~{meta['max']:g}{meta['unit']} 사이로 입력해 주세요."
        )
    return value


def default_alert_label(metric: str, operator: str, threshold: float) -> str:
    word = "이상" if operator == ">=" else "이하"
    return f"{config.alert_metric_label(metric)} {config.fmt_alert_value(metric, threshold)} {word}"


def upsert_user_alert_rule(
    conn: sqlite3.Connection,
    phone: str,
    metric: str,
    operator: str,
    threshold: float,
    label: str | None = None,
    cooldown_minutes: int | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    """타점 비상벨 규칙을 저장한다(무료 회원 1인당 활성 1개).

    같은 조건을 다시 저장하면 라벨만 고치고 쿨다운 기록은 그대로 둔다.
    조건(지표·연산자·임계치)이 달라지면 기존 활성 규칙을 끄고 새 규칙을 만든다.
    """
    phone = (phone or "").strip()
    if not phone:
        raise ValueError("휴대폰 번호가 필요합니다.")
    value = validate_alert_rule(metric, operator, threshold)
    operator = str(operator).strip()
    cooldown = int(cooldown_minutes or config.ALERT_COOLDOWN_MINUTES)
    cooldown = max(config.ALERT_COOLDOWN_MIN, min(config.ALERT_COOLDOWN_MAX, cooldown))
    label = (str(label).strip() if label else "") or default_alert_label(metric, operator, value)
    label = label[: config.ALERT_LABEL_MAX]
    now = config.iso_kst()

    rows = [
        _alert_rule_dict(r) for r in conn.execute(
            "SELECT * FROM alert_rules WHERE phone = ? AND active = 1 ORDER BY id DESC", (phone,)
        ).fetchall()
    ]
    same = next(
        (r for r in rows
         if r["metric"] == metric and r["operator"] == operator
         and abs(float(r["threshold"]) - value) < 1e-9),
        None,
    )
    if same:
        sets = ["label = ?", "cooldown_minutes = ?"]
        params: list[Any] = [label, cooldown]
        if user_id:
            sets.append("user_id = ?")
            params.append(int(user_id))
        params.append(int(same["id"]))
        conn.execute(f"UPDATE alert_rules SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()
        return get_alert_rule(conn, int(same["id"]))  # type: ignore[return-value]

    if rows:
        conn.execute("UPDATE alert_rules SET active = 0 WHERE phone = ? AND active = 1", (phone,))
    cur = conn.execute(
        """INSERT INTO alert_rules
               (user_id, phone, metric, operator, threshold, label,
                cooldown_minutes, last_triggered_at, active, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (int(user_id) if user_id else None, phone, metric, operator, value, label,
         cooldown, None, 1, now),
    )
    conn.commit()
    return get_alert_rule(conn, int(cur.lastrowid))  # type: ignore[return-value]


def set_alert_rules_active(conn: sqlite3.Connection, phone: str, active: bool) -> int:
    """휴대폰 기준 활성 규칙을 켜거나 끈다. 바뀐 규칙 수를 돌려준다."""
    cur = conn.execute(
        "UPDATE alert_rules SET active = ? WHERE phone = ? AND active = ?",
        (1 if active else 0, (phone or "").strip(), 0 if active else 1),
    )
    conn.commit()
    return int(cur.rowcount or 0)


def get_alert_rule(conn: sqlite3.Connection, rule_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM alert_rules WHERE id = ?", (int(rule_id),)).fetchone()
    return _alert_rule_dict(row) if row else None


def list_alert_rules_by_phone(
    conn: sqlite3.Connection, phone: str, include_inactive: bool = False
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM alert_rules WHERE phone = ?"
    if not include_inactive:
        sql += " AND active = 1"
    sql += " ORDER BY id DESC"
    return [_alert_rule_dict(r) for r in conn.execute(sql, ((phone or "").strip(),)).fetchall()]


def get_active_alert_rule(conn: sqlite3.Connection, phone: str) -> dict[str, Any] | None:
    rows = list_alert_rules_by_phone(conn, phone)
    return rows[0] if rows else None


def list_active_alert_rules(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM alert_rules WHERE active = 1 ORDER BY id"
    ).fetchall()
    return [_alert_rule_dict(r) for r in rows]


def mark_alert_rule_triggered(
    conn: sqlite3.Connection, rule_id: int, when: str | None = None
) -> dict[str, Any] | None:
    conn.execute(
        "UPDATE alert_rules SET last_triggered_at = ? WHERE id = ?",
        (when or config.iso_kst(), int(rule_id)),
    )
    conn.commit()
    return get_alert_rule(conn, int(rule_id))


def log_alert_delivery(
    conn: sqlite3.Connection,
    rule_id: int | None,
    phone: str,
    metric: str,
    observed_value: float | None,
    message: str,
    status: str,
    when: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO alert_logs
               (rule_id, phone, metric, observed_value, message, status, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (int(rule_id) if rule_id else None, phone, metric, observed_value, message,
         status, when or config.iso_kst()),
    )
    conn.commit()
    return int(cur.lastrowid)


def alert_rule_summaries(conn: sqlite3.Connection, phones: Iterable[str | None]) -> dict[str, dict[str, Any]]:
    """관리자 회원 목록용: 휴대폰 → 활성 타점 알림 요약(없으면 키 없음)."""
    wanted = [str(p).strip() for p in phones if str(p or "").strip()]
    if not wanted:
        return {}
    marks = ",".join("?" for _ in wanted)
    rows = conn.execute(
        f"""SELECT * FROM alert_rules WHERE active = 1 AND phone IN ({marks})
            ORDER BY id DESC""",
        wanted,
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        rule = _alert_rule_dict(row)
        out.setdefault(rule["phone"], {
            "id": rule["id"],
            "metric": rule["metric"],
            "metric_label": config.alert_metric_label(rule["metric"]),
            "operator": rule["operator"],
            "threshold": rule["threshold"],
            "label": rule["label"],
            "cooldown_minutes": rule["cooldown_minutes"],
            "last_triggered_at": rule["last_triggered_at"],
        })
    return out


def recent_alert_logs(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM alert_logs ORDER BY id DESC LIMIT ?", (int(limit),)
    ).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    init_db()
    with connect() as c:
        print(f"DB ready: {config.DB_PATH}")
        print("posts:", count_posts(c), "| drafts:", count_drafts(c))
