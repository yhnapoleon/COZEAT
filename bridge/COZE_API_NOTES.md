# Coze Studio 内部接口速查（commit fefb05ff，2026-10-19 对照源码核实）

session 接口统一 POST + JSON，鉴权用登录后的 `session_key` cookie（`coze_client.Coze` 已处理，会话约 20 分钟过期会自动重登）。

| 用途 | 接口 | 要点 |
|---|---|---|
| 新建智能体 | `/api/draftbot/create` | 必填 space_id、name（≤50 字）、icon_uri（`default_icon/default_agent_icon.png`）；返回 `data.bot_id`；系统里至少要有 1 个在线模型 |
| 对话流模式 | `/api/playground_api/draftbot/update_draft_bot_info` | `bot_info:{bot_id, bot_mode:2, layout_info:{workflow_id, plugin_id:同 workflow_id}}`；0 单 Agent、1 多 Agent、2 对话流；运行时读已发布版本，对话流必须先 publish |
| 建对话流 | `/api/workflow_api/create` | `flow_mode=3`；默认开始节点输出 USER_INPUT（必填）、CONVERSATION_NAME（默认 "Default"） |
| 调试对话流 | `/api/workflow_api/test_run` | `input:{"USER_INPUT":"..","CONVERSATION_NAME":"Default"}`，值都是字符串；不建会话、不带历史 |
| 会话内调试 | `/api/conversation/chat` | 需要 bot_id、conversation_id、query |
| 建表 | `/api/memory/database/add` | 返回 `database_info.id`（线上 id）和 `database_info.draft_id`；rw_mode 1 按用户隔离、2 只读、3 共享读写 |
| 绑表 | `/api/memory/database/bind_to_bot` | `{database_id:<draft_id>, bot_id}` |
| 工作流数据库节点 | | `databaseInfoID` 用线上 id，调试时自动映射到草稿表 |
| 建插件 | `/api/plugin_api/register` | `{space_id, ai_plugin:<manifest JSON 字符串>, openapi:<YAML 字符串>}`，返回 `data.plugin_id`；manifest 必填 schema_version v1、name_for_model、name_for_human、description_for_model、description_for_human、api:{type:openapi}、auth；鉴权 `{type:"service_http", sub_type:"token/api_key", location:"header", key:"Authorization", service_token:"Bearer xxx"}`；openapi 要有 info.title/description、恰好 1 个 servers、每个 operation 有 operationId 和 summary |
| 调试插件工具 | `/api/plugin_api/debug_api` | `{plugin_id, api_id, parameters:<json 字符串>, operation:1}`；发布前每个工具都要调过 |
| 发布插件 | `/api/plugin_api/publish_plugin` | `{plugin_id, version_name:"v1.0.0", version_desc, privacy_status:false}` |
| 插件工具 id | `/api/plugin_api/get_plugin_apis` | `{plugin_id}` → `api_info[].api_id/name` |
| 插件节点参数 | | apiParam：pluginID、apiID、apiName（operationId）、pluginName、pluginVersion（"0" 草稿、"" 最新线上、"v1.0.0" 指定） |
| 注册模型 | `/api/admin/config/model/create` | `{model_class:1, model_name:显示名, connection:{base_conn_info:{base_url, api_key, model:真实模型名, thinking_type:0}, openai:{by_azure:false}}, enable_base64_url:false}`；创建前会真调一次；需要管理员 |
| 管理员 | `/api/admin/config/basic/save` | 基础配置 `admin_emails`，为空时回退 env `ALLOW_REGISTRATION_EMAIL` |
| 模型 id | `/api/bot/get_type_list` | `{model:true}` → `data.model_list[].model_type`，就是 LLM 节点的 modelType |
| OpenAPI 调对话流 | `/v1/workflows/chat` | Bearer PAT；`workflow_id`、`additional_messages:[{role:"user",content,content_type:"text"}]`、`parameters` 是 JSON 字符串、`ext:{user_id}`；返回 SSE |
| 建 PAT | `/api/permission_api/pat/create_personal_access_token_and_permission` | `{name, duration_day:"30"}`；返回 `data.token` |
| OpenAPI 跑工作流 | `/v1/workflow/run` | Bearer PAT；`{workflow_id, parameters:<JSON 字符串>, ext:{user_id}}`；只跑最新发布版；同步返回 `data`（结束节点输出的 JSON 字符串） |

## 运行时行为（2026-10-20 对照源码和实测）

