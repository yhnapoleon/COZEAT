"""给 E2 打分并出报告：几种切分方式在找店、问店两类查询上的召回，以及主干按店取证据的命中率。

用法：python -I -X utf8 eval/e2_score.py <run>
输出：eval/reports/E2_<run>.md、eval/runs/<run>/e2_metrics.json

检索结果怎么对回店和笔记：
- 每篇笔记一个文档，documentId → 笔记 key。
- mention / mention_np / prod：块的文本对回生成它的那条提及（去空白后比对），得到店。
- auto / fix200 / note（原文切块）：块里出现了这篇笔记某条提及的店名（笔记里写的名字、高德名去括号）或原话前 10 个字，
  就算这家店的；都没有、而这篇笔记只提到一家店时，算这家店的；多店笔记里认不出的块不算任何店。
判分：
- 找店：检索结果按块的顺序去重成店的排序，看金标店的名次：hit@1、hit@5、MRR@10。
- 问店：前 5 块里有没有"金标笔记里、属于金标店"的块（笔记命中@5）；有没有任何属于金标店的块（同店证据@5）；
  前 5 块里属于金标店的比例（越高噪声越少）。
- 主干路径（prod，KB-review，top8 / 0.2）：前 8 块里本店的段数，和主干一样按段首 amap_id 过滤、去掉没有正文的段、最多留 3 段。
"""
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bridge"))
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT))
from e2_build import PROBES, REC, load_corpus, seg_np  # noqa: E402
from e2_run import queries  # noqa: E402
from load_kb import segment  # noqa: E402

# 人工过目后剔除（10-21）：提及挂错了店（金标错）或找店查询太泛、定不了金标
DROP = {"a005": "提及写的是 Professor Lee，挂在 Le Mad Maison", "a024": "写的是米其林三星 GENTLE L，挂在黑手制面",
        "a060": "写的是 glance，挂在 La Moda", "f005": "查询太泛", "f014": "查询太泛", "f017": "查询太泛",
        "f025": "查询是找差评", "f045": "查询是避雷"}


