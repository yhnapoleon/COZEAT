"""E6 对话效果：按脚本多轮对话（不用模拟用户），每轮按规则和评委模型判，失败归到出错的那一层。

用例在 eval/data/e6_cases.json，每条若干轮，每轮可以写这些期望（都可省）：
  kind       回答类型：shop_list / options（追问）/ compare / route / deals / text（没有卡片）；可以给列表
  conds      shop_list 卡片 explain.conds 里的生效条件，{"地点": "海岸城", "预算": "100"}，值是子串，| 分隔备选
  conds_not  不该出现的生效条件
  shops      推荐的每家店都要满足：cate（品类写法，| 分隔，认 std_cate、高德品类、店名、推荐菜）、budget（人均 ≤ 上限 × 1.1）
  ref        指代：本轮卡片里的店 = 上一个 shop_list 的第几家（[2]、[1, 3]）
  not_prev   本轮推荐和上一个 shop_list 不重复
  not_ref    上一个 shop_list 的第几家不能再出现（[2]）
  text_has / text_not   正文（去掉卡片）必须匹配 / 不能匹配的正则
  near_m     这一轮店离锚点的上限（米），默认 1500
  judge      交给评委模型（DeepSeek）判的一句要求，看整段对话
每轮还有默认检查：有回答、没有 "Answer:" 开头、正文外没漏 JSON、shop_list 卡片里的店正文里都有、店离锚点不超过 1.5 公里。

归因（layer）：kind → 意图/澄清；conds → 槽位（第 2 轮起算上下文）；shops 先离线查库里锚点附近有没有满足的店，
没有算数据覆盖，有算召回/生成（要用 eval/recall_probe.py 分开）；ref → 指代/会话状态；text、judge → 回答生成；
默认检查 → 格式或稳定性。自动归因只是初判，人工结论写在 eval/data/e6_conclusion.md，出报告时放在开头。

用法：python -I -X utf8 eval/e6_run.py --run 名字 [-j 3] [--only a01,c05]
      python -I -X utf8 eval/e6_run.py --run 名字 --rescore      # 不重新对话，按存下的回答重判（改了用例期望或判分规则时）
输出：eval/runs/<run>/e6_results.jsonl、eval/reports/E6_<run>.md
"""
import argparse
import datetime
import hashlib
import json
import math
import re
import statistics
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bridge"))
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT))
from chat_api import chat  # noqa: E402
from common.env import need  # noqa: E402
from common.llm import chat_json  # noqa: E402

CASES = ROOT / "eval" / "data" / "e6_cases.json"
CONCLUSION = ROOT / "eval" / "data" / "e6_conclusion.md"
NEAR_M = 1500
LAYERS = ["意图/澄清", "槽位", "上下文", "指代/会话状态", "数据覆盖", "召回/生成", "回答生成", "格式", "稳定性"]


def shops() -> dict:
    out = {}
    for line in (ROOT / "data" / "processed" / "shops.jsonl").read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        out[r["amap_id"]] = r
    return out


S = shops()


def dist_m(a, b) -> float:
    R = 6371000
    p1, p2 = math.radians(a[1]), math.radians(b[1])
    dp, dl = p2 - p1, math.radians(b[0] - a[0])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def parse(answer: str) -> tuple[str, list[dict]]:
    """回答 → (去掉卡片的正文, 卡片列表)。"""
    cards = []
    for m in re.finditer(r"```card\s*\n(.*?)\n```", answer, re.S):
        try:
            cards.append(json.loads(m.group(1)))
        except ValueError:
            cards.append({"type": "_bad"})
    return re.sub(r"```card\s*\n.*?\n```", "", answer, flags=re.S).strip(), cards


def kind_of(cards: list[dict]) -> str:
    for t in ("shop_list", "compare", "route", "deals", "options"):
        if any(c.get("type") == t for c in cards):
            return t
    return "text"


def card(cards: list[dict], t: str) -> dict:
    return next((c for c in cards if c.get("type") == t), {})


def alts(v) -> list[str]:
    return [x for x in str(v).split("|") if x]


def cate_ok(shop: dict, v: str) -> bool:
    text = " ".join(str(shop.get(k) or "") for k in ("std_cate", "cate", "name", "dishes"))
    return any(x in text for x in alts(v))


