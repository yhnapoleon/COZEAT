"""FAQ 检索评测：定 WF-faq 的 min_score。

查询两类：
  in   每条 FAQ 让 DeepSeek 换个说法问一次（不许照抄标准问法和"也可能这样问"），金标是这条 FAQ
  oos  库里答不了的（找店、对比、路线、库里没有的规则、别的业务），不该检索到任何条目
探针工作流 la_faq_probe：同一个 KB-faq、纯语义、top3，按几个 min_score 并行各跑一个节点（节点输出不带分数，只能这样比）。

用法：python -I -X utf8 eval/faq_eval.py gen        # 生成 data/faq/faq_eval.jsonl（有就跳过，--regen 重来）
      python -I -X utf8 eval/faq_eval.py probe      # 建探针工作流
      python -I -X utf8 eval/faq_eval.py run [--run 名字]
输出：eval/reports/FAQ_<run>.md
"""
import argparse
import datetime
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bridge"))
sys.path.insert(0, str(ROOT))
import canvas as cv  # noqa: E402
from coze_client import Coze  # noqa: E402
from common.llm import chat  # noqa: E402
from workflow_api import create_workflow, save_workflow, test_run  # noqa: E402

FAQ = ROOT / "data" / "faq" / "faq.jsonl"
QS = ROOT / "data" / "faq" / "faq_eval.jsonl"
REC = ROOT / "eval" / "data" / "faq_probe.json"
THRESH = [0.65, 0.7, 0.72, 0.74, 0.76, 0.78, 0.8, 0.85]  # 分数是 (cos+1)/2，0.75 对应 cos 0.5（milvus_searchstore.go）
OOS = ["海岸城附近有什么好吃的火锅", "小炳胜和陶陶居哪家好", "从科技园去海岸城怎么走", "南山有什么网红店",
       "记住我不吃香菜", "今天天气怎么样", "帮我写一份周报", "推荐一部电影", "外卖超时了怎么赔偿", "抖音直播怎么开",
       "酒店团购能不能退", "电影票买错了能退吗", "打车软件怎么开发票", "我想投诉快递", "抖音账号被封了怎么办",
       "健身房年卡能转让吗", "医院挂号怎么取消", "第一家人均多少", "附近有停车场吗",
       "能带宠物进店吗", "有没有儿童座椅", "支付宝能付吗", "怎么注册抖音来客商家",
       "商家怎么上架团购", "团购佣金是多少", "你会不会写诗", "讲个笑话", "我好无聊"]


def gen():
    rows = [json.loads(x) for x in FAQ.read_text(encoding="utf-8").splitlines() if x.strip()]
    out = []
    for i in range(0, len(rows), 12):
        part = rows[i:i + 12]
        items = "\n".join(f"{j + 1}. 标准问法：{f['q']}；已有问法：{'；'.join(f.get('alts') or [])}；答案：{f['a'][:120]}"
                          for j, f in enumerate(part))
        prompt = ("下面每条是一个 FAQ。请为每条写一个用户真实会问的新问题，答案能回答它。\n要求：换成和标准问法、已有问法都不一样的说法，"
                  "不要照抄其中连续 4 个字以上；可以带一点用户自己的情况（比如\"上周买的券\"\"我和朋友\"）；像手机打字，8 到 30 个字。\n\n"
                  f"{items}\n\n只输出 JSON 数组，每项 {{\"k\": 编号, \"query\": \"用户的问题\"}}，不要输出别的文字。")
        got = {}
        for _ in range(3):
            raw = chat(prompt, max_tokens=16000)
            m = re.search(r"\[.*\]", raw, re.S)
            try:
                got = {int(x["k"]): x["query"] for x in json.loads(m.group(0))} if m else {}
            except (ValueError, KeyError, TypeError):
                got = {}
            if len(got) >= len(part):
                break
        for j, f in enumerate(part):
            if got.get(j + 1):
                out.append({"id": f"i_{f['id']}", "type": "in", "gold": f["id"], "query": got[j + 1]})
        print("生成", i + len(part), flush=True)
    out += [{"id": f"o{i + 1:02d}", "type": "oos", "gold": "", "query": q} for i, q in enumerate(OOS)]
    QS.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in out), encoding="utf-8")
    print("写入", QS.relative_to(ROOT), len(out))


