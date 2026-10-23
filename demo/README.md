# 网页 Demo

本机跑，单一端口 8095：`demo/server.py` 托管 `web/dist`，`/api/chat` 代理到 Coze `/v1/workflows/chat`。PAT 和高德 key 从 `infra/.env` 读，不进前端代码。没有访问口令和限流，开隧道对外之前要先加上。

## 启动

```
cd web && npm install && npm run build      # 前端改了才要重新打包
python -I -X utf8 demo/server.py            # http://localhost:8095
```

开发前端时可以另开 `cd web && npm run dev`（5173，/api 转给 8095）。

## 页面

- 首页：问候和能力标签、预设问题卡片（"换一批"轮换 12 条）、地图上三个商圈（点一下设为位置）和覆盖范围卡片。
- 输入框下的快捷设置：位置、预算、距离、场景。发送时补在句末（"想吃火锅（在海岸城附近，人均100以内）"），用户消息下方用小标签显示；句子里已经说了的条件不补，记偏好、问路线、问规则、针对上轮某家店的话整句不补（`web/src/settings.ts` 的 `ctxOf`，录制脚本里有一份同样的 `ctx_of`）。
- 回答后的快捷追问：推荐后给"第一家和第二家哪个好""第一家怎么去"等，选定一家后给"从科技园过去怎么走"等（只在实时模式显示）。
- 顶栏"我的口味"打开画像抽屉（`memview.py` 读线上 user_memory 表，只读）：生效中、待确认（离提升差几次）、推荐策略、最近的选择、记忆怎么维护。实时模式每次回答完自动刷新，有"忘掉""立即整理"；演示模式显示录制时每一步之后的快照。`?profile=1` 打开页面时就展开。
- 找店回答上方是"为什么是这几家"：每个条件的来源（本轮 / 本会话 / 长期偏好 / 默认假设）、筛选漏斗、口味偏好的影响。每家店右上角是综合分，"评分明细 · 评价来源"展开 10 个维度的分数、权重、评分标准，小红书口碑分，参考笔记（可信度标签、判断依据、链接）。

## 后端接口

| 接口 | 作用 |
|---|---|
| `POST /api/chat` | 代理到 Coze `/v1/workflows/chat`，回 `{markdown, cards, options}` |
| `GET /api/profile?uid=` | 画像（线上记忆表，只读） |
| `POST /api/profile/consolidate?uid=` | 立即整理一次长期记忆（调 sidecar） |
| `GET /api/shop/{amap_id}/notes` | 这家店的笔记口碑和参考笔记（`etl/note_quality.py` 离线算的） |
| `GET /api/scripts`、`/api/scripts/{name}` | 演示脚本 |

## 两种模式

- **演示回放**（默认，`?mode=demo`）：按 → 或空格放下一句。要打字的句子逐字输入再发送；点选项的那一步先高亮按钮再发送。回答是录好的真实输出，不当场调用接口。
  - `?script=default` 选脚本；`?wait=real` 按录制时的真实耗时等待，默认把等待压缩到 2–4 秒。
  - `?from=N` 把前 N 步直接铺好，从第 N+1 步开始放，录屏中途出错时从断点接着录。
  - R 重新开始，Esc 打开设置面板（模式、脚本、等待、进度）。
- **实时对话**（`?mode=live`）：真实调用 CF-main。每个浏览器一个 user_id（存在 localStorage），"新对话"换会话名，长期记忆不变。

## 录制演示脚本

场景写在 `demo/scenarios/<名字>.json`：`say` 是打字的句子，`click: k` 是点上一轮的第 k 个选项（从 0 数），`new_conv: true` 表示这句之前先开新对话，`set` 是这句之前点的快捷设置（如 `{"area": "海岸城", "budget": 100}`，之后一直生效，回放时会有点选动画）。

```
python -I -X utf8 demo/record.py default            # 用新 user_id 真实跑一遍，写 demo/scripts/default.json（旧的备份成 .bak.json）
python -I -X utf8 demo/record.py default --upto 3   # 只录前 3 步
```

每一步之后会存一份画像快照（`profile` 字段），演示模式的画像抽屉用。

录完先看一遍终端里打印的回答，不满意就重录：DeepSeek 每次的输出不一样，榜单的网页结果每天也不同。

## 自动出视频

```
python -I -X utf8 demo/make_video.py default            # 要先启动 server.py；输出 demo/videos/default_<时间>.mp4
python -I -X utf8 demo/make_video.py default --headed   # 弹出窗口录（无头模式地图出不来时用）
```

用本机 Chrome（Playwright 自带的浏览器版本对不上，没另外下载）无头回放：每轮回答出来后滚到这一轮开头，长回答慢慢往下滚，找店回答依次点第 2、3 家展示地图联动；Playwright 录 webm，ffmpeg 转 H.264 mp4（1600×900）。等待用的是压缩模式。

## 回答里的卡片（CF-main v0.0.18）

正文是 markdown，末尾附 ```card JSON，`coze_chat.parse` 把它们拆出来：

| type | 出处 | 内容 |
|---|---|---|
| shop_list | 推荐格式化 | anchor 和锚点坐标；items 里每家店的坐标、地址、评分、人均、距离、理由、符合项、显示出来的团购、综合分 score、维度分 dims；explain 里是条件来源 conds 和筛选漏斗 funnel（v0.0.19） |
| compare | 对比格式化 | 每家店的坐标、评分、人均 |
| route | 路线格式化 | 起终点坐标；步行、公交、打车的时间和距离；折线 poly（步行 2 公里内画步行，否则画驾车，最多 120 个点） |
| deals | 团购回答（问某一家的团购） | 这家店的坐标和团购，合成的标 synthetic |
| options | 意图澄清、条件追问 | 选项按钮的文字，点按钮等于用户说了这句 |
