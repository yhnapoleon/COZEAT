# 交接文档：抖音本地生活推荐 Agent（实习第二阶段）

> 更新于 2026-10-23（CF-main v0.0.19：画像页、多维量化评分、推荐解释、笔记可信度，见 0.15；网页 Demo 用法在 `demo/README.md`，10-22 的进度在 0.2）。新对话先读这份文件，再按第 2 节读记忆和设计文档。stage1 和 READAR 的交接在 `ByteDance/handover.md`，和本项目无关。

## 0. 下一个对话先做什么

### 0.1 规矩（每次都适用）

1. 确认服务在：`docker ps` 看 coze-*、lifeagent-litellm、lifeagent-sidecar 都 Up；`curl localhost:8090/health` 返回 `coze_pat: true`、`decider_model: deepseek-flash`。重启电脑后的处理见第 4 节末尾。
2. **暂停一切真实爬取**（用户 10-21：账号收到警告）。不跑 `etl/xhs_crawl.py`、不下载小红书图片、不用 Claude in Chrome 或 `data/browser/xhs-profile` 打开小红书/点评/抖音页面。测试、修数据、重建知识库只用 `data/` 里已有的；需要新数据先问用户。高德、博查这类 API 照常少量用。
3. **模型只用 DeepSeek V4.1 Flash，不调 Claude Opus**（用户 10-20：太贵）。Coze 节点、sidecar 决策层、ETL（`common/llm.py`）默认都是 `deepseek-flash`；评测不跑 Claude 对照。
4. **MVP 优先**：完整链路先做出来，稳定性和"让 DeepSeek 对齐 Claude"以后靠结构设计和 checkpoint 做。

### 0.15 10-23 做完的事（画像页、多维评分、推荐解释、笔记可信度，v0.0.19）

用户 10-23 看完视频提了三点：用户要能看到自己的画像和偏好怎么被维护；评价那块要展示评分标准细则、参考的小红书链接，并评估可信度（是不是软广），评分要多元、每项给量化分；要明确体现偏好怎么影响筛选。用户说 1 现在做，2、3 做一部分，其余放第二阶段，可以看看有没有开源的推广识别模型。

1. **"我的口味"页**（`demo/memview.py` + `web/src/ProfileDrawer.tsx`）：顶栏按钮打开右侧抽屉。分生效中（来源是明说还是从几个会话归纳、对推荐的作用、原话）、待确认（出现几个会话、离提升差几次）、推荐策略、最近的选择、记忆怎么维护（六步规则）、历史。
   - 数据直接读线上 user_memory 表：coze-mysql 的 `opencoze.table_<物理 id>`，字段对应从 `online_database_info.table_field` 取，`docker exec` 在容器里跑 mysql，账号用容器的环境变量。
   - 只读；"忘掉"走对话，"立即整理"调 sidecar `/mem/consolidate`。
   - 演示模式显示录制时每一步之后的画像快照（`record.py` 每步存 `profile`）。
2. **多维量化评分**（CODE_TRIM）：原来那条打分公式拆成 10 个维度，每项 0–10 分，加权平均得综合分，排序就用它。
   - 维度和权重：口碑评分 0.20、笔记口碑 0.10、距离 0.15、预算 0.10、品类 0.15、场景 0.10、口味 0.10、评价命中 0.10、热度 0.05、可信度 0.05。用不上的维度不算，权重按算了的归一。
   - 评分标准细则在 `web/src/Insight.tsx` 的 `RULES`，和 `dims()` 一一对应，改一处要同步另一处。
   - 合并证据发现评价里写到场景的，场景分改成 8 分并重算。
   - 卡片 items 带 `score`、`dims`；前端每家店可以展开评分明细。
   - 综合分只决定哪 8 家交给推荐生成，最后 3 家由推荐生成挑，所以不一定按综合分排，面板里写了这句。
3. **"为什么是这几家"**：
   - 合并槽位输出 `conds_json`：每个条件标来源（本轮 / 本会话 / 长期偏好 / 默认假设），忌口和本轮改口也列出来。
   - 裁剪候选和合并证据输出 `funnel`：5 公里内多少家、各条件满足几家、进候选、场景过滤、交给推荐生成、推荐几家；还有口味偏好让哪些店加分或往后排。
   - 格式化把两者放进 shop_list 卡片的 `explain`。
4. **笔记可信度和笔记口碑**（`etl/note_quality.py`，只用现有数据）：
   - 88 篇对上门店的笔记用 DeepSeek 零样本判断。线索参照 CHASM（arXiv 2604.20511，小红书隐蔽广告数据集，4992 条，MIT），加餐饮探店特有的；另用规则抓评论"广告/恰饭"、正文"受邀/私信"等。
   - 抽查后改成四档：推广分 < 0.5 真实体验、0.5–0.8 存疑、≥ 0.8 疑似推广，写明合作要规则和模型都认。结果：真实体验 46、疑似推广 26、存疑 15、明确合作 1。第一版只有三档时疑似推广 42 篇，热门的真实笔记（只夸、评论问店名）也被判进去了。
   - 240 条提及按口味、环境、服务、性价比、排队打 1–5 分，按店汇总时疑似推广的笔记降权（1 - 0.7 × 推广分），得到笔记口碑分（0–10），138 家有分。
   - 结果在 `data/processed/note_cred.json`、`shop_quality.json`；CODE_TRIM 构建时把每家店的笔记口碑分和可信度内联进代码节点（`flow_code._qual()`）。改了数据要重建 WF-recall。
   - Demo 后端 `/api/shop/{amap_id}/notes` 给每家店的参考笔记：标题、日期、互动数、可信度和判断依据、链接 `xiaohongshu.com/explore/<note_id>`。没存 xsec_token，链接能不能直接打开要看浏览器的登录态。
5. **开源推广识别模型的调研**：
   - CHASM：数据集在 HF（Jingyi77/CHASM-Covert_Advertisement_on_RedNote），代码在 GitHub，没有放微调好的模型。论文里零样本 GPT-4o F1 0.597、DeepSeek-V3 加示例 0.592，微调 Qwen2.5-7B 0.756；去掉图片或评论都会明显变差。
   - AdDetector（ACM 2026）做知乎软文，有小红书迁移实验，正文 403 没读到。
   - 中文 HF/ModelScope 上没找到现成的软广分类模型。本机没有 NVIDIA 显卡，微调放到第二阶段。
6. **发布和评测**：
   - WF-understand v0.0.7、WF-recall v0.0.7、CF-main **v0.0.19**。
   - E5 12/12，只按文本判 10/12，单轮延迟中位 24 秒（`E5_e5-v19-1023.md`）。
   - 演示数据按原场景重录（user `demo-rec-1023111923`，带画像快照）；视频 `demo/videos/default_1023_1127.mp4`（3 分 15 秒），里面展开了第一家的评分明细、打开了两次画像抽屉。

**放到第二阶段：** 用 CHASM 微调推广识别（要云上显卡），用我们的餐饮笔记标一个小测试集量零样本准确率；读图（CHASM 结论是图片和评论都关键）；可信度用到证据和原话挑选上（现在只进综合分 5%）；画像页里直接改偏好值；E6 个性化评测；评分权重用评测集调（现在是拍的初值）。

### 0.2 网页 Demo（10-22 做完）

用户 10-22 定的：卡片直接带坐标；演示用写死的数据（先真实跑一遍录下来，演示时回放），出一个演示模式，用户自己录屏；先不做远程访问，录屏直接发。

**做了什么：**
1. **CF-main v0.0.18**（WF-understand v0.0.6、WF-recall v0.0.6、WF-compare v0.0.3、WF-route v0.0.3）：
   - shop_list 卡片带锚点坐标（解析坐标节点那一个，和召回算距离用的是同一个）和每家店的 lng/lat、地址、显示出来的团购；SQL_RECALL、ROW_SCHEMA 加了 lng、lat、address。
   - compare 卡片带坐标（对比格式化从子工作流输入取，挂证据节点会去掉坐标）。
   - 路线回答加 route 卡片：sidecar `/route` 多返回起终点坐标和一条折线（步行 2 公里内取步行，否则驾车，抽到 120 个点以内），sidecar 已重建。
   - 选项按钮：意图澄清在 CODE_JOIN 里按候选意图给 2–3 句能直接发的话（地点取槽位提取认出的，没有就海岸城）；条件追问由 `slot_extract.v7` 出 options，CODE_MERGE 末尾固定加"都行，你看着办"。回答带 `card {"type":"options"}`，写进会话状态的追问 q 只留文字（下一轮决策请求要读）。
   - **问某一家的团购**（第 11 节原来第一条）：CODE_JOIN 看到上轮有推荐、句子里有团购类词和"第 N 家/这家"（或刚选定一家后问"有团购吗"）时走新分流 `dl` → 读门店 → 认店 → 读团购（单独一条 SQL）→ 团购回答，出 deals 卡片，不记 accept 反馈、不动会话状态。团购不能放进"读门店"的 300 行里：代码节点参数走命令行，超长会报 argument list too long。
