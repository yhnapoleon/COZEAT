"""UGC 提及对齐门店，对不上的查高德补门店。每次从头算，不沿用上一轮的 amap_id（10-21 改，见 HANDOVER 第 11 节）。

步骤：
  1. 清掉上一轮的判定（amap_id、amap_id_far、dist_m、resolve、align_*）。
  2. 写了南山以外的区或外市的（etl.ugc.loc_check 的规则），记 resolve="foreign"，不查高德。
  3. 本地门店库对不上的，按"店名 + 分店"查高德（/v5/place/text，限深圳、餐饮），候选用和本地同一套 name_match + loc_check；
     笔记没写分店、查到的又远，再分别带上锚点名查。离最近锚点 ≤ --radius 米的补进 shops.jsonl。
  4. 用补完的门店库再对齐一遍，所以连跑两次结果相同。还对不上的：查到但远记 far（amap_id_far + dist_m），
     本地同名店被位置否决又没查到别的记 loc_reject，什么都没查到记 none，没写店名记 noname。
  5. key 按 etl.ugc.mention_key 重算，同 key 的重复提及只留第一条。
高德结果缓存在 data/processed/amap_cache.json，重跑不花额度。会重写 ugc_mentions.jsonl、shops.jsonl（先备份成 .bak）。
之后跑 etl/audit_mentions.py（管线关卡），再 shop_tags → deals → load_shops → load_kb --rebuild。

用法：python -I -X utf8 etl/resolve_mentions.py [--anchor 海岸城 ...] [--radius 3000] [--offline] [--dry-run]
  --offline  不调高德，只用缓存，报告还缺几次调用（先估额度）
"""
import argparse
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from etl.amap_crawl import to_row  # noqa: E402
from etl.ugc import (ANCHORS, KWD_ANCHOR, OUT, SHOPS, Amap, _split_loc, align, clean_loc, dist_m, is_foreign,  # noqa: E402
                     loc_check, mention_key, name_match, norm)

CLOSED = ("暂停营业", "停业", "装修中", "已关闭")


def nearest(anchors, lng, lat) -> float:
    return min(dist_m(a[0], a[1], lng, lat) for _, a in anchors)


def foreign(m: dict) -> bool:
    branches, areas = _split_loc(m.get("branch")), _split_loc(m.get("area"))
    if any("南山" in x for x in branches + areas):
        return False
    return (bool(branches) and all(is_foreign(b) for b in branches)) or (not branches and any(is_foreign(a) for a in areas))


def junk_name(name: str) -> bool:
    """抽取出来的"店名"不是店名的，不查高德。"""
    return len(norm(name)) < 2 or any(w in name for w in ("未知", "…", "...", "某家", "这家"))


