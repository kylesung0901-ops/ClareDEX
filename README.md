# ClareDEX v2 — 웹 · 수집엔진 · 휴대폰 인증 가입

클레어덱스(웨일파트너스)의 v2 프로토타입입니다.
**공식 원문만 수집 → 중요도(S/A/B) 분류 → 사실만 요약 → 웹 발행 → 휴대폰 인증 가입 → 네이버 블로그 SEO 초안**까지의 파이프라인을 담고 있습니다.

```
collector.py      공식 소스 수집 → SQLite 저장 → 중요도 분류 → 썸네일 PNG → 시장지표 캐시 → 공식 일정 스냅샷
content.py        원문 제목·공시 항목 기반 요약 3줄 · 시장 영향 문구 · 중요도 규칙
naver_drafts.py   글마다 네이버 SEO 초안(제목·키워드·본문 HTML·CTA) 생성
server.py         정적 파일 + JSON API 서버 (표준 라이브러리만 사용)
verify.py         DB 무결성 + 실제 HTTP 자체 검증
public/           메인 · 휴대폰 인증 가입 · 운영자(/admin) 블로그 초안 도구
calendar.json     기관 공식 페이지에서 확인한 발표 일정(커밋 대상)
data/             claredex.db, market_cache.json, verify_report.json (git 제외)
```

## 실행 방법

```bash
cd claredex-v2

python3 collector.py          # 1) 공식 원문 수집 (+ 중요도 분류·썸네일·시장지표·일정 스냅샷)
python3 naver_drafts.py       # 2) 네이버 블로그 SEO 초안 생성
python3 server.py --port 8000 # 3) 서버 실행 → http://localhost:8000
python3 verify.py             # 4) 자체 검증 (임시 포트로 서버를 띄웠다가 종료)
```

- 필수 패키지: Python 3.12 표준 라이브러리 + `Pillow`(썸네일). `lxml`/`matplotlib`은 쓰지 않았습니다.
- 한글 폰트: `/usr/share/fonts/pickle-hwp/NotoSansCJKkr-Regular.otf` 를 사용하며, 다른 환경에서는 `thumbnail.py` 의 `FONT_CANDIDATES` 에 경로를 추가하면 됩니다.
- `.env`(선택): `.env.example` 을 복사해 키를 넣습니다. 없으면 해당 수집·발송만 건너뛰고 나머지는 그대로 동작합니다.

### 휴대폰 인증 · SMS 개발/데모 모드

`/signup` 은 **휴대폰 번호 → 6자리 인증번호 → 관심자산 선택** 순서로 진행합니다.

- `SMS_WEBHOOK_URL` 이 없으면 **개발/데모 모드**입니다. 실제 문자가 나가지 않는 대신, `POST /api/phone/send-code` 응답과 화면에 `demo: true` 와 **테스트용 인증번호(`dev_code`)** 를 표시합니다. 그 번호로 `POST /api/phone/verify-code` 를 호출하면 그대로 가입 테스트가 됩니다.
- `SMS_WEBHOOK_URL`(필요 시 `SMS_API_KEY`, `SMS_SENDER`)을 설정하면 `{"to","text","sender"}` JSON 을 그 주소로 POST 하는 **실발송 모드**로 바뀌고, 응답에는 인증번호가 더 이상 포함되지 않습니다. 실제 문자 대행사(알리고·네이버 클라우드·Twilio 등)의 규격에 맞춘 프록시 URL 을 연결하세요.
- 인증번호는 5분 유효, 5회 입력 제한, 같은 번호로 10분에 3회까지 발송합니다. 저장은 평문이 아니라 번호별 salt + SHA-256 해시입니다.
- 인증 완료 토큰은 30분 유효·1회용이며, 가입(`POST /api/signup`)에서 소모됩니다.

### 운영자 화면(`/admin`) 비밀번호

블로그 초안 도구는 **운영자 전용**이라 `/admin` 로그인 게이트 뒤에 있습니다.

