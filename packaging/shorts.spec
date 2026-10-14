# PyInstaller 설정: pyinstaller packaging/shorts.spec
# 결과물: dist/AI-Shorts/AI-Shorts.exe (폴더째 배포)
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent

hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("app")
    + collect_submodules("edge_tts")
    + collect_submodules("gtts")
)
datas = [
    (str(ROOT / "app" / "web"), "app/web"),
    (str(ROOT / ".env.example"), "."),
]
datas += collect_data_files("certifi")
datas += collect_data_files("edge_tts")

a = Analysis(
    [str(ROOT / "launcher.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AI-Shorts",
    console=True,  # 서버 로그와 안내를 보여 주고, 창을 닫으면 종료됩니다.
)
coll = COLLECT(exe, a.binaries, a.datas, name="AI-Shorts")