def budget_ok(shop: dict, b: float) -> bool:
    try:
        return 0 < float(shop.get("cost") or 0) <= b * 1.1
    except (TypeError, ValueError):
        return False


def core(name: str) -> str:
    return re.split(r"[（(·•\-]", str(name or ""))[0].strip()


def same_shop(a: dict, b: dict) -> bool:
    if a.get("amap_id") and b.get("amap_id"):
        return a["amap_id"] == b["amap_id"]
    x, y = core(a.get("name")), core(b.get("name"))
    return bool(x and y and (x in str(b.get("name")) or y in str(a.get("name"))))


def summarize(text: str, cards: list[dict]) -> str:
    """给评委看的一轮回答：正文 + 卡片摘要（不给整段 JSON）。"""
    out = [text[:1500]]
    for c in cards:
        t = c.get("type")
        if t == "shop_list":
            out.append("［推荐卡片］" + "；".join(f"{x.get('name')}（人均 {x.get('cost')}，距 {x.get('dist_m')} 米）" for x in c.get("items") or []))
        elif t == "options":
            out.append("［选项按钮］" + " / ".join(c.get("items") or []))
        elif t in ("compare", "deals"):
            out.append(f"［{t} 卡片］" + "；".join(str(x.get("name")) for x in c.get("items") or []))
        elif t == "route":
            out.append(f"［路线卡片］{(c.get('origin') or {}).get('name')} → {(c.get('dest') or {}).get('name')}")
    return "\n".join(out)


JUDGE_SYS = (f"你是本地生活推荐助手的评测员。今天是 {datetime.date.today()}，评价里 2026 年的日期不算未来。"
             "助手只覆盖深圳南山的海岸城、深圳湾万象城、科技园一带的 338 家店。"
             "给你一段对话和对最后一轮回答的一条要求，只判这条要求有没有做到，不评别的。"
             '只输出 JSON：{"pass": true 或 false, "reason": "一句话理由"}')


# 评委结果按（对话, 要求）缓存在 run 目录里：--rescore 时同样的输入不重判，结果可复现
JUDGE_CACHE: dict = {}
JUDGE_LOCK = threading.Lock()


def judge(history: list[tuple[str, str]], req: str) -> dict:
    conv = "\n\n".join(("用户：" if r == "user" else "助手：") + t for r, t in history)
    prompt = f"对话：\n{conv}\n\n要求（针对最后一轮助手的回答）：{req}"
    key = hashlib.sha1((JUDGE_SYS + prompt).encode("utf-8")).hexdigest()
    if key in JUDGE_CACHE:
        return JUDGE_CACHE[key]
    # DeepSeek 的推理 token 也算在 max_tokens 里，1024 时偶尔只推理不出结论（10-24 R2 两次 no json），给足并重试
    err = None
    for _ in range(3):
        try:
            r = chat_json(prompt, JUDGE_SYS, max_tokens=4096)
            out = {"ok": bool(r.get("pass")), "detail": str(r.get("reason") or "")}
            break
        except Exception as e:  # noqa: BLE001
            err = e
    else:
        return {"ok": False, "detail": f"评委出错 {type(err).__name__}: {err}", "judge_error": True}
    with JUDGE_LOCK:
        JUDGE_CACHE[key] = out
    return out


