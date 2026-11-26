# ClareDEX v2 — 웹 · 수집엔진 · 네이버 블로그 초안기

클레어덱스(웨일파트너스)의 1단계 승인 와이어프레임을 구현한 프로토타입입니다.
**공식 원문만 수집 → 사실만 요약 → 웹 발행 → 네이버 블로그 SEO 초안 생성 → 복사 발행**까지의 파이프라인을 담고 있습니다.

```
collector.py      공식 소스 수집 → SQLite 저장 → 브랜드 썸네일 PNG 생성 → 시장지표 캐시
naver_drafts.py   글마다 네이버 SEO 초안(제목·키워드·본문 HTML·CTA) 생성
server.py         정적 파일 + JSON API 서버 (표준 라이브러리만 사용)
verify.py         DB 무결성 + 실제 HTTP 자체 검증 (171개 항목)
public/           메인 · 회원가입 · 관리자 콘솔(/admin: 대시보드·회원·콘텐츠·수집기·블로그 초안·보안)
data/             claredex.db, market_cache.json, admin_auth.json(비밀번호 해시), collector_state.json
calendar.json     공식 일정(기관 원문에서 확인한 발표 시각) — 저장소 루트에서 버전 관리, Dockerfile 로 이미지에 포함
```

## 실행 방법

```bash
cd claredex-v2

python3 collector.py          # 1) 공식 원문 수집 (+ 썸네일·시장지표 캐시)
python3 naver_drafts.py       # 2) 네이버 블로그 SEO 초안 생성
python3 server.py --port 8000 # 3) 서버 실행 → http://localhost:8000
python3 verify.py             # 4) 자체 검증 (임시 포트로 서버를 띄웠다가 종료)
```

- 필수 패키지: Python 3.12 표준 라이브러리 + `Pillow`(썸네일). `lxml`/`matplotlib`은 쓰지 않았습니다.
- 한글 폰트: `/usr/share/fonts/pickle-hwp/NotoSansCJKkr-Regular.otf` 를 사용하며, 다른 환경에서는 `thumbnail.py` 의 `FONT_CANDIDATES` 에 경로를 추가하면 됩니다.
- `.env`(선택): `.env.example` 을 복사해 `OPENDART_API_KEY`, `ECOS_API_KEY` 를 넣으면 국내 공시·한국은행 기준금리 수집이 활성화됩니다. 키가 없으면 로그만 남기고 건너뜁니다.

### 운영자 화면(`/admin`) 비밀번호

`/admin/` 은 **운영자 전용 관리자 콘솔**이며 로그인 게이트 뒤에 있습니다.

- **최초 비밀번호**: `ADMIN_PASSWORD` 환경변수(또는 `.env`). 없으면 개발용 기본값 `claredex-admin-2026` 으로 동작하고 기동 시 경고를 남깁니다.
- **변경**: 로그인 후 **보안 · 설정 → 비밀번호 변경**. 현재 비밀번호를 확인한 뒤 새 비밀번호를 `data/admin_auth.json` 에 **PBKDF2-HMAC-SHA256 + salt(240,000회)** 해시로 저장합니다(권한 0600). 이 파일은 docker 볼륨(`./data:/app/data`)에 있어 **컨테이너를 재시작해도 유지**되며, 파일이 있으면 `ADMIN_PASSWORD` 보다 우선합니다.
- **복구**: 비밀번호를 잊었으면 서버에서 `data/admin_auth.json` 을 지우고 컨테이너를 재시작하면 `.env` 의 `ADMIN_PASSWORD` 로 돌아갑니다.
- 세션은 서버 메모리에만 두는 무작위 토큰이며 `HttpOnly; SameSite=Lax` 쿠키(12시간)로 전달됩니다. 서버를 재시작하면 모든 세션이 끊깁니다. 비밀번호를 바꾸면 현재 기기를 뺀 나머지 세션은 즉시 종료됩니다.
- 같은 IP에서 5분에 10회 이상 틀리면 잠시 `429` 로 차단합니다.
- `/api/admin/*` 와 `/api/naver-drafts` 는 모두 세션을 검사해 미로그인 시 `401` 을 돌려주고, 상태를 바꾸는 POST 는 같은 출처(Origin)만 허용합니다.

