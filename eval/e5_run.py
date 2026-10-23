"""E5 端到端：DeepSeek 扮用户，和发布版智能体多轮对话找店，程序判选中的店是否满足全部条件。

任务在 eval/data/e5_tasks.json。每个条件有暴露方式（参照 τ-Rec 的三档）：
  say     第一句就说
  ask     被问到、或推荐的店不符合时才说
  memory  从不说；开跑前用一个准备会话"记住……"写进长期记忆，看系统会不会自己用上
用户模拟器看到符合全部要求的店就说"就第 N 家"，最多 4 轮。选中的店从线上 session_state 的 last_shown 取
（编号对回 amap_id），再用 shops.jsonl 判：
  area      离锚点 1 公里内（海岸城和万象城相距 0.9 公里）
  category  品类、店名、推荐菜里含任一写法
  budget    人均 ≤ 上限 × 1.1
  scene     门店场景标签里有
另记一个"系统侧"成功：最后一轮展示的店里有没有满足全部条件的（不管模拟器选没选对）。

用法：python -I -X utf8 eval/e5_run.py --check            # 只看每个任务库里有几家店满足
      python -I -X utf8 eval/e5_run.py [--run 名字] [-j 3] [--only t01]
输出：eval/runs/<run>/e5_results.jsonl、eval/reports/E5_<run>.md
"""
import argparse
import datetime
import json
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bridge"))
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT))
from chat_api import chat  # noqa: E402
from common.env import need  # noqa: E402
from common.llm import chat as llm  # noqa: E402
from e3_run import mysql, versions  # noqa: E402

IDS = json.loads((ROOT / "lifeagent_ids.json").read_text(encoding="utf-8"))
TASKS = ROOT / "eval" / "data" / "e5_tasks.json"
ANCHOR = {"海岸城": (113.937635, 22.518308), "深圳湾万象城": (113.946529, 22.51695), "南山科技园": (113.94604, 22.54461)}
MAX_TURNS = 4
TYPE_CN = {"area": "地点", "category": "想吃", "budget": "人均上限（元）", "scene": "场合"}


def shops() -> dict:
    out = {}
    for line in (ROOT / "data" / "processed" / "shops.jsonl").read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        out[r["amap_id"]] = r
    return out


def dist_m(a, b) -> float:
    R = 6371000
    p1, p2 = math.radians(a[1]), math.radians(b[1])
    dp, dl = p2 - p1, math.radians(b[0] - a[0])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def check(shop: dict, c: dict, std: bool = True) -> bool:
    t, v = c["type"], c["value"]
    if t == "area":
        return dist_m((float(shop["lng"]), float(shop["lat"])), ANCHOR[v]) <= 1000
    if t == "category":
        # 10-21 起品类也认标准品类 std_cate（etl/shop_cats.py，338 家人工看过一遍）：高德文本经常不带品类词，
        # 陶陶居只写"中餐厅"、天天天妇罗写"餐饮相关场所"，按文本判会把对的店判错。std=False 是以前的纯文本口径，报告里并列
        text = f"{shop.get('cate') or ''} {shop.get('name') or ''} {shop.get('dishes') or ''}"
        if std:
            text += " " + str(shop.get("std_cate") or "")
        return any(x in text for x in str(v).split("|"))
    if t == "budget":
        try:
            return 0 < float(shop.get("cost") or 0) <= v * 1.1
        except (TypeError, ValueError):
            return False
    if t == "scene":
        return v in str(shop.get("tags") or "")
    return False


def ok_all(shop: dict, cons: list[dict], std: bool = True) -> tuple[bool, list[str]]:
    bad = [f"{TYPE_CN[c['type']]}={c['value']}" for c in cons if not check(shop, c, std)]
    return not bad, bad


def state_table() -> str:
    tid = IDS["tables_online"]["session_state"]
    return mysql(f"SELECT physical_table_name FROM opencoze.online_database_info WHERE id = {tid}")[0][0]


def last_shown(table: str, uid: str, conv: str) -> list[dict]:
    rs = mysql(f"SELECT f_4 FROM opencoze.{table} WHERE bstudio_connector_uid = '{uid}' AND f_1 = '{conv}' "
               f"ORDER BY bstudio_id DESC LIMIT 1")
    try:
        return json.loads(rs[0][0]) if rs and rs[0][0] else []
    except ValueError:
        return []


