"""원문에서 확인한 사실만으로 글의 제목·요약·태그·해석 문구를 만든다.

원칙
- 원문에 없는 숫자·인용·전망을 만들어 넣지 않는다.
- 해석은 "영향을 줄 수 있다" 수준의 조건문으로만 쓰고, 매수·매도 권유를 하지 않는다.
- 한국어 제목을 확신할 수 없으면 원문 제목을 그대로 인용한다.
"""
from __future__ import annotations

import hashlib
import re

import config

# ---------------------------------------------------------------- 공통


def sha256_url(url: str) -> str:
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _short(text: str, limit: int) -> str:
    text = _clean(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------- 중요도 등급


def importance_label(level: str) -> str:
    return config.IMPORTANCE_LEVELS.get(level, config.IMPORTANCE_LEVELS[config.IMPORTANCE_DEFAULT])["label"]


def subject_particle(name: str) -> str:
    """한글 이름 뒤 주격 보조사(은/는)를 받침 여부로 고른다."""
    text = _clean(name)
    if not text:
        return "는"
    last = text[-1]
    if "가" <= last <= "힣":
        return "은" if (ord(last) - 0xAC00) % 28 else "는"
    return "은" if last.isdigit() or last.isalpha() else "는"


# 연준 보도자료 URL 의 경로 접두어는 발표 유형을 그대로 보여준다(추측이 아니라 원문 주소 기반).
FED_URL_KINDS = {
    "bcreg": "은행 규제",
    "enforce": "제재·시정조치",
    "monetary": "통화정책",
    "orders": "은행 지주회사 명령",
    "other": "기타 발표",
    "press": "보도자료",
    "bcmin": "은행 규제",
}

# 지엽적 행정·행사성 발표는 B(일반)로 둔다.
NARROW_FED_KEYWORDS = (
    "mortgage", "exemption", "criteria", "fee schedule", "fees for", "dollar thresholds",
    "holiday", "challenge", "award", "winners", "appoint", "names new",
    "obituary", "in memoriam",
)


def fed_kind_from_url(url: str) -> str:
    m = re.search(r"/pressreleases/([a-z]+)\d", url or "")
    if m:
        return FED_URL_KINDS.get(m.group(1), "보도자료")
    return "보도자료"


def fed_importance(topic: str, url: str = "", title: str = "") -> str:
    """FOMC·CPI·고용 같은 시장 전반 이벤트는 S/A, 지엽적 행정 발표는 B."""
    if topic in ("fomc-statement", "fomc-minutes"):
        return "S"
    if topic in ("discount-rate-minutes", "loan-thresholds", "appraisal-exemption"):
        return "B"
    lowered = (title or "").lower()
    if any(k in lowered for k in NARROW_FED_KEYWORDS):
        return "B"
    if topic == "regulation":
        # 자본·스트레스테스트·최종규칙처럼 업권 비용을 바꾸는 규제는 A.
        if any(k in lowered for k in ("capital", "stress test", "final rule")):
            return "A"
        return "B"
    if topic == "enforcement":
        # 개별 기관 제재는 업권 이슈로, 시장 전체 S/A 보다는 B.
        return "B"
    return "B"


def sec_importance(form: str, items: list[str]) -> str:
    """빅테크 실적 공시(8-K 2.02)는 S, 정기보고서·주요 수시공시는 A."""
    if form == "8-K" and "2.02" in items:
        return "S"
    if form in ("10-Q", "10-K"):
        return "A"
    if form == "8-K" and any(code in ("1.01", "2.01", "2.03", "3.01", "4.02", "5.01", "5.02") for code in items):
        return "A"
    return "B"


DART_MAJOR_KEYWORDS = (
    "잠정", "실적", "유상증자", "무상증자", "전환사채", "신주인수권", "교환사채",
    "최대주주", "횡령", "배임", "상장폐지", "불성실", "감사의견", "합병", "분할",
)


def dart_importance(report: str) -> str:
    text = report or ""
    if any(k in text for k in DART_MAJOR_KEYWORDS):
        return "A"
    return "B"


def classify_post_importance(post: dict) -> str:
    """이미 저장된 글(제목·주제·요약)에서 등급을 다시 계산한다(마이그레이션·백필용)."""
    topic = str(post.get("topic") or "")
    title = str(post.get("title") or "")
    source = str(post.get("source_name") or "")
    if topic in ("fomc-statement", "fomc-minutes") or "연준" in source:
        return fed_importance(topic, str(post.get("source_url") or ""), title)
    if "SEC" in source:
        form = ""
        for candidate in ("10-K", "10-Q", "8-K"):
            if candidate in title:
                form = candidate
                break
        items: list[str] = []
        for line in post.get("summary_lines") or []:
            if "신고 항목" in line:
                labels = [part.strip() for part in line.split("신고 항목:", 1)[-1].split(",")]
                reverse = {label: code for code, label in FORM8K_ITEMS.items()}
                items = [reverse[label] for label in labels if label in reverse]
        return sec_importance(form, items)
    if "DART" in source:
        return dart_importance(title)
    return config.IMPORTANCE_DEFAULT


# ---------------------------------------------------------------- 연준 RSS

FED_RULES: list[tuple[str, list[str], str, str]] = [
    # (topic, keywords, 한국어 요지, 화면용 주제키)
    ("fomc-minutes", ["minutes of the federal open market committee"], "연준, FOMC 의사록 공개", "fomc-minutes"),
    ("discount-rate-minutes", ["discount rate meetings"], "연준, 지역연준 할인율 회의 의사록 공개", "discount-rate-minutes"),
    ("loan-thresholds", ["truth in lending", "consumer leasing"], "연준, 소비자금융 규제 기준 금액 발표", "loan-thresholds"),
    ("appraisal-exemption", ["smaller loan exemption", "appraisal requirements"], "연준, 소액 대출 감정평가 면제 기준 발표", "appraisal-exemption"),
    ("loan-thresholds", ["dollar thresholds"], "연준, 소액 대출 면제 기준 금액 발표", "loan-thresholds"),
    ("fomc-statement", ["fomc statement", "statement on monetary policy"], "연준, FOMC 성명 발표", "fomc-statement"),
    ("enforcement", ["enforcement action"], "연준, 금융기관 제재·시정조치 발표", "enforcement"),
    ("survey", ["survey"], "연준, 금융기관 대상 조사 결과 공개", "survey"),
    ("payments", ["pricing", "payment services", "check services"], "연준, 지급결제 서비스 요금·제도 발표", "payments"),
    ("award", ["challenge winners", "award"], "연준, College Fed Challenge 수상자 발표", "general-macro"),
    ("regulation", ["guidance", "proposal", "comment period", "final rule", "regulation", "capital", "stress test"],
     "연준, 금융규제 관련 발표", "regulation"),
    ("speech", ["speech", "remarks"], "연준 인사 연설·발언 공개", "general-macro"),
    ("appointment", ["appoint", "names new"], "연준, 인사 발표", "general-macro"),
]

FED_IMPACT = {
    "fomc-minutes": (
        "금리 경로를 가늠하는 문구가 담기는 문서로, 달러·미 국채 10년물 금리·나스닥 성장주·비트코인의 "
        "할인율 가정에 직접 영향을 줄 수 있습니다. 위원별 금리 전망 표현의 변화가 확인 포인트입니다."
    ),
    "fomc-statement": (
        "정책금리 결정과 성명 문구가 함께 나오는 발표로, 달러·미 국채금리·나스닥·비트코인의 "
        "할인율 가정이 이 문구에 반응할 수 있습니다. 금리 결정과 향후 경로 문구를 함께 봐야 합니다."
    ),
    "enforcement": (
        "특정 금융기관에 대한 제재·시정조치로, 그 기관과 같은 업권(은행·지주)의 규제 비용 인식에 "
        "영향을 줄 수 있습니다. 제재 대상과 사유가 원문에 적힌 그대로인지가 확인 포인트입니다."
    ),
    "survey": (
        "은행의 유동성·자금 관리 행태를 보여주는 조사로, 단기 자금시장(SOFR·단기 국채)과 "
        "지급준비금 해석의 참고 자료가 됩니다. 은행·금융주 투자 심리에 간접적으로 닿을 수 있습니다."
    ),
    "payments": (
        "지급결제·수수료 제도 관련 발표로, 은행·핀테크·결제대행 업권의 비용 구조에 "
        "영향을 줄 수 있습니다. 적용 시점과 대상 기관이 확인 포인트입니다."
    ),
    "regulation": (
        "금융규제 방향과 적용 시점을 담은 발표로, 은행·금융지주·핀테크의 자본 비용과 "
        "밸류에이션 가정에 영향을 줄 수 있습니다. 최종규칙인지 제안 단계인지가 핵심입니다."
    ),
    "general-macro": (
        "연준이 공개한 공식 자료로, 달러·금리·위험자산 심리에 닿을 수 있습니다. "
        "다만 발표 유형(규제·조사·지급결제·행사)에 따라 시장 파급력이 크게 달라집니다."
    ),
    "discount-rate-minutes": (
        "지역연준이 권고한 할인율(discount rate) 결정을 담은 의사록으로, 연준의 유동성 공급 창구 "
        "금리 운용을 보여줍니다. 단기 자금시장과 은행 조달 비용 해석의 참고 자료입니다."
    ),
    "loan-thresholds": (
        "소비자신용·리스 거래에 규제가 적용되는 기준 금액을 정하는 행정적 발표로, "
        "카드·캐피탈·은행 소비자금융의 실무 기준에 닿습니다. 주가·금리 방향에 대한 직접 영향은 제한적입니다."
    ),
    "appraisal-exemption": (
        "고가 주택담보대출의 감정평가 요건을 면제하는 소액 대출 기준을 정하는 발표로, "
        "모기지·주택담보대출 업권의 실무 기준에 닿습니다. 주가·금리 방향에 대한 직접 영향은 제한적입니다."
    ),
}

# 요약 3번째 줄: 문서 유형별로 '원문에서 무엇을 확인해야 하는지'를 구체적으로 적는다.
# ("세부 내용과 수치는 원문에서 확인하세요" 같은 반복 안내문은 쓰지 않는다)
FED_DETAIL = {
    "fomc-minutes": (
        "의사록은 회의에서 논의된 정책금리 경로와 경기 판단을 담는 문서입니다. "
        "금리 인하·동결·인상 논의의 강도와 위원별 전망 표현이 원문에서 확인할 항목입니다."
    ),
    "fomc-statement": (
        "성명서는 정책금리 결정과 함께 향후 금리 경로에 대한 위원회 문구를 담습니다. "
        "직전 회의 성명과 달라진 문장이 원문에서 확인할 항목입니다."
    ),
    "enforcement": (
        "제재·시정조치 발표는 대상 기관, 사유, 부과된 조치를 담습니다. "
        "해당 기관명과 조치 내용이 원문에서 확인할 항목입니다."
    ),
    "survey": (
        "조사 결과는 은행의 유동성·자금 관리 행태에 대한 응답을 담습니다. "
        "조사 대상과 응답 항목이 원문에서 확인할 항목입니다."
    ),
    "payments": (
        "지급결제 관련 발표는 수수료·서비스 제도와 적용 시점을 담습니다. "
        "적용 대상과 시행 일자가 원문에서 확인할 항목입니다."
    ),
    "regulation": (
        "금융규제 발표는 규제 내용과 적용 범위, 최종규칙·제안 단계 여부를 담습니다. "
        "의견수렴 기간과 시행 시점이 원문에서 확인할 항목입니다."
    ),
    "general-macro": (
        "연준 보도자료는 은행 규제·조사·지급결제·행사 등 여러 유형이 있습니다. "
        "원문 주소의 발표 유형과 제목에 적힌 주제가 확인할 항목입니다."
    ),
    "discount-rate-minutes": (
        "의사록은 각 지역연준이 권고한 할인율과 그 배경을 담습니다. "
        "지역별 권고 수준과 반대 의견 여부가 원문에서 확인할 항목입니다."
    ),
    "loan-thresholds": (
        "발표는 소비자신용·리스 거래에 규제가 적용되는 기준 금액과 시행 시점을 담습니다. "
        "기준 금액과 시행일이 원문에서 확인할 항목입니다."
    ),
    "appraisal-exemption": (
        "발표는 고가 주택담보대출의 감정평가 요건을 면제하는 소액 대출 기준 금액과 적용 시점을 담습니다. "
        "기준 금액과 시행일이 원문에서 확인할 항목입니다."
    ),
}


def classify_fed(title: str) -> tuple[str, str]:
    lowered = title.lower()
    for topic, keywords, gist, key in FED_RULES:
        if any(k in lowered for k in keywords):
            return gist, key
    return "연준 공식 보도자료", "general-macro"


def fed_post(item: dict, verified_at: str) -> dict:
    title = _clean(item.get("title"))
    gist, topic = classify_fed(title)
    published = item.get("published_kst")  # datetime
    published_iso = config.iso_kst(published) if published else None
    published_label = config.fmt_kst(published, "%Y-%m-%d %H:%M") if published else "확인 불가"

    extra = ""
    if topic in ("fomc-minutes", "fomc-statement"):
        m = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:-(\d{1,2}))?,?\s*(\d{4})", title)
        if m:
            month = MONTH_KO.get(m.group(1))
            span = m.group(2) + ("~" + m.group(3) if m.group(3) else "")
            extra = f" ({month}월 {span}일 회의, {m.group(4)})"

    summary = [
        (f"미 연방준비제도(Fed)가 {published_label} KST에 보도자료를 공개했습니다."
         if published else
         "미 연방준비제도(Fed)가 공식 보도자료를 공개했습니다(발표 시각은 원문에서 확인 필요)."),
        f"원문 제목: {_short(title, 160)}",
        FED_DETAIL.get(topic, FED_DETAIL["general-macro"]),
    ]
    return {
        "title": f"{gist}{extra}",
        "category": "macro",
        "source_name": "미 연방준비제도(Fed)",
        "source_grade": config.SOURCE_GRADES["primary"],
        "source_url": item["link"],
        "source_hash": sha256_url(item["link"]),
        "verified_at": verified_at,
        "summary_lines": summary,
        "market_impact": FED_IMPACT.get(topic, FED_IMPACT["general-macro"]),
        "tags": _fed_tags(topic),
        "published_at": published_iso,
        "topic": topic,
        "importance": fed_importance(topic, item["link"], title),
    }