### 관리자 콘솔 구성

| 탭 | 내용 |
|---|---|
| 대시보드 | 총 회원수 · 오늘/7일 신규 · 총 속보·공시 · 블로그 초안 · 수집기 상태 KPI, 14일 가입 추이 차트, 카테고리·출처 분포, 최근 가입·최근 글 |
| 회원 관리 | SQLite `users` 검색(이메일·이름·관심자산)·가입경로/상태 필터·페이지네이션·CSV 내보내기 |
| 콘텐츠 관리 | 카테고리·출처·상태 필터, 공개↔숨김 전환(숨긴 글은 메인·공개 API에서 제외), 삭제, 본문 미리보기·원문 링크 |
| 수집기 · 시스템 | 마지막 실행·소요 시간·소스별 결과(Fed/SEC/DART/ECOS/시장), **지금 수집 실행**(`collector.py` → `naver_drafts.py`), 실행 로그, 시스템 정보 |
| 네이버 블로그 초안 | 기존 SEO 초안 목록·미리보기·HTML/텍스트·1클릭 복사 도구 |
| 보안 · 설정 | 비밀번호 변경(강도 표시), 해시 저장 방식·세션·잠금 정책, 최근 감사 로그 |

- 콘솔 화면은 공개 사이트와 완전히 분리된 레이아웃(사이드바 + 상단바 + KPI/테이블)이며 `noindex, nofollow`·`Cache-Control: no-store` 로 제공됩니다.
- 수집 실행은 백그라운드 스레드에서 처리되고 상태를 `data/collector_state.json` 에 남깁니다. 동시 실행과 30초 내 재실행은 거부합니다.

## 수집 소스와 처리 원칙

| 소스 | 용도 | 비고 |
|---|---|---|
| 미 연준 보도자료 RSS | 매크로 속보 | `federalreserve.gov/feeds/press_all.xml`, 최신 5건. 시장과 무관한 행사·수상 소식은 제외 |
| SEC EDGAR submissions | 8-K·10-Q·10-K 공시 | `User-Agent: ClareDEX AdminContact@claredex.com` 필수, 애플·엔비디아 각 최신 2건 |
| 업비트 시세 API | BTC/ETH 원화 시세 | 김치프리미엄 계산의 원화 가격 |
| Binance → Binance.US → Coinbase | BTC/USD | 이 환경에서는 Binance가 HTTP 451(지역 제한)로 막혀 **Binance.US로 자동 전환**되며, 응답의 `source`에 실제 사용한 소스가 표기됩니다 |
| OKX 롱/숏 계정 비율 | BTC 롱숏 | Binance Futures가 차단되어 OKX로 대체. 계산식은 응답 `note`에 명시 |
| Alternative.me | 공포탐욕지수 | 일별 지수 |
| exchangerate-api (open.er-api.com) | USD/KRW | 일별 고시 환율. 김치프리미엄 계산에 사용 |
| OpenDART / 한국은행 ECOS | 국내 공시·기준금리 | `.env` 키가 있을 때만 동작하는 어댑터 |

**사실성 원칙**
- 수치·인용은 원문 응답에서 받은 값만 사용하며, 생성한 문장에 숫자를 새로 만들지 않습니다.
- 모든 글은 `source_url`(정확한 원문 링크)과 KST `verified_at`(확인 시각)을 저장합니다.
- `source_hash = sha256(source_url)` + `UNIQUE` 로 중복 발행을 차단합니다.
- 해석 문구는 "영향을 줄 수 있습니다" 수준의 조건문이며 매수·매도 권유·목표가·수익 보장 표현을 쓰지 않습니다.
- 연준 제목을 한국어로 확신할 수 없으면 원문 제목을 그대로 병기합니다.

## DB 스키마 (`data/claredex.db`)

