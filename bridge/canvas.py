"""Helpers that build Coze Studio workflow canvas JSON.

Schema learned from backend/domain/workflow/internal/canvas/examples/*.json:
  {nodes:[{id,type,meta:{position},data:{nodeMeta,inputs,outputs}}], edges:[{sourceNodeID,targetNodeID,sourcePortID}], versions}
Node type numbers: 1 Entry, 2 Exit, 3 LLM, 5 Code, 8 Selector, 9 Selector(cond)?, 12 Database, 15 Text, 18 QA, ...
Only the node kinds we need are modelled here; add more as the radar grows.
"""
from __future__ import annotations

import itertools
import json

ENTRY_ID = "100001"
EXIT_ID = "900001"
_ids = itertools.count(200001)

# variable type codes used in rawMeta.type
VT = {"string": 1, "integer": 2, "boolean": 3, "float": 4, "object": 6, "list<string>": 99, "list<object>": 103}

# From .env: MODEL_ID_0 / displayed by /api/bot/get_type_list
MODEL_TYPE = 100001
MODEL_NAME = "gpt-5-2025-08-07"


def new_id() -> str:
    return str(next(_ids))


def lit(value, vtype: str = "string") -> dict:
    return {"type": vtype, "value": {"content": value, "rawMeta": {"type": VT[vtype]}, "type": "literal"}}


def ref(block_id: str, name: str, vtype: str = "string", schema: dict | None = None) -> dict:
    d = {"type": vtype.split("<")[0] if vtype.startswith("list") else vtype,
         "value": {"content": {"blockID": block_id, "name": name, "source": "block-output"},
                   "rawMeta": {"type": VT[vtype]}, "type": "ref"}}
    if vtype.startswith("list"):
        d["type"] = "list"
        d["schema"] = schema or {"type": "string"}
    return d


def param(name: str, inp: dict) -> dict:
    return {"name": name, "input": inp}


def out(name: str, vtype: str = "string", required: bool = False, schema: dict | None = None) -> dict:
    d = {"name": name, "type": vtype, "required": required}
    if schema:
        d["schema"] = schema
    return d


def entry(outputs: list[dict], pos=(0, 0)) -> dict:
    return {"id": ENTRY_ID, "type": "1", "meta": {"position": {"x": pos[0], "y": pos[1]}},
            "data": {"nodeMeta": {"title": "开始"}, "outputs": outputs, "trigger_parameters": []}}


def exit_node(params: list[dict], pos=(1200, 0)) -> dict:
    return {"id": EXIT_ID, "type": "2", "meta": {"position": {"x": pos[0], "y": pos[1]}},
            "data": {"nodeMeta": {"title": "结束"},
                     "inputs": {"inputParameters": params, "terminatePlan": "returnVariables"}}}


def llm(node_id: str, title: str, prompt: str, system_prompt: str, params: list[dict],
        outputs: list[dict], json_mode: bool = True, max_tokens: int = 4096, pos=(600, 0)) -> dict:
    llm_param = [
        param("modelType", lit(str(MODEL_TYPE), "integer")),
        param("modleName", lit(MODEL_NAME)),
        param("generationDiversity", lit("balance")),
        param("temperature", lit("1", "float")),
        param("maxTokens", lit(str(max_tokens), "integer")),
        param("responseFormat", lit("2" if json_mode else "0", "integer")),
        param("prompt", lit(prompt)),
        param("enableChatHistory", lit(False, "boolean")),
        param("chatHistoryRound", lit("3", "integer")),
        param("systemPrompt", lit(system_prompt)),
    ]
    return {"id": node_id, "type": "3", "meta": {"position": {"x": pos[0], "y": pos[1]}},
            "data": {"nodeMeta": {"title": title, "subTitle": "大模型", "mainColor": "#5C62FF"},
                     "inputs": {"inputParameters": params, "llmParam": llm_param,
                                "settingOnError": {"processType": 1, "retryTimes": 0, "timeoutMs": 180000}},
                     "outputs": outputs}}


