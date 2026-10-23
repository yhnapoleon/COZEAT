"""跑 E1：每条用例调试运行一次 la_understand（WF-understand），存原始结果；打分在 e1_score.py。

调试运行读草稿。开跑前核对三件事写进 meta.json：草稿和发布版是不是同一次提交、草稿和 canvases/wf_understand.json
是否一致、画布里实际用的是 prompts/ 下哪几个文件。WF-understand 不写库（写库都在 WF-memwrite），可以随便跑；
/decide 的请求和结果照常记进 sidecar 的 decide_log。

用法：python -I -X utf8 eval/e1_run.py [--run 名字] [--only s01,g002] [--split test] [-j 4]
输出：eval/runs/<run>/e1_results.jsonl（续跑时已有的 id 跳过）、meta.json
"""
import argparse
import datetime
import hashlib
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bridge"))
sys.path.insert(0, str(ROOT))
import flow_code as fc  # noqa: E402
from coze_client import Coze  # noqa: E402
from workflow_api import test_run  # noqa: E402
from common.env import need  # noqa: E402
from etl.smoke import call  # noqa: E402

DATA = ROOT / "eval" / "data" / "e1.jsonl"
IDS = json.loads((ROOT / "lifeagent_ids.json").read_text(encoding="utf-8"))
WF = IDS["workflows"]["understand"]
NODES = {"决策(sidecar)": "dec", "意图识别": "intent", "记忆抽取": "mem", "槽位提取": "slot", "汇合": "join",
         "合并槽位": "merge", "结束": "end"}


