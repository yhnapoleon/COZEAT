"""跨店召回评测：E2 的 60 条"找店"查询（eval/data/e2_queries.jsonl，type=find，每条标了答案店），
每条先跑一次 WF-understand 草稿，再用它的输出跑两遍 WF-recall 草稿：跨店检索开（kb_on 按理解结果）和关（强制 kb_on=0），
看答案店是否进了裁剪后的 12 家（cands_all）和交给推荐生成的 8 家（candidates_json），以及在 8 家里的名次。

偏差：这批查询是 E2 时从答案店的评价段生成的，用词和评价原文接近，跨店检索的效果会偏乐观。
答案店离锚点超过 5 公里（SQL 硬条件）或不在门店库里的单独记，不算进分母。

用法：python -I -X utf8 eval/kb_cross_eval.py [--run 名字] [-j 4] [--only f001,f002]
输出：eval/runs/<run>/kbx_results.jsonl、eval/reports/KBX_<run>.md
"""
import argparse
import datetime
import json
import math
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bridge"))
sys.path.insert(0, str(ROOT / "eval"))
from coze_client import Coze  # noqa: E402
from recall_probe import recall, understand  # noqa: E402


def dist_m(a, b) -> float:
    R = 6371000
    p1, p2 = math.radians(a[1]), math.radians(b[1])
    dp, dl = p2 - p1, math.radians(b[0] - a[0])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def rank_of(js: str, aid: str) -> int:
    for i, x in enumerate(json.loads(js or "[]")):
        if x.get("amap_id") == aid:
            return i + 1
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default=datetime.datetime.now().strftime("kbx-%m%d-%H%M"))
    ap.add_argument("-j", type=int, default=4)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    qs = [json.loads(x) for x in (ROOT / "eval/data/e2_queries.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    qs = [q for q in qs if q["type"] == "find" and (not a.only or q["id"] in a.only.split(","))]
    shops = {json.loads(x)["amap_id"]: json.loads(x) for x in (ROOT / "data/processed/shops.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()}
    ids = json.loads((ROOT / "lifeagent_ids.json").read_text(encoding="utf-8"))
    c = Coze()
    c.login()  # 共用一次登录：同账号再登录会把前一个 session 顶掉
    out_dir = ROOT / "eval" / "runs" / a.run
    out_dir.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    res = []

    def one(q):
        r = {"id": q["id"], "query": q["query"], "gold": q["gold_shop"], "gold_name": q["shop_name"]}
        try:
            u = understand(c, ids, q["query"])
            r.update({"slots": u.get("slots_json"), "kb_q": u.get("kb_q"), "kb_on": u.get("kb_on"), "route": u.get("route")})
            for tag, no_kb in (("on", False), ("off", True)):
                o, nodes = recall(c, ids, u, no_kb)
                geo = {}
                n = nodes.get("解析坐标")
                if n:
                    try:
                        geo = json.loads(n.get("output") or "{}")
                    except ValueError:
                        geo = {}
                r[tag] = {"r12": rank_of(o.get("cands_all"), q["gold_shop"]), "r8": rank_of(o.get("candidates_json"), q["gold_shop"]),
                          "anchor": o.get("anchor"), "lng": geo.get("lng"), "lat": geo.get("lat"), "relaxed": o.get("relaxed")}
            g = shops.get(q["gold_shop"])
            on = r["on"]
            if not g:
                r["skip"] = "答案店不在库里"
            elif on.get("lng") and dist_m((float(g["lng"]), float(g["lat"])), (float(on["lng"]), float(on["lat"]))) > 5000:
                r["skip"] = "答案店离锚点超过 5 公里"
        except Exception as e:  # noqa: BLE001
            r["error"] = f"{type(e).__name__}: {e}"
        with lock:
            res.append(r)
            print("PROGRESS", len(res), "/", len(qs), q["id"], "on", (r.get("on") or {}).get("r8"), "off", (r.get("off") or {}).get("r8"),
                  r.get("skip") or r.get("error") or "", flush=True)
        return r

    with ThreadPoolExecutor(a.j) as ex:
        list(ex.map(one, qs))
    res.sort(key=lambda r: r["id"])
    (out_dir / "kbx_results.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in res), encoding="utf-8")

    ok = [r for r in res if "on" in r and "off" in r and not r.get("skip")]
    kb = [r for r in ok if str(r.get("kb_on")) == "1"]

    def rate(rs, tag, k):
        return sum(1 for r in rs if 0 < r[tag][k]) , len(rs)

    L = [f"# 跨店召回评测：{a.run}\n",
         f"- 查询：E2 找店 {len(qs)} 条；可评 {len(ok)} 条（跳过：答案店不在库里或离锚点超过 5 公里 {sum(1 for r in res if r.get('skip'))} 条，出错 {sum(1 for r in res if r.get('error'))} 条）",
         f"- 开跨店检索的（理解出了 need 或场景）{len(kb)} 条；其余的开关两遍结果相同",
         "- 偏差：查询是从答案店的评价段生成的，用词贴近原文，跨店检索效果偏乐观\n",
         "| 子集 | 条数 | 关：进 12 家 | 开：进 12 家 | 关：进 8 家 | 开：进 8 家 | 关：8 家里第 1 | 开：8 家里第 1 |",
         "|---|---|---|---|---|---|---|---|"]
    for name, rs in (("全部可评", ok), ("开了跨店检索的", kb)):
        if not rs:
            continue
        f12, n = rate(rs, "off", "r12")
        t12, _ = rate(rs, "on", "r12")
        f8, _ = rate(rs, "off", "r8")
        t8, _ = rate(rs, "on", "r8")
        f1 = sum(1 for r in rs if r["off"]["r8"] == 1)
        t1 = sum(1 for r in rs if r["on"]["r8"] == 1)
        L.append(f"| {name} | {n} | {f12} | {t12} | {f8} | {t8} | {f1} | {t1} |")
    L.append("\n## 逐条\n")
    L.append("| id | 查询 | 答案店 | kb_q | 关 r12/r8 | 开 r12/r8 | 备注 |")
    L.append("|---|---|---|---|---|---|---|")
    for r in res:
        on, off = r.get("on") or {}, r.get("off") or {}
        L.append(f"| {r['id']} | {r['query']} | {r['gold_name'][:14]} | {r.get('kb_q') or ''} | {off.get('r12', '')}/{off.get('r8', '')} | "
                 f"{on.get('r12', '')}/{on.get('r8', '')} | {r.get('skip') or r.get('error') or ''} |")
    rep = ROOT / "eval" / "reports" / f"KBX_{a.run}.md"
    rep.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("报告", rep)


if __name__ == "__main__":
    main()
