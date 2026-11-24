# ClareDEX v2 — 웹 · 수집엔진 · 네이버 블로그 초안기

클레어덱스(웨일파트너스)의 1단계 승인 와이어프레임을 구현한 프로토타입입니다.
**공식 원문만 수집 → 사실만 요약 → 웹 발행 → 네이버 블로그 SEO 초안 생성 → 복사 발행**까지의 파이프라인을 담고 있습니다.

```
collector.py      공식 소스 수집 → SQLite 저장 → 브랜드 썸네일 PNG 생성 → 시장지표 캐시
naver_drafts.py   글마다 네이버 SEO 초안(제목·키워드·본문 HTML·CTA) 생성
server.py         정적 파일 + JSON API 서버 (표준 라이브러리만 사용)
verify.py         DB 무결성 + 실제 HTTP 40개 항목 자체 검증
public/           메인 · 회원가입 · 블로그 초안 도구
data/             claredex.db, market_cache.json, calendar.json, verify_report.json
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

- **posts** — id, title, category, source_name, source_grade, source_url, `source_hash UNIQUE`, verified_at, summary_lines(JSON, 3줄), market_impact, tags(JSON), image_path, is_member_early, created_at, **published_at**(원문 발표 시각, 추가), **topic**(초안 생성용 주제 키, 추가)
- **users** — id, email UNIQUE, name, provider, watchlist(JSON), created_at
- **naver_drafts** — id, post_id UNIQUE, seo_title, keywords(JSON 5~8), body_html, body_text, telegram_cta, web_cta, image_path, created_at

`published_at`·`topic` 은 brief 의 필수 컬럼에 더해 사실 보존과 초안 품질을 위해 추가한 컬럼입니다.

## HTTP API

| Method | Path | 설명 |
|---|---|---|
| GET | `/` | 메인 (티커·히어로·오늘의 한 장·카운트다운·속보 타임라인·컴플라이언스 푸터) |
| GET | `/signup.html`, `/signup` | 10초 가입 + 관심자산 온보딩 (기존 404 경로 복구) |
| GET | `/admin-blog.html`, `/admin-blog` | 네이버 초안 미리보기·1클릭 복사 |
| GET | `/api/ticker` | BTC KRW/USD, 김치프리미엄, 공포탐욕, 롱숏, USD/KRW, ETH. 실패 시 `data/market_cache.json` 폴백(`stale: true`) |
| GET | `/api/posts?category=&tag=&q=&limit=` | 속보 목록 |
| GET | `/api/naver-drafts` | 초안 목록 (본문 HTML 포함) |
| GET | `/api/calendar` | KST 공식 일정 |
| POST | `/api/signup` | `{email, name, provider, watchlist}` → users 저장 (201) |
| POST | `/api/watchlist` | `{email, watchlist}` → 관심자산 갱신 |

## 화면

- **메인** — 다크 지표 티커(항목별 출처 표기) → 히어로(승인 문구 + `내 관심자산 속보 무료로 받기 →` + 텔레그램) → 2열 핵심 존(오늘의 한 장 / KST 카운트다운) → 실시간 속보 타임라인(카테고리·관심자산 필터, 검색, `N분 전 확인`, 출처 등급 배지, 3줄 요약, 시장 영향 한 줄, 자산 태그, `공식 원문 보기 ↗`) → 사이드바(관심자산·텔레그램·수집 소스) → 컴플라이언스 푸터.
- **회원가입** — 카카오/구글 버튼(정식 오픈 전 "준비 중" 안내) + 동작하는 이메일 가입 → 관심자산 6종 선택(BTC·ETH·나스닥·코스피·삼성전자·NVDA) → 완료 화면 + 텔레그램 CTA. 선택값은 `localStorage`에도 저장되어 메인의 `★ 내 관심자산` 필터에 바로 반영됩니다.
- **블로그 초안 도구** — 초안 목록, 제목/서식 유지 본문/HTML/태그/이미지 링크 복사, 미리보기·HTML·텍스트 탭.

## 공식 일정(카운트다운) 데이터

`data/calendar.json` 에 기관 공식 일정 페이지에서 확인한 3건을 넣었습니다.

1. 미 3분기 GDP 2차 추정치 — 2026-11-25 22:30 KST (`bea.gov/news/schedule`, 08:30 ET)
2. FOMC 결과 발표(12/8~9 회의) — 2026-12-10 04:00 KST (`federalreserve.gov/monetarypolicy/fomccalendars.htm`, 14:00 ET)
3. 미 11월 CPI — 2026-12-10 22:30 KST (`bls.gov/schedule/news_release/cpi.htm`, 08:30 ET)

## 네이버 블로그 자동화 범위

네이버는 2020년 글쓰기 API를 종료했고 자동 발행은 약관·저품질 리스크가 있어, **초안 생성 + 1클릭 복사**까지만 자동화했습니다. 초안에는
- SEO 제목(50자 이내)과 키워드 5~8개,
- 3줄 요약 + 시장 영향 해석 + 원문 출처/확인 시각,
- `https://t.me/Clare_PerpDEX` 로 연결되는 클릭 이미지,
- 주제별 텔레그램·웹 CTA(최근 10개 초안과 `difflib.SequenceMatcher` 로 비교해 가장 덜 겹치는 조합 선택),
- 유사투자자문업 컴플라이언스 고지
가 들어갑니다.

