"""给已建的表加列（/api/memory/database/update）。

改表接口按 alterId 认列：传了的列保留或修改，alterId 为 0 的是新列，没传的列会被删掉（physicaltable.UpdateFieldInfo）。
所以这里先 get_by_id 取全部现有列原样带上，再追加新列。官方文档说一张表最多 20 列，源码里只有 Excel 导入
（sheet.CheckSheetIsValid）查这个数，改表接口不查。

用法：
  python -I -X utf8 bridge/alter_table.py shop                      # 只列出现有列
  python -I -X utf8 bridge/alter_table.py shop --add std_cate:1:标准品类
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from coze_client import Coze  # noqa: E402

IDS_FILE = HERE.parent / "lifeagent_ids.json"


def get_info(c: Coze, online_id: str) -> dict:
    b = c.post("/api/memory/database/get_by_id", {"id": online_id, "is_draft": False, "need_sys_fields": False})
    return b.get("database_info") or (b.get("data") or {}).get("database_info") or {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("table")
    ap.add_argument("--add", action="append", default=[], help="名字:类型:说明，类型 1 Text 2 Number 3 Date 4 Float 5 Boolean")
    a = ap.parse_args()
    ids = json.loads(IDS_FILE.read_text(encoding="utf-8"))
    online_id = ids["tables_online"][a.table]
    c = Coze()
    c.login()
    info = get_info(c, online_id)
    fields = [f for f in info.get("field_list") or [] if not f.get("is_system_field")]
    for f in fields:
        print(f"{f['name']:16} type={f['type']} alterId={f.get('alterId')} {f.get('desc', '')}")
    print("共", len(fields), "列")
    if not a.add:
        return
    have = {f["name"] for f in fields}
    new = []
    for spec in a.add:
        name, typ, desc = (spec.split(":", 2) + ["", ""])[:3]
        if name in have:
            print("已有列，跳过:", name)
            continue
        new.append({"name": name, "desc": desc, "type": int(typ or 1), "must_required": False, "id": 0, "alterId": 0})
    if not new:
        return
    keep = [{"name": f["name"], "desc": f.get("desc", ""), "type": f["type"], "must_required": bool(f.get("must_required")),
             "id": int(f.get("id") or 0), "alterId": int(f.get("alterId") or 0)} for f in fields]
    if any(not f["alterId"] for f in keep):
        sys.exit("现有列里有 alterId 为 0 的，按接口规则会被当成新列，停下来人工看")
    body = {"id": online_id, "icon_uri": info.get("icon_uri") or "", "table_name": info["table_name"],
            "table_desc": info.get("table_desc") or "", "field_list": keep + new, "rw_mode": info["rw_mode"],
            "prompt_disabled": bool(info.get("prompt_disabled"))}
    c.post("/api/memory/database/update", body)
    after = [f["name"] for f in get_info(c, online_id).get("field_list") or [] if not f.get("is_system_field")]
    print("改后", len(after), "列:", ", ".join(after))
    lost = have - set(after)
    if lost:
        sys.exit("有列丢了: " + ", ".join(sorted(lost)))


if __name__ == "__main__":
    main()
