"""小红书笔记可信度（是不是推广/软广）和每家店的笔记口碑分（10-23，网页 Demo 的评分明细和评价来源用）。

只用 data/ 里已有的笔记和提及，不抓新数据。
1. 笔记可信度：DeepSeek 零样本判断，提示词里的线索参照 CHASM（arXiv 2604.20511，小红书隐蔽广告数据集：
   购买渠道藏在图片或评论里、引导私信/进群、标题党和夸张推销口吻、只夸一个品牌、评论区有人说是广告），
   加上餐饮探店特有的（达人探店模板、"受邀/感谢款待"、只夸不说缺点）。CHASM 上零样本 F1 约 0.6，结果只作参考。
   规则另抓明面线索（评论说"广告/恰饭"、正文"邀请/合作/私信"等），和模型判断一起给出最终分。
2. 提及口碑：每条对上门店的提及按 口味 / 环境 / 服务 / 性价比 / 排队 打 1–5 分（没提到的留空）。
3. 按店汇总：疑似推广的笔记降权（权重 1 - 0.7 × 推广分），得到每个维度的均分、笔记口碑分（0–10）和可信度统计。

输出：data/processed/note_cred.json（按笔记）、data/processed/shop_quality.json（按店）。结果按内容哈希缓存，重跑只补新的。
  python -I -X utf8 etl/note_quality.py
"""
import ast
import glob
import hashlib
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from common.llm import chat  # noqa: E402

PROC = ROOT / "data" / "processed"
CACHE = PROC / "note_quality_cache.json"
ASPECTS = ["口味", "环境", "服务", "性价比", "排队"]

AD_RULES = [
    ("评论区有人说是广告", "comments", r"广告|恰饭|软广|推广吧|广吗|收钱|打广告|水军"),
    ("写明合作或受邀", "text", r"受邀|应邀|品牌方|赞助|商务合作|合作探店|探店邀约|感谢.{0,8}(款待|邀请)"),
    ("引导私信、进群或领券", "text", r"私信|dd我|滴滴我|进群|加群|领券|戳链接|团购链接|主页链接"),
    ("探店模板信息块", "text", r"(地址|📍).{0,40}(营业时间|⏰).{0,80}(人均|💰)"),
]

CRED_PROMPT = """你是小红书笔记的推广识别模块。判断每篇餐饮笔记是不是商家推广（包括明确合作、没标注的软广）。

参考线索（来自小红书隐蔽广告研究 CHASM，和餐饮探店的常见做法）：
- 偏推广：购买渠道或团购藏在正文/评论里；引导私信、进群、领券；标题党、夸张推销口吻（"闭眼冲""yyds""全网最"）；
  只夸一家店、通篇没有缺点；达人探店模板（店名、地址、营业时间、人均、推荐菜整齐罗列）；写"受邀/感谢款待/合作"；
  评论区有人说"广告""恰饭"；互动数和内容不相称（很长的种草文几乎没人评论，或评论都是"求地址"）。
- 偏真实体验：写了具体缺点（贵、排队久、咸、服务慢）；自费、和别家对比；有个人经历细节；评论区有人反驳或讨论。

对每篇给出：
- ad：0 到 1 的推广可能性
- label：真实体验 / 疑似推广 / 明确合作（文中写了合作、受邀等才用"明确合作"）
- cues：最多 3 条依据，每条 12 字以内，用原文里的事实
只输出 JSON 数组，按输入顺序：[{"key": "...", "ad": 0.2, "label": "真实体验", "cues": ["写了排队久、偏咸"]}]

笔记：
"""

ASPECT_PROMPT = """你是餐饮评价打分模块。下面每条是小红书笔记里对一家店的评价摘录。按维度打 1–5 分：
口味、环境、服务、性价比、排队（排队维度：不用排/很快 5 分，排很久 1 分）。评价里没提到的维度写 null，不要猜。
3 分是"一般/还行"，4 分是明确说好，5 分是强烈推荐，2 分是明确说不好，1 分是很差。
只输出 JSON 数组，按输入顺序：[{"id": 0, "口味": 4, "环境": null, "服务": null, "性价比": 4, "排队": 2}]

评价：
"""


