# ClareDEX v2 — 호스팅어 VPS 운영 런북 (72.62.72.12 / srv1748563)

> 배포일: 2026-11-24 (KST) · 담당: Pickle (클레어덱스님 작업 대행)

## 1. 무엇이 떠 있나

| 항목 | 값 |
|---|---|
| 서버 | `root@72.62.72.12` (`srv1748563.hstgr.cloud`), Ubuntu 24.04 |
| 앱 경로 | `/root/claredex-v2` (compose 프로젝트명 `claredex-v2`) |
| 컨테이너 | `claredex-v2` (python:3.12-slim, 내부 8000포트, 외부 포트 미개방) |
| 리버스 프록시 | 기존 Traefik(`traefik-beug-traefik-1`)의 `proxy` 네트워크에 합류 |
| 검증용 주소 | https://claredex-v2.72.62.72.12.sslip.io (Let's Encrypt 정상 발급) |
| 운영 도메인 | `claredex.com`, `www.claredex.com` 라우터 등록 완료 (DNS 전환 대기) |
| DB | `/root/claredex-v2/data/claredex.db` (호스트 볼륨, 컨테이너 재생성에도 유지) |
| 썸네일 | `/root/claredex-v2/public/images/posts/*.png` (호스트 볼륨) |
| 운영자 비밀번호 | `/root/claredex-v2/.env` 의 `ADMIN_PASSWORD` (권한 600) |

## 2. 자주 쓰는 명령

```bash
cd /root/claredex-v2
docker compose ps
docker compose logs -f --tail=100
curl -s localhost:8000/api/health            # 컨테이너 내부 헬스 (호스트에서는 docker exec 사용)
docker exec claredex-v2 python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/api/health').read().decode())"
```

코드 갱신(로컬에서 수정 후):

```bash
# 로컬 PC(=Pickle 작업공간)에서
tar -czf /tmp/claredex-v2.tgz -C claredex-v2-deploy .
# VPS 로 업로드 후
cd /root/claredex-v2 && tar -xzf ~/claredex-v2.tgz && docker compose build && docker compose up -d
```

### 2-1. git 으로 갱신 (브랜치 `live-prod-snapshot`)

VPS 의 `/root/claredex-v2` 가 git 저장소라면 아래 순서로 반영합니다.
`data/`(SQLite·시장 캐시·네이버 세션)와 `.env` 는 .gitignore 대상이라 pull 로 덮어써지지 않습니다.

```bash
cd /root/claredex-v2
git remote -v                       # origin = GitHub 클레어덱스 저장소인지 확인
git stash list                      # 로컬 수정이 있으면 먼저 정리
git fetch origin
git checkout live-prod-snapshot     # 현재 다른 브랜치에 있으면 전환
git pull --ff-only origin live-prod-snapshot
git log --oneline -3

docker compose build                # 코드·public·calendar.json 은 이미지에 포함 → 재빌드 필요
docker compose up -d
docker compose ps
docker exec claredex-v2 python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/api/health').read().decode())"
docker exec claredex-v2 python verify.py     # 자체 검증 (아래 기대값 참고)
```

git 저장소가 아니라 tar 배포본이라면, 같은 브랜치를 새 경로에 clone 하고 운영 데이터만 복사해 올리는 방식이 안전합니다
(컨테이너 이름은 그대로라 기존 컨테이너를 대체합니다. 기존 디렉터리는 백업으로 남겨 둡니다).

```bash
cd /root
git clone --branch live-prod-snapshot --depth 1 <저장소 URL> claredex-v2-git
cd /root/claredex-v2-git
rm -rf data public/images/posts
cp -a /root/claredex-v2/data ./data
mkdir -p public/images/posts && cp -a /root/claredex-v2/public/images/posts/. public/images/posts/
cp /root/claredex-v2/.env ./.env
chmod 600 .env

docker compose build && docker compose up -d
docker exec claredex-v2 python verify.py
```