- **posts** — id, title, category, source_name, source_grade, source_url, `source_hash UNIQUE`, verified_at, summary_lines(JSON, 3줄), market_impact, tags(JSON), image_path, is_member_early, created_at, **published_at**(원문 발표 시각, 추가), **topic**(초안 생성용 주제 키, 추가), **status**(`published` / `hidden` — 운영자 공개·숨김)
- **users** — id, **email UNIQUE(NULL 허용 — 휴대폰 단독 가입)**, name, provider(`email`/`phone`/`kakao`/`google`/`naver`), watchlist(JSON), created_at, **status**(`active` / `paused`), **phone**(휴대폰, 정규화 `010-1234-5678`, 인덱스), **email_verified**, **phone_verified**, **marketing_agreed**, **marketing_agreed_at**(최초 동의 시각). 예전 DB(email NOT NULL)는 기동 시 자동으로 NULL 허용 테이블로 옮겨지고 값·id·가입일은 그대로 보존됩니다.
- **naver_drafts** — id, post_id UNIQUE, seo_title, keywords(JSON 5~8), body_html, body_text, telegram_cta, web_cta, image_path, created_at
- **auth_otp** — 본인인증 인증번호(채널 `email`/`sms`, 대상, 코드 **해시**, 발송 시각, 만료, 시도 횟수, 소비 시각). 코드 평문은 저장하지 않습니다.
- **signup_verification** — 이메일별 인증 통과 상태(휴대폰, 이메일 인증 시각, 휴대폰 인증 시각). 인증 후 30분 안에만 가입 가능.
- **phone_auth_state** — 휴대폰 번호별 인증 통과 상태(인증 시각, 갱신 시각). 이메일 없이 가입하는 1단계 퍼널의 기준이며 인증 후 30분 안에만 가입 가능.
- **naver_publish** — 네이버 발행 이력(draft_id, post_id, status, url, message, attempted_at, published_at)

`published_at`·`topic` 은 brief 의 필수 컬럼에 더해 사실 보존과 초안 품질을 위해 추가한 컬럼입니다.

## HTTP API

| Method | Path | 설명 |
|---|---|---|
| GET | `/` | 메인 (티커·히어로·오늘의 한 장·카운트다운·속보 타임라인·컴플라이언스 푸터) |
| GET | `/signup.html`, `/signup` | 가치 먼저 · 휴대폰 1단계 가입 퍼널 (관심자산 → 3초 브리핑 → 휴대폰 OTP) |
| GET | `/admin`, `/admin/` | 운영자 전용 관리자 콘솔 (미로그인 시 로그인 게이트, `noindex`·`no-store`) |
| GET | `/admin/admin.css`, `/admin/app.js` | 콘솔 전용 자산 |
| GET | `/api/ticker` | BTC KRW/USD, 김치프리미엄, 공포탐욕, 롱숏, USD/KRW, ETH. 실패 시 `data/market_cache.json` 폴백(`stale: true`) |
| GET | `/api/posts?category=&tag=&q=&importance=&limit=` | 속보 목록 (importance=S/A/B 필터) |
| GET | `/api/naver-drafts` | 초안 목록 (본문 HTML 포함) — **운영자 로그인 필요, 미로그인 401** |
| GET | `/api/calendar` | KST 공식 일정 (중요도 S/A/B·기관·공식 원문 링크 포함) |
| GET | `/api/stats` | 오늘(KST 자정 기준)·최근 확인 건수, 전체 글 수 |
| GET | `/api/auth/channels` | 본인인증 발송 채널 상태(SMTP/Resend·SMS 설정 여부, 샌드박스 여부) |
| POST | `/api/auth/send-email-otp` | `{email}` → 6자리 인증번호 발송(5분 유효, 30초 재발송 대기) |
| POST | `/api/auth/verify-email-otp` | `{email, code}` → 이메일 인증 통과 |
| POST | `/api/auth/send-sms-otp` | `{phone, email?}` → 휴대폰 인증번호 발송 (**이메일 없이도 가능**, 이메일 인증 선행 없음) |
| POST | `/api/auth/verify-sms-otp` | `{phone, code, email?}` → 휴대폰 인증 통과(번호 기준 `phone_auth_state` 기록) |
| GET | `/api/admin/session` | 현재 운영자 세션 상태 `{authenticated, password_source}` |
| GET | `/api/admin/overview` | KPI · 14일 가입 추이 · 카테고리/출처 분포 · 최근 가입/글 · 수집기 상태 |
| GET | `/api/admin/users?q=&provider=&status=&limit=&offset=` | 회원 목록 + 전체 건수 |
| GET | `/api/admin/posts?category=&source=&status=&q=&limit=&offset=` | 글 목록(상태 포함) + 필터 옵션 + 통계 |
| GET | `/api/admin/collector` | 수집기 실행 상태 · 최근 결과 · 로그 |
| GET | `/api/admin/security` | 비밀번호 저장 방식 · 보안 정책 · 감사 로그 |
| POST | `/api/admin/posts/status` | `{id, status}` 공개↔숨김 |
| POST | `/api/admin/posts/delete` | `{id}` 글 삭제(연결 초안 포함) |
| POST | `/api/admin/collect` | 수집기 + 초안 생성 백그라운드 실행 (`202`, 실행 중 `409`) |
| POST | `/api/admin/change-password` | `{current_password, new_password, confirm_password}` → `data/admin_auth.json` 해시 갱신 |
| POST | `/api/signup` | `{phone, watchlist, email?, name?, marketing_agreed?, provider?, alert_rule?}` → **휴대폰 인증 필수 · 이메일은 인증번호 없이 형식만 확인해 저장 · 성함/닉네임 선택**. users 저장(name 포함) + 관심자산 즉시 저장 (201). `alert_rule`(`{metric, operator, threshold, label?}`)을 보내면 타점 비상벨 조건도 함께 저장. 휴대폰 미인증 403 `verification_required` · 잘못된 이메일 형식 400 · 잘못된 alert_rule 400 `invalid_alert_rule` · 이미 다른 회원이 쓰는 이메일/휴대폰 409 `email_in_use` |
| GET | `/api/alerts?phone=` | 내 타점 비상벨 조건 조회(활성 규칙 + 최근 이력). 가입되지 않은 번호 404 `not_member` |
| POST | `/api/alerts` | `{phone, alert_rule:{metric, operator, threshold, label?}}` → 조건 저장·교체(활성 1개) · `{phone, active:false}` → 끄기 |
| POST | `/api/watchlist` | `{email\|phone, watchlist}` → 관심자산 갱신 (이메일 없는 휴대폰 단독 회원은 `phone`) |
| POST | `/api/admin/login` | `{password}` → 200 + `Set-Cookie: claredex_admin=…` (틀리면 401) |
| POST | `/api/admin/logout` | 세션 폐기 + 쿠키 만료 (`Max-Age=0`) |

