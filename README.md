# 抖音本地生活推荐 Agent（框架 v0）

基于 Coze Studio 的深圳餐饮推荐 Agent：找餐厅、选套餐、对比评价、路线规划。设计见 [DESIGN.md](DESIGN.md)，调研见 [research/](research/)。

模型：10-20 起 Agent 用 DeepSeek V4.1 Flash（`deepseek-flash`），之前用 Claude Opus 5.5，都经 LiteLLM 网关接入；网关还配了豆包（火山方舟）。切换步骤：`infra/litellm.yaml` 加模型组 → `bridge/register_model.py add` 注册到 Coze → 改 `bridge/flow_code.py` 的 `MODEL_TYPE` 和 compose 里 sidecar 的 `DECIDER_MODEL` → 重建发布子工作流和主干。ETL 的抽取和读图（`common/llm.py`）10-20 起默认也是 `deepseek-flash`，在 DeepSeek 上还没跑过。**不要再调 Claude Opus**（太贵），除非用户明说。

## 组成

| 部分 | 位置 | 状态 |
|---|---|---|
| 运行时 | 本机 Coze Studio（localhost:8888） | 运行中；coze-server 打了检索补丁（`infra/coze-patch/`，启动要多带一个 `-f`） |
| LLM 网关 | `infra/litellm.yaml`，容器 lifeagent-litellm（:4000） | 运行中；Claude 的 temperature/top_p/top_k 在这里丢掉 |
| sidecar | `sidecar/app/main.py`，容器 lifeagent-sidecar（:8090） | `/health`、`/decide`（plain 后端，接口与 Jev 一致）、`/geo`（高德地理编码 + POI 兜底，key 不进画布）、`/route`（高德步行/驾车/公交）、`/hot`（博查搜索，按天缓存）、`/mem/touch` 和 `/mem/consolidate`（用户安静 120 秒后经 OpenAPI 跑 WF-consolidate） |
| 存储 | `bridge/build_storage.py` | 3 张表 shop / user_memory / session_state，4 个知识库 KB-review / deal / faq / rank |
| 智能体 | `bridge/build_agent.py` | "抖音本地生活推荐"，对话流模式，主干绑 CF-main |
| 主干对话流 | `bridge/build_cf_main.py`，画布另存 `canvases/cf_main.json` | v0.0.17 已发布：读状态 → 读画像 → WF-understand → 分流 → 找店：追问（同一缺口只问一次，"你看着办"直接按默认推荐）或（WF-recall → 推荐生成 → 格式化）；偏好管理；对比、路线：认店 → WF-compare / WF-route；看榜单：WF-hot；意图拿不准时澄清；选定上轮某家时确认；FAQ 和闲聊走 WF-faq → 汇总 → WF-memwrite |
| 子工作流 | `bridge/build_subflows.py`，节点源码和 SQL 在 `bridge/flow_code.py`，画布另存 `canvases/wf_*.json` | 6 个 + WF-faq，WF-understand v0.0.5、WF-recall v0.0.5、其余 v0.0.2、WF-faq v0.0.1，主干锁版本号（`lifeagent_ids.json` 的 `sub_versions`）。WF-understand：决策请求 → sidecar `/decide`（D2 意图 + D1 反馈）‖ Coze 意图节点（兜底和影子对照）‖ 记忆抽取 ‖ 槽位提取 → 汇合、合并槽位（等记忆抽取：本轮改口推翻的画像条目当轮不用）；WF-recall：地理编码 ‖ KB-review 跨店检索（品类 + 用户要求 + 场景）→ SQL 召回（标准品类展开、菜名两级命中、预算区间、跨店名次）→ 裁剪并逐级放宽（先放宽用户说的距离） → KB-review 证据（批处理）→ 场景重排；WF-compare、WF-route、WF-hot；WF-memwrite：写会话状态 ‖ 写/删记忆 → 通知 sidecar |
| 记忆整理 | `bridge/build_memory.py`，画布另存 `canvases/wf_consolidate.json` | WF-consolidate v0.0.6：候选偏好归组 → D4 稳定性（sidecar `/decide`，每组一个 noul）→ 按支持次数（不同会话）、一致性和 D4 提升 → 改写推荐策略（SAGER） |
| 门店 ETL | `etl/amap_crawl.py` → `etl/resolve_mentions.py` → `etl/audit_mentions.py`（关卡）→ `etl/shop_tags.py` → `etl/shop_cats.py`（标准品类）→ `etl/deals.py` → `bridge/load_shops.py` | 南山三个商圈 338 家（海岸城、深圳湾万象城、南山科技园），草稿表和线上表都已写入；每家都打了标准品类（`common/cate_tax.py` 两级表，10-21）；团购 20 家来自笔记、其余合成并标注 |
| UGC 管线 | `etl/xhs_crawl.py` → `etl/xhs_ingest.py` → `etl/xhs_images.py` → `etl/resolve_mentions.py` → `etl/audit_mentions.py` → `bridge/load_kb.py` | 小红书 200 篇（登录态浏览器，低频只读）→ 抽取 → 对齐门店（`etl/ugc.py` 的 `align`：名字证据 + 位置一致性否决，10-21 重写）→ KB-review，232 段（对上门店的 242 条提及里有内容的）；点评探店笔记、千帆抖音笔记（`etl/dy_notes.py`）也能进同一管线 |
| 冒烟测试 | `etl/smoke.py` | 高德、博查、Anthropic、OpenRouter 正常；豆包搜索、聚推客见下 |