- 비밀번호는 `ADMIN_PASSWORD` 환경변수(또는 `.env`)로 지정합니다. `.env.example` 에 `ADMIN_PASSWORD=claredex-admin-2026` 이 들어 있습니다.
- 값이 없으면 **개발용 기본값 `claredex-admin-2026`** 으로 동작하고 기동 시 경고를 남깁니다. 실제 배포(`claredex.com`)에서는 반드시 길고 무작위인 값으로 교체하세요.
- 세션은 서버 메모리에만 두는 무작위 토큰이며 `HttpOnly; SameSite=Lax` 쿠키(12시간)로 전달됩니다. 서버를 재시작하면 모든 세션이 끊깁니다.
- 같은 IP에서 5분에 10회 이상 틀리면 잠시 `429` 로 차단합니다.

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
- 중요도 S/A/B 는 **발표 유형**(FOMC·CPI·고용·8-K 2.02 실적 등)과 원문 주소·공시 항목으로 정한 클레어덱스의 분류 값입니다. 시장 방향 예측이 아닙니다.

### 중요도 등급

| 등급 | 표시 | 기준 예시 |
|---|---|---|
| **S** | 빨간 배지 | FOMC 성명·의사록, 8-K 항목 2.02(빅테크 실적), 공식 일정의 CPI·고용보고서 |
| **A** | 금색 배지 | 10-Q·10-K 정기보고서, 8-K 주요 항목(계약·경영진·통제권 변경 등), 자본·스트레스테스트 규제, DART 잠정실적·유상증자 |
| **B** | 회색 테두리 | 모기지 면제 기준 금액 같은 지엽적 행정 보도자료, 개별 기관 제재·조사, 일반 공시 |

- `content.fed_importance`, `content.sec_importance`, `content.dart_importance` 가 규칙을 갖고 있고, `content.classify_post_importance` 가 이미 저장된 글을 다시 분류합니다.
- 컬럼이 없던 시절에 쌓인 글은 `collector.py` 실행 시 자동으로 등급이 채워집니다.
- 예전 버전이 만든 "연준 공식 보도자료"·천편일률적 안내 문구도 `collector.py` 가 저장된 원문 제목을 근거로 다시 씁니다(새 숫자를 만들지 않음).

## DB 스키마 (`data/claredex.db`)

- **posts** — id, title, category, source_name, source_grade, source_url, `source_hash UNIQUE`, verified_at, summary_lines(JSON, 3줄), market_impact, tags(JSON), image_path, is_member_early, created_at, published_at, topic, **importance**(S/A/B)
- **users** — id, `email UNIQUE`(NULL 허용), name, provider, watchlist(JSON), **phone**, **phone_verified**, **phone_verified_at**, **plan**(`free`/`pro`), **alerts_enabled**, created_at
- **phone_verifications** — id, phone, code_hash, salt, purpose, attempts, token_hash, verified_at, consumed_at, expires_at, created_at
- **naver_drafts** — id, post_id UNIQUE, seo_title, keywords(JSON 5~8), body_html, body_text, telegram_cta, web_cta, image_path, created_at

`init_db()` 는 기존 DB 를 그대로 살리면서 마이그레이션합니다. `users.email` 의 NOT NULL 은 휴대폰만으로 가입할 수 있도록 테이블 재작성으로 풀고, `posts.importance`·`users.phone/phone_verified/plan/alerts_enabled` 는 `ALTER TABLE` 로 추가합니다. `phone` 에는 NULL 을 제외한 UNIQUE 인덱스가 걸립니다.

## HTTP API

