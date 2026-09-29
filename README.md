# CF Snap

CF Snap 是一个 Codeforces 题面截图识别游戏。玩家只看到去掉标题、题号和场次信息的题面图，需要回答 Contest ID + 题号，或 Round + Div + 题号。项目包含账户积分、筛选题库、共享题别名、双人房间和 Elo rating。

## 已实现

- 注册、登录和安全的 HttpOnly 会话；密码使用 PBKDF2-SHA256 存储
- 简单（≤1200）、中等（1300-1900）、困难（≥2000）及混合题库
- 按 Contest ID、年份和 Div.1 / Div.2 / Div.3 / Educational 筛选
- 两种答案格式；共享题接受所有合法别名
- 单人计时、速度分、连对加分、总积分和正确率
- 6 位房间码双人对战，首答高分，娱乐 / Rating 两种模式
- Elo：前 10 场 K=40，之后 K=24
- SQLite 持久化、Dockerfile、Render 部署配置
- 12 道经过唯一性检查的内置题，包含 `2269E / 2268C` 共享题

## 本地运行

只需要 Python 3.10+，不依赖第三方包：

```bash
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

测试覆盖注册、出题、SVG 题面、四种共享题答案，以及两位玩家创建、加入和完成一轮抢答。

## 题库规则

题面图片由后端从已审核的无标题线索生成，浏览器拿不到问题标题和答案。只有 `active=1` 且 `unique_checked=1` 的题会进入抽题池。新增题目时需要满足：

1. 线索包含完整操作或目标及关键约束，不能只有几个单词。
2. 同一题的 Div.1 / Div.2 Contest ID 都写入 `aliases`。
3. Gym 不入库。
4. 相似的 Easy / Hard 版本必须在约束上能够唯一辨认，否则只保留一个版本。

内置题位于 `cfshot/seed.py`。修改后重新启动应用会幂等更新题库。

批量扩题时，先从官方 API 生成不含 Gym 的元数据骨架，再由编辑补充去标题且可唯一识别的线索：

```bash
python3 scripts/build_catalog.py --min-contest 2000 --output work/catalog.json
python3 scripts/import_pack.py work/catalog.json --dry-run
python3 scripts/import_pack.py work/catalog.json
```

`import_pack.py` 会拒绝过短线索、泄露标题、Gym 链接、重复别名和相似度高于 86% 的线索。只有验证通过的题包才会标记为可出题。

## 部署

仓库包含 `Dockerfile`，适用于 Render、Railway、Fly.io 或普通 VPS。Render 可以直接使用 `render.yaml`；其他平台将容器端口映射到 `PORT` 即可。双人对战使用短轮询，不依赖 WebSocket，因此普通单实例 HTTP 部署即可运行。

当前 SQLite 架构适合早期单实例。准备横向扩容时，应把用户、房间和答题记录迁移到 PostgreSQL，并将房间事件放入 Redis 或 WebSocket 服务。

## 数据来源说明

题号、Round、rating 与标签应以 Codeforces 官方公开 API 为准。Codeforces API 限制为两秒一次请求；题面网页可能触发 Cloudflare，因此正式题库采用审核后缓存，不在玩家每次答题时抓取官网。页面揭晓后会链接回原题。
