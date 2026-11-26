#!/usr/bin/env bash
# ClareDEX v2 정기 자동화 (호스트 cron 이 KST 07:10 · 19:10 에 호출)
#  1) collector.py   → 1차 공식 소스 수집 + 새 글 1건 발행  (하루 2회 × 1건 = 2건/일)
#  2) naver_drafts.py → 새 글의 네이버 SEO 초안 생성
#  3) naver_publish.py → 초안 1건을 blog.naver.com/cryptobrothers 로 발행
#  ※ 네이버 로그인 세션(data/naver_state.json)이 없으면 3)은 발행하지 않고 상태만 기록한다.
#  ※ 실행 결과는 data/collector_state.json 에 기록되어 /admin 수집기 탭에 그대로 보인다.
set -u
LOG_TAG="[$(date -u '+%Y-%m-%dT%H:%M:%SZ')]"
MAX_NEW="${CLAREDEX_MAX_NEW_PER_RUN:-1}"
NAVER_LIMIT="${CLAREDEX_NAVER_PER_RUN:-1}"
APP_DIR="/root/claredex-v2"
STATE_FILE="$APP_DIR/data/collector_state.json"

if ! docker inspect -f '{{.State.Running}}' claredex-v2 2>/dev/null | grep -q true; then
  echo "$LOG_TAG SKIP: claredex-v2 컨테이너가 실행 중이 아닙니다."
  exit 0
fi

TMP="$(mktemp -d)"
STARTED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

echo "$LOG_TAG START collect (max_new=$MAX_NEW)"
docker exec claredex-v2 python -c "import json, os, collector; print('__RESULT__' + json.dumps(collector.run(fed_limit=5, sec_limit=2, max_new=int(os.environ.get('CLAREDEX_MAX_NEW_PER_RUN', '$MAX_NEW'))), ensure_ascii=False, default=str))" > "$TMP/1.out" 2>&1
rc1=$?
echo "$LOG_TAG collector exit=$rc1"
docker exec claredex-v2 python -c "import json, naver_drafts; print('__RESULT__' + json.dumps(naver_drafts.run(), ensure_ascii=False, default=str))" > "$TMP/2.out" 2>&1
rc2=$?
echo "$LOG_TAG naver_drafts exit=$rc2"
docker exec claredex-v2 python naver_publish.py --limit "$NAVER_LIMIT" > "$TMP/3.out" 2>&1
rc3=$?
echo "$LOG_TAG naver_publish exit=$rc3"

# 실행 로그를 그대로 남긴다 (__RESULT__ 줄은 사람이 읽기 쉽게 걸러서)
for f in "$TMP"/1.out "$TMP"/2.out "$TMP"/3.out; do
  grep -v '^__RESULT__' "$f" 2>/dev/null | sed 's/^/[step] /'
done

python3 - "$TMP" "$rc1" "$rc2" "$rc3" "$MAX_NEW" "$NAVER_LIMIT" "$STARTED_AT" "$STATE_FILE" <<'PY'
import json, sys, time
from datetime import datetime, timezone, timedelta

tmp, rc1, rc2, rc3, max_new, naver_limit, started_at, state_file = sys.argv[1:9]
KST = timezone(timedelta(hours=9))

names = [("collector.py", f"{tmp}/1.out", int(rc1)),
         ("naver_drafts.py", f"{tmp}/2.out", int(rc2)),
         ("naver_publish.py", f"{tmp}/3.out", int(rc3))]
steps, log_lines, results = [], [], {}
for name, path, rc in names:
    parsed = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("__RESULT__"):
                    try:
                        parsed = json.loads(line[len("__RESULT__"):])
                    except Exception:
                        parsed = None
                elif line.strip():
                    log_lines.append(f"[{name}] {line.rstrip()}")
    except FileNotFoundError:
        pass
    steps.append({"name": name, "ok": rc == 0, "exit_code": rc,
                  "result": parsed})
    if parsed:
        results[name] = parsed

collector_result = results.get("collector.py") or {}
drafts = results.get("naver_drafts.py") or {}
naver = results.get("naver_publish.py") or None
state = {
    "last_run_at": datetime.now(tz=KST).replace(microsecond=0).isoformat(),
    "started_at": started_at,
    "trigger": "cron",
    "ok": all(rc == 0 for _, _, rc in names),
    "duration_s": None,
    "steps": steps,
    "source_status": collector_result.get("source_status") or {},
    "new_posts": collector_result.get("new"),
    "total_posts": collector_result.get("total"),
    "deferred_posts": collector_result.get("deferred"),
    "drafts_created": drafts.get("created"),
    "drafts_total": drafts.get("total"),
    "naver": naver,
    "max_new": max_new,
    "naver_limit": naver_limit,
    "log": log_lines[-80:],
}
with open(state_file, "w", encoding="utf-8") as fh:
    json.dump(state, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
print(f"[state] {state_file} 기록 · 신규 {state['new_posts']} · 네이버 {((naver or {}).get('status'))}")
PY

rm -rf "$TMP"
exit $(( rc1 != 0 || rc2 != 0 || rc3 != 0 ))