def score_turn(t: dict, ans: str, idx: int, prev_list: dict, history: list[tuple[str, str]]) -> list[dict]:
    text, cards = parse(ans)
    k = kind_of(cards)
    sl = card(cards, "shop_list")
    items = sl.get("items") or []
    res = []

    def add(name, ok, detail, layer):
        res.append({"check": name, "ok": bool(ok), "detail": detail, "layer": layer})

    # 默认检查
    if not ans.strip() or ans.startswith("[出错]"):
        add("有回答", False, ans[:200] or "空回答", "稳定性")
        return res
    add("没有 Answer: 开头", not re.match(r"\s*Answer\s*:", text), text[:30], "格式")
    add("正文外没漏 JSON", not re.search(r'\{\s*"[a-z_]+"\s*:', text), "", "格式")
    if any(c.get("type") == "_bad" for c in cards):
        add("卡片 JSON 能解析", False, "", "格式")
    if items:
        miss = [x.get("name") for x in items if str(x.get("name")) not in text]
        add("卡片里的店正文里都有", not miss, "缺 " + "、".join(map(str, miss)) if miss else "", "格式")
        lim = int(t.get("near_m") or NEAR_M)
        far = [f"{x.get('name')} {x.get('dist_m')}m" for x in items if float(x.get("dist_m") or 0) > lim]
        add(f"离锚点 {lim} 米内", not far, "；".join(far), "召回/生成")
        # "符合"只能写用户提过的条件：生效条件里没有预算，却写了"人均 X 内"
        if not any(c.get("k") == "预算" for c in (sl.get("explain") or {}).get("conds") or []):
            fake = [f"{x.get('name')}：{m}" for x in items for m in x.get("matched") or []
                    if re.search(r"人均\s*¥?\s*\d+\s*元?\s*(以内|内|以下)|预算", str(m))]
            add("符合里没有用户没说的预算", not fake, "；".join(fake), "回答生成")
    # 期望
    if t.get("kind"):
        want = t["kind"] if isinstance(t["kind"], list) else [t["kind"]]
        add("回答类型", k in want, f"期望 {'/'.join(want)}，实际 {k}", "意图/澄清")
    conds = (sl.get("explain") or {}).get("conds") or []
    layer_c = "槽位" if idx == 0 else "上下文"
    for key, v in (t.get("conds") or {}).items():
        got = [c.get("v") for c in conds if c.get("k") == key]
        add(f"条件 {key}~{v}", any(any(a in str(g) for a in alts(v)) for g in got), f"实际 {key}={got or '无'}" if sl else "没有推荐卡片", layer_c)
    for key, v in (t.get("conds_not") or {}).items():
        got = [c.get("v") for c in conds if c.get("k") == key]
        add(f"条件里没有 {key}~{v}", not any(any(a in str(g) for a in alts(v)) for g in got), f"实际 {key}={got}", layer_c)
    if t.get("shops"):
        want = t["shops"]
        bad = []
        for x in items:
            s = S.get(x.get("amap_id")) or {}
            why = []
            if want.get("cate") and not cate_ok(s, want["cate"]):
                why.append("品类 " + str(s.get("std_cate") or s.get("cate")))
            if want.get("budget") and not budget_ok(s, want["budget"]):
                why.append("人均 " + str(s.get("cost")))
            if why:
                bad.append(f"{x.get('name')}（{'，'.join(why)}）")
        ok, layer, note = bool(items) and not bad, "召回/生成", list(bad) or ["没有推荐"]
        if not ok:
            # 锚点附近库里有没有满足的店：没有就是数据覆盖问题
            a = (sl.get("anchor_lng"), sl.get("anchor_lat"))
            pool = [s for s in S.values() if a[0] and a[1]
                    and dist_m((float(s["lng"]), float(s["lat"])), (float(a[0]), float(a[1]))) <= NEAR_M
                    and (not want.get("cate") or cate_ok(s, want["cate"])) and (not want.get("budget") or budget_ok(s, want["budget"]))]
            if a[0] and not pool:
                layer = "数据覆盖"
            note.append(f"库里锚点附近满足的 {len(pool)} 家")
        add("推荐的店满足条件", ok, "；".join(note) if not ok else "", layer)
    if t.get("ref"):
        prev = prev_list.get("items") or []
        want = [prev[i - 1] for i in t["ref"] if 0 < i <= len(prev)]
        if k == "route":
            got = [(card(cards, "route").get("dest") or {})]
        else:
            got = (card(cards, k).get("items") or []) if k in ("compare", "deals") else []
        ok = bool(want) and len(want) == len(t["ref"]) and all(any(same_shop(w, g) for g in got) for w in want)
        add("指代对上", ok, f"期望 {[w.get('name') for w in want]}，实际 {[g.get('name') for g in got]}", "指代/会话状态")
    if t.get("not_prev"):
        prev_ids = {x.get("amap_id") for x in prev_list.get("items") or []}
        dup = [x.get("name") for x in items if x.get("amap_id") in prev_ids]
        add("和上一批不重复", items and not dup, "重复 " + "、".join(map(str, dup)) if dup else ("" if items else "没有推荐"), "召回/生成")
    if t.get("not_ref"):
        prev = prev_list.get("items") or []
        ban = [prev[i - 1] for i in t["not_ref"] if 0 < i <= len(prev)]
        dup = [x.get("name") for x in items if any(same_shop(x, b) for b in ban)]
        add("被拒的店没再出现", items and ban and not dup, "又出现 " + "、".join(map(str, dup)) if dup else ("" if items else "没有推荐"), "召回/生成")
    for rx in t.get("text_has") or []:
        add(f"正文有 /{rx}/", re.search(rx, text), "", "回答生成")
    for rx in t.get("text_not") or []:
        m = re.search(rx, text)
        add(f"正文没有 /{rx}/", not m, text[max(0, m.start() - 20):m.end() + 20] if m else "", "回答生成")
    if t.get("judge"):
        j = judge(history, t["judge"])
        add("评委：" + t["judge"], j["ok"], j["detail"], "回答生成")
    return res