def lookup(m: dict, anchors, amap: Amap, radius: int, kwd: str = ""):
    """→ (门店行, 离最近锚点米数) 或 None。"""
    shop = str(m.get("shop") or "").strip()
    branches = [clean_loc(b) for b in _split_loc(m.get("branch"))]
    branches = [b for b in branches if len(b) >= 2]

    def best_of(kw):
        best, best_key = None, None
        for p in amap.text(kw):
            if any(w in (p.get("name") or "") for w in CLOSED):
                continue
            sc, rule = name_match(shop, p.get("name") or "")
            if sc < 95:
                continue  # 从全城结果里补新店只认确切的名字证据；"中文近似"（90）只用于本地门店库（"四方面馆"会配上"四方面庄"）
            row = to_row(p)
            ok, _, strength = loc_check(m, row, amap)
            if not ok:
                continue
            dm = nearest(anchors, row["lng"], row["lat"])
            key = (sc, rule == "同名", strength, -dm)
            if best_key is None or key > best_key:
                best, best_key = (row, dm, rule == "同名"), key
        return best

    hit = best_of(f"{shop} {branches[0]}" if branches else shop)
    if (not hit or hit[1] > radius) and not branches:
        # 没写分店、查到的又远：带上锚点名再查，看锚点附近有没有分店。笔记搜索词里有锚点就只查那一个（省额度）。
        # 第一次已经查到完全同名的店（只是远），再查只认同样完全同名的："外婆家"在远处有同名店，
        # 带上锚点名会搜出附近的"外婆家手工奶皮子"
        names = [nm for nm, _ in anchors]
        picked = [v for k, v in KWD_ANCHOR.items() if k in (kwd or "") and v in names]
        for nm in dict.fromkeys(picked) or names:
            near = best_of(f"{shop} {nm}")
            if near and near[1] <= radius and (near[2] or not (hit and hit[2])):
                return near[:2]
    return hit[:2] if hit else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--anchor", action="append", help="默认 " + "、".join(ANCHORS))
    ap.add_argument("--radius", type=int, default=3000)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", help="提及结果写到这个路径（不动 ugc_mentions.jsonl、shops.jsonl），审阅用")
    a = ap.parse_args()
    amap = Amap(offline=a.offline)

    shops = [json.loads(l) for l in SHOPS.read_text(encoding="utf-8").splitlines() if l.strip()]
    recs = [json.loads(l) for l in OUT.read_text(encoding="utf-8").splitlines() if l.strip()]
    anchors = []
    for nm in a.anchor or ANCHORS:
        g = amap.geo(nm)
        if not g:
            sys.exit(f"锚点 {nm} 编码失败（offline 模式下要先联网跑一次）")
        anchors.append((nm, (g["lng"], g["lat"])))

    # 1) 清掉上一轮的判定
    for r in recs:
        for k in ("amap_id", "amap_id_far", "dist_m", "resolve", "align_score", "align_why"):
            r.pop(k, None)

    # 2-3) 本地对不上的查高德，近的补门店
    known = {s["amap_id"] for s in shops}
    added, looked = [], {}
    for r in recs:
        m = r["mention"]
        if not norm(m.get("shop")) or norm(m.get("shop")) in ("none", "null") or foreign(m):
            continue
        if align(m, shops, amap, r.get("kwd") or "")[0] or junk_name(str(m.get("shop"))):
            continue
        k = (norm(m.get("shop")), norm(m.get("branch") or ""), norm(m.get("area") or ""))
        if k not in looked:
            looked[k] = lookup(m, anchors, amap, a.radius, r.get("kwd") or "")
            hit = looked[k]
            print(f"[高德] {m.get('shop')} / {m.get('branch')} → " + (f"{hit[0]['name']} {hit[1]:.0f}m" if hit else "无"),
                  flush=True)
        hit = looked[k]
        if hit and hit[1] <= a.radius and hit[0]["amap_id"] not in known:
            shops.append(hit[0])
            known.add(hit[0]["amap_id"])
            added.append(hit[0]["name"])

    # 4) 用补完的门店库从头对齐
    for r in recs:
        m = r["mention"]
        if not norm(m.get("shop")) or norm(m.get("shop")) in ("none", "null"):
            r.update(amap_id=None, resolve="noname")
            continue
        if foreign(m):
            r.update(amap_id=None, resolve="foreign", align_why=f"位置写的是外区或外市：{m.get('branch')} / {m.get('area')}")
            continue
        shop, score, why = align(m, shops, amap, r.get("kwd") or "")
        if shop:
            r.update(amap_id=shop["amap_id"], resolve="local", align_score=score, align_why=why)
            continue
        hit = looked.get((norm(m.get("shop")), norm(m.get("branch") or ""), norm(m.get("area") or "")))
        r["amap_id"] = None
        if hit:  # 近的已经补进门店库并在上面对上了，剩下的都是远的
            r.update(amap_id_far=hit[0]["amap_id"], dist_m=round(hit[1]), resolve="far",
                     align_why=f"高德 {hit[0]['name']} 离最近锚点 {hit[1]:.0f}m")
        elif why:
            r.update(resolve="loc_reject", align_why=why)
        else:
            r["resolve"] = "none"

    # 5) key 重算、去重
    seen, out = set(), []
    for r in recs:
        r["key"] = mention_key(r)
        if r["key"] in seen:
            print(f"[去重] {r['doc_id']} {r['mention'].get('shop')} / {r['mention'].get('branch')}")
            continue
        seen.add(r["key"])
        out.append(r)
    assert not any(r.get("amap_id") and r.get("amap_id_far") for r in out)

    amap.save()
    st = Counter(r["resolve"] for r in out)
    print(f"\n高德实际调用 {amap.calls} 次" + (f"，离线模式缺缓存 {len(amap.missed)} 次" if a.offline else "")
          + f"；新增门店 {len(added)} 家：{added}")
    print(f"提及 {len(out)} 条（去重 {len(recs) - len(out)}）：" + "，".join(f"{k} {v}" for k, v in st.most_common()))
    if a.out:
        Path(a.out).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out), encoding="utf-8")
        print("已写", a.out)
        return
    if a.dry_run or a.offline:
        return
    shutil.copy(OUT, OUT.with_suffix(".jsonl.bak"))
    shutil.copy(SHOPS, SHOPS.with_suffix(".jsonl.bak"))
    OUT.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out), encoding="utf-8")
    SHOPS.write_text("".join(json.dumps(s, ensure_ascii=False) + "\n" for s in shops), encoding="utf-8")
    print(f"已写 {OUT.name}、{SHOPS.name}（门店共 {len(shops)} 家），备份为 .bak")


if __name__ == "__main__":
    main()