2. **Demo**（`demo/`、`web/`，用法见 `demo/README.md`）：FastAPI 单端口 8095，托管 Vite + React + TS 打包结果，`/api/chat` 收完 Coze 的 SSE 后拆成 `{markdown, cards, options}` 返回；左边对话、右边高德 JS API 2.0 地图（店编号标点、锚点、路线折线，点卡片高亮对应的店）。演示模式按 → 回放 `demo/scripts/<名字>.json`，`?from=N` 从断点续放，等待默认压缩到 2–4 秒（`?wait=real` 用真实耗时）。
3. **首页和快捷设置（10-22 晚，用户说启动页草率）**：问候、能力标签、预设问题卡片（12 条轮换）、地图上三个商圈（点了设为位置）和覆盖范围卡片；输入框下有位置、预算、距离、场景四个快捷设置，发送时补在句末、用户消息下方显示标签（规则在 `web/src/settings.ts`，`demo/record.py` 有一份同样的，场景文件用 `set` 字段）；实时模式回答后有快捷追问、顶栏有"我的口味"。之后又做了一轮视觉精修（设计变量集中在 `web/src/styles.css` 开头：暖灰底、橙色渐变主色、统一圆角和阴影；首页渐变标题和光晕、预设卡片错开入场、门店卡片编号和引用样式、路线卡片图标、等待态骨架屏、地图标记选中脉冲、地图底部提示条、顶栏状态点）。产品标识是 `web/src/Logo.tsx`（图钉里一只冒热气的碗，顶栏、首页、头像共用）和 `web/public/favicon.svg`（同一份图形）。实时试过"想吃火锅"+ 海岸城 + 人均 100，推荐 3 家都在 100 以内。
4. **录好的脚本** `demo/scripts/default.json`（10-22 23:30 重录，user `demo-rec-1022233005`，11 步，旧版在 default.bak.json）：饿了（这次走条件追问，选项"海岸城吃火锅 / 深圳湾万象城吃西餐 / 南山科技园吃粤菜 / 都行，你看着办"）→ 点"海岸城吃火锅"→ 第一家和第二家哪个好 → 就第二家吧（芈重山）→ 从科技园过去怎么走 → 有团购吗（选定后直接问，deals 卡片）→ 记住我不吃辣 → 海岸城有什么热门的店 → 新对话：快捷设置点深圳湾万象城 + 约会，说"想吃西餐，最好能拍照"（TOVA 第一，注意里提醒帕德龙辣椒）→ 第一家有团购吗 → 团购券过期了还能退吗。
5. **自动出的视频** `demo/videos/default_1022_2339.mp4`（2 分多钟，1600×900），`demo/make_video.py` 生成，抽帧看过。

**还要注意：**
- 新用户说"饿了"，这次走的是意图澄清（D2 判不清），没走条件追问；两条出口都带选项。想演示条件追问就改场景里的第一句再录。
- 榜单那条的网页结果有一项写得不像店名（"以上三家所在商圈实测下来…"），是博查结果加 hot.v2 的问题，不满意可以重录或把这步删掉。
- 压缩等待是为了录屏节奏，真实一轮找店要 18–33 秒；给导师看时最好说明一句，或者用 `?wait=real`。
- 地图缩放用 `setBounds(…, true)`：带动画的版本会被随后的标记更新打断，路线停在终点附近。
- PAT 11-18 到期（第 4 节）；重启电脑后 Coze 要带补丁的 `-f`（`infra/coze-patch/README.md`）。

### 0.3 10-21 夜做完的事（跨店召回、场景挑店，v0.0.16–v0.0.17）

用户 10-21 说"KB-review 跨店召回可做，然后把 3 也修了后 demo 不迟"。

1. **KB-review 跨店召回**：
   - 槽位提取 `slot_extract.v6` 新加 `need`（用户对店本身的要求：不用排队、量大、能拍照、某道菜做得好）。
   - WF-recall 里和地理编码并行加一路"跨店检索"（KB-review，混合检索 top 20），查询词是"品类 + need + 场景"。need 或场景不为空、或者用户点了具体的菜、说了表外的词时才开（`kb_on`），只说品类节点时不开。
   - "跨店收敛"按段首 amap_id 收敛到店（最多 10 家、每家 2 段），名次传给 SQL：只在满足品类、预算的店里往前排，不越过硬条件。裁剪候选按名次加分（前 3 名 0.8、4–6 名 0.5、7–10 名 0.3）。命中的评价段放在这家店证据的第一条，候选带 `need_hit`。
   - 点名菜或表外词时，跨店检索前 5 名也算品类命中（菜名 2 级、表外词 1 级，`kb_w`）。只说品类节点时不算，避免评价里顺口提一句"比西餐好"的店混进来。复合词取包含的所有词的并集（"烧肉丼饭"→日式烧肉 + 日式简餐）。
   - 做法参照 `research/14` 末节（SQL 候选集 → 评论检索 → 按店收敛 → 只留候选集里的店）。融合用的是名次加分，不是 RRF：基础排序是启发式打分，不是检索列表。
2. **推荐生成不按场景挑店**（E5 t02）：
   - 合并证据给每个候选算 `scene_ok`（场景标签或证据里有场景词，约会的词表加了"仪式感"），对得上的够 3 家时只把这些店交给推荐生成。
   - `recommend.v8` 加了三条规则：说了场景先选 scene_ok；有 need 时优先 need_hit、quote 引用第一条证据；离得近的够选时不选 1500 米外的。
   - 格式化里场景对得上的店排前面。
3. **人均数据修复**：`load_shops` 分批写库时，Coze `update_records` 按每批第一条记录的字段建列，第一条人均为空，同批 24 家人均就写成了 NULL（TOVA、CALIX 都在里面）。改成按字段组合分批写，重写后库里人均为空的是 18 家，和 jsonl 一致。另外，以前 SQL 遇到人均为空时预算条件整个是 NULL，用户没说预算也被算成不满足；现在没说预算时算满足，说了预算时算不满足。卡片上人均为空显示"人均未知"，以前显示 ¥0。
4. **评测**：
   - 跨店召回（`eval/kb_cross_eval.py`，E2 的 60 条找店查询，57 条可评）：答案店进推荐生成的 8 家候选，开 51、关 35；排第 1 的开 33、关 19（`KBX_kbx-1021b.md`）。第一版只加分时是开 39、关 31。第二版改了 kb_w、复合词并集、品类表三处、约会加"仪式感"，其中后两处是看了第一版的漏召回才改的，有针对性。这批查询是从答案店的评价段生成的，结果偏乐观。
   - E1 179 条（加了 n01–n06 带要求的句子）：槽位 F1 0.995，need 6/6；意图 178/179（1 条 /decide 超时）。其余用例里模型填了 need 的有 37 条，多是团购、榜单、FAQ 类，不走召回或无害，没收紧（`E1_e1-kb-1021.md`）。
   - E5 v0.0.17：12/12，只按文本判 9/12；t02 另跑 3 次，4 次都第一轮选中 TOVA（`E5_e5-v17-1021.md`）。v0.0.16 那次 11/12，模拟用户选了评价写"氛围"、但没有约会标签的 CALIX。单轮延迟中位 23 秒。
5. **发布**：WF-understand v0.0.5、WF-recall v0.0.5（v0.0.4 是第一版跨店召回）、CF-main v0.0.17（v0.0.16 是人均修复前）；调试工具 `eval/recall_probe.py "一句话" [--no-kb]` 打印召回各阶段的店。

### 0.4 10-21 晚做完的事（语义扩展、范围检索，v0.0.15）

用户 10-21 说"可以开始进行下一步，也可以看一下类似的开源项目是怎么处理类似问题的"。开源调研结果在 `research/14_品类与区间检索开源调研.md`：品类体系有现成的（高德编码、Overture 等），但没有一个能直接当用户视角的两级表；五个对话推荐/规划项目都没有品类层级展开和价格区间；中文区间解析库都不合用（JioNLP 把"一两百"解析成 201、"以内"报错）。所以品类表、查询展开、区间规则都是自己写的，方法上和调研结论一致（整表进 prompt 受限分类、代码生成展开条件、"左右"±20%、步行每分钟 80 米）。

1. **门店标准品类**：`common/cate_tax.py` 两级表（13 个大类、54 个细类，每个节点带同义词和菜名），打标和查询展开共用这一份。`etl/shop_cats.py` 用 DeepSeek 按店名、高德品类、推荐菜、对上这家店的笔记打标，一批 15 家，最多 2 组，代码校验细类和大类对得上，结果缓存在 `data/processed/shop_cats.json`（只对品类名算哈希，改同义词不用重打）。338 家我全量看了一遍，改了 1 家（Boooard 岸板，见脚本里的 OVERRIDE）；拿不准的有川源顺刺身鱼生（日料还是顺德鱼生）、韩中阁（只凭店名判韩餐）、BOTTEGA VENETA（高德标意式餐厅，判成非餐饮）。7 家非餐饮（便利店、珠宝店、外卖柜等）不进召回。存在 shop 表新加的第 21 列 `std_cate`（`bridge/alter_table.py` 加的，格式 `|西餐|意大利菜|`）；20 列上限只在 Excel 导入时检查。
2. **查询侧展开**：CODE_MERGE 按品类表把用户说的词映射到节点（`cate_tax.query_res`，构建时把源码和查询词表内联进代码节点），SQL 用三个 REGEXP 参数算两级命中 kw_hit：1 = 同品类（std_cate 命中节点，说大类就命中全部细类；没打标的店退回同义词比店名和高德品类），2 = 店名或推荐菜里有用户点名的菜（"意面=意粉=pasta"一组同义说法）。"粤菜"带上潮汕菜、客家菜。裁剪候选里有 2 级命中的店时，品类条件就是"做这道菜"，宁可在这些店里放宽距离、预算，也不退到同品类不做这道菜的店（调试时画像默认人均 100 把做意面的三家都筛掉、只剩披萨店，才加的这条）。
3. **范围**：槽位提取 `slot_extract.v5` 直接出 budget_min / budget_max / dist_max（"一两百"100–200、"100 左右"80–120、"一百多"100–150、"三五百"300–500、"步行十分钟"800 米、"不想走太远"1000 米）。预算本轮说了任何一端就整体替换。SQL 按区间算 cost_ok；用户说了距离时，裁剪候选先在这个范围内找，找不到先放宽距离，再放宽预算、品类。sidecar 别名表加了"万象城"→深圳湾万象城（只说"万象城"时高德给的是罗湖的公交站）。
4. **离线对照**（`eval/cate_recall.py` → `eval/reports/cate_recall_1021.md`）：同一批查询词，旧 LIKE 和新召回各命中哪些店。西餐 7 → 20、日料 25 → 41、韩餐 6 → 13、粤菜 7 → 28、面馆 2 → 24、东南亚菜 0 → 10；丢掉的大多是"中餐"以前靠高德"中餐厅/火锅店"命中的火锅店，以及"泰国菜"以前靠"泰国/越南菜品餐厅"命中的越南粉店，都符合预期。
5. **评测**：E1 173 条（新加 r01–r10 区间、距离、品类展开，"左右"两条金标改成区间）槽位 micro F1 0.99，budget_min、dist_max 全对；意图 170/173，错的 3 条都是 /decide 超过 10 秒退回意图节点（`E1_e1-range-1021b.md` 末尾有说明）。第一次跑时 s09 的槽位整段丢了（模型在追问里用了英文双引号），加了逐字段兜底后重跑。E5 11/12：判分的品类条件改成同时认 std_cate，只按高德文本判是 8/12，差的 3 个都是选中的店品类对、高德文本里没有品类词（陶陶居、天天天妇罗、绵绵冰）；t02 品类过了，没过的是约会（0.3 第 2 项修了）。单轮延迟中位 31 秒，比上一版的 17 秒长，原因没查清（`E5_e5-range-1021.md`）。
6. **发布**：WF-understand v0.0.4、WF-recall v0.0.3、CF-main v0.0.15；shops.jsonl 加了 std_cate 后草稿表、线上表都重写了（改前的备份 `data/processed/shops.before_stdcate.jsonl`）。

