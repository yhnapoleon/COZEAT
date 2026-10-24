"""构建主干对话流 CF-main（设计文档 4.1、4.2）。

v0.0.11 起拆成子工作流（build_subflows.py 建），主干只留读状态、分流和推荐生成：
  读会话状态 → 上轮槽位 → 读画像 → 画像摘要 → WF-understand（决策层 D2 意图、D1 反馈；记忆抽取；槽位提取）→ 分流
    rec   要追问吗 → 追问 ｜ WF-recall → 推荐生成 → 格式化
    pm    偏好回复
    cmp/nav/dl  读门店 → 认店 → WF-compare ｜ WF-route ｜ 团购回答 ｜ 认店追问
    hot   WF-hot
    ask   意图澄清（D2 置信度低于阈值）
    other WF-faq（检索 KB-faq 后回答；FAQ、闲聊、其他，v0.0.13 起替换通用回复）
  → 汇总（回答 + 要写的会话状态）→ WF-memwrite → 结束（不流式）
节点源码和 SQL 在 flow_code.py。

用法：
  python -I -X utf8 bridge/build_cf_main.py build            # 子工作流锁 sub_versions 里的发布版
  python -I -X utf8 bridge/build_cf_main.py build --draft    # 子工作流读草稿，改子工作流时调试用，不能拿去发布
  python -I -X utf8 bridge/build_cf_main.py run "海岸城附近人均100以内的火锅" [会话名]
  python -I -X utf8 bridge/build_cf_main.py release v0.0.11  # 按发布版重建、调试一次再发布

画布 JSON 另存到 canvases/cf_main.json，其中的 sidecar 令牌替换成占位符，不进仓库。
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
from build_subflows import RECALL_IN, SUBS, UNDERSTAND_OUT  # noqa: E402
from coze_client import Coze  # noqa: E402
from workflow_api import create_workflow, publish_workflow, save_workflow, summarize_process, test_run  # noqa: E402
from common.env import need  # noqa: E402

IDS_FILE = ROOT / "lifeagent_ids.json"
M = (fc.MODEL_TYPE, fc.MODEL_NAME)
LS = {"type": "string"}
PACK_OUT = ["answer", "stage", "slots", "cands", "shown", "q", "turn"]


def pack(node_id: str, title: str, pos, **vals) -> dict:
    """分支出口：回答 + 要写的会话状态（stage 为空就不写），给汇总节点。"""
    return cv.code(node_id, title, [cv.param(k, v) for k, v in vals.items()], [cv.out(k) for k in PACK_OUT], fc.CODE_PACK,
                   pos=pos)


def build(ids: dict, space_id: str, draft: bool = False) -> str:
    T = ids["tables_online"]
    W = ids["workflows"]
    V = ids.get("sub_versions", {})
    missing = [k for k in SUBS if k not in W or (not draft and k not in V)]
    if missing:
        raise SystemExit(f"子工作流没建或没发布：{missing}，先跑 build_subflows.py build / release")
    ver = (lambda k: "") if draft else (lambda k: V[k])  # noqa: E731
    S = cv.ENTRY_ID
    conv = cv.ref(S, "CONVERSATION_NAME")
    q = cv.ref(S, "USER_INPUT")
    n = {k: cv.new_id() for k in ["s1", "c0", "s2", "c6", "U", "x2", "x1", "pq", "RC", "l2", "c4", "pr", "pp", "k1", "k2",
                                  "k3", "CMP", "RT", "k4", "HOT", "pa", "pc", "lg", "agg", "MW", "dl", "ds"]}
    u = lambda k, vt="string": cv.ref(n["U"], k, vt, schema=LS if vt.startswith("list") else None)  # noqa: E731
    rc = lambda k, vt="string": cv.ref(n["RC"], k, vt)  # noqa: E731
    nodes = [cv.chat_entry()]
    nodes.append(cv.custom_sql(n["s1"], "读会话状态", T["session_state"], fc.SQL_STATE_READ, [cv.param("conv", conv)],
                               fc.STATE_SCHEMA, pos=(300, 0)))
    nodes.append(cv.code(n["c0"], "上轮槽位", [cv.param("rows", cv.ref(n["s1"], "outputList", "list<object>",
                                                                      schema={"type": "object", "schema": fc.STATE_SCHEMA}))],
                         [cv.out("prev_slots"), cv.out("turn", "integer"), cv.out("shown"), cv.out("shown_text"),
                          cv.out("stage"), cv.out("pending_q")], fc.CODE_PREV, pos=(600, 0)))
    nodes.append(cv.custom_sql(n["s2"], "读画像", T["user_memory"], fc.SQL_PROFILE, [], fc.PROF_SCHEMA, pos=(900, 0)))
    nodes.append(cv.code(n["c6"], "画像摘要", [cv.param("rows", cv.ref(n["s2"], "outputList", "list<object>",
                                                                      schema={"type": "object", "schema": fc.PROF_SCHEMA})),
                                              cv.param("conv", conv)],
                         [cv.out("profile"), cv.out("prof_lines"), cv.out("prof_json"), cv.out("active_json"),
                          cv.out("pending", "integer"), cv.out("off_ids")], fc.CODE_PROFILE, pos=(1200, 0)))
    c0 = lambda k, vt="string": cv.ref(n["c0"], k, vt)  # noqa: E731
    c6 = lambda k, vt="string": cv.ref(n["c6"], k, vt)  # noqa: E731
    nodes.append(cv.subflow(n["U"], "理解(WF-understand)", W["understand"], ver("understand"), space_id, [
        ("query", "string", q), ("prev_slots", "string", c0("prev_slots")), ("shown", "string", c0("shown")),
        ("shown_text", "string", c0("shown_text")), ("prof_lines", "string", c6("prof_lines")),
        ("prof_json", "string", c6("prof_json")), ("active_json", "string", c6("active_json")),
        ("pending", "integer", c6("pending", "integer")), ("conv", "string", conv), ("turn", "integer", c0("turn", "integer")),
        ("stage", "string", c0("stage")), ("pending_q", "string", c0("pending_q"))],
        [cv.out(x[0], x[2] if len(x) > 2 else "string") if not (len(x) > 2 and x[2].startswith("list"))
         else cv.out(x[0], "list", schema=LS) for x in UNDERSTAND_OUT], pos=(1500, 0)))
    route = u("route")
    ROUTES = ("rec", "pm", "cmp", "nav", "hot", "ask", "acc", "dl")  # 端口 true、true_1 … true_7，其余走 false
    nodes.append(cv.selector(n["x2"], "分流", [[(route, "==", cv.lit(r))] for r in ROUTES], pos=(1800, 0)))
    X = 2100
    # 找餐厅、选套餐：缺地点和品类就追问，否则召回 → 推荐生成 → 格式化
    nodes.append(cv.selector(n["x1"], "要追问吗", [[(u("need_clarify", "boolean"), "==", cv.lit(True, "boolean"))]],
                             pos=(X, -300)))
    # 回答里带选项按钮的卡片（question_ans）；写进会话状态的追问（q）只留文字，下一轮决策请求要读它
    nodes.append(pack(n["pq"], "追问", (X + 300, -500), answer=u("question_ans"), stage=cv.lit("collect"), slots=u("slots_json"),
                      cands=cv.lit("[]"), shown=cv.lit("[]"), q=u("question"), turn=u("turn")))
    nodes.append(cv.subflow(n["RC"], "召回(WF-recall)", W["recall"], ver("recall"), space_id,
                            [(k, "string", u(k)) for k in RECALL_IN],
                            [cv.out("candidates_json"), cv.out("cands_all"), cv.out("anchor"), cv.out("anchor_lng"),
                             cv.out("anchor_lat"), cv.out("relaxed"), cv.out("funnel"),
                             cv.out("count", "integer"), cv.out("with_evidence", "integer"), cv.out("geo_fallback", "boolean")],
                            pos=(X + 300, -250)))
    nodes.append(cv.llm_m(n["l2"], "推荐生成", "用户本轮输入：{{query}}\n已知条件：{{slots}}\n用户画像：{{profile}}\n锚点：{{anchor}}\n条件放宽：{{relaxed}}\n候选门店（只能从这里选）：{{candidates}}",
                          (fc.PROMPTS / "recommend.v9.md").read_text(encoding="utf-8"),
                          [cv.param("query", q), cv.param("slots", u("slots_eff")), cv.param("profile", u("profile_eff")),
                           cv.param("anchor", rc("anchor")), cv.param("candidates", rc("candidates_json")),
                           cv.param("relaxed", rc("relaxed"))],
                          [cv.out("output")], *M, json_mode=False, pos=(X + 600, -250)))
    nodes.append(cv.code(n["c4"], "格式化", [cv.param("picks", cv.ref(n["l2"], "output")),
                                             cv.param("candidates_json", rc("candidates_json")), cv.param("anchor", rc("anchor")),
                                             cv.param("relaxed", rc("relaxed")), cv.param("profile_note", u("profile_note")),
                                             cv.param("anchor_lng", rc("anchor_lng")), cv.param("anchor_lat", rc("anchor_lat")),
                                             cv.param("funnel", rc("funnel")), cv.param("conds_json", u("conds_json")),
                                             cv.param("cid", u("cid", "integer"))],
                         [cv.out("answer"), cv.out("shown_json")], fc.CODE_FORMAT, pos=(X + 900, -250)))
    nodes.append(pack(n["pr"], "推荐出口", (X + 1200, -250), answer=cv.ref(n["c4"], "answer"), stage=cv.lit("recommend"),
                      slots=u("slots_json"), cands=rc("cands_all"), shown=cv.ref(n["c4"], "shown_json"), q=cv.lit(""),
                      turn=u("turn")))
    # 偏好管理：理解子工作流里已算好回复（显式的记住/忘掉由 WF-memwrite 写表）
    nodes.append(pack(n["pp"], "偏好回复", (X, 0), answer=u("pm_reply")))
    # 对比评价 / 路线规划：认店 → WF-compare ｜ WF-route ｜ 认不出店就追问
    Y = 300
    nodes.append(cv.custom_sql(n["k1"], "读门店", T["shop"], fc.SQL_SHOPS, [], fc.SHOP_SCHEMA, pos=(X, Y)))
    nodes.append(cv.code(n["k2"], "认店", [
        cv.param("query", q), cv.param("route", route), cv.param("shown", c0("shown")),
        cv.param("rows", cv.ref(n["k1"], "outputList", "list<object>", schema={"type": "object", "schema": fc.SHOP_SCHEMA})),
        cv.param("prev_slots", c0("prev_slots")), cv.param("prof_json", c6("prof_json"))],
        [cv.out("go"), cv.out("targets_json"), cv.out("cand_items", "list", schema=LS), cv.out("ask"),
         cv.out("origin"), cv.out("to_name"), cv.out("to_lng"), cv.out("to_lat"), cv.out("pick_ids")], fc.CODE_PICK,
        pos=(X + 300, Y)))
    k2 = lambda k, vt="string": cv.ref(n["k2"], k, vt, schema=LS if vt.startswith("list") else None)  # noqa: E731
    go = k2("go")
    nodes.append(cv.selector(n["k3"], "对比还是路线", [[(go, "==", cv.lit("cmp"))], [(go, "==", cv.lit("nav"))],
                                                    [(go, "==", cv.lit("deal"))]], pos=(X + 600, Y)))
    # 问某一家的团购（10-22）：认店后直接列这家的团购，不动会话状态
    nodes.append(cv.custom_sql(n["ds"], "读团购", T["shop"], fc.SQL_DEALS, [cv.param("ids", k2("pick_ids"))], fc.DEAL_SCHEMA,
                               pos=(X + 900, Y + 125)))
    nodes.append(cv.code(n["dl"], "团购回答", [
        cv.param("rows", cv.ref(n["ds"], "outputList", "list<object>", schema={"type": "object", "schema": fc.DEAL_SCHEMA})),
        cv.param("ids", k2("pick_ids"))], [cv.out("answer")], fc.CODE_DEAL_ANS, pos=(X + 1200, Y + 125)))
    nodes.append(cv.subflow(n["CMP"], "对比(WF-compare)", W["compare"], ver("compare"), space_id,
                            [("query", "string", q), ("profile", "string", u("profile_eff")),
                             ("targets_json", "string", k2("targets_json")),
                             ("cand_items", "list<string>", k2("cand_items", "list<string>"))],
                            [cv.out("answer")], pos=(X + 900, Y - 100)))
    nodes.append(cv.subflow(n["RT"], "路线(WF-route)", W["route"], ver("route"), space_id,
                            [(k, "string", k2(k)) for k in ["to_lng", "to_lat", "to_name", "origin"]],
                            [cv.out("answer")], pos=(X + 900, Y + 50)))
    nodes.append(pack(n["k4"], "认店追问", (X + 900, Y + 200), answer=k2("ask")))
    # 看榜单
    nodes.append(cv.subflow(n["HOT"], "榜单(WF-hot)", W["hot"], ver("hot"), space_id, [("query", "string", q)],
                            [cv.out("answer")], pos=(X, Y + 400)))
    # 意图拿不准：问一句，记下已知槽位，下一轮接着用（stage=clarify 时不会再连着追问）
    nodes.append(pack(n["pa"], "意图澄清", (X, Y + 550), answer=u("clarify_ans"), stage=cv.lit("clarify"), slots=u("slots_json"),
                      cands=cv.lit("[]"), shown=c0("shown"), q=u("clarify"), turn=u("turn")))
    # 选定上轮某家（D1 accept 且指明第几家）：确认，上轮推荐收窄成这一家
    nodes.append(pack(n["pc"], "确认选择", (X, Y + 625), answer=u("acc_reply"), stage=cv.lit("accepted"), slots=u("slots_json"),
                      cands=cv.lit("[]"), shown=u("acc_shown"), q=cv.lit(""), turn=u("turn")))
    # FAQ、闲聊、其他：WF-faq 检索 KB-faq 后回答（v0.0.13 起，替换原来的通用回复）
    nodes.append(cv.subflow(n["lg"], "FAQ(WF-faq)", W["faq"], ver("faq"), space_id,
                            [("query", "string", q), ("cid", "integer", u("cid", "integer")),
                             ("shown_text", "string", c0("shown_text"))],
                            [cv.out("answer"), cv.out("n_hit", "integer"), cv.out("faq")], pos=(X, Y + 700)))
    # 汇总：每组取第一个非 nil；理解子工作流的 empty（恒为空串）放最后兜底，不写会话状态的分支 stage 就是空
    writers = [n["pr"], n["pq"], n["pa"], n["pc"]]
    groups = {"answer": ("string", [cv.ref(n["pr"], "answer"), cv.ref(n["pq"], "answer"), cv.ref(n["pa"], "answer"),
                                    cv.ref(n["pc"], "answer"),
                                    cv.ref(n["pp"], "answer"), cv.ref(n["CMP"], "answer"), cv.ref(n["RT"], "answer"),
                                    cv.ref(n["k4"], "answer"), cv.ref(n["dl"], "answer"), cv.ref(n["HOT"], "answer"),
                                    cv.ref(n["lg"], "answer"),
                                    u("empty")])}
    for k in PACK_OUT[1:]:
        groups[k] = ("string", [cv.ref(w, k) for w in writers] + [u("empty")])
    nodes.append(cv.aggregator(n["agg"], "汇总", groups, pos=(X + 1500, 0)))
    g = lambda k: cv.ref(n["agg"], k)  # noqa: E731
    nodes.append(cv.subflow(n["MW"], "写记忆(WF-memwrite)", W["memwrite"], ver("memwrite"), space_id,
                            [("conv", "string", conv)] + [(k, "string", g(k)) for k in PACK_OUT[1:]] +
                            [("ins_items", "list<string>", u("ins_items", "list<string>")),
                             ("del_items", "list<string>", u("del_items", "list<string>"))],
                            [cv.out("uid"), cv.out("stage")], pos=(X + 1800, 0)))
    # 不流式：流式模式下 LLM 节点偶发不发 node_end_streaming，执行挂在 running、对话流不收尾（2026-10-20 三次）
    nodes.append(cv.chat_exit(g("answer"), pos=(X + 2100, 0), streaming=False))

    e = cv.edge
    edges = [e(S, n["s1"]), e(n["s1"], n["c0"]), e(n["c0"], n["s2"]), e(n["s2"], n["c6"]), e(n["c6"], n["U"]),
             e(n["U"], n["x2"]),
             e(n["x2"], n["x1"], "true"), e(n["x2"], n["pp"], "true_1"), e(n["x2"], n["k1"], "true_2"),
             e(n["x2"], n["k1"], "true_3"), e(n["x2"], n["HOT"], "true_4"), e(n["x2"], n["pa"], "true_5"),
             e(n["x2"], n["pc"], "true_6"), e(n["x2"], n["k1"], "true_7"), e(n["x2"], n["lg"], "false"),
             e(n["x1"], n["pq"], "true"), e(n["x1"], n["RC"], "false"), e(n["RC"], n["l2"]), e(n["l2"], n["c4"]),
             e(n["c4"], n["pr"]),
             e(n["k1"], n["k2"]), e(n["k2"], n["k3"]), e(n["k3"], n["CMP"], "true"), e(n["k3"], n["RT"], "true_1"),
             e(n["k3"], n["ds"], "true_2"), e(n["ds"], n["dl"]), e(n["k3"], n["k4"], "false"),
             *[e(n[k], n["agg"]) for k in ["pr", "pq", "pp", "CMP", "RT", "k4", "dl", "HOT", "pa", "pc", "lg"]],
             e(n["agg"], n["MW"]), e(n["MW"], cv.EXIT_ID)]
    return cv.canvas(nodes, edges)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    draft = "--draft" in sys.argv
    cmd = args[0] if args else "build"
    c = Coze()
    c.login()
    ids = json.loads(IDS_FILE.read_text(encoding="utf-8"))
    if "cf_main" not in ids.setdefault("workflows", {}):
        # 从头搭时主干还不存在：建一个对话流（flow_mode 3），id 记进 lifeagent_ids.json
        ids["workflows"]["cf_main"] = create_workflow(c, "cf_main", "主干对话流：理解、分流、找店、对比、路线、榜单、FAQ、记忆", flow_mode=3)
        IDS_FILE.write_text(json.dumps(ids, ensure_ascii=False, indent=1), encoding="utf-8")
        print("建主干对话流", ids["workflows"]["cf_main"])
    wid = ids["workflows"]["cf_main"]

    def save(draft_subs: bool):
        schema = build(ids, c.space_id(), draft=draft_subs)
        r = save_workflow(c, wid, schema)
        print("保存:", r.get("code"), r.get("msg"), "（子工作流读草稿）" if draft_subs else f"（子工作流 {ids.get('sub_versions')}）")
        (ROOT / "canvases").mkdir(exist_ok=True)
        (ROOT / "canvases" / "cf_main.json").write_text(schema.replace(need("SIDECAR_TOKEN"), "<SIDECAR_TOKEN>"),
                                                         encoding="utf-8")

    if cmd == "build":
        save(draft)
    elif cmd == "run":
        d = test_run(c, wid, {"USER_INPUT": args[1], "CONVERSATION_NAME": args[2] if len(args) > 2 else "dev-1"},
                     bot_id=ids["bot_id"], timeout_s=300)
        print(summarize_process(d)[-6000:])
    elif cmd == "release":
        # 发布版必须锁子工作流版本；Coze 要求草稿先成功调试一次才能发布
        save(False)
        d = test_run(c, wid, {"USER_INPUT": "海岸城附近的火锅", "CONVERSATION_NAME": "release-check"},
                     bot_id=ids["bot_id"], timeout_s=300)
        if d.get("executeStatus") != 2:
            print(summarize_process(d)[-2000:])
            raise SystemExit("调试未通过，不发布")
        print(publish_workflow(c, wid, version=args[1], desc="CF-main"))


if __name__ == "__main__":
    main()
