"""把 data/faq/faq.jsonl 写进 KB-faq：一条 FAQ 一段，整库重建（先删旧文档再建）。

每段：
  问：标准问法
  也可能这样问：其他问法；……
  答：答案
"也可能这样问"是给检索用的（用户的说法五花八门，多写几种问法能提高纯语义检索的命中）。
用法：python -I -X utf8 bridge/load_faq.py
"""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from coze_client import Coze  # noqa: E402

FAQ = ROOT / "data" / "faq" / "faq.jsonl"
SEP = "\n\n#####\n\n"


def segment(f: dict) -> str:
    alts = "；".join(f.get("alts") or [])
    return f"问：{f['q']}" + (f"\n也可能这样问：{alts}" if alts else "") + f"\n答：{f['a']}"


def main():
    ids = json.loads((ROOT / "lifeagent_ids.json").read_text(encoding="utf-8"))
    kb = ids["kbs"]["kb_faq"]
    rows = [json.loads(x) for x in FAQ.read_text(encoding="utf-8").splitlines() if x.strip()]
    c = Coze()
    c.login()
    old = c.post("/api/knowledge/document/list", {"dataset_id": kb, "page": 0, "size": 100}).get("document_infos") or []
    if old:
        c.post("/api/knowledge/document/delete", {"document_ids": [str(d["document_id"]) for d in old]})
        print("删掉旧文档", len(old))
    body = c.post("/api/knowledge/document/create", {
        "dataset_id": kb, "format_type": 0,
        "document_bases": [{"name": "faq-v1", "source_info": {"document_source": 2,
                                                             "custom_content": SEP.join(segment(f) for f in rows)}}],
        "chunk_strategy": {"chunk_type": 1, "separator": "#####", "max_tokens": 2000, "overlap": 0,
                           "remove_extra_spaces": False, "remove_urls_emails": False}})
    doc_id = str((body.get("document_infos") or body["data"]["document_infos"])[0]["document_id"])
    for _ in range(40):
        st = (c.post("/api/knowledge/document/progress/get", {"document_ids": [doc_id]}).get("data") or [{}])[0]
        if st.get("status") == 1:
            break
        time.sleep(3)
    info = c.post("/api/knowledge/document/list", {"dataset_id": kb, "page": 0, "size": 10}).get("document_infos") or []
    print(f"KB-faq：{len(rows)} 条 FAQ，文档状态 {st.get('status')}，切成 {info[0].get('slice_count') if info else '?'} 段")


if __name__ == "__main__":
    main()
