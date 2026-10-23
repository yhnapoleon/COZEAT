"""CF-main 和各子工作流共用的东西：模型、意图、行结构、代码节点源码、SQL。

代码节点源码里的 Python 跑在 Coze 沙箱里（async def main(args: Args) -> Output），不在本机执行。
"""
import inspect
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import canvas as cv  # noqa: E402
from common.cate_tax import MAJORS, query_map, query_res  # noqa: E402

PROMPTS = HERE.parent / "prompts"
# 所有 LLM 节点和意图节点用的模型（主干、子工作流、WF-consolidate 都从这里取）；id 用 bridge/register_model.py list 查。
# 10-20 起换成 DeepSeek V4.1 Flash；换回 Claude 改成 100002, "Claude Opus 5.5（经 LiteLLM 网关）"
MODEL_TYPE, MODEL_NAME = 100003, "DeepSeek V4.1 Flash（经 LiteLLM 网关）"
SIDECAR = "http://host.docker.internal:8090"

INTENTS = [  # 顺序即 classificationId 1..8、端口 branch_0..branch_7
    "找餐厅：想找吃饭的地方，问附近有什么好吃的、推荐餐厅",
    "选套餐：问团购、套餐、优惠券、双人餐",
    "对比评价：比较两家或几家店，问某家店怎么样、口碑如何",
    "路线规划：问怎么去、多远、要多久",
    "看榜单：问热门、排行榜、必吃榜、心动榜、网红店",
    "FAQ：团购券使用、退款、过期、预约、发票等平台规则问题",
    "偏好管理：让助手记住或忘掉自己的口味、预算、忌口",
    "闲聊：与吃饭找店无关的话",
]

ROW_SCHEMA = [cv.out(n, "string") for n in ["amap_id", "name", "cate", "std_cate", "biz_area", "rating", "cost", "dishes", "dist_m",
                                              "deals", "tags", "note_cnt", "kw_hit", "cost_ok", "kb_pos",
                                              "lng", "lat", "address"]]
STATE_SCHEMA = [cv.out(n, "string") for n in ["slots", "turn", "last_shown", "stage", "pending_q"]]
PROF_SCHEMA = [cv.out(n, "string") for n in ["id", "kind", "mkey", "mvalue", "status", "ev", "conv"]]

CODE_PREV = r'''
import json
async def main(args: Args) -> Output:
    rows = args.params.get("rows") or []
    prev, turn, shown, stage, pq = {}, 0, [], "", ""
    if rows:
        r = rows[0]
        stage, pq = str(r.get("stage") or ""), str(r.get("pending_q") or "")
        try:
            prev = json.loads(r.get("slots") or "{}")
        except Exception:
            prev = {}
        try:
            turn = int(float(r.get("turn") or 0))
        except Exception:
            turn = 0
        try:
            shown = [x for x in json.loads(r.get("last_shown") or "[]") if isinstance(x, dict)]
        except Exception:
            shown = []
    lines = [str(i + 1) + ". " + str(x.get("name") or "") + "（" + str(x.get("cate") or "") + "，人均 " + str(x.get("cost") or "") + "）"
             for i, x in enumerate(shown)]
    return {"prev_slots": json.dumps(prev, ensure_ascii=False), "turn": turn,
            "shown": json.dumps(shown, ensure_ascii=False), "shown_text": chr(10).join(lines) or "（无）",
            "stage": stage, "pending_q": pq}
'''

# 长期记忆（设计文档 4.6）：每轮读 active 偏好和策略拼成画像；记忆抽取和意图识别并行，汇合后决定写哪些行。
# 本会话里用户改口推翻过的条目（记忆抽取给了 target_id，那一轮写成本会话的候选行）本会话内不再当默认值：
# prof_json / profile 是生效画像，跳过它们；active_json / prof_lines 是记忆管理用的完整视图，保留并标注。
# 长期记忆本身照旧等异步整理（10-21，问题 2）
CODE_PROFILE = r'''
import json
import re
async def main(args: Args) -> Output:
    rows = args.params.get("rows") or []
    conv = str(args.params.get("conv") or "")
    NAMES = {"avoid": "忌口", "taste": "口味", "cuisine_like": "喜欢", "cuisine_dislike": "不喜欢", "budget": "人均上限",
             "area": "常去", "scene": "常见场景"}
    prof = {"avoid": [], "taste": [], "cuisine_like": [], "cuisine_dislike": [], "scene": [], "budget": 0, "area": "",
            "strategy": ""}
    off = set()
    for r in rows:
        if (r.get("status") or "") == "candidate" and conv and str(r.get("conv") or "") == conv:
            try:
                tid = str(json.loads(r.get("ev") or "{}").get("target_id") or "")
            except Exception:
                tid = ""
            if tid:
                off.add(tid)
    active, lines, pending = [], [], 0
    for r in rows:  # 按 id 倒序，单值的 budget、area 取最新一条
        kind, k, v, st = r.get("kind") or "", r.get("mkey") or "", str(r.get("mvalue") or "").strip(), r.get("status") or ""
        if st == "candidate":
            pending += kind == "pref"
            continue
        if st != "active" or not v:
            continue
        if kind == "strategy":
            prof["strategy"] = prof["strategy"] or v
            continue
        if kind != "pref" or k not in NAMES:
            continue
        rid = str(r.get("id"))
        active.append({"id": rid, "key": k, "value": v})
        lines.append("[" + rid + "] " + k + "=" + v + ("（本会话已改口）" if rid in off else ""))
        if rid in off:
            continue
        if k == "budget":
            digits = re.findall(r"\d+", v)
            if digits and not prof["budget"]:
                prof["budget"] = int(digits[0])
        elif k == "area":
            prof["area"] = prof["area"] or v
        elif v not in prof[k]:
            prof[k].append(v)
    parts = [NAMES[k] + " " + "、".join(prof[k]) for k in ["avoid", "taste", "cuisine_like", "cuisine_dislike", "scene"] if prof[k]]
    if prof["area"]:
        parts.append("常去 " + prof["area"])
    if prof["budget"]:
        parts.append("人均上限 " + str(prof["budget"]) + " 元")
    text = "；".join(parts)
    if prof["strategy"]:
        text = (text + "。" if text else "") + "推荐策略：" + prof["strategy"]
    return {"profile": text or "（空）", "prof_lines": chr(10).join(lines) or "（无）",
            "prof_json": json.dumps(prof, ensure_ascii=False), "active_json": json.dumps(active, ensure_ascii=False),
            "pending": pending, "off_ids": ",".join(sorted(off))}
'''

