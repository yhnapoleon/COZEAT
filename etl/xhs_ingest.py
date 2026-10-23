"""小红书笔记导入：浏览器登录态读取的 JSONL → 抽取 → 对齐 → ugc_mentions.jsonl。

笔记由用户本人已登录的 Chrome 低频只读获取（搜索页取 id 和 xsec_token，详情页读 __INITIAL_STATE__），
每行一篇：key（页面内对笔记 id 做的 FNV 短哈希）、kwd、title、text、ts（毫秒）、likes、collects、comments。
抽取和对齐复用 etl/ugc.py；评论拼进正文一起抽，"求推荐"帖的店名多在评论里。

用法：python -I -X utf8 etl/xhs_ingest.py [data/raw/ugc/xhs_browser/*.jsonl]
"""
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from etl.ugc import OUT, SHOPS, Amap, align, extract, mention_key  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GLOB = str(ROOT / "data" / "raw" / "ugc" / "xhs_browser" / "*.jsonl")


def to_doc(n: dict) -> dict:
    text = n.get("text") or ""
    cs = [c.get("t") for c in n.get("comments") or [] if c.get("t")]
    if cs:
        text += "\n\n评论：\n" + "\n".join(f"- {c}" for c in cs)
    num = lambda x: int(x) if str(x).isdigit() else None  # noqa: E731  页面上过万会显示"1.2万"，这里只收纯数字
    return {"doc_id": n["key"], "title": n.get("title") or "", "text": text, "ts": int((n.get("ts") or 0) / 1000),
            "likes": num(n.get("likes")), "collects": num(n.get("collects")), "shop_hint": None}


def main():
    files = sys.argv[1:] or sorted(glob.glob(DEFAULT_GLOB))
    shops = [json.loads(l) for l in SHOPS.read_text(encoding="utf-8").splitlines()]
    amap_c = Amap()
    done = set()
    if OUT.exists():
        for l in OUT.read_text(encoding="utf-8").splitlines():
            r = json.loads(l)
            if r.get("source") == "xhs":
                done.add(r["doc_id"])
    stats = {"docs": 0, "skipped": 0, "empty": 0, "mentions": 0, "aligned": 0}
    with OUT.open("a", encoding="utf-8") as out:
        for f in files:
            for line in Path(f).read_text(encoding="utf-8").splitlines():
                n = json.loads(line)
                if n["key"] in done:
                    stats["skipped"] += 1
                    continue
                doc = to_doc(n)
                stats["docs"] += 1
                ms = extract(doc)
                stats["empty"] += not ms
                for m in ms:
                    shop, score, why = align(m, shops, amap_c)
                    stats["mentions"] += 1
                    stats["aligned"] += bool(shop)
                    rec = {"source": "xhs", "page_id": n["key"], "doc_id": n["key"], "ts": doc["ts"],
                           "likes": doc["likes"], "collects": doc["collects"], "mention": m,
                           "amap_id": shop["amap_id"] if shop else None, "align_score": score, "align_why": why,
                           "dp_hint": None, "kwd": n.get("kwd")}
                    rec["key"] = mention_key(rec)
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    print(f"  {n['key']} | {m.get('shop')} | {m.get('branch')} → {shop['name'] if shop else '-'} ({score:.0f})")
    amap_c.save()
    print("汇总", json.dumps(stats, ensure_ascii=False))
    print("这里的对齐只是预览，接着跑 etl/resolve_mentions.py 从头重算并补门店")


if __name__ == "__main__":
    main()