def _fed_tags(topic: str) -> list[str]:
    base = ["매크로", "연준", "금리"]
    if topic in ("fomc-minutes", "fomc-statement"):
        base += ["FOMC", "나스닥", "BTC"]
    elif topic == "enforcement":
        base += ["은행", "규제"]
    elif topic == "regulation":
        base += ["규제", "금융주"]
    elif topic == "payments":
        base += ["은행", "핀테크"]
    elif topic == "discount-rate-minutes":
        base += ["은행", "유동성"]
    elif topic == "loan-thresholds":
        base += ["규제", "은행"]
    elif topic == "appraisal-exemption":
        base += ["규제", "모기지"]
    elif topic == "survey":
        base += ["은행"]
    return list(dict.fromkeys(base))


# ---------------------------------------------------------------- SEC EDGAR

SEC_COMPANIES = [
    {"cik": "0000320193", "name": "Apple Inc.", "name_ko": "애플", "ticker": "AAPL", "exchange": "Nasdaq", "sector": "미국주식"},
    {"cik": "0001045810", "name": "NVIDIA Corporation", "name_ko": "엔비디아", "ticker": "NVDA", "exchange": "Nasdaq", "sector": "반도체"},
]

MONTH_KO = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}

FORM_META = {
    "8-K": ("수시공시", "기업의 주요 사건을 확인되는 즉시 공시하는 서식입니다."),
    "10-Q": ("분기 보고서", "분기 재무제표와 경영진 논의(MD&A)를 담은 정기 보고서입니다."),
    "10-K": ("연간 보고서", "연간 재무제표와 사업 구조·위험 요인을 담은 정기 보고서입니다."),
}

