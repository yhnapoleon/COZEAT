"""网页 Demo 的"我的口味"页：读某个用户在线上 user_memory 表里的行，整理成生效偏好、待确认、反馈、推荐策略、历史。

只读。线上表在 coze-mysql 的 opencoze.table_<物理 id>，按 bstudio_connector_uid（= OpenAPI 的 ext.user_id）分用户；
字段名和物理列的对应从 online_database_info.table_field 取。用 docker exec 在容器里跑 mysql，账号用容器自己的环境变量，不写进代码。
"""
import json
import re
import subprocess
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IDS = json.loads((ROOT / "lifeagent_ids.json").read_text(encoding="utf-8"))
NAMES = {"avoid": "忌口", "taste": "口味", "cuisine_like": "喜欢吃", "cuisine_dislike": "不喜欢", "budget": "人均上限",
         "area": "常去商圈", "scene": "常见场景"}
# 提升规则（设计文档 4.6、HANDOVER 第 8 节）：支持 ≥ 2 个不同会话且一致性 ≥ 0.67，或稳定性 ≥ 0.8
PROMOTE_SUPPORT, PROMOTE_CONS, PROMOTE_STABLE = 2, 0.67, 0.8


def _mysql(sql: str) -> str:
    r = subprocess.run(["docker", "exec", "-i", "coze-mysql", "sh", "-c",
                        'mysql -N -B --raw --default-character-set=utf8mb4 -uroot -p"$MYSQL_ROOT_PASSWORD" opencoze 2>/dev/null'],
                       input=sql.encode("utf-8"), capture_output=True, timeout=20)
    return r.stdout.decode("utf-8", "replace")


@lru_cache(maxsize=1)
def _table() -> tuple[str, dict]:
    tid = IDS["tables_online"]["user_memory"]
    out = _mysql(f"SELECT physical_table_name, table_field FROM online_database_info WHERE id = {int(tid)};").strip()
    name, fields = out.split("\t", 1)
    cols = {f["Name"]: f["PhysicalName"] for f in json.loads(fields)}
    return name, cols


def rows(uid: str) -> list[dict]:
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", uid or ""):
        return []
    table, cols = _table()
    pairs = ", ".join(f"'{k}', {v}" for k, v in cols.items())
    sql = (f"SELECT JSON_OBJECT('id', bstudio_id, 'created', DATE_FORMAT(bstudio_create_time, '%Y-%m-%dT%H:%i:%s'), {pairs}) "
           f"FROM {table} WHERE bstudio_connector_uid = '{uid}' ORDER BY bstudio_id DESC LIMIT 400;")
    out = []
    for line in _mysql(sql).splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def _ev(r: dict) -> dict:
    try:
        d = json.loads(r.get("evidence") or "{}")
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def profile(uid: str) -> dict:
    rs = rows(uid)
    active, cand, fb, hist, strategy = [], [], [], [], None
    for r in rs:
        kind, st, ev = r.get("kind"), r.get("status"), _ev(r)
        if kind == "strategy":
            if st == "active" and strategy is None:
                strategy = {"text": r.get("mvalue") or "", "updated_at": r.get("updated_at") or r.get("created")}
            continue
        if kind == "feedback":
            fb.append({"id": r["id"], "label": ev.get("label") or r.get("mvalue"), "shop": ev.get("name") or "", "cate": ev.get("cate") or "",
                       "cost": ev.get("cost"), "others": [o.get("name") for o in ev.get("others") or [] if isinstance(o, dict)],
                       "reason": ev.get("reason") or "", "status": st, "time": r.get("last_seen") or r.get("created")})
            continue
        if kind != "pref":
            continue
        item = {"id": r["id"], "key": r.get("mkey"), "name": NAMES.get(r.get("mkey"), r.get("mkey")), "value": r.get("mvalue") or "",
                "status": st, "explicit": bool(ev.get("explicit")), "support": r.get("support"), "consistency": r.get("consistency"),
                "confidence": r.get("confidence"), "text": ev.get("text") or "", "op": ev.get("op") or "",
                "target_id": ev.get("target_id") or "", "conv": r.get("conv") or "", "time": r.get("last_seen") or r.get("created"),
                "updated_at": r.get("updated_at")}
        if st == "active":
            active.append(item)
        elif st == "candidate":
            cand.append(item)
        else:
            hist.append(item)
    # 待确认按"键 + 值"归组，算出现在几个不同会话、离提升还差多少
    groups = {}
    for c in cand:
        g = groups.setdefault((c["key"], c["value"]), {"key": c["key"], "name": c["name"], "value": c["value"], "convs": set(),
                                                       "texts": [], "ids": [], "conf": 0.0, "op": c["op"]})
        g["convs"].add(c["conv"])
        g["ids"].append(c["id"])
        g["conf"] = max(g["conf"], float(c["confidence"] or 0))
        if c["text"] and c["text"] not in g["texts"]:
            g["texts"].append(c["text"])
    pending = []
    for g in groups.values():
        n = len(g["convs"])
        pending.append({**{k: v for k, v in g.items() if k != "convs"}, "sessions": n, "need": max(0, PROMOTE_SUPPORT - n)})
    pending.sort(key=lambda x: (-x["sessions"], -x["conf"]))
    order = list(NAMES)
    active.sort(key=lambda x: (order.index(x["key"]) if x["key"] in order else 99, -(x["id"] or 0)))
    return {"uid": uid, "active": active, "pending": pending, "feedback": fb[:30], "strategy": strategy, "history": hist[:30],
            "rule": {"support": PROMOTE_SUPPORT, "consistency": PROMOTE_CONS, "stable": PROMOTE_STABLE},
            "counts": {"active": len(active), "pending": len(pending), "feedback": len(fb)}}


if __name__ == "__main__":
    import sys
    print(json.dumps(profile(sys.argv[1]), ensure_ascii=False, indent=1)[:3000])