### 0.5 10-21 下午做完的事（提及对齐、改口、"你看着办"、检索补丁，v0.0.14）

提及对齐重写（`etl/ugc.py` 的 name_match + loc_check + align 四处共用，对上 228 → 242 条，门店 325 → 338，KB-review 整库重建 232 段，逐条差异在 `data/processed/align_diff_1021.md`）；本轮改口当轮不再当默认值（第 8 节）；条件追问只问一次、"你看着办"直接按默认推荐（第 7 节找店那条）；Coze 检索补丁上线（第 10 节、`infra/coze-patch/`、`eval/reports/E2_patch_compare_1021.md`）。细节在记忆 `local-life-agent.md` 10-21 下午那段。

### 0.6 改流程的固定步骤

节点源码在 `bridge/flow_code.py`。
   - 只改主干（分流、推荐生成、格式化）：改 `build_cf_main.py` 或 `prompts/` → `build` → `debug_run.py` → `release v0.0.x`。
   - 改子工作流：改 `flow_code.py` 或 `build_subflows.py` → `build_subflows.py build 名字` → `build_cf_main.py build --draft`（主干读子工作流草稿）→ `debug_run.py` → `build_subflows.py release 名字`（版本号自动加 1，写进 `sub_versions`）→ `build_cf_main.py release v0.0.x`（锁新版本）。**不要拿 `--draft` 的画布去发布**，发布版会跟着草稿变。
   - 最后用 `chat_api.py` 换 user_id 测发布版。当前版本：CF-main **v0.0.19**（下一个 v0.0.20），WF-understand v0.0.7，WF-recall v0.0.7，WF-compare v0.0.3，WF-route v0.0.3，WF-hot、WF-memwrite 各 v0.0.2，WF-faq v0.0.1，WF-consolidate v0.0.6（v0.0.5 误发成和 v0.0.4 相同的内容，`build_memory.py release` 以前不先保存画布，已改）。

## 1. 现状（框架 v0.9）

导师任务（`Agent1/instructions.txt`，12 条加"用户画像长短期偏好"）的进度：

| 能力 | 状态 |
|---|---|
| 找餐厅、选套餐 | 完整链路：槽位（含口味/忌口、预算区间、距离、对店的要求 need）→ KB-review 跨店检索 ‖ SQL 召回（标准品类展开、菜名两级命中）→ 逐级放宽 → KB-review 证据 → 按场景过滤和重排 → 推荐生成（引用小红书原话、正反面、团购）→ 格式化 |
| 长期记忆、用户画像 | 每轮读画像、抽偏好和反馈；"记住/忘掉"当轮生效；本轮改口（抽取给了 target_id）当轮起本会话内不再当默认值；其余异步整理（WF-consolidate）后生效；SAGER 式推荐策略 |
| 对比评价、路线规划、看榜单 | 已接，见第 7 节 |
| 偏好管理 | 列出、记住、忘掉 |
| FAQ、闲聊 | WF-faq：检索 KB-faq（66 条）后回答，v0.0.13 |
| 数据 | 南山三个商圈 338 家店，170 家有笔记；小红书 200 篇；对上门店的店铺提及 242 条（10-21 对齐重写后），KB-review 232 段；团购 20 家来自笔记原话，318 家合成并标注；每家都有标准品类（10-21，13 大类 54 细类） |
| 模型 | DeepSeek V4.1 Flash（`deepseek-flash`，经 LiteLLM 网关），10-20 从 Claude Opus 5.5 换过来 |
| PRD | 初稿在 Claude Docs 文档"本地生活推荐 Agent PRD"（https://claude.ai/code/artifact/9b289b11-2451-4171-aafe-6ca074b77eb0），10 节，含功能清单和用户流程图；用户看过，待确认 5 条已定（见第 3 节） |
| 编排 | 主干 + 6 个子工作流（WF-understand / recall / compare / route / hot / memwrite）；决策层接了 D2 意图、D1 反馈、D4 长期稳定性（sidecar `/decide`，plain 后端） |
| 评测 | E1 意图/槽位、E2 知识库召回和切分对照（含检索补丁前后对照）、E3 长期记忆、E5 端到端，`eval/`；10-21 下午在 v0.0.14 上重跑了 E1、E3、E5，晚上在 v0.0.15、夜里在 v0.0.17 上重跑了 E1、E5，另有品类召回离线对照（`eval/cate_recall.py`）和跨店召回评测（`eval/kb_cross_eval.py`）；E4 校准、E6 个性化没做 |
| 网页 Demo | 10-22 做完（`demo/`、`web/`），演示回放 + 实时对话，见 0.2 |
| 没做 | 决策层的 D3、D5–D8 |

10-20 一天的版本：v0.0.5 口味槽 → v0.0.8 长期记忆（含修对话挂住）→ v0.0.9 对比/路线/榜单 → v0.0.10 三商圈数据和团购 → v0.0.11 拆子工作流、接决策层 → v0.0.12 模型换成 DeepSeek V4.1 Flash。10-21：v0.0.13 接 WF-faq → v0.0.14 本轮改口当轮生效、条件追问只问一次（WF-understand v0.0.3）→ v0.0.15 标准品类、预算区间和距离（WF-understand v0.0.4、WF-recall v0.0.3）→ v0.0.16 KB-review 跨店召回、按场景过滤候选（WF-understand v0.0.5、WF-recall v0.0.4）→ v0.0.17 人均数据修复、人均未知的显示（WF-recall v0.0.5）。

## 2. 先读什么

| 文件 | 内容 |
|---|---|
| PRD（Claude Docs，链接见第 1 节） | 产品要做什么：场景、功能清单、用户流程图、规则、数据、异常、指标、待确认；用文档工具读（`anthropic-skills:docs` 技能），不要 web fetch |
| `Agent1/README.md` | 组成、常用命令、踩坑、已知缺口 |
| `Agent1/DESIGN.md` | 设计 v0.3：架构、表、知识库、工作流清单、决策层（4.5）、记忆（4.6，末尾有实现状态）、异常、评测、里程碑。数据源部分已过时，以本文件第 5 节为准 |
| `Agent1/bridge/COZE_API_NOTES.md` | Coze 内部接口速查，加上"运行时行为"一节（按用户隔离、SQL 参数、并行、流式挂住、HTTP 10 秒超时） |
| `Agent1/research/13_抖音本地生活数据渠道复核.md` | 抖音/美团团购渠道、数据服务商、同量级开源项目怎么拿数据 |
| `Agent1/research/00_方案草案.md` | 调研合成结论；01–13 是原始报告 |
| 记忆 `local-life-agent.md`、`deepseek-main-no-opus.md` | 本项目全部决定和坑的流水；模型和优先级的规矩 |
| 记忆 `doc-writing-style.md`、`research-discussion-style.md` | 写文档和讨论的规则 |

## 3. 用户已经定下的事（不要再问）