SIM = """你在扮演一个正在用美食推荐助手找饭店的用户。人设：{persona}。

你的真实要求：
- 一开口就会说的：{say}
- 只有助手问到、或者它推荐的店不符合时才会说的：{ask}
- 你不会说出来、但希望助手自己记得的：{memory}（绝对不要说出来）

规则：
- 每次只说一句，像在手机上打字，口语，不超过 30 个字。
- 第一句只说"一开口就会说的"要求。
- 读助手的回复：如果它列出的店里有一家看起来符合你全部要求（品类、人均、位置、场合），就说"就第 N 家吧"，N 是它回复里的编号，accept 填 N。
- 没有符合的：说出一条不符合的地方（这时可以说"问到才说"的要求），让它换，accept 填 0。
- 助手问你问题时，按你的要求如实回答，accept 填 0。

到目前为止的对话：
{history}

只输出一个 JSON：{{"say": "你这一轮说的话", "accept": 0}}"""


def fmt(cons, how):
    xs = [f"{TYPE_CN[c['type']]}：{c['value'].split('|')[0] if isinstance(c['value'], str) else c['value']}" for c in cons if c["how"] == how]
    return "；".join(xs) or "（无）"


def simulate(task: dict, history: list[tuple[str, str]]) -> dict:
    h = "\n".join(f"{'你' if r == 'user' else '助手'}：{t[:1800]}" for r, t in history) or "（还没开始，这是你的第一句）"
    p = SIM.format(persona=task["persona"], say=fmt(task["cons"], "say"), ask=fmt(task["cons"], "ask"),
                   memory=fmt(task["cons"], "memory"), history=h)
    for _ in range(3):
        raw = llm(p, max_tokens=8000)
        m = re.search(r"\{.*\}", raw, re.S)
        try:
            d = json.loads(m.group(0)) if m else {}
            if d.get("say"):
                return {"say": str(d["say"]), "accept": int(d.get("accept") or 0)}
        except (ValueError, TypeError):
            pass
    return {"say": "还有别的推荐吗", "accept": 0}


def run_task(task: dict, run: str, table: str, S: dict) -> dict:
    uid, conv = f"e5-{run}-{task['id']}", f"e5-{run}-{task['id']}-c1"
    log = []
    for s in task.get("setup") or []:
        chat(s, uid, f"{conv}-setup")
    if task.get("setup"):
        httpx.post("http://localhost:8090/mem/consolidate", params={"uid": uid},
                   headers={"X-Sidecar-Token": need("SIDECAR_TOKEN")}, timeout=300)
    history, chosen, shown, turns = [], None, [], 0
    for turn in range(MAX_TURNS + 1):
        sim = simulate(task, history)
        if sim["accept"] and shown and 0 < sim["accept"] <= len(shown):
            chosen = shown[sim["accept"] - 1]
            log.append({"user": sim["say"], "accept": sim["accept"]})
            break
        if turn == MAX_TURNS:
            break
        t0 = time.time()
        try:
            ans = chat(sim["say"], uid, conv)
        except Exception as e:  # noqa: BLE001
            ans = f"[出错] {type(e).__name__}: {e}"
        turns += 1
        shown = last_shown(table, uid, conv)
        history += [("user", sim["say"]), ("bot", ans)]
        log.append({"user": sim["say"], "answer": ans[:700], "s": round(time.time() - t0, 1),
                    "shown": [x.get("name") for x in shown]})
    cons = task["cons"]
    res = {"id": task["id"], "uid": uid, "turns": turns, "log": log}
    if chosen:
        sh = S.get(chosen.get("amap_id"))
        good, bad = ok_all(sh, cons) if sh else (False, ["店不在库里"])
        res["success_text"] = ok_all(sh, cons, std=False)[0] if sh else False
        res.update({"chosen": chosen.get("name"), "success": good, "violations": bad})
    else:
        res.update({"chosen": None, "success": False, "violations": ["模拟用户没有选定"]})
    res["system_ok"] = any(ok_all(S[x["amap_id"]], cons)[0] for x in shown if x.get("amap_id") in S)
    res["shown_last"] = [x.get("name") for x in shown]
    res["latency"] = [x["s"] for x in log if "s" in x]
    return res