## 常用命令

```
python -I infra/make_env.py                                   # keys/*.txt → infra/.env（不打印 key）
docker compose -f infra/docker-compose.yml --env-file infra/.env up -d --build
python -I -X utf8 etl/smoke.py                                 # 每个外部 API 调一次
python -I -X utf8 etl/amap_crawl.py --anchor 海岸城 --radius 1000 --pages 2
python -I -X utf8 etl/resolve_mentions.py [--offline]          # 提及对齐门店（每次从头算；高德结果缓存在 data/processed/amap_cache.json）
python -I -X utf8 etl/audit_mentions.py [--kb]                 # 对齐审计 = 管线关卡，有硬性问题退出码 1
python -I -X utf8 etl/shop_cats.py [--dry-run]                 # 标准品类打标（缓存 data/processed/shop_cats.json，改了品类名才重打）
python -I -X utf8 eval/cate_recall.py                          # 品类召回离线对照：旧 LIKE vs 标准品类，差异逐条列出
python -I -X utf8 eval/recall_probe.py "海岸城不用排队的火锅" [--no-kb]   # 调试召回：打印跨店命中、裁剪后 12 家、交给推荐生成的 8 家
python -I -X utf8 bridge/load_shops.py                         # 草稿表 + 线上表
python -I -X utf8 bridge/alter_table.py shop [--add 名字:类型:说明]   # 列出或给表加列（会原样带上现有列）
python -I -X utf8 bridge/load_kb.py [--rebuild]                # KB-review；对齐规则或提及变了用 --rebuild 整库重写
python -I -X utf8 bridge/build_subflows.py build|run|release [名字...]   # 子工作流；release 调试通过后发布下一个版本
python -I -X utf8 bridge/build_cf_main.py build --draft        # 主干读子工作流草稿，改子工作流时调试用
python -I -X utf8 bridge/build_cf_main.py build                # 主干锁 sub_versions 里的发布版
python -I -X utf8 bridge/build_cf_main.py run "海岸城附近人均100以内的火锅" dev-1
python -I -X utf8 bridge/build_cf_main.py release v0.0.18      # 锁版本重建、调试一次再发布（当前 v0.0.17）
python -I -X utf8 bridge/build_memory.py build|run|release v0.0.7   # WF-consolidate（当前 v0.0.6；release 会先重建保存）
python -I -X utf8 bridge/chat_api.py "深圳湾万象城附近两个人约会，想吃西餐" demo-user conv-1
python -I -X utf8 bridge/debug_run.py "第一家怎么去" dev-1   # 调试运行，打印关键节点和回答
python -I -X utf8 bridge/stress_run.py                       # 连跑 24 轮，查对话流挂住
python -I -X utf8 bridge/mem_consolidate.py demo-user          # 立即整理某个用户的记忆
```

## 踩过的坑

