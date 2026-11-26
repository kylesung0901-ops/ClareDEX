#!/usr/bin/env bash
# 클레어덱스 v2 → GitHub Pages 라이브 반영 (한 번에 실행)
#
#   ./deploy_pages.sh              # 현재 브랜치 push + docs 동기화 + gh-pages 미러
#   ./deploy_pages.sh --no-push    # 소스 push 생략하고 gh-pages 프리뷰만 갱신
#
# 현재 라이브 구조 (2026-11-26 확인):
#   · https://kylesung0901-ops.github.io/ClareDEX/  ← GitHub Pages 가 서빙하는 사이트
#   · Pages 소스 브랜치 = gh-pages (루트). gh-pages 를 push 하면 자동으로 다시 빌드된다.
#   · claredex-v2-preview = PR #1 의 베이스 브랜치. 이 스크립트는 여기에 밀어 넣지 않는다.
#
# 안전 규칙:
#   · push 하는 대상은 지금 체크아웃된 브랜치 하나뿐이다 (HEAD). 다른 브랜치로 강제 push 하지 않는다.
#   · 사이트 반영은 gh-pages 미러로만 한다. main · PR 베이스 브랜치는 건드리지 않는다.
set -euo pipefail
cd "$(dirname "$0")"

REPO="kylesung0901-ops/ClareDEX"
PAGES_BRANCH="gh-pages"            # Pages 가 실제로 서빙하는 브랜치 (사이트 루트)
SITE="https://kylesung0901-ops.github.io/ClareDEX/"
PUSH_SOURCE=1
GIT_ID=(-c user.name=kylesung0901-ops -c user.email=229571632+kylesung0901-ops@users.noreply.github.com)

for arg in "$@"; do
  case "$arg" in
    --no-push) PUSH_SOURCE=0 ;;
    *) echo "알 수 없는 옵션: $arg" >&2; exit 2 ;;
  esac
done

echo "1) docs/ 동기화 (정적 JSON + public 복사)"
./sync_docs.sh

if [ "$PUSH_SOURCE" = "1" ]; then
  BRANCH="$(git rev-parse --abbrev-ref HEAD)"
  if [ "$BRANCH" = "HEAD" ]; then
    echo "   ⚠ detached HEAD — 소스 push 생략" >&2
  else
    echo "2) 소스 브랜치($BRANCH) 커밋·push"
    git add -A
    if git diff --cached --quiet; then
      echo "   변경 없음"
    else
      git "${GIT_ID[@]}" commit -q -m "docs: GitHub Pages 프리뷰 갱신"
    fi
    git push -q origin "HEAD:refs/heads/${BRANCH}"
    echo "   push 완료: $(git rev-parse --short HEAD) → ${BRANCH}"
  fi
else
  echo "2) 소스 push 생략 (--no-push)"
fi

echo "3) 사이트 브랜치($PAGES_BRANCH) 미러"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
git clone -q --depth 1 --branch "$PAGES_BRANCH" "https://github.com/${REPO}.git" "$TMP"
# 미러 대상 폴더를 비우고 docs 내용으로 교체 (.git 은 유지)
find "$TMP" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
cp -R docs/. "$TMP"/
rm -f "$TMP/.gitignore"   # '.gitignore 의 /data/ 규칙'이 data/*.json 을 빼먹지 않도록
( cd "$TMP" && git add -A && { git diff --cached --quiet || git "${GIT_ID[@]}" commit -q -m "gh-pages: 클레어덱스 v2 정적 프리뷰"; } && git push -q origin "$PAGES_BRANCH" )
echo "   미러 push 완료"

echo "4) 라이브 확인: $SITE"
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$SITE" || true)
  if [ "$code" = "200" ]; then
    # 새 빌드가 반영됐는지까지 확인한다: 차별화 기능 마크업이 라이브에 있어야 한다.
    has_watch=$(curl -s -m 10 "$SITE" | grep -c 'id="watchChips"' || true)
    admin=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "${SITE}admin/" || true)
    drafts=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "${SITE}data/naver-drafts.json" || true)
    legacy=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "${SITE}admin-blog.html" || true)
    if [ "$has_watch" -ge 1 ] && [ "$admin" = "200" ] && [ "$drafts" = "404" ] && [ "$legacy" = "404" ]; then
      echo "   ✅ HTTP 200 · 차별화 기능 마크업 반영 · /admin/ 200 · 운영자 데이터 404"
      for page in signup.html "signup.html?watch=FOMC" admin/ data/posts.json data/calendar.json assets/style.css; do
        echo "   $(curl -s -o /dev/null -m 10 -w '%{http_code}' "${SITE}${page}") ${SITE}${page}"
      done
      # 운영자 전용 데이터는 공개 정적 호스팅에 없어야 한다 (404 기대)
      for page in data/naver-drafts.json admin-blog.html; do
        echo "   $(curl -s -o /dev/null -m 10 -w '%{http_code}' "${SITE}${page}") ${SITE}${page}  (기대: 404)"
      done
      exit 0
    fi
    printf "   Pages 재빌드 대기 %s/30 (watchChips=%s admin=%s drafts=%s admin-blog=%s)\n" "$i" "$has_watch" "$admin" "$drafts" "$legacy"
  else
    printf "   대기 %s/30 (HTTP %s)\n" "$i" "$code"
  fi
  sleep 10
done
echo "   ⚠ 새 빌드가 아직 반영되지 않았습니다. Pages 빌드 상태를 확인하세요: https://github.com/${REPO}/deployments"
exit 1
