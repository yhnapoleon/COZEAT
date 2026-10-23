"""E3 长期记忆：按 eval/data/e3_scripts.json 的多轮脚本和发布版智能体对话，最后核对这个用户画像的终态。

每段脚本一个新 user_id（e3-<run>-<脚本 id>），每个会话一个会话名；每个会话说完立即调 sidecar /mem/consolidate
整理一次（线上是安静 120 秒后整理，这里不等）。终态直接读 coze-mysql 里线上 user_memory 表这个用户的行。

判分：生效（kind=pref、status=active）的行里，expect 每条都要有（键相同、值互相包含，| 分隔可接受写法），
forbid 每条都不能有；有 answer_forbid 的脚本，指定那一轮的回答里不能出现给定的写法（正则）。全部满足这段脚本才算过。

用法：python -I -X utf8 eval/e3_run.py [--run 名字] [-j 3] [--only m01,m02]
输出：eval/runs/<run>/e3_results.jsonl、eval/reports/E3_<run>.md
"""
import argparse
import datetime
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bridge"))
sys.path.insert(0, str(ROOT))
from chat_api import chat  # noqa: E402
from common.env import need  # noqa: E402

IDS = json.loads((ROOT / "lifeagent_ids.json").read_text(encoding="utf-8"))
SCRIPTS = ROOT / "eval" / "data" / "e3_scripts.json"
COLS = ["kind", "mkey", "mvalue", "status", "support", "consistency", "confidence"]  # 线上表 f_1..f_7，顺序同建表字段


def mysql(sql: str) -> list[list[str]]:
    cmd = ["docker", "exec", "coze-mysql", "sh", "-c",
           f'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" --default-character-set=utf8mb4 -B -N -e "{sql}" 2>/dev/null']
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8").stdout
    return [line.split("\t") for line in out.splitlines() if line]


def mem_table() -> str:
    tid = IDS["tables_online"]["user_memory"]
    return mysql(f"SELECT physical_table_name FROM opencoze.online_database_info WHERE id = {tid}")[0][0]


def rows_of(table: str, uid: str) -> list[dict]:
    rs = mysql(f"SELECT f_1, f_2, f_3, f_4, f_5, f_6, f_7 FROM opencoze.{table} WHERE bstudio_connector_uid = '{uid}' ORDER BY bstudio_id")
    return [dict(zip(COLS, r)) for r in rs]


def hit(row: dict, key: str, alts: str) -> bool:
    # 只认"写法包含在值里"：反过来也算的话，"能吃辣"会被当成命中禁止项"不能吃辣"（10-21 第一次跑 m09 误判）
    v = (row.get("mvalue") or "").strip()
    return row.get("mkey") == key and bool(v) and any(a and a in v for a in alts.split("|"))


def turn_answer(r: dict, session: int, turn: int) -> str:
    said = [x for x in r["log"] if "user" in x and x["session"] == session]
    return said[turn - 1]["answer"] if 0 < turn <= len(said) else ""


def rescore(r: dict, s: dict) -> dict:
    active = [x for x in r["rows"] if x["kind"] == "pref" and x["status"] == "active"]
    r["miss"] = [e for e in s["expect"] if not any(hit(x, *e) for x in active)]
    r["bad"] = [f for f in s["forbid"] if any(hit(x, *f) for x in active)]
    # answer_forbid：[会话序号, 第几轮, 正则]，那一轮的回答里不能出现（10-21，同一会话改口后回答里不能再写旧画像）
    r["ans_bad"] = [a for a in s.get("answer_forbid") or [] if re.search(a[2], turn_answer(r, a[0], a[1]))]
    r["pass"] = not r["miss"] and not r["bad"] and not r["ans_bad"]
    return r


def run_script(s: dict, run: str, table: str) -> dict:
    uid = f"e3-{run}-{s['id']}"
    log = []
    for si, turns in enumerate(s["sessions"]):
        conv = f"{uid}-s{si + 1}"
        for t in turns:
            t0 = time.time()
            try:
                ans = chat(t, uid, conv)
            except Exception as e:  # noqa: BLE001
                ans = f"[出错] {type(e).__name__}: {e}"
            log.append({"session": si + 1, "user": t, "answer": ans[:600], "s": round(time.time() - t0, 1)})
        t0 = time.time()
        try:
            r = httpx.post("http://localhost:8090/mem/consolidate", params={"uid": uid},
                           headers={"X-Sidecar-Token": need("SIDECAR_TOKEN")}, timeout=300).json()
            cons = {"error": r.get("error"), "data": str(r.get("data"))[:800]}
        except Exception as e:  # noqa: BLE001
            cons = {"error": f"{type(e).__name__}: {e}"}
        log.append({"session": si + 1, "consolidate": cons, "s": round(time.time() - t0, 1)})
    return rescore({"id": s["id"], "kind": s["kind"], "uid": uid, "rows": rows_of(table, uid), "log": log}, s)


def versions() -> dict:
    from coze_client import Coze
    c = Coze()
    c.login()
    out = {}
    for k in ("cf_main", "consolidate", "understand", "memwrite"):
        d = c.post("/api/workflow_api/canvas", {"workflow_id": IDS["workflows"][k], "space_id": c.space_id()})["data"]
        out[k] = d.get("workflow_version")
    return out