| Method | Path | 설명 |
|---|---|---|
| GET | `/` | 메인 (단일 컬럼: 티커 한 줄 · 히어로 · 오늘의 한 장 · 주요 속보 캐러셀 · 최신 속보 · 공식 일정 · 무료/Pro 안내) |
| GET | `/signup.html`, `/signup` | 휴대폰 인증 가입 + 관심자산 온보딩 |
| GET | `/admin`, `/admin/` | 운영자 전용 네이버 초안 도구 (미로그인 시 “운영자 로그인” 화면, `noindex`) |
| GET | `/api/ticker` | BTC KRW/USD, 김치프리미엄, 공포탐욕, 롱숏, USD/KRW, ETH. 실패 시 `data/market_cache.json` 폴백(`stale: true`) |
| GET | `/api/posts?category=&tag=&q=&limit=` | 속보 목록 (`importance`, `importance_label` 포함) |
| GET | `/api/stats` | KST 기준 오늘 확인 건수·최근 확인일·전체 건수·등급별 건수 |
| GET | `/api/calendar` | KST 공식 일정 (빈 값은 서버에서 제거) |
| GET | `/api/plans` | 무료/Pro 플랜 설명과 SMS 모드(`demo`/`live`) |
| GET | `/api/naver-drafts` | 초안 목록 (본문 HTML 포함) — **운영자 로그인 필요, 미로그인 401** |
| GET | `/api/admin/session` | 현재 운영자 세션 상태 `{authenticated}` |
| POST | `/api/phone/send-code` | `{phone}` → 6자리 인증번호 발송. 데모 모드면 `dev_code` 포함 |
| POST | `/api/phone/verify-code` | `{phone, code}` → `verification_token`(30분·1회용) |
| POST | `/api/signup` | `{phone, verification_token, name?, watchlist}` 또는 `{email, name?, provider, watchlist}` → users 저장 (201) |
| POST | `/api/watchlist` | `{email, watchlist}` → 관심자산 갱신 (이메일 가입 회원용) |
| POST | `/api/admin/login` | `{password}` → 200 + `Set-Cookie: claredex_admin=…` (틀리면 401) |
| POST | `/api/admin/logout` | 세션 폐기 + 쿠키 만료 (`Max-Age=0`) |

## 화면

- **메인** — TUPL 스타일 단일 컬럼(720px, 오프화이트 `#FAF8F4`). 상단 헤더(로고·실시간 속보·공식 일정·Pro 안내·`휴대폰 인증`/`무료 가입`) → 얇은 지표 티커 한 줄 → 히어로(`OFFICIAL BREAKING` → 한 줄 헤드라인 → 골드 구분선 → `오늘의 시장, 공식 원문 3초 결론`) → **다크 네이비 `오늘의 한 장` 카드 1장**(라벨·제목·2~3줄 요약·골드 CTA 1개 → `/signup`) → **주요 속보 캐러셀**(흰 카드 1장씩 + 점 인디케이터, 7초 자동 전환) → **최신 속보**(`오늘 확인 N건`, **관심자산 1초 미리보기 칩**, 전체·S 긴급·미국주식·국내주식·코인·매크로 칩, 카드마다 카테고리·등급 라벨 / 제목 / 요약+시장 영향 한 줄 / `출처·확인 시각(KST)`+`공식 원문 ↗` 4요소) → **공식 일정**(다음 일정 D-day 카운트다운 + 체크포인트 1줄 + 1줄 행 목록) → **가입 안내**(무료 vs Pro 2열 카드) → 컴플라이언스 푸터. 사이드바·2단 대시보드·썸네일·해시태그 나열은 두지 않습니다.
- **관심자산 1초 미리보기 (전환 퍼널)** — 메인 최신 속보 위 `관심자산` 줄에서 `BTC · NVDA · AAPL · 연준·금리` 를 로그인 없이 한 번 누르면, 그 자산의 공식 원문만 피드에 남습니다(글 `tags` 로 매칭). 선택하면 피드 바로 위에 한 줄 전환 바(`선택하신 NVDA 속보, 발표 즉시 휴대폰으로 받기 →`)가 뜨고 `signup.html?watch=NVDA` 로 이어집니다. `index.html?watch=NVDA` 로 들어와도 같은 상태로 열립니다.
- **가입 화면의 `watch` 파라미터** — `/signup.html?watch=NVDA` 는 관심자산 단계에서 NVDA 를 미리 체크하고, `?watch=FOMC`(공식 일정의 `발표 알림 받기`)는 `FOMC 회의 결과` 알림 요청으로 읽어 `연준·금리` 속보와 함께 설정합니다(`config.EVENT_WATCH_ALIASES` = `public/signup.html` 의 `EVENT_ALIAS`).
- **오늘 확인 건수** — `오늘 확인 N건` 은 **KST 자정 기준 오늘 `verified_at` 건수**만 셉니다. 오늘 0건이면 `오늘 확인 0건 · 최근 확인 N건 (M/D)` 로 최근 확인일을 함께 표시합니다.
- **회원가입** — 같은 오프화이트 단일 컬럼(중앙 정렬 헤드 + 540px 폼 카드 + 혜택 목록). 휴대폰 번호 → 인증번호 6자리(5분 타이머) → 관심자산 8종 선택(BTC·ETH·NVDA·AAPL·나스닥·코스피·삼성전자·연준·금리) → 완료. `?watch=` 로 들어오면 해당 자산을 미리 체크하고 맨 위에 1줄 안내를 띄웁니다. 이메일만으로 가입하는 경로도 남아 있습니다. SMS 키가 없으면 화면에 `개발/데모 모드` 와 테스트 인증번호를 표시합니다. 정적 프리뷰에서는 `정적 미리보기 모드` 로 브라우저에만 저장됩니다.
- **운영자 페이지(/admin)** — 비밀번호 로그인 게이트 → 초안 목록, 제목/서식 유지 본문/HTML/태그/이미지 링크 복사, 미리보기·HTML·텍스트 탭, 로그아웃. 정적 프리뷰에서는 초안 대신 비공개 안내를 표시합니다.

