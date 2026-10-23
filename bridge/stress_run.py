"""连跑 CF-main 调试 24 轮，统计执行状态没到 2（成功）的次数。用来查对话流挂住（见 COZE_API_NOTES 运行时行为）。

用法：python -I -X utf8 bridge/stress_run.py
"""
import json, sys, time
from pathlib import Path
import os
os.chdir(Path(__file__).resolve().parent.parent)
sys.path.insert(0, "bridge"); sys.path.insert(0, ".")
from coze_client import Coze
from workflow_api import test_run
c = Coze(); c.login()
ids = json.loads(open("lifeagent_ids.json", encoding="utf-8").read())
qs = ["你记得我哪些偏好", "想吃辣的", "你好呀", "团购券过期了能退吗", "记住我不吃香菜"] * 4 + \
     ["海岸城附近人均100以内的火锅", "海岸城附近清淡点的", "附近有啥好吃的", "海岸城附近两个人约会"]
bad = 0
for i, q in enumerate(qs):
    t0 = time.time()
    d = test_run(c, ids["workflows"]["cf_main"], {"USER_INPUT": q, "CONVERSATION_NAME": f"stress-{i}"}, bot_id=ids["bot_id"], timeout_s=120)
    st = d.get("executeStatus")
    bad += st != 2
    print(f"{i:02d} status={st} {time.time()-t0:5.1f}s {q}", flush=True)
print(f"DONE runs={len(qs)} not_ok={bad}", flush=True)