def code(node_id: str, title: str, params: list[dict], outputs: list[dict], source: str, pos=(600, 0)) -> dict:
    """Python code node. `source` must define `async def main(args: Args) -> Output`."""
    return {"id": node_id, "type": "5", "meta": {"position": {"x": pos[0], "y": pos[1]}},
            "data": {"nodeMeta": {"title": title, "subTitle": "代码", "mainColor": "#00B2B2"},
                     "inputs": {"inputParameters": params, "code": source, "language": 3,
                                "settingOnError": {"processType": 1, "retryTimes": 0, "timeoutMs": 60000}},
                     "outputs": outputs}}


def http_get(node_id: str, title: str, url_template: str, timeout_s: int = 60, retries: int = 1, pos=(600, 0),
             default_on_error: dict | None = None) -> dict:
    """GET request. url_template may contain {{block_output_<id>.<name>}} placeholders.
    default_on_error: if given, the node returns this output instead of failing (processType 2)."""
    on_err = {"processType": 1, "retryTimes": 0, "timeoutMs": timeout_s * 1000}
    if default_on_error is not None:
        on_err = {"switch": True, "processType": 2, "retryTimes": 0, "timeoutMs": timeout_s * 1000,
                  "dataOnErr": json.dumps(default_on_error, ensure_ascii=False)}
    node = {"id": node_id, "type": "45", "meta": {"position": {"x": pos[0], "y": pos[1]}},
            "data": {"nodeMeta": {"title": title, "subTitle": "HTTP 请求", "mainColor": "#3071F2"},
                     "inputParameters": [],
                     "inputs": {"apiInfo": {"method": "GET", "url": url_template},
                                "auth": {"authOpen": False, "authType": "CUSTOM_AUTH",
                                         "authData": {"bearerTokenData": [], "customData": {"addTo": "query", "data": []}}},
                                "body": {"bodyType": "EMPTY", "bodyData": {}},
                                "headers": [{"name": "User-Agent", "input": lit("lifeagent/0.1 (local coze studio)")}],
                                "params": [],
                                "setting": {"retryTimes": retries, "timeout": timeout_s},
                                "settingOnError": on_err},
                     "outputs": [out("body", "string"), out("statusCode", "integer"), out("headers", "string")]}}
    node["data"]["settingOnError"] = on_err
    return node


def batch(node_id: str, title: str, items_ref: dict, inner_nodes: list[dict], inner_edges: list[dict],
          outputs: list[dict], batch_size: int = 50, concurrent: int = 5, pos=(600, 0)) -> dict:
    """Batch over a list. Inner nodes read the current item via ref(node_id, 'items').
    `outputs` entries are {"name":..., "input": ref(<inner node>, <name>, list<...>)}."""
    return {"id": node_id, "type": "28", "meta": {"position": {"x": pos[0], "y": pos[1]}},
            "data": {"nodeMeta": {"title": title, "subTitle": "批处理", "mainColor": "#00B2B2"},
                     "inputs": {"batchSize": {"type": "integer", "value": {"content": str(batch_size), "type": "literal"}},
                                "concurrentSize": {"type": "integer", "value": {"content": str(concurrent), "type": "literal"}},
                                "inputParameters": [param("items", items_ref)]},
                     "outputs": outputs},
            "blocks": inner_nodes, "edges": inner_edges}


def batch_edges(batch_id: str, chain: list[str]) -> list[dict]:
    """Inner edges for a linear chain of inner node ids inside a batch."""
    e = [{"sourceNodeID": batch_id, "targetNodeID": chain[0], "sourcePortID": "batch-function-inline-output"}]
    for a, b in zip(chain, chain[1:]):
        e.append({"sourceNodeID": a, "targetNodeID": b, "sourcePortID": ""})
    e.append({"sourceNodeID": chain[-1], "targetNodeID": batch_id, "sourcePortID": "", "targetPortID": "batch-function-inline-input"})
    return e