def run_case(c: dict, run: str) -> dict:
    uid, conv = f"e6-{run}-{c['id']}", f"e6-{run}-{c['id']}-c1"
    for s in c.get("setup") or []:
        chat(s, uid, f"{conv}-setup")
    if c.get("setup"):
        httpx.post("http://localhost:8090/mem/consolidate", params={"uid": uid},
                   headers={"X-Sidecar-Token": need("SIDECAR_TOKEN")}, timeout=300)
    log = []
    for t in c["turns"]:
        t0 = time.time()
        try:
            ans = chat(t["user"], uid, conv)
        except Exception as e:  # noqa: BLE001
            ans = f"[出错] {type(e).__name__}: {e}"
        log.append({"user": t["user"], "answer": ans, "s": round(time.time() - t0, 1)})
    return {"id": c["id"], "uid": uid, "log": log}


def score_case(c: dict, r: dict) -> dict:
    history, prev_list, turns = [], {}, []
    for i, (t, x) in enumerate(zip(c["turns"], r["log"])):
        text, cards = parse(x["answer"])
        history.append(("user", t["user"]))
        history.append(("bot", summarize(text, cards)))
        checks = score_turn(t, x["answer"], i, prev_list, history)
        turns.append({"user": t["user"], "s": x["s"], "kind": kind_of(cards), "checks": checks,
                      "conds": (card(cards, "shop_list").get("explain") or {}).get("conds"),
                      "funnel": (card(cards, "shop_list").get("explain") or {}).get("funnel"),
                      "shown": [x.get("name") for x in card(cards, "shop_list").get("items") or []]})
        if card(cards, "shop_list"):
            prev_list = card(cards, "shop_list")
    r = dict(r, cat=c["cat"], tags=c.get("tags") or [], turns=turns)
    r["pass"] = all(ch["ok"] for t in turns for ch in t["checks"])
    return r


