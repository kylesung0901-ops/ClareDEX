"""클레어덱스 네이버 블로그 상단 배너 생성기.

- 1920x380: 원본/보관용 (brief 요구 경로)
- 966x290: 네이버 리모콘 '타이틀 직접등록' 규격(가로 966px, 세로 50~600px)
"""
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

INK = (15, 23, 42)        # #0F172A
IVORY = (250, 248, 245)   # #FAF8F5
EMERALD = (5, 150, 105)   # #059669
GOLD = (198, 168, 108)
MUTED = (168, 178, 196)

SANS = "/usr/share/fonts/pickle-hwp/NotoSansCJKkr-Regular.otf"
SERIF = "/usr/share/fonts/pickle-hwp/NotoSerifCJKkr-Regular.otf"
OUT = Path(__file__).resolve().parent.parent / "reports"


def spaced(d, xy, text, font, fill, tracking=0.0):
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=font, fill=fill)
        x += d.textlength(ch, font=font) + tracking
    return x


def chart(d, W, H, pad):
    cx0, cx1 = W * 0.60, W - pad
    cy0, cy1 = H * 0.32, H * 0.72
    for k in range(3):
        gy = cy0 + (cy1 - cy0) * k / 2
        d.line([(cx0, gy), (cx1, gy)], fill=(33, 43, 62), width=1)
    pts = [(0.0, 0.85), (0.14, 0.62), (0.28, 0.70), (0.42, 0.44),
           (0.56, 0.52), (0.70, 0.28), (0.84, 0.34), (1.0, 0.10)]
    poly = [(cx0 + (cx1 - cx0) * x, cy1 - (cy1 - cy0) * (1 - y)) for x, y in pts]
    d.line(poly, fill=(9, 62, 48), width=max(4, W // 190), joint="curve")
    d.line(poly, fill=EMERALD, width=max(1, W // 480), joint="curve")
    r = max(3, W // 240)
    d.ellipse([poly[-1][0] - r, poly[-1][1] - r, poly[-1][0] + r, poly[-1][1] + r], fill=EMERALD)
    f = ImageFont.truetype(SANS, max(9, H // 29))
    d.text((cx0, cy0 - H // 16), "PRIMARY SOURCE INTELLIGENCE", font=f, fill=(112, 124, 146))


def build(W, H, path, t, s, k, pad_x, pad_y, with_chart=True):
    img = Image.new("RGB", (W, H), INK)
    d = ImageDraw.Draw(img)
    for i in range(H):
        v = i / (H - 1)
        sh = int(8 * (1 - abs(v - 0.5) * 2))
        d.line([(0, i), (W, i)], fill=(INK[0] + sh, INK[1] + sh, INK[2] + sh))
    d.rectangle([pad_x * 0.4, pad_y * 0.6, W - pad_x * 0.4, H - pad_y * 0.6],
                outline=(52, 62, 84), width=1)
    d.rectangle([pad_x, int(H * 0.30), pad_x + max(2, W // 320), int(H * 0.72)], fill=EMERALD)
    if with_chart:
        chart(d, W, H, pad_x)

    f_kick = ImageFont.truetype(SANS, k)
    f_title = ImageFont.truetype(SERIF, t)
    f_sub = ImageFont.truetype(SANS, s)
    f_small = ImageFont.truetype(SANS, max(9, k - 2))

    x0 = pad_x + int(k * 2.2)
    y = int(pad_y * 0.8)
    spaced(d, (x0, y), "INVESTMENT JOURNAL", f_kick, GOLD, tracking=k * 0.40)
    y += int(k * 2.0)
    end = spaced(d, (x0, y), "CLAREDEX", f_title, IVORY, tracking=t * 0.10)
    cy = y + int(t * 0.33)
    rr = max(4, t // 13)
    d.ellipse([end + 8, cy - rr, end + 8 + 2 * rr, cy + rr], fill=EMERALD)
    y += int(t * 1.36)
    d.text((x0, y), "공식 원문 기반 실시간 속보 · 매크로·공시 인사이트", font=f_sub, fill=(216, 221, 230))

    d.line([(x0, H - pad_y), (W - pad_x, H - pad_y)], fill=(44, 54, 74), width=1)
    txt = "claredex.com"
    w = d.textlength(txt, font=f_small)
    d.text((W - pad_x - w, H - pad_y + 8), txt, font=f_small, fill=MUTED)
    d.ellipse([W - pad_x - w - 16, H - pad_y + 13, W - pad_x - w - 8, H - pad_y + 21], fill=EMERALD)

    img.save(path, "PNG", optimize=True)
    print(path, img.size)


if __name__ == "__main__":
    build(966, 290, OUT / "naver-blog-header-banner-966x290.png", 66, 17, 13, 58, 46)
    build(1920, 380, OUT / "naver-blog-header-banner.png", 92, 33, 21, 92, 62)
