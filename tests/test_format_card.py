"""CODE_FORMAT（推荐回答 + shop_list 卡片）：正文和卡片一致、空理由条目丢弃、口碑引文必须出自证据、兜底。"""
import json

from conftest import card_of, flow_code, run_node


def cand(i: int, **kw) -> dict:
    c = {"amap_id": "B" + str(i), "name": "店" + str(i), "cate": "火锅", "rating": 4.5, "cost": 90, "dist_m": 100 * i,
         "lng": 113.9, "lat": 22.5, "evidence": [{"quote": "牛肉很新鲜，不用排队", "facts": ""}], "deals": []}
    c.update(kw)
    return c


def fmt(cands: list[dict], picks: list[dict] | str, **kw) -> dict:
    params = {"anchor": "海岸城", "candidates_json": json.dumps(cands, ensure_ascii=False),
              "picks": picks if isinstance(picks, str) else json.dumps(picks, ensure_ascii=False),
              "conds_json": "[]", "funnel": "{}", "anchor_lng": "113.93", "anchor_lat": "22.51"}
    params.update(kw)
    return run_node(flow_code.CODE_FORMAT, **params)


def test_text_and_card_agree():
    cands = [cand(1), cand(2), cand(3)]
    picks = [{"amap_id": "B2", "reason": "近", "matched": ["火锅"]}, {"amap_id": "B1", "reason": "便宜"}]
    out = fmt(cands, picks)
    card = card_of(out["answer"])
    assert card["type"] == "shop_list" and card["anchor"] == "海岸城"
    names = [x["name"] for x in card["items"]]
    assert names == ["店2", "店1"]
    text = out["answer"].split("```card")[0]
    assert "挑了 2 家" in text
    for i, n in enumerate(names, 1):
        assert "**" + str(i) + ". " + n + "**" in text
    assert [x["name"] for x in json.loads(out["shown_json"])] == names


def test_empty_reason_and_unknown_id_dropped():
    picks = [{"amap_id": "B1", "reason": "好"}, {"amap_id": "B2", "reason": ""}, {"amap_id": "X9", "reason": "编的店"}]
    card = card_of(fmt([cand(1), cand(2)], picks)["answer"])
    assert [x["amap_id"] for x in card["items"]] == ["B1"]


def test_at_most_three():
    cands = [cand(i) for i in range(1, 6)]
    picks = [{"amap_id": "B" + str(i), "reason": "r"} for i in range(1, 6)]
    assert len(card_of(fmt(cands, picks)["answer"])["items"]) == 3


def test_quote_must_come_from_evidence():
    picks = [{"amap_id": "B1", "reason": "r", "quote": "牛肉很新鲜，不用排队"}, {"amap_id": "B2", "reason": "r", "quote": "全深圳最好吃"}]
    text = fmt([cand(1), cand(2)], picks)["answer"]
    assert "口碑：“牛肉很新鲜，不用排队”" in text
    assert "全深圳最好吃" not in text


def test_scene_ok_shops_first():
    cands = [cand(1, scene_ok=False), cand(2, scene_ok=True)]
    picks = [{"amap_id": "B1", "reason": "r"}, {"amap_id": "B2", "reason": "r"}]
    assert [x["amap_id"] for x in card_of(fmt(cands, picks)["answer"])["items"]] == ["B2", "B1"]


def test_unknown_cost_not_shown_as_zero():
    text = fmt([cand(1, cost=0)], [{"amap_id": "B1", "reason": "r"}])["answer"]
    assert "人均未知" in text and "¥0" not in text


def test_synthetic_deals_hidden_by_default():
    deals = [{"name": "双人餐", "price": 168, "synthetic": True}]
    picks = [{"amap_id": "B1", "reason": "r"}]
    assert "双人餐" not in fmt([cand(1, deals=deals)], picks)["answer"]
    assert "示例数据，非真实优惠" in fmt([cand(1, deals=deals)], picks, cid="2")["answer"]


def test_no_valid_pick_falls_back_to_first_candidate():
    out = fmt([cand(1), cand(2)], "模型没按格式输出")
    card = card_of(out["answer"])
    assert [x["amap_id"] for x in card["items"]] == ["B1"]
    assert "最接近" in out["answer"]


def test_no_candidates():
    out = fmt([], [])
    assert "没找到符合条件的店" in out["answer"]
    assert card_of(out["answer"])["items"] == []


def test_profile_note_and_relaxed_prefixed():
    out = fmt([cand(1)], [{"amap_id": "B1", "reason": "r"}], profile_note="按你的长期偏好：常去深圳湾", relaxed="符合全部条件的不多，放宽了距离")
    head = out["answer"].split("在海岸城附近")[0]
    assert "放宽了距离" in head and "常去深圳湾" in head
