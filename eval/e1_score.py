"""给 E1 打分并出报告：意图准确率（系统 / 只看 /decide / 平台意图节点）、该不该澄清、分流、槽位 F1、D1 反馈、延迟。

用法：python -I -X utf8 eval/e1_score.py <run>
输出：eval/reports/E1_<run>.md、eval/runs/<run>/e1_metrics.json

判分规则：
- 意图：预测落在 intent_ok（没写就是 intent）里算对。
- 平台基线 = Coze 意图节点单独用（v0.0.10 以前的做法）：编号 0 没有对应意图，算错；它不会做意图澄清，
  只有找店/套餐且缺地点和品类时会追问条件。
- 系统追问：分流到 ask 是意图澄清；分流到找店且 need_clarify 是条件追问；其余是不问。落在 ask_ok（没写就是 ask）里算对。
- 槽位：只评有 slots 金标的用例，比合并后的槽位（含上轮条件）。金标没写的键应为空；"*" 不评。
  地点、品类、场景宽松匹配（去掉"附近、那边"等后缀，互相包含也算，被包含的一方至少 3 个字）；
  口味按短语算，金标每个短语有一个对上就算命中；预算、人数比整数。
  一个槽位：对 → TP；漏 → FN；填错 → FP + FN；金标为空却填了 → FP。
"""
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEYS_I = ["rec", "deal", "cmp", "nav", "hot", "faq", "pm", "chat"]
NAME_I = {"rec": "找餐厅", "deal": "选套餐", "cmp": "对比评价", "nav": "路线", "hot": "榜单", "faq": "FAQ", "pm": "偏好管理",
          "chat": "闲聊", "none": "（编号0）", "?": "（无结果）"}
ROUTE_OF = {"rec": "rec", "deal": "rec", "cmp": "cmp", "nav": "nav", "hot": "hot", "pm": "pm", "faq": "other", "chat": "other"}
SCENE_SYN = {"约会": ["约会", "情侣", "二人食", "浪漫", "氛围"], "带老人": ["带老人", "家庭", "长辈", "爸妈", "老人"],
             "带娃": ["带娃", "亲子", "儿童", "小朋友", "宝宝"], "聚餐": ["聚餐", "聚会", "团建"],
             "一人食": ["一人食", "一个人", "工作餐"], "生日": ["生日", "庆祝"], "商务": ["商务", "宴请", "包间"],
             "拍照": ["拍照", "出片", "漂亮饭"], "下午茶": ["下午茶"]}  # 和 flow_code.CODE_MERGE 一致
SLOTS = ["area", "category", "taste", "budget_min", "budget_max", "dist_max", "people", "scene", "need"]  # budget_min、dist_max、need 10-21 加
ONLY_IF_GOLD = {"need"}  # 只在金标写了这个键的用例上评（以前的用例没标 need）
SUFFIX = re.compile(r"(附近|周边|一带|那边|这边|旁边|那块|这块|那儿|这儿|这里|那里)$")


