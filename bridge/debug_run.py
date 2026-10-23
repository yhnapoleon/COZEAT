"""调试运行 CF-main（读写草稿表，用户是当前账号），打印决策层结果、分流、出错节点、各节点耗时和最终回答。

用法：python -I -X utf8 bridge/debug_run.py "问题" 会话名
"""
import json, sys
from pathlib import Path
import os
os.chdir(Path(__file__).resolve().parent.parent)
sys.path.insert(0, "bridge"); sys.path.insert(0, ".")
from coze_client import Coze
from workflow_api import test_run
c = Coze(); c.login()
ids = json.loads(open("lifeagent_ids.json", encoding="utf-8").read())
d = test_run(c, ids["workflows"]["cf_main"], {"USER_INPUT": sys.argv[1], "CONVERSATION_NAME": sys.argv[2]}, bot_id=ids["bot_id"], timeout_s=150)
print("status", d.get("executeStatus"), "cost", d.get("workflowExeCost"), d.get("reason") or "")
times = []
for n in d.get("nodeResults") or []:
    nm = n.get("NodeName")
    times.append(f"{nm} {n.get('nodeExeCost') or ''}")
    if nm == "理解(WF-understand)":
        try:
            o = json.loads(n.get("output") or "{}")
            print("== 理解", o.get("dec_note"), "| route", o.get("route"), "| 槽位", o.get("slots_json"),
                  "| 记忆写入", len(o.get("ins_items") or []), "删除", len(o.get("del_items") or []))
            print("   画像说明", o.get("profile_note") or "-", "| 本轮屏蔽", o.get("off_ids") or "-",
                  "| 生效画像", o.get("profile_eff") or "-")
            for x in o.get("ins_items") or []:
                print("   +", x[:200])
        except ValueError:
            print("== 理解", (n.get("output") or "")[:500])
    elif nm in ("认店",) or n.get("errorInfo"):
        print("==", nm, n.get("errorInfo") or "", "|", (n.get("output") or "")[:500])
print("== 耗时", "；".join(t for t in times if t.split()[-1] not in ("", "0s")))
for n in d.get("nodeResults") or []:
    if n.get("NodeName") == "结束":
        print("== 回答\n" + (n.get("output") or ""))