> 초안 본문 이미지 URL은 `CLAREDEX_PUBLIC_BASE_URL`(기본 `https://www.claredex.com`) 기준입니다. `public/images/posts/*.png` 를 실제 도메인에 배포해야 네이버에서 이미지가 보입니다.

## 검증 결과 (요약)

`python3 verify.py` → **41/41 통과**
- 3개 테이블·필수 컬럼, `sha256(source_url)` 일치·중복 없음, 3줄 요약·태그·KST · 금지 표현 없음, 9개 썸네일 1200×630 PNG
- 글 1건당 초안 1건, 키워드 5~8, 텔레그램/웹 링크·고지·이미지 링크 포함, CTA 최대 유사도 0.40
- HTTP: `/`, `/signup.html`, `/signup`, `/admin-blog.html`, `/assets/style.css`, `/images/posts/1.png`, `/api/ticker`, `/api/posts`(+category·q), `/api/naver-drafts`, `/api/calendar` 200
- POST `/api/signup` 201 → `/api/watchlist` 200 → SQLite 저장 확인, 같은 이메일 재가입 시 관심자산 보존, 잘못된 이메일 400, 미지원 자산 4xx, 없는 경로 404, 상위 경로 403, 테스트 서버 정상 종료

## GitHub Pages 정적 프리뷰

서버 없이 화면만 그대로 확인할 수 있도록 정적 스냅샷 배포를 넣었습니다.

```bash
python3 export_static.py   # data/*.json + DB → public/data/{posts,ticker,calendar,naver-drafts}.json
./sync_docs.sh             # 내보내기 + public/ → docs/ 복사
./deploy_pages.sh          # 위 둘 + 커밋 + push + 라이브 200 확인 (평소엔 이것만 실행)
```

- **라이브 URL**: https://kylesung0901-ops.github.io/ClareDEX/ (공개)
  - 회원가입: https://kylesung0901-ops.github.io/ClareDEX/signup.html
  - 블로그 초안 도구: https://kylesung0901-ops.github.io/ClareDEX/admin-blog.html
- 저장소: `kylesung0901-ops/ClareDEX` (public)
  - 사이트 서빙: **`gh-pages` 브랜치 루트** — GitHub Pages 소스. push 하면 자동 재빌드.
  - 소스 보관: **`claredex-v2-preview` 브랜치** — 이 프로젝트 전체 + `docs/` 사본.
  - `ClareDEX` 의 `main` 브랜치(기존 클레어덱스 사이트)는 건드리지 않습니다.
- 화면은 API(`/api/...`)를 먼저 호출하고, 정적 호스팅에서 404/비-JSON 응답이 오면 `data/*.json` 스냅샷으로 자동 대체합니다(`index.html`, `admin-blog.html`).
- 모든 자산·링크 경로는 저장소 하위 경로(`/ClareDEX/`)에서도 동작하도록 상대 경로입니다(`assets/style.css`, `images/posts/N.png`, `signup.html`).
- `/signup.html` 은 API 가 없으면 가입 정보를 브라우저 `localStorage` 에만 저장하고 **"정적 미리보기 모드"** 안내를 표시합니다(서버 전송 없음).
- 티커는 마지막 수집 스냅샷이므로 `지연 표시` 로 표기됩니다.
- 블로그 초안의 **미리보기**는 본문 이미지를 상대 경로로 보여주고, **복사용 HTML** 은 네이버 발행 기준(운영 도메인)을 그대로 유지합니다.

## 남은 작업(배포·운영 단계)

1. `public/` 를 실제 호스팅(claredex.com)에 배포하고, 수집기를 cron/스케줄러로 10~30분 주기 실행.
2. 카카오·구글 OAuth 키 발급 후 소셜 로그인 실연동(현재 버튼은 안내만).
3. 텔레그램 봇 토큰으로 채널 자동 전송, 이메일 알림 발송.
4. OpenDART·ECOS 키 등록으로 국내 공시·기준금리 확장.
5. 회원 로그인 세션·관심자산 알림 저장(현재 프로토타입은 이메일 기반 식별).