- 运行时用本机开源 Coze Studio。城市深圳，范围南山：海岸城（后海）、深圳湾万象城、南山科技园。
- **模型：一律 DeepSeek V4.1 Flash，不跑 Claude Opus**（用户 10-20 夜：太贵）。key 在 `keys/ds.txt`，API 名 `deepseek-flash`（官方 2026-10-17 升级到 1017 版，我查过更新日志）。Claude Opus 5.5 还留在网关（`claude-opus-5-5`）和 Coze（100002）里，只在用户明确要求时用。豆包（火山方舟）已开通并经网关测通，README 宣称支持豆包即可。
- **MVP 优先**：先跑通完整链路，稳定复现和对齐 Claude 的表现以后再做（对齐靠结构设计和 checkpoint）。延迟用户说不是问题。
- **PRD 待确认的 5 条（10-20 定）**：范围只做餐饮、南山三个商圈，Demo 够用（以后按用户定位取周边范围也能做）；合成团购标注后可以用于演示，不买第三方会员；单说一个地名时先问一句是对的，可以再加一个确认框让用户点选；验收标准等基线跑出来再定；延迟不是问题。
- 持续学习做 SAGER 式每用户策略文本，错题本可选；GEPA 由我决定，只用在意图和槽位两个节点。
- "jev"指 TypeSafe 的 Jev 决策模型，**先不用**；决策层保留兼容接口，后端用普通 LLM。
- **项目不商用，信源不考虑合规；数据可以来自第三方，以图文为主（导师 10-20 确认）。** 抖音官方团购数据个人拿不到，不再追。
- 我的边界：不破解签名、不绕过登录墙和验证码；公开页面低频抓取可以；用户本人账号的登录态可以用，只读，不碰发布/评论/点赞。
- ~~小红书 100 篇/天不是硬上限~~。**10-21 起暂停一切真实爬取**：用户账号收到警告，测试和修数据只用现有数据，要新数据先问（记忆 `no-live-crawling.md`）。
- 不做交叉验证，测试小规模。评测按乐观口径看就行（用户 10-21 夜："我说了就是以乐观为标准的"），偏差在报告里注明一次，不用反复强调。
- 参照对象选同量级开源项目，不拿企业级产品比。
- key 放在 `Remote_Intern/keys/*.txt`，`python -I infra/make_env.py` 生成 `infra/.env`（会保留表外的行，如 COZE_PAT）；不打印、不回显 key。现有：gaode、webjs（两行）、bocha、huoshan、volcano、baidu（千帆，未充值能用）、claude、openrouters、jutuike.com（已无用）。

## 4. 系统现状

| 组件 | 位置 / id | 说明 |
|---|---|---|
| Coze Studio | localhost:8888，`Remote_Intern/coze-studio` | 启动见记忆 `coze-studio-local.md`，**10-21 起要多带检索补丁的 `-f`**（`infra/coze-patch/README.md`）；coze-server 镜像是 `coze-studio-server:la-v0.5.1-p1`（v0.5.1 + 检索补丁，源码 `Remote_Intern/coze-v0.5.1`）；账号在 `Agent1/local-account.txt` |
| LLM 网关 | 容器 lifeagent-litellm，:4000，`infra/litellm.yaml` | 模型组 `deepseek-flash`（在用）、`claude-opus-5-5`（不用，丢掉 temperature/top_p/top_k）、`doubao` |
| sidecar | 容器 lifeagent-sidecar，:8090，`sidecar/app/main.py`（改完要 `up -d --build sidecar`） | `/health`、`/decide`（plain；请求和结果记在 `data/sidecar/decide.sqlite` 的 decide_log）、`/geo`（高德地理编码 + POI 兜底 + 片区别名）、`/route`（步行/驾车/公交并发）、`/hot`（博查，按天缓存）、`/mem/touch`、`/mem/consolidate`；挂载 `lifeagent_ids.json` 读工作流 id；用 COZE_PAT 调 Coze OpenAPI |
| Coze 模型 | model_type **100003** DeepSeek V4.1 Flash；100002 Claude Opus 5.5（都经网关） | 在 `flow_code.py` 的 `MODEL_TYPE` 一处改，主干、子工作流、WF-consolidate 都从这里取；sidecar 决策层的模型是 compose 里的 `DECIDER_MODEL`。注册新模型用 `bridge/register_model.py`。豆包没在 Coze 里注册 |
| 智能体 | bot_id 7698241214255988736 | "抖音本地生活推荐"，对话流模式，主干 CF-main；草稿里的智能体模型 10-20 也改成了 100003 |
| 主干对话流 CF-main | 7698242979080699904 | **v0.0.19**（10-23：多维评分、推荐解释、条件来源；WF-understand v0.0.7、WF-recall v0.0.7、WF-compare v0.0.3、WF-route v0.0.3）；`bridge/build_cf_main.py`，画布另存 `canvases/cf_main.json` |
| 子工作流 ×6 | `lifeagent_ids.json` 的 `workflows.understand/recall/compare/route/hot/memwrite`，版本在 `sub_versions` | WF-understand v0.0.5、WF-recall v0.0.5，其余各 v0.0.2；`bridge/build_subflows.py`，画布另存 `canvases/wf_<名字>.json`；Coze 里名字是 la_understand 等 |
| 记忆整理 WF-consolidate | 7698668801872625664 | v0.0.6（v0.0.3 加了 D4；v0.0.6 换 consolidate.v2、候选行自带 target_id 时 add 按 update 办）；`bridge/build_memory.py`，画布另存 `canvases/wf_consolidate.json` |
| 实验工作流 | `lifeagent_ids.json` 的 `wf_partest`、`wf_sql_admin` | partest 是并行实验用的，可删；sql_admin 给 `sql_exec.py` 用 |
| WF-faq | `lifeagent_ids.json` 的 `workflows.faq`（la_faq），v0.0.1 | 检索 KB-faq 后回答，主干 other 分支用 |
| 评测专用 | 知识库 e2_auto / e2_fix200 / e2_note / e2_mention / e2_mention_np，工作流 la_e2_probe、la_faq_probe；id 在 `eval/data/e2_kbs.json`、`eval/data/faq_probe.json` | E2 切分对照用，不影响主干；E3、E5 跑出来的 e3-* / e5-* 用户的记忆和会话行留在线上表里 |
| 表、知识库 | `Agent1/lifeagent_ids.json` | shop 338 行（21 列，10-21 加了 std_cate）、user_memory、session_state；KB-review（232 段，10-21 整库重建）、KB-faq（66 条）在用，KB-deal/rank 空（团购放在 shop.deals） |
| PAT | `infra/.env` 的 COZE_PAT | 10-19 创建，30 天有效，**11-18 前要换**；换完 `docker compose ... up -d sidecar` |

**重启电脑后：** Docker Desktop 不会自启，手动开（`C:\Program Files\Docker\Docker\Docker Desktop.exe`），容器会自动拉起；8888 返回 502 就 `docker restart coze-web`。Docker 偶尔整体卡死（API 全返回 500），十几分钟能自己恢复，或重启 Docker Desktop。sidecar 重启会丢掉还没触发的记忆整理计时（candidate 行还在，下次 touch 时一起整理）。

## 5. 数据与信源

| 数据 | 来源 | 状态 |
|---|---|---|
| 门店、评分、人均、营业时间、推荐菜 | 高德 POI 2.0 | 338 家（三个锚点周边各 75 家 + 从笔记补进来的）。个人 5000 次/月，10-20 累计约 600 次（估算），10-21 对齐重写又用了 426 次；地理编码和关键字搜索结果缓存在 `data/processed/amap_cache.json` |
| **小红书笔记**（正文、互动、前 10 条评论、图片） | `etl/xhs_crawl.py`：Playwright + 独立 Chrome profile（`data/browser/xhs-profile`，用户已扫码登录） | **主力来源**。200 篇，日期 2025-12 至 2026-10。10-20 抓了 185 篇，没遇到验证码，偶发页面打开超时 |
| 点评探店笔记 | `m.dianping.com/discovery/<id>` 普通 GET | 可用，日期多为 2020–2025，只入了 3 条 |
| 抖音图文笔记、视频页文本 | 千帆搜索限定 `www.douyin.com`，`etl/dy_notes.py` | 10-20 试了 10 个查询词，61 条提及**没有一条在南山**（千帆不按地域筛），只拿来当合成团购的格式参照。笔记页本身是反爬挑战，不碰 |
| 团购 | `etl/deals.py` | 笔记里写到的整理出来（20 家，带来源和日期）；没有的按品类、人均、推荐菜合成 2 个并标 `synthetic` |
| 抖音/美团团购结构化数据 | — | 个人渠道已断（聚推客 2025-06-10 下线抖音团购，美团联盟只对企业）。用户 10-20 定了：合成团购标注后可以接受，不买第三方会员 |
| 网页榜单 | 博查（约 0.036 元/次） | 看榜单时实时查，sidecar 按天缓存 |

## 6. 数据管线

```
python -I -X utf8 etl/amap_crawl.py --anchor 深圳湾万象城 --radius 1000 --pages 3   # 扩商圈；默认合并进 shops.jsonl
python -I -X utf8 etl/xhs_crawl.py crawl --kw "..." --kw "..." --per-kw 6 --daily 300  # 只读抓取，会弹出独立 Chrome
python -I -X utf8 etl/xhs_ingest.py            # 抽取店铺提及（common/llm.py 默认 deepseek-flash；以前用 Claude 跑的）
python -I -X utf8 etl/xhs_images.py --auto     # 读图：纯图笔记等
python -I -X utf8 etl/xhs_ingest.py            # 读图结果再抽一次
python -I -X utf8 etl/resolve_mentions.py     # 对齐门店（每次从头算），对不上的查高德补门店（默认三个锚点）；--offline 先估调用量
python -I -X utf8 etl/audit_mentions.py       # 管线关卡：硬性问题不为 0 就退出码 1，不要往下跑
python -I -X utf8 etl/shop_tags.py             # 场景标签、笔记数进 shop
python -I -X utf8 etl/shop_cats.py             # 标准品类打标（缓存 data/processed/shop_cats.json；补了新店后跑，只打没打过的）
python -I -X utf8 eval/cate_recall.py          # 品类召回离线对照，看新店或改品类表后哪些店进出了召回
python -I -X utf8 etl/deals.py                 # 团购：整理 + 合成（缓存 data/processed/deals_cache.json）
python -I -X utf8 bridge/load_shops.py         # 写 shop 草稿表 + 线上表（全量替换）
python -I -X utf8 bridge/load_kb.py [--rebuild]   # 写 KB-review（默认增量，记录在 kb_loaded.json）；对齐规则或提及变了要 --rebuild 整库重写
python -I -X utf8 etl/audit_mentions.py --kb  # 核对 KB 里没有过期段、没有漏写
python -I -X utf8 bridge/sql_exec.py shop "SELECT COUNT(*) AS n FROM shop"   # 核对行数（草稿表）
```

