"""이메일·휴대폰 본인인증(OTP).

설계 원칙
  · 인증번호 6자리는 소금(salt) + SHA-256 해시로만 DB 에 저장한다.
  · 5분 만료 · 5회 입력 제한 · 재발송 30초 대기 · 10분에 6회 발송 제한.
  · 이메일은 SMTP 또는 Resend, 문자는 Solapi·Aligo·CoolSMS 를 .env 자격증명이 있을 때만 쓴다.
  · 자격증명이 없으면 "보냈다"고 말하지 않는다. delivery_mode="unconfigured" 와 함께
    화면에서 확인할 수 있는 테스트용 인증번호(preview_code)를 돌려주고 그 사실을 문구로 알린다.
  · 이메일 인증과 휴대폰 인증을 모두 통과해야 /api/signup 이 회원을 만든다.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr

import config

config.load_env()

import db  # noqa: E402

OTP_TTL = 300            # 인증번호 유효 시간(초)
OTP_MAX_ATTEMPTS = 5     # 인증번호 1건당 입력 가능 횟수
OTP_RESEND_SECONDS = 30  # 같은 대상 재발송 대기
OTP_WINDOW_SECONDS = 600 # 아래 횟수 제한 구간
OTP_WINDOW_MAX = 6       # 10분에 최대 6회 발송
VERIFICATION_TTL = 1800  # 인증 통과 후 가입 가능 시간(초) — 30분

CHANNEL_EMAIL = "email"
CHANNEL_SMS = "sms"
CHANNELS = (CHANNEL_EMAIL, CHANNEL_SMS)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
PHONE_RE = re.compile(r"^(01[016789])(\d{3,4})(\d{4})$")


# ------------------------------------------------------------------ 정규화
def normalize_email(value: str | None) -> str:
    return (value or "").strip().lower()


def normalize_phone(value: str | None) -> str | None:
    """010-1234-5678 / 01012345678 / +82 10-1234-5678 → 010-1234-5678."""
    raw = re.sub(r"[^0-9+]", "", (value or "").strip())
    if raw.startswith("+82"):
        raw = "0" + raw[3:]
    elif raw.startswith("82") and len(raw) in (12, 13):
        raw = "0" + raw[2:]
    m = PHONE_RE.match(raw)
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def masked_email(value: str) -> str:
    local, _, domain = normalize_email(value).partition("@")
    if not domain:
        return normalize_email(value)
    head = local[:2] if len(local) > 2 else local[:1]
    return f"{head}{'*' * max(1, len(local) - len(head))}@{domain}"


def masked_phone(value: str | None) -> str:
    phone = normalize_phone(value)
    if not phone:
        return ""
    parts = phone.split("-")
    return f"{parts[0]}-{parts[1][:1]}***-{parts[2][-2:]}"


# ------------------------------------------------------------------ 발송 채널
def email_mode() -> str:
    if (os.environ.get("SMTP_HOST") or "").strip():
        return "smtp"
    if (os.environ.get("RESEND_API_KEY") or "").strip():
        return "resend"
    return "unconfigured"


def sms_mode() -> str:
    provider = (os.environ.get("SMS_PROVIDER") or "").strip().lower()
    key = (os.environ.get("SMS_API_KEY") or "").strip()
    secret = (os.environ.get("SMS_API_SECRET") or "").strip()
    sender = normalize_phone(os.environ.get("SMS_SENDER"))
    if not (key and sender):
        return "unconfigured"
    if provider in ("solapi", "coolsms") and secret:
        return provider
    if provider == "aligo":
        return "aligo"
    return "unconfigured"


def delivery_status() -> dict:
    email = email_mode()
    sms = sms_mode()
    return {
        "email": {
            "mode": email,
            "configured": email != "unconfigured",
            "from": (os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or "") if email == "smtp" else "",
            "note": {
                "smtp": "SMTP 서버로 실제 메일을 발송합니다.",
                "resend": "Resend API 로 실제 메일을 발송합니다.",
                "unconfigured": "SMTP_HOST/SMTP_USER/SMTP_PASS 또는 RESEND_API_KEY 가 없어 실제 메일이 나가지 않습니다.",
            }[email],
        },
        "sms": {
            "mode": sms,
            "configured": sms != "unconfigured",
            "sender": masked_phone(os.environ.get("SMS_SENDER")) if sms != "unconfigured" else "",
            "note": {
                "solapi": "Solapi 문자 API 로 실제 문자를 발송합니다.",
                "coolsms": "CoolSMS 문자 API 로 실제 문자를 발송합니다.",
                "aligo": "Aligo 문자 API 로 실제 문자를 발송합니다.",
                "unconfigured": "SMS_PROVIDER·SMS_API_KEY·SMS_API_SECRET·SMS_SENDER 가 없어 실제 문자가 나가지 않습니다.",
            }[sms],
        },
        "otp_ttl": OTP_TTL,
        "verification_ttl": VERIFICATION_TTL,
        "sandbox": email == "unconfigured" or sms == "unconfigured",
    }


def _http_json(url: str, payload: dict, headers: dict[str, str], timeout: int = 12) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            body = res.read().decode("utf-8", "replace")
            try:
                return res.status, json.loads(body) if body else {}
            except json.JSONDecodeError:
                return res.status, {"raw": body[:300]}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace") if exc.fp else ""
        try:
            return exc.code, json.loads(body) if body else {}
        except json.JSONDecodeError:
            return exc.code, {"raw": body[:300]}


def _send_email_smtp(to: str, subject: str, text: str) -> tuple[bool, str]:
    host = (os.environ.get("SMTP_HOST") or "").strip()
    try:
        port = int((os.environ.get("SMTP_PORT") or "587").strip())
    except ValueError:
        port = 587
    user = (os.environ.get("SMTP_USER") or "").strip()
    password = os.environ.get("SMTP_PASS") or ""
    sender = (os.environ.get("SMTP_FROM") or user or "").strip()
    if not sender:
        return False, "SMTP_FROM(또는 SMTP_USER)이 없습니다."
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((config.BRAND, sender))
    msg["To"] = to
    msg.set_content(text)
    context = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15, context=context) as smtp:
                if user:
                    smtp.login(user, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                smtp.ehlo()
                if port != 25:
                    smtp.starttls(context=context)
                    smtp.ehlo()
                if user:
                    smtp.login(user, password)
                smtp.send_message(msg)
    except Exception as exc:  # noqa: BLE001
        return False, f"SMTP 발송 실패: {exc}"
    return True, "smtp"


def _send_email_resend(to: str, subject: str, text: str) -> tuple[bool, str]:
    api_key = (os.environ.get("RESEND_API_KEY") or "").strip()
    sender = (os.environ.get("RESEND_FROM") or os.environ.get("SMTP_FROM") or "ClareDEX <onboarding@resend.dev>").strip()
    status, body = _http_json(
        "https://api.resend.com/emails",
        {"from": sender, "to": [to], "subject": subject, "text": text},
        {"Authorization": f"Bearer {api_key}"},
    )
    if 200 <= status < 300 and body.get("id"):
        return True, "resend"
    return False, f"Resend 발송 실패(HTTP {status}): {str(body)[:200]}"


def send_email(to: str, subject: str, text: str) -> tuple[str, bool, str | None]:
    """(delivery_mode, ok, error). unconfigured 이면 ok=False, error=None."""
    mode = email_mode()
    if mode == "smtp":
        ok, detail = _send_email_smtp(to, subject, text)
        return mode, ok, None if ok else detail
    if mode == "resend":
        ok, detail = _send_email_resend(to, subject, text)
        return mode, ok, None if ok else detail
    return "unconfigured", False, None


def _solapi_signature(api_secret: str, date: str, salt: str) -> str:
    return hmac.new(api_secret.encode("utf-8"), (date + salt).encode("utf-8"), hashlib.sha256).hexdigest()


def _send_sms_solapi(to: str, text: str) -> tuple[bool, str]:
    key = (os.environ.get("SMS_API_KEY") or "").strip()
    secret = (os.environ.get("SMS_API_SECRET") or "").strip()
    sender = normalize_phone(os.environ.get("SMS_SENDER")) or ""
    date = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    salt = secrets.token_hex(16)
    signature = _solapi_signature(secret, date, salt)
    auth = f"HMAC-SHA256 apiKey={key}, date={date}, salt={salt}, signature={signature}"
    status, body = _http_json(
        "https://api.solapi.com/messages/v4/send",
        {"message": {"to": re.sub(r"[^0-9]", "", to), "from": re.sub(r"[^0-9]", "", sender), "text": text}},
        {"Authorization": auth},
    )
    if 200 <= status < 300:
        return True, "solapi"
    return False, f"Solapi 발송 실패(HTTP {status}): {str(body)[:200]}"


def _send_sms_coolsms(to: str, text: str) -> tuple[bool, str]:
    # CoolSMS v4 는 Solapi 와 같은 엔드포인트 규격을 쓴다.
    return _send_sms_solapi(to, text)


def _send_sms_aligo(to: str, text: str) -> tuple[bool, str]:
    key = (os.environ.get("SMS_API_KEY") or "").strip()
    user_id = (os.environ.get("SMS_USER_ID") or key).strip()
    sender = normalize_phone(os.environ.get("SMS_SENDER")) or ""
    data = urllib.parse.urlencode({
        "key": key, "user_id": user_id, "sender": re.sub(r"[^0-9]", "", sender),
        "receiver": re.sub(r"[^0-9]", "", to), "msg": text, "testmode_yn": "N",
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://apis.aligo.in/send/", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            body = json.loads(res.read().decode("utf-8", "replace") or "{}")
    except Exception as exc:  # noqa: BLE001
        return False, f"Aligo 발송 실패: {exc}"
    if str(body.get("result_code")) == "1":
        return True, "aligo"
    return False, f"Aligo 발송 실패: {str(body)[:200]}"


def send_sms(to: str, text: str) -> tuple[str, bool, str | None]:
    mode = sms_mode()
    if mode == "solapi":
        ok, detail = _send_sms_solapi(to, text)
        return mode, ok, None if ok else detail
    if mode == "coolsms":
        ok, detail = _send_sms_coolsms(to, text)
        return mode, ok, None if ok else detail
    if mode == "aligo":
        ok, detail = _send_sms_aligo(to, text)
        return mode, ok, None if ok else detail
    return "unconfigured", False, None


# ------------------------------------------------------------------ OTP 저장
def _new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _hash_code(code: str) -> str:
    salt = secrets.token_hex(8)
    digest = hashlib.sha256((salt + code).encode("utf-8")).hexdigest()
    return f"sha256${salt}${digest}"


def _check_hash(stored: str, code: str) -> bool:
    try:
        algo, salt, digest = stored.split("$", 2)
    except ValueError:
        return False
    if algo != "sha256":
        return False
    return hmac.compare_digest(hashlib.sha256((salt + code).encode("utf-8")).hexdigest(), digest)


def _now() -> datetime:
    return config.now_kst()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    return config.parse_kst(value)


def _seconds_left(value: str | None) -> int:
    dt = _parse(value)
    if not dt:
        return 0
    return max(0, int((dt - _now()).total_seconds()))


def email_text(code: str) -> tuple[str, str]:
    subject = f"[{config.BRAND}] 이메일 본인인증 번호 {code}"
    text = (
        f"{config.BRAND} 이메일 본인인증 안내\n\n"
        f"인증번호: {code}\n"
        f"유효 시간: {OTP_TTL // 60}분\n\n"
        "위 인증번호를 가입 화면에 입력하면 이메일 인증이 완료됩니다.\n"
        "본인이 요청하지 않았다면 이 메일을 무시하시면 됩니다. 인증번호는 누구에게도 알려주지 마세요.\n\n"
        f"{config.OPERATOR} · {config.BRAND}\n"
    )
    return subject, text


def sms_text(code: str) -> str:
    return f"[{config.BRAND}] 인증번호 [{code}] {OTP_TTL // 60}분 안에 입력해 주세요."


def issue(channel: str, target: str, email: str | None = None) -> dict:
    """인증번호 발급 + 발송. 실패해도 예외 대신 사유가 담긴 dict 를 돌려준다."""
    if channel not in CHANNELS:
        return {"ok": False, "code": "bad_channel", "error": "지원하지 않는 인증 채널입니다."}

    if channel == CHANNEL_EMAIL:
        target = normalize_email(target)
        if not EMAIL_RE.match(target):
            return {"ok": False, "code": "bad_email", "error": "이메일 형식을 확인해 주세요."}
        bind_email = target
    else:
        phone = normalize_phone(target)
        if not phone:
            return {"ok": False, "code": "bad_phone",
                    "error": "휴대폰 번호 형식을 확인해 주세요. 예) 010-1234-5678"}
        target = phone
        bind_email = normalize_email(email)
        if not bind_email:
            return {"ok": False, "code": "email_required", "error": "이메일 인증을 먼저 진행해 주세요."}
        with db.session() as conn:
            state = verification_state(conn, bind_email)
        if not state["email_verified"]:
            return {"ok": False, "code": "email_required",
                    "error": "이메일 본인인증을 먼저 완료해 주세요.", "email_verified": False}

    with db.session() as conn:
        last = db.latest_otp(conn, channel, target)
        if last:
            left = OTP_RESEND_SECONDS - int((_now() - _parse(last["sent_at"])).total_seconds())
            if left > 0:
                return {"ok": False, "code": "cooldown", "retry_after": left,
                        "error": f"{left}초 뒤에 다시 요청해 주세요."}
        since = config.iso_kst(_now() - timedelta(seconds=OTP_WINDOW_SECONDS))
        if db.count_recent_otp(conn, channel, target, since) >= OTP_WINDOW_MAX:
            return {"ok": False, "code": "rate_limited",
                    "error": "요청이 너무 많습니다. 10분 뒤에 다시 시도해 주세요."}

    code = _new_code()
    sent_at = _now()
    expires_at = sent_at + timedelta(seconds=OTP_TTL)

    if channel == CHANNEL_EMAIL:
        subject, text = email_text(code)
        mode, ok, error = send_email(target, subject, text)
    else:
        mode, ok, error = send_sms(target, sms_text(code))

    if mode != "unconfigured" and not ok:
        return {"ok": False, "code": "delivery_failed", "delivery_mode": mode,
                "error": error or "발송에 실패했습니다. 잠시 후 다시 시도해 주세요.",
                "sent": False}

    with db.session() as conn:
        db.insert_otp(
            conn, channel=channel, target=target, code_hash=_hash_code(code),
            delivery_mode=mode, sent_at=config.iso_kst(sent_at),
            expires_at=config.iso_kst(expires_at), email=bind_email,
        )

    payload = {
        "ok": True,
        "sent": mode != "unconfigured",
        "delivery_mode": mode,
        "channel": channel,
        "target": masked_email(target) if channel == CHANNEL_EMAIL else masked_phone(target),
        "expires_in": OTP_TTL,
        "retry_after": OTP_RESEND_SECONDS,
    }
    if mode == "unconfigured":
        # 실제로 보내지 못했다. 화면에서 확인할 수 있게 테스트용 코드를 함께 돌려준다.
        payload["preview_code"] = code
        payload["delivery_note"] = (
            "발송 채널이 설정되지 않아 실제 "
            + ("이메일이" if channel == CHANNEL_EMAIL else "문자가")
            + " 나가지 않았습니다. 테스트용 인증번호를 화면에 표시합니다. "
            + (".env 의 SMTP_HOST/SMTP_USER/SMTP_PASS 또는 RESEND_API_KEY" if channel == CHANNEL_EMAIL
               else ".env 의 SMS_PROVIDER/SMS_API_KEY/SMS_API_SECRET/SMS_SENDER")
            + " 를 설정하면 실제 발송으로 전환됩니다."
        )
        payload["message"] = "발송 채널 미설정 · 테스트 모드로 인증번호를 표시했습니다."
    else:
        payload["message"] = ("인증번호를 보냈습니다. " if channel == CHANNEL_EMAIL else "인증번호 문자를 보냈습니다. ") \
                             + f"{OTP_TTL // 60}분 안에 입력해 주세요."
    return payload


def verify(channel: str, target: str, code: str, email: str | None = None) -> dict:
    if channel not in CHANNELS:
        return {"ok": False, "code": "bad_channel", "error": "지원하지 않는 인증 채널입니다."}
    code = re.sub(r"\D", "", str(code or ""))
    if len(code) != 6:
        return {"ok": False, "code": "bad_code", "error": "인증번호 6자리를 입력해 주세요."}

    if channel == CHANNEL_EMAIL:
        target = normalize_email(target)
        bind_email = target
    else:
        phone = normalize_phone(target)
        if not phone:
            return {"ok": False, "code": "bad_phone", "error": "휴대폰 번호 형식을 확인해 주세요."}
        target = phone
        bind_email = normalize_email(email)
        if not bind_email:
            return {"ok": False, "code": "email_required", "error": "이메일 인증을 먼저 진행해 주세요."}

    with db.session() as conn:
        row = db.latest_otp(conn, channel, target)
        if not row:
            return {"ok": False, "code": "no_otp", "error": "인증번호를 먼저 요청해 주세요."}
        if row["attempts"] >= OTP_MAX_ATTEMPTS:
            return {"ok": False, "code": "too_many", "error": "입력 횟수를 초과했습니다. 인증번호를 다시 받아 주세요."}
        if _seconds_left(row["expires_at"]) <= 0:
            return {"ok": False, "code": "expired", "error": "인증번호가 만료되었습니다. 다시 받아 주세요."}
        if not _check_hash(row["code_hash"], code):
            attempts = db.bump_otp_attempts(conn, row["id"])
            left = max(0, OTP_MAX_ATTEMPTS - attempts)
            return {"ok": False, "code": "mismatch",
                    "error": f"인증번호가 일치하지 않습니다. 남은 시도 {left}회.",
                    "attempts_left": left}
        db.consume_otp(conn, row["id"], config.iso_kst())
        if channel == CHANNEL_EMAIL:
            db.set_verification(conn, bind_email, email_ok=True)
        else:
            db.set_verification(conn, bind_email, phone=target, phone_ok=True)

    return {
        "ok": True,
        "channel": channel,
        "verified": True,
        "target": masked_email(target) if channel == CHANNEL_EMAIL else masked_phone(target),
    }


def verification_state(conn, email: str) -> dict:
    """이메일 기준 인증 상태. 인증 후 30분이 지나면 다시 인증해야 한다."""
    email = normalize_email(email)
    row = db.get_verification(conn, email) or {}
    email_at = _parse(row.get("email_verified_at"))
    phone_at = _parse(row.get("phone_verified_at"))
    now = _now()
    email_ok = bool(email_at and (now - email_at).total_seconds() <= VERIFICATION_TTL)
    phone_ok = bool(phone_at and (now - phone_at).total_seconds() <= VERIFICATION_TTL)
    stamps = [at for at in (email_at, phone_at) if at]
    oldest = min(stamps) if stamps else None
    return {
        "email": email,
        "phone": row.get("phone"),
        "email_verified": email_ok,
        "phone_verified": phone_ok and bool(row.get("phone")),
        "email_verified_at": row.get("email_verified_at") if email_ok else None,
        "phone_verified_at": row.get("phone_verified_at") if phone_ok else None,
        "expires_in": max(0, VERIFICATION_TTL - int((now - oldest).total_seconds())) if oldest else 0,
        "ttl": VERIFICATION_TTL,
    }