이후 갱신은 `/root/claredex-v2-git` 에서 `git pull --ff-only origin live-prod-snapshot` → `docker compose build && docker compose up -d` 로 하면 됩니다.
기존 `/root/claredex-v2` 디렉터리는 문제가 없으면 나중에 `claredex-v2-backup-<날짜>` 로 이름을 바꿔 보관합니다.

> `calendar.json` 은 저장소 루트에 있고 Dockerfile 의 `COPY calendar.json` 으로 이미지에 들어갑니다.
> 예전 배포본이 쓰던 `data/calendar.json` 은 읽기 폴백으로만 남겨 두었습니다(볼륨 보존).
> 일정만 고칠 때도 `docker compose build` 가 필요합니다.

## 3. 정기 작업 (호스트 cron, KST)

| 시각 | 파일 | 내용 |
|---|---|---|
| **매 10~15분** | `/etc/cron.d/claredex-v2-collect` → `/usr/local/bin/claredex-collect.sh` | `collector.py --max-new 5` — 공식 RSS(연준·한국은행·BEA·노동부·SEC)·SEC 공시·시장 급변 속보를 그때그때 발행. 네이버 단계는 `CLAREDEX_NAVER_ENABLED=0` 으로 건너뜀 |
| 07:10 / 19:10 | 같은 스크립트 (네이버 단계 포함) | `collector.py --max-new 5` → `naver_drafts.py`(초안) → `naver_publish.py --limit 1`(네이버 발행 1건) |
| 03:30 | `/etc/cron.d/claredex-v2-backup` → `/usr/local/bin/claredex-backup.sh` | SQLite 백업 → `/root/claredex-v2/backups/` (14일 보관, gzip) |

`/etc/cron.d/claredex-v2-collect` 예시(서버 시간이 KST 일 때):

```cron
# 15분 주기 — 새 공시·속보를 밀리지 않고 바로 올린다(네이버 발행은 하지 않음)
*/15 * * * * root CLAREDEX_MAX_NEW_PER_RUN=5 CLAREDEX_NAVER_ENABLED=0 /usr/local/bin/claredex-collect.sh >> /var/log/claredex-v2-collect.log 2>&1
# 하루 2회 — 네이버 초안 생성 + 발행 1건까지
10 7,19 * * * root CLAREDEX_MAX_NEW_PER_RUN=5 /usr/local/bin/claredex-collect.sh >> /var/log/claredex-v2-collect.log 2>&1
```

- 변수는 `collect.sh` 가 읽는 환경변수다. `CLAREDEX_MAX_NEW_PER_RUN`(기본 5) · `CLAREDEX_NAVER_PER_RUN`(기본 1) · `CLAREDEX_NAVER_ENABLED`(기본 1, 0이면 네이버 단계 생략).
- 10분 주기로 돌리고 싶으면 `*/10` 으로 바꾸면 된다. 실행이 겹치면 `source_hash` 중복 검사로 같은 글은 한 번만 들어간다.
- 유닛을 바꾼 뒤에는 `crontab`/`cron.d` 를 다시 읽히고(`systemctl reload cron`), 한 번 수동 실행해 `/admin` 수집기 탭의 `source_status` 에 `rss_bok`·`rss_bea`·`rss_dol`·`rss_sec_press`·`shock` 이 찍히는지 본다.

로그: `/var/log/claredex-v2-collect.log` (logrotate 주간 8개 보관)

> `collect.sh` 를 고쳤을 때는 cron 이 부르는 호스트 람퍼도 함께 맞춘다.
> `/usr/local/bin/claredex-collect.sh` 가 **사본**이면 `cp /root/claredex-v2/collect.sh /usr/local/bin/claredex-collect.sh && chmod 755 /usr/local/bin/claredex-collect.sh`,
> **심볼릭 링크**면 그대로 둔다(`ls -l /usr/local/bin/claredex-collect.sh` 로 확인).
> 사본·링크가 아니고 `collect.sh` 를 직접 호출하는 cron 이라면 손릴 것이 없다.
> 바꾼 뒤 `systemctl reload cron` → 수동 1회 실행으로 `/admin` 수집기 탭에 `rss_*`·`shock` 항목이 보이는지 확인.

### 3-1. 네이버 블로그 자동 발행 (blog.naver.com/cryptobrothers)