整串参照 `data/raw/ugc/xhs_browser/_pipeline3.sh`（10-20 那次的实际跑法），长任务后台跑、用 Monitor 看 `=== STEP` 和 `STEP-FAIL`。

- **抓取规则：** 每篇间隔 12–20 秒，搜索之间 15–25 秒；按笔记 id 的 FNV 短哈希去重；遇到验证/登录提示、连续 2 次搜索 0 条就停；页面打开超时会重试一次，再失败跳过（搜索页被跳过的关键词要补抓）。标题里有外地城市名的跳过（`BAD_CITY`，故意不放"香港"）。
- **对齐规则（10-21 重写，`etl/ugc.py`）：** 名字证据（`name_match`）：比较前繁简归一（T2S 表）；中文部分较短一方是较长一方的子串，2 个字时要在开头，或"·"前品牌名是对方开头，或开头 2 字相同且整体相似 ≥75（"中文近似"，90 分）；只有品类词的中文部分（`GENERIC_CJK`）不算；拉丁部分按整词比、2 个字符以下的词不算、只共享通用词（`GENERIC_LATIN`）不算；两边都有中文却对不上、或都有拉丁词却一个不共享，算冲突。位置否决（`loc_check`）：分店写外区外市拒；和门店分店名或地址字面一致收；两边都认出商场（`MALLS`，海岸城/保利/天利算相邻）按商场比；都认不出才地理编码比距离（1 公里，片区名 2–3 公里）；没有分店信息时区域写的全是南山以外就拒。同名多家按位置强度和笔记搜索词里的锚点挑。补门店（`resolve_mentions.lookup`）只认 ≥95 分的名字，限离最近锚点 3 公里，更远的记 `amap_id_far`；过滤暂停营业、装修中。见到新的商场、片区、品类词往表里加。
- **当前结果（10-21）：** 532 条提及：对上 242、远处 59、查不到 169、本地同名店被位置否决 10、外区外市 8、没写店名 44；进 KB 232 条（对上的里面 10 条没内容）。
- **团购显示：** 真实团购总显示，写"（小红书 2026-04 提到，以店内为准）"；合成的只在选套餐意图（cid 2）显示，写"（示例数据，非真实优惠）"。合成价格校验在 人均 × 人数 × [0.55, 1.05]。
- **读图：** CDN 签名链接几小时内有效，不带 Cookie 能下；`chat_vision` 的 max_tokens 要 16000。以前用 Claude 读的；DeepSeek V4.1 Flash 的接口声明支持图片输入，但读图和抽取在 DeepSeek 上**都没跑过**，下次抓新笔记时先小批量看效果。

## 7. CF-main v0.0.19 结构

```
读会话状态 → 上轮槽位 → 读画像 → 画像摘要 → WF-understand → 分流
  rec（找餐厅、选套餐）：要追问吗 → 追问 ｜ WF-recall → 推荐生成 → 格式化 → 推荐出口
  pm（偏好管理）：偏好回复
  cmp / nav / dl（对比、路线、问某家团购）：读门店 → 认店 → WF-compare ｜ WF-route ｜ 读团购 → 团购回答 ｜ 认不出店就追问
  hot（看榜单）：WF-hot
  ask（D2 意图拿不准）：意图澄清
  acc（D1 判成选定上轮第 N 家）：确认选择
  other（FAQ、闲聊、其他）：WF-faq（v0.0.13 起）
→ 汇总（回答 + 要写的会话状态）→ WF-memwrite → 结束（不流式）

WF-understand：开始 → [决策请求 → 决策(sidecar /decide) ‖ 意图识别(Coze) ‖ 记忆抽取 ‖ 槽位提取] → 汇合、合并槽位（合并槽位等记忆抽取和槽位提取两路）
WF-recall：    [地理编码(sidecar) → 解析坐标 ‖ 跨店检索(KB-review) → 跨店收敛] → SQL 召回（品类两级命中、预算区间、跨店名次）→ 裁剪候选（逐级放宽，先放宽距离）→ 证据召回(批处理) → 合并证据（按场景过滤）
WF-compare：   对比证据(批处理) → 挂证据 → 对比生成 → 对比格式化
WF-route：     路线(sidecar) → 路线格式化
WF-hot：       [榜单搜索(sidecar) ‖ 本地热度] → 整理 → 榜单回答
WF-memwrite：  准备 → [要写会话状态吗 → 清/写会话状态 ‖ 写记忆 → 删记忆 → 读用户号 → 通知整理(sidecar)]
```

- **会话状态怎么写：** 各分支出口是一个打包节点（`CODE_PACK`），输出回答和 stage/slots/cands/shown/q/turn。汇总节点每组取第一个非 nil；组末尾放 WF-understand 的 `empty`（恒为空串）兜底。stage 为空的分支（对比、路线、榜单、偏好、闲聊）WF-memwrite 不动会话状态，和 v0.0.10 一样。stage 取值：`collect`（槽位追问）、`recommend`、`clarify`（意图澄清）、`accepted`（已选定）。
- **拆分带来的延迟变化：** 槽位提取从分流后挪进 WF-understand 和意图并行，找店一轮少一段串行 LLM 调用；代价是非找店意图白跑一次槽位提取。WF-understand 要等四路里最慢的一路。10-20 用 node_execution 统计：理解阶段中位 6.5 → 5.4 秒、P90 11.3 → 7.4 秒（拆分前 30 轮找店，拆分后 39 轮各种意图）；理解 + 召回中位 8.7 → 9.2 秒，基本持平，子工作流的调度开销把省下的抵掉了。一轮的大头仍是推荐生成（11–27 秒）。拆完后 `stress_run.py` 连跑 24 轮全部成功（status 2），没有挂住。
- **决策层（D1、D2）：** 决策请求把用户输入、上轮推荐、上一轮的追问（stage 是 collect/clarify 时）拼成一次 `/decide` 请求，问 intent（choice 八类）、unclear（noul）、有上轮推荐时再问 feedback（accept/reject/neutral）和 target（第几家）。汇合节点：
  - 意图用 `/decide` 的结果；最高概率 < 0.6 或 unclear ≥ 0.6 → ask；上一轮刚追问过就不再追问。`/decide` 失败（10 秒超时、出错）时用 Coze 意图节点。
  - feedback 置信度 ≥ 0.7 才写反馈行，原因取记忆抽取给的那句，没有就用用户原话；`/decide` 没答时用记忆抽取的 feedback。
  - accept 且指明第 N 家、意图是找店/套餐/对比/闲聊时 → acc：回确认，会话里的上轮推荐收窄成这一家；下一轮问"怎么去""它怎么样"没点名时认店就取它；stage=accepted 的下一轮不再重复记 accept。
  - `dec_note` 记一行"decide/coze、cid、conf、unclear、coze_cid、反馈来源"，在 WF-understand 的输出里，`debug_run.py` 会打印；两边意图对不上的情况可以从这里和 decide_log 对照。
- **决策层（D4）：** 见第 8 节。

