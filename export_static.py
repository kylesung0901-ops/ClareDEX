"""정적 호스팅(GitHub Pages)용 JSON 스냅샷 내보내기.

`public/data/` 에 아래 파일을 만든다. 각 파일의 구조는 server.py 의 해당 API 응답과 같아서
프론트엔드가 API 대신 이 파일을 그대로 읽어도 화면이 동일하게 그려진다.

  posts.json         ← GET /api/posts?limit=60
  ticker.json        ← GET /api/ticker (마지막 수집 스냅샷)
  calendar.json      ← GET /api/calendar
  naver-drafts.json  ← GET /api/naver-drafts

이미지 경로는 GitHub Pages 하위 경로(`/claredex-v2/`)에서도 동작하도록 상대 경로
(`images/posts/N.png`)로 기록한다.
"""
from __future__ import annotations

import json
from pathlib import Path

import config
import db

OUT_DIR = config.PUBLIC_DIR / "data"


def rel_image(path: str | None) -> str | None:
    """'/images/posts/1.png' → 'images/posts/1.png' (하위 경로 호스팅 대응)."""
    if not path:
        return None
    return path.lstrip("/")


def post_json(post: dict) -> dict:
    out = dict(post)
    out["category_label"] = config.CATEGORIES.get(post["category"], {}).get("label", post["category"])
    out["image_path"] = rel_image(post.get("image_path"))
    out["image_url"] = out["image_path"]  # 정적 미리보기: 페이지 기준 상대 경로
    return out


def write(name: str, payload: dict) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> int:
    db.init_db()
    with db.session() as conn:
        posts = db.list_posts(conn, limit=60)
        drafts = db.list_drafts(conn)

    write("posts.json", {"ok": True, "count": len(posts), "posts": [post_json(p) for p in posts]})

    for d in drafts:
        d["image_url"] = rel_image(d.get("image_path"))
        d["image_local"] = rel_image(d.get("image_path"))
        d["telegram_url"] = config.TELEGRAM_URL
        d["web_url"] = config.WEB_URL
    write("naver-drafts.json", {"ok": True, "count": len(drafts), "drafts": drafts})

    ticker: dict
    try:
        ticker = json.loads(config.MARKET_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        ticker = {"ok": False, "items": [], "error": "market_cache.json 없음"}
    ticker["stale"] = True
    ticker["served_from"] = "snapshot"
    ticker["note"] = "정적 미리보기: 마지막으로 수집한 시장지표 스냅샷입니다."
    write("ticker.json", ticker)

    try:
        calendar = json.loads(config.CALENDAR_PATH.read_text(encoding="utf-8"))
    except Exception:
        calendar = {"events": []}
    write("calendar.json", {"ok": True, **calendar})

    print(f"내보내기 완료 → {OUT_DIR}")
    print(f"  posts.json        {len(posts)}건")
    print(f"  naver-drafts.json {len(drafts)}건")
    print(f"  ticker.json       {len(ticker.get('items') or [])}항목")
    print(f"  calendar.json     {len(calendar.get('events') or [])}일정")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
