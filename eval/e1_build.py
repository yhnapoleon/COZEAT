"""建 E1（意图、槽位、该不该澄清）评测集：手写难例 + DeepSeek 按指定标签生成的覆盖面用例。

标签是先定好再让模型写句子（规格 → 句子），不是让模型给句子打标签；生成后人工过一遍。
  eval/data/e1_seed.jsonl     手写：上下文追问、含糊、意图边界、多意图、反馈
  eval/data/e1_gen_raw.json   生成缓存（规格 + 模型原始输出），有就不重新调模型；--regen 重来
  eval/data/e1.jsonl          合并后的评测集，带 split（dev / test，按 id 哈希 2:1）

用法：python -I -X utf8 eval/e1_build.py [--regen]

用例字段：
  query, ctx{stage, pending_q, prev_slots, shown[店名]}, intent（金标主意图）, intent_ok（也算对的意图）,
  ask（"" / intent / slot：系统该不该先问、问哪种）, ask_ok, route（可选，金标分流）,
  slots（只在找店、套餐类用例上评；没写的键表示应为空；"*" 表示不评；字符串里用 | 分隔可接受的写法；
         taste 是短语列表；budget_max 可以是整数列表）, fb{label, target}（可选，对上轮推荐的反馈）
"""
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from common.llm import chat, DEFAULT_MODEL  # noqa: E402

DATA = ROOT / "eval" / "data"
SEED, RAW, OUT = DATA / "e1_seed.jsonl", DATA / "e1_gen_raw.json", DATA / "e1.jsonl"

AREAS = [("海岸城", "海岸城"), ("深圳湾万象城", "万象城|深圳湾万象城"), ("南山科技园", "科技园|南山科技园"), ("后海", "后海")]
CATS = [("火锅", "火锅"), ("日料", "日料|日本料理"), ("粤菜", "粤菜|广东菜"), ("川菜", "川菜|四川菜"), ("湘菜", "湘菜|湖南菜"),
        ("西餐", "西餐"), ("烧烤", "烧烤"), ("椰子鸡", "椰子鸡"), ("韩餐", "韩餐|韩国料理|韩料"), ("茶餐厅", "茶餐厅"),
        ("牛肉火锅", "牛肉火锅|潮汕牛肉火锅|火锅"), ("自助餐", "自助餐|自助"), ("越南菜", "越南菜|越南"), ("面馆", "面馆|面|拉面"),
        ("甜品", "甜品|甜点"), ("咖啡", "咖啡|咖啡厅|咖啡馆")]
BUDGETS = [50, 60, 80, 100, 120, 150, 200, 300]
PEOPLE = [2, 3, 4, 6]
SCENES = ["约会", "聚餐", "带娃", "带老人", "商务", "生日"]
TASTES = [("清淡", "清淡|口味淡|淡一点"), ("想吃辣", "辣|想吃辣|辣的|吃辣|重辣"), ("不辣", "不辣|不要辣|不吃辣|不能吃辣|别太辣|不要太辣"),
          ("不吃香菜", "不吃香菜|不要香菜|香菜"), ("少油", "少油|不油腻|不要太油|清淡")]
STYLES = ["口语短句", "带语气词（呀、呗、嘛、哈）", "完整礼貌的一句话", "打字随意，可以省略主语、少标点", "先说一点无关背景（如刚下班、好饿）再提要求"]
SHOPS = ["陶陶居", "小炳胜", "炳胜品味", "寿司郎", "摩打食堂", "八合里", "潮汕大目", "芈重山老火锅", "SOKO SOKO", "木槿钰",
         "湘小小", "渝月", "香港新文华", "怂火锅", "至正潮菜", "花园椰子鸡", "一个锅韩国料理"]
ASPECTS = ["口碑", "性价比", "环境", "好不好吃", "哪个更值得去", "适不适合约会", "要不要排队", "招牌菜是什么"]
FAQS = ["团购券过期了能不能退", "退款多久到账", "团购券要不要提前预约", "能不能开发票", "两张券能不能叠加用", "周末节假日能不能用券",
        "团购券怎么核销", "买错了套餐能不能换", "一张双人套餐券几个人能用", "包间要不要另外收费", "随时退是什么意思", "券能不能转给朋友用"]
PMS = [("记住", "我不吃香菜"), ("记住", "我人均一般不超过一百"), ("记住", "我常在科技园上班"), ("记住", "我吃素"),
       ("忘掉", "我说过的不吃辣"), ("忘掉", "常去海岸城那条"), ("查看", "记了我哪些偏好"), ("修改", "我现在能吃辣了"),
       ("记住", "我对花生过敏"), ("查看", "我的预算你记的是多少")]
CHATS = ["问天气", "让讲个笑话", "道谢", "打招呼", "吐槽工作累", "问电影推荐", "让帮忙写周报", "问助手叫什么"]
DEAL_KW = ["团购", "套餐", "优惠券", "代金券", "双人餐", "划算的套餐"]
HOT_KW = ["热门", "排行榜", "必吃榜", "网红店", "最近很火的", "大家都在去的"]
NAV_KW = ["怎么去", "多远", "要多久", "坐地铁怎么走", "开车方便吗", "走路能到吗"]


