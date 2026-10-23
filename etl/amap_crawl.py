"""以一个地标为中心抓周边餐饮 POI，转成 shop 表的行，写 data/processed/shops.jsonl。

用法：python -I -X utf8 etl/amap_crawl.py --anchor 海岸城 --city 深圳 --radius 1000 --pages 2
调用量 = 1 次地理编码 + pages 次周边搜索。每页最多 25 条。默认和已有 shops.jsonl 合并，--replace 才覆盖。
"""
import argparse
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from common.env import need  # noqa: E402
from etl.smoke import call  # noqa: E402

OUT = ROOT / "data" / "processed" / "shops.jsonl"
FOOD_TYPES = "050000"  # 餐饮服务大类


def amap(path: str, **params) -> dict:
    params["key"] = need("AMAP_WEB_SERVICE_KEY")
    st, d = call("GET", f"https://restapi.amap.com{path}?" + urllib.parse.urlencode(params))
    if st != 200 or str(d.get("status")) != "1":
        raise RuntimeError(f"amap {path} http={st} info={d.get('info')} infocode={d.get('infocode')}")
    return d


def to_minutes(hhmm: str):
    m = re.match(r"(\d{1,2}):(\d{2})", hhmm or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def parse_hours(s: str):
    """'10:00-22:00' / '11:00-14:00 17:00-02:00' → (最早开门, 最晚打烊)，跨零点时打烊 +1440。"""
    spans = re.findall(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", s or "")
    if not spans:
        return None, None
    opens = [to_minutes(a) for a, _ in spans]
    closes = []
    for a, b in spans:
        o, c = to_minutes(a), to_minutes(b)
        closes.append(c + 1440 if c is not None and o is not None and c <= o else c)
    return min(opens), max(closes)


def to_row(p: dict) -> dict:
    b = p.get("business") or {}
    lng, lat = (p.get("location") or "0,0").split(",")
    levels = (p.get("type") or "").split(";")
    open_min, close_min = parse_hours(b.get("opentime_today") or b.get("opentime_week") or "")
    dishes = [x for x in re.split(r"[,，;；]", b.get("tag") or "") if x][:5]

    def num(x, cast=float):
        try:
            return cast(float(x))
        except (TypeError, ValueError):
            return None

    return {
        "amap_id": p.get("id"), "name": p.get("name"), "cate": "/".join(levels[1:3]) or levels[0],
        "district": p.get("adname") or "", "biz_area": b.get("business_area") or "",
        "address": p.get("address") or "", "lng": float(lng), "lat": float(lat),
        "rating": num(b.get("rating")), "cost": num(b.get("cost"), int),
        "open_min": open_min, "close_min": close_min, "tags": "", "deals": "[]", "deal_min_price": None,
        "note_cnt": 0, "hot_score": 0.0, "dishes": "|".join(dishes), "synthetic": "", "dy_poi_id": "",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--anchor", default="海岸城")
    ap.add_argument("--city", default="深圳")
    ap.add_argument("--radius", type=int, default=1000)
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--replace", action="store_true", help="覆盖 shops.jsonl（默认合并追加）")
    a = ap.parse_args()

    geo = amap("/v3/geocode/geo", address=a.anchor, city=a.city)["geocodes"][0]
    loc = geo["location"]
    print(f"锚点 {a.anchor} → {loc}（{geo.get('district')}）")
    rows, seen = [], set()
    for page in range(1, a.pages + 1):
        d = amap("/v5/place/around", location=loc, radius=a.radius, types=FOOD_TYPES,
                 show_fields="business", page_size=25, page_num=page, sortrule="weight")
        pois = d.get("pois") or []
        for p in pois:
            if p.get("id") in seen:
                continue
            seen.add(p.get("id"))
            rows.append(to_row(p))
        print(f"第 {page} 页 {len(pois)} 条")
        if len(pois) < 25:
            break
        time.sleep(0.4)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if not a.replace and OUT.exists():
        # 默认合并：保留已有门店（含 resolve_mentions 补进来的），只追加新的
        old = [json.loads(l) for l in OUT.read_text(encoding="utf-8").splitlines() if l.strip()]
        have = {r["amap_id"] for r in old}
        new = [r for r in rows if r["amap_id"] not in have]
        print(f"合并：已有 {len(old)} 家，本次 {len(rows)} 家，其中新增 {len(new)} 家")
        rows = old + new
    with OUT.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    rated = sum(1 for r in rows if r["rating"])
    costed = sum(1 for r in rows if r["cost"])
    print(f"写入 {OUT.relative_to(ROOT)}：{len(rows)} 家，有评分 {rated}，有人均 {costed}，有推荐菜 {sum(1 for r in rows if r['dishes'])}")


if __name__ == "__main__":
    main()
