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

## 3. 정기 작업 (호스트 cron, KST)

| 시각 | 파일 | 내용 |
|---|---|---|
| 07:10 / 19:10 | `/etc/cron.d/claredex-v2-collect` → `/usr/local/bin/claredex-collect.sh` | `collector.py --max-new 1`(새 글 1건 발행) → `naver_drafts.py`(초안) → `naver_publish.py --limit 1`(네이버 발행) → **하루 2건** |
| 03:30 | `/etc/cron.d/claredex-v2-backup` → `/usr/local/bin/claredex-backup.sh` | SQLite 백업 → `/root/claredex-v2/backups/` (14일 보관, gzip) |

로그: `/var/log/claredex-v2-collect.log` (logrotate 주간 8개 보관)

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
- 자체 검증: `docker exec claredex-v2 python verify.py` (2026-11-24 기준 140/140)
- **주의**: 이 서버에는 `Cryptomining`(peterpan3.com), `kylesung-schedule-ui`, `claredex-live/claredex-beta/claredex-web`,
  `ett-naver-publisher` 등 다른 서비스가 함께 돈다. `docker compose down -v`, `docker system prune -a`,
  `proxy` 네트워크 삭제는 절대 금지. `claredex-v2` 디렉터리 밖은 건드리지 않는다.
- `preview.claredex.com` 은 기존 `claredex-private-preview-express-1`(Basic Auth)이 계속 서비스한다. 변경 없음.
- `claredex-web` 컨테이너에 `claredex.com` 라우터 라벨이 남아 있지만 `traefik.enable=false` 라 비활성 상태다.
  나중에 그 컨테이너를 켜면 라우터가 충돌하므로, 켤 일이 있으면 그 라벨을 먼저 지운다.
