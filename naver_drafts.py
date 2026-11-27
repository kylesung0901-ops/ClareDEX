"""네이버 블로그 SEO 초안 생성기.

- 글마다 SEO 제목 / 키워드 5~8개 / 본문 HTML·텍스트 / 텔레그램·웹 CTA 를 만든다.
- 본문 이미지는 https://t.me/Clare_PerpDEX 로 연결되는 클릭 링크로 감싼다.
- CTA 문구는 최근 10개 초안과 difflib.SequenceMatcher 로 비교해 가장 겹치지 않는 조합을 고른다.
- 하단에 투자 유의 고지(디스클레이머)를 항상 포함한다. 신고 상태를 단정하는 문구는 넣지 않는다.
"""
from __future__ import annotations

import argparse
import difflib
import sys

import config

config.load_env()

import content  # noqa: E402
import db  # noqa: E402

INTRO = {
    "fomc-minutes": "{source}가 공개한 FOMC 의사록을 원문에서 직접 확인했습니다. 아래 내용은 확인된 사실만 정리한 것입니다.",
    "fomc-statement": "{source}가 발표한 FOMC 성명을 원문에서 확인했습니다. 확인된 내용만 정리했습니다.",
    "enforcement": "{source}가 공개한 제재·시정조치 발표를 원문에서 확인했습니다.",
    "survey": "{source}가 공개한 조사 결과를 원문에서 확인했습니다.",
    "payments": "{source}가 공개한 지급결제 관련 발표를 원문에서 확인했습니다.",
    "regulation": "{source}가 공개한 금융규제 관련 발표를 원문에서 확인했습니다.",
    "general-macro": "{source}가 공개한 공식 자료를 원문에서 확인했습니다.",
    "kr-macro": "{source}가 공개한 보도자료를 원문에서 확인했습니다. 확인된 내용과 수치만 정리했습니다.",
    "us-macro": "{source}가 발표한 공식 자료를 원문에서 확인했습니다. 수치와 발표 시각은 원문 그대로입니다.",
    "market-shock": "{source}의 실시간 수치가 클레어덱스 기준선을 넘어 정리했습니다. 관측값과 시각을 그대로 적었습니다.",
    "earnings": "{source}에 접수된 실적 관련 공시를 확인했습니다. 수치는 원문에서 직접 확인하실 수 있습니다.",
    "filing": "{source}에 접수된 공시를 확인했습니다. 세부 내용은 원문 링크에서 확인하실 수 있습니다.",
}

H2 = 'style="font-size:20px;font-weight:700;margin:28px 0 10px;color:#16150f;"'
P = 'style="font-size:16px;line-height:1.8;color:#2f2d28;margin:0 0 14px;"'
LI = 'style="font-size:16px;line-height:1.8;color:#2f2d28;margin:0 0 8px;"'
# 링크는 문단과 분리해 앞뒀 문단과 빈 줄 간격을 두고 한 줄로 보여준다
LINK_P = 'style="font-size:16px;line-height:1.8;margin:22px 0 26px;"'


def _normalize(text: str) -> str:
    return "".join(text.split())


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def pick_cta(post: dict, recent: list[dict]) -> tuple[str, str, float]:
    topic = post.get("topic", "filing")
    telegram_variants = content.TELEGRAM_CTA.get(topic) or content.TELEGRAM_CTA["filing"]
    web_variants = content.WEB_CTA.get(topic) or content.WEB_CTA["filing"]
    recent_blob = [f"{d['telegram_cta']} {d['web_cta']}" for d in recent]

    best = (telegram_variants[0], web_variants[0], 1.0)
    for tg in telegram_variants:
        for web in web_variants:
            blob = f"{tg} {web}"
            worst = max((_similarity(blob, prev) for prev in recent_blob), default=0.0)
            if worst < best[2]:
                best = (tg, web, worst)
    return best


