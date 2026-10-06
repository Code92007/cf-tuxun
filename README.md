# CF Snap

CF Snap 是一个 Codeforces 题面截图识别游戏。玩家只看到去掉标题、题号和场次信息的题面图，需要回答 Contest ID + 题号，或 Round + Div + 题号。项目包含账户积分、筛选题库、共享题别名、双人房间和 Elo rating。

## 已实现

- 注册、登录和安全的 HttpOnly 会话；密码使用 PBKDF2-SHA256 存储
- 简单（≤1200）、中等（1300-1900）、困难（≥2000）、最强大脑及混合题库
- 按 Contest ID、年份和 Div.1 / Div.2 / Div.3 / Educational 筛选
- 两种答案格式；共享题接受所有合法别名
- Round 写法的组别可留空；同一 Round 存在多个组别时使用 `1 / 2 / 3 / 4 / 12 / E` 消歧
- 完整题面、文字片段和图片裁剪三类线索；同一题可保留多种问法
- 普通唯一答案题与开放多解题使用独立题池，单人和双人均需显式选择
- 登录用户可选择、拖拽或粘贴截图投稿；管理员审核通过/拒绝，原投稿和图片永久保留
- 单人娱乐 / Rating 模式，支持倒计时、多次尝试、放弃题目和提前结束
- 单人可选择传统对错或 5000 分距离积分；距离积分每题只锁定一次答案
- Rating 单人局采用固定公平题池和 120 秒限时；每次提交至少间隔 5 秒，最多尝试 10 次
- “最强大脑”答错、超时或放弃时不揭晓答案，保护数量有限的题库
- 6 位房间码双人对战，首答高分，支持娱乐 / Rating 模式和主动退出/弃权
- 双人支持抢答与距离积分赛制，可设置单题时间、错答罚时和放弃后的剩余时间
- 每日固定五题挑战、120 秒单题计时、距离积分和当日排行榜
- Elo：前 10 场 K=40，之后 K=24
- 普通用户、审核管理员、超级管理员三级权限；仅超级管理员可任免审核管理员
- SQLite 持久化、Dockerfile、Render 部署配置
- 21 条经过唯一性检查的内置线索，包含 `2269E / 2268C` 共享题

## 本地运行

使用 Python 3.10+。搜题功能使用免费开源依赖，截图文字识别还需要系统安装 Tesseract（Docker 镜像已包含英文语言包）：

```bash
python3 -m pip install -r requirements.txt
python3 app.py
```

打开 `http://127.0.0.1:8000`。

可选环境变量：

```bash
HOST=0.0.0.0
PORT=8000
DATA_DIR=/path/to/persistent/data
COOKIE_SECURE=1
```

生产环境必须使用 HTTPS，并设置 `COOKIE_SECURE=1`。如果部署平台的磁盘不是持久化的，请把 `DATA_DIR` 指向持久卷，否则账户和积分会在实例重建后丢失。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖注册、独立题池、Round 组别消歧、共享题答案、单人/双人距离积分、最强大脑答案保护、每日挑战、限时多次作答、Rating 公平限制、同题多线索投稿审核、管理员分级，以及双人房间的加入、退出和抢答。

## 题库规则

题面由后端从已审核的无标题文字或图片线索生成，浏览器拿不到问题标题和答案。只有 `active=1` 且 `unique_checked=1` 的题会进入抽题池。新增题目时需要满足：

1. 普通模式的文字线索包含完整操作或目标及关键约束；图片裁剪必须有可辨识的结构。
2. 同一题的 Div.1 / Div.2 Contest ID 都写入 `aliases`。
3. Gym 不入库。
4. 相似的 Easy / Hard 版本必须在约束上能够唯一辨认，否则只保留一个版本。
5. 极小片段只进入“最强大脑”，由管理员确认唯一性；同题的多张图使用不同 `canonical_key`，共享 `aliases`。

内置题位于 `cfshot/seed.py`。修改后重新启动应用会幂等更新题库。

批量扩题时，先从官方 API 生成不含 Gym 的元数据骨架，再由编辑补充去标题且可唯一识别的线索：

```bash
python3 scripts/build_catalog.py --min-contest 2000 --output work/catalog.json
python3 scripts/import_pack.py work/catalog.json --dry-run
python3 scripts/import_pack.py work/catalog.json
```

`import_pack.py` 支持 `statement`、`fragment` 和 `image`，允许同题多线索，但会拒绝过短线索、泄露标题、Gym 链接、答案误配和相似度高于 86% 的线索。只有验证通过的题包才会标记为可出题。

首个超级管理员在用户注册后通过服务器命令授予：

```bash
python3 scripts/make_admin.py 用户名 --super
```