- 调试运行读写草稿表，发布后的对话读写线上表。ETL 要两边都写，用 `update_records`，`database_id` 传线上 id，`table_type` 1 草稿、2 线上。
- 对话流必须先成功调试一次才能发布，`release` 命令已经串好。
- Docker Desktop 重启后，coze-web 可能缓存旧的 coze-server 地址，返回 502；`docker restart coze-web` 即可。
- 高德地理编码对"科技园"这类非标准地名会失败，sidecar 改用 POI 关键词搜索兜底；但会有歧义（"科技园"命中宝安的福海国际科技园），槽位提取提示词要求写完整地名。
- `python -I` 会忽略 PYTHONUTF8，打印中文要加 `-X utf8`。
- Coze HTTP 节点实际 10 秒超时，sidecar 接口要在 10 秒内返回（`/route` 因此并发查三种路线）。
- HTTP 节点的超时写在全局 `http.DefaultClient` 上，一次执行里所有 HTTP 节点用最后构建的那个值；所以本项目统一 10 秒（`canvas.HTTP_TIMEOUT_S`）。POST 节点不能重试：重试复用已读空的请求体，第二次发出去是空的。
- 子工作流节点 `workflowVersion` 为空时运行时读草稿，发布版的主干必须锁版本号；子工作流改完要先 `release`，再 `build_cf_main.py release`。
- 意图节点不要用极速模式：它只认纯数字输出，换 DeepSeek 后报 invalid syntax；现在用标准模式并配了出错返回默认值（`bridge/COZE_API_NOTES.md`）。
- 对话流结束节点不要开流式：流式模式下 LLM 节点偶发不回报完成，整轮挂住（`bridge/COZE_API_NOTES.md`）。
- 记忆表按用户隔离：调试运行用当前账号、读写草稿表；端到端测记忆用 `chat_api.py` 换 user_id。
- 提及对齐（10-21 审计后重写）：店名按字符包含会把拉丁名"le""la"配错、把"Susea寿司"配给寿司郎；只看店名不看位置会把"海岸城店"挂到来福士店（1.3 公里）。现在名字要有证据（中文 2 字要在开头、品类词不算、拉丁名按整词），两边都写了商场就按商场比（海岸城和深圳湾万象城只隔 900 米），认不出商场才按地理编码距离（1 公里，片区名放宽）。从全城高德结果补新店只认确切名字。`resolve_mentions` 每次从头算，连跑两次结果相同。

- 品类（10-21）：高德品类太粗（42% 的店只写"中餐厅""餐饮相关场所"），也有写错的，召回改按标准品类 std_cate（第 21 列）。品类表只有一份（`common/cate_tax.py`），打标脚本和 CODE_MERGE 都从这里取，CODE_MERGE 构建时把查询词表和 `query_res` 源码内联进去。菜名写进细类同义词会变成整类命中（"螺蛳粉"命中全部 25 家粉面店），具体的菜放菜名列表。
- Coze `update_records` 一批记录按第一条的字段建列：第一条没有某个键，同批其他记录这个字段全写成 NULL（10-21 查出 24 家人均丢了）。`load_shops` 已按字段组合分批写。
- Windows 的 Git Bash 里用 curl 传中文参数不是 UTF-8，sidecar 收到乱码、高德报 ENGINE_RESPONSE_DATA_ERROR；测 sidecar 用 Python。

## 已知缺口（下一步）

1. 聚推客抖音团购 2025-06-10 已下线，个人拿不到抖音团购结构化数据（research/13）；团购信息从小红书、抖音笔记正文和读图里抽取，结构化字段待合成并标注。
2. 小红书改用登录态浏览器读取（`etl/xhs_crawl.py`，独立 Chrome profile，低频只读），后处理见 HANDOVER 第 8 节第 1 条；抖音 shipin 页仍缺发现入口，千帆限定 www.douyin.com 只能拿到图文笔记正文。
3. KB-rank 空，榜单只靠搜索和本地热度。记忆的提升阈值、决策层的 D1/D2/D4 阈值都是初值，还没用评测集调过。
4. 门店覆盖南山三个商圈 338 家，再扩要看高德配额（个人 5000 次/月，10-21 累计约 1030 次）。10-21 起暂停真实抓取。
