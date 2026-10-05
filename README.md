# 微孔板配液规划服务（Microplate Dilution Planner）

给定母液、稀释液、目标孔、中间液容器和移液器能力，计算一份**可直接执行的精确转移计划**：
每一步的来源、目的、体积，以及各孔的最终浓度。

支持两种模式：

- **普通模式**（`POST /plans`）：母液随请求内联声明，一次性求解；
- **库存模式**（`POST /inventory/plans`）：母液以**批次**预先登记（唯一 ID、浓度、初始体积），
  多份计划共享同一批母液；计划可行时按实际转出体积**原子预留**库存，
  预留可**确认执行**（计入已用量）或**取消**（归还库存）。
  发现批次污染时可凭批次号 + 原因发起**不可撤销召回**（`POST /batches/{id}/recall`），
  冻结受影响计划与目标孔清单，并阻断召回后的新预留与待处理预留的确认。

- 体积一律使用**整数纳升（nL）**；
- 浓度一律使用**有理数**（`fractions.Fraction`，输入如 `"2/3"`、`"0.25"`、`"40"`）；
- 中间液可逐级稀释，容器间转移构成的图**不得有环**；
- **目标孔只能是目的，永不作为来源**；
- 每次移液器转移必须满足 `min ≤ 体积 ≤ max` 且为 `step` 的整数倍；
- 来源不得透支、目的容器/孔不得溢出；
- 混合严格按溶质质量守恒（`浓度 × 体积` 线性混合），所有目标的体积与浓度**精确达成**。

无解时返回受阻目标与具体约束，**不会返回部分计划**。非法数值或非法引用以 `400` 明确拒绝。
同一 `X-Request-ID` + 相同请求体重试返回原冻结计划（`200`）；同 ID 异内容返回 `409 Conflict`。

---

## 1. 启动

依赖：Docker 与 Docker Compose v2。

```bash
docker compose up --build
```

启动时会：

1. 拉取 `postgres:16-alpine` 并健康检查通过；
2. 在应用镜像内 `pip install -r requirements.txt`；
3. 应用启动时自动连接数据库并执行 `CREATE TABLE IF NOT EXISTS ...` 建表
   （`plans`、`batches`、`reservations`、`reservation_items`、`recalls`；
   对既有库用 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 补齐批次召回列，无需手工迁移）。

服务地址与端口：

- API：`http://localhost:8000`
- 交互文档（OpenAPI / Swagger UI）：`http://localhost:8000/docs`
- 健康检查：`http://localhost:8000/health`

停止并保留数据：`docker compose stop`；连同数据卷清除：`docker compose down -v`。

---

## 2. 配置项（环境变量）

均位于 `docker-compose.yml`，可通过同名环境变量或 `.env` 覆盖：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SERVICE_PORT` | `8000` | 宿主机映射端口；容器内固定监听 8000 |
| `SERVICE_HOST` | `0.0.0.0` | uvicorn 监听地址 |
| `DATABASE_URL` | `postgresql://planner:planner@db:5432/planner` | PostgreSQL 连接串 |
| `POSTGRES_USER` | `planner` | 数据库用户 |
| `POSTGRES_PASSWORD` | `planner` | 数据库密码 |
| `POSTGRES_DB` | `planner` | 数据库名 |
| `POSTGRES_PORT` | `5432` | 宿主机数据库端口 |

---

## 3. HTTP API

### `GET /health`

返回 `{"status": "ok"}`，同时探活数据库。

### `POST /plans`

请求头：

- `X-Request-ID`（**必填**）：请求号，用于重试幂等与冲突检测。

请求体字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `stocks[]` | array | 母液：`id`、`concentration`（整数或有理数字符串，非负）、`available_nl`（正整数） |
| `diluent.available_nl` | int | 稀释液可用体积（nL，≥0） |
| `wells[]` | array | 目标孔：`id`、`final_volume_nl`（正整数）、`final_concentration`、可选 `capacity_nl` |
| `containers[]` | array | 有容量上限的中间液容器（数量有限）：可选 `id`、`capacity_nl`（正整数）。省略 `id` 时返回 `C0,C1,...` |
| `pipette` | object | `min_nl`、`max_nl`、`step_nl`，均为正整数 |
| `default_well_capacity_nl` | int | 未给单孔容量时的孔容量 |
| `limits` | object | 可选：`max_depth`（默认 3）、`max_search_nodes`（默认 200000）、`max_enumeration_units`、`max_factor` |

