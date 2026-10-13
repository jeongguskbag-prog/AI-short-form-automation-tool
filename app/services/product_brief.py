"""쇼핑몰 상품 제목 → 짧은 제품명·특징·영어 검색어 (Gemini).

쇼핑몰 제목은 "2026 신형 캠핑 LED 랜턴 충전식 방수 초경량 …"처럼 길어서, 대본 칸에 넣기 좋게 정리합니다.
영어 검색어는 해외 쇼핑몰·스톡 영상에서 같은 제품의 영상·사진을 찾을 때 씁니다.
"""

from google import genai
from pydantic import BaseModel

from ..config import Settings
from .llm import generate_structured

SYSTEM_INSTRUCTION = """\
당신은 쇼핑 쇼츠 대본 작가의 조수입니다. 쇼핑몰 상품 제목(과 있으면 상품 정보)을 보고 정리합니다.

- product_name: 한국어 일반 제품명, 짧게 (예: "캠핑 LED 랜턴"). 브랜드·연도·모델 번호·광고 문구("무료배송", "1+1")는 뺍니다.
- features: 제목·정보에 실제로 적힌 장점·용도만 최대 4개, 각각 짧게 (예: "충전식", "방수", "초경량").
  적혀 있지 않은 기능은 지어내지 마세요.
- search_keyword_en: 해외 쇼핑몰·스톡 영상 검색용 영어 키워드 2~4단어 (예: "camping led lantern").
"""


class ProductBrief(BaseModel):
    """LLM 응답 스키마 (Gemini response_schema로도 쓰므로 기본값 없이 단순하게)."""

    product_name: str
    features: list[str]
    search_keyword_en: str


def clean(b: ProductBrief) -> ProductBrief:
    def one_line(s: str, limit: int) -> str:
        return " ".join(s.split())[:limit]

    return ProductBrief(
        product_name=one_line(b.product_name, 60),
        features=[f for f in (one_line(f, 40) for f in b.features) if f][:4],
        search_keyword_en=one_line(b.search_keyword_en, 60),
    )


class GeminiProductBriefer:
    def __init__(self, client: genai.Client, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def brief(self, title: str, details: str = "") -> ProductBrief:
        prompt = f"상품 제목: {title.strip()}"
        if details.strip():
            prompt += f"\n상품 정보: {details.strip()[:1500]}"
        result = await generate_structured(
            self._client, self._settings, prompt=prompt, system_instruction=SYSTEM_INSTRUCTION,
            schema=ProductBrief, what="상품 정리", temperature=0.2,
        )
        return clean(result)
