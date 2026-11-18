# 连续净收入差额预警 — 测试记录

对应需求：财务按渠道盯连续多日净收入差额；获批日账最高修订号、绝对差额连续三天严格超 100 元才预警、缺日断开；迟到补齐可合并、更正回落可拆分；合并/拆分沿用关联派单，全新段才通知收件箱，重试不重复派单；并发批账日账与预警同事务生效；页面可从原派单回查当时账日与现存区间并标注消失原因。

## 1. 本机环境与限制（如实记录）

- 容器/编排：本机**没有 Docker、没有 pip、无法访问外网**，无法 `docker compose up`，也无法安装 `requirements.txt` 中的 FastAPI/SQLAlchemy/PyMySQL 等。
- 可用：Python 3.11.2 标准库（含 `sqlite3`、`unittest`、`ast`）、Node v22.23.4（仅用于 `node --check` 语法校验）。
- 因此本环境**不能运行真实 FastAPI/MySQL/Redis 全栈**，也无法做浏览器截图；这些项在下方标注为「未在本机执行」，需按 README 用 Docker 复验。
- 为不降低核心规则的验证强度，业务决策引擎 `alert_engine.py` 被刻意写成**零第三方依赖纯标准库**，可直接被 unittest 加载；持久化测试用与 ORM 模型一致的 DDL 在 sqlite 内存库中验证 SQL 契约与端到端演化。

## 2. 已执行并通过的检查

### 2.1 引擎单元测试 `tests/test_alert_engine.py`（20 项，全过）

纯标准库，直接驱动 `src/services/alert_engine.py`，覆盖：

- 最高修订号取值、同修订号重复幂等；
- 阈值为「严格大于 100」（恰好 100 不预警）、负差额取绝对值、连续三日才成段、两日不预警、四日单段；
- 缺日断开、回到阈值内断开；
- 重试/重复对账不产生新派单键、对自身历史为 `kept` 而非新建；
- 迟到补齐断日合并两段、继承两条历史派单、被并入段 `merged` 留痕；
- 相邻但无日重叠不合并（独立新段 + 历史段关闭）；
- 更正使碎片不足三日则关闭并标 `corrected`、更正拆成两个达标段、缺日关闭标 `gap`、高修订号驱动拆分；
- 多渠道独立作用域、不同渠道不同派单键、派单键稳定性。

### 2.2 持久化与端到端演化 `tests/test_alert_persistence_sqlite.py`（8 项，全过）

用与 `models/alert.py` 相同的表结构/唯一约束建 sqlite 内存库，复用真实 `alert_engine` 决策、按 `services/alerts.py` 同构映射落库，验证：

- 首批按渠道产生 3 个全新段 → 3 条派单 + 3 条收件箱通知；
- 相同 `batch_id` 重试幂等：批次/日账/派单/通知数量均不增长；
- 重复自然键 `(channel, day, revision)` 触发唯一约束 `IntegrityError`（对应并发/重复写入兜底）；
- 迟到补齐断日合并：派单与收件箱零增长，现存段合并为 25–31，谱系含 `merged`，旧段置 `merged`；
- 更正回落关闭段：状态 `closed`、谱系 `lost_reason=corrected` 且记录消失账日，不新增通知；
- 高修订号重算生效（取 revision=2）；
- 从原派单键 trace：时间线含 `opened/merged`，现存区间用同一派单键可回查且已延伸；
- 相邻不重叠批次不会误合并，派单数保持 1。

### 2.3 静态校验

- `python3 -m compileall src scripts migrations`：后端全部模块编译通过。
- 对 `frontend/src` 下全部 `.js` 执行 `node --check`：语法全部通过。
- 种子两批演化用引擎手工复刻验证：首批 3 条新派单；次批合作渠道合并（继承 2 派单、0 新通知），直营更正关闭并标 `corrected`。

运行日志：`unit-run.log`（`Ran 28 tests ... OK`）。

## 3. 未在本机执行、需 Docker 环境复验的项

1. `docker compose up -d --build --wait` 五服务健康检查与 `python -m scripts.seed_demo`（含新增 `seed_alerts` 两批数据）。
2. 真实 MySQL 8.0 上的 `FOR UPDATE` 行锁与并发批账串行化、唯一约束行为（本机仅以 sqlite 约束 + 同构事务逻辑验证）。
3. FastAPI 路由鉴权：`POST /alerts/batches` 仅 admin、只读接口对 admin/analyst/viewer 开放、401/403 与审计 `alert.*` 落库。
4. 前端 `#/alerts` 与 `#/alerts/{dispatch_key}` 页面在 Nginx 反代下的真实渲染、过账表单、收件箱标记已读、派单追溯交互与截图。
5. worker/Redis 既有链路与新模块的共存回归（新模块不依赖 Redis，过账为同步事务接口）。

## 4. 复验命令（具备 Docker 的环境）

```bash
cp .env.example .env
docker compose up -d --build --wait
docker compose exec -T backend python -m scripts.seed_demo
# 登录后访问 http://localhost:18217/#/alerts
# 纯逻辑回归（任意带 Python3 的环境，无需第三方依赖）：
cd test-artifacts/gb-1018/tests
python3 -m unittest test_alert_engine test_alert_persistence_sqlite -v
```