FORM8K_ITEMS = {
    "1.01": "주요 계약 체결",
    "1.05": "사이버 보안 사고",
    "2.01": "자산 취득·처분 완료",
    "2.02": "실적 및 재무 상태",
    "2.03": "직접 금융채무 발생",
    "2.04": "우발채무 발생",
    "2.05": "구조조정 비용",
    "3.01": "상장 유지 요건 미충족 통지",
    "3.02": "미등록 증권 판매",
    "4.02": "이전 재무제표 신뢰 불가",
    "5.01": "통제권 변경",
    "5.02": "임원·이사 선임 또는 사임",
    "5.03": "정관·회계연도 변경",
    "5.07": "주주총회 표결 결과",
    "7.01": "Regulation FD 공개",
    "8.01": "기타 주요 사건",
    "9.01": "재무제표 및 첨부서류",
}

SEC_IMPACT = {
    "2.02": "실적 관련 수시공시로, 공개된 매출·이익이 시장 기대와 비교되며 주가에 직접 영향을 줄 수 있습니다.",
    "5.02": "경영진 변동 공시로, 전략의 연속성에 대한 시장 판단에 영향을 줄 수 있습니다.",
    "1.01": "주요 계약 공시로, 향후 매출과 비용 구조 해석에 영향을 줄 수 있습니다.",
    "2.03": "자금조달 관련 공시로, 재무구조와 이자비용 해석에 영향을 줄 수 있습니다.",
    "7.01": "기업이 자발적으로 공개한 정보로, 해당 종목과 동종 업종 심리에 영향을 줄 수 있습니다.",
    "8.01": "기업이 공개한 주요 사항으로, 해당 종목의 수급에 영향을 줄 수 있습니다.",
    "10-Q": "분기 실적과 재무 상태를 보여주는 정기 보고서로, 직전 분기 대비 변화가 주가에 영향을 줄 수 있습니다.",
    "10-K": "연간 사업 구조와 위험 요인을 담은 보고서로, 중장기 밸류에이션 가정에 영향을 줄 수 있습니다.",
}