**响应：**

- `201 Created`：新计划。体含 `status:"feasible"`、`request_id`、`plan`：
  - `plan.transfers[]`：`step`（自 1 连续编号）、`source`、`destination`、`volume_nl`；
  - `plan.intermediates[]`：`id`、`container`、`concentration`、`produced_nl`、`remaining_nl`、`recipe{high,low,high_fraction}`；
  - `plan.wells[]`：`id`、`final_volume_nl`、`final_concentration`。
- `200 OK`：相同请求号 + 相同内容重试，返回原冻结计划。
- `409 Conflict`：相同请求号但内容不同。
- `422 Unprocessable Entity`：请求合法但**无解**；体含 `blocked[]`（受阻孔与约束代码）、`reason`、`constraints`（全局约束，如 `insufficient_total_solute`、`insufficient_total_volume`、`target_above_max_stock_concentration`、`container_shortage` 等）。无解结果同样被冻结，重试返回 `422`。
- `400 Bad Request`：非法数值/类型/引用（负数、0、浮点浓度、重复 id、移液器区间非法、终体积超孔容量等）。

节点引用的 `kind` 为 `stock` / `diluent` / `intermediate` / `well`。

### `GET /plans/{request_id}`

取回冻结结果（可行 `200`，无解 `422`，不存在 `404`）。

### `POST /batches`

登记一个母液批次。请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 批次唯一 ID（1–64 字符，不得以 `@` 开头） |
| `concentration` | int / string | 有理数浓度（非负），如 `"1000"`、`"2/3"` |
| `initial_volume_nl` | int | 初始体积（nL，正整数） |

**响应：**

- `201 Created`：新批次，体含 `id`、`concentration`、`initial_volume_nl`、`reserved_nl`、`used_nl`、`available_nl`；
- `200 OK`：同 ID 且内容完全相同的重复登记（幂等），返回已存在批次；
- `409 Conflict`：同 ID 但浓度或初始体积不同（`error: "batch_conflict"`）；
- `400 Bad Request`：非法数值/类型。

批次的可用量恒为 `available_nl = initial_volume_nl - reserved_nl - used_nl`。

### `GET /batches` / `GET /batches/{batch_id}`

列出全部批次，或查询单个批次的登记信息与实时 `reserved_nl` / `used_nl` / `available_nl`（不存在 `404`）。

### `POST /inventory/plans`

库存模式配液计划。请求头 `X-Request-ID`（**必填**），语义与 `/plans` 相同。

请求体与 `/plans` 的唯一区别：用 `batch_ids`（已登记批次 ID 列表，至少 1 个、不可重复）
替换 `stocks`；`diluent`、`wells`、`containers`、`pipette`、`default_well_capacity_nl`、
`limits` 完全相同。求解沿用同一精确求解器，批次以其**当前可用量**参与求解。

**响应：**

- `201 Created`：新计划。体含 `status:"feasible"`、`request_id`、`plan`（结构同 `/plans`）、
  `reservations[]`（`{batch_id, reserved_nl}`，即计划实际从各批次转出的体积）、
  `reservation_status:"reserved"`。**预留与计划冻结在同一个数据库事务中原子提交**：
  批次行以 `SELECT ... FOR UPDATE` 加锁后校验可用量，并发提交不会超卖。
- `200 OK`：相同请求号 + 相同内容重试，返回原创建结果（**不会重复预留**）。
- `409 Conflict`：相同请求号但内容不同。
- `422 Unprocessable Entity`：无解或库存不足。体含 `blocked[]`、`reason`、`constraints`；
  库存不足时 `reason` 为 `insufficient_inventory`（竞态下还附 `shortages[]`：
  各批次的 `required_nl` 与 `available_nl`），并附 `inventory[]` 快照说明各批次余量。
  **失败结果同样被冻结，且不占用任何库存**。
- `400 Bad Request`：非法数值/引用；引用未登记批次时 `error` 为 `unknown_batch`
  （不冻结、不占库存，登记后可复用同一请求号重试）。

### `POST /inventory/plans/{request_id}/confirm`

确认执行已预留的计划：各批次对应体积从 `reserved_nl` 转入 `used_nl`（计入已用量）。

