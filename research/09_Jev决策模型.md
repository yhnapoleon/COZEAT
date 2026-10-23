# Jev（System One 决策模型）核实与引入方案

2026-10-17。一手来源：typesafe.ai 博客与 docs、arXiv 2609.23986（Jev-Mem）、2610.21411（Jev-LiteMAS）、2610.02076（LLM-as-Jev）、2609.36965（Chinese-Jev）、GitHub jaredpalmer/kev、libingzheren/Jev-Mem、xiaohuaxi/jev-study；二手：dailydoseofds、firecrawl、sarthakai substack。厂商数字一律标自报。

## 1. 是什么

- TypeSafe AI 2026-09-15 发布的"System One Model"。不生成文本，输入 `state`（文本或 JSON）加若干类型化 `questions`，输出带概率的类型化答案。三种原语：`choice`（从候选里选一个，返回各选项概率）、`score`（有序量表定位）、`noul`（是/否，返回 0–1 真值概率）。训练方法自称 RLCD（Reinforcement Learning for Calibrated Decisions）。
- 端点 `POST https://api.typesafe.ai/v1/systemone`，Python SDK `typesafe-sdk`，`client.system_one(state=..., questions=...)`。一次请求可并行多个问题（jev-study 实测单次最多 900 个问题；state 加最贵单问题 ≤ 32,743 token，整请求 ≤ 65,792 token）。
- 自报：延迟 70–500ms，输入 $0.042/MTok，输出免费；"快 40–200 倍、便宜 400 倍"是厂商内部对比。闭源权重。
- 接入：注册 2026-09-20 开放、09-22 因需求暂停、09-27 重开，现在无免费额度（二手，建议直接上 console.typesafe.ai 看）。OpenRouter 有过 `typesafe/jev-1.13-20260917` 快照（jev-study 用的就是它），今天 OpenRouter 模型列表只剩 `typesafe/jev-router`。Vercel AI Gateway 模型名 `typesafe-ai/jev`。本机能直连 openrouter.ai 和 typesafe.ai。
- 生态：LangChain `langchain-typesafe`（TypeSafeClassifier、ModelRouterMiddleware、AutoModeMiddleware，实验性）、DSPy 分支、MCP 连接器、Vercel AI SDK 7 `evaluate`。

## 2. 官方承认的局限（docs.typesafe.ai/model-jaggedness/jev-1.13）

字面理解不推断意图；数学、计数、日期比较弱（放代码）；间接推理和双重否定差；state 里无关信息会拉低准确率（先在代码里过滤）；对抗注入会影响结果；instructions 和 criteria 矛盾会混淆；选项顺序影响结果（调换顺序验证）；不能生成文本；不能抽取未知值（先找候选再让它选）。概率必须在自己的数据上按决策类型校验，置信度不是安全边界。

## 3. 中文可用性

- 官方：英文最强，CJK"支持但精度不一"，让开发者自己测。
- jev-study（xiaohuaxi，约 23.8 万次调用 $13）：中文分类 12/12，讽刺、双重否定、俚语理解正确，约一汉字一 token，3 万字文档里能找到关键句；score 的漂移主要来自量表措辞而非语言。样本是手工构造，没有准确率意义上的数字。
- Chinese-Jev（2609.36965，2026-09-29，颜水成等）：轻量 encoder-only 骨干，1000 万中文语料预训练 + 医疗/法律/金融微调，自建 CJ-Bench；自报通用域准确率比 Jev 高 1.24%、快 20.3 倍，专业域平均达 Jev 的 92%。权重是否开源未确认。
- 结论：中文分类类问题可用，上线前要用自己的 200 条标注做校准曲线。

## 4. 开源替代

| 项目 | 情况 | 适合我们吗 |
|---|---|---|
| Kev（jaredpalmer/kev，9.3k star，Apache-2.0） | Qwen3.5 Base + LoRA，0.8B/4B/9B/27B，API 与 TypeSafe 完全兼容（`python -m kev.serve` 起 `/v1/systemone`）。自报新数据源准确率 4B 0.817、27B 0.851，Jev 0.857。模型卡写明仅英文。4B 要 16GB 显存 | 本机无 GPU、31GB 内存，只能跑 0.8B 且英文；不适合 |
| LLM-as-Jev（2610.02076） | 不训练：候选编号 `[1]…[K]`，前缀 `Best answer: [` 后读各编号 logprob 做 softmax。Qwen3.5-4B 零样本 JevBench 81.4%。需要模型暴露 logprobs | 可作后备：bridge 侧用任意带 logprobs 的 OpenAI 兼容接口（本机 Ollama qwen 或云端）模拟同一 schema，中文没问题 |
| Chinese-Jev | 见上，权重未确认 | 待观察 |
| Laya（Jev-Mem 的本地后端） | 小 encoder，Jev-Mem 已集成 | 英文为主，未核中文 |

