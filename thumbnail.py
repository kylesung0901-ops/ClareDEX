"""글마다 브랜드 썸네일 PNG(1200x630)를 만든다.

Pillow 만 사용한다. 컨테이너에 있는 Noto Sans CJK KR 폰트로 한글을 그린다.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import config

W, H = 1200, 630
CREAM = "#FCFAF6"
INK = "#16150F"
INK_SOFT = "#3E3C35"
MUTED = "#7B776B"
LINE = "#E4DFD2"
GOLD = "#A07830"

FONT_CANDIDATES = [
    "/usr/share/fonts/pickle-hwp/NotoSansCJKkr-Regular.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "C:/Windows/Fonts/malgun.ttf",
]


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    raise RuntimeError(
        "한글 폰트를 찾지 못했습니다. FONT_CANDIDATES 에 사용할 .ttf/.otf 경로를 추가하세요."
    )


def _tracked(draw: ImageDraw.ImageDraw, xy, text: str, font, fill, tracking: int = 2):
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking
    return x


def _wrap(draw, text: str, font, max_width: int, max_lines: int) -> list[str]:
    lines: list[str] = []
    cur = ""
    for word in text.split(" "):
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        cur = word
        while draw.textlength(cur, font=font) > max_width and len(cur) > 1:
            cut = len(cur)
            while cut > 1 and draw.textlength(cur[:cut], font=font) > max_width:
                cut -= 1
            lines.append(cur[:cut])
            cur = cur[cut:]
    if cur:
        lines.append(cur)

    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while draw.textlength(last + "…", font=font) > max_width and len(last) > 1:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


def render(post: dict) -> Path:
    """post(dict) -> 저장된 이미지 경로"""
    config.ensure_dirs()
    cat = config.CATEGORIES.get(post["category"], {"label": "속보", "color": INK})
    accent = cat["color"]

    img = Image.new("RGB", (W, H), CREAM)
    draw = ImageDraw.Draw(img)

    # 종이 질감 대신 아주 옅은 격자
    for x in range(0, W, 48):
        draw.line([(x, 0), (x, H)], fill="#F3EFE5", width=1)
    for y in range(0, H, 48):
        draw.line([(0, y), (W, y)], fill="#F3EFE5", width=1)

    # 상단 액센트 바
    draw.rectangle([0, 0, W, 10], fill=accent)

    f_logo = _font(27)
    f_small = _font(17)
    f_badge = _font(20)
    f_title = _font(56)
    f_meta = _font(19)
    f_meta_sm = _font(16)

    # 로고
    draw.rounded_rectangle([64, 58, 78, 72], radius=2, fill=accent)
    _tracked(draw, (92, 48), "CLAREDEX", f_logo, INK, tracking=4)
    draw.text((300, 52), "공식 원문 속보 · INVESTMENT JOURNAL", font=f_small, fill=MUTED)

    # 카테고리 배지
    badge_text = cat["label"]
    tw = draw.textlength(badge_text, font=f_badge)
    bx, by = 64, 122
    draw.rounded_rectangle([bx, by, bx + tw + 62, by + 44], radius=22, fill=accent)
    draw.ellipse([bx + 16, by + 19, bx + 23, by + 26], fill=CREAM)
    draw.text((bx + 32, by + 10), badge_text, font=f_badge, fill=CREAM)

    # 중요도 배지 (S=긴급 빨강 / A=주요 금색)
    level = str(post.get("importance") or "B").upper()
    meta = config.IMPORTANCE_LEVELS.get(level)
    if meta and level in ("S", "A"):
        label = f"{level} {meta['label']}"
        iw = draw.textlength(label, font=f_badge)
        ix = bx + tw + 62 + 12
        draw.rounded_rectangle([ix, by, ix + iw + 52, by + 44], radius=22,
                               fill=meta["color"])
        draw.text((ix + 26, by + 10), label, font=f_badge, fill=CREAM)

    # 제목 (크기 자동 축소)
    title = post["title"]
    max_w = W - 128
    lines: list[str] = []
    for size in (56, 52, 48, 44, 40):
        f_title = _font(size)
        lines = _wrap(draw, title, f_title, max_w, max_lines=3)
        widest = max((draw.textlength(l, font=f_title) for l in lines), default=0)
        if widest <= max_w and len(lines) <= 3 and sum(len(l) for l in lines) >= len(title) - 1:
            break

    y = 200
    line_h = int(f_title.size * 1.32)
    for line in lines:
        draw.text((64, y), line, font=f_title, fill=INK, stroke_width=1, stroke_fill=INK)
        y += line_h

    # 제목 아래 얇은 금색 룰 + 원문 발췌(공간이 남을 때만)
    summary = post.get("summary_lines") or []
    if len(lines) <= 2 and len(summary) > 1 and y < 390:
        draw.line([(64, y + 10), (64 + 56, y + 10)], fill=GOLD, width=3)
        f_ex = _font(20)
        excerpt = _wrap(draw, summary[1], f_ex, max_w, max_lines=2)
        ey = y + 34
        for line in excerpt:
            draw.text((64, ey), line, font=f_ex, fill=MUTED)
            ey += 31

    # 구분선
    draw.line([(64, 470), (W - 64, 470)], fill=LINE, width=2)

    # 좌: 출처 / 등급
    grade = post.get("source_grade", "공식")
    draw.text((64, 496), post.get("source_name", ""), font=f_meta, fill=INK_SOFT)
    gw = draw.textlength(grade, font=f_meta_sm)
    draw.rounded_rectangle([64 + draw.textlength(post.get("source_name", ""), font=f_meta) + 16, 495,
                            64 + draw.textlength(post.get("source_name", ""), font=f_meta) + 16 + gw + 26, 527],
                           radius=16, fill="#F0EAD9")
    draw.text((64 + draw.textlength(post.get("source_name", ""), font=f_meta) + 29, 500),
              grade, font=f_meta_sm, fill=GOLD)

    # 우: 확인 시각
    verified = post.get("verified_at", "")
    try:
        vlabel = config.fmt_kst(config.parse_kst(verified), "%Y.%m.%d %H:%M KST")
    except Exception:
        vlabel = verified
    draw.text((W - 64 - draw.textlength(f"확인 {vlabel}", font=f_meta), 496),
              f"확인 {vlabel}", font=f_meta, fill=INK_SOFT)

    # 하단 고지
    disclaimer = "본 카드는 공식 원문 확인 시점 기준 정보입니다. 투자 판단의 책임은 투자자 본인에게 있습니다."
    draw.text((64, 560), disclaimer, font=f_meta_sm, fill=MUTED)
    draw.text((W - 64 - draw.textlength("claredex.com", font=f_meta_sm), 560),
              "claredex.com", font=f_meta_sm, fill=MUTED)

    out = config.IMAGE_DIR / f"{post['id']}.png"
    img.save(out, "PNG", optimize=True)
    return out


def size_of(path: Path) -> tuple[int, int]:
    with Image.open(path) as im:
        return im.size


if __name__ == "__main__":
    import sys

    if "--all" in sys.argv:
        import db

        with db.session() as conn:
            for row in db.list_posts(conn):
                path = render(row)
                conn.execute("UPDATE posts SET image_path = ? WHERE id = ?", (f"/images/posts/{row['id']}.png", row["id"]))
                print(f"  {row['id']}.png ← {row['title'][:50]}")
            conn.commit()
        print("썸네일 재생성 완료")
    else:
        sample = {
            "id": 0,
            "title": "연준, FOMC 의사록 공개 (10월 27~28일 회의, 2026)",
            "category": "macro",
            "source_name": "미 연방준비제도(Fed)",
            "source_grade": "1차 공식",
            "verified_at": config.iso_kst(),
            "summary_lines": ["", "Minutes of the Federal Open Market Committee, October 27-28, 2026", ""],
        }
        p = render(sample)
        print(p, size_of(p))
