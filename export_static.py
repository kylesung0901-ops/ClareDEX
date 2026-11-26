"""정적 호스팅(GitHub Pages)용 JSON 스냅샷 내보내기.

`public/data/` 에 아래 파일을 만든다. 각 파일의 구조는 server.py 의 해당 API 응답과 같아서
프론트엔드가 API 대신 이 파일을 그대로 읽어도 화면이 동일하게 그려진다.

  posts.json         ← GET /api/posts?limit=60 (중요도 S/A/B 포함)
  ticker.json        ← GET /api/ticker (마지막 수집 스냅샷)
  calendar.json      ← GET /api/calendar
  stats.json         ← GET /api/stats (KST 기준 오늘 확인 건수)

**네이버 초안(`naver-drafts.json`)은 일부러 내보내지 않는다.**
초안은 운영자 내부 자료이고 `/api/naver-drafts` 는 로그인 없이는 401 이므로, 공개 정적
호스팅에 스냅샷을 두면 누구나 내려받을 수 있게 된다. 이전 버전이 남긴 파일도 지운다.
운영자는 실제 서버(`claredex.com/admin`)에서 로그인해 사용한다.

이미지 경로는 GitHub Pages 하위 경로(`/claredex-v2/`)에서도 동작하도록 상대 경로
(`images/posts/N.png`)로 기록한다.
"""
from __future__ import annotations

import json
from pathlib import Path

import config
import db

OUT_DIR = config.PUBLIC_DIR / "data"

# 공개 정적 호스팅에 두면 안 되는 파일 (이전 배포본이 남긴 것까지 제거)
FORBIDDEN_EXPORTS = ("naver-drafts.json",)


def rel_image(path: str | None) -> str | None:
    """'/images/posts/1.png' → 'images/posts/1.png' (하위 경로 호스팅 대응)."""
    if not path:
        return None
    return path.lstrip("/")


def post_json(post: dict) -> dict:
    out = dict(post)
    out["category_label"] = config.CATEGORIES.get(post["category"], {}).get("label", post["category"])
    level = str(post.get("importance") or config.IMPORTANCE_DEFAULT).upper()
    if level not in config.IMPORTANCE_LEVELS:
        level = config.IMPORTANCE_DEFAULT
    out["importance"] = level
    out["importance_label"] = config.IMPORTANCE_LEVELS[level]["label"]
    out["image_path"] = rel_image(post.get("image_path"))
    out["image_url"] = out["image_path"]  # 정적 미리보기: 페이지 기준 상대 경로
    return out


def write(name: str, payload: dict) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def purge_forbidden() -> list[Path]:
    removed = []
    for name in FORBIDDEN_EXPORTS:
        path = OUT_DIR / name
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed


def main() -> int:
    db.init_db()
    removed = purge_forbidden()
    with db.session() as conn:
        posts = db.list_posts(conn, limit=60)
        stats = db.today_stats(conn)
        levels = db.importance_counts(conn)

    write("posts.json", {"ok": True, "count": len(posts), "posts": [post_json(p) for p in posts]})
    write("stats.json", {"ok": True, "today": stats, "importance": levels, "timezone": "Asia/Seoul"})

    ticker: dict
    try:
        ticker = json.loads(config.MARKET_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        ticker = {"ok": False, "items": [], "error": "market_cache.json 없음"}
    ticker["stale"] = True
    ticker["served_from"] = "snapshot"
    ticker["note"] = "정적 미리보기: 마지막으로 수집한 시장지표 스냅샷입니다."
    write("ticker.json", ticker)

    calendar = config.load_calendar()
    write("calendar.json", {"ok": True, **calendar})

    print(f"내보내기 완료 → {OUT_DIR}")
    print(f"  posts.json        {len(posts)}건 (S {levels['S']} · A {levels['A']} · B {levels['B']})")
    print(f"  ticker.json       {len(ticker.get('items') or [])}항목")
    print(f"  calendar.json     {len(calendar.get('events') or [])}일정")
    print(f"  stats.json        오늘 확인 {stats['count']}건 · 전체 {stats['total']}건")
    print("  naver-drafts.json 내보내지 않음 (운영자 전용 · 서버 로그인 후 조회)")
    for path in removed:
        print(f"  삭제: {path.name} (공개 정적 호스팅 노출 방지)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
