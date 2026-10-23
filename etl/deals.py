"""团购字段：笔记里真实提到的整理出来，没有的按设计文档 3.4 合成并标注。写回 shops.jsonl 的 deals / deal_min_price / synthetic。

1. 真实：对齐到门店的提及里有 deal 原话的（小红书、点评、抖音），LLM 整理成套餐名、团购价、原价，标来源和日期。
   只收原话里写明的，没写价格就留空，不补。
2. 合成：没有真实团购的店，每店 2 个，LLM 按品类、人均、推荐菜生成；格式和价位参照抖音笔记里的真实团购原话
   （千帆搜到的大多不在南山，只借格式）。代码再校验：团购价落在 人均 × 人数 × [0.55, 1.05]，原价高于团购价，否则按规则修正。
   合成的 deals 每项带 "synthetic": true，shop.synthetic 里加 "deals"；对话里要标"示例数据"。
LLM 结果缓存在 data/processed/deals_cache.json（按门店和输入内容），重跑只算变了的。

用法：python -I -X utf8 etl/deals.py [--dry-run]   （之后跑 bridge/load_shops.py 写库）
"""
import argparse
import hashlib
import json
import random
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from common.llm import chat_json  # noqa: E402

SHOPS = ROOT / "data" / "processed" / "shops.jsonl"
MENTIONS = ROOT / "data" / "processed" / "ugc_mentions.jsonl"
CACHE = ROOT / "data" / "processed" / "deals_cache.json"
SRC = {"xhs": "小红书", "dp": "点评", "dy": "抖音"}

REAL_PROMPT = """下面是几家深圳餐厅在小红书、点评、抖音笔记里被提到的团购或套餐原话。把每家店能确认的团购整理出来。
规则：只收原话里写明的；套餐名用原话的说法；price 是团购价、orig_price 是原价或门市价，原话没写就填 null，不要推算；
"68 抵 100 代金券"记成 name="100 元代金券"、price=68、orig_price=100；同一个套餐出现多次只留一条；原话太含糊（只说"有团购"）的不收。
只输出 JSON 对象：{{"<amap_id>": [{{"name": "", "price": null, "orig_price": null, "people": null, "src_idx": 0}}]}}，
src_idx 是该条出自下面第几条原话（从 0 开始）。没有可收的店给空数组。

{items}"""

SYN_PROMPT = """为下面每家深圳餐厅编 2 个抖音团购套餐，用于演示（会标注"示例数据"）。
要求：
- 套餐像抖音上真实的团购：常见形式是单人餐、双人餐、3–4 人餐、代金券（"100 元代金券"）。按品类和人均选合适的两种，饮品店、面包店用单人或代金券
- content 写套餐包含的菜，优先用这家店的推荐菜，没有推荐菜就写这个品类常见的菜，3–6 样，用"+"连接；代金券的 content 写"全场通用，酒水除外"
- price 是团购价（整数元），orig_price 是门市价（整数元，比 price 高 20%–60%），people 是适用人数
- 价位参考：团购价约为 人均 × 人数 × 0.6–0.95
参考真实团购的说法：
{examples}

门店：
{items}

只输出 JSON 对象：{{"<amap_id>": [{{"name": "", "content": "", "price": 0, "orig_price": 0, "people": 1}}]}}"""


def load_cache() -> dict:
    return json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}


