# Coze Studio 检索补丁（10-21 已应用）

**现状：** 本机 coze-server 跑的是 `coze-studio-server:la-v0.5.1-p1`（v0.5.1 源码 + 本补丁），源码在 `Remote_Intern/coze-v0.5.1`（已打补丁）。
启动 Coze Studio 的 compose 命令要多带一个 `-f`（在 `Remote_Intern/coze-studio` 下）：
```
MSYS_NO_PATHCONV=1 docker compose -f docker/docker-compose.yml -f docker/docker-compose.minio-fix.yml -f ../Agent1/infra/coze-patch/docker-compose.lifeagent.yml --env-file docker/.env up -d
```
回滚：去掉最后那个 `-f` 再 `up -d coze-server`。验证结果见 `eval/reports/E2_patch_compare_1021.md`：同一份数据上找店（混合检索）hit@1 15% → 89%、hit@5 65% → 96%，问店笔记命中@5 51% → 96%；主干路径和 FAQ 阈值表不变。

2026-10-21 做 E2 和 FAQ 时读源码发现知识库检索有三个实现问题，上游 v0.5.1（本机在跑的版本，二进制 2026-02-05 构建，和 v0.5.1 发布日相同）和 main 分支都还在：

| 问题 | 位置 | 影响 | 补丁 |
|---|---|---|---|
| 混合检索的 RRF 对同一块取两路里的最大分，不求和；同分顺序随机 | `backend/infra/document/rerank/impl/rrf/rrf.go` | 混合检索等于把向量前几名和全文前几名随机穿插。E2 同一库上找店 hit@5 混合 67%、纯语义 95% | 改成求和；同分按 ID 排，结果稳定 |
| 节点设的 top_k 没传到检索后端，向量一路固定 4 条（milvus `topK = 4`），全文固定 10 条 | `backend/domain/knowledge/service/retrieve.go` retrieveChannels | 纯语义最多返回 4 条，top_k 设多少都没用 | 把 Strategy.TopK 用 `retriever.WithTopK` 传下去 |
| 返回顺序是切片存储顺序，不是相关度：packResults 用 `WHERE id IN (...)` 回表、没有 ORDER BY | 同上 packResults | 取"第一条"拿到的不是最相关的；评测里 hit@1、MRR 没法算 | 按重排后的顺序恢复（不能按 Score 排：混合模式下 BM25 分和 0–1 的向量分混在一起） |

补丁在 `retrieval.patch`（用本地 7 月版源码生成，三处代码和 v0.5.1 一致，在 v0.5.1 上 `git apply` 干净打上）。

## 现在不修也能用

主干的两处检索都绕开了这些问题：按店取证据是混合检索 + 段首 amap_id 过滤 + 按日期重排，不依赖顺序，E2 测得 161 家全部拿到本店段；FAQ 用纯语义、top3（小于 4）、min_score 0.74，候选全部交给回答节点，也不依赖顺序。补丁的价值在以后的开放式检索（"哪家烧鹅好"这类跨店找证据）和评测能算排序指标。

## 怎么应用

1. 拿 v0.5.1 源码（不要用本地 coze-studio 目录，那是 7 月的提交，直接重编等于升级服务端 5 个月，数据库迁移和前端兼容都有风险）：
   ```
   git clone --depth 1 --branch v0.5.1 https://github.com/coze-dev/coze-studio.git coze-v0.5.1
   cd coze-v0.5.1 && git apply ../Agent1/infra/coze-patch/retrieval.patch   # 打不上就手改
   ```
2. 编译镜像（只换二进制，底座是本机的官方镜像；首次要下 golang:1.24-alpine 和依赖，约 1–2 GB）：
   ```
   docker build -f ../Agent1/infra/coze-patch/Dockerfile.patched -t coze-studio-server:la-v0.5.1-p1 .
   ```
   10-21 第一次编译时 goproxy.cn 下 lz4 中途断开（unexpected EOF），Dockerfile 已改成 `|` 分隔的多个代理（前一个出任何错都换下一个）外加整体重试 3 次，第二次编译通过，二进制约 174 MB，镜像 1 GB。
   确认本机镜像就是 v0.5.1：上游 main 在 v0.5.1 标签（01-20）到发布和镜像构建（02-05）之间没有新提交，所以用这个标签的源码重编不会混进别的改动。
3. 切换：在启动 Coze Studio 的 compose 命令后面加 `-f <Agent1>/infra/coze-patch/docker-compose.lifeagent.yml`（minio-fix 那个 -f 照旧要带），`up -d coze-server`。回滚：去掉这个 -f 再 `up -d coze-server`。
4. 验证（只用现有数据，不抓取）：
   - `python -I -X utf8 eval/e2_run.py --run e2-patched --types ask,find` + `e2_score.py e2-patched`：纯语义前 10 条应该能返回超过 4 条；混合检索应不再明显差于纯语义；hit@1、MRR 开始有意义。
   - `python -I -X utf8 eval/faq_eval.py run --run faq-patched`：阈值表应和 faq-1021b 接近（补丁不改分数）。
   - 主干回归：`bridge/debug_run.py` 跑一句找店、一句 FAQ；`eval/e1_run.py`（理解阶段不碰知识库，应完全不变）。
5. 记下来：以后升级 Coze Studio 要重新打这个补丁；本机就和官方版本有了差异，README 和 HANDOVER 里写明。
