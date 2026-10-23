"""把对齐到门店的 UGC 提及汇总进 shop 行：tags（标准场景标签，| 分隔）和 note_cnt（提到这家店的笔记数）。

SQL 召回按 tags 是否命中用户场景排序，所以场景说法要先归一。对话流"合并槽位"节点里有同一份同义词表，改这里要同步改那里。
用法：python -I -X utf8 etl/shop_tags.py   （之后跑 bridge/load_shops.py 写库）
"""
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHOPS = ROOT / "data" / "processed" / "shops.jsonl"
MENTIONS = ROOT / "data" / "processed" / "ugc_mentions.jsonl"

SCENE_SYN = {
    "约会": ["约会", "情侣", "二人食", "浪漫", "氛围"],
    "带老人": ["带老人", "家庭", "长辈", "爸妈", "老人"],
    "带娃": ["带娃", "亲子", "儿童", "小朋友", "宝宝"],
    "聚餐": ["聚餐", "聚会", "团建"],
    "一人食": ["一人食", "一个人", "工作餐"],
    "生日": ["生日", "庆祝"],
    "商务": ["商务", "宴请", "包间"],
    "拍照": ["拍照", "出片", "漂亮饭"],
    "下午茶": ["下午茶"],
}


def canon(scene: str) -> set:
    return {k for k, ws in SCENE_SYN.items() if any(w in (scene or "") for w in ws)}


def main():
    tags, docs = defaultdict(set), defaultdict(set)
    for l in MENTIONS.read_text(encoding="utf-8").splitlines():
        r = json.loads(l)
        aid = r.get("amap_id")
        if not aid:
            continue
        docs[aid].add(r["doc_id"].split("#")[0])
        for s in r["mention"].get("scenes") or []:
            tags[aid] |= canon(str(s))
    shops = [json.loads(l) for l in SHOPS.read_text(encoding="utf-8").splitlines()]
    for s in shops:
        s["tags"] = "|".join(sorted(tags.get(s["amap_id"], set())))
        s["note_cnt"] = len(docs.get(s["amap_id"], set()))
    SHOPS.write_text("".join(json.dumps(s, ensure_ascii=False) + "\n" for s in shops), encoding="utf-8")
    tagged = [s for s in shops if s["tags"]]
    print(f"门店 {len(shops)} 家，有笔记 {sum(1 for s in shops if s['note_cnt'])} 家，有场景标签 {len(tagged)} 家")
    for s in sorted(tagged, key=lambda x: -x["note_cnt"])[:15]:
        print(f"  {s['name'][:20]:<20} 笔记 {s['note_cnt']} | {s['tags']}")


if __name__ == "__main__":
    main()