def rec_specs(rng: random.Random) -> list[dict]:
    """找店 35 条：几种条件组合；最后 4 条没地点没品类，系统应追问。"""
    out = []

    def add(n, keys):
        for _ in range(n):
            sl, cond = {}, []
            if "area" in keys:
                a = rng.choice(AREAS)
                sl["area"], cond = a[1], cond + ["地点=" + a[0]]
            if "cat" in keys:
                c = rng.choice(CATS)
                sl["category"], cond = c[1], cond + ["品类=" + c[0]]
            if "budget" in keys:
                b = rng.choice(BUDGETS)
                sl["budget_max"], cond = b, cond + [f"人均上限={b} 元"]
            if "people" in keys:
                p = rng.choice(PEOPLE)
                sl["people"], cond = p, cond + [f"人数={p}"]
            if "scene" in keys:
                s = rng.choice(SCENES)
                sl["scene"], cond = s, cond + ["场景=" + s]
                if s == "约会" and "people" not in keys:
                    sl["people"] = "*"  # 约会可能被填成 2 人，不评
                if s in ("聚餐", "带老人", "带娃", "生日", "商务") and "people" not in keys:
                    sl["people"] = "*"
            if "taste" in keys:
                t = rng.choice(TASTES)
                sl["taste"], cond = [t[1]], cond + ["口味=" + t[0]]
            need = not ("area" in keys or "cat" in keys)
            out.append({"intent": "rec", "slots": sl, "ask": "slot" if need else "", "ask_ok": ["slot", "intent"] if need else None,
                        "spec": "意图：找吃饭的地方；条件：" + "，".join(cond)})

    add(6, ["area", "cat"])
    add(5, ["area", "cat", "budget"])
    add(4, ["area", "cat", "people"])
    add(4, ["area", "scene"])
    add(4, ["area", "taste"])
    add(4, ["cat"])
    add(4, ["area", "cat", "budget", "people", "taste"])
    add(2, ["budget"])
    add(2, ["taste", "scene"])
    return out


def other_specs(rng: random.Random) -> list[dict]:
    out = []
    for i in range(12):  # 套餐：一半带地点，三分之一带品类，几条带人数
        sl, cond = {}, ["关键词=" + DEAL_KW[i % len(DEAL_KW)]]
        if i % 2 == 0:
            a = rng.choice(AREAS)
            sl["area"], cond = a[1], cond + ["地点=" + a[0]]
        if i % 3 == 0:
            c = rng.choice(CATS[:12])
            sl["category"], cond = c[1], cond + ["品类=" + c[0]]
        if i % 4 == 1:
            p = rng.choice(PEOPLE)
            sl["people"], cond = p, cond + [f"人数={p}"]
        if DEAL_KW[i % len(DEAL_KW)] == "双人餐" and "people" not in sl:
            sl["people"] = 2
        if i % 6 == 4:
            b = rng.choice(BUDGETS)
            sl["budget_max"], cond = b, cond + [f"人均上限={b} 元"]
        need = not ("area" in sl or "category" in sl)
        out.append({"intent": "deal", "slots": sl, "ask": "slot" if need else "",
                    "ask_ok": ["slot", "intent", ""] if need else None,
                    "spec": "意图：找团购套餐或优惠；" + "，".join(cond)})
    for i in range(10):
        shops = rng.sample(SHOPS, 2 if i % 2 else 1)
        out.append({"intent": "cmp", "spec": "意图：问某家店怎么样或比较几家店；涉及的店：" + "、".join(shops)
                    + "；想了解：" + ASPECTS[i % len(ASPECTS)]})
    for i in range(10):
        dest = rng.choice(SHOPS + ["海岸城", "深圳湾万象城"])
        origin = rng.choice(["", "科技园", "深大地铁站", "后海地铁站", "公司", "南山书城"])
        out.append({"intent": "nav", "spec": "意图：问路线；目的地：" + dest + ("；出发地：" + origin if origin else "")
                    + "；问法：" + NAV_KW[i % len(NAV_KW)]})
    for i in range(10):
        a = rng.choice(AREAS + [("南山", "南山"), ("", "")])
        out.append({"intent": "hot", "spec": "意图：看热门店或榜单；" + ("地点：" + a[0] + "；" if a[0] else "")
                    + "关键词：" + HOT_KW[i % len(HOT_KW)]})
    for t in FAQS:
        out.append({"intent": "faq", "spec": "意图：问团购券或平台规则；问题：" + t})
    for op, what in PMS:
        out.append({"intent": "pm", "spec": "意图：让助手" + op + "自己的偏好；内容：" + what})
    for t in CHATS:
        out.append({"intent": "chat", "spec": "意图：和吃饭找店无关的闲聊；内容：" + t})
    return out


