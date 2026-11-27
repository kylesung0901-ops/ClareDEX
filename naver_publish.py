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


PUBLISH_LABEL = "발행"
# 라이브 진단(2026-11-27) 상단 버튼 목록: ['예약 발행 0건', '저장', '1', '발행', ...]
# `button:has-text('발행')` 부분 일치는 이 목록의 첫 버튼 '예약 발행 0건' 을 먼저 집어 발행에 실패했다.
# → '발행' 과 정확히 일치하는 버튼만 고르고, '예약 발행' 이 들어간 버튼은 후보에서 뺀다.
PUBLISH_BLOCKLIST = ("예약 발행", "예약발행")

EDITOR_POPUP_TEXT = "작성 중인 글"  # 임시저장 복구 팝업: "작성 중인 글이 있습니다"
# 2026-11-27 라이브 진단: 팝업 제목은 STRONG.se-popup-title, 버튼은 .se-popup-container 안의 '취소'.
# 같은 문구가 숨은 템플릿에도 여러 벌 있어 is_visible() 로 보이는 것만 누른다.
EDITOR_POPUP_MARKERS = (
    ".se-popup-button-cancel",
    ".se-popup-title",
    "[class*='popup'] [class*='popup-title']",
)
EDITOR_POPUP_CANCEL = (
    ".se-popup-button-cancel",
    ".se-popup-container button:has-text('취소')",
    ".se-popup-container :text-is('취소')",
    "[class*='popup'] button:text-is('취소')",
    "[class*='popup'] button:has-text('취소')",
    "[class*='dialog'] button:text-is('취소')",
    "button:text-is('취소')",
)
HELP_PANEL_CLOSE = (
    ".se-help-panel-close-button",
    "[class*='help_panel'] [class*='close']",
    "[class*='help-panel'] [class*='close']",
    "[class*='help'] button[class*='close']",
    ".se-help-panel [class*='close']",
)
# 에디터 본체가 그려졌는지 판단하는 신호 (제목란·편집 가능 영역)
EDITOR_READY_SELECTORS = (
    ".se-title-text",
    "[data-placeholder*='제목']",
    ".se-component.se-documentTitle",
    "[contenteditable='true']",
)
# 제목란 후보 — 라이브 DOM 은 DIV.se-module.se-title-text 자체가 편집 영역이다.
TITLE_SELECTORS = (
    ".se-title-text .se-text-paragraph",
    ".se-title-text",
    "[data-placeholder*='제목']",
    ".se-component.se-documentTitle [contenteditable='true']",
)
# 본문 후보 — 제목란을 제외하고 고른다.
BODY_SELECTORS = (
    ".se-component.se-text .se-text-paragraph",
    ".se-main-container .se-text-paragraph",
    ".se-content [contenteditable='true']",
    "[contenteditable='true']",
)


def _label_of(el) -> str:
    """버튼의 눈에 보이는 글자. 읽지 못하면 빈 문자열."""
    try:
        return (el.inner_text(timeout=1500) or "").replace("\n", " ").strip()
    except Exception:  # noqa: BLE001
        return ""