CODE_JOIN = r'''
import json
import re
import datetime
async def main(args: Args) -> Output:
    p = args.params
    now = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).isoformat(timespec="seconds")  # 沙箱是 UTC，存北京时间
    conv = p.get("conv") or ""
    KEYS = {"avoid": "忌口", "taste": "口味", "cuisine_like": "喜欢", "cuisine_dislike": "不喜欢", "budget": "人均上限",
            "area": "常去", "scene": "常见场景"}
    SINGLE = ("budget", "area")
    def load(s, d):
        try:
            return json.loads(s) if s else d
        except Exception:
            return d
    m = re.search(r"\{.*\}", p.get("mem") or "", re.S)
    mem = load(m.group(0), {}) if m else {}
    shown = load(p.get("shown"), [])
    active = load(p.get("active_json"), [])
    def f(x):
        try:
            return max(0.0, min(1.0, float(x)))
        except Exception:
            return 0.0
    def obj(x):
        return x if isinstance(x, dict) else {}
    # 决策层（设计文档 4.5）：D2 意图、D1 反馈走 sidecar /decide；它超时或出错时，意图用 Coze 意图节点、反馈用记忆抽取的判断
    KEYS_I = ["rec", "deal", "cmp", "nav", "hot", "faq", "pm", "chat"]  # 下标 + 1 = Coze 意图编号
    NAMES_I = {"rec": "找附近的餐厅", "deal": "看团购套餐", "cmp": "比较几家店", "nav": "查怎么去", "hot": "看热门榜单",
               "faq": "问团购券怎么用、能不能退", "pm": "让我记住或忘掉你的口味"}
    TAU, FB_TAU = 0.6, 0.7  # D2、D1 的初始阈值，没校准过
    ans = obj(obj(load(p.get("dec"), {})).get("answers"))
    it, un = obj(ans.get("intent")), obj(ans.get("unclear"))
    coze_cid = int(p.get("cid") or 0)
    if it.get("choice") in KEYS_I:
        cid, src, conf = KEYS_I.index(it["choice"]) + 1, "decide", f(it.get("confidence"))
    else:
        cid, src, conf = coze_cid, "coze", 1.0
    unclear = f(un.get("noul"))
    clarify, clarify_card = "", ""
    # 意图拿不准就先问；上一轮刚追问过（槽位追问或意图澄清）时不再追问，按最可能的走
    if src == "decide" and (conf < TAU or unclear >= TAU) and (p.get("stage") or "") not in ("collect", "clarify"):
        probs = sorted(((k, f(v)) for k, v in obj(it.get("probabilities")).items() if k in NAMES_I), key=lambda kv: -kv[1])
        top = [k for k, v in probs if v >= 0.05][:3] or ["rec", "hot", "nav"]
        opts = NAMES_I[top[0]] if len(top) == 1 else "、".join(NAMES_I[k] for k in top[:-1]) + "，还是" + NAMES_I[top[-1]]
        clarify = "没太明白你想做什么：是想" + opts + "？说一句就行，比如“海岸城附近的火锅”“海岸城有什么热门店”。"
        # 选项按钮（网页 Demo，10-22）：每个候选意图给一句能直接发出去的话，地点用槽位提取认出的地方，没有就海岸城
        g = re.search(r'"area"\s*:\s*"([^"]*)"', p.get("slot_raw") or "")
        loc = (g.group(1) if g else "") or "海岸城"
        SAY = {"rec": loc + "附近有什么好吃的", "deal": loc + "有什么划算的团购", "hot": loc + "有什么热门的店",
               "faq": "团购券过期了还能退吗", "pm": "看看你记住了我哪些偏好",
               "cmp": "第一家和第二家哪个好" if len(shown) >= 2 else "", "nav": "第一家怎么去" if shown else ""}
        says = [SAY[k] for k in top if SAY.get(k)]
        for k in ["rec", "hot"]:
            if len(says) < 2 and SAY[k] not in says:
                says.append(SAY[k])
        clarify_card = "```card" + chr(10) + json.dumps({"type": "options", "items": says[:3]}, ensure_ascii=False) + chr(10) + "```"
    fa, fb_src, fbs = obj(ans.get("feedback")), "decide", []
    if fa.get("choice") in ("accept", "reject", "neutral"):
        if fa["choice"] != "neutral" and f(fa.get("confidence")) >= FB_TAU:
            why = next((str(x.get("reason") or "") for x in mem.get("feedback") or []
                        if isinstance(x, dict) and x.get("label") == fa["choice"]), "") or str(p.get("query") or "")
            fbs.append({"idx": obj(ans.get("target")).get("choice") or 0, "label": fa["choice"], "reason": why})
    else:
        fb_src, fbs = "memory", mem.get("feedback") or []
    def row(kind, k, v, status, conf, ev):
        return json.dumps({"kind": kind, "mkey": k, "mvalue": v, "status": status, "support": 1, "cons": 1.0,
                           "conf": round(conf, 2), "ev": json.dumps(ev, ensure_ascii=False), "conv": conv, "now": now},
                          ensure_ascii=False)
    def drop(ids):
        nonlocal active
        for i in ids:
            dels.append(json.dumps({"id": i, "now": now}))
        active = [a for a in active if a.get("id") not in ids]
    ins, dels, done, n_cand = [], [], [], 0
    for x in mem.get("prefs") or []:
        if not isinstance(x, dict):
            continue
        k, v = str(x.get("key") or ""), str(x.get("value") or "").strip()
        if k not in KEYS:
            continue
        op, tid = x.get("op") or "add", str(x.get("target_id") or "")
        explicit, dur, text = bool(x.get("explicit")), f(x.get("durable")), str(x.get("evidence") or "")[:100]
        if explicit and op == "remove":
            old = next((a for a in active if a.get("id") == tid), None)
            if old:
                drop([tid])
                done.append("已删除：" + KEYS.get(old["key"], k) + " " + old["value"])
            else:
                done.append("没找到这条偏好：" + KEYS[k] + " " + v)
            continue
        if not v:
            continue
        if explicit:
            # 用户直接要求记住：立即生效，替换被它推翻的旧条目
            if any(a.get("key") == k and a.get("value") == v for a in active):
                done.append("已经记着：" + KEYS[k] + " " + v)
                continue
            drop([a["id"] for a in active if a.get("id") == tid or (k in SINGLE and a.get("key") == k)])
            ins.append(row("pref", k, v, "active", 1.0, {"op": "add", "explicit": True, "text": text}))
            active.append({"id": "new", "key": k, "value": v})
            done.append("已记住：" + KEYS[k] + " " + v)
            continue
        if dur < 0.3:
            continue  # 只管本次的（"今天想吃辣"）留在会话槽位里，不进长期记忆
        ins.append(row("pref", k, v, "candidate", dur, {"op": op, "target_id": tid, "text": text}))
        n_cand += 1
    for fb in fbs:
        if not isinstance(fb, dict) or fb.get("label") not in ("accept", "reject") or not shown:
            continue
        if fb["label"] == "accept" and (p.get("stage") or "") == "accepted":
            continue  # 上一轮已确认选定并记过一次，接着问"怎么去"不再重复记
        try:
            idx = int(fb.get("idx") or 0)
        except Exception:
            idx = 0
        targets = shown if idx == 0 else ([shown[idx - 1]] if 0 < idx <= len(shown) else [])
        for t in targets:
            others = [{"name": o.get("name"), "cate": o.get("cate"), "cost": o.get("cost")} for o in shown if o is not t]
            ev = {"label": fb["label"], "name": t.get("name"), "cate": t.get("cate"), "cost": t.get("cost"),
                  "others": others if idx else [], "reason": str(fb.get("reason") or "")[:100]}
            ins.append(row("feedback", str(t.get("amap_id") or ""), fb["label"], "candidate", 1.0, ev))
    # 偏好管理的回复：本轮操作结果 + 当前生效的偏好
    NL = chr(10)
    groups = {}
    for a in active:
        groups.setdefault(a.get("key"), []).append(str(a.get("value")))
    cur = "；".join(KEYS[k] + " " + "、".join(vs) for k, vs in groups.items() if k in KEYS)
    reply = NL.join(done)
    if cur:
        reply += (NL + NL if reply else "") + "现在记着的偏好：" + cur + "。" + NL + "说“忘掉 XX”可以删掉其中一条。"
    else:
        reply += (NL + NL if reply else "") + "现在还没有记住你的偏好。可以直接告诉我，比如“记住我不吃香菜”“我常在海岸城一带”。"
    pend = int(p.get("pending") or 0) + n_cand
    if pend:
        reply += NL + "另外有 " + str(pend) + " 条从聊天里听到的偏好待确认，多次出现或你明确说过之后会记上。"
    route = "ask" if clarify else {1: "rec", 2: "rec", 3: "cmp", 4: "nav", 5: "hot", 7: "pm"}.get(cid, "other")
    # D1 判成"选定上轮第 N 家"（"第二家不错，就它了"）：回一句确认，会话里的上轮推荐收窄成这一家，
    # 接着问"怎么去""它怎么样"就指它。不这样的话意图多半落到找餐厅，会重新推荐一轮、把上轮列表换掉
    acc_reply, acc_shown = "", "[]"
    if fb_src == "decide" and fbs and fbs[0].get("label") == "accept" and cid in (1, 2, 3, 8):
        try:
            k = int(fbs[0].get("idx") or 0)
        except Exception:
            k = 0
        if 0 < k <= len(shown):
            t = shown[k - 1]
            route, acc_shown = "acc", json.dumps([t], ensure_ascii=False)
            acc_reply = ("好，就定 **" + str(t.get("name") or "") + "**。" + NL + "要去的话问我“怎么去”就行，可以带上出发地，"
                         "比如“从科技园过去”；想再确认口碑可以问“它怎么样”。")
    # 问上轮某一家的团购（"第一家有团购吗"，选定一家后问"有团购吗"）：认店后直接答这家的团购（10-22）。
    # 以前意图落到选套餐、重新推荐一轮；D1 也可能把它判成选定，所以放在确认选择后面覆盖掉
    q_txt = str(p.get("query") or "")
    if (shown and cid in (1, 2, 3, 8) and re.search(r"团购|套餐|优惠|券|代金", q_txt)
            and (re.search(r"第\s*[一二三123]\s*[家个]|这家|那家|它家|这个店|这店", q_txt)
                 or (len(shown) == 1 and (p.get("stage") or "") == "accepted"))):
        route, acc_reply, acc_shown = "dl", "", "[]"
        ins = [x for x in ins if '"kind": "feedback"' not in x]  # 问团购不算选定，不记反馈
    note = (src + " cid=" + str(cid) + " conf=" + str(round(conf, 2)) + " unclear=" + str(round(unclear, 2))
            + " coze_cid=" + str(coze_cid) + " fb=" + fb_src + ":" + str(len(fbs)))
    # empty 恒为空串，给主干汇总节点各组当最后一项兜底（这个子工作流一定执行，值非 nil）
    return {"route": route, "cid": cid, "clarify": clarify, "clarify_ans": (clarify + NL + clarify_card) if clarify else "",
            "dec_note": note, "ins_items": ins, "del_items": dels,
            "pm_reply": reply, "acc_reply": acc_reply, "acc_shown": acc_shown, "empty": ""}
'''

# D2 意图 + D1 反馈合成一次 /decide 请求（Jev 的 choice / noul 格式），由 HTTP 节点原样 POST
CODE_DEC_REQ = r'''
import json
async def main(args: Args) -> Output:
    p = args.params
    try:
        shown = [x for x in json.loads(p.get("shown") or "[]") if isinstance(x, dict)]
    except Exception:
        shown = []
    state = {"user_input": p.get("query") or "", "last_shown": p.get("shown_text") or "（无）"}
    if (p.get("stage") or "") in ("collect", "clarify") and p.get("pending_q"):
        state["last_question"] = p.get("pending_q")  # 上一轮助手刚追问过，这句多半是在回答它
    qs = {"intent": {"type": "choice", "instructions": "用户这句话想做什么。state 里有 last_question 时，先看这句是不是在回答它",
                     "criteria": __INTENT_CRITERIA__},
          "unclear": {"type": "noul",
                      "instructions": "这句话太含糊，得先问用户想做什么才能继续。这句是在回答 last_question 时为假"}}
    if shown:
        qs["feedback"] = {"type": "choice", "instructions": "用户对 last_shown（上一轮推荐的店）的反应",
                          "criteria": {"accept": "选定、想去、问怎么去、说不错", "reject": "嫌贵、嫌远、不想吃、要换一批",
                                       "neutral": "没表态，或在问别的"}}
        crit = {"0": "没指明哪家，或针对全部"}
        for i, x in enumerate(shown[:3]):
            crit[str(i + 1)] = "第 " + str(i + 1) + " 家：" + str(x.get("name") or "")
        qs["target"] = {"type": "choice", "instructions": "用户的反应针对上一轮的哪一家", "criteria": crit}
    return {"body": json.dumps({"point": "D2_D1", "state": state, "questions": qs}, ensure_ascii=False)}
'''.replace("__INTENT_CRITERIA__", json.dumps(dict(zip(["rec", "deal", "cmp", "nav", "hot", "faq", "pm", "chat"], INTENTS)),
                                             ensure_ascii=False))

# 各分支的回答和要写的会话状态打成一包，交给汇总节点（变量聚合取第一个非 nil 的值）
CODE_PACK = r'''
async def main(args: Args) -> Output:
    p = args.params
    keys = [("answer", ""), ("stage", ""), ("slots", "{}"), ("cands", "[]"), ("shown", "[]"), ("q", ""), ("turn", "0")]
    return {k: str(p.get(k) if p.get(k) is not None else d) for k, d in keys}
'''