# 요약 3번째 줄: 서식·항목별로 '원문에서 무엇을 확인해야 하는지'를 구체적으로 적는다.
SEC_DETAIL = {
    "2.02": "2.02(실적 및 재무 상태) 항목은 매출·이익 등 실적과 재무 상태를 다루는 공시입니다. 첨부된 실적 자료의 수치가 직전 분기와 비교할 대상입니다.",
    "1.01": "1.01(주요 계약 체결) 항목은 계약 상대·기간·금액을 담습니다. 계약 규모와 매출 인식 시점이 원문에서 확인할 항목입니다.",
    "2.01": "2.01(자산 취득·처분 완료) 항목은 거래 대상과 규모를 담습니다. 거래 금액과 손익 반영 시점이 원문에서 확인할 항목입니다.",
    "2.03": "2.03(직접 금융채무 발생) 항목은 조달 금액과 조건을 담습니다. 금리·만기가 재무비용 해석의 기준입니다.",
    "3.01": "3.01(상장 유지 요건 미충족 통지) 항목은 통지 사유와 시정 기간을 담습니다. 거래 정지·상장폐지 가능성이 확인할 항목입니다.",
    "4.02": "4.02(이전 재무제표 신뢰 불가) 항목은 재작성 대상 기간과 사유를 담습니다. 회계 처리 변경 범위가 확인할 항목입니다.",
    "5.01": "5.01(통제권 변경) 항목은 새 최대주주와 지분율을 담습니다. 경영진 교체 여부가 확인할 항목입니다.",
    "5.02": "5.02(임원·이사 선임 또는 사임) 항목은 인사와 사유를 담습니다. 후임·권한 변화가 확인할 항목입니다.",
    "7.01": "7.01(Regulation FD) 항목은 기업이 선택 공개한 정보를 담습니다. 공개 경로와 내용이 확인할 항목입니다.",
    "8.01": "8.01(기타 주요 사건) 항목은 위 항목에 속하지 않는 주요 사항을 담습니다. 사건의 성격과 규모가 확인할 항목입니다.",
    "10-Q": "10-Q는 분기 재무제표와 경영진 논의(MD&A)를 담습니다. 매출·영업이익·현금흐름의 직전 분기 대비 변화가 확인할 항목입니다.",
    "10-K": "10-K는 연간 재무제표와 사업 구조·위험 요인을 담습니다. 부문별 매출과 위험 요인 문구의 전년 대비 변화가 확인할 항목입니다.",
    "8-K": "8-K는 주요 사건을 수시로 공시하는 서식입니다. 신고 항목과 첨부 서류가 원문에서 확인할 항목입니다.",
}