- **找店：** 槽位提取用文本输出，合并槽位里解析。合并槽位做这几件事：品类按标准品类表展开成三个正则和命中权重（`cate_tax.query_res`，v0.0.15 起，见下一条）；预算区间本轮说了任何一端就整体替换；场景归一成 `scene_tag`；画像作默认值，但本轮记忆抽取给了 target_id 的条目先拿掉（第 8 节），并输出过滤后的 `profile_eff` 给推荐生成和 WF-compare；条件追问只问一次：上一轮已经追问过（stage=collect）或槽位提取判 `delegate`（"你看着办"）时不再问，地点按画像常去、没有就海岸城，品类放开，slots_eff 里带 `_assume`，回答开头写明假设。SQL 召回取 5 公里内 30 家（排除 std_cate 是非餐饮的），带 deals；裁剪候选逐级放宽（都满足 → 放宽距离 → 放宽预算 → 放宽品类 → 都放宽；距离只在用户说了范围时参与），按评分、距离、场景、笔记数、口味软加减分、点名菜（kw_hit=2 加 1 分）取 12 家。证据召回每家查 KB-review（查询词"品牌名 + amap_id"，按段首 amap_id 过滤，最多 3 条）。合并证据把跨店检索命中的段放进证据第一条，算 scene_ok，场景对得上的够 3 家时只留这些店，留 8 家。推荐生成用 `recommend.v8.md`（有 `_assume` 时挑不同品类；说了场景先选 scene_ok；有 need 先选 need_hit；近处够选不选 1500 米外）；没指定品类时 CODE_TRIM 同一大类（std_cate 的第一个大类）最多留 2 家。候选和卡片里的 cate 显示标准细类，没打标的店显示高德品类。格式化校验原话必须在证据里找得到，理由为空的条目当作没选。DeepSeek 下一次 18–40 秒（Claude 时 22–53 秒）。
- **品类和范围（v0.0.15）：** 品类表只有一份 `common/cate_tax.py`（打标、查询展开、离线对照都从这里取）。SQL 的 kw_hit：std_cate 命中节点算 1；没打标的店，店名或高德品类命中同义词展开算 1；店名或推荐菜命中用户原词算 1，原词是表里的菜名时算 2（`dish_w`）。具体的菜放细类的菜名列表、不要放同义词，放进同义词就变成整类命中（"螺蛳粉"命中全部粉面店）。有 2 级命中的店时，逐级放宽只在这些店里放宽距离、预算。预算 `cost >= bmin AND cost <= budget`；人均为空的店（18 家）没说预算时算符合，说了预算时算不满足（10-21 夜改；以前整个条件是 NULL，没说预算也算不满足）。跨店检索：名次 kb_pos 进 SQL 排序和裁剪加分；点名菜或表外词时前 5 名按 kb_w 算品类命中（`cate_tax.query_res` 的说明）。
- **口味（v0.0.5）：** `taste` 槽，裁剪时软加减分（词表在 CODE_TRIM），由推荐生成按推荐菜和证据判断；只凭品类推断的要写成"粤菜整体偏清淡"；"过敏、不吃 X"按硬条件，主打 X 的店不选。
- **对比、路线（v0.0.9）：** 认店是代码，按上轮编号（第一家、前两家、这家）和店名（全名、去首字如"炳胜"、长店名前 3–4 字如"八合里"）认出最多 3 家。对比用 `compare.v1.md`，固定维度出表格。路线起点按这个顺序取：问题里的"从 X 过去"、会话商圈、画像常去商圈、海岸城；sidecar 并发查，约 3 秒。
- **榜单（v0.0.9/v0.0.10）：** 博查结果 + 本地热度表（笔记最多的 40 家，带地址），`hot.v2.md` 要求按地址筛商场、写明来源站点和日期、摘要没写店名的不猜。
- 场景同义词表有两份：`etl/shop_tags.py` 的 `SCENE_SYN` 和 `build_cf_main.py` 的 CODE_MERGE，改一处要同步另一处。
- 有歧义的片区名在 sidecar `ALIAS` 里改写（"科技园"→南山科技园，10-21 加了"万象城"→深圳湾万象城），查之前先去掉"附近、一带"等后缀，见到新的往里加。

## 8. 长期记忆和用户画像（v0.0.8）

按 DESIGN 4.6 实现。user_memory 按用户隔离：Coze 自动按隐藏列 `bstudio_connector_uid` 过滤，这个值就是 OpenAPI 的 `ext.user_id`。

**每轮（CF-main 内）：**
- 记忆抽取（`memory_extract.v2.md`，10-21 起"不太能吃、少吃、怕"归 taste，改口要给 target_id）和意图识别并行，输出偏好（key 限 avoid/taste/cuisine_like/cuisine_dislike/budget/area/scene，带 op、explicit、durable、target_id、原话）和对上轮推荐的反馈（accept/reject + 原因）。
- 汇合节点决定写什么：用户明说"记住/忘掉"的当轮生效；durable < 0.3 的（"今天想吃辣"）不进长期记忆；其余写 candidate；反馈写 kind=feedback 行，带选中店和同屏没选的店。
- 画像只作默认值（常去商圈、人均上限、口味），本轮说了以本轮为准，不写回会话槽位；忌口总是带上。用了画像会在回答开头写"按你的长期偏好：……"。
- **改口（10-21）：** 优先级是本轮 > 本会话 > 长期。本轮记忆抽取给了 target_id 的画像条目，合并槽位当轮就拿掉（显式不显式都算）；之后的轮次由画像摘要按本会话的候选行（`evidence.target_id`、`conv` = 当前会话）跳过。被跳过的条目在 prof_lines 里标"（本会话已改口）"，记忆管理视图（active_json、偏好管理回复）照旧显示。兜底：本轮口味里正面提到的东西（"能吃辣"）不按画像忌口排除。长期记忆等异步整理，整理时候选行自带 target_id 而 LLM 判 add 的按 update 办（旧条目标 superseded）。

**异步整理（WF-consolidate）：** sidecar 收到 `/mem/touch?uid=` 后，用户安静 120 秒（`MEM_IDLE_S`）再用 OpenAPI `/v1/workflow/run` 带 `ext.user_id` 跑一次。LLM（`consolidate.v2.md`）把候选按同义归组、对照已生效偏好判 add/keep/update/delete、给 stable，并按反馈改写推荐策略；代码按规则定状态。
- 提升规则（MATE 两信号）：支持次数按**不同会话**计，一致性 = 支持 / (支持 + 矛盾)；支持 ≥ 2 且一致性 ≥ 0.67，或 stable ≥ 0.8，才生效。没提升的留着下次再归组，30 天没提升的标 expired。budget、area 是单值，新的生效时旧的标 superseded。阈值是初值，没用评测集调过。
- 手动触发：`python -I -X utf8 bridge/mem_consolidate.py <user_id>`。
- 调试运行的用户是当前账号、读写草稿表；但通知 sidecar 后整理的是这个账号的**线上**行。端到端测记忆用 `chat_api.py` 换 user_id。

**已测：** 显式记住/忘掉当轮生效；"人均八十左右"两个会话各说一次后生效；单次的"一人食"留在待确认；mem-u2 看不到 mem-u1 的偏好；画像里的忌口在下一次找店时按硬条件生效。

**决策层（v0.0.11 / WF-consolidate v0.0.3）：** D1 反馈判定走 `/decide`（第 7 节），记忆抽取的 feedback 只在 `/decide` 失败时用。D4 在 WF-consolidate 里：整理 LLM 归组后，"稳定性请求"给每组拼一个 noul（带 key、value、用户原话、会话数），`/decide` 的概率代替 LLM 的 stable 进提升规则（≥ 0.8 直接提升）；`/decide` 失败时用 LLM 的 stable，整理日志里写明 `stable D4` 还是 `stable LLM`。每轮"只管本次的不入库"（durable < 0.3）还是记忆抽取节点判断。D3、D5 仍合在整理记忆的那次 LLM 调用里。

**已测（10-20，发布版）：** `split-u1` 一个会话里说"我一直不太能吃辣，海岸城附近人均100以内的火锅"→"第二家就它了"→"怎么去"，120 秒后整理：D4 给 0.92，avoid=辣 直接提升；反馈只记 1 条（"怎么去"那轮没重复记）。调试账号里单次出现的"一人食"D4 给 0.2、"不太辣"给 0.45，留在待定。

## 9. 代码结构

```
Agent1/
  infra/      make_env.py、docker-compose.yml、litellm.yaml、.env.example；coze-patch/（Coze 检索补丁，10-21 已应用，启动命令见其 README）
  eval/       E1/E2/E3/E5、FAQ 阈值、品类召回（cate_recall）、跨店召回（kb_cross_eval、recall_probe）评测脚本，data/（用例）、runs/（原始结果）、reports/（报告），见 eval/README.md
  data/faq/   faq.jsonl（= faq_platform + faq_assistant，66 条）、SOURCES.md（平台规则出处）
  sidecar/    app/main.py
  common/     env.py、llm.py（chat / chat_json / chat_vision，经网关）、cate_tax.py（标准品类表和查询展开 query_res，10-21）
  etl/        note_quality（笔记可信度和笔记口碑，10-23）、amap_crawl、ugc（点评管线 + extract；对齐 name_match/loc_check/align、Amap 缓存、mention_key、kb_eligible）、xhs_crawl、
              xhs_ingest、xhs_images、resolve_mentions、audit_mentions（管线关卡）、shop_tags、shop_cats（标准品类打标）、deals、dy_notes、smoke
  bridge/     coze_client、canvas、workflow_api、build_storage、build_agent、
              flow_code（代码节点源码、SQL、意图表，主干和子工作流共用）、build_subflows（6 个子工作流）、build_cf_main、build_memory、
              load_shops、load_kb、chat_api、sql_exec、alter_table（给表加列）、debug_run、stress_run、mem_consolidate、COZE_API_NOTES.md
  demo/       网页 Demo 服务（server.py、coze_chat.py）、录制脚本 record.py、scenarios/（场景）、scripts/（录好的回答），见 demo/README.md
  web/        Demo 前端（Vite + React + TS + 高德 JS API 2.0），打包到 web/dist
  prompts/    在用：slot_extract.v7、recommend.v8、memory_extract.v2、consolidate.v2、compare.v1、hot.v2、faq.v1（旧版本留作对照）
  canvases/   cf_main.json、wf_<子工作流>.json、wf_consolidate.json（sidecar 令牌已替换成占位符）
  data/       raw/ugc/xhs_browser（笔记 JSONL、日志、管线脚本）、raw/ugc/xhs_img、raw/ugc/dy_notes；
              processed/shops.jsonl、ugc_mentions.jsonl、kb_loaded.json、deals_cache.json、shop_cats.json（标准品类缓存）、amap_cache.json（高德结果缓存）、align_diff_1021.md（对齐修复前后差异），
              以及 *.before_expand.jsonl（10-20 扩充前）、*.before_1021fix.*（10-21 对齐修复前）、shops.before_stdcate.jsonl（10-21 加标准品类前）的备份；
              browser/（登录 profile，不进仓库）；sidecar/（decide、记忆整理、榜单缓存的 SQLite）
```

调试工具：`debug_run.py "问题" 会话名`（调试运行并打印关键节点）、`stress_run.py`（连跑 24 轮查挂住）、`mem_consolidate.py <user_id>`。

## 10. 坑（都验证过）