# WF-memwrite：stage 为空的分支（对比、路线、榜单、偏好管理、闲聊）不动会话状态
CODE_MW_PREP = r'''
import datetime
async def main(args: Args) -> Output:
    p = args.params
    now = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).isoformat(timespec="seconds")  # 沙箱是 UTC，存北京时间
    try:
        turn = str(int(float(p.get("turn") or 0)))
    except Exception:
        turn = "0"
    stage = str(p.get("stage") or "")
    return {"do_state": bool(stage), "stage": stage, "slots": str(p.get("slots") or "{}"), "cands": str(p.get("cands") or "[]"),
            "shown": str(p.get("shown") or "[]"), "q": str(p.get("q") or ""), "turn": turn, "now": now}
'''

CODE_MEM_UNPACK = r'''
import json
async def main(args: Args) -> Output:
    try:
        d = json.loads(args.params.get("row") or "{}")
    except Exception:
        d = {}
    return {k: str(d.get(k, "")) for k in ["kind", "mkey", "mvalue", "status", "support", "cons", "conf", "ev", "conv", "now", "id"]}
'''

CODE_UID = r'''
async def main(args: Args) -> Output:
    rows = args.params.get("rows") or []
    return {"uid": str((rows[0] or {}).get("u") or "") if rows else ""}
'''

CODE_MERGE = r'''
import json
import re
import datetime
__QRES__
async def main(args: Args) -> Output:
    p = args.params
    try:
        prev = json.loads(p.get("prev_slots") or "{}")
    except Exception:
        prev = {}
    # 槽位提取用文本输出、这里解析：JSON 输出模式的 LLM 节点偶发不回报完成，整轮对话挂住（2026-10-20 两次）
    m = re.search(r"\{.*\}", p.get("raw") or "", re.S)
    try:
        sl = json.loads(m.group(0)) if m else {}
    except Exception:
        # 整段解析失败时逐个字段捞：模型在 question 里用了英文双引号，整段作废、"人均一百以内"的预算也丢了（10-21 E1 s09）
        txt, sl = (m.group(0) if m else ""), {}
        for k in ["budget_min", "budget_max", "dist_max", "people"]:
            g = re.search('"' + k + r'"\s*:\s*(\d+)', txt)
            if g:
                sl[k] = int(g.group(1))
        for k in ["area", "category", "taste", "scene", "need"]:
            g = re.search('"' + k + r'"\s*:\s*"([^"]*)"', txt)
            if g:
                sl[k] = g.group(1)
        for k in ["delegate", "need_clarify"]:
            g = re.search('"' + k + r'"\s*:\s*(true|false)', txt)
            if g:
                sl[k] = g.group(1)
        g = re.search(r'"question"\s*:\s*"(.*?)"\s*,\s*"options"', txt, re.S) or re.search(r'"question"\s*:\s*"(.*)"\s*\}', txt, re.S)
        if g:
            sl["question"] = g.group(1).replace('"', "")
        g = re.search(r'"options"\s*:\s*\[(.*?)\]', txt, re.S)
        if g:
            sl["options"] = re.findall(r'"([^"]+)"', g.group(1))
    def num(x):
        try:
            return int(float(x))
        except Exception:
            return 0
    new = {"area": str(sl.get("area") or ""), "category": str(sl.get("category") or ""), "budget_max": num(sl.get("budget_max")),
           "people": num(sl.get("people")), "scene": str(sl.get("scene") or ""), "taste": str(sl.get("taste") or ""),
           "dist_max": num(sl.get("dist_max")), "need": str(sl.get("need") or "")}
    bmin, bmax = num(sl.get("budget_min")), new["budget_max"]
    if bmin and bmax and bmin > bmax:
        bmin, bmax = bmax, bmin
        new["budget_max"] = bmax
    merged = dict(prev)
    for k, v in new.items():
        if v not in ("", 0, None):
            merged[k] = v
    # 预算是一个区间，本轮说了任何一端就整体替换（上轮"一两百"、本轮"200 以内"时不能留着下限 100）
    if bmin or bmax:
        merged["budget_min"], merged["budget_max"] = bmin, bmax
        if not bmax:
            merged.pop("budget_max", None)
        if not bmin:
            merged.pop("budget_min", None)
    # 画像只做默认值：本轮和会话里没说的才用，不写回会话槽位；忌口总是带上
    try:
        prof = json.loads(p.get("prof_json") or "{}")
    except Exception:
        prof = {}
    # 本轮改口（10-21，问题 2）：记忆抽取给了 target_id 的画像条目，不管是不是用户明说要忘掉，本轮起就不当默认值。
    # 这个节点和记忆抽取串行（有一条从记忆抽取进来的边），之后的轮次由画像摘要按本会话的候选行跳过
    mm = re.search(r"\{.*\}", p.get("mem") or "", re.S)
    try:
        mem = json.loads(mm.group(0)) if mm else {}
    except Exception:
        mem = {}
    try:
        active = {str(a.get("id")): a for a in json.loads(p.get("active_json") or "[]") if isinstance(a, dict)}
    except Exception:
        active = {}
    off = []
    for x in (mem.get("prefs") if isinstance(mem, dict) else None) or []:
        tid = str(x.get("target_id") or "") if isinstance(x, dict) else ""
        if tid in active:
            k, v = active[tid].get("key"), str(active[tid].get("value") or "")
            if k == "budget":
                prof["budget"] = 0
            elif k == "area":
                prof["area"] = ""
            elif isinstance(prof.get(k), list):
                prof[k] = [y for y in prof[k] if y != v]
            off.append(tid)
    # 兜底：抽取漏给 target_id 时，本轮口味里正面提到的东西（"能吃辣"）不再按画像忌口排除
    cur_taste = str(new.get("taste") or "")
    if cur_taste and not re.search(r"[不少别怕微]", cur_taste):
        prof["avoid"] = [x for x in prof.get("avoid") or [] if x not in cur_taste]
    eff, notes = dict(merged), []
    if not eff.get("area") and prof.get("area"):
        eff["area"] = prof["area"]
        notes.append("常去" + prof["area"])
    if not eff.get("budget_max") and not eff.get("budget_min") and prof.get("budget"):
        eff["budget_max"] = prof["budget"]
        notes.append("人均 " + str(prof["budget"]) + " 以内")
    tastes = [str(merged.get("taste") or "")] if merged.get("taste") else list(prof.get("taste") or [])
    if not merged.get("taste") and prof.get("taste"):
        notes.append("口味" + "、".join(prof["taste"]))
    if prof.get("avoid"):
        tastes += ["不吃" + x for x in prof["avoid"]]
        notes.append("不吃" + "、".join(prof["avoid"]))
    eff["taste"] = "、".join(t for t in tastes if t)
    # 生效画像的文字版（给推荐生成、对比生成），写法和画像摘要一致
    NAMES = {"avoid": "忌口", "taste": "口味", "cuisine_like": "喜欢", "cuisine_dislike": "不喜欢", "scene": "常见场景"}
    parts = [NAMES[k] + " " + "、".join(prof[k]) for k in NAMES if prof.get(k)]
    if prof.get("area"):
        parts.append("常去 " + str(prof["area"]))
    if prof.get("budget"):
        parts.append("人均上限 " + str(prof["budget"]) + " 元")
    profile_eff = "；".join(parts)
    if prof.get("strategy"):
        profile_eff = (profile_eff + "。" if profile_eff else "") + "推荐策略：" + str(prof["strategy"])
    need = not (eff.get("area") or eff.get("category"))
    # 条件追问只问一次（10-21，问题 3）：上一轮已经追问过条件（stage=collect）还是没给，或者用户说"你看着办"，
    # 就不再问：地点用海岸城（画像常去的已在上面填过），品类放开，推荐生成挑不同品类，回答开头说明假设
    delegate = str(sl.get("delegate")).lower() == "true"
    assume = []
    if need and (str(p.get("stage") or "") == "collect" or delegate):
        eff["area"] = "海岸城"
        assume = ["地点先按海岸城一带（你没说在哪）", "品类不限，挑了几种不同的"]
        eff["_assume"] = "；".join(assume)
        need = False
    budget, bmin_eff, dist_max = num(eff.get("budget_max")), num(eff.get("budget_min")), num(eff.get("dist_max"))
    if budget <= 0:
        budget = 99999
    q = str(sl.get("question") or "") or "想在哪一带吃，想吃点什么？比如：海岸城吃火锅、深圳湾万象城吃西餐。"
    # 追问的选项做成按钮（网页 Demo，10-22）：点了等于用户说了这句；末尾固定给一个"你看着办"
    opts = [str(x).strip() for x in (sl.get("options") if isinstance(sl.get("options"), list) else []) if str(x).strip()][:3]
    if len(opts) < 2:
        opts = ["海岸城吃火锅", "深圳湾万象城吃西餐", "科技园吃粤菜"]
    opts.append("都行，你看着办")
    q_ans = q + chr(10) + "```card" + chr(10) + json.dumps({"type": "options", "items": opts}, ensure_ascii=False) + chr(10) + "```"
    now =(datetime.datetime.utcnow() + datetime.timedelta(hours=8)).isoformat(timespec="seconds")  # 沙箱是 UTC，存北京时间
    # 场景归一成门店 tags 的标准标签，同义词表和 etl/shop_tags.py 一致
    syn = {"约会": ["约会", "情侣", "二人食", "浪漫", "氛围"], "带老人": ["带老人", "家庭", "长辈", "爸妈", "老人"],
           "带娃": ["带娃", "亲子", "儿童", "小朋友", "宝宝"], "聚餐": ["聚餐", "聚会", "团建"],
           "一人食": ["一人食", "一个人", "工作餐"], "生日": ["生日", "庆祝"], "商务": ["商务", "宴请", "包间"],
           "拍照": ["拍照", "出片", "漂亮饭"], "下午茶": ["下午茶"]}
    sc = str(merged.get("scene") or "")
    scene_tag = next((k for k, ws in syn.items() if any(w in sc for w in ws)), "")
    # 品类展开（10-21）：用户说的词按标准品类表（common/cate_tax.py，构建时内联成 QMAP）映射到节点，
    # SQL 召回用三个正则：shop.std_cate 按节点比（说大类就命中全部细类）、店名和高德品类比品类词、推荐菜只比菜名。
    # 表里没有的词（"猪手""椰子水"）退回原词子串匹配，和以前的 LIKE 一样。query_res 是构建时从 cate_tax 内联进来的源码
    QMAP = __QMAP__
    kw = str(merged.get("category") or "").strip()
    cate_re, name_re, dish_re, dish_w, kb_w = query_res(kw, QMAP)
    # 跨店召回（10-21）：用户对店本身的要求（need，"不用排队""能拍照"）或场景不为空，或者点了具体的菜、说了表外的词时，
    # 拿"品类 + 要求 + 场景"去 KB-review 里跨店检索。只说品类节点（"火锅"）时不开：SQL 按标准品类已经能召回
    need_txt, scene_txt = str(merged.get("need") or ""), str(merged.get("scene") or "")
    kb_on = bool(need_txt or scene_txt or kb_w != "0")
    kb_q = " ".join(x for x in [kw, need_txt.replace("、", " "), scene_txt] if x) if kb_on else "无"
    # 每个生效条件从哪来（10-23，网页 Demo 的"为什么是这几家"）：本轮 / 本会话（之前几轮说的）/ 长期偏好 / 默认假设
    def src(k):
        return "本轮" if new.get(k) not in ("", 0, None) else "本会话" if prev.get(k) not in ("", 0, None) else ""
    conds = []
    if eff.get("area"):
        conds.append({"k": "地点", "v": eff["area"], "src": "默认假设" if assume else (src("area") or "长期偏好")})
    if merged.get("category"):
        conds.append({"k": "品类", "v": merged["category"], "src": src("category")})
    if bmin or bmax:
        conds.append({"k": "预算", "v": ("人均 " + str(bmin) + "–" + str(bmax)) if bmin and bmax else
                      ("人均 " + str(bmin) + " 以上") if bmin else ("人均 " + str(bmax) + " 以内"), "src": "本轮"})
    elif merged.get("budget_max") or merged.get("budget_min"):
        conds.append({"k": "预算", "v": "人均 " + str(merged.get("budget_max") or merged.get("budget_min")) + (" 以内" if merged.get("budget_max") else " 以上"), "src": "本会话"})
    elif eff.get("budget_max"):
        conds.append({"k": "预算", "v": "人均 " + str(eff["budget_max"]) + " 以内", "src": "长期偏好"})
    for k, name in [("dist_max", "距离"), ("scene", "场景"), ("need", "要求")]:
        if merged.get(k):
            conds.append({"k": name, "v": (str(merged[k]) + " 米内") if k == "dist_max" else str(merged[k]), "src": src(k)})
    if merged.get("taste"):
        conds.append({"k": "口味", "v": str(merged["taste"]), "src": src("taste")})
    elif prof.get("taste"):
        conds.append({"k": "口味", "v": "、".join(prof["taste"]), "src": "长期偏好"})
    for x in prof.get("avoid") or []:
        conds.append({"k": "忌口", "v": "不吃" + str(x), "src": "长期偏好"})
    for x in off:
        a = active.get(x) or {}
        conds.append({"k": "改口", "v": "本轮不再用：" + str(a.get("value") or ""), "src": "本轮"})
    note = "；".join(x for x in [("按你的长期偏好：" + "、".join(notes)) if notes else "",
                                 ("我先这样假设：" + "，".join(assume) + "。想换地方或品类直接说") if assume else ""] if x)
    return {"scene_tag": scene_tag, "area_q": eff.get("area") or "", "kw": kw, "budget": str(budget),
            "bmin": str(bmin_eff), "dist_max": str(dist_max), "cate_re": cate_re, "name_re": name_re, "dish_re": dish_re,
            "dish_w": dish_w, "kb_q": kb_q, "kb_on": "1" if kb_on else "0", "kb_w": kb_w,
            "taste": eff["taste"], "slots_eff": json.dumps(eff, ensure_ascii=False), "profile_note": note,
            "profile_eff": profile_eff or "（空）", "off_ids": ",".join(off), "conds_json": json.dumps(conds, ensure_ascii=False),
            "slots_json": json.dumps(merged, ensure_ascii=False), "need_clarify": need,
            "question": q if need else "", "question_ans": q_ans if need else "",
            "turn": str(int(p.get("turn") or 0) + 1), "now": now}
'''.replace("__QMAP__", json.dumps(query_map(), ensure_ascii=False)).replace("__QRES__", inspect.getsource(query_res))

