"""CODE_MERGE（合并槽位）：多轮槽位继承、预算区间、槽位输出解析兜底、条件追问只问一次、画像默认值和改口。"""
import json

from conftest import flow_code, run_node


def merge(raw: dict | str, prev: dict | None = None, **kw) -> dict:
    params = {"prev_slots": json.dumps(prev or {}, ensure_ascii=False),
              "raw": raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False),
              "prof_json": "{}", "mem": "", "active_json": "[]", "stage": "", "turn": "0"}
    params.update(kw)
    return run_node(flow_code.CODE_MERGE, **params)


def slots(out: dict) -> dict:
    return json.loads(out["slots_json"])


def test_inherits_previous_turn():
    out = merge({"budget_max": 100}, prev={"area": "海岸城", "category": "火锅"})
    s = slots(out)
    assert s["area"] == "海岸城" and s["category"] == "火锅" and s["budget_max"] == 100
    assert out["need_clarify"] is False
    assert out["turn"] == "1"


def test_budget_range_replaced_as_a_whole():
    # 上轮"一两百"，本轮"200 以内"：下限不能留着
    out = merge({"budget_max": 200}, prev={"area": "海岸城", "budget_min": 100, "budget_max": 200})
    s = slots(out)
    assert "budget_min" not in s and s["budget_max"] == 200


def test_budget_min_max_swapped():
    out = merge({"area": "海岸城", "budget_min": 300, "budget_max": 100})
    s = slots(out)
    assert (s["budget_min"], s["budget_max"]) == (100, 300)


def test_broken_json_falls_back_to_field_regex():
    # 10-21 E1 s09：question 里有英文双引号，整段 JSON 解析失败，预算不能丢
    raw = '{"area": "海岸城", "budget_max": 100, "need_clarify": false, "question": "要"辣"的吗", "options": ["要", "不要"]}'
    s = slots(merge(raw))
    assert s["area"] == "海岸城" and s["budget_max"] == 100


def test_ask_when_area_and_category_missing():
    out = merge({"budget_max": 100})
    assert out["need_clarify"] is True and out["question"]
    # 追问附带选项按钮，末尾固定"你看着办"
    card = json.loads(out["question_ans"].split("```card", 1)[1].split("```", 1)[0])
    assert card["type"] == "options" and card["items"][-1] == "都行，你看着办"
    assert 3 <= len(card["items"]) <= 4


def test_ask_only_once_per_gap():
    # 上轮已经追问过（stage=collect）还是没给地点和品类：不再问，按默认假设推荐并说明
    out = merge({"budget_max": 100}, stage="collect")
    assert out["need_clarify"] is False
    assert out["area_q"] == "海岸城"
    assert "我先这样假设" in out["profile_note"]


def test_delegate_skips_question():
    out = merge({"delegate": True})
    assert out["need_clarify"] is False and out["area_q"] == "海岸城"


def test_profile_fills_defaults_but_not_session():
    prof = {"area": "深圳湾", "budget": 150, "avoid": ["香菜"]}
    out = merge({"category": "火锅"}, prof_json=json.dumps(prof, ensure_ascii=False))
    eff = json.loads(out["slots_eff"])
    assert eff["area"] == "深圳湾" and eff["budget_max"] == 150 and "不吃香菜" in eff["taste"]
    # 画像只做默认值，不写回会话槽位
    assert "area" not in slots(out)
    srcs = {c["k"]: c["src"] for c in json.loads(out["conds_json"])}
    assert srcs["地点"] == "长期偏好" and srcs["品类"] == "本轮"


def test_turn_beats_profile():
    prof = {"area": "深圳湾", "budget": 150}
    out = merge({"area": "海岸城", "category": "火锅", "budget_max": 80}, prof_json=json.dumps(prof, ensure_ascii=False))
    eff = json.loads(out["slots_eff"])
    assert eff["area"] == "海岸城" and eff["budget_max"] == 80


def test_contradicted_profile_item_dropped_this_turn():
    # 10-21 问题 2：记忆抽取指出被推翻的画像条目（target_id），本轮起不再当默认值
    prof = {"avoid": ["辣"]}
    active = [{"id": 901, "key": "avoid", "value": "辣"}]
    mem = json.dumps({"prefs": [{"target_id": 901, "key": "taste", "value": "能吃辣"}]}, ensure_ascii=False)
    out = merge({"area": "海岸城", "category": "川菜"}, prof_json=json.dumps(prof, ensure_ascii=False),
                active_json=json.dumps(active, ensure_ascii=False), mem=mem)
    assert out["off_ids"] == "901"
    assert "不吃辣" not in json.loads(out["slots_eff"])["taste"]


def test_positive_taste_overrides_avoid_without_target_id():
    out = merge({"area": "海岸城", "category": "川菜", "taste": "能吃辣"}, prof_json=json.dumps({"avoid": ["辣"]}, ensure_ascii=False))
    assert "不吃辣" not in json.loads(out["slots_eff"])["taste"]


def test_scene_normalized_to_tag():
    assert merge({"area": "海岸城", "scene": "和女朋友约会"})["scene_tag"] == "约会"
    assert merge({"area": "海岸城", "scene": "带爸妈吃饭"})["scene_tag"] == "带老人"


def test_cross_shop_kb_only_when_needed():
    assert merge({"area": "海岸城", "category": "火锅"})["kb_on"] == "0"
    assert merge({"area": "海岸城", "category": "火锅", "need": "不用排队"})["kb_on"] == "1"
