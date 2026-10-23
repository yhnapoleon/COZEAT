"""跑 E2：每条查询调试运行一次探针工作流 la_e2_probe，存下 8 个检索节点的排序结果；打分在 e2_score.py。

查询有三类：
  ask / find    eval/data/e2_queries.jsonl 里的问店、找店
  prod          主干按店取证据的写法："品牌名 amap_id"（flow_code.CODE_TRIM），KB 里每家店一条
  prod_brand    只有品牌名，看 amap_id 在查询里起了多大作用

用法：python -I -X utf8 eval/e2_run.py [--run 名字] [-j 4] [--retry-failed]
输出：eval/runs/<run>/e2_results.jsonl、meta.json
"""
import argparse
import datetime
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bridge"))
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT))
from coze_client import Coze  # noqa: E402
from e1_run import secs, sha  # noqa: E402
from e2_build import PROBES, REC, load_corpus  # noqa: E402
from workflow_api import test_run  # noqa: E402

QFILE = ROOT / "eval" / "data" / "e2_queries.jsonl"


def queries() -> list[dict]:
    qs = [json.loads(x) for x in QFILE.read_text(encoding="utf-8").splitlines() if x.strip()]
    shops, _, by_note = load_corpus()
    aids = sorted({m["amap_id"] for ms in by_note.values() for m in ms})
    for i, a in enumerate(aids):
        brand = (shops[a]["name"] or "").split("(")[0].split("（")[0]
        qs.append({"id": f"p{i + 1:03d}", "type": "prod", "query": brand + " " + a, "gold_shop": a})
        qs.append({"id": f"b{i + 1:03d}", "type": "prod_brand", "query": brand, "gold_shop": a})
    return qs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default=datetime.datetime.now().strftime("e2-%m%d-%H%M"))
    ap.add_argument("-j", type=int, default=4)
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--types", default="", help="只跑这几类，逗号分隔：ask,find,prod,prod_brand")
    a = ap.parse_args()
    rec = json.loads(REC.read_text(encoding="utf-8"))
    qs = queries()
    if a.types:
        qs = [q for q in qs if q["type"] in a.types.split(",")]
    out_dir = ROOT / "eval" / "runs" / a.run
    out_dir.mkdir(parents=True, exist_ok=True)
    res_file = out_dir / "e2_results.jsonl"
    done = set()
    if res_file.exists():
        rows = [json.loads(x) for x in res_file.read_text(encoding="utf-8").splitlines() if x.strip()]
        if a.retry_failed:
            rows = [r for r in rows if r.get("status") == 2]
            res_file.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        done = {r["id"] for r in rows}
    todo = [q for q in qs if q["id"] not in done]
    meta_file = out_dir / "meta.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
    meta.update({"run": a.run, "probe": rec["probe"], "kbs": rec["kbs"], "slices": rec.get("slices"),
                 "probes": [list(p) for p in PROBES], "queries_sha": sha(QFILE.read_bytes()), "n": len(qs),
                 "started": meta.get("started") or datetime.datetime.now().isoformat(timespec="seconds")})
    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"run={a.run} 待跑 {len(todo)}/{len(qs)}", flush=True)

    c = Coze()
    c.login()
    lock, cnt = threading.Lock(), [0, 0]
    titles = {p[0] for p in PROBES}

    def one(q):
        try:
            d = test_run(c, rec["probe"], {"query": q["query"]}, timeout_s=90)
            nodes, node_s = {}, {}
            for n in d.get("nodeResults") or []:
                if n.get("NodeName") in titles:
                    o = json.loads(n.get("output") or "{}")
                    nodes[n["NodeName"]] = [[str(x.get("documentId")), x.get("output") or ""] for x in o.get("outputList") or []]
                    node_s[n["NodeName"]] = secs(n.get("nodeExeCost"))
            r = {"status": d.get("executeStatus"), "reason": d.get("reason") or "", "nodes": nodes, "node_s": node_s}
        except Exception as e:  # noqa: BLE001
            r = {"status": -1, "reason": f"{type(e).__name__}: {e}"[:300]}
        r.update({"id": q["id"], "type": q["type"], "query": q["query"]})
        with lock:
            with res_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            cnt[0 if r.get("status") == 2 else 1] += 1
            k = cnt[0] + cnt[1]
            if k % 25 == 0 or k == len(todo):
                print(f"PROGRESS {k}/{len(todo)} 成功 {cnt[0]} 失败 {cnt[1]}", flush=True)

    with ThreadPoolExecutor(a.j) as ex:
        list(ex.map(one, todo))
    meta["finished"] = datetime.datetime.now().isoformat(timespec="seconds")
    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print("DONE", res_file.relative_to(ROOT), flush=True)


if __name__ == "__main__":
    main()