CODE_GEO = r'''
import json
async def main(args: Args) -> Output:
    try:
        d = json.loads(args.params.get("body") or "{}")
    except Exception:
        d = {}
    return {"lng": str(d.get("lng") or 113.937635), "lat": str(d.get("lat") or 22.518308),
            "anchor": (d.get("name") or "海岸城").replace("广东省", "").replace("深圳市", ""),
            "geo_fallback": bool(d.get("fallback", True))}
'''

def _qual() -> dict:
    """data/processed/shop_quality.json（etl/note_quality.py 生成）压成 amap_id → [笔记口碑分, 可信度, 笔记数, 疑似推广数]。"""
    p = HERE.parent / "data" / "processed" / "shop_quality.json"
    if not p.exists():
        return {}
    d = json.loads(p.read_text(encoding="utf-8"))
    return {k: [v.get("ugc_score"), v.get("cred") if v.get("cred") is not None else 1.0, v.get("n_notes") or 0, v.get("n_ad") or 0]
            for k, v in d.items()}


CODE_TRIM = r'''
import json
async def main(args: Args) -> Output:
    rows = args.params.get("rows") or []
    scene = args.params.get("scene") or ""
    def f(x, d=0.0):
        try:
            return float(x)
        except Exception:
            return d
    kw = args.params.get("kw") or ""
    def deals_of(x):
        # 团购最多留 2 个；synthetic=true 是合成的示例数据（etl/deals.py）
        try:
            ds = json.loads(x or "[]")
        except Exception:
            ds = []
        return [{k: d.get(k) for k in ("name", "price", "orig_price", "content", "src", "synthetic")}
                for d in ds if isinstance(d, dict)][:2]
    MAJORS = __MAJORS__
    dmax = int(f(args.params.get("dist_max")))
    cands = []
    for r in rows:
        # 标准品类 "|西餐|意大利菜|"：展示用细类（没有细类用大类），分组去重用第一个大类；没打标的店退回高德品类
        toks = [t for t in (r.get("std_cate") or "").split("|") if t]
        groups = []  # 按组显示：有细类显示细类，没有就显示大类（"西餐 + 奶茶果茶"不能只显示奶茶果茶）
        for t in toks:
            if t in MAJORS or not groups:
                groups.append([t])
            else:
                groups[-1].append(t)
        std = "、".join("、".join(g[1:]) if len(g) > 1 else g[0] for g in groups)
        cands.append({"amap_id": r.get("amap_id"), "name": r.get("name"), "cate": std or r.get("cate"), "area": r.get("biz_area"),
                      "rating": f(r.get("rating")), "cost": f(r.get("cost")) or None, "dist_m": int(f(r.get("dist_m"), 99999)),
                      "dishes": (r.get("dishes") or "").replace("|", "、"), "tags": r.get("tags") or "",
                      "note_cnt": int(f(r.get("note_cnt"))), "deals": deals_of(r.get("deals")),
                      "lng": f(r.get("lng")), "lat": f(r.get("lat")), "addr": (r.get("address") or "")[:40],
                      "_major": next((t for t in toks if t in MAJORS), r.get("cate") or ""),
                      "_kw": f(r.get("kw_hit")) > 0, "_lvl": f(r.get("kw_hit")), "_cost": f(r.get("cost_ok")) > 0,
                      "_kb": int(f(r.get("kb_pos")))})
    # 逐级放宽：都满足 → 放宽距离 → 放宽预算 → 放宽品类 → 都放宽。距离只在用户说了范围（dist_max）时参与，SQL 本身取 5 公里内。
    # 用户点名了菜、且有店写明做这道菜（kw_hit=2）时，品类条件就是"做这道菜"，同品类但没写这道菜的店不算满足：
    # 宁可在这些店里放宽距离、预算（10-21：画像默认人均 100 把做意面的三家都筛掉，留下的是不卖意面的披萨店）
    need = 2 if any(c["_lvl"] >= 2 for c in cands) else 1
    hit = lambda c: c["_lvl"] >= need  # noqa: E731
    near = (lambda c: c["dist_m"] <= dmax) if dmax > 0 else (lambda c: True)
    # 筛选漏斗（网页 Demo 的"为什么是这几家"，10-23）：SQL 召回的店里各条件满足几家
    bmax_q, bmin_q = int(f(args.params.get("budget"))), int(f(args.params.get("bmin")))
    funnel = {"total": len(cands), "conds": []}
    if kw:
        funnel["conds"].append({"k": "品类", "v": kw, "ok": sum(1 for c in cands if hit(c))})
    if bmin_q or bmax_q < 99999:
        bt = ("人均 " + str(bmin_q) + "–" + str(bmax_q)) if bmin_q and bmax_q < 99999 else (
            "人均 " + str(bmin_q) + " 以上" if bmin_q else "人均 " + str(bmax_q) + " 以内")
        funnel["conds"].append({"k": "预算", "v": bt, "ok": sum(1 for c in cands if c["_cost"])})
    if dmax > 0:
        funnel["conds"].append({"k": "距离", "v": str(dmax) + " 米内", "ok": sum(1 for c in cands if near(c))})
    tiers = [(lambda c: hit(c) and c["_cost"] and near(c), ""), (lambda c: hit(c) and c["_cost"], "距离"),
             (lambda c: hit(c), "预算"), (lambda c: c["_cost"], "品类"), (lambda c: True, "品类和预算")]
    relaxed = ""
    for cond, note in tiers:
        sel = [c for c in cands if cond(c)]
        if sel:
            cands, relaxed = sel, note
            break
    if relaxed == "距离":
        relaxed = str(dmax) + " 米内没有符合条件的店，已放宽距离"
    elif relaxed:
        relaxed = "附近没有完全符合条件的店，已放宽" + relaxed + ("（没有标明「" + kw + "」的店）" if "品类" in relaxed and kw else "")
    for c in cands:
        c.pop("_kw", None)
        c.pop("_cost", None)
    funnel["after"] = len(cands)
    # 多维打分（10-23，网页 Demo 的评分明细）：每项 0–10 分，按权重求加权平均得综合分，排序就用它。
    # 只算用得上的维度：没说品类不算品类分，没说场景不算场景分，没有笔记的店不算笔记口碑和可信度；权重按算了的维度归一。
    # 口味只做软排序，决定哪些店进候选；是否真符合由推荐生成按推荐菜和证据判断。
    taste = args.params.get("taste") or ""
    LIGHT = ["粤", "广东", "潮汕", "椰子鸡", "粥", "日本料理", "寿司", "茶餐厅", "点心", "早茶", "蒸", "白切"]
    SPICY = ["川", "湘", "麻辣", "香辣", "烤鱼", "云贵", "冒菜", "串串", "酸菜鱼", "酸汤", "辣"]
    want_light = any(w in taste for w in ["清淡", "不辣", "不吃辣", "少辣", "不要太辣", "别太辣", "少油", "不油腻", "养生"])
    want_spicy = not want_light and "微辣" not in taste and any(w in taste for w in ["辣", "重口", "下饭"])
    kb_on = str(args.params.get("kb_on") or "0") == "1"
    QUAL = __QUAL__  # amap_id → [笔记口碑分 0–10, 可信度 0–1, 笔记数, 疑似推广数]，构建时从 data/processed/shop_quality.json 内联
    W = {"口碑评分": 0.20, "笔记口碑": 0.10, "距离": 0.15, "预算": 0.10, "品类": 0.15, "场景": 0.10, "口味": 0.10,
         "评价命中": 0.10, "热度": 0.05, "可信度": 0.05}
    taste_down, taste_up = [], []
    def clamp(x):
        return round(max(0.0, min(10.0, x)), 1)
    def dims(c):
        d = {"口碑评分": clamp((c["rating"] - 3.5) / 1.5 * 10)}
        q = QUAL.get(c["amap_id"])
        if q and q[0] is not None:
            d["笔记口碑"] = clamp(q[0])
        dm = c["dist_m"]
        d["距离"] = clamp(10 - max(0, dm - dmax) / dmax * 10) if dmax > 0 else clamp(10 - dm / 200)
        if bmin_q or bmax_q < 99999:
            cost = c["cost"] or 0
            if not cost:
                d["预算"] = 4.0
            elif cost > bmax_q:
                d["预算"] = clamp(10 - (cost - bmax_q) / max(bmax_q, 1) * 20)
            elif bmin_q and cost < bmin_q:
                d["预算"] = clamp(10 - (bmin_q - cost) / bmin_q * 20)
            else:
                d["预算"] = 10.0
        if kw:
            d["品类"] = 10.0 if c["_lvl"] >= 2 else 8.0 if c["_lvl"] >= 1 else 0.0
        if scene:
            d["场景"] = 10.0 if scene in c["tags"].split("|") else 3.0
        if want_light or want_spicy:
            text = (c["name"] or "") + (c["cate"] or "") + c["dishes"]
            light, spicy = any(w in text for w in LIGHT), any(w in text for w in SPICY)
            if want_light:
                d["口味"] = 0.0 if spicy else 10.0 if light else 6.0
            else:
                d["口味"] = 10.0 if spicy else 4.0
            if d["口味"] <= 4:
                taste_down.append(c["name"])
            elif d["口味"] >= 10:
                taste_up.append(c["name"])
        if kb_on:
            kb = c["_kb"]
            d["评价命中"] = 10.0 if 1 <= kb <= 3 else 7.0 if 4 <= kb <= 6 else 5.0 if kb >= 7 else 2.0
        d["热度"] = clamp(min(c["note_cnt"], 5) * 2)
        if q and q[2]:
            d["可信度"] = clamp(q[1] * 10)
        return d
    for c in cands:
        c["dims"] = dims(c)
        tw = sum(W[k] for k in c["dims"])
        c["score"] = round(sum(W[k] * v for k, v in c["dims"].items()) / tw, 2)
    cands.sort(key=lambda c: -c["score"])
    if want_light or want_spicy:
        funnel["taste"] = {"pref": "清淡、不吃辣" if want_light else "想吃辣", "down": taste_down[:6], "up": taste_up[:6]}
    funnel["weights"] = W
    top = cands[:12]  # 多取几家查证据，合并时按场景重排后再留 8 家给推荐生成
    if not kw:
        # 品类没限定（包括"你看着办"）：同一大类最多 2 家，推荐生成才挑得出不同品类（10-21）
        per, top = {}, []
        for c in cands:
            k = c["_major"] or ""
            if per.get(k, 0) < 2:
                per[k] = per.get(k, 0) + 1
                top.append(c)
            if len(top) == 12:
                break
    for c in cands:
        c.pop("_major", None)
        c.pop("_lvl", None)
        c.pop("_kb", None)
    items = []
    for c in top:
        brand = (c["name"] or "").split("(")[0].split("（")[0]
        items.append(json.dumps({"amap_id": c["amap_id"], "q": brand + " " + str(c["amap_id"])}, ensure_ascii=False))
    funnel["top"] = len(top)
    funnel["relaxed"] = relaxed
    return {"candidates_json": json.dumps(top, ensure_ascii=False), "count": len(cands), "cand_items": items,
            "relaxed": relaxed, "funnel": json.dumps(funnel, ensure_ascii=False)}
'''.replace("__MAJORS__", json.dumps(MAJORS, ensure_ascii=False)).replace("__QUAL__", repr(_qual()))