def sec_index_url(cik: str, accession: str) -> str:
    cik_int = str(int(cik))
    nodash = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{nodash}/{accession}-index.htm"


def sec_post(company: dict, filing: dict, verified_at: str) -> dict:
    form = filing["form"]
    form_ko, form_desc = FORM_META.get(form, ("공시", "미 SEC에 제출된 공시 서류입니다."))
    items = [i.strip() for i in (filing.get("items") or "").split(",") if i.strip()]
    item_labels = [FORM8K_ITEMS.get(i, f"항목 {i}") for i in items]
    report_date = filing.get("reportDate") or "-"
    filing_date = filing.get("filingDate") or "-"
    acceptance = filing.get("acceptanceDateTime") or ""
    published = None
    if acceptance:
        from datetime import datetime, timezone

        published = datetime.fromisoformat(acceptance.replace("Z", "+00:00")).astimezone(config.KST)
    published_label = config.fmt_kst(published, "%Y-%m-%d %H:%M") if published else f"{filing_date} (접수일)"

    impact_key = form
    if form == "8-K" and items:
        for code in items:
            if code in SEC_IMPACT:
                impact_key = code
                break
    name_ko = company.get("name_ko") or company["name"]
    sector = company.get("sector") or "미국주식"
    sector_phrase = f"{sector} 업종" if sector != "미국주식" else "미국주식 종목"

    summary = [
        f"{company['name']}({company['ticker']})가 {filing_date} 미 SEC에 {form}({form_ko})를 제출했습니다.",
        f"접수번호 {filing.get('accessionNumber')} · 회계기준일 {report_date}"
        + (f" · 신고 항목: {', '.join(item_labels)}" if item_labels else "")
        + f" · 접수 시각 {published_label} KST",
        SEC_DETAIL.get(impact_key) or SEC_DETAIL.get(form) or SEC_DETAIL["8-K"],
    ]
    return {
        "title": f"{name_ko}({company['ticker']}), {form} {form_ko} 제출 ({filing_date})",
        "category": "us-stock",
        "source_name": "미 SEC EDGAR",
        "source_grade": config.SOURCE_GRADES["filing"],
        "source_url": sec_index_url(company["cik"], filing["accessionNumber"]),
        "source_hash": sha256_url(sec_index_url(company["cik"], filing["accessionNumber"])),
        "verified_at": verified_at,
        "summary_lines": summary,
        "market_impact": (
            f"{name_ko}({company['ticker']}){subject_particle(name_ko)} {sector_phrase}으로, 이 공시가 같은 업종 종목과 "
            f"나스닥 성장주 수급에 영향을 줄 수 있습니다. "
            + SEC_IMPACT.get(impact_key, SEC_IMPACT.get(form, "개별 기업 공시로, 해당 종목과 동종 업종 심리에 영향을 줄 수 있습니다."))
        ),
        "tags": list(dict.fromkeys([company["ticker"], "나스닥", "미국주식", sector, "공시"])),
        "published_at": config.iso_kst(published) if published else None,
        "topic": "earnings" if form == "8-K" and "2.02" in items else "filing",
        "importance": sec_importance(form, items),
    }


# ---------------------------------------------------------------- OpenDART (키 있을 때만)