def _pick_publish_button(page, *, last: bool = False, exclude_box: dict | None = None):
    """'발행' 과 정확히 일치하는 보이는 버튼 하나를 고른다. '예약 발행' 은 절대 고르지 않는다.

    last=False: 상단 '발행' — 정확 일치 → 클래스 → data-click-area 순서의 첫 버튼.
    last=True : 발행 설정 레이어의 최종 '발행' — confirm 클래스/레이어 한정 선택자를 먼저 보고,
                없으면 정확 일치 버튼 중 마지막을 고른다.
    exclude_box 를 주면 같은 자리에 있는 버튼(이미 누른 상단 '발행')은 건너뛴다.
    """
    blocked = list(PUBLISH_BLOCKLIST) + (["취소", "닫기"] if last else [])
    candidates: list[tuple[str, object]] = []
    if last:
        for selector in (
            "button[class*='confirm_btn']",
            "button[class*='confirm-btn']",
            "[class*='layer'] button:text-is('발행')",
            "[class*='publish_layer'] button:text-is('발행')",
        ):
            try:
                candidates.append((selector, page.locator(selector)))
            except Exception:  # noqa: BLE001
                continue
    try:
        candidates.append(("role=button[name=발행,exact]",
                           page.get_by_role("button", name=PUBLISH_LABEL, exact=True)))
    except Exception:  # noqa: BLE001
        pass
    for selector in (
        "button:text-is('발행')",
        "button[class*='publish_btn']",
        "button[class*='publish-btn']",
        "button[data-click-area='tpb.publish']",
    ):
        try:
            candidates.append((selector, page.locator(selector)))
        except Exception:  # noqa: BLE001
            continue

    for name, locator in candidates:
        try:
            total = locator.count()
        except Exception:  # noqa: BLE001
            continue
        chosen = None
        for index in range(min(total, 25)):
            el = locator.nth(index)
            try:
                if not el.is_visible():
                    continue
            except Exception:  # noqa: BLE001
                continue
            if any(bad in _label_of(el) for bad in blocked):
                continue
            if exclude_box is not None:
                try:
                    box = el.bounding_box()
                except Exception:  # noqa: BLE001
                    box = None
                if box and all(abs(box[k] - exclude_box[k]) < 1 for k in ("x", "y", "width", "height")):
                    continue
            chosen = el
            if not last:
                break
        if chosen is not None:
            return name, chosen
    return None, None


def _visible_elements(page, selector: str, limit: int = 5) -> list:
    """선택자에 걸린 요소 중 지금 화면에 보이는 것만 돌려준다(숨은 템플릿 제외)."""
    out = []
    try:
        loc = page.locator(selector)
        total = min(loc.count(), limit)
    except Exception:  # noqa: BLE001
        return out
    for index in range(total):
        try:
            el = loc.nth(index)
            if el.is_visible():
                out.append(el)
        except Exception:  # noqa: BLE001
            continue
    return out


def _robust_click(el, timeout: int = 4000) -> str:
    """일반 클릭 → force 클릭 → JS 클릭 순서로 시도하고 쓴 방법을 돌려준다. 다 막히면 빈 문자열."""
    try:
        el.click(timeout=timeout)
        return "click"
    except Exception:  # noqa: BLE001
        pass
    try:
        el.click(timeout=timeout, force=True)
        return "force"
    except Exception:  # noqa: BLE001
        pass
    try:
        el.evaluate("(e) => e.click()")
        return "js"
    except Exception:  # noqa: BLE001
        return ""


def _dom_focus_end(el) -> bool:
    """DOM focus 로 커서를 요소 끝에 둔다.

    오버레이·도움말 패널은 마우스 클릭만 막고 DOM focus 는 막지 못한다 → 클릭이 계속 막힐 때의 확실한 수단.
    """
    try:
        return bool(el.evaluate(
            """(e) => {
                const doc = e.ownerDocument || document;
                let target = e;
                if (!target.isContentEditable) {
                    target = e.querySelector(
                        "[contenteditable='true'], [contenteditable=''], .se-text-paragraph, p"
                    ) || e;
                }
                try { target.focus(); } catch (err) {}
                try {
                    const range = doc.createRange();
                    range.selectNodeContents(target);
                    range.collapse(false);
                    const sel = doc.defaultView.getSelection();
                    sel.removeAllRanges();
                    sel.addRange(range);
                } catch (err) {}
                const active = doc.activeElement;
                return active === target || target.contains(active) || (active && active.contains(target));
            }"""
        ))
    except Exception:  # noqa: BLE001
        return False


def _same_or_inside(el, other) -> bool:
    """두 요소가 같거나 한쪽이 다른 쪽을 품고 있는지(제목란을 본문 후보에서 빼기 위한 검사).

    ElementHandle 을 JS 인자로 넘기면 직렬화되어 비교가 실패했다 → page 쪽에 표시를 남기고
    후보 요소에서 그 표시와의 관계를 본다(요소 속성은 건드리지 않는다).
    """
    try:
        other.evaluate("(e) => { window.__pickleExclude = e; }")
        return bool(el.evaluate(
            """(e) => {
                const t = window.__pickleExclude;
                return !!t && (e === t || e.contains(t) || t.contains(e));
            }"""
        ))
    except Exception:  # noqa: BLE001
        return False


def _has_text(el, text: str) -> bool:
    probe = (text or "").strip()[:24]
    if not probe:
        return True
    try:
        return probe in (el.inner_text(timeout=2000) or "")
    except Exception:  # noqa: BLE001
        return False


