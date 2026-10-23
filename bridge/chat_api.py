"""经 Coze OpenAPI 和智能体对话（网页 Demo 后面也走这条路）。PAT 首次运行时创建并写进 infra/.env。

用法：python -I -X utf8 bridge/chat_api.py "海岸城附近的西餐" [user_id] [conversation_name]
"""
import json
import secrets
import sys
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
from coze_client import BASE, Coze  # noqa: E402
from common.env import ENV_FILE, load_env  # noqa: E402

IDS_FILE = ROOT / "lifeagent_ids.json"


def ensure_pat() -> str:
    env = load_env()
    if env.get("COZE_PAT"):
        return env["COZE_PAT"]
    c = Coze()
    c.login()
    body = c.post("/api/permission_api/pat/create_personal_access_token_and_permission",
                  {"name": f"lifeagent-{secrets.token_hex(2)}", "duration_day": "30"})
    token = body["data"]["token"]
    with ENV_FILE.open("a", encoding="utf-8") as f:
        f.write(f"COZE_PAT={token}\n")
    print("已创建 PAT 并写入 infra/.env（30 天有效）")
    return token


def chat(text: str, user_id: str = "demo-user", conv_name: str = "Default") -> str:
    ids = json.loads(IDS_FILE.read_text(encoding="utf-8"))
    body = {"workflow_id": ids["workflows"]["cf_main"], "bot_id": ids["bot_id"],
            "additional_messages": [{"role": "user", "content": text, "content_type": "text"}],
            "parameters": json.dumps({"CONVERSATION_NAME": conv_name}, ensure_ascii=False),
            "ext": {"user_id": user_id}}
    r = requests.post(f"{BASE}/v1/workflows/chat", json=body, stream=True, timeout=300,
                      headers={"Authorization": f"Bearer {ensure_pat()}", "Content-Type": "application/json"})
    answer, event = [], ""
    for raw in r.iter_lines(decode_unicode=True):
        if not raw:
            continue
        if raw.startswith("event:"):
            event = raw[6:].strip()
        elif raw.startswith("data:"):
            data = raw[5:].strip()
            if event == "conversation.message.delta":
                try:
                    answer.append(json.loads(data).get("content") or "")
                except ValueError:
                    pass
            elif event in ("error", "conversation.chat.failed"):
                print("错误事件:", data[:300])
    return "".join(answer)


if __name__ == "__main__":
    args = sys.argv[1:] + [None, None]
    print(chat(args[0], args[1] or "demo-user", args[2] or "Default"))