OP = {"==": 1, "!=": 2, "len>": 3, "len>=": 4, "len<": 5, "len<=": 6, "contains": 7, "!contains": 8,
      "empty": 9, "!empty": 10, "true": 11, "false": 12, ">": 13, ">=": 14, "<": 15, "<=": 16}


def selector(node_id: str, title: str, branches: list[list[tuple]], pos=(600, 0)) -> dict:
    """branches: list of branches; each branch is a list of (left_input, op, right_input) ANDed together.
    Output ports: 'true', 'true_1', ... per branch, then 'false'."""
    bs = []
    for conds in branches:
        bs.append({"condition": {"logic": 2, "conditions": [
            {"left": {"input": l}, "operator": OP[o], "right": {"input": r}} for l, o, r in conds]}})
    return {"id": node_id, "type": "8", "meta": {"position": {"x": pos[0], "y": pos[1]}},
            "data": {"nodeMeta": {"title": title, "subTitle": "选择器", "mainColor": "#00B2B2"},
                     "inputs": {"branches": bs}}}


def custom_sql(node_id: str, title: str, database_id: str, sql: str, params: list[dict], row_schema: list[dict], pos=(600, 0)) -> dict:
    """DatabaseCustomSQL. Use '{{param}}' (quoted) placeholders in sql."""
    return {"id": node_id, "type": "12", "meta": {"position": {"x": pos[0], "y": pos[1]}},
            "data": {"nodeMeta": {"title": title, "subTitle": "SQL 自定义", "mainColor": "#FF811A"},
                     "inputs": {"databaseInfoList": [{"databaseInfoID": database_id}], "inputParameters": params, "sql": sql},
                     "outputs": [{"type": "list", "name": "outputList", "schema": {"type": "object", "schema": row_schema}},
                                 out("rowNum", "integer")]}}


def knowledge_retriever(node_id: str, title: str, dataset_ids: list[str], query_input: dict, top_k: int = 5,
                        min_score: float = 0.3, pos=(600, 0), strategy: int = 1) -> dict:
    """strategy：0 纯语义、1 混合、20 全文。混合模式的 RRF 取最大值、同分随机（E2，2026-10-21），开放式检索用 0。"""
    return {"id": node_id, "type": "6", "meta": {"position": {"x": pos[0], "y": pos[1]}},
            "data": {"nodeMeta": {"title": title, "subTitle": "知识库检索", "mainColor": "#FF811A"},
                     "inputs": {"datasetParam": [
                         {"name": "datasetList", "input": {"type": "list", "schema": {"type": "string"}, "value": {"type": "literal", "content": dataset_ids}}},
                         {"name": "topK", "input": {"type": "integer", "value": {"type": "literal", "content": top_k}}},
                         {"name": "useRerank", "input": {"type": "boolean", "value": {"type": "literal", "content": False, "rawMeta": {"type": 3}}}},
                         {"name": "useRewrite", "input": {"type": "boolean", "value": {"type": "literal", "content": False, "rawMeta": {"type": 3}}}},
                         {"name": "isPersonalOnly", "input": {"type": "boolean", "value": {"type": "literal", "content": False, "rawMeta": {"type": 3}}}},
                         {"name": "minScore", "input": {"type": "float", "value": {"type": "literal", "content": min_score}}},
                         {"name": "strategy", "input": {"type": "integer", "value": {"type": "literal", "content": strategy}}}],
                         "inputParameters": [param("Query", query_input)]},
                     "outputs": [{"type": "list", "name": "outputList", "schema": {"type": "object", "schema": [out("output", "string")]}}]}}


def edge(src: str, dst: str, port: str = "") -> dict:
    return {"sourceNodeID": src, "targetNodeID": dst, "sourcePortID": port}


def canvas(nodes: list[dict], edges: list[dict]) -> str:
    return json.dumps({"nodes": nodes, "edges": edges, "versions": {"loop": "v2"}}, ensure_ascii=False)


# ---------------- 本项目补充的节点（对话流、意图识别、聚合、带头部的 HTTP） ----------------

