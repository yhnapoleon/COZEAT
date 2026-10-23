"""E2 准备：同一批笔记按几种切分方式各建一个临时知识库，再建一个检索探针工作流。

语料 = KB-review 里那 227 条小红书提及所在的笔记（109 篇，标题 + 正文 + 前 10 条评论，和抽取时喂给模型的一样；
纯图笔记用读图结果）。点评那 1 条没有原文，不进 E2。

切分方式（Coze 的切分按分隔符切开、每段单独成块、不合并；超过上限按字数硬切）：
  auto      平台默认（chunk_type 0：按换行切，上限 800 字，重叠 10%）——一行一块
  fix200    固定长度：不按分隔符，200 字一块，重叠 10%
  note      整篇一块
  mention   现在的做法：一条店铺提及一段，段首带"amap_id | 店名 | 商圈 | 品类 | 来源 | 日期"（load_kb.segment）
  mention_np  同上去掉段首前缀，看前缀的作用
每篇笔记一个文档（文档名 = 笔记 key），检索结果的 documentId 就能对回笔记。

探针工作流 la_e2_probe：输入 query，并行跑 8 个检索节点（每种切分的混合检索；mention 再加纯语义、纯全文；
生产库 KB-review 按主干的参数 top8 / 0.2），各节点结果从调试运行的 nodeResults 里取。

用法：python -I -X utf8 eval/e2_build.py [kb|probe|all]
记录：eval/data/e2_kbs.json（知识库 id、文档 id→笔记 key、探针工作流 id）
"""
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bridge"))
sys.path.insert(0, str(ROOT))
import canvas as cv  # noqa: E402
from coze_client import Coze  # noqa: E402
from load_kb import SEP, segment  # noqa: E402
from workflow_api import create_workflow, save_workflow, test_run  # noqa: E402

DATA = ROOT / "eval" / "data"
REC = DATA / "e2_kbs.json"
IDS = json.loads((ROOT / "lifeagent_ids.json").read_text(encoding="utf-8"))
VARIANTS = {
    "auto": {"chunk_type": 0},
    "fix200": {"chunk_type": 1, "separator": "⁣", "max_tokens": 200, "overlap": 10},
    "note": {"chunk_type": 1, "separator": "⁣", "max_tokens": 4000, "overlap": 0},
    "mention": {"chunk_type": 1, "separator": "#####", "max_tokens": 800, "overlap": 0},
    "mention_np": {"chunk_type": 1, "separator": "#####", "max_tokens": 800, "overlap": 0},
}
# (节点标题, 知识库, 检索模式 0 语义 / 1 混合 / 20 全文, top_k, min_score)
PROBES = [("auto|hybrid", "auto", 1, 10, 0.0), ("fix200|hybrid", "fix200", 1, 10, 0.0), ("note|hybrid", "note", 1, 10, 0.0),
          ("mention|hybrid", "mention", 1, 10, 0.0), ("mention_np|hybrid", "mention_np", 1, 10, 0.0),
          ("mention|semantic", "mention", 0, 10, 0.0), ("mention|fulltext", "mention", 20, 10, 0.0),
          ("prod|hybrid", "prod", 1, 8, 0.2),
          # 第一轮跑完发现混合检索明显不如纯语义（Coze 的 RRF 取最大值、同分随机），切分对照改在纯语义下再比一次
          ("auto|semantic", "auto", 0, 10, 0.0), ("fix200|semantic", "fix200", 0, 10, 0.0),
          ("note|semantic", "note", 0, 10, 0.0), ("mention_np|semantic", "mention_np", 0, 10, 0.0)]