## 공식 일정(카운트다운) 데이터

`calendar.json` 에 기관 공식 페이지에서 **직접 확인한 일정만** 커밋합니다. 화면과 `/api/calendar` 는 필수 값(`id·title·org·datetime_kst·local_time·source_url`)이 비어 있는 일정을 버리므로 `—` 가 노출되지 않습니다.

| 일정 | KST | 중요도 | 원문 |
|---|---|---|---|
| 미 11월 고용보고서 | 2026-12-04 22:30 | S | `bls.gov/schedule/news_release/empsit.htm` |
| 미 10월 국제무역수지 | 2026-12-08 22:30 | B | `bea.gov/news/schedule` |
| FOMC 결과 발표(12/8~9, SEP) | 2026-12-10 04:00 | S | `federalreserve.gov/monetarypolicy/fomccalendars.htm` |
| 미 11월 CPI | 2026-12-10 22:30 | S | `bls.gov/schedule/news_release/cpi.htm` |
| 미 3분기 GDP 3차 추정치·기업이익 | 2026-12-23 22:30 | A | `bea.gov/news/schedule` |
| 미 11월 개인소득·지출(PCE) | 2026-12-23 22:30 | A | `bea.gov/news/schedule` |
| FOMC 결과 발표(2027년 1/26~27) | 2027-01-28 04:00 | A | `federalreserve.gov/monetarypolicy/fomccalendars.htm` |

> 발표 시각은 해당 기관 일정 페이지의 ET 표기를 KST 로 환산한 값입니다. FOMC 성명은 통상 회의 둘째 날 14:00 ET 로, 원문 일정 페이지의 회의 날짜와 함께 표기했습니다.

각 일정에는 **발표 전 체크포인트 1줄**(`checkpoint`)과 **알림 신청 키**(`watch`)가 함께 들어 있습니다. 체크포인트는 매수·매도 권유가 아닌 관전 포인트이며, `verify.py` 가 빈 값·80자 초과·금지 표현을 검사합니다. 화면에서는 일정 행 아래 둘째 줄에 체크포인트와 `이 일정 발표 알림 받기 →`(`signup.html?watch=FOMC` 등)가 붙습니다.

## 무료 회원 · Pro 플랜

- **무료 회원(휴대폰 인증)**: 내 관심자산 실시간 알림(최대 3개), 3초 요약 전문, S등급 긴급 속보 알림, 이번 주 공식 일정.
- **Pro(출시 준비 중)**: 무지연 속보, 공시 즉시 분석(SEC 8-K·10-Q · DART), 무제한 관심자산, 개인 텔레그램 봇·카카오 알림톡 연동.
- 가격과 출시 일정은 아직 확정하지 않았습니다. 화면과 API 어디에도 추측 가격을 넣지 않습니다. `config.PLANS` 가 단일 출처이고 `/api/plans` 로 노출됩니다.
- 결제 연동은 아직 없습니다. `users.plan` 은 `free`/`pro` 값만 갖고, Pro 권한 게이트는 다음 단계 작업입니다.