- `200 OK`：`status:"confirmed"`；重复确认幂等（`changed:false`，余额不再变动）；
- `409 Conflict`：该预留已取消（`error: "reservation_state_conflict"`）；
- `404 Not Found`：该请求号没有预留记录。

确认后的预留**不可取消**。

### `POST /inventory/plans/{request_id}/cancel`

取消已预留的计划：各批次对应体积从 `reserved_nl` 归还为可用量，**只归还一次**。

- `200 OK`：`status:"cancelled"`；重复取消幂等（`changed:false`，不会重复归还）；
- `409 Conflict`：该预留已确认（确认后不可取消）；
- `404 Not Found`：该请求号没有预留记录。

确认/取消通过预留行的 `SELECT ... FOR UPDATE` 串行化：**重复与并发操作收敛到同一最终状态**
（并发 confirm+cancel 中恰好一个生效，其余收到幂等回放或 409）。

### `GET /inventory/plans/{request_id}`

取回冻结的库存计划（可行 `200`，无解 `422`，不存在 `404`），并叠加**实时**预留状态
（`reservation_status` 与 `reservation_items`）；`GET /plans/{request_id}` 返回的则是
创建时刻冻结的原始结果，两者互补。

### `POST /batches/{batch_id}/recall`

发现某批母液污染时，实验员凭**批次号 + 原因**发起**不可撤销**的召回。
请求体：`{"reason": "…"}`（非空，1–1000 字符）。召回与影响清单在**同一个数据库事务**中
冻结；批次行先 `SELECT ... FOR UPDATE`，再锁定触及该批次的预留行，因此与计划创建、
确认并发时一律按**权威批次状态**裁决（统一“先批次后预留”的加锁顺序，无死锁）。

**响应：**

- `201 Created`：首次召回。体含 `batch_id`、`reason`、`recalled_at` 与冻结的 `report`；
- `200 OK`：同批次**同原因**重复召回，原样返回首次结果（幂等回放，结果永不改变）；
- `409 Conflict`：该批次已召回但**原因不同**（`error: "recall_reason_conflict"`，
  附 `existing_reason`；召回不可撤销、不可改因）；
- `404 Not Found`：批次从未登记（`error: "unknown_batch"`）；
- `400 Bad Request`：原因缺失/空白/超长。

`report` 仅纳入**实际取用该批次**（存在 `reservation_items` 记录）且仍处于
**预留（reserved）或已确认（confirmed）**状态的计划；已取消计划排除在外。
每个计划条目沿转移步骤从该批次出发，**穿过中间液逐级追踪到目标孔**（逐级稀释链同样覆盖），
同一计划同一孔只列一次：

```json
{
  "batch_id": "S1",
  "reason": "QC aliquot tested positive for contamination",
  "recalled_at": "2026-10-05T08:30:00+00:00",
  "report": {
    "batch_id": "S1",
    "affected_plan_count": 2,
    "plans": [
      {"request_id": "run-A", "reservation_status": "reserved",
       "drawn_nl": 1100, "wells": ["A1", "A2"]},
      {"request_id": "run-B", "reservation_status": "confirmed",
       "drawn_nl": 1100, "wells": ["A1", "A2"]}
    ]
  }
}
```

**召回生效后的语义（权威批次状态裁决）：**

- **新请求**在 `POST /inventory/plans` 引用该批次时被**明确拒绝**（`409`，
  `error: "batch_recalled"`，附 `recalled_batches`），**不冻结请求号、不建预留**，
  同一 `X-Request-ID` 以后仍可使用；即使召回与请求并发，事务内对批次行锁的复核也会拒绝
  召回生效后产生的任何新预留；
- 相关**待处理预留（reserved）不可确认**（confirm 返回 `409 batch_recalled`，
  体积不转入已用），但**仍可取消**（cancel 照常归还，且只归还一次）；
- **已确认（confirmed）结果不变**：确认幂等回放仍为 `200`、取消仍为 `409`，
  已用体积与既有冻结请求的读取（`GET /plans|/inventory/plans`）、同号同体重试
  完全保持原语义；
- 召回只影响库存模式；普通模式 `POST /plans`（内联母液）行为不变。

### `GET /batches/{batch_id}/recall`

取回该批次冻结的召回记录（原因、时间与 `report`）：

