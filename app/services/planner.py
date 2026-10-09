"""대본 → 씬 기획 (Gemini)."""

from google import genai

from ..config import Settings
from ..schemas import MAX_SCENES, ScenePlan, ShortsPlan
from .llm import generate_structured

SYSTEM_INSTRUCTION = """\
당신은 조회수가 높은 유튜브 쇼츠를 만드는 전문 기획자입니다.
사용자의 대본을 9:16 세로 쇼츠 영상의 씬(Scene) 목록으로 분해합니다.

규칙:
- 대본의 내용과 순서를 지키되, 첫 씬은 시청자를 붙잡는 강한 훅으로 만드세요.
- 각 씬의 narration은 한국어로 소리 내어 읽었을 때 약 3~5초(대략 15~35자) 분량입니다.
- subtitle_text는 화면에 띄울 짧은 한국어 자막입니다. narration의 핵심만 25자 이내로 요약하세요.
- visual_prompt는 이미지 생성 AI용 영어 묘사입니다. 피사체, 배경, 구도, 조명, 색감을 구체적으로 쓰고,
  이미지 안에 글자·자막·로고가 들어가지 않게 하세요. 모든 씬의 화풍은 일관되게 유지하세요.
- stock_query는 무료 사진 사이트에서 이 씬에 어울리는 실사 사진을 찾을 영어 검색어 2~4단어입니다.
  추상적인 표현 대신 사진으로 찍을 수 있는 구체적인 사물·장면을 쓰세요 (예: "glass of water kitchen").
- scene_id는 1부터 시작하는 연속된 정수입니다.
- 씬은 최소 1개, 최대 {max_scenes}개입니다.
- 대본에 광고 표시 문장(제휴·수수료를 알리는 문장)이 있으면 빼거나 바꾸지 말고, narration과 subtitle_text에
  그대로 넣어 마지막 씬으로 두세요.
"""


class PlanningError(RuntimeError):
    pass


def normalize_plan(plan: ShortsPlan) -> ShortsPlan:
    """LLM 출력을 정리합니다. 빈 씬 제거, scene_id 재부여, 씬 수 제한."""
    scenes: list[ScenePlan] = []
    for scene in plan.scenes:
        narration = scene.narration.strip()
        if not narration:
            continue
        scenes.append(
            ScenePlan(
                scene_id=len(scenes) + 1,
                narration=narration,
                visual_prompt=scene.visual_prompt.strip() or narration,
                subtitle_text=scene.subtitle_text.strip() or narration,
                stock_query=" ".join(scene.stock_query.split()[:6]),
            )
        )
        if len(scenes) == MAX_SCENES:
            break
    if not scenes:
        raise PlanningError("기획 결과에 유효한 씬이 없습니다.")
    return ShortsPlan(title=plan.title.strip() or "Untitled", scenes=scenes)


class GeminiPlanner:
    def __init__(self, client: genai.Client, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def plan(self, script: str, tone: str) -> ShortsPlan:
        plan = await generate_structured(
            self._client,
            self._settings,
            prompt=f"영상 분위기: {tone}\n\n[대본]\n{script}",
            system_instruction=SYSTEM_INSTRUCTION.format(max_scenes=MAX_SCENES),
            schema=ShortsPlan,
            what="씬 기획",
        )
        return normalize_plan(plan)