def report(run: str, res: list[dict], scripts: list[dict]) -> str:
    L = [f"# E3 长期记忆：{run}\n"]
    h = httpx.get("http://localhost:8090/health", timeout=10).json()
    v = versions()
    L.append(f"- 被测：发布版智能体（CF-main {v['cf_main']}、WF-understand {v['understand']}、WF-memwrite {v['memwrite']}、"
             f"WF-consolidate {v['consolidate']}），经 OpenAPI 对话；"
             f"/decide {h.get('decider_model')}；每个会话结束立即整理一次")
    n_turn = sum(1 for r in res for x in r["log"] if "user" in x)
    L.append(f"- {len(res)} 段脚本、{n_turn} 轮对话；判分看最终生效的偏好（status=active）\n")
    ok = sum(r["pass"] for r in res)
    n_exp = sum(len(s["expect"]) for s in scripts if s["id"] in {r["id"] for r in res})
    n_exp_ok = sum(len(next(s for s in scripts if s["id"] == r["id"])["expect"]) - len(r["miss"]) for r in res)
    n_forb = sum(len(s["forbid"]) for s in scripts if s["id"] in {r["id"] for r in res})
    n_bad = sum(len(r["bad"]) for r in res)
    L.append(f"**终态全对 {ok}/{len(res)}**；该生效的 {n_exp_ok}/{n_exp} 条生效；不该生效的 {n_forb} 条里生效了 {n_bad} 条。"
             "对照基线\"只用会话历史\"换会话后什么都不记得，该生效的一条也拿不到。\n")
    concl = ROOT / "eval" / "data" / "e3_conclusion.md"
    if concl.exists():
        L.append(concl.read_text(encoding="utf-8"))
    L.append("| 脚本 | 类型 | 结果 | 最终生效的偏好 | 其余行（状态） |")
    L.append("|---|---|---|---|---|")
    for r in res:
        act = "；".join(f"{x['mkey']}={x['mvalue']}" for x in r["rows"] if x["kind"] == "pref" and x["status"] == "active") or "—"
        other = "；".join(f"{x['mkey']}={x['mvalue']}（{x['status']}）" for x in r["rows"]
                         if not (x["kind"] == "pref" and x["status"] == "active")) or "—"
        why = "过" if r["pass"] else "没过：" + "、".join([f"缺 {k}={v}" for k, v in r["miss"]] + [f"不该有 {k}={v}" for k, v in r["bad"]]
                                                       + [f"会话{a[0]}第{a[1]}轮回答出现「{a[2]}」" for a in r.get("ans_bad") or []])
        L.append(f"| {r['id']} | {r['kind']} | {why} | {act} | {other[:160]} |")
    L.append("\n## 对话记录（节选）\n")
    for r in res:
        L.append(f"### {r['id']} {r['kind']}（{r['uid']}）\n")
        for x in r["log"]:
            if "user" in x:
                L.append(f"- 会话{x['session']} 用户：{x['user']}（{x['s']} 秒）\n  - 回答：{x['answer'][:160].replace(chr(10), ' ')}")
            else:
                c = x["consolidate"]
                L.append(f"- 会话{x['session']} 结束后整理（{x['s']} 秒）：{('出错 ' + str(c['error'])) if c.get('error') else c.get('data', '')[:300]}")
        L.append("")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default=datetime.datetime.now().strftime("e3-%m%d-%H%M"))
    ap.add_argument("-j", type=int, default=3)
    ap.add_argument("--only", default="")
    ap.add_argument("--rescore", action="store_true", help="不重跑，按现在的判分规则重算已有结果、重出报告")
    a = ap.parse_args()
    scripts = json.loads(SCRIPTS.read_text(encoding="utf-8"))
    if a.rescore:
        S = {s["id"]: s for s in scripts}
        rf = ROOT / "eval" / "runs" / a.run / "e3_results.jsonl"
        res = [rescore(json.loads(x), S[json.loads(x)["id"]]) for x in rf.read_text(encoding="utf-8").splitlines() if x.strip()]
        rf.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in res), encoding="utf-8")
        (ROOT / "eval" / "reports" / f"E3_{a.run}.md").write_text(report(a.run, res, scripts), encoding="utf-8")
        print("重算", f"过 {sum(r['pass'] for r in res)}/{len(res)}")
        return
    if a.only:
        scripts = [s for s in scripts if s["id"] in a.only.split(",")]
    table = mem_table()
    out_dir = ROOT / "eval" / "runs" / a.run
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"run={a.run} {len(scripts)} 段脚本，线上记忆表 {table}", flush=True)
    res = []

    def one(s):
        r = run_script(s, a.run, table)
        print(f"PROGRESS {s['id']} {'过' if r['pass'] else '没过'} miss={r['miss']} bad={r['bad']} ans_bad={r['ans_bad']}", flush=True)
        return r

    with ThreadPoolExecutor(a.j) as ex:
        res = list(ex.map(one, scripts))
    (out_dir / "e3_results.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in res), encoding="utf-8")
    rep = ROOT / "eval" / "reports" / f"E3_{a.run}.md"
    rep.write_text(report(a.run, res, scripts), encoding="utf-8")
    print("DONE", rep.relative_to(ROOT), f"过 {sum(r['pass'] for r in res)}/{len(res)}", flush=True)


if __name__ == "__main__":
    main()