超级管理员可在站内“权限管理”页面提拔或撤销普通审核管理员。普通审核管理员只能处理投稿，无权查看权限页面或改变其他用户角色。

## 部署

仓库包含 `Dockerfile` 和 `compose.yaml`。针对 `wannafly.cn` 现有服务器的 Caddy 配置与完整命令见 [`deploy/server-setup.md`](deploy/server-setup.md)；应用仅监听服务器回环地址的 `8024` 端口，不与现有 80/443 服务冲突。Render 仍可直接使用 `render.yaml`。双人对战使用短轮询，不依赖 WebSocket，因此普通单实例 HTTP 部署即可运行。

当前 SQLite 架构适合早期单实例。准备横向扩容时，应把用户、房间和答题记录迁移到 PostgreSQL，并将房间事件放入 Redis 或 WebSocket 服务。

## 数据来源说明

题号、Round、rating 与标签应以 Codeforces 官方公开 API 为准。Codeforces API 限制为两秒一次请求；题面网页可能触发 Cloudflare，因此正式题库采用审核后缓存，不在玩家每次答题时抓取官网。页面揭晓后会链接回原题。

## 超级管理员搜题

“搜题”入口只对超级管理员显示，搜索、进度、回刷控制接口都在后端强制校验超级管理员权限；POST 同时校验 CSRF。支持英文题面片段、粘贴截图按钮、Ctrl/Cmd+V 粘贴截图及拖拽图片，返回最多 20 个候选题、题面摘要和原题链接。

文本采用 SQLite FTS5 倒排索引、BM25 排序及词前缀匹配，再对 OCR、TeX 和数字样例做短语 n-gram 重排。完整题面包含输入、输出与 Note。截图通过 Tesseract OCR 提取英文；公式通过 MathJax + resvg 离线渲染并使用细粒度墨迹向量匹配；原始插图通过 OpenCV 多尺度局部模板匹配，再融合灰度向量候选。数据保存在 SQLite，不需要 ES、Milvus、GPU、付费接口或模型下载。相似度是匹配指标，不是正确概率，短片段和易／难版本仍需核对。公式索引需要 Node.js 和 `npm install --omit=dev --ignore-scripts`，Docker 已包含依赖。已有插图库按最近比赛优先补抓原始插图，失败会保留并重试。

应用首次启动自动开始历史回刷，之后会记住暂停状态。回刷调用官方 `contest.list?gym=false` 获取全部已结束 contest 区比赛，按比赛开始时间倒序；每场通过 `contest.standings` 获取所有题目（不依赖 problemset 是否收录），抓取英文题面和插图。每次请求至少间隔 2.2 秒，每道题提交检查点，失败比赛指数退避重试；重启后继续，每小时刷新目录以补充新结束的比赛。进行中的比赛和 Gym 不抓取。搜索库和游戏题库完全独立，抓取结果不会自动成为游戏线索。

超级管理员可在搜题页查看完成／待处理／失败数量、错误原因，以及暂停／继续回刷。数据保存在 `DATA_DIR/search.db`，同样需要持久化和备份。初始全库需要较长时间，页面上已有题目数量表示实际可搜索覆盖；Cloudflare 拦截会明确显示为失败待重试，不能将仅有元数据视为题面抓取完成。

也可单独在前台运行回刷；进程锁会避免和站内任务重复抓取，站内暂停也会通知命令行任务停止：

```bash
python3 scripts/backfill_search.py
```

本地 macOS 可用 `brew install tesseract` 安装 OCR；Linux 可安装 `tesseract-ocr` 和 `tesseract-ocr-eng`。代码的核心游戏仍可直接运行，图片搜索需要安装依赖；生产环境推荐重建 Docker 镜像后使用。

### GitHub 题库备份与恢复

公开搜索数据以 `catalog/search-corpus/manifest.json` 保存在 Git 中，包含题面、题号、比赛元数据、局部图像向量、离线公式缩略图、原始插图和已完成的比赛标记，不包含账户、会话、投稿、运行时设置或错误日志。应用启动时若搜索库为空，会自动恢复这份快照，再从未完成的比赛继续回刷；已有搜索库不会被快照覆盖。

生成最新公开数据快照：

```bash
python3 scripts/export_search.py
```

生产容器可导出到持久卷后下载并提交 Git：

```bash
docker compose exec -T app python scripts/export_search.py --output /data/search-corpus/manifest.json
```

快照采用一致的 SQLite 读事务和原子文件替换，同一份数据重复导出的文件内容一致。全量回刷结束后应再导出并推送最新快照；版本化快照用于公开题库恢复，账户数据库仍需单独备份。

公开快照按固定的 25 个 contest ID 范围分片，manifest 校验每片 SHA-256；同步时复制整个 `search-corpus` 目录。旧版单 gzip 快照仍可导入。