## 네이버 블로그 자동화 범위

네이버는 2020년 글쓰기 API를 종료했고 자동 발행은 약관·저품질 리스크가 있어, **초안 생성 + 1클릭 복사**까지만 자동화했습니다. 초안에는
- SEO 제목(50자 이내)과 키워드 5~8개,
- 3줄 요약 + 시장 영향 해석 + 원문 출처/확인 시각,
- `https://t.me/Clare_PerpDEX` 로 연결되는 클릭 이미지,
- 주제별 텔레그램·웹 CTA(최근 10개 초안과 `difflib.SequenceMatcher` 로 비교해 가장 덜 겹치는 조합 선택),
- 유사투자자문업 컴플라이언스 고지
가 들어갑니다.

> 초안 본문 이미지 URL은 `CLAREDEX_PUBLIC_BASE_URL`(기본 `https://www.claredex.com`) 기준입니다. `public/images/posts/*.png` 를 실제 도메인에 배포해야 네이버에서 이미지가 보입니다.
> 수집기가 기존 글의 제목·요약을 갱신한 뒤에는 `python3 naver_drafts.py --rebuild` 로 초안을 다시 만들 수 있습니다.

## 검증 결과 (요약)

`python3 verify.py` → **113/113 통과**
- 3+1개 테이블·필수 컬럼(`importance`, `phone`, `phone_verified`, `plan`, `alerts_enabled`), `sha256(source_url)` 일치·중복 없음, 3줄 요약·태그·KST · 금지 표현 없음, 중요도 값 S/A/B·분류 규칙 7종, 오늘(KST) 건수 ≤ 전체 · 0건일 때 최근 확인일 · 다음 날 기준 재계산, 1200×630 PNG
- 글 1건당 초안 1건, 키워드 5~8, 텔레그램/웹 링크·고지·이미지 링크 포함, CTA 최대 유사도 0.47
- HTTP: `/`, `/signup.html`(휴대폰 인증 마크업), `/signup`, `/admin`·`/admin/`(로그인 게이트 200·`no-store`), `/assets/style.css`, `/images/posts/1.png`, `/api/ticker`, `/api/posts`(+category·q·중요도), `/api/stats`, `/api/plans`, `/api/calendar`(3건 이상·빈 값 없음·KST·정렬)
- 긴급 속보 `바로 가입하기` 버튼: `signup.html` 링크 + `aria-disabled` 없음 / 중요도 배지·일정 목록·무료/Pro 안내 마크업
- 차별화 기능: 관심자산 미리보기 칩 4종·1줄 전환 바(`signup.html?watch=`), 일정 체크포인트·발표 알림 링크, 카드 메타 `출처 + KST 확인 시각` 1줄, 가입 화면 `watch` 사전 선택, 일정 알림 키 → 관심자산 매핑, `watchlist` 에 `AAPL`·`연준·금리` 저장
- 운영자 인증: 미로그인 401 → 틀린 비밀번호 401 → 로그인 200·`HttpOnly; SameSite=Lax` 쿠키 → 초안 목록 200 → 로그아웃 200 → 다시 401
- 휴대폰 인증: 데모 인증번호 발급 → 잘못된 번호 400 → 틀린 인증번호 400 → 인증 200·토큰 → 토큰 없는 가입 400 → 가입 201(`phone_verified`, `plan:free`) → 토큰 재사용 401 → SQLite 저장 확인
- 정적 프리뷰: `export_static.py` 가 `naver-drafts.json` 을 만들지 않음, `stats.json`·`calendar.json`·`posts.json(importance)` 생성, 메인·회원가입에 `admin-blog` 링크 없음, `public/admin-blog.html` 삭제 확인
- POST `/api/signup`(이메일) 201 → `/api/watchlist` 200 → SQLite 저장, 같은 이메일 재가입 시 관심자산 보존, 잘못된 이메일 400, 미지원 자산 4xx, 없는 경로 404, 상위 경로 403, 테스트 서버 정상 종료