## 화면

- **메인 (TUPL 톤 · 오프화이트 `#FAF8F4` · 720px 단일 컬럼)** — 시장 지표 티커(항목별 출처 표기) → 히어로 → **오늘의 한 장**(다크 카드 1장, 최근 72시간 S등급 우선 · `내 종목 야간 S등급 비상벨 켜기 →` 는 글 태그에 따라 `signup.html?watch=BTC|NVDA|AAPL|연준·금리` 로 연결) → 주요 속보 캐러셀 → 최신 속보(관심자산 1초 미리보기 BTC·NVDA·AAPL·연준·금리, S/A/B·카테고리 필터, 3줄 요약·시장 영향 한 줄·`공식 원문 ↗`) → 공식 일정(KST 카운트다운·D-day·S/A/B 배지·`이 일정 발표 알림 받기 →`) → 무료/Pro 안내 → 컴플라이언스 푸터.
- **회원가입 (540px 단일 컬럼 카드 · 가치 먼저, 휴대폰 1개만 마지막에)** — **1단계** 관심자산 칩 10종(BTC·NVDA·AAPL·연준·금리·삼성전자·SK하이닉스·ETH·TSLA·나스닥·코스피)을 고르면 `/api/posts`·`/api/calendar` 에서 그 자산의 최근 공식 공시 1~2건과 다가오는 S등급 일정을 즉시 보여줍니다(가입 전 미리 보는 3초 브리핑). **2단계**는 「잠든 사이 내 종목 S등급 기습 공시, 3초 결론으로 깨워드립니다」 아래 **휴대폰 번호 + 인증번호 6자리**(`/api/auth/send-sms-otp`·`verify-sms-otp`), 그리고 **이메일(선택)** 과 **성함 또는 닉네임(선택)** 입력란이 접힘 없이 바로 노출됩니다. 이메일은 **인증번호 발송·확인 없이 형식만 실시간으로 확인**해 저장하고(비워 두면 휴대폰만), 성함/닉네임은 `/api/signup` 의 `name` 으로 전달돼 `users.name` 과 `/admin` 회원 목록에 그대로 보입니다. 카카오·구글(준비 중) 버튼은 없습니다. 「마케팅 활용 동의」는 체크박스 + `[보기]` 모달(수집·이용 항목, 목적, 제3자 제공, 보유 기간, 거부 권리·불이익, 전송 방법·수신 거부)이며 선택 사항입니다. 완료 화면에는 방금 예약된 관심자산과 **12월 S등급 일정(고용보고서 12/4 · FOMC 12/10 · CPI 12/10)** 카드가 뜹니다. `?watch=FOMC` 처럼 일정에서 넘어오면 `config.EVENT_WATCH_ALIASES` 규칙으로 해당 관심자산을 자동 선택하고 화면 위에 1줄로 안내합니다. 2단계에는 **김프·공포탐욕·롱숏 타점 비상벨** 패널이 있습니다 — `/api/market` 의 현재 수치(김프·공포탐욕·롱 비율)와 5가지 원클릭 프리셋(🔥 김프 +3.0% 이상 · ❄️ 역프 -0.5% 이하 · 😱 공포탐욕 15 이하 · 🤑 공포탐욕 75 이상 · ⚖️ 롱 비율 65% 돌파), 그리고 직접 숫자 입력. `signup.html?alert=kimp_high` 처럼 들어오면 해당 프리셋이 자동 선택되고, 완료 화면에 설정된 타점 조건이 표시됩니다. 메인 상단에는 「⚡ 김프·공포탐욕·롱숏 1초 비상벨 설정 →」(→ `signup.html?alert=kimp_high`) 바가 있습니다.
- **운영자 페이지(/admin)** — 비밀번호 로그인 게이트 → 초안 목록, 제목/서식 유지 본문/HTML/태그/이미지 링크 복사, 미리보기·HTML·텍스트 탭, 로그아웃. 정적 프리뷰에서는 초안 대신 비공개 안내를 표시합니다.