def dart_post(row: dict, verified_at: str) -> dict:
    corp = _clean(row.get("corp_name"))
    report = _clean(row.get("report_nm"))
    rcept_no = _clean(row.get("rcept_no"))
    rcept_dt = _clean(row.get("rcept_dt"))
    url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
    published = None
    if re.fullmatch(r"\d{8}", rcept_dt):
        from datetime import datetime

        published = datetime.strptime(rcept_dt, "%Y%m%d").replace(tzinfo=config.KST)
    if "정기" in report or "사업보고서" in report or "분기보고서" in report or "반기보고서" in report:
        detail_line = ("정기보고서는 직전 보고서 대비 매출·영업이익과 재무 상태 변화를 담습니다. "
                       "부문별 실적과 부채비율 변화가 원문에서 확인할 항목입니다.")
        impact_line = (f"국내 상장사 {corp}의 정기보고서로, 같은 업종 종목과 코스피 수급에 "
                       f"영향을 줄 수 있습니다. 매출·영업이익의 직전 보고서 대비 변화가 확인 포인트입니다.")
    elif "주요사항" in report:
        detail_line = ("주요사항보고서는 공시 사유와 금액·일정을 담습니다. "
                       "거래 금액과 이사회 결의일이 원문에서 확인할 항목입니다.")
        impact_line = (f"{corp}의 주요사항보고서로, 자본·지배구조 변화가 해당 종목과 업종 수급에 "
                       f"영향을 줄 수 있습니다. 공시 사유와 금액이 확인 포인트입니다.")
    else:
        detail_line = "공시 유형과 첨부 문서가 원문에서 확인할 항목입니다."
        impact_line = (f"국내 상장사 {corp}의 공시로, 해당 종목과 업종 수급에 영향을 줄 수 있습니다. "
                       f"공시 유형({report})에 따라 확인할 항목이 달라집니다.")
    return {
        "title": f"{corp} {report}",
        "category": "kr-stock",
        "source_name": "금융감독원 OpenDART",
        "source_grade": config.SOURCE_GRADES["filing"],
        "source_url": url,
        "source_hash": sha256_url(url),
        "verified_at": verified_at,
        "summary_lines": [
            f"{corp}가 금융감독원 전자공시(DART)에 「{report}」을 공시했습니다.",
            f"접수번호 {rcept_no} · 접수일 {rcept_dt}",
            detail_line,
        ],
        "market_impact": impact_line,
        "tags": list(dict.fromkeys([corp, "국내주식", "공시"])),
        "published_at": config.iso_kst(published) if published else None,
        "topic": "filing",
        "importance": dart_importance(report),
    }


# ---------------------------------------------------------------- 네이버 초안용

TOPIC_HOOKS = {
    "fomc-minutes": ("금리 경로 3줄 요약", ["연준", "FOMC", "의사록", "미국금리", "금리인하", "나스닥", "비트코인"]),
    "fomc-statement": ("금리 결정 3줄 요약", ["연준", "FOMC", "기준금리", "미국금리", "달러", "나스닥"]),
    "enforcement": ("규제 리스크 체크", ["연준", "제재", "은행주", "규제", "금융기관"]),
    "survey": ("자금시장 참고 지표", ["연준", "은행", "유동성", "자금시장", "지급준비금"]),
    "payments": ("결제 업권 영향", ["연준", "지급결제", "수수료", "핀테크", "은행"]),
    "regulation": ("규제 변화 체크", ["연준", "금융규제", "자본규제", "은행주", "리스크"]),
    "general-macro": ("매크로 참고", ["연준", "매크로", "미국경제", "금리", "시장"]),
    "earnings": ("실적 공시 3줄", ["실적", "공시", "미국주식", "SEC", "8-K", "어닝"]),
    "filing": ("공시 원문 3줄", ["공시", "SEC", "미국주식", "기업분석", "원문"]),
}

TAG_KEYWORD_MAP = {
    "BTC": "비트코인",
    "ETH": "이더리움",
    "NVDA": "엔비디아",
    "AAPL": "애플",
    "나스닥": "나스닥",
    "코스피": "코스피",
    "삼성전자": "삼성전자",
    "금리": "미국금리",
    "매크로": "매크로",
    "공시": "공시",
}


def _smart_cut(text: str, room: int) -> str:
    """문장 중간의 숫자·괄호를 잘라내지 않도록 경계에서 자른다."""
    if len(text) <= room:
        return text
    cut = text[:room]
    if "(" in cut and ")" not in cut[cut.rfind("("):]:
        cut = cut[: cut.rfind("(")]
    for sep in (")", "·", "—", ",", " "):
        idx = cut.rfind(sep)
        if idx >= max(10, room - 14):
            cut = cut[: idx + 1]
            break
    return cut.rstrip(" -—·,(")


def seo_title_for(post: dict) -> str:
    hook, _ = TOPIC_HOOKS.get(post.get("topic", "filing"), ("원문 요약", []))
    base = _clean(post["title"])
    title = f"{base} | {hook}"
    if len(title) > 50:
        title = f"{_smart_cut(base, 50 - len(hook) - 4)} | {hook}"
    return title


def keywords_for(post: dict) -> list[str]:
    _, base = TOPIC_HOOKS.get(post.get("topic", "filing"), ("", []))
    words: list[str] = []
    for tag in post.get("tags", []):
        kw = TAG_KEYWORD_MAP.get(tag)
        if kw:
            words.append(kw)
    words.extend(base)
    words.append("클레어덱스")
    words = list(dict.fromkeys(words))
    return words[:8] if len(words) >= 5 else list(dict.fromkeys(words + ["시장 브리핑", "투자 정보"]))[:8]


# ---------------------------------------------------------------- CTA 라이브러리