CODE_EVID_UNPACK = r'''
import json
async def main(args: Args) -> Output:
    try:
        d = json.loads(args.params.get("row") or "{}")
    except Exception:
        d = {}
    return {"amap_id": d.get("amap_id") or "", "kb_query": d.get("q") or ""}
'''

CODE_EVID_FILTER = r'''
import json
async def main(args: Args) -> Output:
    # KB-review 每段首行是 "amap_id | 店名 | 商圈 | 品类 | 来源 X | 日期"，按 amap_id 过滤掉别家的段
    p = args.params
    aid = p.get("amap_id") or ""
    snips, seen = [], set()
    for r in p.get("rows") or []:
        t = ((r.get("output") if isinstance(r, dict) else str(r)) or "").strip()
        if not t or not aid:
            continue
        lines = t.split(chr(10))
        head = [x.strip() for x in lines[0].split("|")]
        if not head or head[0] != aid:
            continue
        src = head[4].replace("来源", "").strip() if len(head) > 4 else ""
        date = head[5] if len(head) > 5 else ""
        quote = lines[1].strip() if len(lines) > 1 else ""
        facts = " ".join(x.strip() for x in lines[2:])[:300]
        if not quote and not facts:
            continue  # "求推荐"帖评论里只报了店名的提及，没有评价内容
        key = quote[:30] or facts[:30]
        if key in seen:
            continue
        seen.add(key)
        snips.append({"src": src, "date": date, "quote": quote[:200], "facts": facts})
    snips.sort(key=lambda x: x["date"], reverse=True)
    return {"evid": json.dumps({"amap_id": aid, "snips": snips[:3]}, ensure_ascii=False)}
'''

CODE_KB_CROSS = r'''
import json
async def main(args: Args) -> Output:
    # 跨店召回（10-21）：KB-review 按"品类 + 要求 + 场景"检索的结果，按段首 amap_id 收敛到店，保持检索顺序（打了检索补丁后按分数排），
    # 每家最多留 2 段。输出给 SQL 召回（kb_ids，只在 SQL 硬条件筛出的店里起作用）、裁剪候选（按名次加分）和合并证据（放在证据最前面）
    p = args.params
    if str(p.get("kb_on") or "0") != "1":
        return {"kb_ids": "", "kb_json": "[]"}
    shops, order = {}, []
    for r in p.get("rows") or []:
        t = ((r.get("output") if isinstance(r, dict) else str(r)) or "").strip()
        lines = t.split(chr(10))
        head = [x.strip() for x in lines[0].split("|")] if lines else []
        if not head or not head[0]:
            continue
        aid = head[0]
        quote = lines[1].strip() if len(lines) > 1 else ""
        facts = " ".join(x.strip() for x in lines[2:])[:300]
        if not quote and not facts:
            continue
        if aid not in shops:
            if len(order) >= 10:
                continue
            shops[aid] = []
            order.append(aid)
        if len(shops[aid]) < 2:
            shops[aid].append({"src": head[4].replace("来源", "").strip() if len(head) > 4 else "",
                               "date": head[5] if len(head) > 5 else "", "quote": quote[:200], "facts": facts})
    out = [{"amap_id": a, "rank": i + 1, "snips": shops[a]} for i, a in enumerate(order)]
    return {"kb_ids": ",".join(order), "kb_json": json.dumps(out, ensure_ascii=False)}
'''

