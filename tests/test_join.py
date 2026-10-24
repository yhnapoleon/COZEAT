"""理解阶段的汇合（CODE_JOIN）：reject 反馈 → 召回要排除的店（excl_ids）。/decide 返回用 10-24 E6 c09 实录的格式。"""
import json

from conftest import flow_code, run_node

SHOWN = [{"amap_id": "A1", "name": "寿司郎(海岸城店)"}, {"amap_id": "A2", "name": "摩打食堂"}, {"amap_id": "A3", "name": "大渔铁板烧"}]


def join(feedback: str, target: str, intent: str = "rec", query: str = "这几家都不想去，有没有别的") -> dict:
    ans = {"intent": {"choice": intent, "probabilities": {intent: 0.97}, "confidence": 0.97}, "unclear": {"noul": 0.02},
           "feedback": {"choice": feedback, "probabilities": {feedback: 0.98}, "confidence": 0.98},
           "target": {"choice": target, "probabilities": {target: 0.98}, "confidence": 0.98}}
    return run_node(flow_code.CODE_JOIN, cid=1, mem="", dec=json.dumps({"answers": ans}), shown=json.dumps(SHOWN, ensure_ascii=False),
                    active_json="[]", pending=0, conv="t", query=query, stage="recommend", slot_raw="{}")


def test_reject_all_excludes_whole_batch():
    out = join("reject", "0")
    assert out["route"] == "rec" and out["excl_ids"] == "A1,A2,A3"


def test_reject_one_excludes_that_shop():
    assert join("reject", "2", query="第二家不想去，还有别的吗")["excl_ids"] == "A2"


def test_neutral_excludes_nothing():
    assert join("neutral", "0", query="人均100以内的")["excl_ids"] == ""


def test_chat_after_slot_question_goes_to_rec():
    # /decide 超时退回 Coze 意图节点（cid 8 闲聊），上一轮刚追问过条件：按找店走（10-24，E6 b03）
    out = run_node(flow_code.CODE_JOIN, cid=8, mem="", dec="{}", shown="[]", active_json="[]", pending=0, conv="t",
                   query="嗯，还没想好", stage="collect", slot_raw="{}")
    assert out["route"] == "rec"
    out = run_node(flow_code.CODE_JOIN, cid=8, mem="", dec="{}", shown="[]", active_json="[]", pending=0, conv="t",
                   query="今天天气怎么样", stage="", slot_raw="{}")
    assert out["route"] == "other"