def load_corpus():
    """返回 shops、notes（key → 笔记）、by_note（笔记 key → 该笔记里进了 KB 的提及）。"""
    shops = {}
    for line in (ROOT / "data" / "processed" / "shops.jsonl").read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        shops[r["amap_id"]] = r
    notes = {}
    for f in sorted((ROOT / "data" / "raw" / "ugc" / "xhs_browser").glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                notes[r["key"]] = r
    loaded = set(json.loads((ROOT / "data" / "processed" / "kb_loaded.json").read_text(encoding="utf-8")))
    by_note = defaultdict(list)
    for line in (ROOT / "data" / "processed" / "ugc_mentions.jsonl").read_text(encoding="utf-8").splitlines():
        m = json.loads(line)
        if m["key"] in loaded and m.get("amap_id") in shops and m["doc_id"] in notes:
            by_note[m["doc_id"]].append(m)
    return shops, notes, dict(by_note)


def note_text(n: dict) -> str:
    """和 etl/xhs_ingest.py 喂给抽取模型的文本一致：标题、正文、评论。"""
    text = n.get("text") or ""
    cs = [c.get("t") for c in n.get("comments") or [] if isinstance(c, dict) and c.get("t")]
    if cs:
        text += "\n\n评论：\n" + "\n".join(f"- {c}" for c in cs)
    return (n.get("title") or "") + "\n" + text


def seg_np(m: dict) -> str:
    """mention 段去掉第一行前缀。"""
    return m["_seg"].split("\n", 1)[1] if "\n" in m["_seg"] else m["_seg"]


def contents(variant: str, shops, notes, by_note) -> dict:
    out = {}
    for key, ms in sorted(by_note.items()):
        for m in ms:
            m["_seg"] = segment(m, shops[m["amap_id"]])
        if variant in ("auto", "fix200", "note"):
            out[key] = note_text(notes[key])
        elif variant == "mention":
            out[key] = SEP.join(m["_seg"] for m in ms)
        else:
            out[key] = SEP.join(seg_np(m) for m in ms)
    return out


def build_kbs(c: Coze, rec: dict) -> None:
    shops, notes, by_note = load_corpus()
    print(f"语料：{len(by_note)} 篇笔记，{sum(map(len, by_note.values()))} 条提及，{len({m['amap_id'] for ms in by_note.values() for m in ms})} 家店")
    rec.setdefault("kbs", {})
    rec.setdefault("docs", {})
    for v, cs in VARIANTS.items():
        if v in rec["kbs"] and rec["docs"].get(v):
            print("已建", v)
            continue
        if v not in rec["kbs"]:
            body = c.post("/api/knowledge/create", {
                "name": f"e2_{v}", "description": f"E2 切分对照：{v}（评测用，可删）", "space_id": c.space_id(),
                "icon_uri": "default_icon/default_dataset_icon.png", "format_type": 0, "biz_id": "0", "project_id": "0"})
            rec["kbs"][v] = str(body.get("dataset_id") or body["data"]["dataset_id"])
            REC.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        docs = contents(v, shops, notes, by_note)
        strat = dict({"remove_extra_spaces": False, "remove_urls_emails": False}, **VARIANTS[v])
        items = list(docs.items())
        for i in range(0, len(items), 20):  # 一次 20 篇
            part = items[i:i + 20]
            c.post("/api/knowledge/document/create", {
                "dataset_id": rec["kbs"][v], "format_type": 0, "chunk_strategy": strat,
                "document_bases": [{"name": k, "source_info": {"document_source": 2, "custom_content": t}} for k, t in part]})
        for _ in range(60):
            r = c.post("/api/knowledge/document/list", {"dataset_id": rec["kbs"][v], "page": 0, "size": 200})
            infos = r.get("document_infos") or []
            if len(infos) >= len(docs) and all(str(d.get("status")) == "1" for d in infos):
                break
            time.sleep(3)
        rec["docs"][v] = {str(d["document_id"]): d["name"] for d in infos}
        n_slices = sum(int(d.get("slice_count") or 0) for d in infos)
        rec.setdefault("slices", {})[v] = n_slices
        REC.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{v}：{len(infos)} 个文档，{n_slices} 块，状态 {sorted({str(d.get('status')) for d in infos})}", flush=True)


def build_probe(c: Coze, rec: dict) -> None:
    kb = dict(rec["kbs"], prod=IDS["kbs"]["kb_review"])
    nodes, edges = [cv.entry([cv.out("query")])], []
    for i, (title, v, strategy, k, ms) in enumerate(PROBES):
        nid = cv.new_id()
        n = cv.knowledge_retriever(nid, title, [kb[v]], cv.ref(cv.ENTRY_ID, "query"), top_k=k, min_score=ms,
                                   pos=(400, i * 160))
        for p in n["data"]["inputs"]["datasetParam"]:
            if p["name"] == "strategy":
                p["input"]["value"]["content"] = strategy
        n["data"]["outputs"] = [{"type": "list", "name": "outputList", "schema": {"type": "object", "schema": [
            cv.out("documentId", "string"), cv.out("output", "string")]}}]
        nodes.append(n)
        edges += [cv.edge(cv.ENTRY_ID, nid), cv.edge(nid, cv.EXIT_ID)]
    nodes.append(cv.exit_node([cv.param("query", cv.ref(cv.ENTRY_ID, "query"))], pos=(900, 0)))
    if not rec.get("probe"):
        rec["probe"] = create_workflow(c, "la_e2_probe", "E2 检索探针：几种切分方式的知识库并行检索（评测用）")
    r = save_workflow(c, rec["probe"], cv.canvas(nodes, edges))
    REC.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    print("探针工作流", rec["probe"], r.get("code"), r.get("msg") or "")
    d = test_run(c, rec["probe"], {"query": "小炳胜的烧鹅怎么样"}, timeout_s=60)
    print("试跑 status", d.get("executeStatus"), d.get("reason") or "")
    for n in d.get("nodeResults") or []:
        print(" ", n.get("NodeName"), n.get("nodeExeCost"), (n.get("errorInfo") or "")[:200], str(n.get("output"))[:160])


def main():
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    rec = json.loads(REC.read_text(encoding="utf-8")) if REC.exists() else {}
    c = Coze()
    c.login()
    if what in ("kb", "all"):
        build_kbs(c, rec)
    if what in ("probe", "all"):
        build_probe(c, rec)


if __name__ == "__main__":
    main()
