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


# ------------------------------------------------------------------ 타점 비상벨

ALERT_MESSAGE_PREFIX = "[클레어덱스 비상벨]"


def alert_metric_values(market_payload: dict | None) -> dict[str, float]:
    """collect_market() 결과에서 타점 비상벨 지표만 꺼낸다.

    · kimp_pct  ← values.kimchi_premium (없으면 items 의 kimchi_premium 값)
    · fng       ← values.fear_greed (없으면 items 의 fear_greed 값)
    · long_pct  ← values.long_pct (items 의 long_short 값은 롱/숏 비율이라 쓰지 않는다)
    """
    out: dict[str, float] = {}
    if not isinstance(market_payload, dict):
        return out
    values = market_payload.get("values") or {}
    items = {i.get("key"): i for i in market_payload.get("items") or [] if isinstance(i, dict)}

    def pick(keys: list[str], item_key: str | None = None) -> float | None:
        for key in keys:
            raw = values.get(key)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                return float(raw)
        if item_key:
            raw = (items.get(item_key) or {}).get("value")
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                return float(raw)
        return None

    for metric, got in (
        ("kimp_pct", pick(["kimp_pct", "kimchi_premium"], "kimchi_premium")),
        ("fng", pick(["fng", "fear_greed"], "fear_greed")),
        ("long_pct", pick(["long_pct"])),
    ):
        if got is not None:
            out[metric] = got
    return out


def alert_condition_met(observed: float, operator: str, threshold: float) -> bool:
    if operator == ">=":
        return observed >= threshold
    if operator == "<=":
        return observed <= threshold
    return False


def alert_cooldown_left(rule: dict, now: datetime | None = None) -> int:
    """쿨다운이 남은 초. 아직 발송 이력이 없거나 시각이 깨졌으면 0."""
    last = rule.get("last_triggered_at")
    if not last:
        return 0
    try:
        last_dt = config.parse_kst(str(last))
    except Exception:  # noqa: BLE001
        return 0
    now = now or config.now_kst()
    elapsed = (now - last_dt).total_seconds()
    if elapsed < 0:  # 미래 시각은 기록 오류로 보고 쿨다운을 걸지 않는다
        return 0
    minutes = int(rule.get("cooldown_minutes") or config.ALERT_COOLDOWN_MINUTES)
    return max(0, int(minutes * 60 - elapsed))


def format_alert_message(rule: dict, observed: float, when: datetime | None = None) -> str:
    """문자에 그대로 들어가는 1줄 경보."""
    metric = rule.get("metric") or ""
    operator = rule.get("operator") or ">="
    symbol = "≥" if operator == ">=" else "≤"
    when = when or config.now_kst()
    return (
        f"{ALERT_MESSAGE_PREFIX} {config.alert_metric_label(metric)} "
        f"{config.fmt_alert_value(metric, observed)} {symbol} "
        f"{config.fmt_alert_value(metric, rule.get('threshold'))} 조건 도달 · "
        f"{config.fmt_kst(when, '%m-%d %H:%M')} KST"
    )


def evaluate_market_alerts(conn, market_payload: dict | None, send_fn=None,
                            now: datetime | None = None) -> dict:
    """활성 타점 비상벨 규칙과 현재 지표를 비교해 조건 도달 시 문자를 보낸다.

    · send_fn(phone, text) 는 기본이 authflow.send_sms_text 이며, 테스트에서는 스텁을 넣는다.
    · 쿨다운(cooldown_minutes, 기본 360분) 안의 같은 규칙은 다시 보내지 않는다.
    · 발송에 성공(또는 테스트 모드)했을 때만 last_triggered_at 을 갱신한다. 실패는 alert_logs 에 남긴다.
    """
    now = now or config.now_kst()
    if send_fn is None:
        import authflow  # 순환 임포트 방지: 호출 시점에만 가져온다
        send_fn = authflow.send_sms_text

    observed_all = alert_metric_values(market_payload)
    rules = db.list_active_alert_rules(conn)
    result: dict = {
        "ok": True, "checked": 0, "matched": 0, "sent": 0, "test_mode": 0,
        "skipped_cooldown": 0, "failed": 0, "missing_metrics": [], "triggers": [],
    }
    missing: set[str] = set()
    for rule in rules:
        metric = rule.get("metric") or ""
        observed = observed_all.get(metric)
        if observed is None:
            missing.add(metric)
            continue
        result["checked"] += 1
        if not alert_condition_met(observed, rule.get("operator") or "", float(rule["threshold"])):
            continue
        result["matched"] += 1
        left = alert_cooldown_left(rule, now)
        if left > 0:
            result["skipped_cooldown"] += 1
            result["triggers"].append({
                "rule_id": rule["id"], "phone": rule["phone"], "metric": metric,
                "observed": observed, "status": "cooldown", "cooldown_left": left,
            })
            continue

        message = format_alert_message(rule, observed, now)
        try:
            sent = send_fn(rule["phone"], message)
        except Exception as exc:  # noqa: BLE001 — 한 건 실패가 전체 평가를 멈추지 않는다
            sent = {"ok": False, "error": str(exc)}
        if isinstance(sent, bool):
            sent = {"ok": sent, "sent": sent}
        sent = sent if isinstance(sent, dict) else {"ok": False, "error": "발송 결과를 확인할 수 없습니다."}
        ok = bool(sent.get("ok"))
        if ok:
            status = "sent" if sent.get("sent") else "test_mode"
            db.mark_alert_rule_triggered(conn, int(rule["id"]), config.iso_kst(now))
            if status == "sent":
                result["sent"] += 1
            else:
                result["test_mode"] += 1
        else:
            status = "failed"
            result["failed"] += 1
        db.log_alert_delivery(
            conn, int(rule["id"]), rule["phone"], metric, observed, message, status,
            config.iso_kst(now),
        )
        result["triggers"].append({
            "rule_id": rule["id"], "phone": rule["phone"], "metric": metric,
            "observed": observed, "status": status, "message": message,
        })
    result["missing_metrics"] = sorted(missing)
    log(
        f"[비상벨] 활성 규칙 {len(rules)}개 · 판정 {result['checked']}개 · 조건 도달 {result['matched']}개 "
        f"· 발송 {result['sent']}건(테스트 {result['test_mode']}) · 쿨다운 {result['skipped_cooldown']}건 "
        f"· 실패 {result['failed']}건"
    )
    return result