**Coze**
- 调试读写草稿表，发布后读写线上表；写数据用 `update_records`，`database_id` 传线上 id，`table_type` 1/2。发布前必须调试成功一次（`release` 已串好）。
- `list_records` 一次最多返回 100 行，`HasMore` 还报 False；`load_shops` 已改成删到列不出为止，行数用 `sql_exec.py` 的 COUNT 核对。
- shop 表的 Number 列只收整数（写 26.3 时 update_records 报 500）；`load_shops` 写前取整。
- 自定义 SQL：子串黑名单（UNION、JOIN 等）只查模板，`'{{x}}'` 是绑定参数，值里写什么都行；数值要 CAST；`ST_Distance_Sphere` 可用；同一参数可出现多次。`REGEXP '{{re}}'` 可用（中文正常、不区分大小写），"全部命中"用 `^`、"永不命中"用 `a^`；别名不能用 MySQL 保留字（`AS empty` 报 1064）。
- **update_records 一批按第一条记录建列**：第一条没有某个键，同批其他记录的这个字段写成 NULL（10-21 查出 24 家人均丢了）。`load_shops` 已按字段组合分批。
- **表的列数**：官方说最多 20 列，源码里只有 Excel 导入查这个数，改表接口不查。改表按 alterId 认列，没带上的列会被删，用 `bridge/alter_table.py`（先取现有列原样带上）。
- **结束节点不能开流式**：流式模式下 LLM 节点偶发不回报完成，执行状态停在 1、对话不收尾（10-20 约 1/10）。v0.0.8 起关掉，连跑 24 轮没再出现。排查查 coze-mysql 的 `opencoze.workflow_execution`、`node_execution`，看哪个节点停在 status 2。
- LLM 节点一律文本输出、后接代码节点解析：JSON 输出模式下 Claude 偶尔在 JSON 后接话，解析失败。
- HTTP 节点实际 10 秒超时（画布里设 30 秒没用），sidecar 接口要在 10 秒内返回。
- **HTTP 节点的超时是全局的**：每个 HTTP 节点构建时把自己的超时写进共享的 `http.DefaultClient`，一次执行里所有 HTTP 节点都用最后构建的那个值。v0.0.11 调试时"通知整理"设 5 秒，`/decide` 跑 5.15 秒就被掐断。所有 HTTP 节点统一用 `canvas.HTTP_TIMEOUT_S`（10），不要单独设。
- **意图节点不要用极速模式**：它只认纯数字输出（`strconv.ParseInt`），换 DeepSeek 后报 invalid syntax、整轮失败。现在用标准模式，出错返回 classificationId 0。
- **`/decide` 的答案键**：DeepSeek 回过 `"g0（noul）"` 这种把类型说明带进键名的答案，D4 因此对不上、走了 LLM 兜底。sidecar 提示词已改成"问题 "g0"，类型 noul"并列出合法 id，解析时去掉括号部分再对键。
- **POST 节点不能重试**：重试复用已读空的请求体，第二次是空请求（sidecar 回 422）。`canvas.http_post_json` 已设 0 次重试。
- **子工作流**：`workflowVersion` 为空读草稿、填了读指定发布版，发布主干时不会自动锁；`build_cf_main.py release` 按 `sub_versions` 锁。子执行在 `workflow_execution` 另起一行（`root_execution_id`、`parent_node_id`），内部节点耗时按子执行 id 查 `node_execution`。
- **变量聚合**取第一个非 nil，空串也算有值；没执行的分支是 nil。兜底项放组末尾。
- 代码节点沙箱是 UTC，存时间用 `utcnow() + 8h`。
- 一个节点连出多条边就并行；多入边节点等所有前驱完成；批处理对空列表正常结束。
- KB 文档接口：`/api/knowledge/document/create`、`/list`、`/delete`。
- **知识库切分**（读源码 `chunk_custom.go`）：按分隔符切开后每段单独成块、不合并，超过上限按字数（rune）硬切，`max_tokens` 实际是字数；平台默认（chunk_type 0）是按换行切、800 字、重叠 10%，等于一行一块。
- **检索的三个实现问题（官方 v0.5.1，本机 10-21 起已打补丁修掉，见 `infra/coze-patch/`）**：混合检索（strategy 1）的 RRF（`rrf.go`）对同一块取两路里的最大分而不是求和、同分顺序随机；返回顺序是切片存储顺序（`retrieve.go` 的 packResults 用 `WHERE id IN (...)` 回表没排序）；向量一路最多 4 条（milvus `topK = 4`，节点 top_k 没下传）。检索节点 strategy：0 语义、1 混合、20 全文。换回官方镜像这三条会回来。
- **分数刻度**：向量分数换算成 (cos+1)/2（IP/COSINE），min_score 0.5 等于不设线；FAQ 用 0.74（约 cos 0.48）。
- 同一账号再次登录会把前一个 session_key 顶掉：多线程调内部 API 要共用一次登录；评测脚本在跑时不要另开脚本登录（建工作流、调试运行都会登录）。
- E1 报告表头的"草稿=发布"比的是 Coze 版本记录里的两个 commit id，用接口保存画布不更新它们，不可信；看"草稿=本地画布"。
- 线上用户表在 coze-mysql 的 `opencoze.table_<物理 id>`（`online_database_info` 按表 id 查），列是 f_1、f_2……按建表字段顺序，`bstudio_connector_uid` 是 ext.user_id；mysql 客户端要加 `--default-character-set=utf8mb4`，不然中文是问号。

**数据**
- 小红书：搜索结果约 4.5 秒才出来；搜索页前端会追加 `&type=51`，撞上时报"上下文被销毁"（`safe_eval` 已处理）；Claude in Chrome 插件会把笔记 id 和 token 拦成 [BLOCKED]，不要绕。
- "海岸城""万象城""科技园"各地都有，抓取按标题过滤外地，对齐靠高德深圳限定和距离。
- 高德"科技园"文本搜索第一条是宝安的福海国际科技园，周边搜索也不对，所以用别名表。
- 高德商圈字段很粗（万象城的店也标"后海片区"），按商场筛要看地址。

**本机**
- 经 Bash 传给 Python 的长 heredoc 会出问题（反斜杠、引号），补丁写成 .py 文件再跑；命令里不要出现三个反引号。
- DeepSeek 的推理 token 算在 max_tokens 里：批量生成（合成团购 15 家一批）给 8192 时整批正文为空，一律给 16000。
- `bridge/build_memory.py release` 10-21 前不先保存画布，只发布当时的草稿（v0.0.5 因此和 v0.0.4 相同），已改成先重建保存；`build_subflows.py`、`build_cf_main.py` 的 release 本来就会保存。
- 编译 coze-server：goproxy.cn 下大包偶尔中途断开（unexpected EOF），Dockerfile 用 `|` 分隔多个代理并重试。
- `python -I` 忽略 PYTHONUTF8，打印中文加 `-X utf8`。
- Git Bash 里用 curl 传中文参数不是 UTF-8，sidecar 收到乱码、高德报 ENGINE_RESPONSE_DATA_ERROR，看起来像 key 坏了；测 sidecar 用 Python。
- DeepSeek 批量打标偶尔整批返回空正文（10-21 一批 15 家连续两次），`shop_cats.py` 重跑只补没打上的。
- 槽位提取的 JSON 里，模型偶尔在追问文字里用英文双引号，整段解析失败；CODE_MERGE 已加逐字段兜底，prompt 也要求用「」。

## 11. 已知问题

- ~~"第一家有团购吗"会重新推荐一轮~~（10-22 v0.0.18 加了 dl 分流，见 0.2）。只按编号和"这家"认，"海底捞有团购吗"这种直接说店名的还是走选套餐。
- 认店时同一品牌只取笔记最多的一家，不看离起点远近（多分店时路线可能指到远的那家）。
- 高德的推荐菜有时很怪（木槿钰只有"芝士"），推荐卡片会照抄。
- 记忆阈值是初值；sidecar 重启丢计时，没有每晚补跑。
- KB-rank 是空的。
- 决策层阈值（D2 0.6、D1 0.7、D4 0.8）是初值，plain 后端的概率是模型口头给的、没校准。单说"海岸城"先问一句，用户 10-20 确认是对的，可以再加确认框（选项按钮）。
- ~~品类"西餐"召回不全~~（10-21 晚标准品类修了，西餐 7 → 20 家，见 0.3）。
- ~~推荐生成不按场景挑店~~（10-21 夜：合并证据按 scene_ok 过滤候选、recommend.v8，E5 t02 4/4，见 0.3）。场景判断看标签或证据里的场景词，证据里写"氛围"就算约会对得上，和 E5 判分（只看标签）口径不同。
- **画像默认预算会压过场景**：调试账号画像里有人均上限 100，"深圳湾万象城约会吃西餐"时带约会依据的店都超预算，只能推没有约会依据的店（回答里会写明）。画像只作默认值是有意的，要不要在场景和默认预算冲突时先放宽预算以后再定。
- **跨店检索会误读评价**："不用排队"查出"不排队的话不介意去吃"（意思是要排队）、"吃顿"查出酸奶店。检索只看相似，不判断正反。
- **need 槽会在团购、榜单、FAQ 类句子里填东西**（E1 里 37 条，"有团购""必吃榜"）。不走召回的意图用不上；走召回的团购类会打开跨店检索、偏向评价里提到团购的店。
- **没说距离时，场景加分能盖过一公里多的距离**：只说"深圳湾万象城约会"，推荐里有两家在海岸城（约 0.9 公里）。评分里距离每公里扣 0.5，场景加 0.6。
- **标准品类里拿不准的几家**：川源顺刺身鱼生（日料还是顺德鱼生）、韩中阁（只凭店名判韩餐）、BOTTEGA VENETA（高德标意式餐厅，判成非餐饮）、我杯茶（高德标冷饮店，笔记说是米其林推荐的潮汕菜馆）。印度菜归在"东南亚菜/其他东南亚菜"下，名字不准。
- **点名菜只认店名和推荐菜**："烧鹅"只有 2 家写了烧鹅，做烧鹅但高德推荐菜没写的粤菜馆不会排在前面；有 2 级命中时也不会退到同品类的店。
- **/decide 尾部延迟**：10-21 16 点这一小时 345 次里 5 次超过 10 秒（12 点那批 0 次），超时就退回 Coze 意图节点，E1 因此错了 3 条。
- **E5 判分用了 std_cate**：品类条件同时认高德文本和标准品类，判分和系统用的是同一份标签（338 家人工看过一遍）；只按文本判的数也写在报告里。
- WF-understand 要等四路里最慢的一路。Claude 时偶发 7–10 秒；换 DeepSeek 后实测 1.9–4.7 秒。
- **DeepSeek 守 prompt 规则不如 Claude 严**（10-20 抽看 7 轮找店；用户说先不修，MVP 之后靠结构设计和 checkpoint 对齐）：用户没问也主动提团购；把"清汤做法不辣"这类推断写进"符合"；约会场景挑了轻食店；有一次第 2 家只给了空条目（格式化已改成理由为空就当没选）。D2 给的概率更极端（常见 1/0），意图澄清可能比 Claude 时少触发。
- DeepSeek 下"想吃辣的"推荐生成要 15–33 秒（推理 token 是其他找店句的两倍多），原因没查。
- 通用回复偶尔在开头加"Answer:"（10-20 最近 12 次里 1 次），prompt 的老问题。
- 记忆抽取会把"我现在能吃海鲜了"这类改口判成显式忘掉，当轮就删掉长期条目（10-21 调试看到）。结果对用户是对的，但跳过了异步整理；要不要收紧 explicit 的判定以后看。
- 对齐的商场表（`etl/ugc.py` 的 `MALLS`）只列了本片区和常见的二十来个商场；表里没有的商场靠地理编码，编码点不准时可能误拒或误收。片区名的放宽半径（科技园 3 公里）会收进一些边界情况，例如"金翠皇宫 科技园店"挂到钻石 CBD 大厦店（2.7 公里）。