## 공식 일정(카운트다운) 데이터

`calendar.json`(저장소 루트)에 기관 공식 일정 페이지에서 확인한 7건을 넣었습니다. 화면·API·정적 스냅샷은 `config.load_calendar()` 가 정규화한 결과(중요도 S/A/B·라벨·기관·발표 시각·공식 원문 링크)를 그대로 씁니다.
예전 배포본이 쓰던 `data/calendar.json` 은 읽기 폴백으로만 남겨 두었습니다(볼륨 보존).

## 네이버 블로그 자동화 범위

네이버는 2020년 글쓰기 API를 종료해 자동 발행은 **Playwright + 저장된 로그인 세션(storage state)** 으로 처리합니다(`naver_publish.py`).
호스트 cron 이 07:10·19:10 에 `collect.sh` 를 실행하면 `collector.py --max-new 1` → `naver_drafts.py` → `naver_publish.py --limit 1` 순서로 돌아 **하루 2건**이 사이트와 네이버 블로그에 함께 올라갑니다.
세션 파일(`data/naver_state.json`)이 없으면 발행을 시도하지 않고 `status="no_session"` 으로 기록하며, `/admin` 수집기 탭에 그대로 표시됩니다. 세션 만드는 방법은 `naver_publish.py` 상단 주석 참고 (`python naver_publish.py --login` → `data/naver_state.json` 을 서버로 복사).
초안에는
- SEO 제목(50자 이내)과 키워드 5~8개,
- 3줄 요약 + 시장 영향 해석 + 원문 출처/확인 시각,
- `https://t.me/Clare_PerpDEX` 로 연결되는 클릭 이미지,
- 주제별 텔레그램·웹 CTA(최근 10개 초안과 `difflib.SequenceMatcher` 로 비교해 가장 덜 겹치는 조합 선택),
- 하단 투자 유의 고지(디스클레이머) — 신고 상태를 단정하는 문구는 넣지 않음
가 들어갑니다.

> 초안 본문 이미지 URL은 `CLAREDEX_PUBLIC_BASE_URL`(기본 `https://www.claredex.com`) 기준입니다. `public/images/posts/*.png` 를 실제 도메인에 배포해야 네이버에서 이미지가 보입니다.

## 검증 결과 (요약)

