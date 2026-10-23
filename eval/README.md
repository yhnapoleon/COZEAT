# 评测

对应 DESIGN.md 第 5 节、PRD 第 9 节。只跑 DeepSeek，不跑 Claude 对照；生成评测数据、用户模拟器也用 DeepSeek。报告在 `reports/`，原始结果在 `runs/<run>/`。

| 编号 | 测什么 | 规模 | 基线报告 |
|---|---|---|---|
| E1 | 意图、该不该先问、槽位、D1 反馈 | 157 条 | `reports/E1_e1-base-1020.md` |
| E2 | 知识库召回、切分对照、主干按店取证据 | 112 条查询 + 161 家店 | `reports/E2_e2-base-1021+e2-sem-1021.md` |
| E3 | 长期记忆终态 | 14 段脚本 | `reports/E3_e3-base-1021.md` |
| E5 | 端到端人设任务 | 12 个任务 | `reports/E5_e5-base-1021.md` |
| 品类召回 | 新旧品类匹配离线对照（不调 Coze） | 40 个查询词 | `reports/cate_recall_1021.md` |
| 跨店召回 | KB-review 跨店检索开/关，答案店进没进候选 | E2 找店 60 条 | `reports/KBX_kbx-1021b.md` |

E4（决策层校准）、E6（个性化）没做；DESIGN 里 E5 的"关掉记忆和决策层"对照版本也没做。

## E1

```
python -I -X utf8 eval/e1_build.py              # 建集：手写 e1_seed.jsonl + DeepSeek 生成（缓存 e1_gen_raw.json）→ e1.jsonl
python -I -X utf8 eval/e1_run.py --run 名字 -j 4  # 每条调试运行一次 la_understand
python -I -X utf8 eval/e1_run.py --run 名字 --retry-failed
python -I -X utf8 eval/e1_score.py 名字
```

- 手写 51 条（上下文追问、含糊、意图边界、多意图、反馈），生成 106 条（先定标签和条件再让模型写句子；人工改 13 条、删 1 条、补标 2 条，记在 `e1_build.py` 的 `FIX`、`FIX_SLOTS`）。`split` 按 id 哈希 2:1 分 dev / test，留给 GEPA。DuRecDial 2.0 改写没做。
- 被测的是 WF-understand 草稿；`e1_run.py` 开跑前核对草稿 = 发布版、= `canvases/wf_understand.json`，记下画布里实际用的 prompt。WF-understand 不写库。
- 各线程共用一次登录：同一账号再登录会把前一个 session 顶掉。

## E2

```
python -I -X utf8 eval/e2_build.py all         # 5 个对照知识库（e2_auto 等）+ 探针工作流 la_e2_probe，记录在 data/e2_kbs.json
python -I -X utf8 eval/e2_queries.py           # DeepSeek 从提及生成问店 / 找店查询（缓存 data/e2_queries.jsonl）
python -I -X utf8 eval/e2_run.py --run 名字 [--types ask,find]
python -I -X utf8 eval/e2_score.py 名字 [名字2]   # 多个 run 合并节点结果
```

- 5 个对照知识库和 la_e2_probe 是评测专用的，留在 Coze 里，不影响主干；不需要时可以在页面上删。
- 判分规则、剔除的查询（`DROP`）在 `e2_score.py`；人工结论在 `data/e2_conclusion.md`，出报告时放在开头。
- 查询是从提及生成的，提及类切分天然占便宜；前缀对照（mention vs mention_np）不受影响。

## E3

```
python -I -X utf8 eval/e3_run.py --run 名字 -j 3     # 每段脚本一个新 user_id，走发布版，会话结束立即整理
python -I -X utf8 eval/e3_run.py --run 名字 --rescore
```

脚本在 `data/e3_scripts.json`（expect 必须生效、forbid 不能生效）。终态直接读 coze-mysql 的线上 user_memory 物理表（`online_database_info` 查表名，f_1..f_7 = kind、mkey、mvalue、status、support、consistency、confidence）。人工备注在 `data/e3_conclusion.md`。

## E5

```
python -I -X utf8 eval/e5_run.py --check            # 每个任务库里有几家店满足全部条件
python -I -X utf8 eval/e5_run.py --run 名字 -j 3
python -I -X utf8 eval/e5_run.py --run 名字 --rescore   # 改了任务条件的写法后重判
```

任务在 `data/e5_tasks.json`：条件分"开口说 / 问到才说 / 只在长期记忆里"三档。DeepSeek 扮用户，最多 4 轮；选中的店从线上 session_state 的 last_shown 取，按 shops.jsonl 判（地点 1 公里内、品类写法、人均 ≤ 上限 × 1.1、场景标签）。

## 品类召回、跨店召回（10-21）

```
python -I -X utf8 eval/cate_recall.py                       # 旧 LIKE vs 标准品类，差异逐条列出，不调 Coze
python -I -X utf8 eval/kb_cross_eval.py --run 名字 -j 4      # 每条：WF-understand 草稿一次，WF-recall 草稿开/关跨店检索各一次
python -I -X utf8 eval/recall_probe.py "一句话" [--no-kb]     # 调试：打印召回各阶段的店
```

- 跨店召回用 E2 的找店查询（`data/e2_queries.jsonl` 里 type=find），答案店离锚点超过 5 公里或不在库里的不算。这批查询是从答案店的评价段生成的，用词贴近原文，结果偏乐观。
- E1 的 `need` 槽只在金标写了这个键的用例上评分（以前的用例没标），其余用例里填出来的 need 要人工看。
- E5 的品类判分 10-21 起同时认标准品类 std_cate，只按高德文本判的数也写在报告里。
