"""아이템 찾기 "왜 뜨는지" AI 한 줄 분석 (Gemini).

영상 자체는 보지 않고 공개 정보(제목·태그·조회수·댓글 수·구독자 대비 성과·시청자 댓글)만 보고 판단합니다.
"""

from google import genai
from pydantic import BaseModel

from ..config import Settings
from .llm import generate_structured
from .trends import TrendVideo, reference_summary

SYSTEM_INSTRUCTION = """\
당신은 쇼핑 쇼츠(제휴 마케팅) 트렌드 분석가입니다. 인기 쇼츠 하나의 공개 정보만 보고 짧게 분석합니다.
영상 화면은 볼 수 없으니, 주어진 제목·태그·숫자·댓글에서 알 수 있는 것만 말하세요. 추측은 추측이라고 쓰세요.

- product_name: 이 영상이 소개하는 것으로 보이는 제품명. 일반 명칭으로 짧게 (예: "접이식 실리콘 설거지통").
  알 수 없으면 빈 문자열.
- hook_type: 첫 문장(후킹) 유형 하나 — "불편 공감", "놀라운 결과", "질문", "비교", "가격·가성비", "기타" 중에서.
- why: 왜 반응이 좋은지 한 줄(40자 안팎). 숫자(구독자 대비 성과, 댓글 수)를 근거로 들 수 있으면 드세요.
- buyer_questions: 댓글에서 보이는 구매 관련 질문·관심 최대 3개 (예: "어디서 사요?", "식세기 돼요?").
  댓글이 없거나 해당 내용이 없으면 빈 목록.
- script_tip: 이 제품으로 대본을 쓸 때 참고할 점 한 줄. 문장을 베끼라는 조언은 하지 마세요.
- features: 제목·태그·댓글에서 알 수 있는 이 제품의 장점·용도 최대 4개, 각각 짧게 (예: "접어서 보관", "식세기 가능").
  알 수 없는 기능은 지어내지 말고, 근거가 없으면 빈 목록.
"""


class TrendAnalysis(BaseModel):
    """LLM 응답 스키마. Gemini의 response_schema로도 쓰이므로 단순하게 유지합니다."""

    product_name: str
    hook_type: str
    why: str
    buyer_questions: list[str]
    script_tip: str
    features: list[str]


def clean(a: TrendAnalysis) -> TrendAnalysis:
    def one_line(s: str, limit: int) -> str:
        return " ".join(s.split())[:limit]

    return TrendAnalysis(
        product_name=one_line(a.product_name, 60),
        hook_type=one_line(a.hook_type, 20),
        why=one_line(a.why, 120),
        buyer_questions=[q for q in (one_line(q, 60) for q in a.buyer_questions) if q][:3],
        script_tip=one_line(a.script_tip, 150),
        features=[f for f in (one_line(f, 40) for f in a.features) if f][:4],
    )


class GeminiTrendAnalyzer:
    def __init__(self, client: genai.Client, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def analyze(self, video: TrendVideo) -> TrendAnalysis:
        result = await generate_structured(
            self._client,
            self._settings,
            prompt=reference_summary(video, max_comment_chars=1500),
            system_instruction=SYSTEM_INSTRUCTION,
            schema=TrendAnalysis,
            what="트렌드 분석",
            temperature=0.3,
        )
        return clean(result)