## GitHub Pages 정적 프리뷰

서버 없이 화면만 그대로 확인할 수 있도록 정적 스냅샷 배포를 넣었습니다.

```bash
python3 export_static.py   # DB + calendar.json → public/data/{posts,ticker,calendar,stats}.json
./sync_docs.sh             # 내보내기 + public/ → docs/ 복사
./deploy_pages.sh          # 위 둘 + 현재 브랜치 커밋·push + gh-pages 미러 + 라이브 확인
./deploy_pages.sh --no-push  # 소스 push 없이 gh-pages 프리뷰만 갱신
```

- `deploy_pages.sh` 는 **지금 체크아웃된 브랜치만** push 하고, 사이트는 `gh-pages` 미러로만 반영합니다. PR 베이스(`claredex-v2-preview`)나 `main` 으로 강제 push 하지 않습니다.

- **라이브 URL**: https://kylesung0901-ops.github.io/ClareDEX/ (공개)
  - 회원가입: https://kylesung0901-ops.github.io/ClareDEX/signup.html
  - 운영자 페이지: https://kylesung0901-ops.github.io/ClareDEX/admin/ (정적 프리뷰에서는 초안을 비공개로 두고 안내만 표시)
- 저장소: `kylesung0901-ops/ClareDEX` (public)
  - 사이트 서빙: **`gh-pages` 브랜치 루트** — GitHub Pages 소스. push 하면 자동 재빌드.
  - 소스 보관: **`claredex-v2-preview` 브랜치** — PR #1 의 베이스. 이 배포 스크립트는 여기에 push 하지 않습니다.
  - `ClareDEX` 의 `main` 브랜치(기존 클레어덱스 사이트)는 건드리지 않습니다.
- 화면은 API(`/api/...`)를 먼저 호출하고, 정적 호스팅에서 404/비-JSON 응답이 오면 `data/*.json` 스냅샷으로 자동 대체합니다(`index.html`).
- **운영자 초안은 정적 호스팅에 내보내지 않습니다.** `/api/naver-drafts` 는 로그인 없이는 401 이고, `export_static.py` 는 `naver-drafts.json` 을 만들지 않으며(이전 배포본 파일도 삭제), `/admin` 은 서버가 없으면 “정적 프리뷰에서는 보안을 위해 내부 초안 데이터를 비공개로 둡니다. 실제 서버(claredex.com/admin)에서 로그인 후 이용하세요.” 안내를 표시합니다.
- 모든 자산·링크 경로는 저장소 하위 경로(`/ClareDEX/`)에서도 동작하도록 상대 경로입니다(`assets/style.css`, `images/posts/N.png`, `signup.html`).
- `/signup.html` 은 API 가 없으면 `정적 미리보기 모드` 로 전환해 가입 정보를 브라우저 `localStorage` 에만 저장하고, 인증번호도 화면에 표시되는 데모 번호를 사용합니다.
- 티커는 마지막 수집 스냅샷이므로 `지연 표시` 로 표기됩니다.
- 블로그 초안의 **미리보기**는 본문 이미지를 상대 경로로 보여주고, **복사용 HTML** 은 네이버 발행 기준(운영 도메인)을 그대로 유지합니다.

## 남은 작업(배포·운영 단계)

1. `public/` 를 실제 호스팅(claredex.com)에 배포하고, 수집기를 cron/스케줄러로 10~30분 주기 실행.
2. 실제 SMS 대행사 규격에 맞춘 `SMS_WEBHOOK_URL` 프록시 연결(현재는 데모 모드로도 전체 흐름 테스트 가능).
3. Pro 결제(빌링) 연동과 `plan` 기반 권한 게이트, 관심자산 3개 제한·무제한 정책 적용.
4. 카카오·구글 OAuth 키 발급 후 소셜 로그인 실연동(현재 화면에는 없음).
5. 텔레그램 봇 토큰으로 채널 자동 전송, 알림톡/이메일 발송.
6. OpenDART·ECOS 키 등록으로 국내 공시·기준금리 확장.
