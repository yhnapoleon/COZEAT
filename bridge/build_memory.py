"""构建 WF-consolidate：长期记忆的异步整理（设计文档 4.6）。

sidecar 在用户会话安静一段时间后，用 OpenAPI /v1/workflow/run 带 ext.user_id 调它；
user_memory 按用户隔离，所以每次运行只看得到这个用户的行。

流程：读记忆 → 准备 → 有待整理的？→ 整理记忆（LLM：归组、判断，改写推荐策略）→ 稳定性请求 → D4 稳定性（sidecar /decide，每组一个 noul）
      → 生成更新（代码：支持次数、一致性和 D4 定是否提升）
      → 批量更新 → 有新策略？→ 旧策略标 superseded → 写新策略

用法：
  python -I -X utf8 bridge/build_memory.py build
  python -I -X utf8 bridge/build_memory.py run          # 调试运行（读写草稿表，用户是当前账号）
  python -I -X utf8 bridge/build_memory.py release v0.0.1
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
import canvas as cv  # noqa: E402
from coze_client import Coze  # noqa: E402
from workflow_api import create_workflow, publish_workflow, save_workflow, summarize_process, test_run  # noqa: E402
from common.env import need  # noqa: E402

IDS_FILE = ROOT / "lifeagent_ids.json"
PROMPTS = ROOT / "prompts"
from flow_code import MODEL_NAME, MODEL_TYPE  # noqa: E402  模型统一在 flow_code 里配
SIDECAR = "http://host.docker.internal:8090"

MEM_SCHEMA = [cv.out(n, "string") for n in ["id", "kind", "mkey", "mvalue", "status", "support", "consistency", "confidence",
                                            "evidence", "conv", "updated_at"]]
SQL_READ = ("SELECT id, kind, mkey, mvalue, status, support, consistency, confidence, evidence, conv, updated_at "
            "FROM user_memory WHERE status IN ('active', 'candidate') ORDER BY id LIMIT 300")
SQL_UPD = ("UPDATE user_memory SET status = '{{status}}', mvalue = '{{mvalue}}', support = CAST('{{support}}' AS SIGNED), "
           "consistency = CAST('{{cons}}' AS DECIMAL(6,3)), confidence = CAST('{{conf}}' AS DECIMAL(6,3)), "
           "updated_at = '{{now}}' WHERE id = CAST('{{id}}' AS SIGNED)")
SQL_STRAT_OLD = "UPDATE user_memory SET status = 'superseded', updated_at = '{{now}}' WHERE kind = 'strategy' AND status = 'active'"
SQL_STRAT_INS = ("INSERT INTO user_memory (kind, mkey, mvalue, status, support, consistency, confidence, last_seen, evidence, conv, updated_at) "
                 "VALUES ('strategy', 'strategy', '{{v}}', 'active', 1, 1, 1, '{{now}}', '{{ev}}', '', '{{now}}')")

CODE_PREP = r'''
import json
import datetime
async def main(args: Args) -> Output:
    rows = args.params.get("rows") or []
    now_dt = datetime.datetime.utcnow() + datetime.timedelta(hours=8)  # 沙箱是 UTC，存北京时间
    def num(x, d):
        try:
            return float(x)
        except Exception:
            return d
    m, act, cand, fb, strat = {}, [], [], [], ""
    for r in rows:
        i = str(r.get("id") or "")
        kind, k, v, st = r.get("kind") or "", r.get("mkey") or "", str(r.get("mvalue") or ""), r.get("status") or ""
        try:
            ev = json.loads(r.get("evidence") or "{}")
        except Exception:
            ev = {}
        if not isinstance(ev, dict):
            ev = {}
        day = str(r.get("updated_at") or "")[:10]
        m[i] = {"kind": kind, "mkey": k, "mvalue": v, "status": st, "support": int(num(r.get("support"), 1)),
                "cons": num(r.get("consistency"), 1.0), "conf": num(r.get("confidence"), 0.0), "conv": r.get("conv") or "",
                "updated_at": str(r.get("updated_at") or ""), "text": str(ev.get("text") or "")[:100],
                "tid": str(ev.get("target_id") or "")}
        if kind == "pref" and st == "active":
            act.append("[" + i + "] " + k + "=" + v + "（支持 " + str(m[i]["support"]) + " 次）")
        elif kind == "pref" and st == "candidate":
            tid = str(ev.get("target_id") or "")
            cand.append("[" + i + "] " + k + "=" + v + " op=" + str(ev.get("op") or "add") + (" target_id=" + tid if tid else "")
                        + " durable=" + str(m[i]["conf"]) + " 会话=" + m[i]["conv"] + " 日期=" + day + " 原话：" + str(ev.get("text") or ""))
        elif kind == "feedback" and st == "candidate":
            others = "、".join(str(o.get("name")) + "（" + str(o.get("cate") or "") + "，人均 " + str(o.get("cost") or "") + "）"
                              for o in ev.get("others") or [] if isinstance(o, dict))
            fb.append("[" + i + "] " + ("选了 " if v == "accept" else "否掉 ") + str(ev.get("name") or k) + "（" + str(ev.get("cate") or "")
                      + "，人均 " + str(ev.get("cost") or "") + "）" + ("；同屏没选：" + others if others else "")
                      + ("；原话：" + str(ev.get("reason")) if ev.get("reason") else "") + "；日期 " + day)
        elif kind == "strategy" and st == "active":
            strat = strat or v
    NL = chr(10)
    return {"rows_json": json.dumps(m, ensure_ascii=False), "actives": NL.join(act) or "（无）", "cands": NL.join(cand) or "（无）",
            "feedback": NL.join(fb) or "（无）", "strategy": strat or "（无）", "has_work": bool(cand or fb),
            "summary": "没有待整理的记忆（候选 0 条，反馈 0 条）"}
'''

# D4 长期稳定性（设计文档 4.5）：每个偏好组问一个 noul，交给 sidecar /decide；整理 LLM 给的 stable 只在 /decide 没答时用
CODE_D4_REQ = r'''
import json
import re
async def main(args: Args) -> Output:
    p = args.params
    try:
        rows = json.loads(p.get("rows_json") or "{}")
    except Exception:
        rows = {}
    m = re.search(r"\{.*\}", p.get("out") or "", re.S)
    try:
        res = json.loads(m.group(0)) if m else {}
    except Exception:
        res = {}
    groups, qs = [], {}
    for i, g in enumerate(res.get("groups") or []):
        if not isinstance(g, dict):
            continue
        cids = [x.strip().strip("[]") for x in str(g.get("cand_ids") or "").replace("，", ",").split(",")]
        said = [rows[c]["text"] for c in cids if c in rows and rows[c].get("text")][:5]
        gid = "g" + str(i)
        groups.append({"gid": gid, "key": g.get("key"), "value": g.get("value"), "action": g.get("action"),
                       "user_said": said, "sessions": len({rows[c]["conv"] for c in cids if c in rows})})
        qs[gid] = {"type": "noul",
                   "instructions": "groups 里 " + gid + " 这条是用户长期稳定的偏好（以后找店都适用），不是只针对这一次（今天、这次、最近一顿）"}
    state = {"note": "key 含义：avoid 忌口，taste 口味，cuisine_like/cuisine_dislike 喜欢/不喜欢的品类，budget 人均上限，area 常去商圈，scene 常见场景",
             "groups": groups}
    return {"body": json.dumps({"point": "D4_stable", "state": state, "questions": qs}, ensure_ascii=False)}
'''

CODE_APPLY = r'''
import json
import re
import datetime
async def main(args: Args) -> Output:
    p = args.params
    now_dt = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    now = now_dt.isoformat(timespec="seconds")
    cutoff = (now_dt - datetime.timedelta(days=30)).isoformat(timespec="seconds")
    try:
        rows = json.loads(p.get("rows_json") or "{}")
    except Exception:
        rows = {}
    m = re.search(r"\{.*\}", p.get("out") or "", re.S)
    try:
        res = json.loads(m.group(0)) if m else None
    except Exception:
        res = None
    def f(x):
        try:
            return max(0.0, min(1.0, float(x)))
        except Exception:
            return 0.0
    upd, log = {}, []
    def put(i, **kw):
        r = rows.get(i)
        if not r:
            return
        d = upd.setdefault(i, {"id": i, "status": r["status"], "mvalue": r["mvalue"], "support": r["support"],
                               "cons": r["cons"], "conf": r["conf"], "now": now})
        d.update(kw)
    def ids(s, want):
        out = []
        for x in str(s or "").replace("，", ",").split(","):
            x = x.strip().strip("[]")
            r = rows.get(x)
            if r and r["kind"] == "pref" and r["status"] == want and x not in out:
                out.append(x)
        return out
    try:
        d4 = (json.loads(p.get("d4") or "{}") or {}).get("answers") or {}
    except Exception:
        d4 = {}
    used = set()
    for gi, g in enumerate((res or {}).get("groups") or []):
        if not isinstance(g, dict):
            continue
        cids = [i for i in ids(g.get("cand_ids"), "candidate") if i not in used]
        if not cids:
            continue
        used.update(cids)
        xids = ids(g.get("contra_ids"), "candidate")
        act, key, val = str(g.get("action") or "add"), str(g.get("key") or ""), str(g.get("value") or "").strip()
        tid = str(g.get("target_id") or "").strip().strip("[]")
        tgt = rows.get(tid) if tid in rows and rows[tid]["status"] == "active" else None
        if act == "add" and not tgt:
            # 记忆抽取当时就指出这条推翻了哪条（"我现在挺能吃辣了"→ target 是 avoid=辣），整理 LLM 却判成 add 时按 update 办，
            # 不然新旧两条会同时生效（10-21）
            tid = next((rows[i].get("tid") for i in cids if rows[i].get("tid") in rows
                        and rows[rows[i]["tid"]]["status"] == "active" and rows[rows[i]["tid"]]["kind"] == "pref"), "")
            if tid:
                act, tgt = "update", rows[tid]
        a4 = d4.get("g" + str(gi))
        if isinstance(a4, dict) and "noul" in a4:
            stable, s_src = f(a4.get("noul")), "D4"
        else:
            stable, s_src = f(g.get("stable")), "LLM"  # /decide 超时或出错
        # MATE 的两个信号：支持次数按不同会话计，一致性 = 支持 / (支持 + 矛盾)；用户明说的长期偏好（stable 高）直接提升
        support = len({rows[i]["conv"] for i in cids}) + (int(tgt["support"]) if tgt and act == "keep" else 0)
        contra = len({rows[i]["conv"] for i in xids})
        cons = round(support / (support + contra), 3) if support + contra else 0.0
        ok = (support >= 2 and cons >= 0.67) or stable >= 0.8
        if key == "budget":
            digits = re.findall(r"\d+", val or rows[cids[0]]["mvalue"])
            val = digits[0] if digits else val
        label = key + "=" + (val or rows[cids[0]]["mvalue"])
        if act == "keep" and tgt:
            put(tid, support=support, cons=cons)
            for i in cids:
                put(i, status="merged")
            log.append("再次出现 " + label + "，支持 " + str(support))
        elif act == "delete" and tgt:
            if ok:
                put(tid, status="deleted")
                for i in cids:
                    put(i, status="merged")
                log.append("删除 " + tgt["mkey"] + "=" + tgt["mvalue"])
            else:
                log.append("待定（删除 " + tgt["mkey"] + "=" + tgt["mvalue"] + "，证据不足）")
        elif ok:
            head = cids[0]
            put(head, status="active", mvalue=val or rows[head]["mvalue"], support=support, cons=cons, conf=stable)
            for i in cids[1:]:
                put(i, status="merged")
            old = [tid] if act == "update" and tgt else []
            if key in ("budget", "area"):
                old += [i for i, r in rows.items() if r["kind"] == "pref" and r["status"] == "active" and r["mkey"] == key and i != head]
            for i in old:
                put(i, status="superseded")
            log.append(("替换为 " if old else "提升 ") + label + "（支持 " + str(support) + "，一致性 " + str(cons) + "，stable " + s_src + " " + str(stable) + "）")
        else:
            log.append("待定 " + label + "（支持 " + str(support) + "，一致性 " + str(cons) + "，stable " + s_src + " " + str(stable) + "）")
    strategy = str((res or {}).get("strategy") or "").strip()[:300]
    fb_ids = [i for i, r in rows.items() if r["kind"] == "feedback" and r["status"] == "candidate"]
    if res is not None:
        for i in fb_ids:
            put(i, status="used")
    for i, r in rows.items():  # 30 天没被提升的候选过期
        if r["kind"] == "pref" and r["status"] == "candidate" and i not in upd and r["updated_at"] and r["updated_at"] < cutoff:
            put(i, status="expired")
    if strategy:
        log.append("策略改写（反馈 " + str(len(fb_ids)) + " 条）：" + strategy)
    if res is None:
        log.append("整理输出解析失败，本次不改动")
    items = [json.dumps({k: str(v) for k, v in d.items()}, ensure_ascii=False) for d in upd.values()]
    return {"upd_items": items, "strategy": strategy, "has_strategy": bool(strategy),
            "strat_ev": "反馈 " + str(len(fb_ids)) + " 条", "now": now, "summary": "；".join(log) or "没有变化"}
'''

CODE_UNPACK = r'''
import json
async def main(args: Args) -> Output:
    try:
        d = json.loads(args.params.get("row") or "{}")
    except Exception:
        d = {}
    return {k: str(d.get(k, "")) for k in ["id", "status", "mvalue", "support", "cons", "conf", "now"]}
'''


def build(ids: dict) -> str:
    T = ids["tables_online"]
    S = cv.ENTRY_ID
    n = {k: cv.new_id() for k in ["s1", "c1", "x1", "l1", "dq", "dh", "c2", "b1", "bu", "bs", "x2", "s2", "s3", "agg"]}
    nodes = [cv.entry([cv.out("noop")]),
             cv.custom_sql(n["s1"], "读记忆", T["user_memory"], SQL_READ, [], MEM_SCHEMA, pos=(300, 0)),
             cv.code(n["c1"], "准备", [cv.param("rows", cv.ref(n["s1"], "outputList", "list<object>",
                                                             schema={"type": "object", "schema": MEM_SCHEMA}))],
                     [cv.out("rows_json"), cv.out("actives"), cv.out("cands"), cv.out("feedback"), cv.out("strategy"),
                      cv.out("has_work", "boolean"), cv.out("summary")], CODE_PREP, pos=(600, 0)),
             cv.selector(n["x1"], "有待整理的？", [[(cv.ref(n["c1"], "has_work", "boolean"), "==", cv.lit(True, "boolean"))]],
                         pos=(900, 0)),
             cv.llm_m(n["l1"], "整理记忆",
                      "已生效偏好：\n{{actives}}\n\n待定偏好：\n{{cands}}\n\n已有推荐策略：{{strategy}}\n\n新的推荐反馈：\n{{feedback}}",
                      (PROMPTS / "consolidate.v2.md").read_text(encoding="utf-8"),
                      [cv.param(k, cv.ref(n["c1"], k)) for k in ["actives", "cands", "strategy", "feedback"]],
                      [cv.out("output")], MODEL_TYPE, MODEL_NAME, json_mode=False, max_tokens=8192, pos=(1200, -100)),
             cv.code(n["dq"], "稳定性请求", [cv.param("out", cv.ref(n["l1"], "output")),
                                             cv.param("rows_json", cv.ref(n["c1"], "rows_json"))],
                     [cv.out("body")], CODE_D4_REQ, pos=(1350, -250)),
             cv.http_post_json(n["dh"], "D4 稳定性(sidecar)", f"{SIDECAR}/decide", {"X-Sidecar-Token": need("SIDECAR_TOKEN")},
                               "{{block_output_" + n["dq"] + ".body}}", pos=(1500, -250),
                               default_on_error={"body": "{}", "statusCode": 0, "headers": ""}),
             cv.code(n["c2"], "生成更新", [cv.param("out", cv.ref(n["l1"], "output")), cv.param("rows_json", cv.ref(n["c1"], "rows_json")),
                                         cv.param("d4", cv.ref(n["dh"], "body"))],
                     [cv.out("upd_items", "list", schema={"type": "string"}), cv.out("strategy"), cv.out("has_strategy", "boolean"),
                      cv.out("strat_ev"), cv.out("now"), cv.out("summary")], CODE_APPLY, pos=(1500, -100)),
             cv.batch(n["b1"], "批量更新", cv.ref(n["c2"], "upd_items", "list<string>"), [
                 cv.code(n["bu"], "解包", [cv.param("row", cv.ref(n["b1"], "items"))],
                         [cv.out(k) for k in ["id", "status", "mvalue", "support", "cons", "conf", "now"]], CODE_UNPACK, pos=(100, 100)),
                 cv.custom_sql(n["bs"], "更新", T["user_memory"], SQL_UPD,
                               [cv.param(k, cv.ref(n["bu"], k)) for k in ["id", "status", "mvalue", "support", "cons", "conf", "now"]],
                               [], pos=(400, 100))],
                 cv.batch_edges(n["b1"], [n["bu"], n["bs"]]), outputs=[{"name": "id", "input": cv.ref(n["bu"], "id", "list<string>")}],
                 batch_size=50, concurrent=1, pos=(1800, -100)),
             cv.selector(n["x2"], "有新策略？", [[(cv.ref(n["c2"], "has_strategy", "boolean"), "==", cv.lit(True, "boolean"))]],
                         pos=(2100, -100)),
             cv.custom_sql(n["s2"], "旧策略作废", T["user_memory"], SQL_STRAT_OLD, [cv.param("now", cv.ref(n["c2"], "now"))], [],
                           pos=(2400, -200)),
             cv.custom_sql(n["s3"], "写新策略", T["user_memory"], SQL_STRAT_INS,
                           [cv.param("v", cv.ref(n["c2"], "strategy")), cv.param("now", cv.ref(n["c2"], "now")),
                            cv.param("ev", cv.ref(n["c2"], "strat_ev"))], [], pos=(2700, -200)),
             cv.aggregator(n["agg"], "汇总", {"summary": ("string", [cv.ref(n["c2"], "summary"), cv.ref(n["c1"], "summary")])},
                           pos=(3000, 0)),
             cv.exit_node([cv.param("summary", cv.ref(n["agg"], "summary"))], pos=(3300, 0))]
    e = cv.edge
    edges = [e(S, n["s1"]), e(n["s1"], n["c1"]), e(n["c1"], n["x1"]),
             e(n["x1"], n["l1"], "true"), e(n["l1"], n["dq"]), e(n["dq"], n["dh"]), e(n["dh"], n["c2"]), e(n["c2"], n["b1"]),
             e(n["b1"], n["x2"]),
             e(n["x2"], n["s2"], "true"), e(n["s2"], n["s3"]), e(n["s3"], n["agg"]), e(n["x2"], n["agg"], "false"),
             e(n["x1"], n["agg"], "false"), e(n["agg"], cv.EXIT_ID)]
    return cv.canvas(nodes, edges)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    c = Coze()
    c.login()
    ids = json.loads(IDS_FILE.read_text(encoding="utf-8"))
    ids.setdefault("workflows", {})
    if "consolidate" not in ids["workflows"]:
        ids["workflows"]["consolidate"] = create_workflow(c, "la_consolidate", "长期记忆异步整理：归组、提升、策略改写")
        IDS_FILE.write_text(json.dumps(ids, ensure_ascii=False, indent=1), encoding="utf-8")
        print("建工作流", ids["workflows"]["consolidate"])
    wid = ids["workflows"]["consolidate"]
    if cmd in ("build", "release"):  # release 也先按本地代码重建保存（10-21 前只发布草稿，v0.0.5 因此和 v0.0.4 相同）
        schema = build(ids)
        print("保存:", save_workflow(c, wid, schema).get("code"))
        (ROOT / "canvases").mkdir(exist_ok=True)
        (ROOT / "canvases" / "wf_consolidate.json").write_text(schema.replace(need("SIDECAR_TOKEN"), "<SIDECAR_TOKEN>"),
                                                               encoding="utf-8")
    elif cmd == "run":
        d = test_run(c, wid, {"noop": "1"}, bot_id=ids["bot_id"], timeout_s=300)
        print(summarize_process(d)[-6000:])
    if cmd == "release":
        d = test_run(c, wid, {"noop": "1"}, bot_id=ids["bot_id"], timeout_s=300)
        if d.get("executeStatus") != 2:
            print(summarize_process(d)[-2000:])
            raise SystemExit("调试未通过，不发布")
        print(publish_workflow(c, wid, version=sys.argv[2], desc="WF-consolidate"))


if __name__ == "__main__":
    main()
