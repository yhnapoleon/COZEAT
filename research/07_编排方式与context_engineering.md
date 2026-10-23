# 编排方式、Coze Studio 约束、context engineering 核查（子代理报告原文，2026-10-17）

## (一) 本机仓库 coze-studio（commit fefb05ff）

1. **编排模式**：`backend/api/model/app/bot_common/bot_common.go:1283-1288` 定义 `BotMode_SingleMode/MultiMode/WorkflowMode`，但 MultiMode 只在 api/model 层，domain 层无实现。实际两条运行路径：`agentrun/internal/run.go:169` 按 `BotMode_WorkflowMode` 分流到 `chatflow_run.go`（智能体整体由一条对话流驱动），否则 `singleagent_run.go`。单智能体 = eino ReAct Agent（`agentflow/agent_flow_builder.go:160-180`）：插件/工作流/数据库/变量统一注册为 tool，走 function calling，模型不支持 FC 直接报错。工作流 `TerminatePlan==UseAnswerContent` 时 `ToolReturnDirectly`。挂载数量未见上限。对话流 `WorkflowMode_ChatFlow=3`：绑定 conversation、开始节点带 `USER_INPUT/CONVERSATION_NAME`、有会话/消息节点，问答/输入节点中断后以 `conversation.chat.requires_action` 返回并可续接。

2. **中断/续接**：工作流 OpenAPI `/v1/workflow/stream_run` SSE 事件 `Message/Interrupt/Done/Error`，续接 `/v1/workflow/stream_resume`（execute_id+event_id+resume_data）。中断类型 `EventType_Question/InputNode/LLM(100)`。对话流 `/v1/workflows/chat`：中断发 `requires_action`，再次调用同 conversation 自动 `StreamResume`。智能体 `/v3/chat`：工作流工具中断→`conversation.chat.required_action`，interrupt 存消息 Ext `resume_info`，下次 `/v3/chat` 解析并续跑（checkpoint 存 CPStore）。无 submit_tool_outputs 类接口。

3. **定时触发**：后端无 cron/trigger 模块。外部可调 `/v1/workflow/run`（同步）、`/v1/workflow/stream_run`、`/v1/workflow/get_run_history`。

4. **提示词动态内容**：系统模板 `agentflow/system_prompt.go:28-61` 占位 `{{agent_name}} {{time}} {{persona}} {{memory_variables}} {{knowledge}} {{tools_pre_retriever}}`；persona 内 Jinja2 `{{var}}` 由智能体变量填充。知识库 `Auto=true` 时每轮自动召回注入 `{{knowledge}}`，否则作为 tool `recallKnowledge` 按需调用；配置 `TopK/MinScore/Auto/SearchStrategy/ShowSource/NoRecallReplyMode/RecallStrategy(UseRewrite/UseRerank/UseNl2sql)`。数据库只以 tool 形式存在，不进提示词。工作流 LLM 节点 `FCParam` 支持挂工作流/插件/知识库，并有 `enableChatHistory/chatHistoryRound`。

5. **代码节点**：只支持 Python（JS 返回 not supported）。内置模块黑名单 23 个（socket/threading/multiprocessing/pty 等），第三方白名单仅 `httpx、numpy`（`backend/conf/workflow/config.yaml`）。`CODE_RUNNER_TYPE=local|sandbox`，默认 local（venv，无隔离）；sandbox=deno+pyodide，`CODE_RUNNER_ALLOW_NET` 默认仅 cdn.jsdelivr.net，超时默认 60s，内存 100MB。

6. **Chat SDK 消息类型**：ContentType = `text/image/video/music/card/widget/app/mix`；`card_status`、`required_action`。问答节点中断走 `renderQACardDSL`。SDK 文档指向 coze.cn。

7. **流式**：输出节点 `streamingOutput`。`/v3/chat` SSE 事件 `conversation.chat.created/in_progress/completed/failed/required_action`、`conversation.message.delta/completed`、`conversation.error`、`conversation.stream.done`。

## (二) 网络核实

8. **Context engineering**
- Anthropic《Effective context engineering for AI agents》2025-09-29：system prompt 取"right altitude"；工具 token-efficient、minimal overlap；just-in-time context + progressive disclosure；长程任务三法 compaction / structured note-taking / sub-agent；"do the simplest thing that works"。https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- Manus 2025-07-18：Design Around the KV-Cache；Mask, Don't Remove；Use the File System as Context；Manipulate Attention Through Recitation；Keep the Wrong Stuff In；Don't Get Few-Shotted。https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus
- Chroma《Context Rot》2025-07-14（18 模型）：needle-question 相似度越低随长度衰减越快；单个干扰项即降性能；LongMemEval 聚焦提示（~300 tok）显著优于全历史（~113k）。https://www.trychroma.com/research/context-rot
- Google ADK 博文 2025-12-04：context as compiled view；Session/Memory/Artifacts 分离；working context 每次重建；compaction over sliding window；Agents-as-Tools vs Agent Transfer。https://developers.googleblog.com/en/architecting-efficient-context-aware-multi-agent-framework-for-production/ ；白皮书《Context Engineering: Sessions & Memory》2025-11。OpenAI 未检索到官方指南。

9. **单 vs 多 Agent**
- Anthropic 2025-06-13：多 Agent 比单 Opus4 高 90.2%；token 用量解释 80% 方差；多 Agent 约 15× token；适用重并行、超单上下文，不适用需共享上下文的域。https://www.anthropic.com/engineering/multi-agent-research-system
- Cognition 2025-06-12《Don't Build Multi-Agents》；2026-04-22《Multi-Agents: What's Actually Working》：并行写仍失败；可行的是"writes stay single-threaded"下的 reviewer、Smart Friend、map-reduce-and-manage。https://cognition.com/blog/multi-agents-working
- Stanford 2604.02460（2026-04-02）：固定 thinking-token 预算下单 Agent 在多跳推理上持平或优于多 Agent；多 Agent 仅在额外算力或单 Agent 上下文退化时占优。未找到任务型对话/推荐的单 vs 多对照实验。

10. **导购/本地生活 Agent 公开资料**
- 美团"小美"2025-09-12 公测：LongCat 256K 直连内部接口；缺参从历史补全并"最少量的确认"；对话内下单；记住地址/偏好；定时任务。https://www.qbitai.com/2025/09/330905.html
- 淘宝 2025-10-16 六款 AI 导购应用，无架构细节；阿里云社区"淘宝问问"示例：Router Agent 分类→品类导购 Agent 逐参数追问→SQL 检索，thread_id 记忆（社区帖）。https://developer.aliyun.com/article/1643815 ；淘宝闪购 2026-08-05 开放 35 个 MCP Server（商家侧）。
- 高德 2025-08-15：主-从 Agent 三层，主 Agent 用千问意图模型拆子任务+重写 query，从 Agent 二次意图+参数抽取，工具匹配模型选工具。https://hub.baai.ac.cn/view/48146 ；扫街榜 2026-09-10 只讲评分权重。
- 京东商家助手 Master+Sub Agents、ReAct、Memory 取近/远期特征（2024-06，商家侧）。
- 抖音"探饭"2025-07-17：豆包模型，按预算/人数/忌口出方案、PK 比店，无架构披露。https://www.36kr.com/p/3382861019773188
