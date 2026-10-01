# CharClamp-01 · 炭窑焖烧志

窑场炭窑与焖烧班次台账基线项目（Litestar + SQLAlchemy 2 + Jinja2 + HTMX）。

## 技术栈

| 层 | 技术 |
| --- | --- |
| Web | Litestar · Jinja2 · HTMX CDN · Session 认证 |
| 数据 | SQLAlchemy 2（async） · PostgreSQL 15 |
| 部署 | Docker Compose · Uvicorn |
| 结构 | `domain/` · `infra/` · `web/` 分层（非 Django apps） |

## 路径与端口

- **项目路径**：`d:\work\document\bytecode\claudeCodePro\CharClamp\CharClamp-01`
- **Web**：http://localhost:4750
- **PostgreSQL**：localhost:6150

## 演示账号

| 用户名 | 密码 | 角色 |
| --- | --- | --- |
| `admin` | `123456` | 管理员 |
| `worker` | `123456` | 操作工 |

登录页已预填 `admin` / `123456`。entrypoint 会建表并写入种子数据（窑场 **乌石岗焖烧坞**，窑号如 **坞东-甲 / 坞东-乙 / 河沿-丙**）。

## 主界面：焖烧时间轴

登录后进入全宽 **焖烧时间轴**（不再使用侧栏 + 双 CRUD 列表）：

1. **顶部窑剪影行**：每座炭窑以 SVG 剪影展示；点击某窑用 HTMX 局部刷新下方时间轴，并更新地址栏 `?clamp_id=`；「全部窑」取消筛选。已码窑若持未核销点火许可帖，剪影带「待点火」角标，数量与许可专页对账。
2. **纵向时间轴**：按开始时间倒序列出 `BurnShift`；每条卡片带窑号徽章（再点可开抽屉）、峰值温度、炭品与当前窑态。
3. **侧抽屉（非独立编辑页）**：「登记班次」写入新班次；点窑徽章打开操作抽屉，可标记「已出炭」（受峰值规则约束）。
4. **点火许可专页**（顶栏「点火许可」）：未核销筛选、新开帖、管理员核销。

## 业务规则

### 出炭峰值门槛（既有）

炭窑状态不可设为「已出炭」（`drawn`），除非该窑**最近一条** `BurnShift` 的 `peakTempC` 已记录且 **≥ 400℃**。

### 点火许可帖（IgnitionPermit）

已码窑要写入**第一笔**焖烧班次并进入「焖烧中」前，须先持一张**未核销**点火许可帖。

- 帖字段：炭窑、开帖日、许可编号、值班管理员、核销时刻（可空）。
- **许可编号全坞唯一**，且必须是 **4 到 8 位数字**（DB CHECK 与应用层双重校验）。
- **仅已码窑可开帖**；未核销期间同一窑不得再开第二张（部分唯一索引 `consumed_at IS NULL` 兜底并发）。
- 开帖与核销仅**值班管理员**可操作。
- **登记首笔班次时**：在**同一数据库事务**内 —— 先 `SELECT ... FOR UPDATE` 锁定窑行、确认存在未核销帖 → 写入该帖核销时刻 → 班次入库 → 窑态改为焖烧中，最后一起提交；任一步失败整体回滚，不会出现「核销失败却班次已入库」。
- 已是焖烧中的窑追加班次，不必再持新帖。
- 出炭仍走既有 400℃ 峰值门槛，不受许可影响。
- 两名管理员同时给同一已码窑开未核销帖时，数据库只允许落下一张；败者收到提示、事务回滚，登录态保留，时间轴与许可专页仍可正常打开。

种子数据中 **坞东-乙** 为「已码窑、无帖、无班次」，作为开帖 → 首班 → 焖烧的演示起点。

规则实现：`src/charclamp/domain/rules.py`；约束：`src/charclamp/domain/models.py`（`IgnitionPermit`）

## 快速启动

```bash
cd d:\work\document\bytecode\claudeCodePro\CharClamp\CharClamp-01
docker compose up --build
```

浏览器打开 http://localhost:4750

## 目录结构

```
CharClamp-01/
├── docker-compose.yml
├── Dockerfile
├── entrypoint.sh
└── src/charclamp/
    ├── main.py
    ├── domain/          # models + rules
    ├── infra/           # db + seed + security
    └── web/             # controllers + templates + static
```