CODE_EVID_MERGE = r'''
import json
async def main(args: Args) -> Output:
    p = args.params
    try:
        cands = json.loads(p.get("candidates_json") or "[]")
    except Exception:
        cands = []
    ev = {}
    for e in p.get("evid") or []:
        try:
            d = json.loads(e)
            ev[d.get("amap_id")] = d.get("snips") or []
        except Exception:
            pass
    try:
        kb = {x["amap_id"]: x for x in json.loads(p.get("kb_json") or "[]")}
    except Exception:
        kb = {}
    try:
        scene = json.loads(p.get("slots_json") or "{}").get("scene") or ""
    except Exception:
        scene = ""
    tag = p.get("scene_tag") or ""
    try:
        funnel = json.loads(p.get("funnel") or "{}")
    except Exception:
        funnel = {}
    syn = {"约会": ["约会", "情侣", "氛围", "浪漫", "仪式感"], "带老人": ["带老人", "家庭", "长辈", "爸妈", "老人"],
           "带娃": ["带娃", "亲子", "儿童", "小朋友", "宝宝"], "聚餐": ["聚餐", "聚会", "团建", "朋友"],
           "一人食": ["一人食", "一个人"], "生日": ["生日", "庆祝", "仪式感"], "商务": ["商务", "宴请", "包间"]}
    words = []
    for k, v in syn.items():
        if k in scene or scene in k and scene:
            words += v
    if scene and not words:
        words = [scene]
    n = 0
    for i, c in enumerate(cands):
        e = ev.get(c.get("amap_id"), [])
        k = kb.get(c.get("amap_id"))
        if k:
            # 跨店检索命中的段对得上用户的要求或场景，放在证据最前面
            seen = {str(x.get("quote") or "")[:30] for x in e}
            e = [x for x in k.get("snips") or [] if str(x.get("quote") or "")[:30] not in seen] + e
            c["need_hit"] = True
        c["evidence"] = e[:3]
        n += bool(c["evidence"])
        text = "".join(str(x.get("quote") or "") + str(x.get("facts") or "") for x in c["evidence"])
        if scene:
            # 场景能不能对上：门店场景标签（etl/shop_tags.py 从笔记汇总）或证据里有场景词
            c["scene_ok"] = bool(tag and tag in str(c.get("tags") or "").split("|")) or any(w in text for w in words)
        dd = c.get("dims") or {}
        if c.get("scene_ok") and dd.get("场景", 10) < 8:
            dd["场景"] = 8.0  # 门店没有场景标签，但评价里写到了
            W = funnel.get("weights") or {}
            tw = sum(W.get(k, 0) for k in dd)
            if tw:
                c["score"] = round(sum(W.get(k, 0) * v for k, v in dd.items()) / tw, 2)
        c["_rank"] = (0 if c.get("scene_ok") else 1, 0 if c.get("need_hit") else 1, 0 if c["evidence"] else 1, i)
    cands.sort(key=lambda c: c["_rank"])
    for c in cands:
        c.pop("_rank", None)
    # 用户说了场景、对得上的店够 3 家时，只把这些店交给推荐生成（10-21，E5 t02：约会场景下 DeepSeek 挑了没有约会标签的店）
    ok = [c for c in cands if c.get("scene_ok")]
    funnel["evid"] = n
    if scene:
        funnel["scene"] = {"v": scene, "ok": len(ok), "filtered": len(ok) >= 3}
    if scene and len(ok) >= 3:
        cands = ok
    funnel["kb"] = sum(1 for c in cands if c.get("need_hit"))
    funnel["to_llm"] = len(cands[:8])
    return {"candidates_json": json.dumps(cands[:8], ensure_ascii=False), "with_evidence": n,
            "funnel": json.dumps(funnel, ensure_ascii=False)}
'''

CODE_FORMAT = r'''
import json
import re
async def main(args: Args) -> Output:
    p = args.params
    anchor = p.get("anchor") or "你所在位置"
    try:
        cands = {c["amap_id"]: c for c in json.loads(p.get("candidates_json") or "[]")}
    except Exception:
        cands = {}
    text = p.get("picks") or ""
    m = re.search(r"\[.*\]", text, re.S)
    try:
        picks = json.loads(m.group(0)) if m else []
    except Exception:
        picks = []
    NL = chr(10)
    lines, items = [], []
    def cost_txt(c):
        # 高德没给人均的店（18 家）显示"人均未知"，以前显示成 ¥0
        try:
            v = float(c.get("cost") or 0)
        except Exception:
            v = 0
        return "人均 ¥" + str(int(v)) if v > 0 else "人均未知"
    # 理由为空的条目当作没选：DeepSeek 偶尔在合格的店后面补一个只有 amap_id 和店名的空条目（10-20）
    picks = [x for x in picks if isinstance(x, dict) and x.get("amap_id") in cands and str(x.get("reason") or "").strip()][:3]
    # 场景对得上的店排前面（10-21）：推荐生成偶尔把没有场景依据的店放第 1，稳定排序，其余保持模型给的顺序
    picks.sort(key=lambda x: 0 if cands[x["amap_id"]].get("scene_ok", True) else 1)
    for pk in picks:
        c = cands[pk["amap_id"]]
        i = len(items) + 1
        lines.append("**" + str(i) + ". " + str(c["name"]) + "**（" + str(c.get("cate") or "") + "）")
        lines.append("评分 " + str(c["rating"]) + " | " + cost_txt(c) + " | 距" + anchor + "约 " + str(c["dist_m"]) + " 米")
        lines.append("推荐理由：" + str(pk.get("reason") or ""))
        if pk.get("matched"):
            lines.append("符合：" + "、".join("✓" + str(x) for x in pk["matched"]))
        q = str(pk.get("quote") or "").strip().strip("“”\"")
        ev_text = "".join(str(x.get("quote") or "") + str(x.get("facts") or "") for x in c.get("evidence") or [])
        if q and q.replace(" ", "")[:20] in ev_text.replace(" ", ""):
            lines.append("口碑：“" + q + "”" + ("（" + str(pk.get("source")) + "）" if pk.get("source") else ""))
        if pk.get("caveat"):
            lines.append("注意：" + str(pk["caveat"]))
        if c.get("dishes"):
            lines.append("推荐菜：" + str(c["dishes"]))
        ds = [d for d in c.get("deals") or [] if d.get("name") and (not d.get("synthetic") or str(p.get("cid")) == "2")]
        c["_ds"] = ds
        if ds:
            def one(d):
                return str(d["name"]) + (" ¥" + str(d["price"]) if d.get("price") else "") + \
                       ("（门市 ¥" + str(d["orig_price"]) + "）" if d.get("orig_price") else "")
            if ds[0].get("synthetic"):
                lines.append("团购（示例数据，非真实优惠）：" + "；".join(one(d) for d in ds))
            else:
                lines.append("团购（" + str(ds[0].get("src") or "笔记") + " 提到，以店内为准）：" + "；".join(one(d) for d in ds))
        lines.append("")
        items.append({"amap_id": c["amap_id"], "name": c["name"], "cate": c.get("cate"), "rating": c["rating"], "cost": c["cost"],
                      "dist_m": c["dist_m"], "reason": pk.get("reason"), "matched": pk.get("matched") or [],
                      "lng": c.get("lng"), "lat": c.get("lat"), "addr": c.get("addr") or "", "deals": c.get("_ds") or [],
                      "score": c.get("score"), "dims": c.get("dims") or {}, "scene_ok": c.get("scene_ok"), "need_hit": c.get("need_hit")})
    if not items and cands:
        # 模型一家都没选时，退回到候选里排第一的店，并如实说明
        c = list(cands.values())[0]
        lines = ["**1. " + str(c["name"]) + "**（" + str(c.get("cate") or "") + "）",
                 "评分 " + str(c["rating"]) + " | " + cost_txt(c) + " | 距" + anchor + "约 " + str(c["dist_m"]) + " 米",
                 "附近完全符合条件的店不多，这家最接近。", ""]
        items.append({"amap_id": c["amap_id"], "name": c["name"], "cate": c.get("cate"), "rating": c["rating"], "cost": c["cost"],
                      "dist_m": c["dist_m"], "reason": "最接近条件的候选", "matched": [], "lng": c.get("lng"), "lat": c.get("lat"),
                      "addr": c.get("addr") or "", "deals": []})
    if items:
        answer = "在" + anchor + "附近给你挑了 " + str(len(items)) + " 家：" + NL + NL + NL.join(lines)
        if p.get("profile_note"):
            answer = "（" + str(p.get("profile_note")) + "）" + NL + NL + answer
        if p.get("relaxed"):
            answer = str(p.get("relaxed")) + "。" + NL + NL + answer
    else:
        answer = "在" + anchor + "附近没找到符合条件的店。可以放宽预算、换个品类，或者换个商圈再试。"
    # 锚点坐标和召回、算距离用的是同一个（解析坐标节点），网页 Demo 在地图上标锚点和店（10-22）
    def fl(x):
        try:
            return float(x)
        except Exception:
            return None
    # 解释（10-23）：每个条件的来源（合并槽位）+ 筛选漏斗（裁剪候选、合并证据）+ 维度权重
    try:
        funnel = json.loads(p.get("funnel") or "{}")
    except Exception:
        funnel = {}
    try:
        conds = json.loads(p.get("conds_json") or "[]")
    except Exception:
        conds = []
    funnel["picked"] = len(items)
    card = {"type": "shop_list", "anchor": anchor, "anchor_lng": fl(p.get("anchor_lng")), "anchor_lat": fl(p.get("anchor_lat")),
            "items": items, "explain": {"conds": conds, "funnel": funnel}}
    answer = answer + NL + "```card" + NL + json.dumps(card, ensure_ascii=False) + NL + "```"
    shown = [{"amap_id": x["amap_id"], "name": x["name"], "cate": x.get("cate"), "cost": x["cost"]} for x in items]
    return {"answer": answer, "shown_json": json.dumps(shown, ensure_ascii=False)}
'''

SQL_STATE_READ = ("SELECT slots, turn, last_shown, stage, pending_q FROM session_state WHERE conv = '{{conv}}' "
                  "ORDER BY id DESC LIMIT 1")
SQL_PROFILE = ("SELECT id, kind, mkey, mvalue, status, evidence AS ev, conv FROM user_memory WHERE status IN ('active', 'candidate') "
               "AND kind IN ('pref', 'strategy') ORDER BY id DESC LIMIT 60")
SQL_MEM_INS = ("INSERT INTO user_memory (kind, mkey, mvalue, status, support, consistency, confidence, last_seen, evidence, conv, updated_at) "
               "VALUES ('{{kind}}', '{{mkey}}', '{{mvalue}}', '{{status}}', CAST('{{support}}' AS SIGNED), "
               "CAST('{{cons}}' AS DECIMAL(6,3)), CAST('{{conf}}' AS DECIMAL(6,3)), '{{now}}', '{{ev}}', '{{conv}}', '{{now}}')")
