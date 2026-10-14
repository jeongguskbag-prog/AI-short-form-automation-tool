#!/usr/bin/env bash
# 안드로이드 Termux에서 서버를 켜고, 켜지면 브라우저로 웹 화면을 엽니다.
#   bash scripts/termux-run.sh      (끄려면 Ctrl+C)
set -uo pipefail
cd "$(dirname "$0")/.."

# 설치(termux-setup.sh)가 끝나지 않았으면 켜도 바로 꺼지므로 먼저 알려 줍니다.
if ! python -c "import fastapi, uvicorn, pydantic_settings, edge_tts" 2>/dev/null; then
  echo
  echo "설치가 아직 끝나지 않았어요. 아래를 실행하고 '설치 완료!'가 나올 때까지 기다린 뒤 다시 켜 주세요 (10~30분):"
  echo "  bash scripts/termux-setup.sh"
  echo
  exit 1
fi

# 화면이 꺼져도 영상 생성이 멈추지 않도록 Termux 절전 해제
termux-wake-lock 2>/dev/null || true
trap 'termux-wake-unlock 2>/dev/null || true' EXIT

# 서버가 실제로 응답하면 그때 브라우저를 엽니다 (최대 90초).
(
  for _ in $(seq 1 90); do
    if python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)" 2>/dev/null; then
      termux-open-url http://localhost:8000 2>/dev/null
      exit 0
    fi
    sleep 1
  done
) &

python main.py
status=$?
if [ $status -ne 0 ]; then
  echo
  echo "서버가 오류로 꺼졌어요 (코드 $status). 위의 빨간 오류 문구를 캡처해서 알려 주세요."
  echo "이미 켜져 있다는 오류(address already in use)면 'AI쇼츠 끄기'를 누른 뒤 다시 켜 주세요."
fi
exit $status