PROMPT = """你在给一个深圳本地美食推荐助手造测试用例。下面每条是一个用例规格，请为每条写一句用户会对助手说的话。

要求：
- 规格里给的条件一个都不能漏，也不能加规格里没有的条件（地点、品类、预算、人数、场景、口味都不能多加）。
- 条件可以换说法，比如"人均上限=100 元"可以说"一个人一百以内""人均不超过一百块"；"地点=南山科技园"可以说"科技园这边"。
- 按每条规格后面的风格写；像真人在手机上打字，一般 5 到 30 个字；不要编号，不要引号，不要提"助手""用例"。
- "先说一点无关背景"的风格里，背景不能带出新条件（不要说"和同事""带孩子"这类会变成人数、场景的话）。

规格：
{specs}

只输出 JSON 数组，每项 {{"k": 规格编号, "query": "用户的话"}}，不要输出别的文字。"""


def generate(specs: list[dict], rng: random.Random) -> list[dict]:
    for s in specs:
        s["style"] = rng.choice(STYLES)
    out = []
    for i in range(0, len(specs), 12):
        part = specs[i:i + 12]
        text = "\n".join(f"{j + 1}. {s['spec']}；风格：{s['style']}" for j, s in enumerate(part))
        raw = chat(PROMPT.format(specs=text), max_tokens=4096)
        m = re.search(r"\[.*\]", raw, re.S)
        got = {int(x["k"]): str(x["query"]).strip() for x in json.loads(m.group(0))} if m else {}
        for j, s in enumerate(part):
            out.append(dict(s, query=got.get(j + 1, ""), raw_batch=i // 12))
        print(f"生成 {i + len(part)}/{len(specs)}", flush=True)
    return out


# 人工过目后的改动（10-21）：模型原始输出留在 e1_gen_raw.json，这里只记改了什么。
# "先说无关背景"的风格里 DeepSeek 一半写成"刚下班好饿"，改写一部分，留十来条当"饿了"干扰项
FIX = {
    "g031": None,  # 规格抽成了"甜品 + 想吃辣"，句子不合常理，删
    "g006": "周末在家闲着，深圳湾万象城有啥火锅推荐吗？",
    "g015": "开完会了，海岸城6个人吃茶餐厅去哪家好？",
    "g037": "唉，有没有三人套餐团购？",
    "g041": "发工资了，想找两人吃的划算套餐。",
    "g050": "朋友推荐的，至正潮菜环境怎么样",
    "g065": "刚出地铁 后海地铁站到深圳湾万象城多远",
    "g068": "第一次来深圳，南山科技园有什么热门店推荐吗？",
    "g072": "周末想出去逛逛，海岸城最近很火的店有哪些？",
    "g075": "刚搬来南山，深圳湾万象城排行榜有哪些",
    "g077": "无聊刷手机，帮我看看网红店",
    "g088": "刚看到一个券上写的，随时退是什么意思",
    "g091": "对了，记一下我人均一般不超过一百",
    "g094": "最近口味变了，忘掉我说过的不吃辣吧",
}
# 金标漏标（第一次跑分后发现）："双人餐"隐含 2 人，规格里没写人数
FIX_SLOTS = {"g040": {"people": 2}, "g046": {"people": 2}}


def fnv(s: str) -> int:
    h = 2166136261
    for b in s.encode("utf-8"):
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return h


def main():
    rng = random.Random(20261021)
    if RAW.exists() and "--regen" not in sys.argv:
        gen = json.loads(RAW.read_text(encoding="utf-8"))["cases"]
    else:
        gen = generate(rec_specs(rng) + other_specs(rng), rng)
        RAW.write_text(json.dumps({"model": DEFAULT_MODEL, "seed": 20261021, "cases": gen}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
    cases = [json.loads(x) for x in SEED.read_text(encoding="utf-8").splitlines() if x.strip()]
    for s in cases:
        s["src"] = "seed"
    for i, g in enumerate(gen):
        gid = f"g{i + 1:03d}"
        q = FIX.get(gid, g.get("query"))
        if not q:
            continue
        c = {"id": gid, "src": "gen" if gid not in FIX else "gen-edited", "tags": [g["intent"], g.get("style", "")],
             "query": q, "intent": g["intent"], "ask": g.get("ask", "")}
        for k in ("intent_ok", "ask_ok", "slots"):
            if g.get(k) is not None:
                c[k] = g[k]
        if gid in FIX_SLOTS:
            c["slots"] = dict(c.get("slots") or {}, **FIX_SLOTS[gid])
        cases.append(c)
    for c in cases:
        c["split"] = "test" if fnv(c["id"]) % 3 == 0 else "dev"
    OUT.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n", encoding="utf-8")
    n_test = sum(c["split"] == "test" for c in cases)
    print(f"写入 {OUT.relative_to(ROOT)}：{len(cases)} 条（seed {sum(c['src'] == 'seed' for c in cases)}，"
          f"gen {sum(c['src'] == 'gen' for c in cases)}；test {n_test}）")


if __name__ == "__main__":
    main()