SQL_MEM_DEL = "UPDATE user_memory SET status = 'deleted', updated_at = '{{now}}' WHERE id = CAST('{{id}}' AS SIGNED)"
# 有待整理的行才取用户号通知 sidecar；bstudio_connector_uid 是 Coze 按用户隔离用的隐藏列，等于 OpenAPI 的 ext.user_id
SQL_UID = "SELECT bstudio_connector_uid AS u FROM user_memory WHERE status = 'candidate' LIMIT 1"
SQL_STATE_DEL = "DELETE FROM session_state WHERE conv = '{{conv}}'"
SQL_STATE_INS = ("INSERT INTO session_state (conv, slots, candidates, last_shown, stage, turn, pending_q, updated_at) "
                 "VALUES ('{{conv}}', '{{slots}}', '{{cands}}', '{{shown}}', '{{stage}}', CAST('{{turn}}' AS SIGNED), '{{q}}', '{{now}}')")
# 品类、预算不做硬过滤，算成排序标记；零结果时由"裁剪候选"逐级放宽（设计文档 4.4）
DIST = "ST_Distance_Sphere(POINT(lng, lat), POINT(CAST('{{lng}}' AS DECIMAL(10,6)), CAST('{{lat}}' AS DECIMAL(10,6))))"
# 品类命中 kw_hit（10-21 起）：0 不中，1 同品类，2 店名或推荐菜里有用户点名的菜。正则由 CODE_MERGE 按品类表生成，
# 规则见 common/cate_tax.query_res。std_cate 标成非餐饮的（便利店、珠宝店、外卖柜）不进召回。
# 预算按区间（bmin 默认 0）；距离仍取 5 公里内，用户说的范围（dist_max）在裁剪候选里当一级放宽条件。
# 人均为空的店（高德没给，42 家）：没说预算时算符合，说了预算时算不满足（没法确认）。10-21 前 cost 为空时整个条件是 NULL，
# 用户没说预算也被当成不满足，一直排在后面。
# kb_pos 是跨店检索的名次（CODE_KB_CROSS，0 = 没命中），只在满足品类、预算的店里往前排
SQL_RECALL = ("SELECT amap_id, name, cate, std_cate, biz_area, rating, cost, dishes, tags, note_cnt, deals, lng, lat, address, ROUND(" + DIST + ") AS dist_m, "
              "GREATEST(IFNULL(std_cate, '') REGEXP '{{cate_re}}', "
              "IFNULL(std_cate, '') = '' AND CONCAT(name, '|', IFNULL(cate, '')) REGEXP '{{name_re}}', "
              "(CONCAT(name, '|', IFNULL(dishes, '')) REGEXP '{{dish_re}}') * CAST('{{dish_w}}' AS SIGNED), "
              "(FIND_IN_SET(amap_id, '{{kb_ids}}') BETWEEN 1 AND 5) * CAST('{{kb_w}}' AS SIGNED)) AS kw_hit, "
              "IFNULL(cost >= CAST('{{bmin}}' AS DECIMAL) AND cost <= CAST('{{budget}}' AS DECIMAL), "
              "CAST('{{bmin}}' AS DECIMAL) = 0 AND CAST('{{budget}}' AS DECIMAL) >= 99999) AS cost_ok, "
              "FIND_IN_SET(amap_id, '{{kb_ids}}') AS kb_pos "
              "FROM shop WHERE " + DIST + " <= 5000 AND IFNULL(std_cate, '') NOT LIKE '%|非餐饮|%' "
              "ORDER BY kw_hit DESC, cost_ok DESC, (FIND_IN_SET(amap_id, '{{kb_ids}}') > 0) DESC, (tags LIKE CONCAT('%', '{{scene}}', '%')) DESC, dist_m ASC LIMIT 30")


# 对比评价 / 路线规划：先认出用户说的是哪几家店（上轮编号或店名），再分别走 PK 和高德路线（设计文档 4.1 WF-compare、WF-route）
SHOP_SCHEMA = [cv.out(n, "string") for n in ["amap_id", "name", "cate", "biz_area", "rating", "cost", "dishes", "tags",
                                             "note_cnt", "lng", "lat"]]
SQL_SHOPS = ("SELECT amap_id, name, cate, biz_area, rating, cost, dishes, tags, note_cnt, lng, lat FROM shop "
             "ORDER BY note_cnt DESC, rating DESC LIMIT 300")
SQL_HOT = ("SELECT name, cate, biz_area, address, rating, cost, note_cnt, tags FROM shop WHERE note_cnt > 0 "
           "ORDER BY note_cnt DESC, rating DESC LIMIT 40")
# 问某一家的团购：认店之后再查这几家的团购（团购放进"读门店"的 300 行会超过代码节点的参数长度上限）
SQL_DEALS = "SELECT amap_id, name, lng, lat, deals FROM shop WHERE FIND_IN_SET(amap_id, '{{ids}}') > 0 LIMIT 3"
DEAL_SCHEMA = [cv.out(n, "string") for n in ["amap_id", "name", "lng", "lat", "deals"]]
HOT_SCHEMA = [cv.out(n, "string") for n in ["name", "cate", "biz_area", "address", "rating", "cost", "note_cnt", "tags"]]

CODE_PICK = r'''
import json
import re
async def main(args: Args) -> Output:
    p = args.params
    q = p.get("query") or ""
    route = p.get("route") or ""
    def load(s, d):
        try:
            return json.loads(s) if s else d
        except Exception:
            return d
    shown = [x for x in load(p.get("shown"), []) if isinstance(x, dict)]
    rows = p.get("rows") or []
    by_id = {r.get("amap_id"): r for r in rows}
    picked = []
    def add(aid):
        if aid and aid in by_id and aid not in picked:
            picked.append(aid)
    # 1. 上轮推荐的编号：第一家、第 2 家；这两家、这几家、都
    CN = {"一": 1, "二": 2, "两": 2, "三": 3, "1": 1, "2": 2, "3": 3}
    for m in re.finditer(r"第\s*([一二三123])\s*[家个]", q):
        i = CN[m.group(1)]
        if 0 < i <= len(shown):
            add(shown[i - 1].get("amap_id"))
    if not picked and shown and re.search(r"[这那前]([两三几])家|它们|都比|三家", q):
        n = 2 if re.search(r"[这那前]两家", q) else len(shown)
        for x in shown[:n]:
            add(x.get("amap_id"))
    # 2. 店名：品牌名（店名去掉括号和"·"后面的分店说明）出现在问题里；同一品牌只取笔记最多的一家
    seen, hits = set(), []
    for r in rows:
        b = re.split(r"[(（·•]", r.get("name") or "")[0].strip()
        if len(b) < 2 or b in seen:
            continue
        # 全名、去掉首字（"炳胜"）、长店名的前 4/3 字（"八合里""费大厨"）；地名开头的前缀不算
        keys = [b] + ([b[1:]] if len(b) >= 3 else []) + ([b[:4]] if len(b) >= 6 else []) + ([b[:3]] if len(b) >= 5 else [])
        keys = [k for k in keys if len(k) >= 2 and not any(w in k for w in ("海岸城", "深圳", "南山", "香港", "中国"))]
        pos = next((q.find(k) for k in keys if k in q), -1)
        if pos >= 0:
            seen.add(b)
            hits.append((pos, r.get("amap_id")))
    for _, aid in sorted(hits):
        add(aid)
    # 3. "这家""它"指上轮第一家
    if not picked and shown and re.search(r"这家|那家|它|这个店|这店", q):
        add(shown[0].get("amap_id"))
    # 4. 上轮只剩一家（用户刚选定）时，没点名就指它
    if not picked and len(shown) == 1:
        add(shown[0].get("amap_id"))
    picked = picked[:3]
    targets = []
    for aid in picked:
        r = by_id[aid]
        targets.append({"amap_id": aid, "name": r.get("name"), "cate": r.get("cate"), "area": r.get("biz_area"),
                        "rating": r.get("rating"), "cost": r.get("cost"), "dishes": (r.get("dishes") or "").replace("|", "、"),
                        "tags": r.get("tags") or "", "note_cnt": r.get("note_cnt"), "lng": r.get("lng"), "lat": r.get("lat"),
                        "dist_m": ""})
    items = [json.dumps({"amap_id": t["amap_id"], "q": re.split(r"[(（]", t["name"] or "")[0] + " " + t["amap_id"]},
                        ensure_ascii=False) for t in targets]
    # 路线起点：问题里"从 X 出发/过去"，否则会话里的商圈，再否则画像里常去的商圈，最后海岸城
    m = re.search(r"从(.{2,12}?)(出发|过去|走过去|开车|打车|坐地铁|到|去)", q)
    prev = load(p.get("prev_slots"), {})
    prof = load(p.get("prof_json"), {})
    origin = (m.group(1) if m else "") or prev.get("area") or prof.get("area") or "海岸城"
    if route == "cmp" and targets:
        go = "cmp"
    elif route == "nav" and targets:
        go = "nav"
    elif route == "dl" and targets:
        go = "deal"
    else:
        go = "ask"
    ask = ("想比较哪几家？可以直接说店名，比如“小炳胜和陶陶居哪个好”；刚推荐过的也可以说“第一家和第二家比一下”。"
           if route == "cmp" else "想看哪家店的团购？说店名，或者说“第一家有团购吗”。" if route == "dl" else
           "想去哪家店？说店名就行，或者说“第一家怎么去”。也可以带上出发地，比如“从科技园过去”。")

    t0 = targets[0] if targets else {}
    return {"go": go, "targets_json": json.dumps(targets, ensure_ascii=False), "cand_items": items, "ask": ask,
            "pick_ids": ",".join(picked),
            "origin": origin, "to_name": str(t0.get("name") or ""), "to_lng": str(t0.get("lng") or ""),
            "to_lat": str(t0.get("lat") or "")}
'''