## 5. 和它相关的三篇论文对我们的用处

- **Jev-Mem**（2609.23986，代码 MIT 开源）：System One 管记忆的全部高频决策，System Two 只在最后合成答案。构建阶段：每条观测用 noul 打 episodic/semantic/procedural/preference 四类分、语义和因果关系分（阈值 0.60 才建边）；每 20 次写入做一次巩固，choice 在 keep_separate/merge/promote/uncertain 中选，merge 或 promote 概率 ≥ 0.85 且矛盾 < 0.85 才让 LLM 生成摘要。检索阶段六个 noul 决定激活哪几张关系图、预算 80 按需求分配、候选四维打分、充分性 ≥ 0.95 自适应停止。LoCoMo（gpt-4o-mini 判）0.777，对 MAGMA 0.700、A-MEM 0.580，构建 158 秒对 A-MEM 3636 秒，查询 0.93 秒。没有消融。
- **Jev-LiteMAS**（2610.21411，2026-10-15）：三个多 Agent 框架里路由和审核占生成 token 的 31.7%–43.5%；用 Jev 做专家选择和中间产物筛选，token 少 12.7%–54.6%，准确率降不超过 1.34 个百分点。
- **LLM-as-Jev**：免训练基线的做法和"只在小模型或高基数路由时才微调"的结论。

## 6. 在本 Agent 里的用法

原则：所有"有限答案集合"的判断交给 Jev，LLM 只做抽取、生成和解释，代码做算术和日期。每个决策点都先用 LLM 版跑通，再换 Jev 并做 shadow 对比，这本身就是一个可写进文档的实验。

| 决策点 | 现在的做法 | Jev 问题 | 为什么换 |
|---|---|---|---|
| 意图识别 | Coze 意图识别节点（LLM） | choice{找餐厅, 选套餐, 对比评价, 路线, FAQ, 闲聊} + noul"是否需要澄清" | 概率输出直接做置信门控：低于阈值走问答节点 |
| 反馈判定 | 无 | choice{接受, 否决, 中性} 对用户下一句 | SAGER 策略改写的信号来源 |
| 记忆写入四选一 | LLM 输出 ADD/UPDATE/DELETE/NOOP | choice{add, update, delete, noop} + noul"是长期稳定偏好吗" + score"与历史一致性" | Jev-Mem 同款分工；每轮都跑，便宜 |
| 记忆巩固 | 异步 LLM 归并 | choice{keep_separate, merge, promote, uncertain}，≥0.85 才让 LLM 写摘要 | 照搬 Jev-Mem 阈值 |
| 证据筛选 | 向量 top-k 直接喂 LLM | 每条评论 noul"是否支持用户当前条件" | 官方用例"检索相关性筛选"；Chroma 的"一个干扰项就降性能" |
| 候选预筛 | SQL 过滤后 20 家全进 LLM | 每家 score"符合约束程度"，留前 8 | Jev-LiteMAS 的"中间产物筛选"，省 LLM token |
| 插件故障分类 | 代码按状态码分支 | choice{规格漂移, 调用错, 执行失败, 输出漂移, 跨源冲突} | ToolBench-X 五类对应五条恢复路径 |

接入方式：Coze Studio 没有 Jev 节点，用 HTTP 节点或自定义插件（OpenAPI 导入）调 `/v1/systemone`；后备是 bridge 侧起一个同 schema 的 LLM-as-Jev 服务，HTTP 节点只改 base URL。

成本：每轮 5–7 个问题、state 几百 token，按 $0.042/MTok 算 1 万轮不到 1 美元；主要成本在注册后要先充值。

## 7. 风险

- 闭源、早期访问、没有独立校准数据；中文准确率没有第三方数字。
- 选项顺序偏置，要做顺序置换一致性检查。
- 不能抽槽位，参数提取仍是 LLM。
- 账号和充值需要用户自己办；拿不到就用 LLM-as-Jev 后备，schema 不变。