def h(x) -> str:
    return hashlib.md5(json.dumps(x, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:10]


def fix_price(d: dict, cost) -> dict:
    """合成价格按人均校验：团购价落在 人均 × 人数 × [0.55, 1.05]，原价高于团购价。"""
    try:
        people = max(1, int(d.get("people") or 1))
        price = int(float(d.get("price") or 0))
        orig = int(float(d.get("orig_price") or 0))
    except (TypeError, ValueError):
        people, price, orig = 1, 0, 0
    if cost:
        lo, hi = int(cost * people * 0.55), int(cost * people * 1.05)
        if not lo <= price <= hi:
            price = int(cost * people * random.uniform(0.65, 0.9))
    if orig <= price:
        orig = int(price * random.uniform(1.25, 1.5))
    d.update(price=price, orig_price=orig, people=people)
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    random.seed(20261020)
    shops = [json.loads(l) for l in SHOPS.read_text(encoding="utf-8").splitlines()]
    ms = [json.loads(l) for l in MENTIONS.read_text(encoding="utf-8").splitlines()]
    cache = load_cache()

    # 1. 真实团购原话，按门店收集
    raw = {}
    for r in ms:
        deal = (r["mention"].get("deal") or "").strip()
        if not r.get("amap_id") or not deal or len(deal) < 4:
            continue
        day = datetime.fromtimestamp(r["ts"]).strftime("%Y-%m") if r.get("ts") else ""
        raw.setdefault(r["amap_id"], []).append({"src": SRC.get(r["source"], r["source"]), "date": day, "text": deal[:150]})
    by_id = {s["amap_id"]: s for s in shops}
    todo = [aid for aid in raw if aid in by_id and cache.get("real:" + aid, {}).get("h") != h(raw[aid])]
    for i in range(0, len(todo), 12):
        batch = todo[i:i + 12]
        items = "\n\n".join(f"[{aid}] {by_id[aid]['name']}（人均 {by_id[aid].get('cost')}）\n"
                            + "\n".join(f"  {j}. {x['src']} {x['date']}：{x['text']}" for j, x in enumerate(raw[aid]))
                            for aid in batch)
        try:
            out = chat_json(REAL_PROMPT.format(items=items), max_tokens=16000)
        except Exception as e:
            print(f"[真实] 批次 {i} 失败 {str(e)[:100]}")
            continue
        for aid in batch:
            deals = []
            for d in (out.get(aid) or []) if isinstance(out, dict) else []:
                if not isinstance(d, dict) or not d.get("name"):
                    continue
                k = d.get("src_idx") if isinstance(d.get("src_idx"), int) and 0 <= d["src_idx"] < len(raw[aid]) else 0
                deals.append({"name": d["name"], "price": d.get("price"), "orig_price": d.get("orig_price"),
                              "people": d.get("people"), "src": raw[aid][k]["src"] + " " + raw[aid][k]["date"],
                              "synthetic": False})
            cache["real:" + aid] = {"h": h(raw[aid]), "deals": deals}
        print(f"[真实] {min(i + 12, len(todo))}/{len(todo)}", flush=True)
        CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")

    # 2. 没有真实团购的店合成 2 个
    examples = sorted({(r["mention"].get("deal") or "")[:60] for r in ms
                       if r["source"] == "dy" and re.search(r"\d+\s*人|双人|单人|代金券", r["mention"].get("deal") or "")})[:15]
    real_ids = {aid for aid in raw if (cache.get("real:" + aid) or {}).get("deals")}
    need = []
    for s in shops:
        if s["amap_id"] in real_ids:
            continue
        inp = {"name": s["name"], "cate": s.get("cate"), "cost": s.get("cost"), "dishes": s.get("dishes")}
        c = cache.get("syn:" + s["amap_id"]) or {}
        if c.get("h") != h(inp) or not c.get("deals"):  # 上次 LLM 漏掉的（空）也重算
            need.append((s, inp))
    for i in range(0, len(need), 15):
        batch = need[i:i + 15]
        items = "\n".join(f"[{s['amap_id']}] {s['name']}｜{(s.get('cate') or '').split('/')[-1]}｜人均 {s.get('cost') or '未知'}"
                          f"｜推荐菜 {(s.get('dishes') or '无').replace('|', '、')}" for s, _ in batch)
        try:
            out = chat_json(SYN_PROMPT.format(examples="\n".join("- " + e for e in examples), items=items), max_tokens=16000)  # DeepSeek 推理也占 max_tokens
        except Exception as e:
            print(f"[合成] 批次 {i} 失败 {str(e)[:100]}")
            continue
        for s, inp in batch:
            # 标注由对话流统一加，模型自己在名字里写的"【示例数据】"去掉
            deals = [fix_price({"name": re.sub(r"[【\[（(]?示例(数据)?[】\]）)]?\s*", "", str(d.get("name"))).strip(),
                                "content": d.get("content"), "price": d.get("price"),
                                "orig_price": d.get("orig_price"), "people": d.get("people")}, s.get("cost"))
                     for d in ((out.get(s["amap_id"]) or []) if isinstance(out, dict) else []) if isinstance(d, dict) and d.get("name")][:2]
            for d in deals:
                d.update(src="合成", synthetic=True)
            cache["syn:" + s["amap_id"]] = {"h": h(inp), "deals": deals}
        print(f"[合成] {min(i + 15, len(need))}/{len(need)}", flush=True)
        CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")

    # 3. 写回门店
    n_real = n_syn = 0
    for c in cache.values():  # 缓存里早先存下的名字也清一次
        for d in c.get("deals") or []:
            d["name"] = re.sub(r"[【\[（(]?示例(数据)?[】\]）)]?\s*", "", str(d.get("name") or "")).strip()
    for s in shops:
        aid = s["amap_id"]
        # 只认现在还有提及指向这家的真实团购：缓存里的可能来自已经改判挂错店的旧提及（10-21）
        real = ((cache.get("real:" + aid) or {}).get("deals") or []) if aid in raw else []
        deals = real or (cache.get("syn:" + aid) or {}).get("deals") or []
        n_real += bool(real)
        n_syn += bool(deals and not real)
        s["deals"] = json.dumps(deals, ensure_ascii=False)
        prices = [d["price"] for d in deals if isinstance(d.get("price"), (int, float)) and d["price"] > 0]
        s["deal_min_price"] = int(round(min(prices))) if prices else None  # 表里是整数列
        syn = [x for x in (s.get("synthetic") or "").split("|") if x and x != "deals"]
        if deals and not real:
            syn.append("deals")
        s["synthetic"] = "|".join(syn)
    print(f"门店 {len(shops)} 家：真实团购 {n_real} 家，合成 {n_syn} 家，无 {len(shops) - n_real - n_syn} 家")
    for s in shops:
        if s["amap_id"] in raw and (cache.get("real:" + s["amap_id"]) or {}).get("deals"):
            print("  真实", s["name"][:16], [(d["name"], d["price"], d["src"]) for d in json.loads(s["deals"])])
    if not a.dry_run:
        SHOPS.write_text("".join(json.dumps(s, ensure_ascii=False) + "\n" for s in shops), encoding="utf-8")
        print("已写", SHOPS.name)


if __name__ == "__main__":
    main()