CODE_CMP_FORMAT = r'''
import json
import re
async def main(args: Args) -> Output:
    p = args.params
    try:
        shops = json.loads(p.get("targets_json") or "[]")
    except Exception:
        shops = []
    m = re.search(r"\{.*\}", p.get("out") or "", re.S)
    try:
        res = json.loads(m.group(0)) if m else {}
    except Exception:
        res = {}
    per = {x.get("amap_id"): x for x in res.get("shops") or [] if isinstance(x, dict)}
    NL = chr(10)
    def cell(x):
        return str(x or "暂无").replace("|", "、").replace(NL, " ")
    def ev_text(s):
        return "".join(str(e.get("quote") or "") + str(e.get("facts") or "") for e in s.get("evidence") or [])
    names = [re.split(r"[(（]", s.get("name") or "")[0] for s in shops]
    lines = []
    if len(shops) == 1:
        s, x = shops[0], per.get(shops[0].get("amap_id"), {})
        lines += ["**" + str(s.get("name")) + "**（" + str(s.get("cate") or "") + "）",
                  "评分 " + cell(s.get("rating")) + " | 人均 ¥" + cell(s.get("cost")) + " | 小红书笔记 " + cell(s.get("note_cnt")) + " 篇",
                  "优点：" + cell(x.get("pros")), "缺点：" + cell(x.get("cons")), "适合：" + cell(x.get("fit"))]
    else:
        lines.append("| | " + " | ".join(names) + " |")
        lines.append("|---" * (len(shops) + 1) + "|")
        rows = [("人均", lambda s, x: "¥" + cell(s.get("cost"))), ("评分", lambda s, x: cell(s.get("rating"))),
                ("品类", lambda s, x: cell((s.get("cate") or "").split("/")[-1])),
                ("推荐菜", lambda s, x: cell("、".join((s.get("dishes") or "").split("、")[:3]))),
                ("适合", lambda s, x: cell(x.get("fit"))), ("优点", lambda s, x: cell(x.get("pros"))),
                ("缺点", lambda s, x: cell(x.get("cons")))]
        for label, fn in rows:
            lines.append("| " + label + " | " + " | ".join(fn(s, per.get(s.get("amap_id"), {})) for s in shops) + " |")
    quotes = []
    for s, nm in zip(shops, names):
        x = per.get(s.get("amap_id"), {})
        q = str(x.get("quote") or "").strip().strip("“”\"")
        if q and q.replace(" ", "")[:20] in ev_text(s).replace(" ", ""):  # 原话必须在证据里找得到
            quotes.append("- " + nm + "：“" + q + "”" + ("（" + str(x.get("source")) + "）" if x.get("source") else ""))
    if quotes:
        lines += ["", "口碑："] + quotes
    if res.get("verdict"):
        lines += ["", "结论：" + str(res["verdict"])]
    if not res:
        lines += ["", "（对比生成失败，上面只列了门店资料。）"]
    try:
        pos = {x.get("amap_id"): x for x in json.loads(p.get("orig_json") or "[]")}
    except Exception:
        pos = {}
    card = {"type": "compare", "items": [{"amap_id": s.get("amap_id"), "name": s.get("name"), "rating": s.get("rating"),
                                          "cost": s.get("cost"), "lng": pos.get(s.get("amap_id"), {}).get("lng"),
                                          "lat": pos.get(s.get("amap_id"), {}).get("lat")} for s in shops]}
    answer = NL.join(lines) + NL + "```card" + NL + json.dumps(card, ensure_ascii=False) + NL + "```"
    return {"answer": answer}
'''

CODE_NAV_FORMAT = r'''
import json
async def main(args: Args) -> Output:
    p = args.params
    try:
        d = json.loads(p.get("body") or "{}")
    except Exception:
        d = {}
    to = p.get("to_name") or d.get("dest") or "目的地"
    org = str(d.get("origin") or p.get("origin") or "").replace("广东省", "").replace("深圳市", "")
    NL = chr(10)
    lines = ["从" + org + "到 **" + to + "**：" if org else "到 **" + to + "**："]
    w, dr, tr = d.get("walk"), d.get("drive"), d.get("transit")
    if w:
        lines.append("- 步行约 " + str(w.get("min")) + " 分钟（" + str(w.get("m")) + " 米）")
    if tr:
        lines.append("- 公交地铁约 " + str(tr.get("min")) + " 分钟" + ("：" + str(tr.get("lines")) if tr.get("lines") else ""))
    if dr:
        lines.append("- 打车约 " + str(dr.get("min")) + " 分钟" + ("，约 " + str(dr.get("taxi_yuan")) + " 元" if dr.get("taxi_yuan") else ""))
    if not (w or tr or dr):
        if d.get("line_m"):
            lines.append("- 路线查询失败，直线距离约 " + str(d.get("line_m")) + " 米")
        else:
            lines.append("- 路线查询失败，稍后再试。")
    elif w and int(w.get("min") or 0) <= 15:
        lines.append(NL + "走路就能到。")
    if d.get("origin_fallback"):
        lines.append("（没认出出发地，按海岸城算的。可以说“从 XX 过去”。）")
    # route 卡片（网页 Demo，10-22）：起终点坐标、三种方式的时间距离、一条折线（步行 2 公里内画步行，否则驾车）
    if d.get("o_lng") and d.get("d_lng"):
        card = {"type": "route", "origin": {"name": org, "lng": d.get("o_lng"), "lat": d.get("o_lat")},
                "dest": {"name": to, "lng": d.get("d_lng"), "lat": d.get("d_lat")},
                "walk": w or None, "drive": dr or None, "transit": tr or None,
                "mode": d.get("poly_mode") or "", "poly": d.get("poly") or ""}
        lines.append("```card" + NL + json.dumps(card, ensure_ascii=False) + NL + "```")
    return {"answer": NL.join(lines)}
'''

# 问某一家的团购（"第一家有团购吗"，10-22）：认店后列这家的团购。笔记里写到的标来源，合成的标示例数据（规则同推荐格式化）
CODE_DEAL_ANS = r'''
import json
async def main(args: Args) -> Output:
    rows = {r.get("amap_id"): r for r in args.params.get("rows") or []}
    shops = []
    for aid in str(args.params.get("ids") or "").split(","):
        r = rows.get(aid)
        if r:
            try:
                ds = json.loads(r.get("deals") or "[]")
            except Exception:
                ds = []
            shops.append({"amap_id": aid, "name": r.get("name"), "lng": r.get("lng"), "lat": r.get("lat"), "deals": ds})
    NL = chr(10)
    lines, items = [], []
    for s in shops:
        ds = [d for d in s.get("deals") or [] if isinstance(d, dict) and d.get("name")][:3]
        lines.append("**" + str(s.get("name") or "") + "**")
        if not ds:
            lines.append("没查到这家的团购信息，可以到店问一下或者在抖音、美团上搜店名。")
        for d in ds:
            price = (" ¥" + str(d["price"]) if d.get("price") else "") + ("（门市 ¥" + str(d["orig_price"]) + "）" if d.get("orig_price") else "")
            tag = "（示例数据，非真实优惠）" if d.get("synthetic") else "（" + str(d.get("src") or "笔记") + " 提到，以店内为准）"
            lines.append("- " + str(d["name"]) + price + (("：" + str(d["content"])) if d.get("content") else "") + tag)
        lines.append("")
        items.append({"amap_id": s.get("amap_id"), "name": s.get("name"), "lng": s.get("lng"), "lat": s.get("lat"),
                      "deals": [{k: d.get(k) for k in ("name", "price", "orig_price", "content", "src", "synthetic")} for d in ds]})
    answer = NL.join(lines).strip() + NL + "```card" + NL + json.dumps({"type": "deals", "items": items}, ensure_ascii=False) + NL + "```"
    return {"answer": answer}
'''

CODE_HOT_PREP = r'''
import json
async def main(args: Args) -> Output:
    p = args.params
    try:
        d = json.loads(p.get("body") or "{}")
    except Exception:
        d = {}
    NL = chr(10)
    web = [str(i + 1) + ". " + str(x.get("title") or "") + "｜" + str(x.get("site") or "") + "｜" + str(x.get("date") or "") + NL
           + "   " + str(x.get("text") or "")[:300] + NL + "   " + str(x.get("url") or "")
           for i, x in enumerate(d.get("items") or [])]
    # 高德的商圈字段很粗（万象城的店也标"后海片区"），带上地址让模型按商场名筛
    local = [str(r.get("name")) + "｜" + str(r.get("address") or "")[:30] + "｜" + str(r.get("cate") or "").split("/")[-1] + "｜评分 " + str(r.get("rating")) + "｜人均 "
             + str(r.get("cost")) + "｜小红书笔记 " + str(r.get("note_cnt")) + " 篇" for r in p.get("rows") or []]
    return {"web": NL.join(web) or "（搜索没有结果" + ("：" + str(d.get("error")) if d.get("error") else "") + "）",
            "local": NL.join(local) or "（无）"}
'''

CODE_CMP_MERGE = r'''
import json
async def main(args: Args) -> Output:
    try:
        shops = json.loads(args.params.get("targets_json") or "[]")
    except Exception:
        shops = []
    ev = {}
    for e in args.params.get("evid") or []:
        try:
            d = json.loads(e)
            ev[d.get("amap_id")] = d.get("snips") or []
        except Exception:
            pass
    for s in shops:
        s["evidence"] = ev.get(s.get("amap_id"), [])
        s.pop("lng", None)
        s.pop("lat", None)
        s.pop("dist_m", None)
    return {"targets_json": json.dumps(shops, ensure_ascii=False)}
'''

# FAQ：检索到的条目拼成编号列表给回答节点；n_hit 记命中条数（评测和排查用）
CODE_FAQ_PREP = r'''
async def main(args: Args) -> Output:
    rows = args.params.get("rows") or []
    items = []
    for r in rows:
        t = ((r.get("output") if isinstance(r, dict) else str(r)) or "").strip()
        if t and t not in items:
            items.append(t)
    NL = chr(10)
    text = (NL + NL).join(str(i + 1) + ". " + t for i, t in enumerate(items))
    return {"faq": text or "（无）", "n_hit": len(items)}
'''