def ws(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def brand_names(m: dict, shop: dict) -> list[str]:
    out = {str(m["mention"].get("shop") or "")}
    nm = shop["name"] or ""
    b = nm.split("(")[0].split("（")[0]
    out |= {b, b.split("·")[0], b.split("•")[0]}
    return [x for x in out if len(x) >= 2]


class Mapper:
    def __init__(self, rec):
        self.shops, self.notes, self.by_note = load_corpus()
        self.docs = rec["docs"]
        self.seg = {"mention": {}, "mention_np": {}, "prod": {}}
        for key, ms in self.by_note.items():
            for m in ms:
                m["_seg"] = segment(m, self.shops[m["amap_id"]])
                self.seg["mention"][ws(m["_seg"])] = (key, m["amap_id"])
                self.seg["mention_np"][ws(seg_np(m))] = (key, m["amap_id"])
        # 生产库还有 1 条点评提及（不在 E2 语料里），按段首 amap_id 认店即可
        for t, v in self.seg["mention"].items():
            self.seg["prod"][t] = v

    def attribute(self, variant: str, doc_id: str, text: str) -> tuple[str, set]:
        """→ (笔记 key, 店 amap_id 集合)。"""
        if variant == "prod":
            t = ws(text)
            if t in self.seg["prod"]:
                k, a = self.seg["prod"][t]
                return k, {a}
            head = (text.strip().split("\n")[0].split("|") or [""])[0].strip()
            return "", ({head} if head in self.shops else set())
        note = self.docs[variant].get(doc_id, "")
        if variant in ("mention", "mention_np"):
            t = ws(text)
            v = self.seg[variant].get(t)
            if v:
                return v[0], {v[1]}
            # 兜底：块是某条提及文本的一部分
            for m in self.by_note.get(note, []):
                s = ws(m["_seg"] if variant == "mention" else seg_np(m))
                if t and (t in s or s in t):
                    return note, {m["amap_id"]}
            return note, set()
        ms = self.by_note.get(note, [])
        got = set()
        for m in ms:
            q = ws(m["mention"].get("quote") or "")[:10]
            if any(b in text for b in brand_names(m, self.shops[m["amap_id"]])) or (len(q) >= 6 and q in ws(text)):
                got.add(m["amap_id"])
        if not got and len({m["amap_id"] for m in ms}) == 1:
            got = {ms[0]["amap_id"]}
        return note, got


def pct(a, b):
    return f"{a / b:.0%}" if b else "—"


def main():
    # 可以给多个 run：同一查询的检索节点结果合并（第二轮只补跑了纯语义节点）
    runs = sys.argv[1:]
    run = "+".join(runs)
    rd = ROOT / "eval" / "runs" / runs[0]
    rec = json.loads(REC.read_text(encoding="utf-8"))
    meta = json.loads((rd / "meta.json").read_text(encoding="utf-8"))
    mp = Mapper(rec)
    allq = {q["id"]: q for q in queries()}
    qs = {i: q for i, q in allq.items() if q["type"] in ("ask", "find")}
    merged = {}
    for rn in runs:
        for x in (ROOT / "eval" / "runs" / rn / "e2_results.jsonl").read_text(encoding="utf-8").splitlines():
            if not x.strip():
                continue
            r = json.loads(x)
            if r["id"] in merged and merged[r["id"]].get("status") == 2:
                merged[r["id"]]["nodes"].update(r.get("nodes") or {})
                merged[r["id"]]["node_s"].update(r.get("node_s") or {})
            else:
                merged[r["id"]] = r
    res = list(merged.values())
    res = [r for r in res if r["id"] not in DROP]
    failed = [r for r in res if r.get("status") != 2]
    titles = [p[0] for p in PROBES]
    variant = {p[0]: p[1] for p in PROBES}

    def ranked(r, title):
        out = []
        for doc_id, text in (r.get("nodes") or {}).get(title, []):
            note, shops = mp.attribute(variant[title], doc_id, text)
            out.append((note, shops, text))
        return out

    M = {"find": defaultdict(lambda: defaultdict(list)), "ask": defaultdict(lambda: defaultdict(list))}
    miss_find = defaultdict(list)
    for r in res:
        if r["type"] not in ("find", "ask") or r.get("status") != 2:
            continue
        q = qs[r["id"]]
        for t in titles:
            if t == "prod|hybrid":
                continue
            rk = ranked(r, t)
            if r["type"] == "find":
                order = []
                for _, shops, _ in rk:
                    for s in sorted(shops):
                        if s not in order:
                            order.append(s)
                pos = order.index(q["gold_shop"]) + 1 if q["gold_shop"] in order else 0
                M["find"][t]["h1"].append(pos == 1)
                M["find"][t]["h5"].append(0 < pos <= 5)
                M["find"][t]["mrr"].append(1 / pos if 0 < pos <= 10 else 0)
                if t == "mention|hybrid" and not 0 < pos <= 5:
                    miss_find[r["id"]] = [mp.shops[s]["name"][:12] for s in order[:5]]
            else:
                top = rk[:5]
                M["ask"][t]["note5"].append(any(n == q["gold_note"] and q["gold_shop"] in s for n, s, _ in top))
                M["ask"][t]["shop5"].append(any(q["gold_shop"] in s for _, s, _ in top))
                M["ask"][t]["purity"].append(sum(q["gold_shop"] in s for _, s, _ in top) / len(top) if top else 0)
                M["ask"][t]["len"].append(statistics.mean(len(x[2]) for x in top) if top else 0)

    L = []
    w = L.append
    w(f"# E2 知识库召回与切分对照：{run}\n")
    w(f"- 时间：{meta.get('started')} → {meta.get('finished') or '未跑完'}；探针工作流 la_e2_probe {meta['probe']}")
    n_find = sum(1 for r in res if r["type"] == "find")
    n_ask = sum(1 for r in res if r["type"] == "ask")
    w(f"- 语料：KB-review 里 227 条小红书提及所在的 109 篇笔记（161 家店），每种切分各建一个知识库；"
      f"查询 {n_find} 条找店、{n_ask} 条问店（DeepSeek 从提及生成，人工剔除 {len(DROP)} 条，见文末），"
      f"主干路径 {sum(1 for r in res if r['type'] == 'prod')} 家店。运行失败 {len(failed)} 条")
    w("- 检索：Coze 知识库节点，混合 = 向量 + 全文按 RRF 合并，不开重排和改写；对照用 top10、min_score 0；"
      "主干路径照主干参数 top8、min_score 0.2")
    w("- 向量模型：Coze Studio .env 配的 OpenAI text-embedding-3-small\n")
    concl = ROOT / "eval" / "data" / "e2_conclusion.md"
    if concl.exists():
        w(concl.read_text(encoding="utf-8"))

    w("## 1. 切分方式\n")
    w("| 知识库 | 切法 | 块数 | 平均每篇 |")
    w("|---|---|---|---|")
    desc = {"auto": "平台默认：按换行切，上限 800 字", "fix200": "固定 200 字，重叠 10%", "note": "整篇一块",
            "mention": "一条提及一段 + 店名前缀（现在的做法）", "mention_np": "一条提及一段，无前缀"}
    for v, d in desc.items():
        n = (meta.get("slices") or {}).get(v) or 0
        w(f"| {v} | {d} | {n} | {n / 109:.1f} |")
    w("")

    w("## 2. 找店（不点名，说地点和需求）\n")
    w(f"{n_find} 条。金标只有出发的那一家，别的店也可能合适，所以是下限。\n")
    w("| 检索 | hit@1 | hit@5 | MRR@10 |")
    w("|---|---|---|---|")
    for t in titles:
        if t in M["find"]:
            d = M["find"][t]
            w(f"| {t} | {pct(sum(d['h1']), len(d['h1']))} | {pct(sum(d['h5']), len(d['h5']))} | {statistics.mean(d['mrr']):.2f} |")
    w("")
    w("## 3. 问店（点名问某个方面）\n")
    w(f"{n_ask} 条。\n")
    w("| 检索 | 笔记命中@5 | 同店证据@5 | 前 5 块里本店占比 | 前 5 块平均字数 |")
    w("|---|---|---|---|---|")
    for t in titles:
        if t in M["ask"]:
            d = M["ask"][t]
            w(f"| {t} | {pct(sum(d['note5']), len(d['note5']))} | {pct(sum(d['shop5']), len(d['shop5']))} | "
              f"{statistics.mean(d['purity']):.0%} | {statistics.mean(d['len']):.0f} |")
    w("")

    # ---- 主干路径 ----
    own_total = defaultdict(int)
    for ms in mp.by_note.values():
        for m in ms:
            own_total[m["amap_id"]] += 1
    w("## 4. 主干路径：按店取证据（KB-review，top8 / 0.2）\n")
    w("prod 是生产库 KB-review 按主干参数（top8、min_score 0.2）；mention|* 是同样内容的对照库，取前 8 块、不设分数线。\n")
    w("| 检索 | 查询写法 | 店数 | 至少拿到 1 段本店 | 本店段召回（前 8） | 前 8 里别家的段 | 过滤后平均留下 |")
    w("|---|---|---|---|---|---|---|")
    prod_m = {}
    for title, typ, nm in [("prod|hybrid", "prod", "品牌名 + amap_id（现在）"), ("prod|hybrid", "prod_brand", "只有品牌名"),
                           ("mention|hybrid", "prod", "品牌名 + amap_id"), ("mention|semantic", "prod", "品牌名 + amap_id"),
                           ("mention|fulltext", "prod", "品牌名 + amap_id"), ("mention|semantic", "prod_brand", "只有品牌名")]:
        rows = [r for r in res if r["type"] == typ and r.get("status") == 2]
        any1, rec_, other, kept = [], [], [], []
        for r in rows:
            aid = allq[r["id"]]["gold_shop"]
            rk = ranked(r, title)[:8]
            mine = [x for x in rk if aid in x[1]]
            nonempty = [x for x in mine if len(x[2].strip().split("\n")) > 1 and "".join(x[2].strip().split("\n")[1:]).strip()]
            any1.append(bool(mine))
            rec_.append(min(1.0, len(mine) / min(own_total.get(aid, 0) or 1, 8)))
            other.append(sum(1 for x in rk if aid not in x[1]))
            kept.append(min(len(nonempty), 3))
        if rows:
            prod_m[f"{title} {typ}"] = {"n": len(rows), "any": sum(any1) / len(rows), "recall": statistics.mean(rec_)}
            w(f"| {title} | {nm} | {len(rows)} | {pct(sum(any1), len(rows))} | {statistics.mean(rec_):.0%} | "
              f"{statistics.mean(other):.1f} | {statistics.mean(kept):.1f} |")
    w("")

    w("## 5. 检索节点耗时（秒，中位）\n")
    w("| " + " | ".join(titles) + " |")
    w("|" + "---|" * len(titles))
    w("| " + " | ".join(f"{statistics.median([r['node_s'].get(t) for r in res if (r.get('node_s') or {}).get(t) is not None] or [0]):.2f}"
                         for t in titles) + " |")
    w("")
    w("## 6. 现在的做法（mention|hybrid）找店没进前 5 的\n")
    for i, top in miss_find.items():
        q = qs[i]
        w(f"- {i}「{q['query']}」金标 {q['shop_name']}；前 5：{'、'.join(top) or '（空）'}")
    w("\n## 7. 剔除的查询\n")
    for i, why in DROP.items():
        w(f"- {i}「{qs[i]['query']}」：{why}")
    if failed:
        w("\n## 运行失败\n")
        for r in failed:
            w(f"- {r['id']}：{r.get('reason')}")

    out = ROOT / "eval" / "reports" / f"E2_{run}.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    metrics = {"find": {t: {"h1": statistics.mean(d["h1"]), "h5": statistics.mean(d["h5"]), "mrr": statistics.mean(d["mrr"])}
                        for t, d in M["find"].items()},
               "ask": {t: {"note5": statistics.mean(d["note5"]), "shop5": statistics.mean(d["shop5"]),
                           "purity": statistics.mean(d["purity"])} for t, d in M["ask"].items()},
               "prod": prod_m}
    (rd / "e2_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=1), encoding="utf-8")
    print("报告", out.relative_to(ROOT))


if __name__ == "__main__":
    main()
