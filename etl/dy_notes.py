"""抖音图文笔记和视频页文本：千帆搜索（百度，限定 www.douyin.com）发现并取正文 → 抽取 → 对齐 → ugc_mentions.jsonl。

只用千帆返回的 content，不请求抖音页面本身（笔记页是 jsvmp 挑战，见 research/13）。
content 里常有团购原话（"68 抵 100 代金券""双人套餐"），后面 etl/deals.py 从提及的 deal 字段整理团购。
来源记为 source="dy"，doc_id 用页面 id。原始结果存 data/raw/ugc/dy_notes/qianfan_<日期>.jsonl。

用法：python -I -X utf8 etl/dy_notes.py --q "海岸城 团购" --q "深圳湾万象城 美食" ...
"""
import argparse
import hashlib
import json
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from common.env import need  # noqa: E402
from etl.smoke import call  # noqa: E402
from etl.ugc import OUT, SHOPS, Amap, align, extract, mention_key  # noqa: E402

RAW = ROOT / "data" / "raw" / "ugc" / "dy_notes"


def search(q: str, top_k: int = 20) -> list[dict]:
    body = {"messages": [{"role": "user", "content": q}], "search_source": "baidu_search_v2",
            "resource_type_filter": [{"type": "web", "top_k": top_k}],
            "search_filter": {"match": {"site": ["www.douyin.com"]}}}
    st, d = call("POST", "https://qianfan.baidubce.com/v2/ai_search/web_search",
                 {"Authorization": "Bearer " + need("BAIDU_QIANFAN_API_KEY")}, body, timeout=60)
    if st != 200:
        raise RuntimeError(f"qianfan http={st} {str(d)[:200]}")
    return d.get("references") or []


def page_id(url: str) -> str:
    m = re.search(r"douyin\.com/(note|video|hashtag|shipin|zhuanti)/([\w-]+)", url or "")
    return f"{m.group(1)}_{m.group(2)}" if m else hashlib.md5((url or "").encode()).hexdigest()[:12]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", action="append", required=True)
    a = ap.parse_args()
    RAW.mkdir(parents=True, exist_ok=True)
    raw_f = RAW / f"qianfan_{date.today():%Y%m%d}.jsonl"
    shops = [json.loads(l) for l in SHOPS.read_text(encoding="utf-8").splitlines()]
    amap_c = Amap()
    done = {json.loads(l)["doc_id"] for l in OUT.read_text(encoding="utf-8").splitlines() if '"source": "dy"' in l}
    stats = {"queries": 0, "pages": 0, "new_docs": 0, "mentions": 0, "aligned": 0}
    seen = set()
    with OUT.open("a", encoding="utf-8") as out, raw_f.open("a", encoding="utf-8") as rawo:
        for q in a.q:
            try:
                refs = search(q)
            except Exception as e:
                print(f"[{q}] 搜索失败 {str(e)[:120]}")
                continue
            stats["queries"] += 1
            print(f"[{q}] {len(refs)} 条", flush=True)
            for r in refs:
                pid = page_id(r.get("url"))
                if pid in seen:
                    continue
                seen.add(pid)
                stats["pages"] += 1
                rawo.write(json.dumps({"q": q, "pid": pid, **{k: r.get(k) for k in ("url", "title", "date", "content")}},
                                      ensure_ascii=False) + "\n")
                if pid in done:
                    continue
                try:
                    ts = int(datetime.strptime(str(r.get("date"))[:10], "%Y-%m-%d").timestamp())
                except ValueError:
                    ts = 0
                doc = {"doc_id": pid, "title": r.get("title") or "", "text": r.get("content") or "", "ts": ts,
                       "likes": None, "collects": None, "shop_hint": None}
                stats["new_docs"] += 1
                for m in extract(doc):
                    shop, score, why = align(m, shops, amap_c)
                    stats["mentions"] += 1
                    stats["aligned"] += bool(shop)
                    rec = {"source": "dy", "page_id": pid, "doc_id": pid, "ts": ts, "likes": None, "collects": None,
                           "mention": m, "amap_id": shop["amap_id"] if shop else None, "align_score": score,
                           "align_why": why, "dp_hint": None, "kwd": q, "url": r.get("url")}
                    rec["key"] = mention_key(rec)
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    print(f"  {pid} | {m.get('shop')} | {m.get('branch')} | 团购:{(m.get('deal') or '')[:30]} → "
                          f"{shop['name'] if shop else '-'} ({score:.0f})", flush=True)
            time.sleep(1)
    amap_c.save()
    print("汇总", json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