# ------------------------------------------------------------------ 글 저장


def _is_fresh(published_iso: str | None, verified_iso: str) -> bool:
    if not published_iso:
        return False
    try:
        return (config.parse_kst(verified_iso) - config.parse_kst(published_iso)) <= timedelta(hours=24)
    except Exception:
        return False


def store_posts(conn, posts: list[dict], max_new: int | None = None) -> tuple[list[dict], int, int]:
    """새 글을 저장한다. max_new 가 있으면 그 수만큼만 저장하고 나머지는 다음 실행으로 미룬다."""
    new_posts: list[dict] = []
    duplicates = 0
    deferred = 0
    for post in posts:
        post.setdefault("created_at", config.iso_kst())
        post["is_member_early"] = _is_fresh(post.get("published_at"), post["verified_at"])
        if max_new is not None and len(new_posts) >= max_new:
            deferred += 1
            continue
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
    return new_posts, duplicates, deferred


def run(fed_limit: int = 5, sec_limit: int = 2, collect_market_too: bool = True,
        max_new: int | None = None) -> dict:
    config.ensure_dirs()
    db.init_db()
    verified_at = config.iso_kst()
    conn = db.connect()

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
    # max_new 가 있으면 "가장 최근에 나온 새 글"부터 그 수만큼만 발행한다.
    posts.sort(key=lambda p: p.get("published_at") or p["verified_at"])
    if max_new is not None:
        posts = list(reversed(posts))
    new_posts, duplicates, deferred = store_posts(conn, posts, max_new=max_new)

    market_payload = None
    if collect_market_too:
        try:
            market_payload = collect_market()
            save_market_cache(market_payload)
            source_status["market"] = f"ok ({len(market_payload['items'])})"
        except Exception as exc:  # noqa: BLE001
            source_status["market"] = f"fail: {exc}"
            log(f"[시장 지표] 수집 실패: {exc}")
        # 시세 수집 직후 타점 비상벨 판정. 알림 쪽 예외가 전체 수집을 멈추지 않게 따로 감싼다.
        if market_payload:
            try:
                alert_result = evaluate_market_alerts(conn, market_payload)
                source_status["alerts"] = (
                    f"sent {alert_result['sent']} · test {alert_result['test_mode']} "
                    f"· cooldown {alert_result['skipped_cooldown']} · fail {alert_result['failed']}"
                )
            except Exception as exc:  # noqa: BLE001
                source_status["alerts"] = f"fail: {exc}"
                log(f"[비상벨] 판정 실패: {exc}")
        else:
            source_status["alerts"] = "skip (시세 없음)"

    total = db.count_posts(conn)
    log("")
    log("=" * 72)
    log(f"수집 완료 · 신규 {len(new_posts)}건 · 중복/기존 {duplicates}건 · DB 전체 {total}건"
        + (f" · 다음 실행으로 {deferred}건 미룸" if deferred else ""))
    for key, status in source_status.items():
        log(f"  - {key:<8} {status}")
    log("=" * 72)
    conn.close()
    return {
        "new": len(new_posts),
        "duplicates": duplicates,
        "deferred": deferred,
        "max_new": max_new,
        "total": total,
        "source_status": source_status,
        "market": market_payload,
    }


def run_alerts_only() -> dict:
    """시세만 조회해 타점 비상벨을 판정한다(글 수집·발행 없음).

    비상벨을 1분 주기로 확인하려면 cron 에서 `python collector.py --alerts-only` 를 돌린다.
    """
    config.ensure_dirs()
    db.init_db()
    payload = None
    try:
        payload = collect_market()
        save_market_cache(payload)
    except Exception as exc:  # noqa: BLE001
        log(f"[시장 지표] 수집 실패: {exc}")
    conn = db.connect()
    try:
        result = evaluate_market_alerts(conn, payload or {})
    finally:
        conn.close()
    return {"market": bool(payload), "alerts": result}


def main() -> int:
    parser = argparse.ArgumentParser(description="ClareDEX v2 공식 원문 수집기")
    parser.add_argument("--fed-limit", type=int, default=5)
    parser.add_argument("--sec-limit", type=int, default=2)
    parser.add_argument("--no-market", action="store_true")
    parser.add_argument("--alerts-only", action="store_true",
                        help="시세만 조회해 타점 비상벨을 판정한다(비상벨 전용 cron 용)")
    parser.add_argument("--max-new", type=int, default=None,
                        help="한 번에 발행할 신규 글 수 (cron 은 1 → 하루 2회 → 2건/일)")
    args = parser.parse_args()
    if args.alerts_only:
        result = run_alerts_only()
        return 0 if result["market"] else 1
    result = run(fed_limit=args.fed_limit, sec_limit=args.sec_limit,
                 collect_market_too=not args.no_market, max_new=args.max_new)
    return 0 if result["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
