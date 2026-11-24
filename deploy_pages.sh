#!/usr/bin/env bash
# 클레어덱스 v2 → GitHub Pages 배포 (한 번에 실행)
#
#   ./deploy_pages.sh                      # 기본: kylesung0901-ops/claredex-v2
#   ./deploy_pages.sh owner/other-repo     # 다른 저장소
#
# 하는 일: docs 동기화 → 커밋 → push → Pages 활성화(main /docs) → 라이브 URL 200 확인
set -euo pipefail
cd "$(dirname "$0")"

REPO="${1:-kylesung0901-ops/claredex-v2}"
BRANCH="main"
SITE="https://${REPO%%/*}.github.io/${REPO##*/}/"

echo "▶ 대상 저장소: $REPO"

echo "1) docs/ 동기화"
./sync_docs.sh

echo "2) 변경 커밋"
git add -A
if git diff --cached --quiet; then
  echo "   변경 없음"
else
  git commit -q -m "docs: GitHub Pages 정적 프리뷰 갱신"
fi

echo "3) 원격 연결"
if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "https://github.com/${REPO}.git"
else
  git remote add origin "https://github.com/${REPO}.git"
fi

echo "4) 원격 이력 확인 (README 등 기존 커밋이 있으면 그 위에 얹음)"
if git fetch -q origin "$BRANCH" 2>/dev/null && git rev-parse --verify -q "origin/${BRANCH}" >/dev/null; then
  if ! git merge-base --is-ancestor "origin/${BRANCH}" HEAD 2>/dev/null; then
    echo "   원격에 기존 커밋 있음 → rebase (충돌 시 이 저장소 파일 우선)"
    git rebase -q -s recursive -X ours "origin/${BRANCH}" || { git rebase --abort || true; echo "   rebase 실패: 수동 확인 필요"; exit 1; }
  fi
else
  echo "   빈 저장소 또는 원격 브랜치 없음"
fi

echo "5) push"
git push -u origin "$BRANCH"

echo "6) Pages 활성화 시도"
if gh api "repos/${REPO}/pages" >/dev/null 2>&1; then
  echo "   이미 활성화됨"
  gh api -X PUT "repos/${REPO}/pages" -f 'source[branch]=main' -f 'source[path]=/docs' >/dev/null 2>&1 || true
else
  if gh api -X POST "repos/${REPO}/pages" -f 'source[branch]=main' -f 'source[path]=/docs' >/dev/null 2>&1; then
    echo "   활성화 요청 성공 (main /docs)"
  else
    echo "   ⚠ API 로 활성화하지 못했습니다(권한). 저장소 Settings → Pages 에서"
    echo "     Source: Deploy from a branch · Branch: main · Folder: /docs 를 선택해 주세요."
  fi
fi

echo "7) 라이브 URL 확인: $SITE"
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$SITE" || true)
  if [ "$code" = "200" ]; then
    echo "   ✅ HTTP 200 — $SITE"
    for page in signup.html admin-blog.html data/posts.json assets/style.css; do
      pcode=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "${SITE}${page}" || true)
      echo "   ${pcode} ${SITE}${page}"
    done
    exit 0
  fi
  printf "   대기 %s/30 (HTTP %s)\n" "$i" "$code"
  sleep 10
done

echo "   ⚠ 아직 200 이 아닙니다. GitHub Pages 첫 빌드는 1~2분 걸릴 수 있습니다."
echo "     다시 확인: ./deploy_pages.sh   또는  curl -I $SITE"
exit 1