def _focused_editable(page) -> bool:
    """지금 키보드 포커스가 편집 가능한 요소에 있는지. 클릭이 오버레이에 먹혔는지 판별한다."""
    try:
        return bool(page.evaluate(
            """() => {
                const a = document.activeElement;
                return !!(a && (a.isContentEditable ||
                    (a.getAttribute && a.getAttribute('contenteditable') !== null)));
            }"""
        ))
    except Exception:  # noqa: BLE001
        return False


def _page_text_len(page) -> int:
    try:
        return int(page.evaluate("() => ((document.body && document.body.innerText) || '').length"))
    except Exception:  # noqa: BLE001
        return 0


def _popup_visible(page) -> bool:
    """임시저장 복구 팝업이 아직 떠 있는지.

    아무 팝업(.se-popup-container)이나 잡으면 발행 설정 레이어의 '취소'를 눌러 버릴 수 있어,
    복구 팝업에만 있는 신호(제목 문구·취소 버튼 클래스)로 판단한다.
    """
    for selector in EDITOR_POPUP_MARKERS:
        if _visible_elements(page, selector, limit=3):
            return True
    return bool(_visible_elements(page, f"text={EDITOR_POPUP_TEXT}", limit=3))


def _dismiss_popups(page) -> list[str]:
    """에디터 진입을 막는 임시저장 복구 팝업·도움말 패널을 닫는다. 없으면 조용히 넘어간다.

    '취소' 는 복구하지 않고 새 글로 시작한다는 뜻 — 자동 발행에는 빈 에디터가 필요하다.
    클릭이 오버레이에 막히면 force 클릭·JS 클릭까지 쓰고, 그래도 남으면 Esc 로 닫는다.
    """
    closed: list[str] = []

    def _cancel_candidates() -> list[tuple[str, object]]:
        found: list[tuple[str, object]] = []
        for selector in EDITOR_POPUP_CANCEL:
            for el in _visible_elements(page, selector, limit=4):
                found.append((selector, el))
        try:
            role_btn = page.get_by_role("button", name="취소", exact=True)
            for index in range(min(role_btn.count(), 4)):
                el = role_btn.nth(index)
                if el.is_visible():
                    found.append(("role=button[취소]", el))
        except Exception:  # noqa: BLE001
            pass
        return found

    for _attempt in range(3):
        if not _popup_visible(page):
            break
        acted = False
        for selector, el in _cancel_candidates():
            how = _robust_click(el, 3000)
            if how:
                page.wait_for_timeout(900)
                closed.append(f"임시저장 팝업 취소({selector} · {how})")
                acted = True
                break
        if not acted:
            break

    if _popup_visible(page):
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(700)
            closed.append("임시저장 팝업 Esc")
        except Exception:  # noqa: BLE001
            pass

    # 도움말 패널: 제목란 위를 덮고 있으면 클릭이 막힌다.
    for selector in HELP_PANEL_CLOSE:
        visible = _visible_elements(page, selector, limit=3)
        if not visible:
            continue
        how = _robust_click(visible[0], 3000)
        if how:
            page.wait_for_timeout(500)
            closed.append(f"도움말 패널 닫기({selector} · {how})")
            break

    if _popup_visible(page):
        log("[naver] 임시저장 팝업이 아직 남아 있습니다 → 제목란은 DOM focus 로 잡습니다.")
    if closed:
        log("[naver] 에디터 팝업 정리 · " + ", ".join(closed))
    return closed


def _wait_editor_ready(page, timeout_ms: int = 20000) -> bool:
    """에디터 본체(제목란·편집 영역)가 그려질 때까지 잠깐 기다린다."""
    deadline = time.time() + timeout_ms / 1000
    while True:
        for selector in EDITOR_READY_SELECTORS:
            if _visible_elements(page, selector, limit=3):
                return True
        if time.time() >= deadline:
            return False
        page.wait_for_timeout(400)


def _find_title(page, timeout_ms: int = 15000):
    """제목란 요소와 그 선택자를 돌려준다. 없으면 (None, '')."""
    deadline = time.time() + timeout_ms / 1000
    while True:
        for selector in TITLE_SELECTORS:
            visible = _visible_elements(page, selector, limit=5)
            if visible:
                return visible[0], selector
        if time.time() >= deadline:
            return None, ""
        page.wait_for_timeout(500)