def report(run, res, tasks) -> str:
    v = versions()
    h = httpx.get("http://localhost:8090/health", timeout=10).json()
    T = {t["id"]: t for t in tasks}
    L = [f"# E5 端到端：{run}\n",
         f"- 被测：发布版智能体（CF-main {v['cf_main']}），经 OpenAPI 对话；/decide {h.get('decider_model')}；用户模拟器 DeepSeek V4.1 Flash",
         f"- {len(res)} 个任务，每个最多 {MAX_TURNS} 轮；条件暴露分三档：开口说、问到才说、只在长期记忆里\n"]
    n = len(res)
    succ = sum(r["success"] for r in res)
    sys_ok = sum(r["system_ok"] for r in res)
    lat = sorted(x for r in res for x in r["latency"])
    succ_turns = [r["turns"] for r in res if r["success"]]
    succ_text = sum(bool(r.get("success_text", r["success"])) for r in res)
    L.append(f"**任务成功 {succ}/{n}**（模拟用户选中的店满足全部条件；品类按高德文本或标准品类判，只按文本判 {succ_text}/{n}）；"
             f"最后一轮展示里有满足全部条件的店 {sys_ok}/{n}；"
             f"成功任务平均 {sum(succ_turns) / len(succ_turns) if succ_turns else 0:.1f} 轮；"
             f"单轮延迟中位 {lat[len(lat) // 2] if lat else 0:.0f} 秒、最长 {lat[-1] if lat else 0:.0f} 秒。\n")
    mem = [r for r in res if any(c["how"] == "memory" for c in T[r["id"]]["cons"])]
    if mem:
        L.append(f"含\"只在长期记忆里\"条件的 {len(mem)} 个任务成功 {sum(r['success'] for r in mem)} 个。\n")
    concl = ROOT / "eval" / "data" / "e5_conclusion.md"
    if concl.exists():
        L.append(concl.read_text(encoding="utf-8"))
    L.append("| 任务 | 条件（说 / 问到才说 / 记忆） | 轮数 | 选中 | 结果 |")
    L.append("|---|---|---|---|---|")
    for r in res:
        t = T[r["id"]]
        cons = "；".join(f"{TYPE_CN[c['type']]} {str(c['value']).split('|')[0]}（{ {'say': '说', 'ask': '问', 'memory': '记忆'}[c['how']] }）"
                        for c in t["cons"])
        out = "成功" if r["success"] else "失败：" + "、".join(r["violations"])
        L.append(f"| {r['id']} | {cons} | {r['turns']} | {r['chosen'] or '—'} | {out} |")
    L.append("\n## 对话记录\n")
    for r in res:
        L.append(f"### {r['id']} {T[r['id']]['persona']}\n")
        for x in r["log"]:
            L.append(f"- 用户：{x['user']}" + (f"（选第 {x['accept']} 家）" if x.get("accept") else ""))
            if "answer" in x:
                L.append(f"  - 助手（{x['s']} 秒）：{x['answer'][:200].replace(chr(10), ' ')}")
                L.append(f"  - 展示：{'、'.join(n_[:12] for n_ in x['shown']) or '（没有推荐）'}")
        L.append("")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default=datetime.datetime.now().strftime("e5-%m%d-%H%M"))
    ap.add_argument("-j", type=int, default=3)
    ap.add_argument("--only", default="")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--rescore", action="store_true", help="不重跑，按现在的任务条件重判已有结果、重出报告")
    a = ap.parse_args()
    tasks = json.loads(TASKS.read_text(encoding="utf-8"))
    if a.only:
        tasks = [t for t in tasks if t["id"] in a.only.split(",")]
    S = shops()
    if a.rescore:
        by_name = {s["name"]: s for s in S.values()}
        T = {t["id"]: t for t in tasks}
        rf = ROOT / "eval" / "runs" / a.run / "e5_results.jsonl"
        res = [json.loads(x) for x in rf.read_text(encoding="utf-8").splitlines() if x.strip()]
        for r in res:
            cons = T[r["id"]]["cons"]
            if r.get("chosen"):
                sh = by_name.get(r["chosen"])
                r["success"], r["violations"] = ok_all(sh, cons) if sh else (False, ["店不在库里"])
                r["success_text"] = ok_all(sh, cons, std=False)[0] if sh else False
            r["system_ok"] = any(ok_all(by_name[n], cons)[0] for n in r.get("shown_last") or [] if n in by_name)
        rf.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in res), encoding="utf-8")
        (ROOT / "eval" / "reports" / f"E5_{a.run}.md").write_text(report(a.run, res, tasks), encoding="utf-8")
        print("重判", f"成功 {sum(r['success'] for r in res)}/{len(res)}")
        return
    if a.check:
        for t in tasks:
            good = [s["name"] for s in S.values() if ok_all(s, t["cons"])[0]]
            print(t["id"], len(good), "、".join(x[:12] for x in good[:6]))
        return
    table = state_table()
    out_dir = ROOT / "eval" / "runs" / a.run
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"run={a.run} {len(tasks)} 个任务", flush=True)

    def one(t):
        r = run_task(t, a.run, table, S)
        print(f"PROGRESS {t['id']} {'成功' if r['success'] else '失败'} 轮数 {r['turns']} 选中 {r['chosen']} {r['violations']}", flush=True)
        return r

    with ThreadPoolExecutor(a.j) as ex:
        res = list(ex.map(one, tasks))
    (out_dir / "e5_results.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in res), encoding="utf-8")
    rep = ROOT / "eval" / "reports" / f"E5_{a.run}.md"
    rep.write_text(report(a.run, res, tasks), encoding="utf-8")
    print("DONE", rep.relative_to(ROOT), f"成功 {sum(r['success'] for r in res)}/{len(res)}", flush=True)


if __name__ == "__main__":
    main()
