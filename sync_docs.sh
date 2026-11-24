#!/usr/bin/env bash
# GitHub Pages 배포용: public/ → docs/ 동기화 (Pages 소스: main 브랜치 /docs)
set -euo pipefail
cd "$(dirname "$0")"

echo "1) 정적 JSON 스냅샷 내보내기"
python3 export_static.py

echo "2) public/ → docs/ 복사"
rm -rf docs
mkdir -p docs
cp -R public/. docs/

echo "3) 완료: docs/ ($(find docs -type f | wc -l | tr -d ' ')개 파일)"
