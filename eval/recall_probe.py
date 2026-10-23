"""调试用：拿一句话跑 WF-understand 草稿，再用它的输出跑 WF-recall 草稿，打印召回各阶段的店（裁剪后 12 家、交给推荐生成的 8 家、跨店检索命中）。

用法：python -I -X utf8 eval/recall_probe.py "深圳湾万象城约会，想吃西餐" [--no-kb]
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bridge"))
from coze_client import Coze  # noqa: E402
from workflow_api import test_run  # noqa: E402

UNDERSTAND_IN = {"prev_slots": "{}", "shown": "[]", "shown_text": "（无）", "prof_lines": "（无）", "prof_json": "{}",
                 "active_json": "[]", "pending": "0", "conv": "probe", "turn": "0", "stage": "", "pending_q": ""}


def outputs(d: dict) -> dict:
    for n in d.get("nodeResults") or []:
        if n.get("NodeType") in ("End", "Exit") or n.get("NodeName") in ("结束", "End"):
            try:
                return json.loads(n.get("output") or "{}")
            except ValueError:
                return {}
    return {}


def understand(c: Coze, ids: dict, query: str) -> dict:
    return outputs(test_run(c, ids["workflows"]["understand"], {"query": query, **UNDERSTAND_IN}, bot_id=ids["bot_id"], timeout_s=120))


def recall(c: Coze, ids: dict, u: dict, no_kb: bool = False) -> tuple[dict, dict]:
    from build_subflows import RECALL_IN
    inp = {k: str(u.get(k) if u.get(k) is not None else "") for k in RECALL_IN}
    if no_kb:
        inp["kb_on"], inp["kb_q"] = "0", "无"
    d = test_run(c, ids["workflows"]["recall"], inp, bot_id=ids["bot_id"], timeout_s=120)
    nodes = {n.get("NodeName"): n for n in d.get("nodeResults") or []}
    return outputs(d), nodes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--no-kb", action="store_true")
    a = ap.parse_args()
    ids = json.loads((ROOT / "lifeagent_ids.json").read_text(encoding="utf-8"))
    c = Coze()
    c.login()
    u = understand(c, ids, a.query)
    print("槽位", u.get("slots_json"), "| kb_q", u.get("kb_q"), "| kb_on", u.get("kb_on"))
    o, nodes = recall(c, ids, u, a.no_kb)
    kc = nodes.get("跨店收敛")
    if kc:
        try:
            kj = json.loads(json.loads(kc.get("output") or "{}").get("kb_json") or "[]")
            print("跨店命中", [(x["rank"], x["amap_id"]) for x in kj])
        except ValueError:
            pass
    shops = {json.loads(l)["amap_id"]: json.loads(l)["name"] for l in (ROOT / "data/processed/shops.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()}
    for k in ("cands_all", "candidates_json"):
        xs = json.loads(o.get(k) or "[]")
        print(k, len(xs))
        for x in xs:
            print("  ", (x.get("name") or "")[:16], x.get("cate"), x.get("dist_m"), x.get("cost"), "tags=" + str(x.get("tags")),
                  "scene_ok=" + str(x.get("scene_ok", "-")), "need_hit=" + str(x.get("need_hit", "-")))
    print("放宽", o.get("relaxed"))


if __name__ == "__main__":
    main()