## 12. 下一步（建议顺序）

1. ~~口味槽~~（v0.0.5）
2. ~~长期记忆和用户画像~~（v0.0.8）。剩 E3 记忆评测调阈值、每晚补跑。
3. ~~对比、路线、榜单~~（v0.0.9）
4. ~~数据扩充~~（v0.0.10）。~~提及挂错店~~（10-21 对齐重写，见 0.3）。暂停抓取期间不再加数据。
5. ~~子工作流拆分和决策层 D1/D2/D4~~（v0.0.11）；~~换 DeepSeek~~（v0.0.12）；~~PRD 初稿~~（Claude Docs）。
6. **评测集 + DeepSeek 基线**（MVP 版，小规模）。**E1 已完成（10-20 夜）**：157 条（手写难例 51 + DeepSeek 按标签生成 106），跑 la_understand v0.0.2。意图系统 155/157、Coze 意图节点基线 142/157（手写难例上 49/51 对 38/51）；该不该先问 154/157；槽位 69 条全对；D1 6/6；理解阶段中位 2.8 秒。生成那一半太容易（两边都接近 100%），区分度全在手写难例上，所以这个数是干净输入下的上限。暴露的问题：s12"你看着办"会反复追问条件；"潮汕大目要排队吗"因 conf 0.55 被意图澄清；/decide 1 次撞 10 秒超时；plain 后端的置信度 131/156 ≥ 0.9，D2 阈值没法靠它校准。
   **E2（10-21）**：同一批 109 篇笔记按 5 种切分各建一个对照库（e2_auto 等，评测专用），探针工作流 la_e2_probe 并行检索。结论：① 主干按店取证据 161 家全部拿到本店段，靠段首 amap_id + 代码过滤，查询里的 amap_id 本身没起作用；② **Coze Studio 的混合检索有实现问题**（`rrf.go` 取最大值不求和、同分随机），同一库上找店 hit@5 混合 67% / 纯语义 95%、问店 53% / 100%，以后 FAQ 等开放检索用纯语义（strategy 0）（10-21 打了检索补丁后这条不成立，混合检索反而更好，见 `eval/reports/E2_patch_compare_1021.md`）；③ 纯语义下"一条提及一段 + 前缀"最好（95% / 100%），平台默认按行切 60% / 82% 且每块只有 35 字，前缀带来 +8 / +25 个点；④ 提及挂错店，全量审计见第 11 节；⑤ 做 FAQ 时又读出两点：知识库节点返回顺序是切片存储顺序（packResults 回表没有 ORDER BY），hit@1、MRR 无意义；向量一路固定只取 4 条（milvus `topK = 4`）。
   **E3（10-21）**：14 段脚本终态全对；m09 暴露"本轮刚改口、回答开头还写旧画像（不吃辣）"。
   **E5（10-21）**：12 个任务全部成功但 10 个一轮就选定，区分度低；3 个只靠记忆的任务都用上了记忆；找店一轮中位 26 秒。
   **10-21 下午在 v0.0.14 上重跑**：E1 163/163（意图）、162/163（该不该问）；E3 16/16（含同会话改口两段）；E5 11/12（t02 没过，见第 11 节）；单轮延迟中位 17 秒。
   **10-21 夜在 v0.0.17 上**：E1 179 条槽位 F1 0.995、意图 178/179；跨店召回评测答案店进 8 家候选 51/57（关跨店检索 35）；E5 12/12（只按文本 9/12），t02 4/4。
   **10-21 晚在 v0.0.15 上重跑**：E1 173 条槽位 F1 0.99、意图 170/173（错的 3 条是 /decide 超时）；E5 11/12（品类判分加了 std_cate，只按文本 8/12；t02 没过，见第 11 节）；单轮延迟中位 31 秒，原因没查清。
   报告里的结论段是人工写的，放在 `eval/data/e*_conclusion.md`。没做的：E4、E6、E5 的关记忆对照、更难的用例。原计划：按 DESIGN 第 5 节 / PRD 第 9 节建 E1（意图、槽位）、E2（KB-review 召回，对应导师清单"测试知识库召回准确率，优化切分策略"）、E3（记忆多轮脚本）、E5（端到端人设任务）的小规模版，脚本放 `eval/`，每次出一份带模型、prompt 版本的报告。只跑 DeepSeek，不跑 Claude 对照。生成评测数据本身也用 DeepSeek。
7. ~~**FAQ**~~（10-21，CF-main v0.0.13）：`data/faq/faq.jsonl` 66 条 = 平台规则 49 条（`faq_platform.jsonl`，依据抖音生活服务规则中心原文，来源和条款号在 `data/faq/SOURCES.md`；没找到官方依据的如实写"规则里没写"）+ 助手自身 17 条（`faq_assistant.jsonl`，系统改了要同步）。`bridge/load_faq.py` 整库重建 KB-faq，一条一段，带"也可能这样问"。WF-faq：纯语义检索 top3、min_score 0.74 → 整理条目 → `faq.v1.md` 回答；主干 other 分支（FAQ、闲聊、其他）从通用回复换成 WF-faq。阈值评测 `eval/faq_eval.py`（66 条改写 + 28 条库外）：0.74 时库内 57/66 检索得到、库外 9/28 误返回（回答节点再把关）。
   **问题 2、3（10-21 下午已修，见 0.3）原方案**：问题 2 根因是"合并槽位"和"记忆抽取"并行、拿不到抽取结果，抽取已经给出 `target_id`（实测"我现在挺能吃辣的了"→ remove 901），但只有显式记住/忘掉才当轮生效；方案是定优先级"本轮 > 本会话 > 长期"，抽取指出被推翻的画像条目时本会话内立即屏蔽（存在会话槽位里），长期记忆照旧等整理，另外把"不太能吃辣"类归到 taste。问题 3 根因是条件追问没有停止规则（意图澄清有"上轮问过就不再问"，条件追问没有）；方案是同一缺口一个会话只问一次，问过仍缺就按默认（画像常去 → 海岸城、品类放开、挑不同品类）推荐并说明假设，槽位提取再加"用户把选择交给你"的判断。
8. ~~**语义扩展、范围检索**~~（10-21 晚，CF-main v0.0.15，见 0.4）：门店标准品类、查询侧品类展开、预算和距离按区间；~~KB-review 跨店召回~~、~~推荐生成按场景挑店~~（10-21 夜，v0.0.17，见 0.3）。
9. ~~**网页 Demo**~~（10-22，见 0.2 和 `demo/README.md`）。
10. 以后再做（MVP 之后）：DeepSeek 对齐 Claude 的表现（例如推荐生成拆成"选店 → 写理由 → 代码校验"几步，每步加 checkpoint）、稳定复现；D6 候选预筛、D7 证据筛选；decide_log 和 Coze 意图节点的影子对照统计；和导师对架构。

## 13. 工作方式

- 用户把我当调研和工程搭档：主动提异议、带数据；用户重申过的一手判断照办；没见过的术语先搜再理解。
- 用户的账号和花钱的事（扫码、付费会员、大批量调用）先问；其余按合理默认先做。**不调 Claude Opus**，除非用户明说。
- 长任务后台跑，用 Monitor 按关键词报进度，不轮询日志；Monitor 30 分钟到期要重挂。
- 跟用户说中文。中文文档去 AI 腔，写完用 `grep -nE "而是|其实|值得|本质|关键在于|真正|——|差别在于|能确定"` 自查。
