"""构建 CF-main 调用的 6 个子工作流（设计文档 4.1）。节点源码和 SQL 在 flow_code.py。

  understand  决策请求(D2+D1) → 决策(sidecar) ‖ 意图识别(Coze，兜底) ‖ 记忆抽取 ‖ 槽位提取 → 汇合、合并槽位
  recall      地理编码 → SQL 召回 → 裁剪候选 → 证据召回(批处理) → 合并证据
  compare     对比证据(批处理) → 挂证据 → 对比生成 → 对比格式化
  route       路线(sidecar) → 路线格式化
  hot         榜单搜索(sidecar) → 本地热度 → 整理 → 榜单回答
  memwrite    准备 → [清/写会话状态] ‖ 写记忆 → 删记忆 → 读用户号 → 通知整理(sidecar)
  faq         检索 KB-faq（纯语义）→ 整理条目 → FAQ 回答

子工作流要先发布，主干发布版锁它的版本号（canvas.subflow 的说明）；版本号记在 lifeagent_ids.json 的 sub_versions。

用法：
  python -I -X utf8 bridge/build_subflows.py build [名字...]      # 不写名字就全建
  python -I -X utf8 bridge/build_subflows.py run 名字             # 用 SAMPLES 的输入调试一次（读写草稿表）
  python -I -X utf8 bridge/build_subflows.py release [名字...]    # 调试通过后发布下一个版本，写回 sub_versions

画布另存到 canvases/wf_<名字>.json，sidecar 令牌替换成占位符。
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
import canvas as cv  # noqa: E402
import flow_code as fc  # noqa: E402
from coze_client import Coze  # noqa: E402
from workflow_api import create_workflow, publish_workflow, save_workflow, summarize_process, test_run  # noqa: E402
from common.env import need  # noqa: E402

IDS_FILE = ROOT / "lifeagent_ids.json"
M = (fc.MODEL_TYPE, fc.MODEL_NAME)
S = cv.ENTRY_ID
E = cv.edge
LS = {"type": "string"}


def tok() -> dict:
    return {"X-Sidecar-Token": need("SIDECAR_TOKEN")}


def ins(*names, vt="string"):
    return [cv.out(n, vt) for n in names]


def exit_vars(pairs: list[tuple]) -> dict:
    """[(名字, 来源节点, 来源字段, 类型)] → 结束节点。"""
    return cv.exit_node([cv.param(nm, cv.ref(src, field, vt, schema=LS if vt.startswith("list") else None))
                         for nm, src, field, vt in pairs], pos=(3000, 0))


# ---------------- understand：理解本轮（意图、反馈、偏好、槽位） ----------------
UNDERSTAND_IN = [("query", "string"), ("prev_slots", "string"), ("shown", "string"), ("shown_text", "string"),
                 ("prof_lines", "string"), ("prof_json", "string"), ("active_json", "string"), ("pending", "integer"),
                 ("conv", "string"), ("turn", "integer"), ("stage", "string"), ("pending_q", "string")]
UNDERSTAND_OUT = [("route", "j1"), ("cid", "j1", "integer"), ("clarify", "j1"), ("clarify_ans", "j1"), ("dec_note", "j1"),
                  ("empty", "j1"), ("question_ans", "c1"),
                  ("ins_items", "j1", "list<string>"), ("del_items", "j1", "list<string>"), ("pm_reply", "j1"),
                  ("acc_reply", "j1"), ("acc_shown", "j1"),
                  ("area_q", "c1"), ("kw", "c1"), ("budget", "c1"), ("slots_json", "c1"), ("need_clarify", "c1", "boolean"),
                  ("question", "c1"), ("turn", "c1"), ("scene_tag", "c1"), ("taste", "c1"), ("slots_eff", "c1"),
                  ("profile_note", "c1"), ("profile_eff", "c1"), ("off_ids", "c1"),
                  ("bmin", "c1"), ("dist_max", "c1"), ("cate_re", "c1"), ("name_re", "c1"), ("dish_re", "c1"),
                  ("dish_w", "c1"), ("kb_q", "c1"), ("kb_on", "c1"), ("kb_w", "c1"), ("conds_json", "c1")]


def build_understand(ids: dict) -> str:
    n = {k: cv.new_id() for k in ["dq", "hd", "i1", "m1", "l1", "j1", "c1"]}
    a = {nm: cv.ref(S, nm, vt) for nm, vt in UNDERSTAND_IN}
    nodes = [cv.entry([cv.out(nm, vt) for nm, vt in UNDERSTAND_IN])]
    nodes.append(cv.code(n["dq"], "决策请求", [cv.param(k, a[k]) for k in ["query", "shown", "shown_text", "stage", "pending_q"]],
                         [cv.out("body")], fc.CODE_DEC_REQ, pos=(300, -300)))
    nodes.append(cv.http_post_json(n["hd"], "决策(sidecar)", f"{fc.SIDECAR}/decide", tok(),
                                   "{{block_output_" + n["dq"] + ".body}}", pos=(600, -300),
                                   default_on_error={"body": "{}", "statusCode": 0, "headers": ""}))
    # Coze 意图节点照常跑：/decide 超时或出错时用它，平时它的结果写进 dec_note 当影子对照。
    # 用标准模式：极速模式只认纯数字输出，DeepSeek 下会报 invalid syntax；它自己出错时返回 0（其他），不拖垮整轮
    nodes.append(cv.intent(n["i1"], "意图识别", a["query"], fc.INTENTS, *M, mode="all", pos=(450, -100),
                           default_on_error={"classificationId": 0, "reason": ""}))
    nodes.append(cv.llm_m(n["m1"], "记忆抽取", "用户本轮输入：{{query}}\n上轮推荐：\n{{shown}}\n已记住的偏好：\n{{prof}}",
                          (fc.PROMPTS / "memory_extract.v2.md").read_text(encoding="utf-8"),
                          [cv.param("query", a["query"]), cv.param("shown", a["shown_text"]), cv.param("prof", a["prof_lines"])],
                          [cv.out("output")], *M, json_mode=False, max_tokens=4096, pos=(450, 100)))
    # 槽位提取和意图并行：以前在分流之后串行跑，找店一轮多等 2–3 秒；其他意图白跑一次
    nodes.append(cv.llm_m(n["l1"], "槽位提取", "用户本轮输入：{{query}}\n上轮已知条件：{{prev_slots}}",
                          (fc.PROMPTS / "slot_extract.v7.md").read_text(encoding="utf-8"),
                          [cv.param("query", a["query"]), cv.param("prev_slots", a["prev_slots"])],
                          [cv.out("output")], *M, json_mode=False, max_tokens=2048, pos=(450, 300)))
    nodes.append(cv.code(n["j1"], "汇合", [
        cv.param("cid", cv.ref(n["i1"], "classificationId", "integer")), cv.param("mem", cv.ref(n["m1"], "output")),
        cv.param("dec", cv.ref(n["hd"], "body")), cv.param("shown", a["shown"]), cv.param("active_json", a["active_json"]),
        cv.param("pending", a["pending"]), cv.param("conv", a["conv"]), cv.param("query", a["query"]),
        cv.param("stage", a["stage"]), cv.param("slot_raw", cv.ref(n["l1"], "output"))],
        [cv.out("route"), cv.out("cid", "integer"), cv.out("clarify"), cv.out("clarify_ans"), cv.out("dec_note"), cv.out("empty"),
         cv.out("ins_items", "list", schema=LS), cv.out("del_items", "list", schema=LS), cv.out("pm_reply"),
         cv.out("acc_reply"), cv.out("acc_shown")],
        fc.CODE_JOIN, pos=(900, -100)))
    # 合并槽位要等记忆抽取：本轮改口推翻的画像条目（target_id）本轮就不能再当默认值（10-21，问题 2）
    nodes.append(cv.code(n["c1"], "合并槽位", [
        cv.param("prev_slots", a["prev_slots"]), cv.param("turn", a["turn"]), cv.param("prof_json", a["prof_json"]),
        cv.param("raw", cv.ref(n["l1"], "output")), cv.param("mem", cv.ref(n["m1"], "output")),
        cv.param("active_json", a["active_json"]), cv.param("stage", a["stage"])],
        [cv.out("area_q"), cv.out("kw"), cv.out("budget"), cv.out("slots_json"), cv.out("need_clarify", "boolean"),
         cv.out("question"), cv.out("question_ans"), cv.out("turn"), cv.out("now"), cv.out("scene_tag"), cv.out("taste"), cv.out("slots_eff"),
         cv.out("profile_note"), cv.out("profile_eff"), cv.out("off_ids"), cv.out("bmin"), cv.out("dist_max"),
         cv.out("cate_re"), cv.out("name_re"), cv.out("dish_re"), cv.out("dish_w"),
         cv.out("kb_q"), cv.out("kb_on"), cv.out("kb_w"), cv.out("conds_json")], fc.CODE_MERGE, pos=(900, 300)))
    nodes.append(exit_vars([(x[0], n[x[1]], x[0], x[2] if len(x) > 2 else "string") for x in UNDERSTAND_OUT]))
    edges = [E(S, n["dq"]), E(n["dq"], n["hd"]), E(n["hd"], n["j1"]),
             E(S, n["i1"]), *[E(n["i1"], n["j1"], f"branch_{i}") for i in range(len(fc.INTENTS))], E(n["i1"], n["j1"], "default"),
             E(S, n["m1"]), E(n["m1"], n["j1"]), E(n["m1"], n["c1"]), E(S, n["l1"]), E(n["l1"], n["c1"]), E(n["l1"], n["j1"]),
             E(n["j1"], cv.EXIT_ID), E(n["c1"], cv.EXIT_ID)]
    return cv.canvas(nodes, edges)


# ---------------- recall：召回候选并挂上 KB-review 证据 ----------------
RECALL_IN = ["area_q", "kw", "budget", "scene_tag", "taste", "slots_json", "bmin", "dist_max", "cate_re", "name_re", "dish_re", "dish_w", "kb_q", "kb_on", "kb_w"]


def evidence_batch(ids: dict, bid: str, title: str, items_ref: dict, size: int, pos) -> tuple[dict, list[str]]:
    """每家查一次 KB-review（查询词"品牌名 + amap_id"），按段首 amap_id 过滤（设计文档 4.4）。"""
    u, k, f = cv.new_id(), cv.new_id(), cv.new_id()
    inner = [
        cv.code(u, "解包", [cv.param("row", cv.ref(bid, "items"))], [cv.out("amap_id"), cv.out("kb_query")],
                fc.CODE_EVID_UNPACK, pos=(100, 100)),
        cv.knowledge_retriever(k, "检索评价", [str(ids["kbs"]["kb_review"])], cv.ref(u, "kb_query"), top_k=8, min_score=0.2,
                               pos=(400, 100)),
        cv.code(f, "过滤证据", [cv.param("amap_id", cv.ref(u, "amap_id")),
                               cv.param("rows", cv.ref(k, "outputList", "list<object>",
                                                       schema={"type": "object", "schema": [cv.out("output")]}))],
                [cv.out("evid")], fc.CODE_EVID_FILTER, pos=(700, 100)),
    ]
    node = cv.batch(bid, title, items_ref, inner, cv.batch_edges(bid, [u, k, f]),
                    outputs=[{"name": "evid", "input": cv.ref(f, "evid", "list<string>")}],
                    batch_size=size, concurrent=min(size, 5), pos=pos)
    return node, [u, k, f]


def build_recall(ids: dict) -> str:
    T = ids["tables_online"]
    n = {k: cv.new_id() for k in ["h1", "c2", "r1", "c3", "b1", "c5", "kx", "kc"]}
    a = {nm: cv.ref(S, nm) for nm in RECALL_IN}
    nodes = [cv.entry(ins(*RECALL_IN))]
    geo = cv.http_get_h(n["h1"], "地理编码(sidecar)", f"{fc.SIDECAR}/geo", tok(), pos=(300, 0),
                        default_on_error={"body": "{}", "statusCode": 0, "headers": ""})
    geo["data"]["inputs"]["params"] = [cv.param("address", a["area_q"])]
    nodes.append(geo)
    nodes.append(cv.code(n["c2"], "解析坐标", [cv.param("body", cv.ref(n["h1"], "body"))],
                         [cv.out("lng"), cv.out("lat"), cv.out("anchor"), cv.out("geo_fallback", "boolean")], fc.CODE_GEO,
                         pos=(600, 0)))
    # 跨店召回（10-21）：和地理编码并行，拿"品类 + 要求 + 场景"在 KB-review 里检索（混合检索，本机已打检索补丁），收敛到店后交给 SQL
    nodes.append(cv.knowledge_retriever(n["kx"], "跨店检索", [str(ids["kbs"]["kb_review"])],
                                        a["kb_q"], top_k=20, min_score=0.2, pos=(300, 250), strategy=1))
    nodes.append(cv.code(n["kc"], "跨店收敛", [
        cv.param("rows", cv.ref(n["kx"], "outputList", "list<object>", schema={"type": "object", "schema": [cv.out("output")]})),
        cv.param("kb_on", a["kb_on"])], [cv.out("kb_ids"), cv.out("kb_json")], fc.CODE_KB_CROSS, pos=(600, 250)))
    nodes.append(cv.custom_sql(n["r1"], "SQL 召回", T["shop"], fc.SQL_RECALL, [
        cv.param("lng", cv.ref(n["c2"], "lng")), cv.param("lat", cv.ref(n["c2"], "lat")), cv.param("budget", a["budget"]),
        cv.param("bmin", a["bmin"]), cv.param("cate_re", a["cate_re"]), cv.param("name_re", a["name_re"]),
        cv.param("dish_re", a["dish_re"]), cv.param("dish_w", a["dish_w"]), cv.param("kb_ids", cv.ref(n["kc"], "kb_ids")), cv.param("kb_w", a["kb_w"]),
        cv.param("scene", a["scene_tag"])], fc.ROW_SCHEMA, pos=(900, 0)))
    nodes.append(cv.code(n["c3"], "裁剪候选", [
        cv.param("rows", cv.ref(n["r1"], "outputList", "list<object>", schema={"type": "object", "schema": fc.ROW_SCHEMA})),
        cv.param("scene", a["scene_tag"]), cv.param("kw", a["kw"]), cv.param("taste", a["taste"]),
        cv.param("dist_max", a["dist_max"]), cv.param("budget", a["budget"]), cv.param("bmin", a["bmin"]), cv.param("kb_on", a["kb_on"])],
        [cv.out("candidates_json"), cv.out("count", "integer"), cv.out("cand_items", "list", schema=LS), cv.out("relaxed"),
         cv.out("funnel")],
        fc.CODE_TRIM, pos=(1200, 0)))
    b, _ = evidence_batch(ids, n["b1"], "证据召回", cv.ref(n["c3"], "cand_items", "list<string>"), 10, (1500, 0))
    nodes.append(b)
    nodes.append(cv.code(n["c5"], "合并证据", [cv.param("candidates_json", cv.ref(n["c3"], "candidates_json")),
                                              cv.param("slots_json", a["slots_json"]), cv.param("scene_tag", a["scene_tag"]),
                                              cv.param("kb_json", cv.ref(n["kc"], "kb_json")),
                                              cv.param("funnel", cv.ref(n["c3"], "funnel")),
                                              cv.param("evid", cv.ref(n["b1"], "evid", "list<string>"))],
                         [cv.out("candidates_json"), cv.out("with_evidence", "integer"), cv.out("funnel")], fc.CODE_EVID_MERGE,
                         pos=(1800, 0)))
    nodes.append(exit_vars([("candidates_json", n["c5"], "candidates_json", "string"),
                            ("cands_all", n["c3"], "candidates_json", "string"), ("anchor", n["c2"], "anchor", "string"),
                            ("anchor_lng", n["c2"], "lng", "string"), ("anchor_lat", n["c2"], "lat", "string"),
                            ("relaxed", n["c3"], "relaxed", "string"), ("count", n["c3"], "count", "integer"),
                            ("with_evidence", n["c5"], "with_evidence", "integer"), ("funnel", n["c5"], "funnel", "string"),
                            ("geo_fallback", n["c2"], "geo_fallback", "boolean")]))
    edges = [E(S, n["h1"]), E(n["h1"], n["c2"]), E(n["c2"], n["r1"]), E(S, n["kx"]), E(n["kx"], n["kc"]), E(n["kc"], n["r1"]),
             E(n["r1"], n["c3"]), E(n["c3"], n["b1"]),
             E(n["b1"], n["c5"]), E(n["c5"], cv.EXIT_ID)]
    return cv.canvas(nodes, edges)


# ---------------- compare：2–3 家固定维度对比 ----------------
def build_compare(ids: dict) -> str:
    n = {k: cv.new_id() for k in ["kb", "k5", "k6", "k7"]}
    nodes = [cv.entry(ins("query", "profile", "targets_json") + [cv.out("cand_items", "list", schema=LS)])]
    b, _ = evidence_batch(ids, n["kb"], "对比证据", cv.ref(S, "cand_items", "list<string>"), 3, (300, 0))
    nodes.append(b)
    nodes.append(cv.code(n["k5"], "挂证据", [cv.param("targets_json", cv.ref(S, "targets_json")),
                                            cv.param("evid", cv.ref(n["kb"], "evid", "list<string>"))],
                         [cv.out("targets_json")], fc.CODE_CMP_MERGE, pos=(600, 0)))
    nodes.append(cv.llm_m(n["k6"], "对比生成", "用户问题：{{query}}\n用户画像：{{profile}}\n门店：{{shops}}",
                          (fc.PROMPTS / "compare.v1.md").read_text(encoding="utf-8"),
                          [cv.param("query", cv.ref(S, "query")), cv.param("profile", cv.ref(S, "profile")),
                           cv.param("shops", cv.ref(n["k5"], "targets_json"))],
                          [cv.out("output")], *M, json_mode=False, max_tokens=4096, pos=(900, 0)))
    nodes.append(cv.code(n["k7"], "对比格式化", [cv.param("out", cv.ref(n["k6"], "output")),
                                                cv.param("targets_json", cv.ref(n["k5"], "targets_json")),
                                                cv.param("orig_json", cv.ref(S, "targets_json"))],
                         [cv.out("answer")], fc.CODE_CMP_FORMAT, pos=(1200, 0)))
    nodes.append(exit_vars([("answer", n["k7"], "answer", "string")]))
    edges = [E(S, n["kb"]), E(n["kb"], n["k5"]), E(n["k5"], n["k6"]), E(n["k6"], n["k7"]), E(n["k7"], cv.EXIT_ID)]
    return cv.canvas(nodes, edges)


# ---------------- route：高德步行/驾车/公交（sidecar 并发查） ----------------
def build_route(ids: dict) -> str:
    n = {k: cv.new_id() for k in ["h3", "k8"]}
    keys = ["to_lng", "to_lat", "to_name", "origin"]
    nodes = [cv.entry(ins(*keys))]
    nav = cv.http_get_h(n["h3"], "路线(sidecar)", f"{fc.SIDECAR}/route", tok(), pos=(300, 0),
                        default_on_error={"body": "{}", "statusCode": 0, "headers": ""})
    nav["data"]["inputs"]["params"] = [cv.param(k, cv.ref(S, k)) for k in keys]
    nodes.append(nav)
    nodes.append(cv.code(n["k8"], "路线格式化", [cv.param("body", cv.ref(n["h3"], "body")), cv.param("to_name", cv.ref(S, "to_name")),
                                                cv.param("origin", cv.ref(S, "origin"))],
                         [cv.out("answer")], fc.CODE_NAV_FORMAT, pos=(600, 0)))
    nodes.append(exit_vars([("answer", n["k8"], "answer", "string")]))
    return cv.canvas(nodes, [E(S, n["h3"]), E(n["h3"], n["k8"]), E(n["k8"], cv.EXIT_ID)])


# ---------------- hot：网页榜单（博查，sidecar 按天缓存）+ 本地小红书热度 ----------------
def build_hot(ids: dict) -> str:
    T = ids["tables_online"]
    n = {k: cv.new_id() for k in ["h4", "s5", "k9", "l3"]}
    nodes = [cv.entry(ins("query"))]
    hot = cv.http_get_h(n["h4"], "榜单搜索(sidecar)", f"{fc.SIDECAR}/hot", tok(), pos=(300, -100),
                        default_on_error={"body": "{}", "statusCode": 0, "headers": ""})
    hot["data"]["inputs"]["params"] = [cv.param("q", cv.ref(S, "query"))]
    nodes.append(hot)
    nodes.append(cv.custom_sql(n["s5"], "本地热度", T["shop"], fc.SQL_HOT, [], fc.HOT_SCHEMA, pos=(300, 100)))
    nodes.append(cv.code(n["k9"], "整理榜单输入", [cv.param("body", cv.ref(n["h4"], "body")),
                                                 cv.param("rows", cv.ref(n["s5"], "outputList", "list<object>",
                                                                          schema={"type": "object", "schema": fc.HOT_SCHEMA}))],
                         [cv.out("web"), cv.out("local")], fc.CODE_HOT_PREP, pos=(600, 0)))
    nodes.append(cv.llm_m(n["l3"], "榜单回答", "用户问题：{{query}}\n\n网页搜索结果：\n{{web}}\n\n本地热度表：\n{{local}}",
                          (fc.PROMPTS / "hot.v2.md").read_text(encoding="utf-8"),
                          [cv.param("query", cv.ref(S, "query")), cv.param("web", cv.ref(n["k9"], "web")),
                           cv.param("local", cv.ref(n["k9"], "local"))],
                          [cv.out("output")], *M, json_mode=False, max_tokens=4096, pos=(900, 0)))
    nodes.append(exit_vars([("answer", n["l3"], "output", "string")]))
    # 网页搜索和本地热度并行，整理节点等两边
    edges = [E(S, n["h4"]), E(S, n["s5"]), E(n["h4"], n["k9"]), E(n["s5"], n["k9"]), E(n["k9"], n["l3"]),
             E(n["l3"], cv.EXIT_ID)]
    return cv.canvas(nodes, edges)


# ---------------- memwrite：每轮末尾写会话状态和长期记忆，通知 sidecar 异步整理 ----------------
MEM_FIELDS = ["kind", "mkey", "mvalue", "status", "support", "cons", "conf", "ev", "conv", "now", "id"]


def build_memwrite(ids: dict) -> str:
    T = ids["tables_online"]
    n = {k: cv.new_id() for k in ["pp", "xs", "w1", "w2", "bi", "biu", "bis", "bd", "bdu", "bds", "s3", "c7", "h2"]}
    keys = ["conv", "stage", "slots", "cands", "shown", "q", "turn"]
    nodes = [cv.entry(ins(*keys) + [cv.out("ins_items", "list", schema=LS), cv.out("del_items", "list", schema=LS)])]
    nodes.append(cv.code(n["pp"], "准备", [cv.param(k, cv.ref(S, k)) for k in keys[1:]],
                         [cv.out("do_state", "boolean")] + ins("stage", "slots", "cands", "shown", "q", "turn", "now"),
                         fc.CODE_MW_PREP, pos=(300, 0)))
    pp = lambda k: cv.ref(n["pp"], k)  # noqa: E731
    nodes.append(cv.selector(n["xs"], "要写会话状态吗", [[(cv.ref(n["pp"], "do_state", "boolean"), "==", cv.lit(True, "boolean"))]],
                             pos=(600, -200)))
    nodes.append(cv.custom_sql(n["w1"], "清会话状态", T["session_state"], fc.SQL_STATE_DEL, [cv.param("conv", cv.ref(S, "conv"))],
                               [], pos=(900, -250)))
    nodes.append(cv.custom_sql(n["w2"], "写会话状态", T["session_state"], fc.SQL_STATE_INS, [
        cv.param("conv", cv.ref(S, "conv")), cv.param("slots", pp("slots")), cv.param("cands", pp("cands")),
        cv.param("shown", pp("shown")), cv.param("stage", pp("stage")), cv.param("turn", pp("turn")), cv.param("q", pp("q")),
        cv.param("now", pp("now"))], [], pos=(1200, -250)))
    nodes.append(cv.batch(n["bi"], "写记忆", cv.ref(S, "ins_items", "list<string>"), [
        cv.code(n["biu"], "解包", [cv.param("row", cv.ref(n["bi"], "items"))], [cv.out(k) for k in MEM_FIELDS],
                fc.CODE_MEM_UNPACK, pos=(100, 100)),
        cv.custom_sql(n["bis"], "插入", T["user_memory"], fc.SQL_MEM_INS,
                      [cv.param(k, cv.ref(n["biu"], k)) for k in MEM_FIELDS[:-1]], [], pos=(400, 100))],
        cv.batch_edges(n["bi"], [n["biu"], n["bis"]]), outputs=[{"name": "kind", "input": cv.ref(n["biu"], "kind", "list<string>")}],
        batch_size=20, concurrent=1, pos=(600, 200)))
    nodes.append(cv.batch(n["bd"], "删记忆", cv.ref(S, "del_items", "list<string>"), [
        cv.code(n["bdu"], "解包", [cv.param("row", cv.ref(n["bd"], "items"))], [cv.out(k) for k in MEM_FIELDS],
                fc.CODE_MEM_UNPACK, pos=(100, 100)),
        cv.custom_sql(n["bds"], "标记删除", T["user_memory"], fc.SQL_MEM_DEL,
                      [cv.param("now", cv.ref(n["bdu"], "now")), cv.param("id", cv.ref(n["bdu"], "id"))], [], pos=(400, 100))],
        cv.batch_edges(n["bd"], [n["bdu"], n["bds"]]), outputs=[{"name": "id", "input": cv.ref(n["bdu"], "id", "list<string>")}],
        batch_size=20, concurrent=1, pos=(900, 200)))
    nodes.append(cv.custom_sql(n["s3"], "读用户号", T["user_memory"], fc.SQL_UID, [], [cv.out("u")], pos=(1200, 200)))
    nodes.append(cv.code(n["c7"], "取用户号", [cv.param("rows", cv.ref(n["s3"], "outputList", "list<object>",
                                                                    schema={"type": "object", "schema": [cv.out("u")]}))],
                         [cv.out("uid")], fc.CODE_UID, pos=(1500, 200)))
    touch = cv.http_get_h(n["h2"], "通知整理(sidecar)", f"{fc.SIDECAR}/mem/touch", tok(), pos=(1800, 200),
                          default_on_error={"body": "{}", "statusCode": 0, "headers": ""})
    touch["data"]["inputs"]["params"] = [cv.param("uid", cv.ref(n["c7"], "uid"))]
    nodes.append(touch)
    nodes.append(exit_vars([("uid", n["c7"], "uid", "string"), ("stage", n["pp"], "stage", "string")]))
    # 会话状态和长期记忆是两张表，并行写；结束节点等两边（不写会话状态时从选择器的 false 口过去）
    edges = [E(S, n["pp"]), E(n["pp"], n["xs"]), E(n["xs"], n["w1"], "true"), E(n["w1"], n["w2"]), E(n["w2"], cv.EXIT_ID),
             E(n["xs"], cv.EXIT_ID, "false"),
             E(n["pp"], n["bi"]), E(n["bi"], n["bd"]), E(n["bd"], n["s3"]), E(n["s3"], n["c7"]), E(n["c7"], n["h2"]),
             E(n["h2"], cv.EXIT_ID)]
    return cv.canvas(nodes, edges)


# ---------------- faq：问规则、问助手、闲聊（检索 KB-faq 后回答） ----------------
FAQ_IN = [("query", "string"), ("cid", "integer"), ("shown_text", "string")]
FAQ_TOP_K, FAQ_MIN_SCORE = 3, 0.74  # 分数是 (cos+1)/2；0.74 时库内 57/66 能检索到、库外 9/28 误返回（eval/reports/FAQ_faq-1021b.md）


def build_faq(ids: dict) -> str:
    n = {k: cv.new_id() for k in ["k", "c", "l"]}
    a = {nm: cv.ref(S, nm, vt) for nm, vt in FAQ_IN}
    nodes = [cv.entry([cv.out(nm, vt) for nm, vt in FAQ_IN])]
    # 纯语义检索：Coze 混合检索的 RRF 取最大值、同分随机，E2 上明显差于纯语义
    nodes.append(cv.knowledge_retriever(n["k"], "检索FAQ", [str(ids["kbs"]["kb_faq"])], a["query"], top_k=FAQ_TOP_K,
                                        min_score=FAQ_MIN_SCORE, pos=(300, 0), strategy=0))
    nodes.append(cv.code(n["c"], "整理条目", [cv.param("rows", cv.ref(n["k"], "outputList", "list<object>",
                                                                   schema={"type": "object", "schema": [cv.out("output")]}))],
                         [cv.out("faq"), cv.out("n_hit", "integer")], fc.CODE_FAQ_PREP, pos=(600, 0)))
    nodes.append(cv.llm_m(n["l"], "FAQ回答", "用户说：{{query}}\n意图编号：{{cid}}\n上轮推荐：\n{{shown}}\n检索到的 FAQ 条目：\n{{faq}}",
                          (fc.PROMPTS / "faq.v1.md").read_text(encoding="utf-8"),
                          [cv.param("query", a["query"]), cv.param("cid", a["cid"]), cv.param("shown", a["shown_text"]),
                           cv.param("faq", cv.ref(n["c"], "faq"))],
                          [cv.out("output")], *M, json_mode=False, max_tokens=4096, pos=(900, 0)))
    nodes.append(exit_vars([("answer", n["l"], "output", "string"), ("n_hit", n["c"], "n_hit", "integer"),
                            ("faq", n["c"], "faq", "string")]))
    edges = [E(S, n["k"]), E(n["k"], n["c"]), E(n["c"], n["l"]), E(n["l"], cv.EXIT_ID)]
    return cv.canvas(nodes, edges)


# 名字 → (构建函数, Coze 里的工作流名, 说明)
SUBS = {
    "understand": (build_understand, "la_understand", "理解本轮：决策层意图/反馈、记忆抽取、槽位提取"),
    "recall": (build_recall, "la_recall", "召回候选并挂 KB-review 证据"),
    "compare": (build_compare, "la_compare", "2–3 家门店对比"),
    "route": (build_route, "la_route", "到店路线"),
    "hot": (build_hot, "la_hot", "榜单：网页搜索 + 本地热度"),
    "memwrite": (build_memwrite, "la_memwrite", "写会话状态和长期记忆，通知异步整理"),
    "faq": (build_faq, "la_faq", "问规则、问助手、闲聊：检索 KB-faq 后回答"),
}


def samples() -> dict:
    """调试和发布前用的输入（test_run 的值都是字符串，列表写 JSON）。"""
    shops = {}
    for line in (ROOT / "data" / "processed" / "shops.jsonl").read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        shops[r["amap_id"]] = r
    two = [shops[x] for x in ("B0L0YH6WAC", "B0LK7L3MKL") if x in shops]
    targets = [{"amap_id": s["amap_id"], "name": s["name"], "cate": s["cate"], "area": s["biz_area"], "rating": s["rating"],
                "cost": s["cost"], "dishes": str(s.get("dishes") or "").replace("|", "、"), "tags": s.get("tags") or "",
                "note_cnt": s.get("note_cnt"), "lng": s["lng"], "lat": s["lat"], "dist_m": ""} for s in two]
    items = [json.dumps({"amap_id": t["amap_id"], "q": t["name"].split("(")[0] + " " + t["amap_id"]}, ensure_ascii=False)
             for t in targets]
    return {
        "understand": {"query": "海岸城附近人均100以内的火锅", "prev_slots": "{}", "shown": "[]", "shown_text": "（无）",
                       "prof_lines": "（无）", "prof_json": "{}", "active_json": "[]", "pending": "0", "conv": "subflow-test",
                       "turn": "0", "stage": "", "pending_q": ""},
        "recall": {"area_q": "海岸城", "kw": "火锅", "budget": "100", "scene_tag": "", "taste": "",
                   "slots_json": json.dumps({"area": "海岸城", "category": "火锅", "budget_max": 100}, ensure_ascii=False),
                   "bmin": "0", "dist_max": "0", "cate_re": "[|](火锅)[|]", "name_re": "(火锅|火锅店)", "dish_re": "(火锅)", "dish_w": "1",
                   "kb_q": "火锅 不用排队", "kb_on": "1", "kb_w": "0"},
        "compare": {"query": "SOKO SOKO 和木槿钰哪个好", "profile": "（空）", "targets_json": json.dumps(targets, ensure_ascii=False),
                    "cand_items": json.dumps(items, ensure_ascii=False)},
        "route": {"to_lng": str(two[0]["lng"]), "to_lat": str(two[0]["lat"]), "to_name": two[0]["name"], "origin": "南山科技园"},
        "hot": {"query": "海岸城有什么热门的店"},
        "faq": {"query": "团购券过期了还能退吗", "cid": "6", "shown_text": "（无）"},
        "memwrite": {"conv": "subflow-test", "stage": "collect", "slots": json.dumps({"area": "海岸城"}, ensure_ascii=False),
                     "cands": "[]", "shown": "[]", "q": "想吃点什么？", "turn": "1", "ins_items": "[]", "del_items": "[]"},
    }


def next_version(v: str | None) -> str:
    if not v:
        return "v0.0.1"
    a, b, c = v.lstrip("v").split(".")
    return f"v{a}.{b}.{int(c) + 1}"


def ensure(c: Coze, ids: dict, name: str) -> str:
    wf = ids.setdefault("workflows", {})
    if name not in wf:
        _, wname, desc = SUBS[name]
        wf[name] = create_workflow(c, wname, desc)
        IDS_FILE.write_text(json.dumps(ids, ensure_ascii=False, indent=1), encoding="utf-8")
        print("建工作流", name, wf[name])
    return wf[name]


def build_one(c: Coze, ids: dict, name: str) -> None:
    wid = ensure(c, ids, name)
    schema = SUBS[name][0](ids)
    r = save_workflow(c, wid, schema)
    print("保存", name, r.get("code"), r.get("msg") or "")
    (ROOT / "canvases").mkdir(exist_ok=True)
    (ROOT / "canvases" / f"wf_{name}.json").write_text(schema.replace(need("SIDECAR_TOKEN"), "<SIDECAR_TOKEN>"),
                                                       encoding="utf-8")


def run_one(c: Coze, ids: dict, name: str) -> dict:
    d = test_run(c, ids["workflows"][name], samples()[name], bot_id=ids["bot_id"], timeout_s=240)
    return d


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    names = sys.argv[2:] or list(SUBS)
    bad = [x for x in names if x not in SUBS]
    if bad:
        raise SystemExit(f"不认识：{bad}；可选 {list(SUBS)}")
    c = Coze()
    c.login()
    ids = json.loads(IDS_FILE.read_text(encoding="utf-8"))
    for name in names:
        if cmd == "build":
            build_one(c, ids, name)
        elif cmd == "run":
            print(f"== {name}")
            print(summarize_process(run_one(c, ids, name))[-5000:])
        elif cmd == "release":
            build_one(c, ids, name)
            d = run_one(c, ids, name)
            if d.get("executeStatus") != 2:
                print(summarize_process(d)[-2500:])
                raise SystemExit(f"{name} 调试未通过，不发布")
            ver = next_version(ids.get("sub_versions", {}).get(name))
            r = publish_workflow(c, ids["workflows"][name], version=ver, desc=SUBS[name][2])
            if r.get("code"):
                raise SystemExit(f"{name} 发布失败：{r}")
            ids.setdefault("sub_versions", {})[name] = ver
            IDS_FILE.write_text(json.dumps(ids, ensure_ascii=False, indent=1), encoding="utf-8")
            print("发布", name, ver, f"{d.get('workflowExeCost')}")
        else:
            raise SystemExit(f"不认识的命令 {cmd}")


if __name__ == "__main__":
    main()
