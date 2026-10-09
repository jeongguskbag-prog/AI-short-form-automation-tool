"""제품 정보 → 쇼핑 쇼츠 대본 (Gemini).

후킹 3안 + 본문 + 행동 유도 + 제목 5안 + 해시태그를 만들고, 광고 표시 문구를 붙이고, 과장·허위로 보일 수 있는
표현을 찾아 경고합니다. 참고 영상(아이템 찾기)은 후킹 유형과 시청자 반응만 참고하고 문장은 베끼지 않습니다.
"""

import re
from dataclasses import dataclass

from google import genai
from pydantic import BaseModel

from ..config import Settings
from .llm import generate_structured

SYSTEM_INSTRUCTION = """\
당신은 제휴 마케팅용 쇼핑 쇼츠(15~40초) 대본을 쓰는 한국어 카피라이터입니다.

구조 (반드시 이 순서):
1. hooks: 첫 3초에 시청자를 붙잡는 첫 문장 3가지. 서로 다른 유형으로 쓰세요
   (예: 공감되는 불편 상황 / 놀라운 결과 / 질문). 각각 25자 안팎.
2. body: 문제 상황 → 이 제품이 해결하는 방식 → 핵심 장점 2~3개. 4~7문장, 문장마다 15~35자.
   말하듯 자연스럽게, 한 문장에 한 가지 내용만.
3. cta: 행동 유도 한 문장 (예: "고정 댓글 링크에서 확인해 보세요").
4. titles: 업로드용 제목 5가지. 40자 이내, 클릭하고 싶게 하되 내용과 다른 낚시는 금지.
5. hashtags: 해시태그 5~8개, '#' 없이 단어만.

반드시 지킬 것:
- 사용자가 준 제품 정보 안에서만 장점을 말하세요. 없는 기능·수치·후기·인증을 지어내지 마세요.
- "무조건", "100%", "최고", "1위", "완치", "효과 보장", "부작용 없음" 같은 단정·과장·의학적 효능 표현을 쓰지 마세요.
- 가격·할인율·재고는 바뀌므로 말하지 마세요.
- 참고 영상이 주어지면 어떤 후킹이 먹혔는지, 시청자가 무엇을 궁금해했는지만 참고하고 문장을 그대로 옮기지 마세요.
- 광고 표시 문구는 시스템이 따로 붙이므로 대본에 넣지 마세요.
"""


class ShoppingScriptDraft(BaseModel):
    """LLM 응답 스키마. Gemini의 response_schema로도 쓰이므로 단순하게 유지합니다."""

    hooks: list[str]
    body: str
    cta: str
    titles: list[str]
    hashtags: list[str]


@dataclass
class ProductBrief:
    product_name: str
    features: str = ""
    tone: str = "energetic"
    reference: str = ""  # 참고 영상 요약 (제목·태그·댓글)


# 과장·허위 광고로 보일 수 있는 표현. (패턴, 이유)
RISKY_EXPRESSIONS: tuple[tuple[str, str], ...] = (
    (r"무조건|100\s*%|백\s*프로", "단정 표현"),
    (r"최고|최초|1\s*위|넘버\s*원|no\.?\s*1", "근거 없는 최상급 표현"),
    (r"완치|치료|치유|특효|부작용\s*(없|0|제로)", "의약품으로 오인될 수 있는 효능 표현"),
    (r"효과\s*(보장|확실)|보장합니다|책임집니다", "효과 보장 표현"),
    (r"살\s*(이\s*)?빠|체중\s*감량|다이어트\s*효과", "체중 감량 효능 표현"),
    (r"평생|영구|절대", "단정 표현"),
)


def find_risky_expressions(text: str) -> list[str]:
    warnings: list[str] = []
    for pattern, reason in RISKY_EXPRESSIONS:
        found = list(dict.fromkeys(m.group(0) for m in re.finditer(pattern, text, re.IGNORECASE)))
        if found:
            warnings.append(f"{reason}: {', '.join(repr(w) for w in found)}")
    return warnings


def _clean_lines(items: list[str], limit: int) -> list[str]:
    seen: dict[str, None] = {}
    for item in items:
        line = " ".join(item.split()).strip(" \"'")
        if line:
            seen.setdefault(line, None)
    return list(seen)[:limit]


def _clean_hashtags(tags: list[str], limit: int = 8) -> list[str]:
    cleaned = ["#" + re.sub(r"[^\w]", "", t.lstrip("#")) for t in tags]
    return _clean_lines([t for t in cleaned if len(t) > 1 and t != "#광고"], limit)


@dataclass
class ShoppingScript:
    hooks: list[str]
    body: str
    cta: str
    disclosure: str
    titles: list[str]
    hashtags: list[str]
    description: str
    warnings: list[str]

    def full_script(self, hook_index: int) -> str:
        """선택한 후킹으로 영상 만들기에 그대로 넣을 대본."""
        parts = [self.hooks[hook_index], self.body, self.cta, self.disclosure]
        return "\n".join(p for p in parts if p)


def finalize(draft: ShoppingScriptDraft, settings: Settings) -> ShoppingScript:
    """LLM 결과를 정리하고, 광고 표시와 설명란을 붙이고, 위험 표현을 검사합니다."""
    hooks = _clean_lines(draft.hooks, 3)
    if not hooks:
        raise ValueError("대본에 후킹 문장이 없습니다.")
    body = "\n".join(_clean_lines(draft.body.splitlines(), 20))
    if not body:
        raise ValueError("대본 본문이 비어 있습니다.")
    cta = " ".join(draft.cta.split())
    titles = _clean_lines(draft.titles, 5)
    hashtags = _clean_hashtags(draft.hashtags)

    # 설명란 첫 줄에 광고 표시 (추천·보증 심사지침: 소비자가 쉽게 볼 수 있는 곳에).
    description_lines = [settings.ad_disclosure, "", *titles[:1], " ".join(["#광고", *hashtags])]
    description = "\n".join(description_lines).strip()

    checked = "\n".join([*hooks, body, cta, *titles])
    return ShoppingScript(
        hooks=hooks,
        body=body,
        cta=cta,
        disclosure=settings.ad_disclosure_spoken,
        titles=titles,
        hashtags=hashtags,
        description=description,
        warnings=find_risky_expressions(checked),
    )


def build_prompt(brief: ProductBrief) -> str:
    lines = [f"제품명: {brief.product_name}", f"영상 분위기: {brief.tone}"]
    if brief.features:
        lines.append(f"제품 정보:\n{brief.features}")
    else:
        lines.append("제품 정보: (따로 없음 — 제품명에서 알 수 있는 일반적인 용도만 말하세요)")
    if brief.reference:
        lines.append(f"[참고 영상 — 후킹 유형과 시청자 반응만 참고]\n{brief.reference}")
    return "\n\n".join(lines)


class GeminiScriptWriter:
    def __init__(self, client: genai.Client, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def write(self, brief: ProductBrief) -> ShoppingScript:
        draft = await generate_structured(
            self._client,
            self._settings,
            prompt=build_prompt(brief),
            system_instruction=SYSTEM_INSTRUCTION,
            schema=ShoppingScriptDraft,
            what="쇼핑 대본",
            temperature=0.9,
        )
        return finalize(draft, self._settings)
