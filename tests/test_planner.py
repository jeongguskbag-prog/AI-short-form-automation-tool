import pytest

from app.schemas import MAX_SCENES, ScenePlan, ShortsPlan
from app.services.planner import PlanningError, normalize_plan


def scene(i, narration="말"):
    return ScenePlan(scene_id=i, narration=narration, visual_prompt=" ", subtitle_text="")


def test_normalize_renumbers_and_fills_blanks():
    plan = normalize_plan(ShortsPlan(title=" t ", scenes=[scene(7), scene(7, "  "), scene(3)]))
    assert [s.scene_id for s in plan.scenes] == [1, 2]
    assert plan.title == "t"
    assert plan.scenes[0].subtitle_text == "말"
    assert plan.scenes[0].visual_prompt == "말"


def test_normalize_caps_scene_count():
    plan = normalize_plan(ShortsPlan(title="t", scenes=[scene(i) for i in range(50)]))
    assert len(plan.scenes) == MAX_SCENES


def test_normalize_rejects_empty():
    with pytest.raises(PlanningError):
        normalize_plan(ShortsPlan(title="t", scenes=[scene(1, "")]))


def test_normalize_cleans_stock_excludes():
    s = ScenePlan(
        scene_id=1, narration="말", visual_prompt="v", subtitle_text="s",
        stock_query="carom billiards table",
        stock_exclude=[" Pool ", "pool", "snooker", "billiards hall", ""],
    )
    plan = normalize_plan(ShortsPlan(title="t", scenes=[s]))
    # 중복·빈 값 제거, 검색어와 겹치는 단어("billiards")가 든 제외어는 빼서 모든 사진이 걸러지지 않게 함
    assert plan.scenes[0].stock_exclude == ["pool", "snooker"]


def test_plan_schema_converts_to_gemini_schema():
    from google.genai import Client, _transformers

    schema = _transformers.t_schema(Client(api_key="x")._api_client, ShortsPlan)
    scene_props = schema.properties["scenes"].items.properties
    assert scene_props["stock_exclude"].items.type == "STRING"
