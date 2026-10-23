# 本机 Coze Studio（commit fefb05ff，2026-07-29）支持面核查（子代理报告原文，2026-10-17）

路径相对仓库根 `Remote_Intern/coze-studio`。结论：知识库三种类型都支持，切分可配，但没有召回测试；没有长期记忆；内置插件有高德和博查，没有抖音；没有 MCP 节点；发布渠道里没有抖音。

## 1. 知识库
- 类型三种：文本(0)、表格(1)、图片(2)。文档类 pdf/txt/doc/docx/md，表格类 csv/xlsx/json，图片类 jpg/jpeg/png。`backend/crossdomain/knowledge/model/knowledge.go`、`backend/infra/document/parser/manager.go`。
- 切分三种模式：自动(default)、自定义(custom)、按层级(leveled)。
  - 自定义可配：chunk_size、separator、overlap、trim_space、trim_url_and_email。
  - 按层级可配：max_depth、save_title。
  - 解析选项：快速/精准解析、抽图、抽表、OCR、页过滤；表格可设 sheet、表头行、数据起始行；图片可选模型自动标注或人工标注。
  - `backend/domain/knowledge/entity/strategy.go`。
- 召回测试：没有。知识库路由和 IDL 里都没有 retrieval/hit test 接口，前端也无"召回测试"页面。只有 IDL 残留枚举 `SliceSource.hit_test`。`backend/api/router/coze/api.go`、`idl/data/knowledge/knowledge_svc.thrift`。
- 检索方式：语义、全文、混合三种，可设 TopK（默认 3）、MinScore（默认 0.5），查询改写和 NL2SQL 开关。rerank 由 `RERANK_TYPE` 配置，rrf（默认）或 vikingdb，`backend/infra/document/rerank/impl/`。`retrieve.go` 每次检索都走 rerank，`EnableRerank` 开关没被读取。表格知识库：列有 `Indexing` 标记，只有勾选列参与匹配；查表靠 NL2SQL。无显式"按列过滤"参数。`backend/domain/knowledge/service/retrieve.go`。

## 2. 记忆
- 没有长期记忆。IDL 里有 TimeCapsuleMode 枚举、显示配置里有 LongTermMemoryTabStatus，后端无记忆总结实现。可用的只有变量和数据库，`backend/domain/memory/`（`variables`、`database`）。
- 变量按用户隔离：实例键 BizType|BizID|ConnectorUID|ConnectorID，`backend/domain/memory/variables/entity/variable_instance.go`。
- 数据库"受限读写"模式自动按 uid 过滤，`backend/domain/memory/database/service/database_impl.go:1090`。
- 多轮历史轮数：智能体 `ShortMemoryPolicy.HistoryRound`，新建默认 3，未配置兜底 100（`backend/application/singleagent/create.go` 等）；工作流大模型节点 `chatHistoryRound`（`backend/domain/workflow/internal/nodes/llm/llm.go`）。

## 3. 插件
- 内置清单 `backend/conf/plugin/pluginproduct/plugin_meta.yaml`，18 个：文库搜索、博查搜索、Wolfram Alpha、创客贴、搜狐热闻、图片压缩、什么值得买、板栗看板、天眼查、飞书 8 个、高德地图。
- 高德地图 9 个接口：地理编码、逆地理编码、IP 定位、各类路线规划、周边搜索（`gaode_map.yaml`）。博查 `bocha_search.yaml`。
- 无百度地图、腾讯位置、Bing、抖音。
- 自定义插件可手填或 `/convert_to_openapi` 导入（OpenAPI 3、Swagger 2、Postman、cURL），`backend/domain/plugin/internal/openapi/convert_protocol.go`。

## 4. 工作流节点（`backend/domain/workflow/entity/node_meta.go`，示例 `backend/domain/workflow/internal/canvas/examples/`）
- 基础与逻辑：开始、结束、大模型、插件、代码、知识库检索/写入/删除、选择器、子工作流。
- 数据库：SQL 自定义、增、删、改、查。
- 输入输出与问答：输出、输入、问答（QuestionAnswer）、意图识别。
- 循环与批处理：循环、继续循环、终止循环、批处理。
- 变量与文本：设置变量、变量赋值、变量聚合、文本处理、HTTP 请求、JSON 序列化/反序列化。
- 会话与消息：创建/修改/删除会话，查询会话列表/历史，清空历史，创建/修改/删除/查询消息。
- 无 MCP 节点。插件类型有 `coze-studio-mcp` 常量，但 `invocation_mcp.go` 直接返回 "not implemented"。

## 5. 发布渠道
- 只有 Chat SDK(999)、API(1024)、coze(10000010)。`backend/domain/connector/service/connector_impl.go`。无抖音渠道，抖音只以 IDL 枚举出现。

## 6. 大模型配置
- `docker/.env.example`：MODEL_PROTOCOL_0、MODEL_OPENCOZE_ID_0、MODEL_NAME_0、MODEL_ID_0、MODEL_API_KEY_0、MODEL_BASE_URL_0。代码只读 `_0`（`deprecate_model_get.go`）。另有 BUILTIN_CM_* 供 NL2SQL、查询改写、图片标注、工作流召回使用。
- function calling 和 JSON 模式按模型在 `meta.capability` 声明（`model_meta.json` 41 个 true、6 个 false）。声明支持 JSON 模式的有豆包 1.5 thinking/vision-pro、seed-1.6 系列、Gemini；OpenAI、Claude、DeepSeek、Qwen、Ollama 模板写的是 false。参数 `response_format` 可选 Text/Markdown/JSON。`backend/conf/model/model_meta.json`、`backend/conf/model/template/*.yaml`。
