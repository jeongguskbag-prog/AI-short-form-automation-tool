@echo off
chcp 65001 >nul
setlocal
rem AI 쇼핑 쇼츠 자동화 - D 드라이브에 설치/업데이트
rem 이미 설치되어 있으면 프로그램만 새 버전으로 바꾸고 data, output, .env는 그대로 둡니다.
set "ZIP_URL=https://github.com/jeongguskbag-prog/AI-short-form-automation-tool/releases/download/windows-latest/AI-Shorts-windows.zip"
set "DEST=D:\AI-Shorts"
set "WORK=%TEMP%\ai-shorts-install"

echo ============================================================
echo  AI 쇼핑 쇼츠 자동화 설치
echo  설치 위치: %DEST%
echo ============================================================
echo.

if not exist D:\ (
  echo D 드라이브를 찾을 수 없습니다. 드라이브가 연결되어 있는지 확인하세요.
  pause
  exit /b 1
)

tasklist /FI "IMAGENAME eq AI-Shorts.exe" | find /I "AI-Shorts.exe" >nul
if not errorlevel 1 (
  echo AI-Shorts가 실행 중입니다. 검은 창을 닫은 뒤 아무 키나 누르세요.
  pause >nul
)

echo [1/3] 최신 버전을 받는 중입니다... 약 160MB
if exist "%WORK%" rmdir /s /q "%WORK%"
mkdir "%WORK%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri '%ZIP_URL%' -OutFile '%WORK%\AI-Shorts.zip'"
if errorlevel 1 goto :fail

echo [2/3] 설치하는 중입니다...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Path '%WORK%\AI-Shorts.zip' -DestinationPath '%WORK%\unzipped' -Force"
if errorlevel 1 goto :fail
robocopy "%WORK%\unzipped\AI-Shorts" "%DEST%" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto :fail
rmdir /s /q "%WORK%"

echo [3/3] 바탕화면 바로가기를 만드는 중입니다...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\AI-Shorts.lnk'); $s.TargetPath='%DEST%\AI-Shorts.exe'; $s.WorkingDirectory='%DEST%'; $s.Save()"

echo.
echo 설치를 마쳤습니다: %DEST%
echo 바탕화면의 'AI-Shorts' 바로가기를 더블클릭해 실행하세요.
echo.
if defined AI_SHORTS_NO_PROMPT exit /b 0
choice /C YN /M "지금 실행할까요"
if errorlevel 2 exit /b 0
start "" /D "%DEST%" "%DEST%\AI-Shorts.exe"
exit /b 0

:fail
echo.
echo 설치하지 못했습니다. 인터넷 연결과 D 드라이브 남은 공간을 확인한 뒤 다시 실행하세요.
pause
exit /b 1