def versions() -> dict:
    try:
        from e3_run import versions as v
        return v()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def report(run: str, res: list[dict]) -> str:
    v = versions()
    try:
        h = httpx.get("http://localhost:8090/health", timeout=10).json()
    except Exception:  # noqa: BLE001
        h = {}
    L = [f"# E6 对话效果：{run}\n"]
    if CONCLUSION.exists():
        L += [CONCLUSION.read_text(encoding="utf-8").strip(), ""]
    n_turn = sum(len(r["turns"]) for r in res)
    L.append(f"- 被测：发布版智能体（CF-main {v.get('cf_main')}、WF-understand {v.get('understand')}），经 OpenAPI 对话；"
             f"/decide {h.get('decider_model')}；评委 deepseek-flash；{datetime.date.today()}")
    L.append(f"- {len(res)} 条用例、{n_turn} 轮；用例 `eval/data/e6_cases.json`，判分规则见 `eval/e6_run.py` 开头\n")
    checks = [ch for r in res for t in r["turns"] for ch in t["checks"]]
    ok_case = sum(r["pass"] for r in res)
    ok_turn = sum(all(ch["ok"] for ch in t["checks"]) for r in res for t in r["turns"])
    L.append(f"**用例全过 {ok_case}/{len(res)}**；轮次全过 {ok_turn}/{n_turn}；检查项 {sum(c['ok'] for c in checks)}/{len(checks)}\n")
    lat = [t["s"] for r in res for t in r["turns"]]
    if lat:
        q = statistics.quantiles(lat, n=10) if len(lat) >= 10 else [max(lat)] * 9
        L.append(f"单轮延迟：中位 {statistics.median(lat):.1f} 秒，P90 {q[8]:.1f} 秒，最长 {max(lat):.1f} 秒\n")
    L.append("## 按类别\n\n| 类别 | 用例全过 | 检查项 |\n|---|---|---|")
    by = defaultdict(list)
    for r in res:
        by[r["cat"]].append(r)
    for cat, rs in by.items():
        cs = [ch for r in rs for t in r["turns"] for ch in t["checks"]]
        L.append(f"| {cat} | {sum(r['pass'] for r in rs)}/{len(rs)} | {sum(c['ok'] for c in cs)}/{len(cs)} |")
    L.append("\n## 失败归因（自动初判）\n\n| 层 | 失败检查项 | 涉及用例 |\n|---|---|---|")
    fl = Counter(ch["layer"] for ch in checks if not ch["ok"])
    cases_of = defaultdict(set)
    for r in res:
        for t in r["turns"]:
            for ch in t["checks"]:
                if not ch["ok"]:
                    cases_of[ch["layer"]].add(r["id"])
    for layer in sorted(fl, key=lambda x: -fl[x]):
        L.append(f"| {layer} | {fl[layer]} | {'、'.join(sorted(cases_of[layer]))} |")
    L.append("\n## 失败明细\n")
    for r in res:
        if r["pass"]:
            continue
        L.append(f"### {r['id']}（{r['cat']}：{'、'.join(r['tags'])}）\n")
        for i, t in enumerate(r["turns"]):
            bad = [ch for ch in t["checks"] if not ch["ok"]]
            L.append(f"- 第 {i + 1} 轮「{t['user']}」→ {t['kind']}，{t['s']} 秒" + (f"，推荐 {'、'.join(t['shown'])}" if t["shown"] else ""))
            for ch in bad:
                L.append(f"  - ✗ [{ch['layer']}] {ch['check']}" + (f"：{ch['detail']}" if ch["detail"] else ""))
        L.append("")
    L.append("## 全部用例\n\n| 用例 | 类别 | 结果 | 轮数 | 失败项 |\n|---|---|---|---|---|")
    for r in res:
        bad = [ch["check"] for t in r["turns"] for ch in t["checks"] if not ch["ok"]]
        L.append(f"| {r['id']} | {r['cat']} | {'✓' if r['pass'] else '✗'} | {len(r['turns'])} | {'；'.join(bad)[:120]} |")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default=datetime.datetime.now().strftime("e6-%m%d-%H%M"))
    ap.add_argument("-j", type=int, default=3)
    ap.add_argument("--only", default="")
    ap.add_argument("--rescore", action="store_true")
    a = ap.parse_args()
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    if a.only:
        keep = set(a.only.split(","))
        cases = [c for c in cases if c["id"] in keep]
    out_dir = ROOT / "eval" / "runs" / a.run
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_f = out_dir / "e6_raw.jsonl"
    old = {r["id"]: r for r in map(json.loads, raw_f.read_text(encoding="utf-8").splitlines())} if raw_f.exists() else {}
    if a.rescore:
        raw = old
    else:
        done = 0

        def one(c):
            nonlocal done
            r = run_case(c, a.run)
            done += 1
            print(f"[{done}/{len(cases)}] {c['id']} " + " | ".join(f"{x['s']}s" for x in r["log"]), flush=True)
            return r
        with ThreadPoolExecutor(a.j) as ex:
            raw = {r["id"]: r for r in ex.map(one, cases)}
        # --only 跑的用例合并进同一个 run 已有的结果，报告按这个 run 里的全部用例出
        raw = {**old, **raw}
        raw_f.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in raw.values()), encoding="utf-8")
    all_cases = json.loads(CASES.read_text(encoding="utf-8"))
    cache_f = out_dir / "judge_cache.json"
    if cache_f.exists():
        JUDGE_CACHE.update(json.loads(cache_f.read_text(encoding="utf-8")))
    with ThreadPoolExecutor(4) as ex:
        res = list(ex.map(lambda c: score_case(c, raw[c["id"]]), [c for c in all_cases if c["id"] in raw]))
    cache_f.write_text(json.dumps(JUDGE_CACHE, ensure_ascii=False), encoding="utf-8")
    (out_dir / "e6_results.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in res), encoding="utf-8")
    rep = ROOT / "eval" / "reports" / f"E6_{a.run}.md"
    rep.write_text(report(a.run, res), encoding="utf-8")
    print(f"用例全过 {sum(r['pass'] for r in res)}/{len(res)}，报告 {rep}")


if __name__ == "__main__":
    main()
