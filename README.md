# AI 쇼핑 쇼츠 자동화

제품 키워드 하나로 **아이템 찾기 → 쇼핑 대본 → 영상 → 보관함 → 유튜브 배포 → 성과 확인**까지 이어지는 쇼핑 쇼츠 제작 도구입니다.
Gemini(대본·장면 기획), Edge TTS(내레이션), FFmpeg(9:16 영상 합성)를 쓰는 FastAPI 서버이고, 웹 화면으로 씁니다.
기획 배경과 앞으로의 계획은 [docs/PLAN.md](docs/PLAN.md)에 있습니다.

## 처음 시작하기

1. **설치하고 켜기** — Windows PC는 [실행 파일(EXE)](#windows-실행-파일-exe)을 받아 더블클릭하면 됩니다.
   그 밖에는 아래 [실행](#실행)에서 Docker·PC·Windows·안드로이드 중 하나를 따릅니다.
2. **키 넣기** — 설정 점검 화면의 '키 넣기'(또는 `.env`)에 `GEMINI_API_KEY`(필수)를 넣습니다. 선택: `YOUTUBE_API_KEY`(아이템 찾기),
   `PEXELS_API_KEY`·`PIXABAY_API_KEY`(무료 사진·영상), `YOUTUBE_OAUTH_CLIENT_ID/SECRET`(유튜브 올리기).
3. **설정 점검** — http://localhost:8000/setup 의 '키 넣기'에서 키를 넣거나 고치고(이 PC에서 연 화면만), "점검 시작"으로 모두 정상인지 확인합니다.
4. **첫 영상** — http://localhost:8000/trends 에서 인기 쇼츠를 고르고 "이 제품으로 만들기" → 대본 자동 작성 →
   (선택) 내 영상·사진·배경음악 → 영상 만들기.
5. **올리기** — 보관함(http://localhost:8000/library)에서 유튜브 채널을 연결하고 바로 또는 예약으로 올립니다.

## 파이프라인

```
제품명 ─▶ (아이템 찾기 참고 영상·내 채널 성과) ─▶ Gemini 쇼핑 대본 (후킹 3안·제목·설명란·광고 표시)
대본 ─▶ Gemini 장면 기획 ─▶ (내 영상·사진이 있으면 AI가 장면별로 배치)
     └▶ 장면별 병렬 처리
          ├ Edge TTS 내레이션 + 단어별 시각
          ├ 배경: 내 파일 → 무료 스톡 영상 → AI 이미지·무료 사진 → 기본 배경
          ├ 자막: 읽는 단어를 강조하는 짧은 구절
          └ FFmpeg 장면 렌더링 (9:16, 켄 번스 / 영상 반복)
     └▶ 이어 붙이기 → 배경음악(덕킹) → 썸네일 3안 → 보관함 저장 → 유튜브 올리기(즉시·예약) → 성과 수집
```

# 기능 (쓰는 순서대로)

## 아이템 찾기

분야(생활·가전·인테리어, 주방, 뷰티, 디지털, 패션, 육아, 반려동물)별로 최근 1년 인기 쇼츠의 **공개 지표**를 모아
기간(1·3·6개월, 1년)과 정렬로 골라 봅니다. 영상 파일은 내려받지 않습니다.

- `.env`에 `YOUTUBE_API_KEY`를 넣으면 서버가 시작할 때와 `TRENDS_REFRESH_HOURS`(기본 12시간)마다 모읍니다.
  화면은 모아 둔 저장본(`data/trends.db`)을 쓰므로, 결과 위에 **언제 기준 데이터인지** 표시됩니다.
  Docker로 실행하면 `-v "$PWD/data:/app/data"`로 저장본을 남겨 두세요.
- **성과순** = 조회수 ÷ 채널 구독자 수. 카드의 `x412`는 구독자의 412배가 봤다는 뜻입니다. 채널 인기가 아니라
  제품·후킹 덕에 터진 영상을 찾는 기준입니다. 구독자 수를 숨긴 채널처럼 값을 알 수 없는 영상은 빼지 않고 뒤에 둡니다.
- 검색어는 제목·태그·채널 이름과, 분야마다 성과 상위 영상(`TRENDS_COMMENT_VIDEOS`, 기본 10개)의 공개 댓글에서 찾습니다.
- 즐겨찾기한 영상은 어떤 조건에서든 맨 앞에 오고, 다음 수집에서 빠져도 남습니다.
- 1회 수집에 YouTube 일일 기본 할당량(10,000)의 약 15%(검색 14회 × 100)를 씁니다. 할당량을 다 쓰면 화면에 안내가 나오고
  한 시간 뒤에 다시 시도합니다.
- **왜 뜨는지 (AI 분석)**: 카드의 버튼을 누르면 Gemini가 그 영상의 공개 정보(제목·태그·조회수·구독자 대비 성과·시청자 댓글)로
  추정 제품명, 후킹 유형, 반응이 좋은 이유 한 줄, 댓글 속 구매 질문, 대본 팁을 알려 줍니다. 영상 화면은 보지 않습니다.
  결과는 저장해 두고 다시 부르지 않으며(`POST /api/v1/trends/{video_id}/analysis`, `?refresh=true`로 다시 분석),
  분석 뒤 "이 제품으로 만들기"를 누르면 추정 제품명이 쇼핑 대본 칸에 미리 채워집니다.
- "이 제품으로 만들기"는 참고 영상을 띄운 채 영상 만들기 화면으로 넘어가, 쇼핑 대본 자동 작성에 참고 영상으로 넘깁니다.

API: `GET /api/v1/trends?category=kitchen&period=1m&sort=comments&q=주걱`, `POST /api/v1/trends/refresh`,
`PUT|DELETE /api/v1/trends/{video_id}/favorite`

## 쇼핑 대본 자동 작성

영상 만들기 화면 위쪽의 **쇼핑 대본 자동 작성**에 제품명과 특징을 넣으면 Gemini가 대본을 써 줍니다.

- 첫 문장(후킹) 3안 → 고르면 대본 칸이 그 안으로 채워집니다. 구조: 후킹 → 문제 상황 → 해결 → 장점 2~3개 → 행동 유도.
- 제목 후보 5개, 해시태그, 업로드 **설명란**(첫 줄 광고 표시 + `#광고`)을 함께 만듭니다.
- **광고 표시**: 대본 마지막 문장(`AD_DISCLOSURE_SPOKEN`)과 설명란 첫 줄(`AD_DISCLOSURE`)에 자동으로 붙습니다.
  기본값은 쿠팡 파트너스 문구이니 다른 제휴 프로그램을 쓰면 `.env`에서 바꾸세요.
- "무조건", "100%", "1위", "완치", "효과 보장", "살 빠져요" 같은 **과장·허위로 보일 수 있는 표현**을 찾아 경고합니다
  (막지는 않으니 직접 고치세요). AI에게도 없는 기능·수치를 지어내지 말라고 지시합니다.
- **내 채널 성과 반영**: 유튜브에 올린 영상 중 성과(조회수)를 받아 온 것이 2개 이상이면, 조회수가 높은 영상과 낮은 영상의
  첫 문장을 함께 보여 주고 잘된 유형은 살리고 안 된 유형은 피하게 합니다 (문장은 베끼지 않음). 끄려면 `"use_channel_insights": false`.
- 아이템 찾기에서 "이 제품으로 만들기"로 넘어오면 그 영상의 제목·태그·반응·시청자 댓글을 참고해 후킹을 씁니다.
  문장은 베끼지 않습니다.

API: `POST /api/v1/scripts/shopping` `{"product_name": "...", "features": "...", "tone": "energetic", "reference_video_id": "..."}`

## 내 영상·사진 넣기

영상 만들기 화면의 **내 영상·사진**에 직접 찍었거나 판매자에게 사용 허락을 받은 제품 영상·사진을 올리면,
AI 이미지·무료 사진 대신 장면 배경으로 들어갑니다.

- **AI 배치(기본)**: 파일이 2개 이상이면 Gemini가 각 파일의 미리보기(사진은 줄인 사본, 영상은 중간 프레임)와 장면
  내레이션을 보고 어울리는 파일을 장면마다 고릅니다. 모든 파일을 한 번 이상 쓰고, 같은 파일을 연달아 쓰지 않게 합니다.
  AI 결과가 이상하거나 실패하면 올린 순서대로 넣습니다. 끄려면 화면에서 체크를 풀거나 `"media_order": "upload"`.
- 같은 영상을 다시 쓸 때는 앞에서 쓴 횟수만큼 3초씩 뒤에서 시작합니다.

- 영상(mp4, mov, m4v, webm)은 화면을 꽉 채우도록 9:16으로 가운데를 자르고, 씬보다 짧으면 반복합니다.
  영상의 원래 소리는 빼고 내레이션만 넣습니다.
- 사진(jpg, png, webp)은 지금처럼 천천히 확대·축소(켄 번스)합니다.
- 한 파일 최대 `MAX_UPLOAD_MB`(기본 200MB), 한 영상에 20개까지. 올린 파일은 `data/uploads`에 저장되고
  `UPLOAD_TTL_HOURS`(기본 72시간) 뒤에 지워집니다.

API: `PUT /api/v1/media?filename=상품.mp4` (본문에 파일 내용 그대로) → `{"media_id": "...", "kind": "video", "duration": 4.2}`,
생성 요청에 `"media_ids": ["...", "..."]`.

## 배경음악

영상 만들기 화면의 **배경음악**에 음악 파일(mp3, m4a, aac, wav, ogg)을 올리면 완성 영상에 깔립니다.

- 영상 길이에 맞춰 반복하고, 끝 1.5초 동안 서서히 줄입니다.
- **내레이션이 나올 때는 음악을 자동으로 줄입니다**(사이드체인 덕킹). 끄려면 `BGM_DUCKING=false`.
- 음량은 화면의 슬라이더(5~60%)로 고르고, 기본값은 `BGM_VOLUME`(0.25 = 원래 크기의 25%)입니다.
- 사용 허락을 받은 음악(유튜브 오디오 보관함 등)만 쓰세요. 배경음악을 넣지 못하면 음악 없이 완성하고 경고를 띄웁니다.

API: `PUT /api/v1/media?filename=bgm.mp3`로 올린 뒤 생성 요청에 `"bgm_media_id": "...", "bgm_volume": 0.3`.

## 단어 단위 자막

Edge TTS가 단어마다 읽는 시각을 알려 주면, 자막을 내레이션 그대로 **짧은 구절(한 줄, 10자 안팎)**로 바꿔 가며 보여 주고
**지금 읽는 단어를 노란색**으로 강조합니다. 구절 이미지들을 시간에 맞춰 이어 붙인 하나의 스트림으로 합성하므로
씬마다 합성 비용은 예전과 같습니다. 예전처럼 씬마다 요약 자막 한 줄을 쓰려면 `SUBTITLE_MODE=scene`.

## 썸네일 3안

영상이 완성되면 썸네일을 세 가지 디자인(위쪽 노란 글씨 / 가운데 흰 글씨 / 아래 빨간 상자)으로 함께 만듭니다.
배경은 자막이 없는 장면 원본(올린 영상·사진 또는 AI·무료 사진)에서 서로 다른 장면 3개를 고르고,
마지막 광고 표시 장면은 피합니다. 문구는 **썸네일 문구** 칸에 넣거나(40자), 비우면 영상 제목을 씁니다.
쇼핑 대본에서 첫 문장을 고르면 자동으로 채워집니다. 상태 조회의 `thumbnail_urls`로 받을 수 있습니다.

## 장면 고치기

영상이 완성되면 ④ 완성 단계의 **장면 고치기**에서 장면마다 미리보기를 보며 문장을 고치거나 배경을 올린 파일로 바꿀 수 있습니다.
**바뀐 장면만** 음성·자막·영상을 다시 만들고 나머지 장면은 그대로 이어 붙여(AI 기획은 다시 하지 않음) 새 영상으로 만들고,
보관함에도 새로 저장합니다. 고친 영상을 또 고칠 수 있습니다. 작업 폴더가 남아 있는 동안(`JOB_TTL_HOURS`, 기본 24시간)만 됩니다.

API: `GET /api/v1/shorts/{job_id}/scenes`, `POST /api/v1/shorts/{job_id}/revise`
`{"scenes": [{"narration": "...", "media_id": "..."}, ...], "thumbnail_text": "..."}` → 새 작업.

## 보관함

영상이 완성되면 영상·썸네일을 `data/library/`로 복사하고, 대본·제품명·설명란·제목 후보와 함께 `data/projects.db`에
저장합니다. 작업 폴더(`output/`)는 `JOB_TTL_HOURS`(24시간) 뒤 지워지지만 보관함은 `LIBRARY_TTL_DAYS`(기본 30일) 동안 남습니다.
서버를 다시 켜도 그대로 있습니다. Docker로 실행하면 `-v "$PWD/data:/app/data"`로 남겨 두세요.

API: `GET /api/v1/projects`, `GET /api/v1/projects/{id}`(서명된 영상·썸네일 링크 포함), `DELETE /api/v1/projects/{id}`.
생성 요청의 `"project": {"product_name", "description", "titles", "reference_video_id"}`가 함께 저장됩니다
(영상 만들기 화면은 쇼핑 대본 결과를 자동으로 넣습니다). 상태 조회의 `project_id`로 저장 여부를 알 수 있습니다.

## 배포 (유튜브)

보관함에서 영상을 열고 **유튜브에 올리기**로 연결한 채널에 바로 올립니다. 설명란(광고 표시 포함), 해시태그, `#Shorts`,
'AI로 만든 콘텐츠' 표시(`YOUTUBE_DECLARE_SYNTHETIC`)가 함께 들어가고, 결과 링크가 보관함에 기록됩니다.

처음 한 번 준비 (본인 Google Cloud 프로젝트):
1. [YouTube Data API v3](https://console.cloud.google.com/apis/library/youtube.googleapis.com)를 사용 설정합니다.
2. **OAuth 동의 화면**을 '외부'로 만들고 **테스트 사용자**에 본인 구글 계정을 넣습니다 (심사 없이 본인만 사용).
3. **사용자 인증 정보 → OAuth 클라이언트 ID → 웹 애플리케이션**을 만들고, 승인된 리디렉션 URI에
   `http://localhost:8000/oauth/youtube/callback`(`YOUTUBE_REDIRECT_URI`)을 넣습니다.
4. `.env`에 `YOUTUBE_OAUTH_CLIENT_ID`, `YOUTUBE_OAUTH_CLIENT_SECRET`을 넣고 서버를 다시 켠 뒤, 보관함에서 **유튜브 채널 연결**.

알아 둘 점:
- 구글의 앱 심사(감사) 전에는 API로 올린 영상이 **비공개로만** 올라갑니다. 유튜브 스튜디오에서 공개로 바꾸세요.
- 업로드 1건에 일일 기본 할당량(10,000)의 1,600을 써서 **하루 약 6건**까지 올릴 수 있습니다.
- 연결 토큰은 암호화해 `data/accounts.json`에 저장합니다. 키는 `TOKEN_ENCRYPTION_KEY`(비우면 `data/accounts.key`에 자동 생성).
- **예약 올리기**: "올리는 시간 → 예약"으로 날짜·시각을 고르면 그 시각에 서버가 올립니다 (1분 뒤 ~ 60일 안, 예약 취소 가능).
  하루 약 6건 한도가 있으니 여러 영상을 며칠에 나눠 올릴 때 쓰세요. 서버가 켜져 있어야 올라가고, 꺼져 있던 동안 지난 예약은
  다시 켰을 때 바로 올립니다. API: 올리기 요청에 `"scheduled_at": "2026-10-11T09:00:00+09:00"`,
  취소는 `DELETE /api/v1/projects/{id}/publications/{publication_id}`.
- 인스타그램·페이스북·스레드·틱톡은 앱 심사가 필요해 아직 지원하지 않습니다.

**성과**: 올린 영상의 조회수·좋아요·댓글을 연결한 채널 권한으로 `STATS_REFRESH_HOURS`(기본 6시간)마다 받아 와
보관함 위쪽(올린 영상 수, 총 조회수, 가장 잘된 영상)과 카드·상세에 보여 줍니다. 비공개 영상도 받을 수 있고,
"지금 다시 받기"로 바로 갱신합니다 (유튜브 할당량: 영상 50개당 1). API: `GET /api/v1/stats`, `POST /api/v1/stats/refresh`.

# 설치·운영

## 실행

### Windows 실행 파일 (EXE)

설치 없이 바로 씁니다. Python·FFmpeg가 모두 들어 있습니다.

1. [`install-windows.bat`](https://github.com/jeongguskbag-prog/AI-short-form-automation-tool/releases/download/windows-latest/install-windows.bat)을
   받아 더블클릭하면 `D:\AI-Shorts`에 설치하고 바탕화면에 `AI-Shorts` 바로가기를 만듭니다.
   업데이트할 때도 같은 파일을 실행하면 `data`, `output`, `.env`는 그대로 두고 프로그램만 바꿉니다.
   직접 하려면 [Releases](https://github.com/jeongguskbag-prog/AI-short-form-automation-tool/releases/tag/windows-latest)의
   `AI-Shorts-windows.zip`을 원하는 곳에 풉니다.
2. 바로가기(또는 `AI-Shorts.exe`)를 더블클릭합니다. "Windows의 PC 보호"가 뜨면 **추가 정보 → 실행**.
3. 처음에는 브라우저에 **설정 점검** 화면이 열립니다. '키 넣기'에 Gemini 키를 붙여 넣고 저장하면 프로그램이 저절로 다시 켜져 바로 적용됩니다.

검은 창을 닫으면 종료됩니다. 영상·설정은 exe 옆의 `data`, `output`, `.env`에 저장되니 새 버전으로 바꿀 때 함께 옮기세요.
실행 파일은 `main`에 올라올 때마다 GitHub Actions(`.github/workflows/windows-exe.yml`)가 새로 만듭니다.
직접 만들려면 Windows에서 `pip install -r requirements.txt pyinstaller` 후 `pyinstaller packaging/shorts.spec`
(결과: `dist\AI-Shorts`, FFmpeg는 `dist\AI-Shorts\ffmpeg\`에 `ffmpeg.exe`·`ffprobe.exe`를 넣거나 PATH에 설치).

### Docker (권장)

```bash
cp .env.example .env   # GEMINI_API_KEY 입력
docker build -t ai-shorts .
docker run --env-file .env -p 8000:8000 -v "$PWD/output:/app/output" -v "$PWD/data:/app/data" ai-shorts
```

### 로컬

필요: Python 3.11+, `ffmpeg`/`ffprobe`, 한글 폰트(예: `fonts-noto-cjk`, 나눔고딕). 폰트는 자동 탐색하며 `FONT_PATH`로 지정할 수도 있습니다.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # GEMINI_API_KEY 입력
python main.py         # 개발 중 자동 재시작: python main.py --reload
```

### Windows

PowerShell에서 실행합니다. 한글 자막 폰트는 기본 설치된 맑은 고딕을 자동으로 사용합니다.

```powershell
winget install Python.Python.3.12 Gyan.FFmpeg Git.Git   # 설치 후 PowerShell을 새로 여세요
git clone https://github.com/jeongguskbag-prog/AI-short-form-automation-tool.git
cd AI-short-form-automation-tool
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1      # 막히면: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements.txt
copy .env.example .env           # 메모장으로 열어 GEMINI_API_KEY 입력: notepad .env
python main.py
```

### 안드로이드 휴대폰 (Termux)

휴대폰 안에서 서버를 돌리고 휴대폰 브라우저로 씁니다. 아이폰은 지원하지 않습니다.
아직 실제 휴대폰에서 검증하지 않은 방법이며, 처음 설치할 때 패키지 빌드로 10~30분 걸릴 수 있습니다.

1. **Termux**를 [F-Droid](https://f-droid.org/packages/com.termux/) 또는
   [GitHub 릴리스](https://github.com/termux/termux-app/releases)에서 설치합니다.
2. Termux에서:
   ```bash
   pkg install -y git
   git clone https://github.com/jeongguskbag-prog/AI-short-form-automation-tool.git
   cd AI-short-form-automation-tool
   bash scripts/termux-setup.sh     # 설치 (처음 한 번)
   nano .env                         # GEMINI_API_KEY 입력 → Ctrl+O, Enter → Ctrl+X
   bash scripts/termux-run.sh        # 실행 → 브라우저가 자동으로 열림 (종료: Ctrl+C)
   ```
3. 다음부터는 `cd AI-short-form-automation-tool && bash scripts/termux-run.sh`만 실행하면 됩니다.

- 자막 폰트는 안드로이드 시스템 폰트(`/system/fonts`의 Noto CJK 등)를 자동으로 찾습니다.
- 실행 중에는 Termux 절전 해제(wake lock)를 걸어 화면이 꺼져도 영상 생성이 계속됩니다.
- 발열을 줄이려고 씬을 동시에 2개까지만 만듭니다 (`.env`의 `MAX_CONCURRENT_SCENES`).

웹 화면: http://localhost:8000 — 단계별로 진행: ① 아이템 찾기 → ② 대본(쇼핑 대본 자동 작성·분위기) → ③ 소재·음악(내 영상·사진, 배경음악, 썸네일 문구) → ④ 완성(진행 상황, 미리보기·다운로드, 썸네일, 보관함) · ⑤ 배포는 준비 중

설정 점검: http://localhost:8000/setup — 키를 넣었는데 잘 안 될 때 "점검 시작"을 누르면 FFmpeg·폰트, 저장 공간, Gemini,
Edge TTS, YouTube Data API 키, Pexels, Pixabay, Openverse, 유튜브 채널 연결을 하나씩 가볍게 시험하고 안 되는 이유와 해결 방법을 알려 줍니다
(`GET /api/v1/setup/check`).

보관함: http://localhost:8000/library — 완성된 영상·썸네일·설명란·제목 후보를 저장해 두고 다시 보기·내려받기·삭제

아이템 찾기: http://localhost:8000/trends — 분야별 인기 쇼츠를 성과순·조회수순·좋아요순·댓글순·최신순으로 보기

API 문서: http://localhost:8000/docs

## API

### 인증

`.env`의 `API_KEYS`에 키를 넣으면 `/api/v1/*` 요청에 키가 필요합니다. 쉼표로 여러 개를 넣을 수 있어 키를 바꿀 때
새 키를 추가하고 옛 키를 나중에 지우면 됩니다. 비워 두면 인증 없이 실행됩니다 (로컬 개발용).

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"   # 키 생성
```

- 헤더: `X-API-Key: <키>` 또는 `Authorization: Bearer <키>`
- 웹 화면은 서버가 인증을 요구하면 키 입력란을 보여주고, 입력한 키를 그 브라우저에만 저장합니다.
- `/`, `/health`, `/docs`는 공개입니다.

### 영상 링크

완성 영상은 `<video>` 태그로 재생해야 해서 API 키 대신 **서명된 링크**로 엽니다.

- 상태 조회 응답의 `video_url`은 `/videos/{job_id}/{파일}?expires=...&sig=...` 형태이며, `video_url_expires_at`까지만
  열립니다 (기본 1시간, `VIDEO_URL_TTL_SEC`). 서명은 HMAC-SHA256이라 주소나 만료 시각을 바꾸면 403이 됩니다.
- 만료되면 상태를 다시 조회해 새 링크를 받으세요. 웹 화면은 이를 자동으로 처리합니다.
- `URL_SIGNING_SECRET`을 비워 두면 서버가 시작할 때마다 새 비밀키를 만들어, 재시작하면 기존 링크가 모두 무효가 됩니다.

```bash
# 작업 생성 (202 Accepted)
curl -X POST localhost:8000/api/v1/shorts/generate \
  -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"script": "하루 10분 투자로 인생이 바뀌는 습관 3가지를 알려드릴게요...", "tone": "energetic"}'
# → {"job_id": "3f9c...", "status": "QUEUED", "status_url": "/api/v1/shorts/status/3f9c...", ...}

# 상태 조회
curl -H "X-API-Key: $API_KEY" localhost:8000/api/v1/shorts/status/3f9c...
# → {"status": "COMPLETED", "progress": 100, "video_url": "/videos/3f9c.../final_3f9c....mp4?expires=...&sig=...",
#    "video_url_expires_at": "...",
#    "title": "...", "scene_count": 6, "duration_sec": 27.4, "warnings": [], ...}
```

상태: `QUEUED → PLANNING → GENERATING_ASSETS → RENDERING_FINAL → COMPLETED | FAILED`

## Gemini 요금 등급과 모델

- **씬 기획**은 무료 등급으로 됩니다. 기본값 `gemini-flash-latest`는 Google이 최신 Flash 모델로 자동 연결해 주는 별칭이라
  특정 버전이 내려가도 404가 나지 않습니다. 과부하(503) 등으로 계속 실패하면 `LLM_FALLBACK_MODEL`(기본 `gemini-flash-lite-latest`)로
  한 번 더 시도합니다.
- **AI 이미지 생성은 무료 등급 한도가 0**이라 결제를 켜야 합니다 (2026년 10월 실제 키로 확인). 결제를 켤 때는 AI Studio에서
  월 지출 한도도 함께 설정하세요.
- **결제 없이도 무료 사진이 들어갑니다.** AI가 씬마다 만든 영어 검색어로 세로 사진을 찾아 넣습니다.
  - **Openverse**: 키가 필요 없습니다. 출처 표기만으로 쓸 수 있는 라이선스(CC0, 퍼블릭 도메인, CC BY)만 검색합니다.
    키 없이 쓰는 대신 요청 횟수 제한이 있습니다.
  - **Pixabay**: [pixabay.com/api/docs](https://pixabay.com/api/docs/)에서 가입하면 바로 받는 키를 `PIXABAY_API_KEY`에 넣습니다.
  - **Pexels**: [pexels.com/api](https://www.pexels.com/api/) 키를 `PEXELS_API_KEY`에 넣습니다.
  - 사진 출처는 상태 조회의 `image_credits`와 웹 화면 영상 아래에 표시됩니다. 영상을 올릴 때 설명란에도 출처를 적어 주세요.
    상업적으로 쓰기 전에 각 서비스의 라이선스를 확인하세요.
- **무료 스톡 영상**: Pexels·Pixabay 키가 있으면 올린 영상이 없는 씬에 사진보다 **짧은 무료 영상**을 먼저 찾아 넣습니다
  (세로 영상 우선, 화면 높이에 맞는 화질, 한 개 최대 `MAX_STOCK_VIDEO_MB`=40MB). 못 찾거나 깨진 파일이면 사진으로 넘어갑니다.
  사진만 쓰려면 `STOCK_VIDEO=false`.
- 이미지는 `IMAGE_SOURCES`(기본 `gemini,pexels,pixabay,openverse`) 순서로 시도하고, 키가 없는 서비스는 건너뜁니다.
  한도 초과·키 오류가 난 소스는 1시간 동안 건너뛰어(`IMAGE_SOURCE_COOLDOWN_SEC`) 결제가 꺼진 Gemini에 씬마다 헛된 요청을
  보내지 않습니다. 모두 실패하면 그라디언트 배경을 씁니다.
- 기본 이미지 모델은 `gemini-3.1-flash-image`입니다. `imagen-`으로 시작하는 모델을 지정하면 Imagen API를 사용합니다.
- 쓸 수 있는 모델 목록 확인:
  `python -c "from google import genai; [print(m.name) for m in genai.Client().models.list()]"`

## 설정

모든 설정은 환경변수(.env)로 바꿀 수 있습니다. 전체 목록은 `app/config.py`, 예시는 `.env.example`을 참고하세요.

## 테스트

외부 API 없이 가짜 공급자로 실제 FFmpeg 렌더링까지 검증합니다.

```bash
pip install -r requirements-dev.txt
pytest
```

## 운영 시 참고

- 작업 상태는 프로세스 메모리에 저장되므로 **워커 1개**로 실행하세요. 수평 확장이 필요하면 `JobStore`를 Redis로,
  작업 실행을 Celery/arq 같은 큐로 바꾸면 됩니다 (`ShortsPipeline`은 그대로 재사용 가능).
- 완료된 작업과 파일은 `JOB_TTL_HOURS` 후 자동 삭제됩니다.
- 외부에 공개할 때는 `API_KEYS`를 꼭 설정하고 HTTPS(리버스 프록시) 뒤에서 실행하세요. 키가 평문 헤더로 전송됩니다.