def chat_entry(pos=(0, 0)) -> dict:
    """对话流开始节点：USER_INPUT + CONVERSATION_NAME（examples/chatflow/llm_chat_with_history.json）。"""
    return {"id": ENTRY_ID, "type": "1", "meta": {"position": {"x": pos[0], "y": pos[1]}},
            "data": {"nodeMeta": {"title": "开始"},
                     "outputs": [{"name": "USER_INPUT", "required": False, "type": "string"},
                                 {"name": "CONVERSATION_NAME", "required": False, "type": "string",
                                  "defaultValue": "Default", "description": "本次请求绑定的会话"}],
                     "trigger_parameters": []}}


def chat_exit(answer_ref: dict, pos=(2400, 0), streaming: bool = True) -> dict:
    """对话流结束节点：输出回答。streaming=True 时整条工作流按流式执行（有文本 LLM 节点能流到这里时）。"""
    return {"id": EXIT_ID, "type": "2", "meta": {"position": {"x": pos[0], "y": pos[1]}},
            "data": {"nodeMeta": {"title": "结束"},
                     "inputs": {"inputParameters": [param("output", answer_ref)],
                                "content": {"type": "string", "value": {"type": "literal", "content": "{{output}}"}},
                                "streamingOutput": streaming, "terminatePlan": "useAnswerContent"}}}


def intent(node_id: str, title: str, query_ref: dict, intents: list[str], model_type: int, model_name: str,
           mode: str = "top_speed", pos=(400, 0), default_on_error: dict | None = None) -> dict:
    """意图识别。输出 classificationId（0 为其他，意图从 1 开始）；出边端口 branch_0.. 与 default。
    mode="top_speed"（极速）要求模型只输出一个数字，代码直接 strconv.ParseInt，多一个空格或换行就报 invalid syntax
    （intentdetector/intent_detector.go parseToNodeOut；DeepSeek 10-20 实测会挂）；其他值是标准模式，输出 JSON、取第一对花括号。
    default_on_error：出错时返回这个输出继续往下走，不让整个工作流失败。"""
    node = {"id": node_id, "type": "22", "meta": {"position": {"x": pos[0], "y": pos[1]}},
            "data": {"nodeMeta": {"title": title, "subTitle": "意图识别", "mainColor": "#00B2B2"},
                     "outputs": [{"type": "integer", "name": "classificationId"}, {"type": "string", "name": "reason"}],
                     "inputs": {"chatHistorySetting": {"enableChatHistory": False, "chatHistoryRound": 3},
                                "inputParameters": [param("query", query_ref)],
                                "llmParam": {"generationDiversity": "balance", "maxTokens": 4096, "modelName": model_name,
                                             "modelType": model_type,
                                             "prompt": {"type": "string", "value": {"type": "literal", "content": "{{query}}"}},
                                             "responseFormat": 2, "temperature": 1,
                                             "systemPrompt": {"type": "string", "value": {"type": "literal", "content": ""}},
                                             "enableChatHistory": False, "chatHistoryRound": 3},
                                "intents": [{"name": x} for x in intents], "mode": mode}}}
    if default_on_error is not None:
        node["data"]["inputs"]["settingOnError"] = {"switch": True, "processType": 2, "retryTimes": 0, "timeoutMs": 60000,
                                                    "dataOnErr": json.dumps(default_on_error, ensure_ascii=False)}
    return node


def aggregator(node_id: str, title: str, groups: dict, pos=(2200, 0)) -> dict:
    """变量聚合：每组取第一个非空值。groups = {组名: (类型, [ref...])}。"""
    merge = [{"name": g, "variables": refs} for g, (_, refs) in groups.items()]
    return {"id": node_id, "type": "32", "meta": {"position": {"x": pos[0], "y": pos[1]}},
            "data": {"nodeMeta": {"title": title, "subTitle": "变量聚合", "mainColor": "#00B2B2"},
                     "inputs": {"mergeGroups": merge},
                     "outputs": [{"name": g, "type": t} for g, (t, _) in groups.items()]}}


