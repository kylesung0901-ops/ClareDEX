"""ClareDEX v2 → 네이버 블로그(cryptobrothers) 자동 발행.

파이프라인
  collector.py → naver_drafts.py → **naver_publish.py**
  (호스트 cron 이 07:10 · 19:10 에 collect.sh 를 호출한다)

원칙
  · 네이버는 로그인 세션 없이는 한 줄도 올릴 수 없다. 그래서 세션(storage state)이 없으면
    발행을 "시도조차 하지 않고" status="no_session" 으로 정직하게 기록한다.
  · 실제 발행에 성공한 것만 DB(naver_publish)에 published 로 남긴다. 실패·건너뜀은 사유와 함께 남긴다.
  · 결과는 data/naver_publish_state.json 과 /admin 수집기 탭에 그대로 보인다.

세션 준비 (한 번만)
  방법 1) PC 에서 네이버 로그인 후 storage state 내보내기
      python naver_publish.py --login          # 창이 열린다 → 직접 로그인 → Enter
      → data/naver_state.json 생성
      서버로 복사:  scp data/naver_state.json root@72.62.72.12:/root/claredex-v2/data/
  방법 2) 브라우저 확장으로 쿠키를 export 해 Playwright storage_state 형식으로 저장
  .env 에 NAVER_STORAGE_STATE=/app/data/naver_state.json 로 경로를 지정할 수도 있다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import config

config.load_env()

import db  # noqa: E402

BLOG_ID = (os.environ.get("NAVER_BLOG_ID") or "cryptobrothers").strip()
STATE_PATH = Path(os.environ.get("NAVER_STORAGE_STATE") or (config.DATA_DIR / "naver_state.json"))
RESULT_PATH = config.DATA_DIR / "naver_publish_state.json"
SHOT_DIR = config.DATA_DIR / "naver_shots"
EDITOR_URL = "https://blog.naver.com/{blog_id}/postwrite"
POST_VIEW = "PostView.naver"
# 이 시간보다 오래된 초안은 "새 글"이 아니므로 발행하지 않고 skipped 로 남긴다.
MAX_AGE_HOURS = float(os.environ.get("CLAREDEX_NAVER_MAX_AGE_HOURS", "24"))

STATUS_LABEL = {
    "published": "발행 완료",
    "nothing_pending": "발행할 새 글이 없음",
    "no_session": "네이버 로그인 세션 없음",
    "login_expired": "네이버 세션 만료",
    "playwright_missing": "Playwright 미설치",
    "failed": "발행 실패",
    "skipped": "건너뜀",
    "dry_run": "모의 실행",
}


def log(msg: str) -> None:
    print(msg, flush=True)


def session_info() -> dict:
    path = STATE_PATH
    exists = path.is_file()
    age_days = None
    if exists:
        age_days = round((time.time() - path.stat().st_mtime) / 86400, 1)
    return {
        "available": exists,
        "path": str(path),
        "age_days": age_days,
        "blog_id": BLOG_ID,
        "note": (
            "저장된 네이버 로그인 세션을 사용합니다."
            if exists else
            "네이버 로그인 세션이 없습니다. PC 에서 `python naver_publish.py --login` 으로 만든 "
            f"storage state 를 {path.name} 로 복사해야 실제 발행이 됩니다."
        ),
    }


def playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _save_state() -> None:
    try:
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(
            json.dumps(_STATE, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        log(f"[naver] 상태 저장 실패: {exc}")


_STATE: dict = {}


def _screenshot(page, name: str) -> str | None:
    try:
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SHOT_DIR / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.png"
        page.screenshot(path=str(path), full_page=False)
        return str(path)
    except Exception:  # noqa: BLE001
        return None


def _click_publish(page) -> str:
    """스마트에디터 ONE 의 '발행' 버튼 → 발행 설정 레이어의 최종 '발행' 까지 누른다."""
    clicked = 0
    for selector in (
        "button:has-text('발행')",
        "[class*='publish_btn'] button",
        "button[class*='publish']",
    ):
        try:
            btn = page.locator(selector).first
            if btn.count() and btn.is_visible():
                btn.click(timeout=8000)
                clicked += 1
                page.wait_for_timeout(1800)
                break
        except Exception:  # noqa: BLE001
            continue
    if not clicked:
        raise RuntimeError("발행 버튼을 찾지 못했습니다. 네이버 에디터 구조가 바뀌었을 수 있습니다.")
    # 발행 설정 레이어: 공개 범위(전체 공개) 확인 후 최종 발행
    try:
        for label in ("전체 공개", "공개"):
            opt = page.locator(f"text={label}").first
            if opt.count() and opt.is_visible():
                opt.click(timeout=4000)
                break
    except Exception:  # noqa: BLE001
        pass
    for selector in (
        "button:has-text('발행'):visible",
        "[class*='confirm'] button:has-text('발행')",
    ):
        try:
            btn = page.locator(selector).last
            if btn.count() and btn.is_visible():
                btn.click(timeout=8000)
                page.wait_for_timeout(2500)
                return "ok"
        except Exception:  # noqa: BLE001
            continue
    return "second_click_missing"


def _fill_editor(page, draft: dict) -> None:
    title = (draft.get("seo_title") or draft.get("post_title") or "").strip()[:100]
    html = draft.get("body_html") or ""
    text = draft.get("body_text") or ""

    # 제목
    title_selectors = (
        ".se-title-text .se-text-paragraph",
        ".se-title-text",
        "[data-placeholder*='제목']",
        ".se-component.se-documentTitle [contenteditable='true']",
    )
    filled = False
    for selector in title_selectors:
        try:
            el = page.locator(selector).first
            if el.count():
                el.click(timeout=6000)
                page.keyboard.insert_text(title)
                filled = True
                break
        except Exception:  # noqa: BLE001
            continue
    if not filled:
        raise RuntimeError("제목 입력란을 찾지 못했습니다.")

    page.wait_for_timeout(500)

    # 본문: HTML 을 클립보드로 붙여넣어 서식(문단·링크·이미지)을 살린다.
    body_selectors = (
        ".se-component.se-text .se-text-paragraph",
        ".se-content [contenteditable='true']",
        "[contenteditable='true']",
    )
    body_el = None
    for selector in body_selectors:
        try:
            el = page.locator(selector).last
            if el.count():
                body_el = el
                break
        except Exception:  # noqa: BLE001
            continue
    if body_el is None:
        raise RuntimeError("본문 입력란을 찾지 못했습니다.")

    body_el.click(timeout=6000)
    pasted = False
    try:
        page.evaluate(
            """async (payload) => {
                const item = new ClipboardItem({
                    'text/html': new Blob([payload.html], {type: 'text/html'}),
                    'text/plain': new Blob([payload.text], {type: 'text/plain'})
                });
                await navigator.clipboard.write([item]);
            }""",
            {"html": html, "text": text},
        )
        page.keyboard.press("Control+V")
        page.wait_for_timeout(2500)
        pasted = True
    except Exception as exc:  # noqa: BLE001
        log(f"[naver] HTML 붙여넣기 실패 → 일반 텍스트로 대체: {exc}")
    if not pasted:
        page.keyboard.insert_text(text[:4000])
        page.wait_for_timeout(1200)


def publish_draft(draft: dict, headless: bool = True) -> dict:
    """초안 1건을 네이버에 발행한다. (status, url, message) 를 담은 dict 반환."""
    from playwright.sync_api import sync_playwright

    started = time.time()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--lang=ko-KR"],
        )
        context = browser.new_context(
            storage_state=str(STATE_PATH),
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            viewport={"width": 1440, "height": 1000},
        )
        try:
            context.grant_permissions(
                ["clipboard-read", "clipboard-write"], origin="https://blog.naver.com"
            )
        except Exception:  # noqa: BLE001
            pass
        page = context.new_page()
        shot = None
        try:
            page.goto(EDITOR_URL.format(blog_id=BLOG_ID), wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
            if "nid.naver.com" in page.url:
                shot = _screenshot(page, "login-expired")
                return {"status": "login_expired", "url": None, "shot": shot,
                        "message": "네이버 로그인 화면으로 넘어갔습니다. storage state 를 다시 만들어 주세요."}
            _fill_editor(page, draft)
            result = _click_publish(page)
            if result != "ok":
                shot = _screenshot(page, "publish-unconfirmed")
                return {"status": "failed", "url": None, "shot": shot,
                        "message": "발행 설정 창의 최종 발행 버튼을 찾지 못했습니다."}
            page.wait_for_timeout(3000)
            url = page.url if POST_VIEW in page.url else None
            if not url:
                shot = _screenshot(page, "publish-unverified")
                return {"status": "failed", "url": None, "shot": shot,
                        "message": "발행 버튼은 눌렀지만 글 주소를 확인하지 못했습니다. 네이버에서 직접 확인해 주세요."}
            return {"status": "published", "url": url, "shot": None,
                    "message": f"발행 완료 ({round(time.time() - started, 1)}초)"}
        except Exception as exc:  # noqa: BLE001
            shot = _screenshot(page, "error")
            return {"status": "failed", "url": None, "shot": shot,
                    "message": f"발행 중 오류: {exc}"}
        finally:
            try:
                context.close()
                browser.close()
            except Exception:  # noqa: BLE001
                pass


def run(limit: int = 1, dry_run: bool = False, headless: bool = True) -> dict:
    global _STATE
    config.ensure_dirs()
    db.init_db()

    with db.session() as conn:
        stats = db.naver_publish_stats(conn)
        pending = db.pending_naver_drafts(conn, limit=max(1, limit))
        stale = []

    session = session_info()
    base = {
        "blog_id": BLOG_ID,
        "limit": limit,
        "pending": stats.get("pending"),
        "published_total": stats.get("published"),
        "session": session,
        "playwright": playwright_available(),
        "dry_run": dry_run,
        "items": [],
        "started_at": config.iso_kst(),
    }

    # 너무 오래된 초안은 발행하지 않고 skipped 로 정리한다 (묵은 뉴스가 블로그에 올라가지 않도록)
    fresh = []
    for draft in pending:
        created = draft.get("created_at")
        try:
            age_h = (config.now_kst() - config.parse_kst(created)).total_seconds() / 3600 if created else 0
        except Exception:  # noqa: BLE001
            age_h = 0
        if age_h > MAX_AGE_HOURS:
            stale.append(draft)
        else:
            fresh.append(draft)
    if not dry_run and stale:
        with db.session() as conn:
            for draft in stale:
                db.record_naver_publish(
                    conn, draft_id=draft["id"], post_id=draft["post_id"], status="skipped",
                    message=f"{MAX_AGE_HOURS:.0f}시간이 지난 초안이라 발행하지 않았습니다.",
                    attempted_at=config.iso_kst(),
                )
        log(f"[naver] {len(stale)}건은 {MAX_AGE_HOURS:.0f}시간이 지나 skipped 처리")
    pending = fresh
    if not pending and not stale:
        _STATE = {**base, "ok": True, "status": "nothing_pending",
                  "message": "발행할 새 글이 없습니다.", "finished_at": config.iso_kst()}
        _save_state()
        log("[naver] 발행할 새 초안이 없습니다.")
        return _STATE

    if dry_run:
        _STATE = {**base, "ok": True, "status": "dry_run",
                  "message": f"모의 실행 · 대상 {len(pending)}건",
                  "items": [{"draft_id": d["id"], "post_id": d["post_id"],
                             "title": d["seo_title"][:80]} for d in pending],
                  "finished_at": config.iso_kst()}
        _save_state()
        log(f"[naver] 모의 실행 · 대상 {len(pending)}건")
        return _STATE

    if not session["available"]:
        _STATE = {**base, "ok": True, "status": "no_session", "message": session["note"],
                  "items": [{"draft_id": d["id"], "post_id": d["post_id"], "status": "no_session"}
                            for d in pending],
                  "finished_at": config.iso_kst()}
        _save_state()
        log(f"[naver] 세션 없음 → 발행하지 않았습니다. {session['path']} 를 준비해 주세요.")
        return _STATE

    if not playwright_available():
        _STATE = {**base, "ok": True, "status": "playwright_missing",
                  "message": "Playwright 가 설치되어 있지 않습니다. requirements.txt 를 확인해 주세요.",
                  "items": [{"draft_id": d["id"], "post_id": d["post_id"], "status": "playwright_missing"}
                            for d in pending],
                  "finished_at": config.iso_kst()}
        _save_state()
        log("[naver] Playwright 미설치 → 발행 건너뜀")
        return _STATE

    items: list[dict] = []
    published = 0
    failed = 0
    for draft in pending:
        log(f"[naver] 발행 시도 #{draft['id']} · {draft['seo_title'][:60]}")
        outcome = publish_draft(draft, headless=headless)
        status = outcome["status"]
        items.append({
            "draft_id": draft["id"],
            "post_id": draft["post_id"],
            "title": (draft.get("seo_title") or "")[:80],
            "status": status,
            "url": outcome.get("url"),
            "message": outcome.get("message"),
            "shot": outcome.get("shot"),
        })
        with db.session() as conn:
            db.record_naver_publish(
                conn, draft_id=draft["id"], post_id=draft["post_id"], status=status,
                url=outcome.get("url"), message=outcome.get("message"),
                attempted_at=config.iso_kst(),
                published_at=config.iso_kst() if status == "published" else None,
            )
        if status == "published":
            published += 1
            log(f"[naver] 발행 완료 · {outcome.get('url')}")
        else:
            failed += 1
            log(f"[naver] 발행 실패({status}) · {outcome.get('message')}")
            break  # 세션 만료·구조 변경이면 다음 초안도 실패한다 → 한 번에서 멈춘다

    status = "published" if published and not failed else ("failed" if failed else "skipped")
    _STATE = {
        **base,
        "ok": failed == 0,
        "status": status,
        "attempted": len(items),
        "published": published,
        "failed": failed,
        "items": items,
        "message": (
            f"네이버 발행 {published}건 완료" if published and not failed else
            f"네이버 발행 실패 {failed}건 · {items[-1]['message'] if items else ''}"
        ),
        "finished_at": config.iso_kst(),
    }
    _save_state()
    return _STATE


def main() -> int:
    parser = argparse.ArgumentParser(description="ClareDEX v2 → 네이버 블로그 발행")
    parser.add_argument("--limit", type=int, default=int(os.environ.get("CLAREDEX_NAVER_PER_RUN", "1")))
    parser.add_argument("--dry-run", action="store_true", help="발행하지 않고 대상만 확인")
    parser.add_argument("--check", action="store_true", help="세션·Playwright 상태만 출력")
    parser.add_argument("--headful", action="store_true", help="브라우저 창을 띄워 실행")
    parser.add_argument("--login", action="store_true", help="한 번만 직접 로그인해 storage state 저장")
    args = parser.parse_args()

    if args.check:
        info = {"session": session_info(), "playwright": playwright_available(),
                "blog_id": BLOG_ID, "state_path": str(STATE_PATH)}
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    if args.login:
        return _interactive_login()

    result = run(limit=args.limit, dry_run=args.dry_run, headless=not args.headful)
    print(f"[naver] {result.get('status')} · {result.get('message')}")
    print("__RESULT__" + json.dumps(result, ensure_ascii=False, default=str))
    # 세션·Playwright 미준비는 "실패"가 아니라 "아직 발행할 수 없는 상태"다. cron 이 계속 돌도록 0 을 돌려준다.
    return 0 if result.get("status") in (
        "published", "nothing_pending", "dry_run", "no_session", "playwright_missing",
    ) else 1


def _interactive_login() -> int:
    """창을 띄워 사용자가 직접 로그인하게 하고 storage state 를 저장한다(로컬 PC 용)."""
    if not playwright_available():
        print("Playwright 가 필요합니다:  pip install playwright && python -m playwright install chromium")
        return 2
    from playwright.sync_api import sync_playwright

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(locale="ko-KR", timezone_id="Asia/Seoul")
        page = context.new_page()
        page.goto("https://nid.naver.com/nidlogin.login", wait_until="domcontentloaded")
        print("\n브라우저에서 네이버에 로그인한 뒤, 이 터미널에서 Enter 를 누르세요.")
        try:
            input()
        except EOFError:
            pass
        check = context.new_page()
        check.goto(f"https://blog.naver.com/{BLOG_ID}", wait_until="domcontentloaded")
        logged_in = "nid.naver.com" not in check.url
        context.storage_state(path=str(STATE_PATH))
        browser.close()
    if logged_in:
        print(f"저장 완료: {STATE_PATH}")
        print(f"서버로 복사:  scp {STATE_PATH.name} root@72.62.72.12:/root/claredex-v2/data/")
        return 0
    print("로그인이 확인되지 않았습니다. 저장된 상태로는 발행할 수 없습니다.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