- **按用户隔离的表**：rw_mode 1 的表有隐藏列 `bstudio_connector_uid`。数据库节点的用户是执行配置里的 ConnectorUID：OpenAPI 调用时等于 `ext.user_id`，调试运行时等于当前账号 id（`domain/workflow/internal/nodes/database/common.go` getExecUserID）。SELECT/UPDATE/DELETE 自动追加 `bstudio_connector_uid = 用户`，INSERT 自动填它。自定义 SQL 里可以直接 `SELECT bstudio_connector_uid AS u`，`id` 列对应 bstudio_id，能 SELECT 和用在 WHERE。
- **自定义 SQL 参数**：带引号的 `'{{x}}'` 会被换成 `?` 绑定参数（customsql.go 的 singleQuotesStringRegexp），所以值里出现单引号、UNION、FROM 都不会触发关键字黑名单；黑名单只查 SQL 模板本身。多行 INSERT 也支持。
- **并行分支**：一个节点连出多条边就并行执行；有多条入边的节点等所有前驱都完成才执行（la_partest 实测：慢节点 3 秒、汇合节点等齐两边）。意图节点的所有分支端口可以连到同一个节点。批处理节点对空列表正常结束。
- **流式模式会挂**：结束节点 `streamingOutput: true`，且有文本输出的 LLM 节点能沿引用流到结束节点时，整条工作流按流式执行（workflow_schema.go doRequireStreaming）。这种模式下 LLM 节点偶发收不到 `node_end_streaming`，节点停在 running，执行状态一直是 1，对话流的 SSE 不收尾，客户端读超时。10-20 有 3 次（约 35 次里），两次在 JSON 输出的槽位提取，一次在文本输出的记忆抽取。CF-main 从 v0.0.8 起结束节点不流式，改后连跑 24 轮（含 5 次完整推荐）没有再挂；OpenAPI 对话照样拿到完整回答。排查用 coze-mysql 的 `opencoze.workflow_execution`（status 1 = 运行中）和 `opencoze.node_execution`。
- **JSON 输出模式**：Claude 偶尔在 JSON 后面接一段话（"Wait, I should…"），节点报 `parse LLM structured output failed`。LLM 节点一律用文本输出，后接代码节点用正则取 `{...}` 再解析。
- **代码节点**：沙箱时钟是 UTC，要北京时间用 `utcnow() + 8h`。
- **HTTP 节点**：实际 10 秒超时（node_execution 里 duration 10003 ms、`node timeout`），画布里 settingOnError.timeoutMs 和 setting.timeout 设 30 都没用。慢接口要在 sidecar 里并发或缓存。
- **HTTP 节点的超时是全局的**（2026-10-20 读源码 + 实测）：`httprequester/http_requester.go` 的 `Config.Build` 里 `client := http.DefaultClient; client.Timeout = c.Timeout`，改的是进程级共享客户端，所以一次执行里所有 HTTP 节点用的是最后构建的那个节点的 `setting.timeout`。当时 WF-memwrite 的"通知整理"设 5 秒，`/decide` 跑到 5.15 秒就被掐断。本项目所有 HTTP 节点统一 10 秒（`canvas.HTTP_TIMEOUT_S`）。
- **HTTP 节点重试会发空请求体**：重试循环复用同一个 `http.Request`，请求体（`strings.NewReader`）第一次已经读完，第二次发出去是空的，FastAPI 回 422 `missing`。POST 节点 `setting.retryTimes` 设 0；GET 没有请求体，可以重试。
- **HTTP JSON 请求体**：`bodyType: "JSON"`、`bodyData.json` 是模板，`{{block_output_<节点id>.<字段>}}` 里的字符串变量原样插入、不加引号（`nodes/utils.go` TemplateRender），所以可以在代码节点里拼好整个 JSON 再引用。
- **子工作流节点**（type 9）：`inputs.workflowId`、`workflowVersion`、`spaceId`、`inputParameters`，`outputs` 和子工作流结束节点（returnVariables）的变量一致。`workflowVersion` 为空时运行时读子工作流**草稿**，填版本号读那个发布版（`canvas/adaptor/to_schema.go` toSubWorkflowNodeSchema），发布主干时不会自动锁版本，要自己填。子工作流里的数据库节点用根执行的 `ExeCfg`（调试/线上模式和 ConnectorUID），按用户隔离照常生效。对话流可以调普通工作流（flow_mode 0）。父执行的 nodeResults 里只看得到子工作流节点本身的输入输出；子执行在 `workflow_execution` 里另起一行（`root_execution_id` 是根执行、`parent_node_id` 是父画布里的节点 id），内部节点挂在这个子执行 id 下的 `node_execution`。
- **意图节点的极速模式**（`mode: "top_speed"`）：系统提示词要求模型只回一个数字，代码直接 `strconv.ParseInt(content)`（`nodes/intentdetector/intent_detector.go` parseToNodeOut），多一个空格、换行或解释就报 `invalid syntax`，整个工作流失败。Claude 一直回纯数字所以没暴露，10-20 换 DeepSeek V4.1 Flash 后每次都挂。改用标准模式（mode 写别的值，模型回 JSON，取第一对花括号）并给节点配 `inputs.settingOnError`（processType 2 + dataOnErr），出错时返回默认分类继续走。`settingOnError` 对所有节点类型通用（`canvas/adaptor/to_schema.go` toExceptionConfig）。
- **变量聚合**：每组取第一个**非 nil** 的值，空字符串也算有值。没执行的分支是 nil；要兜底就在组末尾放一个一定执行的节点的空串输出。
- **表的列数**（2026-10-21 读源码 + 实测）：官方说一张表最多 20 列，源码里只有 Excel 导入（`domain/memory/database/internal/sheet/sheet.go` CheckSheetIsValid）查这个数，改表接口 `/api/memory/database/update` 不查。改表按 `alterId` 认列：带了的列保留或修改，`alterId` 为 0 的是新列，没带的列会被删掉（`physicaltable.UpdateFieldInfo`），所以要先 `get_by_id` 把现有列原样带上。shop 表用 `bridge/alter_table.py` 加到 21 列，草稿表和线上表的物理表都加上了列，原有数据不动。
- **自定义 SQL 里的 REGEXP**：`col REGEXP '{{re}}'` 可用（绑定参数，MySQL 8 的 ICU 正则，中文正常，按表的排序规则不区分大小写）。"全部命中"用 `'^'`（空串也命中），"永不命中"用 `'a^'`，这两个实测过；空模式没试。别名不能用 MySQL 保留字（`AS empty` 报 1064）。
- **update_records 批量插入按第一条记录建列**（2026-10-21 实测）：`record_data_add` 一批里，第一条记录没有某个键（`load_shops` 对空值不传这个键），同批其他记录的这个字段全部写成 NULL。shop 表第 6 批第一条人均为空，同批 24 家的 cost 因此丢了（是 load_shops 的老问题，从哪次写库开始的没查）。`load_shops.py` 已改成按字段组合分批；别的地方批量写表也要保证一批里字段一致。
