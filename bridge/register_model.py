"""把 LiteLLM 网关里的一个模型组注册成 Coze Studio 模型，打印它的 model_type（LLM 节点和意图节点用这个 id）。

Coze 只认 OpenAI 协议，所以 base_url 一律指向本机网关，api_key 用 LITELLM_MASTER_KEY；
创建前 Coze 会真调一次模型（COZE_API_NOTES：注册模型），网关里的模型组要先能调通。

用法：
  python -I -X utf8 bridge/register_model.py list
  python -I -X utf8 bridge/register_model.py add deepseek-flash "DeepSeek V4.1 Flash（经 LiteLLM 网关）"
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
from coze_client import Coze  # noqa: E402
from common.env import need  # noqa: E402

GATEWAY = "http://host.docker.internal:4000/v1"


def models(c: Coze) -> list[dict]:
    body = c.post("/api/bot/get_type_list", {"model": True})
    return [{"model_type": m.get("model_type"), "name": m.get("name")} for m in body["data"].get("model_list") or []]


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    c = Coze()
    c.login()
    if cmd == "add":
        group, display = sys.argv[2], sys.argv[3]
        if any(m["name"] == display for m in models(c)):
            print("已注册过：", display)
        else:
            r = c.post("/api/admin/config/model/create", {
                "model_class": 1, "model_name": display,
                "connection": {"base_conn_info": {"base_url": GATEWAY, "api_key": need("LITELLM_MASTER_KEY"),
                                                  "model": group, "thinking_type": 0},
                               "openai": {"by_azure": False}},
                "enable_base64_url": False})
            print("注册:", r.get("code"), r.get("msg") or "")
    for m in models(c):
        print(m["model_type"], m["name"])


if __name__ == "__main__":
    main()
