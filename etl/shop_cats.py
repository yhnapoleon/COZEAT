"""门店标准品类：按 common/cate_tax.py 的两级品类表给每家店打标，写回 shops.jsonl 的 std_cate（"|大类|细类|…|"）。

起因（10-21，E5 t02）：高德品类太粗，338 家里 141 家只写"中餐厅"或"餐饮相关场所"，还有写错的（创意韩餐标成日本料理）；
"西餐"按子串只能匹配到 7 家，品类写成"快餐厅"、推荐菜有意面的 pLeace 召回不到。
做法：DeepSeek 按店名、高德品类、推荐菜、对上这家店的笔记提及判定，一批 15 家；代码校验标签都在品类表里、细类和大类对得上。
结果缓存在 data/processed/shop_cats.json（按门店输入和品类表的哈希），重跑只算变了的；人工改过的写进 OVERRIDE。

用法：python -I -X utf8 etl/shop_cats.py [--dry-run] [--limit N]   （之后跑 bridge/load_shops.py 写库）
"""
import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from common.cate_tax import MAJORS, MINOR_OF, std_value, tax_text  # noqa: E402
from common.llm import chat_json  # noqa: E402

SHOPS = ROOT / "data" / "processed" / "shops.jsonl"
MENTIONS = ROOT / "data" / "processed" / "ugc_mentions.jsonl"
CACHE = ROOT / "data" / "processed" / "shop_cats.json"
NON_FOOD = "非餐饮"
OVERRIDE = {  # amap_id → [(大类, 细类)]，人工抽查改过的放这里（10-21 全量看过一遍）
    # Boooard 岸板：高德西餐厅、人均 93，唯一一条笔记只说果汁好喝，LLM 判成纯饮品店依据不够
    "B0L1B9Z9VH": [("西餐", ""), ("咖啡茶饮", "奶茶果茶")],
}

PROMPT = """给下面每家深圳的餐饮门店定标准品类。品类表（大类：细类）：
{tax}

规则：
- 按这家店主要卖什么判断。依据的可信度：店名 > 推荐菜 > 笔记里的描述 > 高德品类。高德品类经常太粗或写错（"餐饮相关场所""中餐厅"等于没写，创意韩餐被标成日本料理），只作参考。
- 每家给 1 组 [大类, 细类]，店确实同时属于两类时给 2 组，主要的放前面（例如港式茶餐厅主打烧腊：[["中餐","茶餐厅"],["中餐","粤菜"]]）。菜单里有一两道别的菜不算第二类。
- 细类拿不准就只写大类，细类写空字符串。大类和细类都只能从表里选，细类必须属于它前面的大类。
- 融合菜、创意菜按主体菜系归（创意韩餐归韩餐）；连锁快餐（麦当劳、肯德基）归快餐小吃/西式快餐；主营饮品的店不要归到正餐。
- 不是餐饮店（化妆品店、公司）写 [["{non_food}", ""]]。
- why 用一句话写判断依据（十几个字）。

门店：
{items}

只输出 JSON 对象：{{"<amap_id>": {{"cats": [["大类", "细类"]], "why": ""}}}}"""


def h(x) -> str:
    return hashlib.md5(json.dumps(x, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:10]


def shop_input(s: dict, ms: list[dict]) -> dict:
    notes = []
    for m in ms[:3]:
        mm = m.get("mention") or {}
        bits = []
        if mm.get("dishes"):
            bits.append("菜：" + "、".join(mm["dishes"][:5]))
        if mm.get("quote"):
            bits.append(str(mm["quote"])[:60])
        if bits:
            notes.append("；".join(bits))
    return {"name": s["name"], "amap_cate": s.get("cate") or "", "dishes": (s.get("dishes") or "").replace("|", "、"),
            "cost": s.get("cost"), "notes": notes}


def check(cats) -> list[tuple[str, str]]:
    out = []
    for c in cats or []:
        if not isinstance(c, (list, tuple)) or not c:
            continue
        m, s = str(c[0] or "").strip(), str(c[1] if len(c) > 1 else "").strip()
        if m == NON_FOOD:
            return [(NON_FOOD, "")]
        if s and s in MINOR_OF and MINOR_OF[s] != m:
            m = MINOR_OF[s]  # 细类对，大类写错：按细类改大类
        if m not in MAJORS:
            continue
        if s and s not in MINOR_OF:
            s = ""
        if (m, s) not in out:
            out.append((m, s))
    return out[:2]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打标、打印统计，不写 shops.jsonl")
    ap.add_argument("--limit", type=int, default=0, help="只算前 N 家（试 prompt 用）")
    ap.add_argument("--batch", type=int, default=15)
    a = ap.parse_args()
    shops = [json.loads(x) for x in SHOPS.read_text(encoding="utf-8").splitlines() if x.strip()]
    ments = defaultdict(list)
    for x in MENTIONS.read_text(encoding="utf-8").splitlines():
        if x.strip():
            m = json.loads(x)
            if m.get("amap_id"):
                ments[m["amap_id"]].append(m)
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    tax_h = h(tax_text())  # 只看大类、细类名（打标 prompt 里的那份）；改同义词、菜名不用重打
    todo, inputs = [], {}
    for s in shops[: a.limit or None]:
        inp = shop_input(s, ments.get(s["amap_id"], []))
        inputs[s["amap_id"]] = inp
        c = cache.get(s["amap_id"])
        if not c or c.get("h") != h([inp, tax_h]):
            todo.append(s["amap_id"])
    print(f"门店 {len(inputs)}，要打标 {len(todo)}")

    def run(batch):
        items = "\n".join(json.dumps({"amap_id": aid, **inputs[aid]}, ensure_ascii=False) for aid in batch)
        for attempt in range(2):
            try:
                out = chat_json(PROMPT.format(tax=tax_text(), non_food=NON_FOOD, items=items), max_tokens=16000)
                return batch, out
            except Exception as e:  # noqa: BLE001
                print("  批次失败，重试" if attempt == 0 else "  批次失败，跳过", batch[0], str(e)[:120])
        return batch, {}

    batches = [todo[i:i + a.batch] for i in range(0, len(todo), a.batch)]
    with ThreadPoolExecutor(4) as ex:
        for batch, out in ex.map(run, batches):
            for aid in batch:
                r = out.get(aid) if isinstance(out, dict) else None
                cats = check((r or {}).get("cats"))
                if not cats:
                    print("  没给出合法品类:", aid, inputs[aid]["name"], r)
                    continue
                cache[aid] = {"h": h([inputs[aid], tax_h]), "cats": cats, "why": str((r or {}).get("why") or "")}
            CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"  完成一批 {len(batch)} 家")

    major_cnt, empty = Counter(), 0
    for s in shops:
        cats = OVERRIDE.get(s["amap_id"]) or (cache.get(s["amap_id"]) or {}).get("cats") or []
        s["std_cate"] = std_value([tuple(c) for c in cats])
        if not s["std_cate"]:
            empty += 1
        for m, _ in {tuple(c) for c in cats}:
            major_cnt[m] += 1
    print("大类分布:", ", ".join(f"{k} {v}" for k, v in major_cnt.most_common()))
    print("没打上的:", empty)
    if a.dry_run or a.limit:
        print("（dry-run / limit，不写 shops.jsonl）")
        return
    SHOPS.write_text("".join(json.dumps(s, ensure_ascii=False) + "\n" for s in shops), encoding="utf-8")
    print("已写回", SHOPS.name)


if __name__ == "__main__":
    main()
