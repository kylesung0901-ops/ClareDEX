#!/usr/bin/env bash
# 클레어덱스 v2 → GitHub Pages 라이브 반영 (한 번에 실행)
#
#   ./deploy_pages.sh
#
# 현재 라이브 구조 (2026-11-24 확인):
#   · https://kylesung0901-ops.github.io/ClareDEX/  ← GitHub Pages 가 서빙하는 사이트
#   · Pages 소스 브랜치 = gh-pages (루트). gh-pages 를 push 하면 자동으로 Pages 가 켜지고 다시 빌드된다.
#   · claredex-v2-preview 브랜치 = 소스+docs/ 보관용 (기록). 사이트 서빙에는 쓰이지 않는다.
#   · ClareDEX 의 main 브랜치는 건드리지 않는다.
#
# 하는 일: docs 동기화 → 커밋 → claredex-v2-preview push → gh-pages 미러 push → 라이브 URL 200 확인
set -euo pipefail
cd "$(dirname "$0")"

REPO="kylesung0901-ops/ClareDEX"
SRC_BRANCH="claredex-v2-preview"   # 소스 보관 브랜치 (docs/ 폴더 포함)
PAGES_BRANCH="gh-pages"            # Pages 가 실제로 서빙하는 브랜치 (사이트 루트)
SITE="https://kylesung0901-ops.github.io/ClareDEX/"
GIT_ID=(-c user.name=kylesung0901-ops -c user.email=229571632+kylesung0901-ops@users.noreply.github.com)

echo "1) docs/ 동기화 (정적 JSON + public 복사)"
./sync_docs.sh

echo "2) 소스 브랜치($SRC_BRANCH) 커밋·push"
git add -A
if git diff --cached --quiet; then
  echo "   변경 없음"
else
  git "${GIT_ID[@]}" commit -q -m "docs: GitHub Pages 프리뷰 갱신"
fi
git push -q origin "HEAD:refs/heads/${SRC_BRANCH}"
echo "   push 완료: $(git rev-parse --short HEAD) → ${SRC_BRANCH}"

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
    echo "   ✅ HTTP 200"
    for page in signup.html admin-blog.html data/posts.json assets/style.css; do
      echo "   $(curl -s -o /dev/null -m 10 -w '%{http_code}' "${SITE}${page}") ${SITE}${page}"
    done
    exit 0
  fi
  printf "   대기 %s/30 (HTTP %s)\n" "$i" "$code"
  sleep 10
done
echo "   ⚠ 아직 200 이 아닙니다. Pages 빌드는 1~2분 걸릴 수 있습니다: $SITE"
exit 1