- 발행 스크립트: 컨테이너 안 `naver_publish.py` (Playwright + Chromium, 이미지에 포함)
- 로그인 세션: `/root/claredex-v2/data/naver_state.json` (Playwright storage state)
- 세션이 없으면 발행을 시도하지 않고 `status="no_session"` 으로 `data/naver_publish_state.json` 과 `/admin` 수집기 탭에 기록합니다.

세션 만들기(로컬 PC, 화면이 있는 곳):

```bash
pip install playwright && python -m playwright install chromium
python naver_publish.py --login       # 창에서 네이버 로그인 → Enter
scp data/naver_state.json root@72.62.72.12:/root/claredex-v2/data/
```

확인:

```bash
docker exec claredex-v2 python naver_publish.py --check
docker exec claredex-v2 python naver_publish.py --dry-run --limit 2
```

## 4. DNS 전환 (가비아)

가비아 DNS 관리에서 아래처럼 바꾸면 이 서버로 트래픽이 온다.

- `@` A 레코드: `216.198.79.1`(Vercel) → **`72.62.72.12`**
- `www` CNAME(또는 A): `www.claredex.com`(Vercel) → **`claredex.com`** (또는 A `72.62.72.12`)

전환 후 Traefik 이 Let's Encrypt 인증서를 자동 발급한다(HTTP-01, 보통 1~2분).
전환 전에는 Traefik 로그에 `Unable to obtain ACME certificate ... invalid authorization` 이 반복되는데, **DNS 가 Vercel 을 가리켜서 생기는 정상적인 상태**이고 서비스에는 영향이 없다. 전환 후 자동으로 사라진다.

전환 직후 확인:

```bash
curl -sI https://claredex.com/ | head -1          # 200
curl -s  https://claredex.com/api/health          # {"ok": true, ...}
curl -sI https://www.claredex.com/ | head -1
```

## 4-1. 본인인증 발송 채널 (이메일·문자)

`.env` 에 자격증명이 없으면 가입 화면은 **테스트 모드**로 동작합니다(실제 발송 없음, 인증번호를 화면에 표시).
실서비스 전환은 `/root/claredex-v2/.env` 에 아래를 채우고 `docker compose up -d` 로 재시작합니다.

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASS=앱 비밀번호
SMTP_FROM=you@gmail.com
# 또는 RESEND_API_KEY=...

SMS_PROVIDER=solapi        # solapi | coolsms | aligo
SMS_API_KEY=...
SMS_API_SECRET=...
SMS_SENDER=01012345678
```

상태 확인: `curl -s https://www.claredex.com/api/auth/channels`

## 5. 되돌리기 / 장애 대응

- 컨테이너만 재시작: `docker compose restart`
- 이전 이미지로: `docker compose down` 후 `image: claredex-v2:<이전태그>` 로 바꿔 `up -d`
- DB 복구: `gunzip -c backups/claredex-<stamp>.db.gz > data/claredex.db && docker compose restart`
- 코드 롤백: `/root/claredex-v2-backup-<stamp>.tgz` 를 풀고 `docker compose build && docker compose up -d`
- 자체 검증: `docker exec claredex-v2 python verify.py` (2026-11-26 기준 171/171)
- **주의**: 이 서버에는 `Cryptomining`(peterpan3.com), `kylesung-schedule-ui`, `claredex-live/claredex-beta/claredex-web`,
  `ett-naver-publisher` 등 다른 서비스가 함께 돈다. `docker compose down -v`, `docker system prune -a`,
  `proxy` 네트워크 삭제는 절대 금지. `claredex-v2` 디렉터리 밖은 건드리지 않는다.
- `preview.claredex.com` 은 기존 `claredex-private-preview-express-1`(Basic Auth)이 계속 서비스한다. 변경 없음.
- `claredex-web` 컨테이너에 `claredex.com` 라우터 라벨이 남아 있지만 `traefik.enable=false` 라 비활성 상태다.
  나중에 그 컨테이너를 켜면 라우터가 충돌하므로, 켤 일이 있으면 그 라벨을 먼저 지운다.
