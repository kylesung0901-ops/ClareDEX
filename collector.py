"""ClareDEX v2 실시간 수집기.

1차 공식·무료 소스만 사용한다.
  - 미 연준 보도자료 RSS          https://www.federalreserve.gov/feeds/press_all.xml
  - 미 SEC EDGAR submissions API  https://data.sec.gov/submissions/CIK##########.json
  - 업비트 시세                   https://api.upbit.com/v1/ticker
  - 바이낸스/Binance.US/Coinbase  (지역 차단 시 다음 소스로 자동 전환, 응답에 실제 소스 표기)
  - OKX 롱숏 계정 비율
  - 공포탐욕지수                  https://api.alternative.me/fng/
  - USD/KRW 일별 고시환율         https://open.er-api.com/v6/latest/USD
  - OpenDART / 한국은행 ECOS      .env 에 키가 있을 때만 동작

원칙: 원문에 없는 숫자를 만들지 않는다. 모든 글은 source_url 과 KST verified_at 을 남긴다.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import config

config.load_env()

import content  # noqa: E402
import db  # noqa: E402
import thumbnail  # noqa: E402


class FetchError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(msg, flush=True)


def http_get(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = config.HTTP_TIMEOUT,
    retries: int = 2,
) -> bytes:
    hdrs = {
        "User-Agent": config.HTTP_USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
    }
    hdrs.update(headers or {})
    last: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(1.2 * (attempt + 1))
                continue
            raise FetchError(f"HTTP {exc.code} {exc.reason}") from exc
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
    raise FetchError(str(last))


def http_json(url: str, headers: dict[str, str] | None = None, timeout: int = config.HTTP_TIMEOUT) -> object:
    raw = http_get(url, headers=headers, timeout=timeout)
    return json.loads(raw.decode("utf-8-sig"))


# ------------------------------------------------------------------ 연준


# 시장과 무관한 행사·수상 소식은 타임라인에서 제외한다(수집은 하되 발행하지 않음).
FED_IRRELEVANT = (
    "college fed challenge",
    "winners",
    "obituary",
    "in memoriam",
    "holiday schedule",
)


def fetch_fed(limit: int = 5) -> list[dict]:
    raw = http_get("https://www.federalreserve.gov/feeds/press_all.xml")
    root = ET.fromstring(raw.decode("utf-8-sig", errors="replace"))
    items = []
    skipped = 0
    for node in root.findall(".//item"):
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        pub = (node.findtext("pubDate") or "").strip()
        if not title or not link:
            continue
        if any(k in title.lower() for k in FED_IRRELEVANT):
            skipped += 1
            continue
        try:
            published = config.kst_from_utc_string(pub)
        except Exception:
            published = None
        items.append({"title": title, "link": link, "published_kst": published})
    items.sort(key=lambda x: x["published_kst"] or datetime.min.replace(tzinfo=config.KST), reverse=True)
    log(f"[Fed] RSS {len(items)}건 수신 · 최신 {min(limit, len(items))}건 사용" + (f" · 비시장성 {skipped}건 제외" if skipped else ""))
    return items[:limit]


# ------------------------------------------------------------------ SEC


def fetch_sec(limit_per_company: int = 2, forms: tuple[str, ...] = ("8-K", "10-Q", "10-K")) -> list[tuple[dict, dict]]:
    out: list[tuple[dict, dict]] = []
    for company in content.SEC_COMPANIES:
        url = f"https://data.sec.gov/submissions/CIK{company['cik']}.json"
        try:
            data = http_json(url, headers={"User-Agent": config.SEC_USER_AGENT})
        except FetchError as exc:
            log(f"[SEC] {company['ticker']} 실패: {exc}")
            continue
        recent = (data or {}).get("filings", {}).get("recent", {})  # type: ignore[union-attr]
        keys = ["form", "filingDate", "reportDate", "acceptanceDateTime", "accessionNumber",
                "primaryDocument", "primaryDocDescription", "items", "act", "fileNumber"]
        taken = 0
        for i, form in enumerate(recent.get("form", [])):
            if form not in forms:
                continue
            filing = {k: (recent.get(k) or [None] * (i + 1))[i] for k in keys}
            out.append((company, filing))
            taken += 1
            if taken >= limit_per_company:
                break
        log(f"[SEC] {company['name']} ({company['ticker']}) 최근 {taken}건 수신")
        time.sleep(0.2)  # SEC 권장 초당 10회 이하
    return out


# ------------------------------------------------------------------ OpenDART / ECOS (키 필요)


def fetch_dart(limit: int = 3) -> list[dict]:
    key = os.environ.get("OPENDART_API_KEY", "").strip()
    if not key:
        log("[OpenDART] OPENDART_API_KEY 미설정 — 건너뜀 (키 발급 후 .env 에 추가하면 국내 공시가 자동 수집됩니다)")
        return []
    bgn = (config.now_kst() - timedelta(days=7)).strftime("%Y%m%d")
    url = (
        "https://opendart.fss.or.kr/api/list.json"
        f"?crtfc_key={key}&bgn_de={bgn}&pblntf_ty=A&page_count={max(limit, 10)}&page_no=1&sort=date&sort_mth=desc"
    )
    try:
        data = http_json(url, timeout=20)
    except Exception as exc:  # noqa: BLE001
        log(f"[OpenDART] 요청 실패: {exc}")
        return []
    if str(data.get("status")) != "000":  # type: ignore[union-attr]
        log(f"[OpenDART] 응답 상태 {data.get('status')} {data.get('message')}")  # type: ignore[union-attr]
        return []
    rows = (data.get("list") or [])[:limit]  # type: ignore[union-attr]
    log(f"[OpenDART] 최근 공시 {len(rows)}건 수신")
    return rows


def fetch_ecos() -> dict | None:
    key = os.environ.get("ECOS_API_KEY", "").strip()
    if not key:
        log("[ECOS] ECOS_API_KEY 미설정 — 건너뜀 (한국은행 기준금리 수집은 키 발급 후 활성화)")
        return None
    today = config.now_kst()
    start = (today - timedelta(days=400)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    url = f"https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/10/722Y001/D/{start}/{end}"
    try:
        data = http_json(url, timeout=20)
    except Exception as exc:  # noqa: BLE001
        log(f"[ECOS] 요청 실패: {exc}")
        return None
    rows = data.get("StatisticSearch", {}).get("row", []) if isinstance(data, dict) else []
    if not rows:
        log("[ECOS] 응답에 기준금리 시계열이 없습니다 — 건너뜀")
        return None
    last = rows[-1]
    log(f"[ECOS] 한국은행 기준금리 {last.get('TIME')} = {last.get('DATA_VALUE')}% (출처: 한국은행 ECOS)")
    return last


# ------------------------------------------------------------------ 시장 지표


def _upbit() -> dict:
    data = http_json("https://api.upbit.com/v1/ticker?markets=KRW-BTC,KRW-ETH")
    out = {}
    for row in data:  # type: ignore[union-attr]
        out[row["market"]] = row
    return out


def _usd_btc() -> dict:
    """바이낸스 → 바이낸스US → 코인베이스 순으로 시도. 실제로 성공한 소스를 그대로 표기."""
    try:
        d = http_json("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=8)
        if d.get("price"):
            return {"price": float(d["price"]), "source": "Binance", "url": "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"}
    except Exception as exc:  # noqa: BLE001
        log(f"[Binance] 사용 불가({exc}) — 대체 소스로 전환")
    try:
        d = http_json("https://api.binance.us/api/v3/ticker/price?symbol=BTCUSDT", timeout=8)
        if d.get("price"):
            return {"price": float(d["price"]), "source": "Binance.US", "url": "https://api.binance.us/api/v3/ticker/price?symbol=BTCUSDT"}
    except Exception as exc:  # noqa: BLE001
        log(f"[Binance.US] 실패: {exc}")
    d = http_json("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=8)
    return {
        "price": float(d["data"]["amount"]),  # type: ignore[index]
        "source": "Coinbase",
        "url": "https://api.coinbase.com/v2/prices/BTC-USD/spot",
    }


def _long_short() -> dict:
    url = "https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy=BTC&period=1H"
    d = http_json(url, timeout=8)
    row = d["data"][0]  # type: ignore[index]
    ratio = float(row[1])
    long_pct = ratio / (1 + ratio) * 100
    ts = datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc).astimezone(config.KST)
    return {
        "ratio": ratio,
        "long_pct": long_pct,
        "short_pct": 100 - long_pct,
        "source": "OKX",
        "url": url,
        "as_of": config.iso_kst(ts),
        "period": "1H",
    }


def _fng() -> dict:
    d = http_json("https://api.alternative.me/fng/?limit=1", timeout=8)
    row = d["data"][0]  # type: ignore[index]
    labels = {
        "Extreme Fear": "극단적 공포",
        "Fear": "공포",
        "Neutral": "중립",
        "Greed": "탐욕",
        "Extreme Greed": "극단적 탐욕",
    }
    ts = datetime.fromtimestamp(int(row["timestamp"]), tz=timezone.utc).astimezone(config.KST)
    return {
        "value": int(row["value"]),
        "label_en": row["value_classification"],
        "label": labels.get(row["value_classification"], row["value_classification"]),
        "source": "Alternative.me",
        "url": "https://api.alternative.me/fng/?limit=1",
        "as_of": config.iso_kst(ts),
    }


def _usdkrw() -> dict:
    d = http_json("https://open.er-api.com/v6/latest/USD", timeout=8)
    return {
        "value": float(d["rates"]["KRW"]),  # type: ignore[index]
        "source": "exchangerate-api (일별 고시)",
        "url": "https://open.er-api.com/v6/latest/USD",
        "as_of": d.get("time_last_update_utc", ""),  # type: ignore[union-attr]
    }


def collect_market() -> dict:
    """실시간 지표 묶음. 일부 실패해도 성공한 항목만 담아 반환한다."""
    now = config.iso_kst()
    items: list[dict] = []
    errors: dict[str, str] = {}
    values: dict[str, float] = {}

    upbit = {}
    try:
        upbit = _upbit()
    except Exception as exc:  # noqa: BLE001
        errors["upbit"] = str(exc)
        log(f"[업비트] 실패: {exc}")

    btc = upbit.get("KRW-BTC")
    if btc:
        values["btc_krw"] = float(btc["trade_price"])
        items.append({
            "key": "btc_krw",
            "label": "BTC/KRW",
            "value": float(btc["trade_price"]),
            "display": config.fmt_krw(float(btc["trade_price"])),
            "delta": round(float(btc["signed_change_rate"]) * 100, 2),
            "delta_unit": "%",
            "source": "업비트",
            "source_url": "https://api.upbit.com/v1/ticker?markets=KRW-BTC,KRW-ETH",
            "as_of": config.iso_kst(datetime.strptime(btc["trade_date_kst"] + btc["trade_time_kst"], "%Y%m%d%H%M%S").replace(tzinfo=config.KST)),
        })
    eth = upbit.get("KRW-ETH")
    if eth:
        values["eth_krw"] = float(eth["trade_price"])
        items.append({
            "key": "eth_krw",
            "label": "ETH/KRW",
            "value": float(eth["trade_price"]),
            "display": config.fmt_krw(float(eth["trade_price"])),
            "delta": round(float(eth["signed_change_rate"]) * 100, 2),
            "delta_unit": "%",
            "source": "업비트",
            "source_url": "https://api.upbit.com/v1/ticker?markets=KRW-BTC,KRW-ETH",
            "as_of": now,
        })

    usd = None
    try:
        usd = _usd_btc()
        values["btc_usd"] = usd["price"]
        items.append({
            "key": "btc_usd",
            "label": "BTC/USD",
            "value": usd["price"],
            "display": f"${usd['price']:,.0f}",
            "source": usd["source"],
            "source_url": usd["url"],
            "as_of": now,
        })
    except Exception as exc:  # noqa: BLE001
        errors["btc_usd"] = str(exc)
        log(f"[BTC/USD] 실패: {exc}")

    fx = None
    try:
        fx = _usdkrw()
        values["usdkrw"] = fx["value"]
        items.append({
            "key": "usdkrw",
            "label": "USD/KRW",
            "value": round(fx["value"], 2),
            "display": f"{fx['value']:,.1f}원",
            "source": fx["source"],
            "source_url": fx["url"],
            "as_of": fx["as_of"],
        })
    except Exception as exc:  # noqa: BLE001
        errors["usdkrw"] = str(exc)
        log(f"[USD/KRW] 실패: {exc}")

    if btc and usd and fx:
        premium = (float(btc["trade_price"]) / (usd["price"] * fx["value"]) - 1) * 100
        values["kimchi_premium"] = premium
        items.insert(2, {
            "key": "kimchi_premium",
            "label": "김치프리미엄",
            "value": round(premium, 2),
            "display": f"{premium:+.2f}%",
            "delta": round(premium, 2),
            "delta_unit": "%",
            "source": f"업비트·{usd['source']}·USD/KRW",
            "source_url": "https://www.claredex.com/",
            "as_of": now,
            "note": "업비트 BTC 원화가 ÷ (BTC/USD × USD/KRW) − 1",
        })

    try:
        ls = _long_short()
        values["long_pct"] = ls["long_pct"]
        items.append({
            "key": "long_short",
            "label": "BTC 롱/숏",
            "value": round(ls["ratio"], 2),
            "display": f"{ls['long_pct']:.0f} : {ls['short_pct']:.0f}",
            "source": f"{ls['source']} 계정 비율",
            "source_url": ls["url"],
            "as_of": ls["as_of"],
            "note": "롱 계정 ÷ 숏 계정 = " + f"{ls['ratio']:.2f}",
        })
    except Exception as exc:  # noqa: BLE001
        errors["long_short"] = str(exc)
        log(f"[롱숏] 실패: {exc}")

    try:
        fng = _fng()
        values["fear_greed"] = fng["value"]
        items.append({
            "key": "fear_greed",
            "label": "공포탐욕",
            "value": fng["value"],
            "display": f"{fng['value']} · {fng['label']}",
            "source": fng["source"],
            "source_url": fng["url"],
            "as_of": fng["as_of"],
        })
    except Exception as exc:  # noqa: BLE001
        errors["fear_greed"] = str(exc)
        log(f"[공포탐욕] 실패: {exc}")

    order = ["btc_krw", "btc_usd", "kimchi_premium", "fear_greed", "long_short", "usdkrw", "eth_krw"]
    items.sort(key=lambda x: order.index(x["key"]) if x["key"] in order else 99)

    payload = {
        "ok": len(items) >= 3,
        "stale": False,
        "generated_at": now,
        "items": items,
        "values": values,
        "errors": errors,
    }
    return payload


def save_market_cache(payload: dict) -> None:
    config.ensure_dirs()
    tmp = config.MARKET_CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(config.MARKET_CACHE_PATH)
    log(f"[캐시] {config.MARKET_CACHE_PATH} 저장 ({len(payload.get('items', []))}개 지표)")


# ------------------------------------------------------------------ 글 저장


def _is_fresh(published_iso: str | None, verified_iso: str) -> bool:
    if not published_iso:
        return False
    try:
        return (config.parse_kst(verified_iso) - config.parse_kst(published_iso)) <= timedelta(hours=24)
    except Exception:
        return False


def backfill_importance(conn) -> int:
    """중요도 등급이 없는(마이그레이션 이전) 글을 제목·주제·요약 기준으로 분류한다."""
    rows = conn.execute("SELECT * FROM posts WHERE importance IS NULL OR importance = ''").fetchall()
    count = 0
    for row in rows:
        post = db.row_to_post(row)
        level = content.classify_post_importance(post)
        db.set_post_importance(conn, post["id"], level)
        count += 1
    conn.commit()
    if count:
        log(f"[중요도] 기존 글 {count}건 등급 재분류")
    return count


GENERIC_FED_TITLES = (
    "연준 공식 보도자료", "연준, 금융규제 관련 발표", "연준, 인사 발표",
    "연준, 금융기관 대상 조사 결과 공개", "연준, 소액 대출 면제 기준 금액 발표",
    "연준, 금융기관 제재·시정조치 발표", "연준, 소비자금융 규제 기준 금액 발표",
    "연준, 소액 대출 감정평가 면제 기준 발표",
)


def refresh_legacy_content(conn) -> int:
    """예전 버전이 만든 천편일률적인 연준 글의 제목·요약·영향 문구를 새 규칙으로 다시 만든다.

    원문 제목은 저장된 요약 2번째 줄(`원문 제목: …`)에서 그대로 읽어 쓰므로 새 숫자를 만들지 않는다.
    이미 초안이 만들어진 글도 있어 초안은 건드리지 않는다(필요하면 `naver_drafts.py --rebuild`).
    """
    rows = conn.execute(
        "SELECT * FROM posts WHERE category = 'macro' AND source_grade = ?",
        (config.SOURCE_GRADES["primary"],),
    ).fetchall()
    changed = 0
    for row in rows:
        post = db.row_to_post(row)
        base = post["title"].split(" (")[0]
        if base not in GENERIC_FED_TITLES:
            continue
        raw = None
        for line in post["summary_lines"]:
            if line.startswith("원문 제목: "):
                raw = line[len("원문 제목: "):].strip()
                break
        if not raw:
            continue
        gist, topic = content.classify_fed(raw)
        if "공식 보도자료" in gist:
            gist = f"연준, {content.fed_kind_from_url(post['source_url'])} 보도자료 공개"
        suffix = post["title"][len(base):]
        new_title = gist + suffix
        first = post["summary_lines"][0] if post["summary_lines"] else ""
        new_summary = [first, f"원문 제목: {raw}",
                       content.FED_DETAIL.get(topic, content.FED_DETAIL["general-macro"])]
        new_impact = content.FED_IMPACT.get(topic, content.FED_IMPACT["general-macro"])
        level = content.fed_importance(topic, post["source_url"], raw)
        if (new_title, new_summary, new_impact, topic, level) == (
                post["title"], post["summary_lines"], post["market_impact"], post["topic"], post["importance"]):
            continue
        conn.execute(
            "UPDATE posts SET title = ?, summary_lines = ?, market_impact = ?, topic = ?, importance = ? "
            "WHERE id = ?",
            (new_title, json.dumps(new_summary, ensure_ascii=False), new_impact, topic, level, post["id"]),
        )
        # 제목·등급이 바뀌면 썸네일도 다시 그린다.
        thumbnail.render(dict(post, title=new_title, importance=level))
        changed += 1
        log(f"  ↻ [{post['id']}] {post['title'][:40]} → {new_title[:60]} ({level})")
    conn.commit()
    if changed:
        log(f"[콘텐츠] 기존 연준 글 {changed}건 제목·요약·영향 문구 갱신")
    return changed


SEC_ITEM_REVERSE = {label: code for code, label in content.FORM8K_ITEMS.items()}


def refresh_legacy_sec_content(conn) -> int:
    """예전 버전이 만든 SEC 공시 글의 3번째 요약 줄과 시장 영향 문구를 항목 기준으로 다시 만든다.

    서식·신고 항목은 이미 저장된 제목·요약에서 읽어 쓰므로 새 사실을 만들지 않는다.
    """
    rows = conn.execute("SELECT * FROM posts WHERE source_grade = ?",
                        (config.SOURCE_GRADES["filing"],)).fetchall()
    changed = 0
    for row in rows:
        post = db.row_to_post(row)
        title = post["title"]
        if "SEC" not in post["source_name"]:
            continue
        form = next((f for f in ("10-K", "10-Q", "8-K") if f in title), "")
        if not form:
            continue
        items: list[str] = []
        for line in post["summary_lines"]:
            if "신고 항목:" in line:
                labels = [part.strip() for part in line.split("신고 항목:", 1)[-1].split(",")]
                items = [SEC_ITEM_REVERSE[label] for label in labels if label in SEC_ITEM_REVERSE]
                break
        impact_key = form
        if form == "8-K" and items:
            for code in items:
                if code in content.SEC_IMPACT:
                    impact_key = code
                    break
        detail = content.SEC_DETAIL.get(impact_key) or content.SEC_DETAIL.get(form)
        if not detail:
            continue
        summary = list(post["summary_lines"])
        name_ko = re.sub(r"\s*\(.*?\)", "", title.split(",")[0]).strip()
        ticker = ""
        m = re.search(r"\(([A-Z]{1,6})\)", title)
        if m:
            ticker = m.group(1)
        sector = next((t for t in post["tags"] if t not in (ticker, "나스닥", "미국주식", "공시")), "미국주식")
        sector_phrase = f"{sector} 업종" if sector != "미국주식" else "미국주식 종목"
        impact = (f"{name_ko}({ticker}){content.subject_particle(name_ko)} {sector_phrase}으로, 이 공시가 같은 업종 종목과 "
                  f"나스닥 성장주 수급에 영향을 줄 수 있습니다. "
                  + content.SEC_IMPACT.get(impact_key, content.SEC_IMPACT.get(form, "")))
        if len(summary) >= 3 and summary[2] == detail and post["market_impact"] == impact:
            continue
        if len(summary) >= 3:
            summary[2] = detail
        level = content.sec_importance(form, items)
        conn.execute(
            "UPDATE posts SET summary_lines = ?, market_impact = ?, importance = ? WHERE id = ?",
            (json.dumps(summary, ensure_ascii=False), impact, level, post["id"]),
        )
        thumbnail.render(dict(post, summary_lines=summary, market_impact=impact, importance=level))
        changed += 1
        log(f"  ↻ [{post['id']}] {title[:44]} 요약·영향 문구 갱신 ({level})")
    conn.commit()
    if changed:
        log(f"[콘텐츠] 기존 SEC 공시 글 {changed}건 요약·영향 문구 갱신")
    return changed


def sync_calendar_snapshot() -> dict:
    """공식 일정(calendar.json)을 검증해 public/data/calendar.json 스냅샷으로 내보낸다."""
    calendar = config.load_calendar()
    path = config.PUBLIC_DIR / "data" / "calendar.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ok": True, **calendar}, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    events = calendar.get("events") or []
    if events:
        upcoming = [e for e in events if config.parse_kst(e["datetime_kst"]) >= config.now_kst()]
        nxt = (upcoming or events)[0]
        log(f"[일정] 공식 일정 {len(events)}건 · 다음 {nxt['datetime_kst']} {nxt['title']}")
    else:
        log("[일정] calendar.json 에 유효한 공식 일정이 없습니다")
    return calendar


def store_posts(conn, posts: list[dict]) -> tuple[list[dict], int]:
    new_posts: list[dict] = []
    duplicates = 0
    for post in posts:
        post.setdefault("created_at", config.iso_kst())
        post["is_member_early"] = _is_fresh(post.get("published_at"), post["verified_at"])
        post_id, created = db.insert_post(conn, post)
        if not created:
            duplicates += 1
            continue
        row = dict(post, id=post_id)
        image_path = thumbnail.render(row)
        rel = f"/images/posts/{post_id}.png"
        conn.execute("UPDATE posts SET image_path = ? WHERE id = ?", (rel, post_id))
        conn.commit()
        new_posts.append(dict(row, image_path=rel))
        log(f"  + [{post_id}] {post['source_grade']} | {post['title'][:60]} → {image_path.name}")
    return new_posts, duplicates


def run(fed_limit: int = 5, sec_limit: int = 2, collect_market_too: bool = True) -> dict:
    config.ensure_dirs()
    db.init_db()
    verified_at = config.iso_kst()
    conn = db.connect()
    backfill_importance(conn)
    refresh_legacy_content(conn)
    refresh_legacy_sec_content(conn)
    calendar = sync_calendar_snapshot()

    posts: list[dict] = []
    source_status: dict[str, str] = {}

    try:
        fed_items = fetch_fed(limit=fed_limit)
        for item in fed_items:
            posts.append(content.fed_post(item, verified_at))
        source_status["fed"] = f"ok ({len(fed_items)})"
    except Exception as exc:  # noqa: BLE001
        source_status["fed"] = f"fail: {exc}"
        log(f"[Fed] 수집 실패: {exc}")

    try:
        sec_items = fetch_sec(limit_per_company=sec_limit)
        for company, filing in sec_items:
            posts.append(content.sec_post(company, filing, verified_at))
        source_status["sec"] = f"ok ({len(sec_items)})"
    except Exception as exc:  # noqa: BLE001
        source_status["sec"] = f"fail: {exc}"
        log(f"[SEC] 수집 실패: {exc}")

    try:
        dart_rows = fetch_dart(limit=3)
        for row in dart_rows:
            posts.append(content.dart_post(row, verified_at))
        source_status["dart"] = f"ok ({len(dart_rows)})" if dart_rows else "no-key"
    except Exception as exc:  # noqa: BLE001
        source_status["dart"] = f"fail: {exc}"

    try:
        ecos_last = fetch_ecos()
        source_status["ecos"] = "ok" if ecos_last else "no-key"
    except Exception as exc:  # noqa: BLE001
        source_status["ecos"] = f"fail: {exc}"

    # 최신순 정렬 후 저장 (오래된 것부터 넣어 id 가 시간순이 되도록)
    posts.sort(key=lambda p: p.get("published_at") or p["verified_at"])
    new_posts, duplicates = store_posts(conn, posts)

    market_payload = None
    if collect_market_too:
        try:
            market_payload = collect_market()
            save_market_cache(market_payload)
            source_status["market"] = f"ok ({len(market_payload['items'])})"
        except Exception as exc:  # noqa: BLE001
            source_status["market"] = f"fail: {exc}"
            log(f"[시장 지표] 수집 실패: {exc}")

    total = db.count_posts(conn)
    levels = db.importance_counts(conn)
    today = db.today_stats(conn)
    log("")
    log("=" * 72)
    log(f"수집 완료 · 신규 {len(new_posts)}건 · 중복/기존 {duplicates}건 · DB 전체 {total}건")
    log(f"중요도 S {levels['S']} · A {levels['A']} · B {levels['B']} | 오늘 확인 {today['count']}건")
    log(f"공식 일정 {len(calendar.get('events') or [])}건")
    for key, status in source_status.items():
        log(f"  - {key:<8} {status}")
    log("=" * 72)
    conn.close()
    return {
        "new": len(new_posts),
        "duplicates": duplicates,
        "total": total,
        "source_status": source_status,
        "market": market_payload,
        "calendar_events": len(calendar.get("events") or []),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="ClareDEX v2 공식 원문 수집기")
    parser.add_argument("--fed-limit", type=int, default=5)
    parser.add_argument("--sec-limit", type=int, default=2)
    parser.add_argument("--no-market", action="store_true")
    args = parser.parse_args()
    result = run(fed_limit=args.fed_limit, sec_limit=args.sec_limit, collect_market_too=not args.no_market)
    return 0 if result["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
