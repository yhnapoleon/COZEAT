"""WF-recall 的裁剪候选（CODE_TRIM）、合并证据（CODE_EVID_MERGE）和主干认店（CODE_PICK）。"""
import json

from conftest import flow_code, run_node


def row(i: int, dist: int = 300, **kw) -> dict:
    r = {"amap_id": "B" + str(i), "name": "店" + str(i), "cate": "火锅", "std_cate": "|火锅|川渝火锅|", "biz_area": "后海",
         "rating": "4.5", "cost": "90", "dishes": "", "tags": "", "note_cnt": "2", "deals": "[]", "lng": "113.9", "lat": "22.5",
         "address": "", "dist_m": str(dist), "kw_hit": "1", "cost_ok": "1", "kb_pos": "0"}
    r.update(kw)
    return r


def trim(rows, **kw):
    params = {"rows": rows, "scene": "", "kw": "火锅", "taste": "", "dist_max": "0", "budget": "99999", "bmin": "0", "kb_on": "0"}
    params.update(kw)
    out = run_node(flow_code.CODE_TRIM, **params)
    return [c["amap_id"] for c in json.loads(out["candidates_json"])], json.loads(out["funnel"])


def test_trim_excludes_rejected():
    ids, funnel = trim([row(i) for i in range(1, 6)], excl="B1,B2")
    assert "B1" not in ids and "B2" not in ids and funnel["excl"] == 2


def test_trim_keeps_all_when_everything_rejected():
    ids, _ = trim([row(1), row(2)], excl="B1,B2")
    assert set(ids) == {"B1", "B2"}


def test_trim_no_excl_param():
    ids, funnel = trim([row(1), row(2)])
    assert set(ids) == {"B1", "B2"} and "excl" not in funnel


def evid_merge(cands, scene=""):
    out = run_node(flow_code.CODE_EVID_MERGE, candidates_json=json.dumps(cands, ensure_ascii=False), evid=[], kb_json="[]",
                   slots_json=json.dumps({"scene": scene}, ensure_ascii=False), scene_tag=scene, funnel="{}")
    return [c["amap_id"] for c in json.loads(out["candidates_json"])], json.loads(out["funnel"])


def test_far_shops_dropped_when_enough_near():
    cands = [{"amap_id": "F1", "dist_m": 2800, "tags": ""}] + [{"amap_id": "N" + str(i), "dist_m": 500 + i, "tags": ""} for i in range(3)]
    ids, funnel = evid_merge(cands)
    assert ids == ["N0", "N1", "N2"] and funnel["far"] == 1


def test_scene_shops_allowed_up_to_3500m():
    # 商务宴请：3 公里外有场景标签的店留着（可以打车）；4 公里外的、没场景依据又超过 2 公里的不给
    cands = [{"amap_id": "S1", "dist_m": 3100, "tags": "商务"}, {"amap_id": "S2", "dist_m": 4200, "tags": "商务"},
             {"amap_id": "F1", "dist_m": 2500, "tags": ""}] + [{"amap_id": "N" + str(i), "dist_m": 500 + i, "tags": ""} for i in range(3)]
    ids, funnel = evid_merge(cands, scene="商务")
    assert ids[0] == "S1" and set(ids) == {"S1", "N0", "N1", "N2"} and funnel["far"] == 2


def test_far_shops_kept_when_near_not_enough():
    cands = [{"amap_id": "F1", "dist_m": 3100, "tags": ""}, {"amap_id": "N1", "dist_m": 500, "tags": ""}]
    ids, _ = evid_merge(cands)
    assert set(ids) == {"F1", "N1"}


def pick(q, route="cmp", shown=None, rows=None):
    rows = rows or [{"amap_id": "B1", "name": "八合里潮汕鲜牛肉火锅(深圳海岸城店)"}, {"amap_id": "B2", "name": "芈重山老火锅(南山保利店)"}]
    return run_node(flow_code.CODE_PICK, query=q, route=route, shown=json.dumps(shown or [], ensure_ascii=False), rows=rows,
                    prev_slots="{}", prof_json="{}")


def test_pick_by_name():
    out = pick("芈重山和八合里哪个好")
    assert out["go"] == "cmp" and set(out["pick_ids"].split(",")) == {"B1", "B2"}


def test_pick_unknown_shop_says_not_found():
    # 10-24 E6 e05/e13：库里没有的店不回"想比较哪几家"
    for q, nm in [("海底捞海岸城店怎么样", "海底捞海岸城店"), ("太二酸菜鱼怎么样", "太二酸菜鱼")]:
        out = pick(q)
        assert out["go"] == "ask" and "没找到「" + nm + "」" in out["ask"]


def test_pick_ordinal_without_shown_keeps_generic_ask():
    out = pick("第一家和第三家哪个好")
    assert out["go"] == "ask" and "没找到" not in out["ask"]