def sha(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()[:10]


def shops_by_name() -> dict:
    out = {}
    for line in (ROOT / "data" / "processed" / "shops.jsonl").read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        out[r["name"]] = {"amap_id": r["amap_id"], "name": r["name"], "cate": r["cate"], "cost": r["cost"]}
    return out


def inputs(case: dict, shops: dict, run: str) -> dict:
    """按 CF-main 里"上轮槽位"节点（flow_code.CODE_PREV）的格式拼 WF-understand 的输入；画像为空。"""
    ctx = case.get("ctx") or {}
    shown = [shops[n] for n in ctx.get("shown") or []]
    lines = [f"{i + 1}. {x['name']}（{x['cate']}，人均 {x['cost']}）" for i, x in enumerate(shown)]
    return {"query": case["query"], "prev_slots": json.dumps(ctx.get("prev_slots") or {}, ensure_ascii=False),
            "shown": json.dumps(shown, ensure_ascii=False), "shown_text": "\n".join(lines) or "（无）",
            "prof_lines": "（无）", "prof_json": "{}", "active_json": "[]", "pending": "0",
            "conv": f"e1-{run}-{case['id']}", "turn": "1" if ctx else "0", "stage": ctx.get("stage") or "",
            "pending_q": ctx.get("pending_q") or ""}


def secs(x) -> float | None:
    m = re.match(r"([\d.]+)\s*(ms|s)?", str(x or ""))
    if not m:
        return None
    v = float(m.group(1))
    return v / 1000 if m.group(2) == "ms" else v


def parse(d: dict) -> dict:
    out = {"status": d.get("executeStatus"), "cost_s": secs(d.get("workflowExeCost")), "reason": d.get("reason") or "",
           "nodes": {}, "node_s": {}}
    for n in d.get("nodeResults") or []:
        k = NODES.get(n.get("NodeName"))
        if not k:
            continue
        raw = n.get("output") or ""
        try:
            val = json.loads(raw)
        except ValueError:
            val = raw
        out["nodes"][k] = val
        out["node_s"][k] = secs(n.get("nodeExeCost"))
        if n.get("errorInfo"):
            out.setdefault("errors", {})[k] = n["errorInfo"]
    return out


def preflight(c: Coze) -> dict:
    d = c.post("/api/workflow_api/canvas", {"workflow_id": WF, "space_id": c.space_id()})["data"]
    schema = json.loads(d["workflow"]["schema_json"].replace(need("SIDECAR_TOKEN"), "<SIDECAR_TOKEN>"))
    local = json.loads((ROOT / "canvases" / "wf_understand.json").read_text(encoding="utf-8"))
    strings = []

    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, str):
            strings.append(x)
    walk(schema)
    used = {}
    for p in sorted((ROOT / "prompts").glob("*.md")):
        t = p.read_text(encoding="utf-8").strip()
        if any(t == s.strip() for s in strings):  # LLM 节点的系统提示词是整段文件内容
            used[p.name] = sha(t.encode("utf-8"))
    vcs = d.get("vcs_data") or {}
    st, h = call("GET", "http://localhost:8090/health", {}, None, timeout=10)
    side = (ROOT / "sidecar" / "app" / "main.py").read_bytes()
    return {"workflow": "la_understand", "workflow_id": WF, "published_version": d.get("workflow_version"),
            "draft_is_published": bool(vcs.get("submit_commit_id")) and vcs.get("submit_commit_id") == vcs.get("draft_commit_id"),
            "draft_equals_local_canvas": schema == local, "prompts_in_canvas": used,
            "intents_sha": sha(json.dumps(fc.INTENTS, ensure_ascii=False).encode("utf-8")),
            "coze_model": {"model_type": fc.MODEL_TYPE, "name": fc.MODEL_NAME},
            "sidecar_health": h if st == 200 else {"http": st}, "sidecar_main_sha": sha(side)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default=datetime.datetime.now().strftime("%m%d-%H%M"))
    ap.add_argument("--only", default="")
    ap.add_argument("--split", default="")
    ap.add_argument("-j", type=int, default=4)
    ap.add_argument("--retry-failed", action="store_true", help="先删掉结果里失败的行再续跑")
    a = ap.parse_args()
    cases = [json.loads(x) for x in DATA.read_text(encoding="utf-8").splitlines() if x.strip()]
    if a.only:
        keep = set(a.only.split(","))
        cases = [c for c in cases if c["id"] in keep]
    if a.split:
        cases = [c for c in cases if c["split"] == a.split]
    out_dir = ROOT / "eval" / "runs" / a.run
    out_dir.mkdir(parents=True, exist_ok=True)
    res_file = out_dir / "e1_results.jsonl"
    done = set()
    if res_file.exists():
        rows = [json.loads(x) for x in res_file.read_text(encoding="utf-8").splitlines() if x.strip()]
        if a.retry_failed:
            rows = [r for r in rows if r.get("status") == 2]
            res_file.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        done = {r["id"] for r in rows}
    todo = [c for c in cases if c["id"] not in done]

    c0 = Coze()
    c0.login()
    meta_file = out_dir / "meta.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
    meta.update(preflight(c0))
    meta.update({"run": a.run, "dataset": str(DATA.relative_to(ROOT)), "dataset_sha": sha(DATA.read_bytes()),
                 "n_cases": len(cases), "started": meta.get("started") or datetime.datetime.now().isoformat(timespec="seconds")})
    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"run={a.run} 待跑 {len(todo)}/{len(cases)}；发布版 {meta['published_version']}，草稿=发布 {meta['draft_is_published']}，"
          f"草稿=本地画布 {meta['draft_equals_local_canvas']}；prompts {list(meta['prompts_in_canvas'])}", flush=True)
    if not (meta["draft_is_published"] and meta["draft_equals_local_canvas"]):
        print("注意：草稿和发布版或本地画布不一致，这次测的是草稿", flush=True)

    shops = shops_by_name()
    # 各线程共用一个登录会话：同一账号再登录会把前一个 session_key 顶掉，各自登录会互相踢下线
    lock = threading.Lock()
    n_ok = [0, 0]

    def one(case):
        t0 = time.time()
        try:
            d = test_run(c0, WF, inputs(case, shops, a.run), bot_id=IDS["bot_id"], timeout_s=120)
            r = parse(d)
        except Exception as e:  # noqa: BLE001
            r = {"status": -1, "reason": f"{type(e).__name__}: {e}"[:300]}
        r.update({"id": case["id"], "wall_s": round(time.time() - t0, 1)})
        with lock:
            with res_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n_ok[0 if r.get("status") == 2 else 1] += 1
            k = n_ok[0] + n_ok[1]
            if k % 10 == 0 or k == len(todo):
                print(f"PROGRESS {k}/{len(todo)} 成功 {n_ok[0]} 失败 {n_ok[1]}", flush=True)

    with ThreadPoolExecutor(a.j) as ex:
        list(ex.map(one, todo))
    meta["finished"] = datetime.datetime.now().isoformat(timespec="seconds")
    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"DONE {res_file.relative_to(ROOT)}", flush=True)


if __name__ == "__main__":
    main()
