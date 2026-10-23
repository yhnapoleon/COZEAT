"""建 3 张表和 4 个知识库（设计文档 3.2、3.3）。幂等：已建的记在 lifeagent_ids.json 里就跳过。

用法：python -I -X utf8 bridge/build_storage.py
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from coze_client import Coze  # noqa: E402

IDS_FILE = HERE.parent / "lifeagent_ids.json"
T, N, D, F, B = 1, 2, 3, 4, 5            # 字段类型：Text Number Date Float Boolean
LIMITED, READONLY, UNLIMITED = 1, 2, 3   # rw_mode：按用户隔离 / 只读 / 共享读写


def fld(name, typ, desc="", required=False):
    return {"name": name, "type": typ, "desc": desc, "must_required": required, "id": 0}


TABLES = {
    # 门店主表：所有用户共享，由 ETL 写入
    "shop": ("门店主表，高德 POI + UGC 聚合 + 团购", UNLIMITED, [
        fld("amap_id", T, "高德 POI id", True), fld("name", T, "店名", True), fld("cate", T, "一级/二级品类"),
        fld("district", T, "区"), fld("biz_area", T, "商圈"), fld("address", T, "地址"),
        fld("lng", F, "经度 GCJ-02"), fld("lat", F, "纬度 GCJ-02"), fld("rating", F, "高德评分"), fld("cost", N, "人均元"),
        fld("open_min", N, "开门分钟"), fld("close_min", N, "打烊分钟，跨零点 +1440"), fld("tags", T, "属性标签，竖线分隔"),
        fld("deals", T, "团购 JSON 数组"), fld("deal_min_price", N, "最低团购价元"), fld("note_cnt", N, "UGC 提及数"),
        fld("hot_score", F, "热度"), fld("dishes", T, "推荐菜，竖线分隔"), fld("synthetic", T, "合成字段名"),
        fld("dy_poi_id", T, "抖音 poi_id")]),
    # 长期记忆：偏好 / 策略 / 反馈，按用户隔离
    "user_memory": ("用户长期记忆：pref / strategy / feedback", LIMITED, [
        fld("kind", T, "pref / strategy / feedback", True), fld("mkey", T, "偏好键或 amap_id", True), fld("mvalue", T, "值"),
        fld("status", T, "candidate / active / superseded / deleted"), fld("support", N, "支持次数"),
        fld("consistency", F, "一致性"), fld("confidence", F, "判定概率"), fld("last_seen", T, "最近出现 ISO 时间"),
        fld("evidence", T, "原话片段"), fld("conv", T, "来源会话"), fld("updated_at", T, "ISO 时间")]),
    # 短期状态：一个会话一行，按用户隔离
    "session_state": ("会话状态：槽位、候选、阶段", LIMITED, [
        fld("conv", T, "会话名", True), fld("slots", T, "槽位 JSON"), fld("candidates", T, "候选 amap_id JSON"),
        fld("last_shown", T, "上轮展示的 amap_id JSON"), fld("stage", T, "collect / recommend / compare / route"),
        fld("turn", N, "轮次"), fld("pending_q", T, "待回答的澄清问题"), fld("updated_at", T, "ISO 时间")]),
}

KBS = {
    "kb_review": ("KB-review", "UGC 评价片段，前缀 amap_id | 店名 | 商圈 | 品类 | 来源 | 日期"),
    "kb_deal": ("KB-deal", "团购套餐描述，一个套餐一段"),
    "kb_faq": ("KB-faq", "本地生活 FAQ，一问一答一段"),
    "kb_rank": ("KB-rank", "榜单名单与媒体稿摘要"),
}


def load_ids():
    return json.loads(IDS_FILE.read_text(encoding="utf-8")) if IDS_FILE.exists() else {}


def save_ids(ids):
    IDS_FILE.write_text(json.dumps(ids, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    c = Coze()
    login = c.login()
    uid = str(login.get("user_id_str") or login.get("user_id") or c.acct.get("user_id") or "")
    ids = load_ids()
    ids.setdefault("tables_online", {})
    ids.setdefault("fields", {})
    for name, (desc, mode, fields) in TABLES.items():
        if name in ids["tables_online"]:
            print("表已存在:", name)
            continue
        body = c.post("/api/memory/database/add", {
            "creator_id": uid, "space_id": c.space_id(), "project_id": "0",
            "icon_uri": "default_icon/default_database_icon.png",
            "table_name": name, "table_desc": desc, "field_list": fields, "rw_mode": mode, "prompt_disabled": False})
        info = body.get("database_info") or body["data"]["database_info"]
        ids["tables_online"][name] = str(info["id"])          # 工作流数据库节点用
        ids.setdefault("tables_draft", {})[name] = str(info.get("draft_id") or "")  # bind_to_bot、list_records 用
        ids["fields"][name] = {f["name"]: str(f["id"]) for f in info.get("field_list", [])}
        save_ids(ids)
        print(f"建表 {name}：{len(info.get('field_list', []))} 个字段，rw_mode={mode}")
    ids.setdefault("kbs", {})
    for key, (name, desc) in KBS.items():
        if key in ids["kbs"]:
            print("知识库已存在:", name)
            continue
        body = c.post("/api/knowledge/create", {
            "name": name, "description": desc, "space_id": c.space_id(),
            "icon_uri": "default_icon/default_dataset_icon.png", "format_type": 0, "biz_id": "0", "project_id": "0"})
        ids["kbs"][key] = str(body.get("dataset_id") or body["data"]["dataset_id"])
        save_ids(ids)
        print("建知识库", name)


if __name__ == "__main__":
    main()