- `200 OK`：已召回，返回与首次召回完全相同的冻结记录；
- `404 Not Found`：批次存在但从未召回（`error: "not_found"`），
  或批次未登记（`error: "unknown_batch"`）。

`GET /batches` 与 `GET /batches/{id}` 的批次对象另附实时召回字段
`recalled` / `recalled_reason` / `recalled_at`。

---

## 4. 调用示例

```bash
# 1) 提交一个含两级逐级稀释的请求
curl -sS -X POST http://localhost:8000/plans \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: demo-run-001" \
  --data @examples/request.json

# 2) 相同请求号与内容重试 -> 200
# 3) 同号异内容 -> 409
# 4) GET 取回
curl -sS http://localhost:8000/plans/demo-run-001
```

完整脚本（含 200/409/422/400 各种情况）：

```bash
bash examples/curl.sh
python3 examples/call_api.py
```

库存模式完整生命周期（登记批次 → 计划并预留 → 重试/冲突 → 确认/取消 → 实时余量）：

```bash
bash examples/inventory.sh
python3 examples/inventory_demo.py
```

批次污染召回完整流程（预留/确认/取消三种预留 → 不可撤销召回与影响清单 →
同因回放/异因冲突/未知批次 → 新请求拒绝不冻结 → 待处理预留可取消不可确认）：

```bash
bash examples/recall.sh
python3 examples/recall_demo.py
```

示例（1000 浓度母液，移液器 500–1000nL、步进 100nL，目标 A1=400、A2=100）会生成
两个中间液（如 400 与 100），并把较大的批量安排进较大的容器，所有转移均为 500–1000nL。

---

## 5. 求解方法与正确性

- 每个中间液 = 一个“高浓度父液 + 低浓度父液”的精确有理比例混合，整体依赖图是 **DAG**。
- 深度优先搜索为每个孔选择一到两个来源；当现有液体无法直接配制时，按整数稀释因子
  合成逐级稀释链（支持多父液中间液与多目标共享同一中间液）。
- 中间液批量体积自底向上取**最小可行值**：同时满足下游需求整数、两份父液份额均为
  步进整数倍、且每份都可拆成若干次 `[min,max]` 移液器转移；容器用精确二分图匹配分配。
- 生成计划后再做一次**独立的有理数质量守恒回放校验**：逐步检查来源不透支、目的不溢出、
  移液器区间、编号连续、目标体积/浓度精确、依赖无环。校验不过的计划绝不会返回。

本地无 Docker 时运行测试（仅依赖解释器标准库的求解器测试可直接跑）：

```bash
python3 tests/smoke.py
python3 tests/api_smoke.py   # 需已安装 fastapi/pydantic/httpx
python3 -m pytest tests/     # 完整测试（含库存模式与并发用例）
```

## 6. 目录结构

```
app/
  main.py          FastAPI 路由与状态码（/plans、/batches、/inventory/plans、
                   确认/取消、批次召回）
  models.py        Pydantic 请求/响应模型与浓度校验
  planning.py      请求 -> 求解器映射、引用校验、幂等哈希、批次用量统计、
                   召回影响孔追踪（沿转移穿过中间液）
  db.py            asyncpg 连接池、建表、冻结/取回、批次登记、原子预留、
                   确认/取消、不可撤销召回（单事务 + 行锁裁决）
  solver/
    core.py        数据结构、移液器可行性、最小批量
    options.py     候选方案（直接/双源/新中间液/逐级稀释链）
    engine.py      回溯状态、批量与容器可行性
    solve.py       solve() 入口、计划组装、独立校验、无解诊断
examples/          request.json、inventory.json、curl.sh、inventory.sh、
                   call_api.py、inventory_demo.py、recall.sh、recall_demo.py
tests/             smoke.py（求解器）、api_smoke.py（HTTP+幂等）、
                   test_api.py、test_solver.py、test_inventory.py（库存/并发）、
                   test_recall.py（召回/追踪/并发裁决）
Dockerfile, docker-compose.yml, requirements.txt
```

---

## 7. 本轮变动（库存模式）

1. **新增批次登记与库存模式**：`POST/GET /batches`、`POST /inventory/plans`、
   `POST /inventory/plans/{id}/confirm|cancel`、`GET /inventory/plans/{id}`；
   新增 `batches`、`reservations`、`reservation_items` 三张表（启动时自动创建）。