TELEGRAM_CTA = {
    "fomc-minutes": [
        "금리 결정의 배경은 회의록에서 더 구체적으로 드러납니다. 원문을 확인한 직후 정리한 3줄 요약과 다음 FOMC 일정은 텔레그램 채널에서 가장 먼저 받아보실 수 있습니다.",
        "이번 의사록에서 연준 위원들의 시각이 얼마나 달라졌는지, 발표 직후 요약해 전해드립니다. 실시간 알림은 텔레그램에서 켜두시면 놓치지 않습니다.",
        "회의록 한 줄이 금리 기대를 바꾸기도 합니다. 클레어덱스 텔레그램에서는 공식 발표를 확인하는 즉시 핵심만 추려 보내드립니다.",
        "FOMC 일정이 다가올수록 시장은 말보다 시간표에 민감해집니다. 발표 직후 요약과 다음 일정 알림을 텔레그램으로 받아보세요.",
        "금리와 유동성 관련 공식 발표는 확인 시각까지 함께 정리해 텔레그램에 올립니다. 필요할 때 바로 꺼내 보실 수 있습니다.",
    ],
    "fomc-statement": [
        "금리 결정문이 나온 직후의 첫 반응은 텔레그램 채널에서 가장 빠르게 전해드립니다.",
        "성명서 문구의 변화는 다음 회의 기대를 바꿉니다. 발표 즉시 요약해 텔레그램으로 보내드립니다.",
        "발표 시각에 맞춰 확인한 원문과 3줄 요약을 텔레그램에서 받아보실 수 있습니다.",
        "금리 발표 당일에는 알림을 켜두는 것이 가장 빠릅니다. 클레어덱스 텔레그램에서 실시간으로 전해드립니다.",
        "정책금리 결정과 함께 나온 문구까지 정리해 텔레그램 채널에 올립니다.",
    ],
    "enforcement": [
        "금융기관 제재는 업권 전체의 규제 온도를 보여줍니다. 이런 공식 발표를 실시간으로 받아보시려면 텔레그램 채널을 이용해 주세요.",
        "제재·시정조치 소식은 보도보다 원문이 빠릅니다. 클레어덱스 텔레그램에서 확인 즉시 알려드립니다.",
        "은행·금융기관 관련 공식 조치는 텔레그램에서 가장 먼저 정리해 전해드립니다.",
        "규제 관련 속보를 놓치지 않으려면 텔레그램 알림을 켜두세요.",
    ],
    "survey": [
        "은행들의 자금 관리 방식은 시장 유동성을 읽는 단서가 됩니다. 이런 자료가 나올 때마다 텔레그램으로 요약해 보내드립니다.",
        "숫자 뒤의 배경까지 확인한 요약은 텔레그램 채널에서 받아보실 수 있습니다.",
        "자금시장 관련 공식 자료를 빠르게 확인하고 싶다면 텔레그램 채널을 구독해 주세요.",
    ],
    "payments": [
        "지급결제 수수료와 제도 변화는 은행·핀테크 수익성에 연결됩니다. 관련 공식 발표는 텔레그램에서 요약해 전해드립니다.",
        "결제 업권 제도 변화 소식을 실시간으로 받아보시려면 텔레그램을 이용해 주세요.",
        "생활 속 금융에 영향을 주는 제도 변경도 원문 기준으로 정리해 텔레그램에 올립니다.",
    ],
    "regulation": [
        "규제 변화는 업권의 비용 구조를 바꿉니다. 공식 발표가 나오는 즉시 텔레그램으로 정리해 보내드립니다.",
        "금융규제 관련 발표는 원문 확인이 먼저입니다. 클레어덱스 텔레그램에서 핵심만 빠르게 받아보세요.",
        "규제·감독 관련 소식을 놓치지 않도록 텔레그램 알림을 활용해 보세요.",
    ],
    "general-macro": [
        "매크로 공식 발표는 확인 즉시 3줄로 정리해 텔레그램에 올립니다.",
        "시장에 영향을 줄 수 있는 공식 자료를 빠르게 받아보시려면 텔레그램 채널을 구독해 주세요.",
        "발표 원문과 확인 시각을 함께 담아 텔레그램으로 전해드립니다.",
    ],
    "earnings": [
        "실적 공시는 숫자보다 먼저 확인한 사람이 유리합니다. 원문 확인 직후 핵심을 텔레그램으로 보내드립니다.",
        "분기 실적이 나오는 날에는 알림 속도가 중요합니다. 클레어덱스 텔레그램에서 실시간으로 받아보세요.",
        "실적 발표와 함께 제출된 공시까지 한 번에 정리해 텔레그램에 올립니다.",
        "실적 시즌에는 관심 종목 공시를 놓치기 쉽습니다. 텔레그램 알림으로 챙겨보세요.",
    ],
    "filing": [
        "공시 원문은 길고 어렵습니다. 확인한 내용만 골라 3줄로 정리해 텔레그램에 올립니다.",
        "관심 종목의 공시가 올라오면 바로 알림을 받아보고 싶다면 텔레그램 채널을 이용해 주세요.",
        "SEC·DART 공시 소식을 원문 링크와 함께 텔레그램에서 빠르게 확인하실 수 있습니다.",
        "서류 한 줄이 다음 날 주가를 바꾸기도 합니다. 접수된 공시를 확인하는 즉시 텔레그램으로 알려드립니다.",
        "어려운 공시도 원문에서 확인한 핵심만 3줄로 정리해 텔레그램에 올립니다.",
    ],
}