def h(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:12]


def parse_list(txt: str) -> list:
    m = re.search(r"\[.*\]", txt or "", re.S)
    try:
        return json.loads(m.group(0)) if m else []
    except ValueError:
        return []


def load_notes() -> dict:
    notes = {}
    for f in glob.glob(str(ROOT / "data" / "raw" / "ugc" / "xhs_browser" / "*.jsonl")):
        for line in open(f, encoding="utf-8"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("key") and r.get("note_id"):
                notes[r["key"]] = r
    return notes


def lit(x, d):
    try:
        return ast.literal_eval(x) if isinstance(x, str) else (x if x is not None else d)
    except (ValueError, SyntaxError):
        return d


def note_brief(r: dict) -> str:
    comments = [str(c.get("t") or "")[:60] for c in lit(r.get("comments"), [])][:10]
    return json.dumps({"key": r["key"], "title": r.get("title") or "", "text": (r.get("text") or "")[:1200],
                       "tags": lit(r.get("tags"), [])[:10], "likes": r.get("likes"), "collects": r.get("collects"),
                       "comments_n": r.get("ncomments"), "comments": comments}, ensure_ascii=False)


def rule_cues(r: dict) -> list[str]:
    text = (r.get("title") or "") + (r.get("text") or "")
    comments = " ".join(str(c.get("t") or "") for c in lit(r.get("comments"), []))
    return [name for name, where, pat in AD_RULES if re.search(pat, comments if where == "comments" else text, re.S)]


def run_batches(items: list, size: int, fn) -> list:
    batches = [items[i:i + size] for i in range(0, len(items), size)]
    with ThreadPoolExecutor(4) as ex:
        return [x for out in ex.map(fn, batches) for x in out]


def main():
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {"cred": {}, "aspect": {}}
    notes = load_notes()
    mentions = [json.loads(l) for l in open(PROC / "ugc_mentions.jsonl", encoding="utf-8")]
    mentions = [m for m in mentions if m.get("resolve") == "local" and m.get("source") == "xhs" and m.get("amap_id")]
    used = sorted({m["doc_id"] for m in mentions if m["doc_id"] in notes})
    print(f"对上门店的小红书提及 {len(mentions)} 条，来自 {len(used)} 篇笔记")

    # 1. 笔记可信度
    todo = [k for k in used if h(note_brief(notes[k])) not in cache["cred"]]
    print("笔记可信度要判", len(todo), "篇", flush=True)

    def cred_batch(keys):
        briefs = [note_brief(notes[k]) for k in keys]
        for _ in range(2):
            out = {str(x.get("key")): x for x in parse_list(chat(CRED_PROMPT + "\n".join(briefs), max_tokens=16000))
                   if isinstance(x, dict)}
            if all(k in out for k in keys):
                break
        return [(k, out.get(k)) for k in keys]

    for k, res in run_batches(todo, 5, cred_batch):
        if res:
            cache["cred"][h(note_brief(notes[k]))] = res
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    cred = {}
    for k in used:
        r = notes[k]
        llm = cache["cred"].get(h(note_brief(r))) or {}
        rules = rule_cues(r)
        ad = min(1.0, float(llm.get("ad") or 0) + 0.15 * len(rules)) if llm else min(1.0, 0.25 * len(rules))
        # 四档（10-23 抽查后定）：零样本判断精度不高（CHASM 上 GPT-4o 精度 0.46），"通篇只夸、评论问店名"的热门真实笔记也会被打高分，
        # 所以中间一档只说"存疑"，不下结论；写明合作要规则和模型都认才算
        if llm.get("label") == "明确合作" or ("写明合作或受邀" in rules and ad >= 0.5):
            label = "明确合作"
        elif ad >= 0.8:
            label = "疑似推广"
        elif ad >= 0.5:
            label = "存疑"
        else:
            label = "真实体验"
        cred[k] = {"note_id": r["note_id"], "title": r.get("title") or "", "ts": r.get("ts"), "likes": r.get("likes"),
                   "collects": r.get("collects"), "ncomments": r.get("ncomments"), "ad": round(ad, 2), "label": label,
                   "cues": (llm.get("cues") or [])[:3], "rule_cues": rules, "judged": bool(llm)}
    (PROC / "note_cred.json").write_text(json.dumps(cred, ensure_ascii=False, indent=1), encoding="utf-8")
    labels = {}
    for c in cred.values():
        labels[c["label"]] = labels.get(c["label"], 0) + 1
    print("笔记可信度：", labels)

    # 2. 提及口碑
    def mtext(m):
        d = lit(m.get("mention"), {})
        parts = [f"店：{d.get('shop')}", f"原话：{d.get('quote') or ''}", f"缺点：{d.get('cons') or ''}",
                 f"排队：{d.get('queue') or ''}", f"推荐菜：{'、'.join(d.get('dishes') or [])}", f"人均：{d.get('avg_price') or ''}"]
        return "；".join(p for p in parts if not p.endswith("：") and not p.endswith("None"))

    mt = {m["key"]: mtext(m) for m in mentions}
    todo = [k for k, t in mt.items() if h(t) not in cache["aspect"]]
    print("提及口碑要打", len(todo), "条", flush=True)

    def asp_batch(keys):
        lines = [json.dumps({"id": i, "评价": mt[k]}, ensure_ascii=False) for i, k in enumerate(keys)]
        out = {}
        for _ in range(2):
            out = {x.get("id"): x for x in parse_list(chat(ASPECT_PROMPT + "\n".join(lines), max_tokens=16000)) if isinstance(x, dict)}
            if len(out) >= len(keys):
                break
        return [(k, out.get(i)) for i, k in enumerate(keys)]

    for k, res in run_batches(todo, 10, asp_batch):
        if res:
            cache["aspect"][h(mt[k])] = {a: res.get(a) for a in ASPECTS}
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    # 3. 按店汇总
    shops = {}
    for m in mentions:
        s = shops.setdefault(m["amap_id"], {"acc": {a: [0.0, 0.0, 0] for a in ASPECTS}, "notes": []})
        c = cred.get(m["doc_id"])
        w = 1 - 0.7 * (c["ad"] if c else 0.3)
        for a, v in (cache["aspect"].get(h(mt[m["key"]])) or {}).items():
            if isinstance(v, (int, float)) and 1 <= v <= 5:
                s["acc"][a][0] += w * v
                s["acc"][a][1] += w
                s["acc"][a][2] += 1
        if c and m["doc_id"] not in s["notes"]:
            s["notes"].append(m["doc_id"])
    out = {}
    for aid, s in shops.items():
        asp = {a: {"score": round(v[0] / v[1], 1), "n": v[2]} for a, v in s["acc"].items() if v[1] > 0}
        vals = [x["score"] for a, x in asp.items() if a != "排队"]
        cs = [cred[k] for k in s["notes"]]
        n_ad = sum(1 for c in cs if c["label"] in ("疑似推广", "明确合作"))
        out[aid] = {"aspects": asp, "ugc_score": round((sum(vals) / len(vals) - 1) * 2.5, 1) if vals else None,
                    "notes": s["notes"], "n_notes": len(cs), "n_ad": n_ad,
                    "cred": round(1 - sum(c["ad"] for c in cs) / len(cs), 2) if cs else None}
    (PROC / "shop_quality.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("按店汇总", len(out), "家；有笔记口碑分的", sum(1 for x in out.values() if x["ugc_score"] is not None), "家")


if __name__ == "__main__":
    main()
