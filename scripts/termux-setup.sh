#!/usr/bin/env bash
# 안드로이드 Termux에서 처음 한 번 실행하는 설치 스크립트.
#   bash scripts/termux-setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "[1/4] 시스템 패키지 설치"
# update만 하면 일부 라이브러리만 새 버전이 되어 ffmpeg 등이 "CANNOT LINK EXECUTABLE"로 깨집니다.
# 전부 최신으로 올려야 합니다 (설정 파일 질문은 새 버전으로 자동 선택).
pkg upgrade -y -o Dpkg::Options::="--force-confnew"
# rust·clang·binutils: 안드로이드용 미리 빌드된 패키지가 없는 pydantic-core 등을 직접 빌드하는 데 필요
# python-cryptography: 유튜브 연결 토큰 암호화에 쓰며, pip로 빌드하면 오래 걸려 미리 빌드된 패키지를 씁니다
pkg install -y python ffmpeg git nano python-pillow python-cryptography rust clang binutils

echo "[2/4] 파이썬 패키지 설치 (처음에는 빌드 때문에 10~30분 걸릴 수 있어요)"
# maturin(pydantic-core 빌드 도구)이 안드로이드 API 수준을 요구합니다.
export ANDROID_API_LEVEL="$(getprop ro.build.version.sdk)"
# aiohttp 계열은 C 확장 대신 순수 파이썬 버전으로 설치해 빌드 실패를 피합니다.
export AIOHTTP_NO_EXTENSIONS=1 MULTIDICT_NO_EXTENSIONS=1 YARL_NO_EXTENSIONS=1 \
       FROZENLIST_NO_EXTENSIONS=1 PROPCACHE_NO_EXTENSIONS=1
# 여러 빌드를 동시에 돌리면 안드로이드에서 "Text file busy (os error 26)"로 실패하므로 하나씩 빌드합니다.
export CARGO_BUILD_JOBS=1
# 그래도 가끔 같은 오류가 나서, 실패하면 한 번 더 시도합니다 (이미 빌드된 것은 건너뜀).
pip install -r requirements-termux.txt || pip install -r requirements-termux.txt

echo "[3/4] 설정 파일 준비"
if [ ! -f .env ]; then
  cp .env.example .env
  # 휴대폰 발열을 줄이기 위해 씬을 동시에 2개까지만 만듭니다.
  echo "MAX_CONCURRENT_SCENES=2" >> .env
fi

echo "[4/4] 확인"
python - <<'PY'
from app.services.video import ensure_ffmpeg, find_korean_font
ensure_ffmpeg()
print("ffmpeg: OK")
print("자막 폰트:", find_korean_font())
PY

echo
echo "설치 완료! 다음 순서로 진행하세요:"
echo "  1) nano .env  → GEMINI_API_KEY= 뒤에 키 입력 → Ctrl+O, Enter(저장) → Ctrl+X(종료)"
echo "  2) bash scripts/termux-run.sh"
