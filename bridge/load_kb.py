"""把对齐到门店的 UGC 提及写进 KB-review：一条提及一段，段首带上下文前缀（设计文档 3.3）。

前缀格式：amap_id | 店名 | 商圈 | 品类 | 来源 | 日期
入库条件是 etl.ugc.kb_eligible：对上了门店库里的店，且有原话或任一事实字段（全空的进 KB 只剩一行前缀）。
默认增量：只写 kb_loaded.json 里没有的新提及，写成功后追加记录。
--rebuild：删掉 KB-review 里全部文档，按当前 ugc_mentions.jsonl 整库重写，kb_loaded.json 换成这次写入的 key。
  对齐规则改了、提及重算过（key 会变）之后用这个；增量写不会删掉挂错店的旧段。
用法：python -I -X utf8 bridge/load_kb.py [--rebuild]   之后跑 etl/audit_mentions.py --kb 核对
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
from coze_client import Coze  # noqa: E402
from etl.ugc import kb_eligible  # noqa: E402

IDS_FILE = ROOT / "lifeagent_ids.json"
MENTIONS = ROOT / "data" / "processed" / "ugc_mentions.jsonl"
SHOPS = ROOT / "data" / "processed" / "shops.jsonl"
LOADED = ROOT / "data" / "processed" / "kb_loaded.json"  # 已写进 KB 的提及 key，避免重复
SRC_NAME = {"xhs": "小红书", "dp": "点评", "dy": "抖音"}
SEP = "\n\n#####\n\n"


def segment(rec: dict, shop: dict) -> str:
    m = rec["mention"]
    date = datetime.fromtimestamp(rec["ts"]).strftime("%Y-%m-%d") if rec.get("ts") else "日期未知"
    head = " | ".join([shop["amap_id"], shop["name"], shop.get("biz_area") or "", shop.get("cate") or "",
                       "来源 " + SRC_NAME.get(rec["source"], rec["source"]), date])
    facts = []
    if m.get("avg_price"):
        facts.append(f"人均约 {m['avg_price']} 元")
    for k, label in [("dishes", "推荐菜"), ("scenes", "适合")]:
        if m.get(k):
            facts.append(label + "：" + "、".join(map(str, m[k])))
    for k, label in [("queue", "排队"), ("deal", "团购"), ("cons", "缺点")]:
        if m.get(k):
            facts.append(f"{label}：{m[k]}")
    return head + "\n" + (m.get("quote") or "") + ("\n" + "；".join(facts) if facts else "")


def list_docs(c: Coze, kb: str) -> list[dict]:
    out, page = [], 0
    while True:
        infos = c.post("/api/knowledge/document/list", {"dataset_id": kb, "page": page, "size": 100}).get("document_infos") or []
        out += infos
        if len(infos) < 100:
            return out
        page += 1


def main():
    rebuild = "--rebuild" in sys.argv
    ids = json.loads(IDS_FILE.read_text(encoding="utf-8"))
    kb = ids["kbs"]["kb_review"]
    shops = {json.loads(l)["amap_id"]: json.loads(l) for l in SHOPS.read_text(encoding="utf-8").splitlines() if l.strip()}
    recs = [json.loads(l) for l in MENTIONS.read_text(encoding="utf-8").splitlines() if l.strip()]
    loaded = set() if rebuild or not LOADED.exists() else set(json.loads(LOADED.read_text(encoding="utf-8")))
    new = [r for r in recs if kb_eligible(r, shops) and r["key"] not in loaded]
    skipped = sum(1 for r in recs if r.get("amap_id") in shops and not kb_eligible(r, shops))
    segs = [segment(r, shops[r["amap_id"]]) for r in new]
    c = Coze()
    c.login()
    if rebuild:
        old = list_docs(c, kb)
        if old:
            c.post("/api/knowledge/document/delete", {"document_ids": [str(d["document_id"]) for d in old]})
        print(f"删掉 KB-review 旧文档 {len(old)} 个（{sum(int(d.get('slice_count') or 0) for d in old)} 段）")
        LOADED.write_text("[]", encoding="utf-8")
    if not segs:
        print("没有要写的提及，跳过")
        return
    name = f"ugc-{datetime.now():%Y%m%d-%H%M%S}" + ("-rebuild" if rebuild else "")
    body = c.post("/api/knowledge/document/create", {
        "dataset_id": kb, "format_type": 0,
        "document_bases": [{"name": name, "source_info": {"document_source": 2, "custom_content": SEP.join(segs)}}],
        "chunk_strategy": {"chunk_type": 1, "separator": "#####", "max_tokens": 800, "overlap": 0,
                           "remove_extra_spaces": False, "remove_urls_emails": False}})
    infos = body.get("document_infos") or body["data"]["document_infos"]
    doc_id = str(infos[0]["document_id"])
    for _ in range(100):
        p = c.post("/api/knowledge/document/progress/get", {"document_ids": [doc_id]})
        st = (p.get("data") or [{}])[0]
        if st.get("status") == 1:
            break
        time.sleep(3)
    n_slices = next((d.get("slice_count") for d in list_docs(c, kb) if str(d["document_id"]) == doc_id), "?")
    print(f"KB-review 新文档 {name}：{len(segs)} 段（切出 {n_slices} 块），索引状态 {st.get('status')}，进度 {st.get('progress')}；"
          f"对上门店但没内容、没写的 {skipped} 条")
    if st.get("status") == 1:
        LOADED.write_text(json.dumps(sorted(loaded | {r["key"] for r in new})), encoding="utf-8")
    else:
        sys.exit("索引没完成，kb_loaded.json 没更新")


if __name__ == "__main__":
    main()
