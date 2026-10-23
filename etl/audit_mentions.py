"""店铺提及对齐的审计，也是管线关卡（10-21）：resolve_mentions 之后、load_kb 前后各跑一次。只读，不调高德（用缓存）。

硬性检查，任一项不为 0 就退出码 1：
  both_ids      amap_id 和 amap_id_far 同时有值
  status        resolve 和 amap_id 对不上（local 却没有 amap_id，或别的状态却挂着 amap_id）
  key_dup       key 重复
  key_stale     key 和 etl.ugc.mention_key 重算的不一致（旧脚本写的）
  name_fail     按现行 name_match，店名没有证据指向挂的这家
  loc_fail      按现行 loc_check，笔记写的位置和挂的这家不一致
  kb_empty      要写进 KB 的提及没有任何内容（load_kb 会跳过，出现说明两边规则不一致）
  kb_stale      （--kb）kb_loaded.json 里有、现在却不该进 KB 的 key：KB 里有过期段，要 load_kb --rebuild
  kb_missing    （--kb）该进 KB 却不在 kb_loaded.json 里的 key
提示（人工看，不算失败）：fuzzy 靠近似店名对上的；geo 靠地理编码距离接受的分店；unchecked 要编码但缓存里没有的地址。

用法：python -I -X utf8 etl/audit_mentions.py [--kb] [--all]   # --all 每类全列出（默认每类最多 40 条）
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from etl.ugc import OUT, SHOPS, Amap, has_content, kb_eligible, loc_check, mention_key, name_match  # noqa: E402

LOADED = ROOT / "data" / "processed" / "kb_loaded.json"
HARD = ["both_ids", "status", "key_dup", "key_stale", "name_fail", "loc_fail", "kb_empty", "kb_stale", "kb_missing"]
SOFT = ["fuzzy", "geo", "unchecked"]


def main():
    shops = {s["amap_id"]: s for s in (json.loads(l) for l in SHOPS.read_text(encoding="utf-8").splitlines() if l.strip())}
    recs = [json.loads(l) for l in OUT.read_text(encoding="utf-8").splitlines() if l.strip()]
    amap = Amap(offline=True)
    hits = defaultdict(list)
    keys = Counter(r["key"] for r in recs)
    for r in recs:
        m = r["mention"]
        tag = f"{m.get('shop')} / {m.get('branch')} / {m.get('area')}"
        if r.get("amap_id") and r.get("amap_id_far"):
            hits["both_ids"].append(tag)
        if (r.get("resolve") == "local") != bool(r.get("amap_id")):
            hits["status"].append(f"{tag}（resolve={r.get('resolve')}，amap_id={r.get('amap_id')}）")
        if keys[r["key"]] > 1:
            hits["key_dup"].append(tag)
        if r["key"] != mention_key(r):
            hits["key_stale"].append(tag)
        s = shops.get(r.get("amap_id"))
        if not s:
            continue
        tag += f" → {s['name']}"
        sc, rule = name_match(m.get("shop"), s["name"])
        if not sc:
            hits["name_fail"].append(tag)
        elif rule in ("整体相似",) or sc < 100:
            hits["fuzzy"].append(f"{tag}（{rule}）")
        ok, why, _ = loc_check(m, s, amap)
        if not ok:
            hits["loc_fail"].append(f"{tag}（{why}）")
        elif "距门店" in why:
            hits["geo"].append(f"{tag}（{why}）")
    eligible = {r["key"] for r in recs if kb_eligible(r, shops)}
    target = eligible
    if "--kb" in sys.argv:
        loaded = set(json.loads(LOADED.read_text(encoding="utf-8"))) if LOADED.exists() else set()
        hits["kb_stale"] = sorted(loaded - eligible)
        hits["kb_missing"] = sorted(eligible - loaded)
        target = loaded
    hits["kb_empty"] = [r["mention"].get("shop") for r in recs if r["key"] in target and not has_content(r["mention"])]
    hits["unchecked"] = sorted(k[4:] for k in amap.missed if k.startswith("geo:"))

    st = Counter(r.get("resolve") for r in recs)
    print(f"提及 {len(recs)} 条，对上门店 {sum(1 for r in recs if r.get('amap_id') in shops)} 条，"
          f"该进 KB {len(eligible)} 条；resolve：" + "，".join(f"{k} {v}" for k, v in st.most_common()))
    lim = None if "--all" in sys.argv else 40
    for k in HARD + SOFT:
        if k in ("kb_stale", "kb_missing") and "--kb" not in sys.argv:
            continue
        print(f"\n== {k}{'（硬性）' if k in HARD else ''}：{len(hits[k])}")
        for t in hits[k][:lim]:
            print("  ", t)
    bad = {k: len(hits[k]) for k in HARD if hits[k]}
    print("\n关卡：" + ("通过" if not bad else "不通过 " + json.dumps(bad, ensure_ascii=False)))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