def jl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def obj(s):
    if isinstance(s, dict):
        return s
    try:
        v = json.loads(s or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def norm(s) -> str:
    s = re.sub(r"[\s的]", "", str(s or "")).lower()
    return SUFFIX.sub("", s)


def smatch(pred, alts: str) -> bool:
    p = norm(pred)
    if not p:
        return False
    for a in str(alts).split("|"):
        a = norm(a)
        if a and (a == p or (len(a) >= 2 and a in p) or (len(p) >= 3 and p in a)):
            return True
    return False


def predict(r: dict) -> dict:
    """从一次运行结果里取出各路预测。"""
    nd = r.get("nodes") or {}
    end = nd.get("end") if isinstance(nd.get("end"), dict) else {}
    dec = obj(obj(nd.get("dec")).get("body")).get("answers") or {}
    note = str(end.get("dec_note") or "")
    m = re.search(r"coze_cid=(\d+)", note)
    coze = int(m.group(1)) if m else int((nd.get("intent") or {}).get("classificationId") or 0) if isinstance(nd.get("intent"), dict) else 0
    cid = int(end.get("cid") or 0) if end else 0
    route = str(end.get("route") or "") if end else ""
    need = bool(end.get("need_clarify")) if end else False
    it = obj(dec.get("intent"))
    sl = obj(end.get("slots_json"))
    fb, tg = obj(dec.get("feedback")), obj(dec.get("target"))
    return {
        "ok_run": r.get("status") == 2 and bool(end),
        "sys": KEYS_I[cid - 1] if 1 <= cid <= 8 else ("none" if end else "?"),
        "dec": it.get("choice") if it.get("choice") in KEYS_I else "?",
        "dec_conf": it.get("confidence"), "unclear": obj(dec.get("unclear")).get("noul"),
        "coze": KEYS_I[coze - 1] if 1 <= coze <= 8 else "none",
        "src": note.split(" ", 1)[0] if note else "?",
        "route": route,
        "ask": "intent" if route == "ask" else ("slot" if route == "rec" and need else ""),
        "coze_ask": "slot" if 1 <= coze <= 2 and need else "",
        "slots": sl, "scene_tag": str(end.get("scene_tag") or ""),
        "fb": fb.get("choice"), "fb_conf": fb.get("confidence"), "target": tg.get("choice"),
        "raw_slot": (nd.get("slot") or {}).get("output") if isinstance(nd.get("slot"), dict) else None,
    }


def slot_eval(gold: dict, p: dict) -> tuple[dict, list[str]]:
    """返回 {槽位: [tp, fp, fn]} 和这条用例的错误描述。"""
    sl, cnt, errs = p["slots"], {}, []
    for k in SLOTS:
        g = gold.get(k)
        if g == "*" or (k in ONLY_IF_GOLD and k not in gold):
            continue
        tp = fp = fn = 0
        if k in ("taste", "need"):
            phrases = [x for x in re.split(r"[、，,;；/和与及]", str(sl.get(k) or "")) if norm(x)]
            gl = g or []
            hit = [any(smatch(x, ga) for x in phrases) for ga in gl]
            tp, fn = sum(hit), len(hit) - sum(hit)
            fp = sum(1 for x in phrases if not any(smatch(x, ga) for ga in gl))
            if fn or fp:
                errs.append(f"{k} 金标{gl} 预测{sl.get(k) or '空'}")
        elif k in ("budget_min", "budget_max", "dist_max", "people"):
            try:
                v = int(float(sl.get(k) or 0))
            except (TypeError, ValueError):
                v = 0
            ok = g if isinstance(g, list) else ([g] if g is not None else [])
            if ok and v in ok:
                tp = 1
            elif ok and not v:
                fn = 1
            elif ok:
                fp = fn = 1
            elif v:
                fp = 1
            if fp or fn:
                errs.append(f"{k} 金标{g if g is not None else '空'} 预测{v or '空'}")
        else:
            v = str(sl.get(k) or "")
            if k == "scene":
                good = bool(g) and (p["scene_tag"] == g or smatch(v, g))
            else:
                good = bool(g) and smatch(v, g)
            if good:
                tp = 1
            elif g and not v:
                fn = 1
            elif g:
                fp = fn = 1
            elif v:
                fp = 1
            if fp or fn:
                errs.append(f"{k} 金标{g or '空'} 预测{v or '空'}")
        cnt[k] = [tp, fp, fn]
    return cnt, errs


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def pct(a, b):
    return f"{a}/{b}（{a / b:.0%}）" if b else "—"


def q(xs, f):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return "—"
    return f"{xs[min(len(xs) - 1, int(round(f * (len(xs) - 1))))]:.1f}"


def main():
    run = sys.argv[1]
    rd = ROOT / "eval" / "runs" / run
    meta = json.loads((rd / "meta.json").read_text(encoding="utf-8"))
    cases = {c["id"]: c for c in jl(ROOT / meta["dataset"])}
    res = {r["id"]: r for r in jl(rd / "e1_results.jsonl")}
    ids = [i for i in cases if i in res]
    P = {i: predict(res[i]) for i in ids}
    failed = [i for i in ids if not P[i]["ok_run"]]

    def ok_int(i, who):
        c = cases[i]
        return P[i][who] in (c.get("intent_ok") or [c["intent"]])

    def ok_ask(i, who="ask"):
        c = cases[i]
        return P[i][who] in (c.get("ask_ok") or [c["ask"]])

    L = []
    w = L.append
    w(f"# E1 意图与槽位：{run}\n")
    hl = meta.get("sidecar_health") or {}
    w(f"- 时间：{meta.get('started')} → {meta.get('finished') or '未跑完'}")
    w(f"- 被测：{meta['workflow']}（发布版 {meta['published_version']}；调试运行读草稿，草稿=发布 {meta['draft_is_published']}，"
      f"草稿=本地画布 {meta['draft_equals_local_canvas']}）")
    w(f"- 模型：Coze 节点 {meta['coze_model']['name']}（model_type {meta['coze_model']['model_type']}）；"
      f"/decide {hl.get('decider_model')}，后端 {','.join(hl.get('backends') or [])}；sidecar main.py {meta['sidecar_main_sha']}")
    w("- Prompt：" + "、".join(f"{k}（{v}）" for k, v in meta["prompts_in_canvas"].items()) + f"；意图表 {meta['intents_sha']}")
    n_seed = sum(cases[i]["src"] == "seed" for i in ids)
    n_test = sum(cases[i]["split"] == "test" for i in ids)
    import hashlib
    now_sha = hashlib.sha1((ROOT / meta["dataset"]).read_bytes()).hexdigest()[:10]
    if now_sha != meta["dataset_sha"]:
        w(f"- 注意：评测集在跑完后改过（跑时 {meta['dataset_sha']}，现在 {now_sha}），只要句子和上下文没变，按现在的金标重新打分有效")
    w(f"- 数据：{meta['dataset']}（{meta['dataset_sha']}），本次 {len(ids)}/{len(cases)} 条：手写 {n_seed}、生成 {len(ids) - n_seed}；"
      f"test 切分 {n_test} 条。运行失败 {len(failed)} 条（计入分母，按答错算）")
    w("")

    # ---- 意图 ----
    def acc(sub, who):
        return pct(sum(ok_int(i, who) for i in sub), len(sub))
    groups = [("全部", ids), ("test", [i for i in ids if cases[i]["split"] == "test"]),
              ("手写难例", [i for i in ids if cases[i]["src"] == "seed"]),
              ("生成", [i for i in ids if cases[i]["src"] != "seed"])]
    w("## 1. 意图准确率\n")
    w("| 子集 | 系统（/decide，失败时用意图节点） | 只看 /decide | 平台意图节点（基线） |")
    w("|---|---|---|---|")
    for nm, sub in groups:
        w(f"| {nm} | {acc(sub, 'sys')} | {acc(sub, 'dec')} | {acc(sub, 'coze')} |")
    n_coze_fallback = sum(P[i]["src"] == "coze" for i in ids)
    w(f"\n/decide 没给出可用结果、改用意图节点的有 {n_coze_fallback} 条；意图节点回编号 0 的有 "
      f"{sum(P[i]['coze'] == 'none' for i in ids)} 条。\n")
    w("按金标意图：\n")
    w("| 金标意图 | 条数 | 系统对 | 意图节点对 | 系统错成 |")
    w("|---|---|---|---|---|")
    by = defaultdict(list)
    for i in ids:
        by[cases[i]["intent"]].append(i)
    for k in KEYS_I:
        sub = by.get(k, [])
        if not sub:
            continue
        wrong = Counter(P[i]["sys"] for i in sub if not ok_int(i, "sys"))
        w(f"| {NAME_I[k]} | {len(sub)} | {pct(sum(ok_int(i, 'sys') for i in sub), len(sub))} | "
          f"{pct(sum(ok_int(i, 'coze') for i in sub), len(sub))} | "
          f"{'、'.join(f'{NAME_I.get(x, x)}×{n}' for x, n in wrong.most_common()) or '—'} |")
    w("")

    # ---- 追问 ----
    w("## 2. 该不该先问\n")
    w("行是金标，列是系统实际做法。\n")
    w("| 金标 \\ 系统 | 不问 | 意图澄清 | 条件追问 |")
    w("|---|---|---|---|")
    lab = {"": "不问", "intent": "意图澄清", "slot": "条件追问"}
    for g in ("", "intent", "slot"):
        sub = [i for i in ids if cases[i]["ask"] == g]
        cnt = Counter(P[i]["ask"] for i in sub)
        w(f"| {lab[g]}（{len(sub)}） | {cnt.get('', 0)} | {cnt.get('intent', 0)} | {cnt.get('slot', 0)} |")
    sys_ok = sum(ok_ask(i) for i in ids)
    coze_ok = sum(ok_ask(i, "coze_ask") for i in ids)
    should = [i for i in ids if cases[i]["ask"]]
    asked = [i for i in ids if P[i]["ask"]]
    w(f"\n- 做法对（落在可接受做法里）：系统 {pct(sys_ok, len(ids))}；平台意图节点 + 条件追问 {pct(coze_ok, len(ids))}")
    w(f"- 该问的问了：{pct(sum(1 for i in should if P[i]['ask']), len(should))}；"
      f"问了的里面确实该问（含可接受）：{pct(sum(1 for i in asked if ok_ask(i)), len(asked))}")
    gi = [i for i in ids if cases[i].get("route")]
    if gi:
        w(f"- 标了金标分流的用例：{pct(sum(P[i]['route'] == cases[i]['route'] for i in gi), len(gi))}")
    w("")

    # ---- 槽位 ----
    w("## 3. 槽位（条件抽取）\n")
    tot = defaultdict(lambda: [0, 0, 0])
    slot_err, full_ok, ns = {}, 0, 0
    for i in ids:
        c = cases[i]
        if "slots" not in c:
            continue
        ns += 1
        cnt, errs = slot_eval(c["slots"], P[i])
        for k, v in cnt.items():
            for j in range(3):
                tot[k][j] += v[j]
        if errs:
            slot_err[i] = errs
        else:
            full_ok += 1
    w("| 槽位 | 金标非空 | P | R | F1 |")
    w("|---|---|---|---|---|")
    T = [0, 0, 0]
    for k in SLOTS:
        tp, fp, fn = tot[k]
        T = [T[0] + tp, T[1] + fp, T[2] + fn]
        p, r, f = prf(tp, fp, fn)
        w(f"| {k} | {tp + fn} | {p:.2f} | {r:.2f} | {f:.2f} |")
    p, r, f = prf(*T)
    w(f"| 合计（micro） | {T[0] + T[2]} | {p:.2f} | {r:.2f} | {f:.2f} |")
    w(f"\n{ns} 条有槽位金标，全部槽位都对的 {pct(full_ok, ns)}。\n")

    # ---- D1 ----
    fbc = [i for i in ids if cases[i].get("fb")]
    if fbc:
        w("## 4. 对上轮推荐的反馈（D1）\n")
        lab_ok = sum(P[i]["fb"] == cases[i]["fb"]["label"] for i in fbc)
        tg = [i for i in fbc if cases[i]["fb"]["label"] != "neutral"]
        tg_ok = sum(str(P[i]["target"]) == str(cases[i]["fb"]["target"]) for i in tg)
        w(f"反馈类别 {pct(lab_ok, len(fbc))}；有表态的用例里针对哪家 {pct(tg_ok, len(tg))}。用例少，只看个大概。\n")
        for i in fbc:
            c = cases[i]
            w(f"- {i}「{c['query']}」金标 {c['fb']['label']}/{c['fb']['target']}，"
              f"/decide {P[i]['fb']}（{P[i]['fb_conf']}）/{P[i]['target']}")
        w("")

    # ---- 延迟 ----
    w("## 5. 延迟（秒）\n")
    w("| 环节 | 中位 | P90 | 最大 |")
    w("|---|---|---|---|")
    rows = [("WF-understand 整体", [res[i].get("cost_s") for i in ids])]
    for k, nm in [("dec", "决策(sidecar /decide)"), ("intent", "意图识别(Coze)"), ("mem", "记忆抽取"), ("slot", "槽位提取")]:
        rows.append((nm, [(res[i].get("node_s") or {}).get(k) for i in ids]))
    for nm, xs in rows:
        w(f"| {nm} | {q(xs, 0.5)} | {q(xs, 0.9)} | {q(xs, 1.0)} |")
    w("")

    # ---- 错例 ----
    w("## 6. 错例\n")
    w("### 意图（系统）\n")
    for i in ids:
        if not ok_int(i, "sys"):
            c, p_ = cases[i], P[i]
            ctx = "（有上下文）" if c.get("ctx") else ""
            w(f"- {i}「{c['query']}」{ctx} 金标 {NAME_I[c['intent']]}，系统 {NAME_I.get(p_['sys'], p_['sys'])}"
              f"（{p_['src']}，conf {p_['dec_conf']}），意图节点 {NAME_I.get(p_['coze'], p_['coze'])}")
    w("\n### 意图（平台意图节点，基线对照）\n")
    for i in ids:
        if not ok_int(i, "coze"):
            c = cases[i]
            w(f"- {i}「{c['query']}」{'（有上下文）' if c.get('ctx') else ''} 金标 {NAME_I[c['intent']]}，"
              f"意图节点 {NAME_I.get(P[i]['coze'], P[i]['coze'])}")
    w("\n### 追问\n")
    for i in ids:
        if not ok_ask(i):
            c, p_ = cases[i], P[i]
            w(f"- {i}「{c['query']}」金标 {lab[c['ask']]}，系统 {lab[p_['ask']]}（分流 {p_['route']}，"
              f"conf {p_['dec_conf']}，unclear {p_['unclear']}）")
    w("\n### 槽位\n")
    for i, errs in slot_err.items():
        w(f"- {i}「{cases[i]['query']}」：" + "；".join(errs))
    if failed:
        w("\n### 运行失败\n")
        for i in failed:
            w(f"- {i}：status {res[i].get('status')} {res[i].get('reason') or ''} {res[i].get('errors') or ''}")

    out = ROOT / "eval" / "reports" / f"E1_{run}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    metrics = {
        "n": len(ids), "failed": len(failed),
        "intent_acc": {who: sum(ok_int(i, who) for i in ids) / len(ids) for who in ("sys", "dec", "coze")},
        "intent_acc_test": {who: (lambda s: sum(ok_int(i, who) for i in s) / len(s) if s else None)(groups[1][1])
                            for who in ("sys", "dec", "coze")},
        "ask_acc": sys_ok / len(ids), "ask_acc_coze": coze_ok / len(ids),
        "slot_micro": dict(zip(("p", "r", "f1"), prf(*T))), "slot_case_full_ok": full_ok / ns if ns else None,
        "slot_per": {k: dict(zip(("p", "r", "f1"), prf(*tot[k]))) for k in SLOTS},
        "latency_median_s": statistics.median([res[i]["cost_s"] for i in ids if res[i].get("cost_s")] or [0]),
    }
    (rd / "e1_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"报告 {out.relative_to(ROOT)}")
    print(json.dumps(metrics, ensure_ascii=False))


if __name__ == "__main__":
    main()
