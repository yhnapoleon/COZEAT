"""E6 判分规则本身（eval/e6_run.py）：该判失败的时候判失败，归因到对的层。不跑对话、不调评委。"""
import json
import sys

from conftest import ROOT

sys.path.insert(0, str(ROOT / "eval"))
import e6_run  # noqa: E402

SHOPS = list(e6_run.S.values())
HOTPOT = next(s for s in SHOPS if "火锅" in str(s.get("std_cate")) and 0 < float(s["cost"] or 0) <= 100)
PRICEY = next(s for s in SHOPS if "火锅" in str(s.get("std_cate")) and float(s["cost"] or 0) > 150)
SUSHI = next(s for s in SHOPS if "日料" in str(s.get("std_cate")))
ANCHOR = (113.937635, 22.518308)  # 海岸城


def shop_answer(shops: list[dict], conds=None, text_names=None) -> str:
    items = [{"amap_id": s["amap_id"], "name": s["name"], "cost": s["cost"], "dist_m": 300} for s in shops]
    card = {"type": "shop_list", "anchor": "海岸城", "anchor_lng": ANCHOR[0], "anchor_lat": ANCHOR[1], "items": items,
            "explain": {"conds": conds or [], "funnel": {}}}
    names = text_names if text_names is not None else [s["name"] for s in shops]
    body = "在海岸城附近给你挑了：\n" + "\n".join(f"**{i + 1}. {n}**" for i, n in enumerate(names))
    return body + "\n```card\n" + json.dumps(card, ensure_ascii=False) + "\n```"


def failed(checks):
    return {c["check"]: c["layer"] for c in checks if not c["ok"]}


def test_parse_and_kind():
    text, cards = e6_run.parse(shop_answer([HOTPOT]))
    assert "```" not in text and e6_run.kind_of(cards) == "shop_list"
    assert e6_run.kind_of(e6_run.parse("纯文本回答")[1]) == "text"
    opt = "在哪一带？\n```card\n" + json.dumps({"type": "options", "items": ["a", "b"]}) + "\n```"
    assert e6_run.kind_of(e6_run.parse(opt)[1]) == "options"


def test_all_good():
    conds = [{"k": "地点", "v": "海岸城"}, {"k": "预算", "v": "人均 100 以内"}]
    t = {"kind": "shop_list", "conds": {"地点": "海岸城", "预算": "100"}, "shops": {"cate": "火锅", "budget": 100}}
    assert failed(e6_run.score_turn(t, shop_answer([HOTPOT], conds), 0, {}, [])) == {}


def test_wrong_kind_is_intent_layer():
    f = failed(e6_run.score_turn({"kind": "options"}, shop_answer([HOTPOT]), 0, {}, []))
    assert f == {"回答类型": "意图/澄清"}


def test_missing_cond_second_turn_is_context_layer():
    t = {"conds": {"预算": "100"}}
    f = failed(e6_run.score_turn(t, shop_answer([HOTPOT], [{"k": "地点", "v": "海岸城"}]), 1, {}, []))
    assert f == {"条件 预算~100": "上下文"}


def test_shop_violation_with_alternatives_is_recall_or_generation():
    t = {"shops": {"cate": "火锅", "budget": 100}}
    f = failed(e6_run.score_turn(t, shop_answer([PRICEY]), 0, {}, []))
    assert f == {"推荐的店满足条件": "召回/生成"}


def test_shop_violation_without_alternatives_is_data_coverage():
    t = {"shops": {"cate": "日料", "budget": 5}}
    f = failed(e6_run.score_turn(t, shop_answer([SUSHI]), 0, {}, []))
    assert f == {"推荐的店满足条件": "数据覆盖"}


def test_card_text_mismatch_is_format():
    f = failed(e6_run.score_turn({}, shop_answer([HOTPOT], text_names=["别的店"]), 0, {}, []))
    assert f == {"卡片里的店正文里都有": "格式"}


def test_route_reference():
    prev = json.loads(shop_answer([HOTPOT, SUSHI]).split("```card\n")[1].split("\n```")[0])
    route = "怎么去\n```card\n" + json.dumps({"type": "route", "dest": {"name": SUSHI["name"]}}, ensure_ascii=False) + "\n```"
    assert failed(e6_run.score_turn({"ref": [2]}, route, 1, prev, [])) == {}
    assert failed(e6_run.score_turn({"ref": [1]}, route, 1, prev, [])) == {"指代对上": "指代/会话状态"}


def test_not_prev():
    prev = json.loads(shop_answer([HOTPOT]).split("```card\n")[1].split("\n```")[0])
    assert failed(e6_run.score_turn({"not_prev": True}, shop_answer([HOTPOT]), 1, prev, [])) == {"和上一批不重复": "召回/生成"}


def test_not_ref():
    prev = json.loads(shop_answer([HOTPOT, SUSHI]).split("```card\n")[1].split("\n```")[0])
    assert failed(e6_run.score_turn({"not_ref": [2]}, shop_answer([HOTPOT]), 1, prev, [])) == {}
    assert failed(e6_run.score_turn({"not_ref": [2]}, shop_answer([SUSHI]), 1, prev, [])) == {"被拒的店没再出现": "召回/生成"}


def test_error_answer_is_stability():
    assert failed(e6_run.score_turn({"kind": "shop_list"}, "[出错] ReadTimeout: x", 0, {}, [])) == {"有回答": "稳定性"}


def test_cases_file_well_formed():
    cases = json.loads(e6_run.CASES.read_text(encoding="utf-8"))
    assert len(cases) >= 30 and len({c["id"] for c in cases}) == len(cases)
    keys = {"user", "kind", "conds", "conds_not", "shops", "ref", "not_prev", "not_ref", "text_has", "text_not", "judge", "near_m"}
    for c in cases:
        assert c["turns"] and c["cat"]
        for t in c["turns"]:
            assert t["user"] and set(t) <= keys, (c["id"], set(t) - keys)