`python3 verify.py` → **233/233 통과**
- 타점 비상벨: `alert_rules`·`alert_logs` 필수 컴럼, `POST /api/signup` + `alert_rule` 201·SQLite 저장(지표·연산자·임계치·6시간 쿨다운·회원 연결), 잘못된 지표·범위 밖 임계값 400 `invalid_alert_rule`, `GET /api/market` 지표, `GET`/`POST /api/alerts` 조회·변경·끄기(활성 1개 정책), 관리자 회원 목록의 타점 요약
- 비상벨 엔진: 임계치 도달 시 `[클레어덱스 비상벨]` 1줄 발송 · 6시간 쿨다운 내 중복 차단 · 미도달/지표 없음 미발송 · 공포탐욕 조건 · 발송 실패는 실패 기록 후 재시도 가능 · SMS 미설정 시 `test_mode` 안전 반환
- 3개 테이블·필수 컬럼(중요도 `posts.importance` 포함), `sha256(source_url)` 일치·중복 없음, 3줄 요약·태그·KST · 금지 표현 없음, 9개 썸네일 1200×630 PNG
- 중요도: 모든 글 S/A/B 유효값 + 라벨, `content.fed_post` 생성 시 등급 계산, `posts.importance` 저장·재조회, `/api/posts?importance=S` 필터
- 공식 일정: `calendar.json` 정규화(중요도·라벨·기관·원문 링크), watch→관심자산 매핑, 발표 시각 오름차순, `/api/calendar` 응답
- 가입 화면: 관심자산 목록 = `config.WATCHLIST_ASSETS`, 일정→관심자산 매핑 = `config.EVENT_WATCH_ALIASES`, `?watch=` 자동 선택, 실제 OTP·가입 API 연동, 카카오·구글(준비 중)·성함 필수 입력 제거, 3초 브리핑·비상벨 카피·12월 S등급 일정 카드, 540px 단일 컬럼
- 메인 CTA: `내 종목 야간 S등급 비상벨 켜기 →` · `고른 자산 S등급 급변 시 문자로 바로 받기 (광고 전화 0건) →` · `30초 만에 무료 비상벨 켜기 →`
- 메인: `오늘의 한 장` CTA 의 `watchKeyForPost` 연결, 관심자산 칩 4종, 화면 순서(티커→히어로→오늘의 한 장→최신 속보)
- 글 1건당 초안 1건, 키워드 5~8, 텔레그램/웹 링크·고지·이미지 링크 포함, CTA 최대 유사도 0.40
- HTTP: `/`, `/signup.html`, `/signup`, `/admin`·`/admin/`(관리자 콘솔 200·`no-store`·`noindex`), `/admin/admin.css`·`/admin/app.js`, `/assets/style.css`, `/images/posts/1.png`, `/api/ticker`, `/api/posts`(+category·q·importance), `/api/stats`, `/api/calendar` 200 / `/admin-blog.html`·`/admin-blog` 404
- 관리자 API 보호: 미로그인 `/api/admin/*`·`/api/naver-drafts` 전부 401 → 틀린 비밀번호 401 → `login` 200·`HttpOnly; SameSite=Lax` 쿠키
- 대시보드: KPI 6종·14일 가입 추이(빈 날 0 포함)·분포·최근 목록 / 회원 목록 / 글 목록(상태)
- 콘텐츠: 숨김 전환 시 공개 `/api/posts` 에서 제외 → 재공개, 임시 글 삭제 후 DB에서 사라짐
- 비밀번호 변경: 현재 비밀번호 오류 401 · 10자 미만/확인 불일치 400 · 성공 200 → `admin_auth.json` 에 평문 없이 PBKDF2+salt(0600) → 이전 비밀번호 401 · 새 비밀번호 200 → 파일 재로딩 시에도 새 비밀번호만 통과
- 수집 작업: 스텁 단계로 시작→완료 상태 파일 기록, 30초 내 재실행 거부
- 정적 프리뷰: `export_static.py` 가 `public/data/naver-drafts.json` 을 만들지 않음, `posts.json`·`calendar.json` 에 중요도 포함, `stats.json` 생성, 숨김 글 미포함, `public/admin` 에 비밀번호·DB 파일 없음
- POST `/api/signup` 201 → `/api/watchlist` 200 → SQLite 저장 확인, 같은 이메일 재가입 시 관심자산 보존, 잘못된 이메일 400, 미지원 자산 4xx, 없는 경로 404, 상위 경로 403, 테스트 서버 정상 종료
- 휴대폰 1단계: 이메일 없이 `send-sms-otp` → `verify-sms-otp` → `POST /api/signup` 201(이메일 NULL·provider=phone·관심자산 저장), 휴대폰 기준 `/api/watchlist` 200, 관리자 회원 목록에서 이메일 없는 회원 검색·표시(UNIQUE·목록 안 깨짐)