def _find_body(page, exclude=None, timeout_ms: int = 10000):
    """본문 입력란(제목란 제외)과 그 선택자를 돌려준다. 없으면 (None, '')."""
    deadline = time.time() + timeout_ms / 1000
    while True:
        for selector in BODY_SELECTORS:
            visible = _visible_elements(page, selector, limit=25)
            if exclude is not None:
                visible = [el for el in visible if not _same_or_inside(el, exclude)]
            if visible:
                return visible[-1], selector
        if time.time() >= deadline:
            return None, ""
        page.wait_for_timeout(500)


def _type_into(page, el, text: str, *, allow_force: bool = True) -> str:
    """제목란에 글자를 넣는다: 일반 클릭 → force 클릭 → DOM focus 순서.

    클릭이 오버레이에 먹히면 포커스가 엉뚱한 곳으로 가므로, 클릭 뒤 포커스가 편집 영역인지
    먼저 확인한 뒤에만 입력한다(팝업의 '확인' 등을 잘못 누르지 않게).
    """
    modes = [("click", False)]
    if allow_force:
        modes.append(("force", True))
    for mode, force in modes:
        try:
            el.click(timeout=6000, force=force)
        except Exception:  # noqa: BLE001
            continue
        page.wait_for_timeout(300)
        if not _focused_editable(page):
            continue
        page.keyboard.insert_text(text)
        page.wait_for_timeout(400)
        if _has_text(el, text):
            return mode
    if _dom_focus_end(el):
        page.keyboard.insert_text(text)
        page.wait_for_timeout(400)
        if _has_text(el, text):
            return "dom-focus"
    return ""


def _click_publish(page) -> str:
    """스마트에디터 ONE 의 '발행' 버튼 → 발행 설정 레이어의 최종 '발행' 까지 누른다."""
    _dismiss_popups(page)
    name, btn = _pick_publish_button(page)
    if btn is None:
        # 도움말 패널·팝업이 버튼을 가리고 있을 수 있어 한 번 더 정리하고 재시도한다.
        _dismiss_popups(page)
        name, btn = _pick_publish_button(page)
    if btn is None:
        raise RuntimeError("발행 버튼을 찾지 못했습니다. 네이버 에디터 구조가 바뀌었을 수 있습니다.")
    try:
        top_box = btn.bounding_box()
    except Exception:  # noqa: BLE001
        top_box = None
    log(f"[naver] 발행 버튼 클릭 · {name} · '{_label_of(btn)}'")
    try:
        btn.click(timeout=8000)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"발행 버튼 클릭 실패({name}): {exc}") from exc
    page.wait_for_timeout(1800)

    # 발행 설정 레이어: 공개 범위(전체 공개) 확인 후 최종 발행
    try:
        for label in ("전체 공개", "공개"):
            opt = page.get_by_text(label, exact=True).first
            if opt.count() and opt.is_visible():
                opt.click(timeout=4000)
                break
    except Exception:  # noqa: BLE001
        pass

    # 최종 발행 버튼: 정확 일치 우선. 레이어가 늦게 뜨는 경우를 대비해 최대 8초 기다린다.
    confirm = None
    confirm_name = ""
    deadline = time.time() + 8
    while True:
        confirm_name, confirm = _pick_publish_button(page, last=True, exclude_box=top_box)
        if confirm is not None or time.time() >= deadline:
            break
        page.wait_for_timeout(500)
    if confirm is None:
        return "second_click_missing"
    log(f"[naver] 최종 발행 버튼 클릭 · {confirm_name} · '{_label_of(confirm)}'")
    try:
        confirm.click(timeout=8000)
    except Exception as exc:  # noqa: BLE001
        log(f"[naver] 최종 발행 버튼 클릭 실패 · {exc}")
        return "second_click_missing"
    page.wait_for_timeout(2500)
    return "ok"