# HTTP 节点构建时把自己的超时写进全局 http.DefaultClient（httprequester/http_requester.go Config.Build），
# 一次执行里所有 HTTP 节点实际用的是最后构建的那个值，所以本项目所有 HTTP 节点统一用这一个超时
HTTP_TIMEOUT_S = 10


def http_get_h(node_id: str, title: str, url_template: str, headers: dict, timeout_s: int = HTTP_TIMEOUT_S, pos=(600, 0),
               default_on_error: dict | None = None, retries: int = 1) -> dict:
    """GET，可带自定义请求头（值为字面量）。"""
    node = http_get(node_id, title, url_template, timeout_s=timeout_s, retries=retries, pos=pos, default_on_error=default_on_error)
    node["data"]["inputs"]["headers"] += [{"name": k, "input": lit(v)} for k, v in headers.items()]
    return node


def http_post_json(node_id: str, title: str, url: str, headers: dict, body_tpl: str, timeout_s: int = HTTP_TIMEOUT_S,
                   pos=(600, 0), default_on_error: dict | None = None) -> dict:
    """POST JSON。body_tpl 里的 {{block_output_<id>.<name>}} 按原样替换，字符串变量不加引号（nodes/utils.go TemplateRender），
    所以可以先在代码节点里拼好整个请求体，再写 body_tpl="{{block_output_<id>.body}}"。
    不重试：重试复用同一个 http.Request，请求体第一次已经读空，第二次发出去是空的（sidecar 回 422，10-20 实测）。"""
    node = http_get_h(node_id, title, url, headers, timeout_s=timeout_s, pos=pos, default_on_error=default_on_error, retries=0)
    node["data"]["inputs"]["apiInfo"]["method"] = "POST"
    node["data"]["inputs"]["body"] = {"bodyType": "JSON", "bodyData": {"json": body_tpl}}
    return node


def subflow(node_id: str, title: str, workflow_id: str, version: str, space_id: str, inputs: list[tuple],
            outputs: list[dict], pos=(600, 0)) -> dict:
    """子工作流节点（type 9）。version 为空时运行时读子工作流的草稿，填版本号读那个发布版
    （canvas/adaptor/to_schema.go toSubWorkflowNodeSchema），所以发布版的主干要锁版本号。
    子工作流里的数据库节点沿用根执行的调试/线上模式和用户号（nodes/database/common.go 读 RootCtx.ExeCfg）。
    inputs = [(参数名, 类型, 输入值)]，类型写法同 VT 的键；outputs 要和子工作流结束节点的变量一致。"""
    defs = []
    for name, vt, _ in inputs:
        d = {"name": name, "type": "list" if vt.startswith("list") else vt, "required": False, "input": {}}
        if vt.startswith("list"):
            d["schema"] = {"type": vt[5:-1]} if vt != "list<object>" else {"type": "object"}
        defs.append(d)
    return {"id": node_id, "type": "9", "meta": {"position": {"x": pos[0], "y": pos[1]}},
            "data": {"nodeMeta": {"title": title, "subTitle": "工作流", "mainColor": "#00B2B2"},
                     "inputs": {"inputDefs": defs, "inputParameters": [param(n, v) for n, _, v in inputs],
                                "settingOnError": {}, "spaceId": space_id, "type": 0, "workflowId": workflow_id,
                                "workflowVersion": version},
                     "outputs": outputs}}


def llm_m(node_id: str, title: str, prompt: str, system_prompt: str, params: list[dict], outputs: list[dict],
          model_type: int, model_name: str, json_mode: bool = True, max_tokens: int = 8192, pos=(600, 0)) -> dict:
    """llm() 的变体：显式指定模型。"""
    node = llm(node_id, title, prompt, system_prompt, params, outputs, json_mode=json_mode, max_tokens=max_tokens, pos=pos)
    for p in node["data"]["inputs"]["llmParam"]:
        if p["name"] == "modelType":
            p["input"] = lit(str(model_type), "integer")
        if p["name"] == "modleName":
            p["input"] = lit(model_name)
    return node
