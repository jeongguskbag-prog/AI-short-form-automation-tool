"""PC용 실행 파일(EXE) 진입점.

더블클릭하면 서버를 켜고 브라우저로 화면을 엽니다. 콘솔 창을 닫으면 서버도 꺼집니다.

- 실행 파일이 있는 폴더를 작업 폴더로 씁니다 (.env, data/, output/이 여기에 생깁니다).
- 같은 폴더의 ffmpeg/ 안에 있는 ffmpeg.exe·ffprobe.exe를 먼저 씁니다.
- 처음 실행하면 .env를 만들고, Gemini 키가 없으면 설정 화면을 열어 키를 넣게 합니다.
- 설정 화면에서 키를 저장하면 서버를 다시 켜서 바로 적용합니다.

개발 환경에서도 `python launcher.py`로 똑같이 실행할 수 있습니다.
"""

import os
import secrets
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "8000"))
PLACEHOLDER_KEYS = {"", "your_gemini_api_key_here"}


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundle_dir() -> Path:
    """PyInstaller가 함께 묶은 파일(.env.example 등)이 있는 폴더."""
    return Path(getattr(sys, "_MEIPASS", base_dir()))


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


def create_env(path: Path) -> None:
    template = bundle_dir() / ".env.example"
    text = template.read_text(encoding="utf-8") if template.is_file() else "GEMINI_API_KEY=\n"
    # 링크 서명 키를 고정해 두면 다시 켜도 보관함·영상 링크가 그대로 동작합니다.
    text = text.replace("URL_SIGNING_SECRET=\n", f"URL_SIGNING_SECRET={secrets.token_urlsafe(32)}\n", 1)
    path.write_text(text, encoding="utf-8")


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((HOST, port)) == 0


def open_browser_when_ready(url: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://{HOST}:{PORT}/health", timeout=2):
                break
        except OSError:
            time.sleep(0.5)
    webbrowser.open(url)


def fix_console_encoding() -> None:
    """출력이 파일·파이프로 갈 때 Windows 기본 인코딩(cp1252 등)이면 한글을 찍다 멈추므로 UTF-8로 바꿉니다."""
    for stream in (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def main() -> int:
    root = base_dir()
    os.chdir(root)
    ffmpeg_dir = root / "ffmpeg"
    if ffmpeg_dir.is_dir():
        os.environ["PATH"] = str(ffmpeg_dir) + os.pathsep + os.environ.get("PATH", "")

    smoke = "--smoke-test" in sys.argv  # 빌드 확인용: 서버를 잠깐 켜고 /health만 확인한 뒤 끝냅니다.
    env_path = root / ".env"
    if not env_path.exists():
        create_env(env_path)
        print(f"설정 파일을 만들었습니다: {env_path}")
    url = f"http://{HOST}:{PORT}/"
    # Gemini 키가 없으면 설정 화면부터 열어 키를 넣게 합니다.
    start_url = url if read_env(env_path).get("GEMINI_API_KEY", "") not in PLACEHOLDER_KEYS else url + "setup"
    if port_in_use(PORT):
        print(f"이미 실행 중입니다. 브라우저로 엽니다: {url}")
        if not smoke:
            webbrowser.open(url)
        return 0

    import uvicorn

    from app.config import get_settings
    from app.main import create_app

    if smoke:
        app = create_app()
        config = uvicorn.Config(app, host=HOST, port=PORT, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            for _ in range(60):
                try:
                    with urllib.request.urlopen(f"http://{HOST}:{PORT}/health", timeout=2) as r:
                        ok = r.status == 200
                    with urllib.request.urlopen(url, timeout=5) as r:
                        ok = ok and "쇼츠" in r.read().decode("utf-8")
                    print("smoke test:", "OK" if ok else "FAILED")
                    return 0 if ok else 1
                except OSError:
                    time.sleep(1)
            print("smoke test: server did not start")
            return 1
        finally:
            server.should_exit = True
            thread.join(timeout=10)

    bind_host = HOST
    if read_env(env_path).get("PHONE_ACCESS", "").lower() in ("1", "true", "yes", "on"):
        from app.lan import enable_phone_mode, phone_banner

        key, ip = enable_phone_mode(env_path)
        bind_host = "0.0.0.0"  # 같은 와이파이의 폰에서도 접속
        print(phone_banner(key, ip, PORT))

    print()
    print(f" AI 쇼핑 쇼츠 자동화 실행 중 → {url}")
    print(" 이 창을 닫으면 프로그램이 종료됩니다.")
    print()
    threading.Thread(target=open_browser_when_ready, args=(start_url,), daemon=True).start()
    while True:
        # 설정 화면에서 키를 저장하면 서버만 끄고, 바뀐 .env로 다시 켭니다.
        get_settings.cache_clear()
        app = create_app()
        server = uvicorn.Server(uvicorn.Config(app, host=bind_host, port=PORT))
        restart_requested = threading.Event()

        def restart(server: uvicorn.Server = server, flag: threading.Event = restart_requested) -> None:
            flag.set()
            server.should_exit = True

        app.state.restart = restart
        server.run()
        if not restart_requested.is_set():
            return 0
        print("\n 바뀐 설정으로 다시 켜는 중…\n")

if __name__ == "__main__":
    fix_console_encoding()
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — 콘솔이 바로 닫혀 오류를 못 보는 일을 막습니다.
        import traceback

        traceback.print_exc()
        print(f"\n실행 중 오류가 났습니다: {exc}")
        if getattr(sys, "frozen", False):
            input("Enter를 누르면 닫힙니다...")
        sys.exit(1)
