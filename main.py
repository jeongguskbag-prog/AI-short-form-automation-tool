"""로컬 실행 진입점.

    python main.py            # 일반 실행 (이 PC에서만 접속)
    python main.py --phone    # 같은 와이파이의 폰에서도 접속 (접속 키 필요)
    python main.py --reload   # 코드 수정 시 자동 재시작 (개발용)
"""

import sys
from pathlib import Path

import uvicorn

if __name__ == "__main__":
    host, port = "127.0.0.1", 8000
    if "--phone" in sys.argv:
        from app.lan import enable_phone_mode, phone_banner

        key, ip = enable_phone_mode(Path(".env"))
        host = "0.0.0.0"
        print(phone_banner(key, ip, port))
    uvicorn.run("app.main:app", host=host, port=port, reload="--reload" in sys.argv)