def build_draft(post: dict, recent: list[dict]) -> dict:
    verified = config.parse_kst(post["verified_at"])
    published = config.parse_kst(post["published_at"]) if post.get("published_at") else None
    topic = post.get("topic", "filing")
    intro = INTRO.get(topic, INTRO["filing"]).format(source=post["source_name"])
    meta_line = (
        (f"원문 발표 {config.fmt_kst(published, '%Y-%m-%d %H:%M')} KST · " if published else "")
        + f"클레어덱스 확인 {config.fmt_kst(verified, '%Y-%m-%d %H:%M')} KST"
    )

    seo_title = content.seo_title_for(post)
    keywords = content.keywords_for(post)
    telegram_cta, web_cta, sim = pick_cta(post, recent)

    image_abs = f"{config.PUBLIC_BASE_URL}{post.get('image_path') or ''}"
    alt = post["title"].replace('"', "'")

    summary_html = "".join(f"<li {LI}>{line}</li>" for line in post["summary_lines"])
    source_html = (
        f'<p {P}>{post["source_name"]} · {post["source_grade"]} · 확인 시각 '
        f'{config.fmt_kst(verified, "%Y-%m-%d %H:%M")} KST</p>'
        f'<p {LINK_P}><a href="{post["source_url"]}" target="_blank" rel="noopener">원문 링크 열기 ↗</a></p>'
    )
    image_html = (
        f'<div style="text-align:center;margin:26px 0 8px;">'
        f'<a href="{config.TELEGRAM_URL}" target="_blank" rel="noopener">'
        f'<img src="{image_abs}" alt="{alt}" style="max-width:100%;border-radius:12px;border:1px solid #e6e1d5;">'
        f"</a></div>"
        f'<p style="font-size:13px;color:#8a877c;text-align:center;margin:0 0 24px;">'
        f"이미지를 누르면 클레어덱스 텔레그램 채널로 이동합니다.</p>"
    )

    body_html = (
        '<div style="font-family:\'Apple SD Gothic Neo\',\'Malgun Gothic\',sans-serif;">'
        f"<p {P}>{intro}</p>"
        f'<p style="font-size:12.5px;color:#8a877c;margin:-6px 0 16px;">{meta_line}</p>'
        f"<h2 {H2}>핵심 3줄 요약</h2>"
        f"<ul style=\"margin:0 0 18px;padding-left:18px;\">{summary_html}</ul>"
        f"{image_html}"
        f"<h2 {H2}>시장 영향 한 줄 해석</h2>"
        f"<p {P}>{post['market_impact']}</p>"
        f"<h2 {H2}>원문 출처</h2>"
        f"{source_html}"
        f"<h2 {H2}>이런 소식, 더 빠르게 받아보세요</h2>"
        f"<p {P}>{telegram_cta}</p>"
        f'<p {LINK_P}><a href="{config.TELEGRAM_URL}" target="_blank" rel="noopener">텔레그램 채널 열기 ↗</a></p>'
        f"<p {P}>{web_cta}</p>"
        f'<p {LINK_P}><a href="{config.WEB_URL}" target="_blank" rel="noopener">클레어덱스 웹에서 보기 ↗</a></p>'
        f"{content.COMPLIANCE_HTML}"
        "</div>"
    )

    body_text = "\n".join(
        [
            intro,
            meta_line,
            "",
            "[핵심 3줄 요약]",
            *[f"- {line}" for line in post["summary_lines"]],
            "",
            "[시장 영향 한 줄 해석]",
            post["market_impact"],
            "",
            "[원문 출처]",
            f"{post['source_name']} · {post['source_grade']} · 확인 {config.fmt_kst(verified, '%Y-%m-%d %H:%M')} KST",
            "",
            post["source_url"],
            "",
            "[텔레그램]",
            telegram_cta,
            "",
            config.TELEGRAM_URL,
            "",
            "[클레어덱스]",
            web_cta,
            "",
            config.WEB_URL,
            "",
            f"{config.DISCLAIMER}",
        ]
    )

    return {
        "post_id": post["id"],
        "seo_title": seo_title,
        "keywords": keywords,
        "body_html": body_html,
        "body_text": body_text,
        "telegram_cta": telegram_cta,
        "web_cta": web_cta,
        "image_path": post.get("image_path"),
        "created_at": config.iso_kst(),
        "_similarity": sim,
    }


def run(rebuild: bool = False) -> dict:
    db.init_db()
    conn = db.connect()
    if rebuild:
        conn.execute("DELETE FROM naver_drafts")
        conn.commit()
        print("[초안] 기존 초안을 모두 지우고 다시 생성합니다.")

    posts = db.posts_without_drafts(conn)
    if not posts:
        total = db.count_drafts(conn)
        conn.close()
        print(f"[초안] 새로 만들 글이 없습니다. (전체 초안 {total}건)")
        return {"created": 0, "total": total}

    created = 0
    worst_sim = 0.0
    for post in posts:
        recent = db.recent_drafts(conn, 10)
        draft = build_draft(post, recent)
        sim = draft.pop("_similarity")
        worst_sim = max(worst_sim, sim)
        db.insert_draft(conn, draft)
        created += 1
        print(f"  + post #{post['id']} → 초안 «{draft['seo_title']}» · CTA 유사도 {sim:.2f}")

    total = db.count_drafts(conn)
    conn.close()
    print("")
    print("=" * 72)
    print(f"초안 생성 완료 · 신규 {created}건 · 전체 {total}건 · 최근 10건 대비 CTA 최대 유사도 {worst_sim:.2f}")
    print("=" * 72)
    if worst_sim > 0.8:
        print("[경고] 일부 CTA 문구가 최근 글과 0.80을 넘습니다. 문구 라이브러리 보강이 필요합니다.")
    return {"created": created, "total": total, "worst_similarity": worst_sim}


def main() -> int:
    parser = argparse.ArgumentParser(description="네이버 블로그 SEO 초안 생성기")
    parser.add_argument("--rebuild", action="store_true", help="기존 초안을 지우고 전체 재생성")
    args = parser.parse_args()
    run(rebuild=args.rebuild)
    return 0


if __name__ == "__main__":
    sys.exit(main())