2. **原子预留**：可行计划的冻结与各批次 `reserved_nl` 增加在同一事务提交；
   批次行 `FOR UPDATE` 加锁，并发不超卖；失败（非法批次/无解/库存不足）不占库存。
3. **预留生命周期**：`reserved → confirmed`（计入 `used_nl`，不可再取消）或
   `reserved → cancelled`（归还一次）；重复/并发操作幂等收敛。
4. **数据库连接改为连接池**（`asyncpg.create_pool`），`/health`、`/plans` 行为不变。
5. **兼容性**：`/plans` 请求格式、`X-Request-ID` 幂等、状态码与冻结语义完全不变；
   库存模式复用同一冻结机制（同号同体 `200` 原结果、同号异体 `409`）。
6. **修复**：请求校验异常处理器现在对字段校验器抛出的 `ValueError`
   （如 `@` 前缀 ID）也能正确返回 `400`（此前会因错误对象无法 JSON 序列化而 500）。

---

## 8. 本轮变动（批次召回）

1. **新增不可撤销召回**：`POST /batches/{id}/recall`（批次号 + 原因）与
   `GET /batches/{id}/recall`；新增 `recalls` 表，`batches` 表新增
   `recalled` / `recalled_reason` / `recalled_at` 三列（启动时
   `ADD COLUMN IF NOT EXISTS` 自动迁移）。召回与影响清单在同一事务冻结：
   批次行 `FOR UPDATE` 后锁定触及该批次的预留行，报告只含**实际取用且处于
   reserved/confirmed** 的计划，沿转移穿过中间液追踪到孔，同计划同孔一次并标注
   `reservation_status`（附 `drawn_nl`）。
2. **召回幂等与冲突**：同批次同原因重复召回返回原结果（`200`）；异原因 `409
   recall_reason_conflict`（不可改因）；未知批次 `404 unknown_batch`；
   原因非法 `400`。
3. **召回后裁决**：新库存计划引用被召回批次一律 `409 batch_recalled`，
   **不冻结请求号、不占库存**（HTTP 预检 + `reserve_and_freeze` 事务内
   批次行锁复核双重保证，召回生效后不可能产生新预留）；待处理预留
   **不可确认**（`409 batch_recalled`）但**仍可取消**；已确认结果及既有
   冻结请求的读取/重试语义完全不变。
4. **并发正确性**：召回、计划创建、确认/取消统一按“先批次行（排序）后预留行”
   加锁，消除 AB-BA 死锁；召回与确认并发时以权威批次状态线性化（恰好一方生效），
   影响清单与该裁决一致。
5. **兼容性**：`/plans`、`/batches` 登记、既有确认/取消与幂等语义不变；
   批次对象仅**新增** `recalled*` 字段；建表改为幂等迁移，现有 `docker compose up`
   启动方式与健康检查保持可用。
6. **用法补充**：新增 `examples/recall.sh`、`examples/recall_demo.py` 调用示例，
   测试新增 `tests/test_recall.py`（含召回/创建、召回/确认并发用例）。

1. **新增批次登记与库存模式**：`POST/GET /batches`、`POST /inventory/plans`、
   `POST /inventory/plans/{id}/confirm|cancel`、`GET /inventory/plans/{id}`；
   新增 `batches`、`reservations`、`reservation_items` 三张表（启动时自动创建）。
2. **原子预留**：可行计划的冻结与各批次 `reserved_nl` 增加在同一事务提交；
   批次行 `FOR UPDATE` 加锁，并发不超卖；失败（非法批次/无解/库存不足）不占库存。
3. **预留生命周期**：`reserved → confirmed`（计入 `used_nl`，不可再取消）或
   `reserved → cancelled`（归还一次）；重复/并发操作幂等收敛。
4. **数据库连接改为连接池**（`asyncpg.create_pool`），`/health`、`/plans` 行为不变。
5. **兼容性**：`/plans` 请求格式、`X-Request-ID` 幂等、状态码与冻结语义完全不变；
   库存模式复用同一冻结机制（同号同体 `200` 原结果、同号异体 `409`）。
6. **修复**：请求校验异常处理器现在对字段校验器抛出的 `ValueError`
   （如 `@` 前缀 ID）也能正确返回 `400`（此前会因错误对象无法 JSON 序列化而 500）。