def _paste_body(page, el, html: str, text: str) -> str:
    """본문 입력란을 잡고 HTML 을 클립보드로 붙여넣는다. 본문이 비면 일반 텍스트로 대체한다."""
    how = _robust_click(el, 6000)
    focused = _focused_editable(page)
    if not how or not focused:
        if _dom_focus_end(el):
            how = f"{how}+dom-focus" if how else "dom-focus"
    page.wait_for_timeout(300)

    before = _page_text_len(page)
    want = max(8, min(len(text or ""), 4000) * 0.4)
    if html:
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
            if _page_text_len(page) - before >= want:
                return f"{how}+clipboard"
            log("[naver] 클립보드 붙여넣기 후 본문이 비어 있음 → 일반 텍스트로 대체")
        except Exception as exc:  # noqa: BLE001
            log(f"[naver] HTML 붙여넣기 실패 → 일반 텍스트로 대체: {exc}")

    if not _focused_editable(page):
        _dom_focus_end(el)
    if not _focused_editable(page):
        # 붙여넣기 과정에서 문단이 다시 그려졌을 수 있어 본문 입력란을 한 번 더 찾아 포커스를 준다.
        refound, _ref_sel = _find_body(page, None, timeout_ms=4000)
        if refound is not None:
            _dom_focus_end(refound)
    page.keyboard.insert_text((text or "")[:4000])
    page.wait_for_timeout(1200)
    return f"{how}+text"


def _set_title_value(el, title: str) -> None:
    """마지막 수단: 에디터에 직접 값과 input 이벤트를 넣는다(클릭이 계속 막힐 때)."""
    el.evaluate(
        """(e, value) => {
            let target = e;
            if (!target.isContentEditable) {
                target = e.querySelector(
                    "[contenteditable='true'], [contenteditable=''], .se-text-paragraph, p"
                ) || e;
            }
            try { target.focus(); } catch (err) {}
            target.textContent = value;
            target.dispatchEvent(new InputEvent('input', {
                bubbles: true, inputType: 'insertText', data: value
            }));
        }""",
        title,
    )


def _fill_editor(page, draft: dict) -> None:
    title = (draft.get("seo_title") or draft.get("post_title") or "").strip()[:100]
    html = draft.get("body_html") or ""
    text = draft.get("body_text") or ""

    # 1) 본체가 그려지기 전에 팝업을 건드리면 아무것도 못 닫는다 → 제목란이 보일 때까지 잠깐 기다린다.
    if not _wait_editor_ready(page, timeout_ms=20000):
        log("[naver] 에디터 제목란이 늦게 뜹니다 → 팝업 정리 후 다시 확인")

    # 2) 임시저장 복구 팝업·도움말 패널이 떠 있으면 제목·본문 클릭이 막힌다 → 먼저 확실히 닫는다.
    _dismiss_popups(page)
    if not _wait_editor_ready(page, timeout_ms=15000):
        raise RuntimeError("네이버 에디터가 열리지 않았습니다(제목란 없음).")

    # 3) 제목 — 팝업이 남아 있으면 force 클릭은 팝업 버튼을 눌러 버릴 수 있으니 DOM focus 로 잡는다.
    title_el, title_selector = _find_title(page, timeout_ms=15000)
    if title_el is None:
        raise RuntimeError("제목 입력란을 찾지 못했습니다.")
    how = _type_into(page, title_el, title, allow_force=not _popup_visible(page))
    if how:
        log(f"[naver] 제목 입력 · {title_selector} · {how}")
    else:
        try:
            _set_title_value(title_el, title)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"제목 입력란 클릭 실패({title_selector}): {exc}") from exc
        page.wait_for_timeout(400)
        log(f"[naver] 제목 입력 · {title_selector} · js-value")

    page.wait_for_timeout(500)

    # 4) 본문 — 제목란과 겹치지 않는 편집 영역을 고른다. 없으면 제목에서 Enter 로 첫 문단을 만든다.
    body_el, body_selector = _find_body(page, title_el, timeout_ms=8000)
    if body_el is None:
        _dom_focus_end(title_el)
        page.keyboard.press("Enter")
        page.wait_for_timeout(1400)
        body_el, body_selector = _find_body(page, title_el, timeout_ms=14000)
    if body_el is None:
        raise RuntimeError("본문 입력란을 찾지 못했습니다.")

    how = _paste_body(page, body_el, html, text)
    log(f"[naver] 본문 입력 · {body_selector} · {how} · {len(text)}자")


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