## GitHub Pages 정적 프리뷰

서버 없이 화면만 그대로 확인할 수 있도록 정적 스냅샷 배포를 넣었습니다.

```bash
python3 export_static.py   # data/*.json + DB → public/data/{posts,ticker,calendar,stats}.json
./sync_docs.sh             # 내보내기 + public/ → docs/ 복사
./deploy_pages.sh          # 위 둘 + 커밋 + push + 라이브 200 확인 (평소엔 이것만 실행)
```

- **라이브 URL**: https://kylesung0901-ops.github.io/ClareDEX/ (공개)
  - 회원가입: https://kylesung0901-ops.github.io/ClareDEX/signup.html
  - 운영자 페이지: https://kylesung0901-ops.github.io/ClareDEX/admin/ (정적 프리뷰에서는 초안을 비공개로 두고 안내만 표시)
- 저장소: `kylesung0901-ops/ClareDEX` (public)
  - 사이트 서빙: **`gh-pages` 브랜치 루트** — GitHub Pages 소스. push 하면 자동 재빌드.
  - 소스 보관: **`claredex-v2-preview` 브랜치** — 이 프로젝트 전체 + `docs/` 사본.
  - `ClareDEX` 의 `main` 브랜치(기존 클레어덱스 사이트)는 건드리지 않습니다.
- 화면은 API(`/api/...`)를 먼저 호출하고, 정적 호스팅에서 404/비-JSON 응답이 오면 `data/*.json` 스냅샷으로 자동 대체합니다(`index.html`).
- **운영자 초안은 정적 호스팅에 내보내지 않습니다.** `/api/naver-drafts` 는 로그인 없이는 401 이고, `export_static.py` 는 `naver-drafts.json` 을 만들지 않으며(이전 배포본 파일도 삭제), `/admin` 은 서버가 없으면 “정적 프리뷰에서는 보안을 위해 내부 초안 데이터를 비공개로 둡니다. 실제 서버(claredex.com/admin)에서 로그인 후 이용하세요.” 안내를 표시합니다.
- 모든 자산·링크 경로는 저장소 하위 경로(`/ClareDEX/`)에서도 동작하도록 상대 경로입니다(`assets/style.css`, `images/posts/N.png`, `signup.html`).
- `/signup.html` 은 API 가 없으면 가입 정보를 브라우저 `localStorage` 에만 저장하고 **"정적 미리보기 모드"** 안내를 표시합니다(서버 전송 없음).
- 티커는 마지막 수집 스냅샷이므로 `지연 표시` 로 표기됩니다.
- 블로그 초안의 **미리보기**는 본문 이미지를 상대 경로로 보여주고, **복사용 HTML** 은 네이버 발행 기준(운영 도메인)을 그대로 유지합니다.

## 남은 작업(배포·운영 단계)

1. `.env` 에 실제 발송 자격증명 등록 — 이메일(SMTP_HOST·SMTP_PORT·SMTP_USER·SMTP_PASS·SMTP_FROM 또는 RESEND_API_KEY), 문자(SMS_PROVIDER·SMS_API_KEY·SMS_API_SECRET·SMS_SENDER). 없으면 인증번호는 테스트 모드(화면 표시)로 동작합니다.
2. 네이버 로그인 세션(`data/naver_state.json`) 1회 준비 → 이후 자동 발행.
3. 카카오·구글 OAuth 키 발급 후 소셜 로그인 실연동(현재 버튼은 안내만).
4. 텔레그램 봇 토큰으로 채널 자동 전송.
5. OpenDART·ECOS 키 등록으로 국내 공시·기준금리 확장.
