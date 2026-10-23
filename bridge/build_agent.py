"""智能体：创建、绑表、配置人设与模型；对话流发布后切到对话流模式。

用法：
  python -I -X utf8 bridge/build_agent.py create     # 建智能体并绑 3 张表
  python -I -X utf8 bridge/build_agent.py chatflow   # 切到对话流模式，主干绑 CF-main（需先发布 CF-main）
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from coze_client import Coze  # noqa: E402

IDS_FILE = HERE.parent / "lifeagent_ids.json"
sys.path.insert(0, str(HERE.parent))
from flow_code import MODEL_TYPE as _MT  # noqa: E402  智能体模型和工作流节点用同一个（bridge/flow_code.py）
MODEL_TYPE = str(_MT)

PROLOGUE = "你好，我是深圳本地生活推荐助手。告诉我你在哪、想吃什么、几个人、预算多少，我帮你挑店和团购。"
SUGGESTED = ["海岸城附近人均 100 以内的火锅", "两个人约会，南山有什么氛围好的西餐", "带老人吃饭，清淡一点的粤菜"]


def load_ids():
    return json.loads(IDS_FILE.read_text(encoding="utf-8"))


def save_ids(ids):
    IDS_FILE.write_text(json.dumps(ids, ensure_ascii=False, indent=1), encoding="utf-8")


def create(c: Coze, ids: dict):
    if not ids.get("bot_id"):
        body = c.post("/api/draftbot/create", {"space_id": c.space_id(), "name": "抖音本地生活推荐",
                                               "description": "深圳餐饮推荐：找餐厅、选团购、对比评价、路线规划",
                                               "icon_uri": "default_icon/default_agent_icon.png"})
        ids["bot_id"] = str(body["data"]["bot_id"])
        save_ids(ids)
        print("建智能体", ids["bot_id"])
    bot_id = ids["bot_id"]
    for name, draft_id in ids["tables_draft"].items():
        try:
            c.post("/api/memory/database/bind_to_bot", {"database_id": draft_id, "bot_id": bot_id})
            print("绑表", name)
        except RuntimeError as e:
            print("绑表", name, "跳过：", str(e)[:120])
    cur = c.post("/api/playground_api/draftbot/get_draft_bot_info", {"bot_id": bot_id})["data"]["bot_info"]
    model_info = dict(cur.get("model_info") or {})
    model_info.update({"model_id": MODEL_TYPE, "max_tokens": 8192,
                       "short_memory_policy": {"context_mode": 2, "history_round": 2}})
    c.post("/api/playground_api/draftbot/update_draft_bot_info", {"bot_info": {
        "bot_id": bot_id, "model_info": model_info,
        "onboarding_info": {"prologue": PROLOGUE, "suggested_questions": SUGGESTED}}})
    after = c.post("/api/playground_api/draftbot/get_draft_bot_info", {"bot_id": bot_id})["data"]["bot_info"]
    print("已绑定的表:", [d.get("table_name") for d in after.get("database_list") or []])


def chatflow(c: Coze, ids: dict):
    wid = ids["workflows"]["cf_main"]
    c.post("/api/playground_api/draftbot/update_draft_bot_info", {"bot_info": {
        "bot_id": ids["bot_id"], "bot_mode": 2, "layout_info": {"workflow_id": wid, "plugin_id": wid}}})
    after = c.post("/api/playground_api/draftbot/get_draft_bot_info", {"bot_id": ids["bot_id"]})["data"]["bot_info"]
    print("bot_mode:", after.get("bot_mode"), "layout:", after.get("layout_info"))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "create"
    cli = Coze()
    cli.login()
    {"create": create, "chatflow": chatflow}[cmd](cli, load_ids())
