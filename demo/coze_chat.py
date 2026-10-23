"""网页 Demo 和录制脚本共用：调 Coze 对话接口（/v1/workflows/chat），把回答拆成 markdown + 卡片 + 选项。

回答格式见 HANDOVER 0.2：markdown 正文，末尾可能有一段或几段 ```card JSON（shop_list / compare / route / deals / options）。
主干结束节点不流式，整段回答在最后一次 delta 里到，所以这里收完再返回，不把 SSE 转给浏览器。
"""
import json
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bridge"))
from coze_client import BASE  # noqa: E402
from common.env import load_env  # noqa: E402

IDS = json.loads((ROOT / "lifeagent_ids.json").read_text(encoding="utf-8"))
CARD_RE = re.compile(r"```card\s*\n(.*?)\n```", re.S)


def parse(answer: str) -> dict:
    cards = []
    for m in CARD_RE.finditer(answer):
        try:
            cards.append(json.loads(m.group(1)))
        except ValueError:
            pass
    options = [str(x) for c in cards if c.get("type") == "options" for x in c.get("items") or []]
    md = CARD_RE.sub("", answer).strip()
    return {"markdown": md, "cards": [c for c in cards if c.get("type") != "options"], "options": options}


def chat(text: str, user_id: str, conv: str, timeout: int = 180) -> dict:
    """一轮对话。返回 parse() 的结果加 elapsed_s；出错时带 error。"""
    pat = load_env().get("COZE_PAT")
    if not pat:
        return {"markdown": "", "cards": [], "options": [], "error": "infra/.env 里没有 COZE_PAT", "elapsed_s": 0}
    body = {"workflow_id": IDS["workflows"]["cf_main"], "bot_id": IDS["bot_id"],
            "additional_messages": [{"role": "user", "content": text, "content_type": "text"}],
            "parameters": json.dumps({"CONVERSATION_NAME": conv}, ensure_ascii=False),
            "ext": {"user_id": user_id}}
    t0 = time.time()
    parts, event, err = [], "", ""
    try:
        r = requests.post(f"{BASE}/v1/workflows/chat", json=body, stream=True, timeout=timeout,
                          headers={"Authorization": f"Bearer {pat}", "Content-Type": "application/json"})
        for raw in r.iter_lines(decode_unicode=True):
            if not raw:
                continue
            if raw.startswith("event:"):
                event = raw[6:].strip()
            elif raw.startswith("data:"):
                data = raw[5:].strip()
                if event == "conversation.message.delta":
                    try:
                        parts.append(json.loads(data).get("content") or "")
                    except ValueError:
                        pass
                elif event in ("error", "conversation.chat.failed"):
                    err = data[:300]
    except requests.RequestException as e:
        err = type(e).__name__
    out = parse("".join(parts))
    out["elapsed_s"] = round(time.time() - t0, 1)
    if err or not out["markdown"]:
        out["error"] = err or "没有收到回答"
    return out