def probe(c: Coze):
    ids = json.loads((ROOT / "lifeagent_ids.json").read_text(encoding="utf-8"))
    rec = json.loads(REC.read_text(encoding="utf-8")) if REC.exists() else {}
    nodes, edges = [cv.entry([cv.out("query")])], []
    for i, t in enumerate(THRESH):
        nid = cv.new_id()
        nodes.append(cv.knowledge_retriever(nid, f"min{t}", [str(ids["kbs"]["kb_faq"])], cv.ref(cv.ENTRY_ID, "query"),
                                            top_k=3, min_score=t, pos=(400, i * 150), strategy=0))
        edges += [cv.edge(cv.ENTRY_ID, nid), cv.edge(nid, cv.EXIT_ID)]
    nodes.append(cv.exit_node([cv.param("query", cv.ref(cv.ENTRY_ID, "query"))], pos=(900, 0)))
    if not rec.get("probe"):
        rec["probe"] = create_workflow(c, "la_faq_probe", "FAQ 检索阈值探针（评测用）")
    save_workflow(c, rec["probe"], cv.canvas(nodes, edges))
    REC.parent.mkdir(parents=True, exist_ok=True)
    REC.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    print("探针", rec["probe"])


def faq_id(text: str, by_q: dict) -> str:
    m = re.search(r"问：(.+)", text or "")
    return by_q.get(m.group(1).strip(), "?") if m else "?"


def run(c: Coze, name: str):
    rec = json.loads(REC.read_text(encoding="utf-8"))
    faqs = [json.loads(x) for x in FAQ.read_text(encoding="utf-8").splitlines() if x.strip()]
    by_q = {f["q"]: f["id"] for f in faqs}
    qs = [json.loads(x) for x in QS.read_text(encoding="utf-8").splitlines() if x.strip()]

    def one(q):
        d = test_run(c, rec["probe"], {"query": q["query"]}, timeout_s=60)
        got = {}
        for n in d.get("nodeResults") or []:
            if (n.get("NodeName") or "").startswith("min"):
                o = json.loads(n.get("output") or "{}")
                got[n["NodeName"]] = [faq_id(x.get("output"), by_q) for x in o.get("outputList") or []]
        return dict(q, got=got, status=d.get("executeStatus"))

    with ThreadPoolExecutor(4) as ex:
        res = list(ex.map(one, qs))
    out_dir = ROOT / "eval" / "runs" / name
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "faq_results.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in res), encoding="utf-8")
    ins = [r for r in res if r["type"] == "in"]
    oos = [r for r in res if r["type"] == "oos"]
    L = [f"# FAQ 检索阈值：{name}\n", f"- KB-faq {len(faqs)} 条，纯语义、top3；查询 {len(ins)} 条库内改写（DeepSeek 生成）、{len(oos)} 条库里答不了的",
         f"- 失败 {sum(r['status'] != 2 for r in res)} 条",
         "- 不算 hit@1：Coze 知识库节点返回的顺序是切片存储顺序，不是相关度（retrieve.go packResults 回表没有 ORDER BY）\n",
         "| min_score | 库内：金标在返回里 | 库内有返回 | 库外误返回 | 库外误返回的例子 |", "|---|---|---|---|---|"]
    for t in THRESH:
        k = f"min{t}"
        h3 = sum(1 for r in ins if r["gold"] in (r["got"].get(k) or []))
        any_in = sum(1 for r in ins if r["got"].get(k))
        fp = [r for r in oos if r["got"].get(k)]
        L.append(f"| {t} | {h3}/{len(ins)} | {any_in}/{len(ins)} | {len(fp)}/{len(oos)} | "
                 f"{'、'.join(r['query'] + '→' + ','.join(r['got'][k]) for r in fp[:4])} |")
    L.append("\n## 库内没命中的（min0.65）\n")
    for r in ins:
        if r["gold"] not in (r["got"].get("min0.65") or []):
            L.append(f"- {r['gold']}「{r['query']}」→ {r['got'].get('min0.65')}")
    rep = ROOT / "eval" / "reports" / f"FAQ_{name}.md"
    rep.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L[3:3 + len(THRESH) + 2]))
    print("报告", rep.relative_to(ROOT))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd")
    ap.add_argument("--run", default=datetime.datetime.now().strftime("faq-%m%d-%H%M"))
    ap.add_argument("--regen", action="store_true")
    a = ap.parse_args()
    if a.cmd == "gen":
        if QS.exists() and not a.regen:
            print("已有", QS.relative_to(ROOT))
            return
        gen()
        return
    c = Coze()
    c.login()
    if a.cmd == "probe":
        probe(c)
    elif a.cmd == "run":
        run(c, a.run)


if __name__ == "__main__":
    main()