WEB_CTA = {
    "fomc-minutes": [
        "금리와 유동성 흐름을 더 길게 읽고 싶다면 클레어덱스 인사이트의 매크로 해설을 함께 보세요.",
        "원문 링크와 확인 시각까지 남긴 속보 아카이브는 클레어덱스 웹에서 검색하실 수 있습니다.",
        "연준 발표를 시계열로 비교해 보고 싶다면 클레어덱스의 공식 일정 페이지를 확인해 보세요.",
        "이번 회의록이 남긴 질문은 클레어덱스 웹의 심층 인사이트에서 이어서 다룹니다.",
        "관심 자산을 등록해 두면 같은 주제의 속보만 모아서 클레어덱스 웹에서 보실 수 있습니다.",
    ],
    "fomc-statement": [
        "금리 결정의 배경과 다음 일정은 클레어덱스 웹에서 한눈에 정리해 볼 수 있습니다.",
        "발표 이후 시장 해석은 클레어덱스 인사이트에서 더 자세히 다룹니다.",
        "관심 자산을 등록하면 금리 관련 속보만 모아 클레어덱스 웹에서 확인하실 수 있습니다.",
        "성명서 원문과 과거 발표를 함께 비교하려면 클레어덱스 아카이브를 이용해 보세요.",
    ],
    "enforcement": [
        "업권 규제 흐름을 정리한 글은 클레어덱스 웹의 공시·규제 코너에서 보실 수 있습니다.",
        "관심 자산을 등록해 두면 관련 공식 발표만 모아 클레어덱스 웹에서 확인하실 수 있습니다.",
        "제재와 규제 관련 아카이브는 클레어덱스에서 키워드로 검색할 수 있습니다.",
    ],
    "survey": [
        "자금시장 지표를 함께 보고 싶다면 클레어덱스 웹의 시장 지표 위젯을 확인해 보세요.",
        "관련 공식 자료의 원문 링크는 클레어덱스 아카이브에 정리되어 있습니다.",
        "은행·유동성 관련 해설은 클레어덱스 인사이트에서 이어집니다.",
    ],
    "payments": [
        "결제·수수료 구조를 다룬 해설은 클레어덱스 웹에서 확인하실 수 있습니다.",
        "업권별 규제 변화는 클레어덱스의 공시·정책 아카이브에 정리해 두었습니다.",
        "생활 금융 관련 공식 발표는 클레어덱스 웹에서 검색해 보세요.",
    ],
    "regulation": [
        "규제 변화가 업권에 미치는 영향은 클레어덱스 인사이트에서 더 자세히 다룹니다.",
        "관련 공식 문서 원문은 클레어덱스 웹의 자료실에 모아 두었습니다.",
        "관심 자산별 규제 속보는 클레어덱스 웹에서 필터로 확인하실 수 있습니다.",
    ],
    "general-macro": [
        "매크로 지표와 공식 일정은 클레어덱스 웹에서 한 화면에 정리해 볼 수 있습니다.",
        "더 깊은 해설과 지난 발표 아카이브는 클레어덱스 인사이트에서 확인하세요.",
        "관심 자산을 등록하면 관련 속보만 모아 클레어덱스 웹에서 받아보실 수 있습니다.",
    ],
    "earnings": [
        "실적 발표의 세부 수치와 비교는 클레어덱스 웹의 공시 아카이브에서 확인하실 수 있습니다.",
        "관심 종목을 등록해 두면 실적·공시 속보만 모아 클레어덱스 웹에서 보실 수 있습니다.",
        "실적 시즌 해설은 클레어덱스 인사이트에서 이어서 읽어보세요.",
        "실적 발표 뒤 비교 포인트는 클레어덱스 웹에서 시계열로 확인하실 수 있습니다.",
        "발표된 공시 원문과 함께 업종 해설은 클레어덱스 인사이트에서 이어집니다.",
    ],
    "filing": [
        "공시 원문을 읽는 방법은 클레어덱스의 공시 읽기 가이드에서 확인하실 수 있습니다.",
        "관심 종목을 등록하면 새 공시가 올라올 때 클레어덱스 웹에서 바로 확인하실 수 있습니다.",
        "지난 공시 아카이브는 클레어덱스 웹에서 기업명으로 검색해 보세요.",
        "공시가 나온 뒤 무엇을 먼저 봐야 하는지는 클레어덱스 웹의 공시 읽기 가이드에 정리해 두었습니다.",
        "같은 기업의 과거 공시까지 비교하려면 클레어덱스 아카이브를 이용해 보세요.",
    ],
}

# 블로그 본문 하단 고지: 투자 유의 문구만 남긴다.
# ("웨일파트너스는 유사투자자문업 신고를 준비 중입니다." 같은 신고 상태 문구는 넣지 않는다 — 2026-11-24 사용자 요청으로 영구 삭제)
COMPLIANCE_HTML = (
    f'<p style="font-size:12px;color:#8a877c;line-height:1.7;border-top:1px solid #e6e1d5;padding-top:12px;">'
    f"{config.DISCLAIMER} 본 콘텐츠의 수치와 인용은 아래 원문 출처에서 확인한 내용만 사용했습니다."
    "</p>"
)
