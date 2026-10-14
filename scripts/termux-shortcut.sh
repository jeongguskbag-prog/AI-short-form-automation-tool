#!/usr/bin/env bash
# 안드로이드 홈 화면에 'AI 쇼츠' 아이콘을 만듭니다 (Termux:Widget 앱 필요).
#   bash scripts/termux-shortcut.sh
# 만든 뒤 홈 화면 위젯에서 'Termux:Widget'을 추가하면 아이콘 목록이 나옵니다.
set -euo pipefail
cd "$(dirname "$0")/.."
APP_DIR="$(pwd)"
# 켜기·끄기에서 실행 중인지 확인할 때 씁니다 (pgrep·pkill).
command -v pgrep >/dev/null || pkg install -y procps
mkdir -p "$HOME/.shortcuts"
chmod 700 "$HOME/.shortcuts"

# 켜기: 최신으로 받고(인터넷이 안 되면 그냥 진행) 서버를 켠 뒤 브라우저를 엽니다.
cat > "$HOME/.shortcuts/AI쇼츠 켜기" <<SCRIPT
#!/data/data/com.termux/files/usr/bin/bash
cd "$APP_DIR"
if pgrep -f "python main.py" >/dev/null; then
  echo "이미 켜져 있어요. 브라우저로 엽니다."
  termux-open-url http://localhost:8000
  exit 0
fi
echo "최신 버전 확인 중…"
git pull --ff-only || echo "(업데이트를 건너뛰고 지금 버전으로 켭니다)"
bash scripts/termux-run.sh || { echo; echo "Enter를 누르면 이 창이 닫혀요."; read -r _; }
SCRIPT

# 끄기: 켜 둔 서버를 끕니다.
cat > "$HOME/.shortcuts/AI쇼츠 끄기" <<SCRIPT
#!/data/data/com.termux/files/usr/bin/bash
pkill -f "python main.py" && echo "껐어요." || echo "켜져 있지 않아요."
termux-wake-unlock 2>/dev/null || true
sleep 1
SCRIPT

chmod 700 "$HOME/.shortcuts/AI쇼츠 켜기" "$HOME/.shortcuts/AI쇼츠 끄기"
echo
echo "아이콘을 만들었습니다: AI쇼츠 켜기 / AI쇼츠 끄기"
echo "홈 화면을 길게 눌러 → 위젯 → Termux:Widget을 끌어다 놓으면 아이콘이 보여요."
echo "(Termux:Widget 앱이 없으면 Termux를 받은 곳(GitHub·F-Droid)에서 같은 곳의 Termux:Widget을 설치하세요.)"
