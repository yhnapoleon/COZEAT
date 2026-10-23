"""E2 查询集：从进了 KB 的店铺提及出发，让 DeepSeek 写用户会问的话；金标就是出发的那条提及（店、笔记）。

两类，各 60 条，尽量一店一条：
  ask   问店：点名问这家店的某个方面（菜、排队、环境、价格、适合什么场合、缺点）——看能不能找回这篇笔记
  find  找店：不点名，说地点 + 需求（菜、场景、特点）——看这家店能不能进前 5
要求换说法，不能照抄提及原话（菜名、店名除外），否则全文检索白捡分。

用法：python -I -X utf8 eval/e2_queries.py [--regen]
输出：eval/data/e2_queries.jsonl（缓存，有就不重新生成）
"""
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))
from common.llm import chat  # noqa: E402
from e2_build import load_corpus  # noqa: E402

OUT = ROOT / "eval" / "data" / "e2_queries.jsonl"
N_EACH = 60

PROMPT = {
    "ask": """下面每条是小红书笔记里对一家餐厅的一段评价。请为每条写一个用户会向美食助手提的问题，这段评价能回答它。

要求：
- 用"店名"里给的名字点名这家店（可以只说品牌名）。
- 只问一个方面（某道菜好不好吃、要不要排队、环境、价格、适合什么场合、有什么缺点等），选这段评价里实际写到的。
- 换成用户自己的说法，除了店名和菜名，不要照抄评价里连续 4 个字以上的原话。
- 像手机上打字，8 到 25 个字。

{items}

只输出 JSON 数组，每项 {{"k": 编号, "query": "问题", "aspect": "问的方面（2-4 个字）"}}，不要输出别的文字。""",
    "find": """下面每条是小红书笔记里对一家餐厅的一段评价。请为每条写一句用户找店时会说的话，让这家店是很合适的答案。

要求：
- 不要出现店名或品牌名。
- 带上"地点"，再带 1 到 2 个这段评价里写到的具体需求（某道菜、某种场景、某个特点），让需求足够具体。
- 换成用户自己的说法，除了菜名，不要照抄评价里连续 4 个字以上的原话。
- 像手机上打字，8 到 30 个字。

{items}

只输出 JSON 数组，每项 {{"k": 编号, "query": "用户的话", "aspect": "用到的需求（2-6 个字）"}}，不要输出别的文字。""",
}


def facts(m: dict) -> str:
    x = m["mention"]
    parts = [x.get("quote") or ""]
    if x.get("dishes"):
        parts.append("推荐菜：" + "、".join(map(str, x["dishes"])))
    if x.get("scenes"):
        parts.append("适合：" + "、".join(map(str, x["scenes"])))
    for k, lab in [("queue", "排队"), ("cons", "缺点"), ("avg_price", "人均")]:
        if x.get(k):
            parts.append(f"{lab}：{x[k]}")
    return "；".join(p for p in parts if p)


def main():
    if OUT.exists() and "--regen" not in sys.argv:
        print("已有", OUT.relative_to(ROOT))
        return
    shops, notes, by_note = load_corpus()
    ms = [m for v in by_note.values() for m in v]
    good = [m for m in ms if len(m["mention"].get("quote") or "") >= 15
            and (m["mention"].get("dishes") or m["mention"].get("scenes") or m["mention"].get("queue") or m["mention"].get("cons"))]
    rng = random.Random(20261021)
    rng.shuffle(good)
    picked, used_shop = {"ask": [], "find": []}, {"ask": set(), "find": set()}
    for kind in ("ask", "find"):
        for m in good:
            if len(picked[kind]) >= N_EACH:
                break
            if m["amap_id"] in used_shop[kind] or any(m is p for p in picked["ask"]):
                continue
            picked[kind].append(m)
            used_shop[kind].add(m["amap_id"])
    rows = []
    for kind, lst in picked.items():
        for i in range(0, len(lst), 10):
            part = lst[i:i + 10]
            items = "\n".join(
                f"{j + 1}. 店名：{m['mention'].get('shop') or shops[m['amap_id']]['name']}；"
                f"地点：{m['mention'].get('area') or shops[m['amap_id']].get('biz_area') or '南山'}；评价：{facts(m)}"
                for j, m in enumerate(part))
            got = {}
            for _ in range(3):  # 4096 时一批 10 条常被推理 token 占满、正文截断，给足上限并重试
                raw = chat(PROMPT[kind].format(items=items), max_tokens=16000)
                mm = re.search(r"\[.*\]", raw, re.S)
                try:
                    got = {int(x["k"]): x for x in json.loads(mm.group(0))} if mm else {}
                except (ValueError, KeyError, TypeError):
                    got = {}
                if len(got) >= len(part):
                    break
                print("  重试，拿到", len(got), "条；结尾：", raw[-120:].replace("\n", " "), flush=True)
            for j, m in enumerate(part):
                g = got.get(j + 1) or {}
                rows.append({"id": f"{kind[0]}{len([r for r in rows if r['type'] == kind]) + 1:03d}", "type": kind,
                             "query": str(g.get("query") or "").strip(), "aspect": g.get("aspect") or "",
                             "gold_shop": m["amap_id"], "gold_note": m["doc_id"], "gold_mention": m["key"],
                             "shop_name": shops[m["amap_id"]]["name"], "src": facts(m)[:200]})
            print(kind, i + len(part), flush=True)
    OUT.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    print("写入", OUT.relative_to(ROOT), len(rows))


if __name__ == "__main__":
    main()
