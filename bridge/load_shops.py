"""把 data/processed/shops.jsonl 写进 shop 表的草稿表和线上表。

调试运行（test_run）读草稿表，发布后的对话读线上表，两边都要有数据。
走 /api/memory/database/update_records：database_id 传线上 id，table_type 1 草稿、2 线上。
先删后插，可重复执行。

用法：python -I -X utf8 bridge/load_shops.py [--target both|draft|online]
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from coze_client import Coze  # noqa: E402

ROOT = HERE.parent
SHOPS = ROOT / "data" / "processed" / "shops.jsonl"
IDS_FILE = ROOT / "lifeagent_ids.json"
COLS = ["amap_id", "name", "cate", "district", "biz_area", "address", "lng", "lat", "rating", "cost",
        "open_min", "close_min", "tags", "deals", "deal_min_price", "note_cnt", "hot_score", "dishes", "synthetic", "dy_poi_id",
        "std_cate"]  # std_cate 10-21 加的第 21 列（bridge/alter_table.py），etl/shop_cats.py 生成
NUMERIC = {"lng", "lat", "rating", "cost", "open_min", "close_min", "deal_min_price", "note_cnt", "hot_score"}
INTEGER = {"cost", "open_min", "close_min", "deal_min_price", "note_cnt"}  # Number 列只收整数，26.3 会让 update_records 报 500


def list_all(c: Coze, online_id: str, table_type: int) -> list[dict]:
    out, off = [], 0
    while True:
        b = c.post("/api/memory/database/list_records",
                   {"database_id": online_id, "table_type": table_type, "limit": 100, "offset": off})
        rows = b.get("data") or []
        out += rows
        if not b.get("HasMore") or not rows:
            return out
        off += len(rows)


def to_record(r: dict) -> dict:
    rec = {}
    for k in COLS:
        v = r.get(k)
        if v is None:
            if k in NUMERIC:
                continue  # 数值列留空
            v = ""
        if k in INTEGER and v != "":
            try:
                v = int(round(float(v)))
            except (TypeError, ValueError):
                continue
        rec[k] = str(v)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="both", choices=["both", "draft", "online"])
    a = ap.parse_args()
    ids = json.loads(IDS_FILE.read_text(encoding="utf-8"))
    online_id = ids["tables_online"]["shop"]
    rows = [json.loads(l) for l in SHOPS.read_text(encoding="utf-8").splitlines() if l.strip()]
    c = Coze()
    c.login()
    for tt, label in [(1, "草稿"), (2, "线上")]:
        if a.target != "both" and a.target != {1: "draft", 2: "online"}[tt]:
            continue
        # list_records 一次最多给 100 行，HasMore 还报 False（2026-10-20 实测），所以删到列不出为止
        n_old = 0
        for _ in range(50):
            old = list_all(c, online_id, tt)
            dels = [{"bstudio_id": str(x["bstudio_id"])} for x in old if x.get("bstudio_id")]
            if not dels:
                break
            n_old += len(dels)
            for i in range(0, len(dels), 50):
                c.post("/api/memory/database/update_records",
                       {"database_id": online_id, "table_type": tt, "record_data_delete": dels[i:i + 50]})
        recs = [to_record(r) for r in rows]
        # 按字段组合分批：update_records 一批记录按第一条的字段建列，第一条没有人均（空值不传）时，
        # 同批其他店的人均全丢（10-21 查出 24 家 cost 写成了 NULL）
        groups = {}
        for x in recs:
            groups.setdefault(tuple(sorted(x)), []).append(x)
        for g in groups.values():
            for i in range(0, len(g), 25):
                c.post("/api/memory/database/update_records",
                       {"database_id": online_id, "table_type": tt, "record_data_add": g[i:i + 25]})
        print(f"{label}表：删 {n_old} 行，写 {len(recs)} 行（行数用 SQL 核对，list_records 最多显示 100）")


if __name__ == "__main__":
    main()
