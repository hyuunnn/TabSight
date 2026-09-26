#!/bin/zsh
set -e
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
if curl -fsS http://127.0.0.1:8787/api/health >/dev/null 2>&1; then
  open http://127.0.0.1:8787
  exit 0
fi
for tabsight_dependency in uv node npm ffmpeg; do
  if ! command -v "$tabsight_dependency" >/dev/null; then
    echo "필수 프로그램이 없습니다: $tabsight_dependency. README.md의 설치 안내를 확인해 주세요."
    read -r "?Enter 키를 누르면 닫힙니다."
    exit 1
  fi
done
uv sync --frozen
if [[ ! -d node_modules/@coderline/alphatab-vite ]]; then npm ci; fi
npm run build
(
  for tabsight_attempt in {1..40}; do
    if curl -fsS http://127.0.0.1:8787/api/health >/dev/null 2>&1; then
      open http://127.0.0.1:8787
      break
    fi
    sleep .5
  done
) &
echo "TabSight가 실행됩니다. 종료하려면 이 창에서 Control-C를 누르세요."
exec .venv/bin/uvicorn server.app:app --host 127.0.0.1 --port 8787
