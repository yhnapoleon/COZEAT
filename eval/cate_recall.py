"""品类召回离线对照：同一批查询词，旧召回（v0.0.14：一个词在 cate/name/dishes 里 LIKE，cate_syn 一对一改写）和
新召回（10-21：标准品类 + 同义词兜底 + 原词，common/cate_tax.query_res，和 flow_code.SQL_RECALL 的 kw_hit 同一套条件）
各命中哪些店，列出新增和丢掉的店，供人工逐条判断。不算距离和预算，只看品类命中。

用法：python -I -X utf8 eval/cate_recall.py [--out eval/reports/cate_recall_1021.md]
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from common.cate_tax import TAX, query_map, query_res  # noqa: E402

SHOPS = ROOT / "data" / "processed" / "shops.jsonl"
OLD_SYN = {"日料": "日本料理", "日本菜": "日本料理", "韩餐": "韩国料理", "韩料": "韩国料理", "韩国菜": "韩国料理",
           "泰餐": "泰国", "泰国菜": "泰国", "越南菜": "越南", "广东菜": "粤菜", "湖南菜": "湘菜", "四川菜": "川菜"}
# 用户常说的词：每个大类、一部分细类、几个菜名和表外词
QUERIES = [m for m, _, _ in TAX] + ["日料", "韩餐", "粤菜", "川菜", "湘菜", "茶餐厅", "潮汕菜", "意大利菜", "意大利", "法餐", "牛排",
                                    "寿司", "烤肉", "椰子鸡", "面馆", "甜品", "奶茶", "早午餐", "泰国菜",
                                    "意面", "披萨", "烧鹅", "螺蛳粉", "猪脚饭", "椰子水", "咖啡", "pasta"]


def old_hit(s: dict, kw: str) -> bool:
    kw = OLD_SYN.get(kw, kw)
    return any(kw in (s.get(f) or "") for f in ("cate", "name", "dishes"))


def new_hit(s: dict, kw: str, qm: dict) -> int:
    """和 SQL_RECALL 的 kw_hit 一样：0 不中，1 同品类，2 店名或推荐菜里有点名的菜。非餐饮的店不进召回。"""
    cate_re, name_re, dish_re, dish_w, _ = query_res(kw, qm)  # 跨店检索那一级离线算不了，这里不算
    std = s.get("std_cate") or ""
    if "|非餐饮|" in std:
        return 0
    lv = 1 if re.search(cate_re, std, re.I) else 0
    if not std and re.search(name_re, (s.get("name") or "") + "|" + (s.get("cate") or ""), re.I):
        lv = max(lv, 1)
    if re.search(dish_re, (s.get("name") or "") + "|" + (s.get("dishes") or ""), re.I):
        lv = max(lv, int(dish_w))
    return lv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "eval" / "reports" / "cate_recall_1021.md"))
    a = ap.parse_args()
    shops = [json.loads(x) for x in SHOPS.read_text(encoding="utf-8").splitlines() if x.strip()]
    qm = query_map()
    n_std = sum(1 for s in shops if s.get("std_cate"))
    lines = [f"# 品类召回对照（旧 LIKE vs 标准品类）", "",
             f"门店 {len(shops)} 家，打了标准品类的 {n_std} 家。只看品类命中，不算距离和预算。", "",
             "| 查询词 | 旧命中 | 新命中 | 其中点名菜（2 级） | 新增 | 丢掉 |", "|---|---|---|---|---|---|"]
    detail = []
    for q in QUERIES:
        o = {s["amap_id"] for s in shops if old_hit(s, q)}
        lv = {s["amap_id"]: new_hit(s, q, qm) for s in shops}
        n = {k for k, v in lv.items() if v}
        add, drop = n - o, o - n
        lines.append(f"| {q} | {len(o)} | {len(n)} | {sum(1 for v in lv.values() if v >= 2) or ''} | {len(add)} | {len(drop)} |")
        if add or drop:
            by = {s["amap_id"]: s for s in shops}
            detail += [f"### {q}", ""]
            for tag, ids in (("新增", add), ("丢掉", drop)):
                for aid in sorted(ids, key=lambda x: by[x]["name"]):
                    s = by[aid]
                    detail.append(f"- {tag}{'（点名菜）' if lv.get(aid, 0) >= 2 else ''}：{s['name']}｜高德 {s.get('cate')}｜标准 {s.get('std_cate') or '（无）'}｜菜 {(s.get('dishes') or '')[:30]}")
            detail.append("")
    Path(a.out).write_text("\n".join(lines + [""] + detail) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print("明细写到", a.out)


if __name__ == "__main__":
    main()
