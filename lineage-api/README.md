# 数据血缘影响分析 / 重算计划 API（FastAPI + SQLite）

本地部署的血缘服务：批量登记**源字段 / 派生字段 / 报表列**的依赖关系，
提交拟下线字段后递归计算**确实失效的报表列**；数仓调度员重算多个报表列前，
还可提交目标报表列与执行成本，服务在**同一血缘快照**内给出
**全局最低总成本**的重算计划（各列选用的候选输入组 + 满足依赖的执行顺序）。

## 语义规则

- 每个下游列可登记**多个候选输入组**（`candidate_groups`，组的数组）。
- 一个候选组可用 ⇔ 组内字段**全部**可用。
- 一个列可产出 ⇔ **任一**候选组可用；仍有可用组的列不会被误报。
- 源字段（`source`）没有输入；派生 / 报表列必须至少有一个非空候选组。
- 提交拟下线字段后沿血缘图递归传播：
  - 只有所有候选组都不可用的列才计入失效；
  - 返回失效报表列的**每一个**失效候选组及组内缺失字段；
  - 每个缺失字段附带 `root_causes`（追溯到最深层的拟下线字段，而非中间节点）。
- 登记批次若引入循环依赖（含自环），**整批回滚不生效**。

## 重算计划语义

- 请求给出目标报表列 `targets` 与相关非源列（derived / report）的
  非负有限执行成本 `costs`；源列只读取、不执行，不需要也不允许给成本。
- 服务沿血缘反向闭包找出全部相关非源列；**相关非源列超过 16 个时拒绝规划**
  （子集枚举复杂度为 O(2ⁿ)）。
- 每个执行列从已登记的候选输入组中**选一组**：组内源列直接读取，
  组内非源列必须同样纳入执行（传递闭包）。
- 多个目标共用的派生列**只执行一次，且全计划内只能选用同一组输入**。
- 在所有可行执行集合中取**总成本最低**者（成本非负，枚举全部子集精确求解）；
  成本相同时按列名字典序确定性地打破平局。
- 规划是**只读**操作，不修改任何血缘；整个计算在单连接快照事务内完成，
  与登记并发时只会看到登记前或登记后的完整定义，不会混用。

## 目录结构

```
lineage-api/
├── app/
│   ├── main.py         # FastAPI 入口、异常映射、生命周期
│   ├── service.py      # 整批事务登记、循环检测、递归失效分析、重算规划校验
│   ├── planning.py     # 子集枚举最优规划（位掩码 + 拓扑排序，纯函数）
│   ├── db.py           # SQLite 连接 / WAL / 快照与写事务
│   ├── models.py       # Pydantic 请求与响应模型
│   └── exceptions.py   # 领域错误（422/409/404）
├── tests/              # pytest：功能用例 + 真实 uvicorn 并发用例
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── examples/           # 可直接调用的请求样例
```

## Docker Compose 启动

```bash
cd lineage-api
docker compose up -d --build
docker compose ps          # health: healthy
curl http://localhost:8000/health
```

- 服务监听 `8000`，SQLite 数据库落在命名卷 `lineage-data` 的
  `/data/lineage.db`（WAL 模式），容器重建后数据保留。
- 交互式文档：http://localhost:8000/docs （OpenAPI 3.1）。
- 停止与清理：

```bash
docker compose down          # 停止，保留数据卷
docker compose down -v       # 同时删除数据卷
```

## 本地（无 Docker）运行

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
LINEAGE_DB_PATH=./lineage.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API 入口

### 1. 批量登记血缘 `POST /lineage/register`

同一批次内的节点可以互相引用；重复登记同名节点会**整体替换**其候选组。
任何校验失败、类型冲突或循环依赖都返回错误且整批不落库。

```bash
curl -s -X POST http://localhost:8000/lineage/register \
  -H 'Content-Type: application/json' \
  -d @examples/register.json
```

请求体：

```json
{
  "nodes": [
    {"name": "order_amt", "kind": "source"},
    {"name": "coupon_amt", "kind": "source"},
    {"name": "net_amt", "kind": "derived",
     "candidate_groups": [["order_amt", "coupon_amt"]]},
    {"name": "report_gmv", "kind": "report",
     "candidate_groups": [["net_amt"], ["order_amt"]]}
  ]
}
```

错误约定：

| HTTP | error                     | 场景                                       |
|------|---------------------------|--------------------------------------------|
| 422  | `validation_error`        | 空候选组、源字段带输入、引用未登记字段等   |
| 409  | `cycle_detected`          | 批次将引入循环依赖，整批回滚（附 cycle 路径）|
| 409  | `conflict`                | 同名节点已存在但类型不同                   |

### 2. 下线影响分析 `POST /lineage/impact`

```bash
curl -s -X POST http://localhost:8000/lineage/impact \
  -H 'Content-Type: application/json' \
  -d '{"retiring": ["coupon_amt"]}'
```

响应（节选）：

```json
{
  "retiring": ["coupon_amt"],
  "failed_report_columns": [
    {
      "name": "report_coupon_rate",
      "failed_groups": [
        {
          "group_index": 0,
          "inputs": ["coupon_amt", "order_amt"],
          "missing": [
            {"name": "coupon_amt", "kind": "source", "root_causes": ["coupon_amt"]}
          ]
        }
      ]
    }
  ],
  "unavailable_count": 3,
  "unavailable_nodes": ["coupon_amt", "net_amt", "report_coupon_rate"]
}
```

- `failed_report_columns` 只包含**所有候选组均失效**的报表列；
  仍有可用组（如上例中的 `report_gmv`，备用组 `[order_amt]` 可用）不会出现。
- `unavailable_nodes` 列出全部不可用节点（含中间派生列），方便逐级排查。
- 拟下线字段未登记时返回 `404 not_found`。

### 3. 重算计划 `POST /lineage/replan`

提交目标报表列和相关非源列的执行成本，返回全局最低总成本的重算任务。

```bash
curl -s -X POST http://localhost:8000/lineage/replan \
  -H 'Content-Type: application/json' \
  -d @examples/replan.json
```

请求体：

```json
{
  "targets": ["report_gmv", "report_coupon_rate"],
  "costs": {
    "net_amt": 8.0,
    "report_gmv": 2.0,
    "report_coupon_rate": 3.0
  }
}
```

响应：

```json
{
  "targets": ["report_coupon_rate", "report_gmv"],
  "total_cost": 13.0,
  "columns": [
    {"name": "net_amt", "kind": "derived", "group_index": 0,
     "inputs": ["coupon_amt", "order_amt"], "cost": 8.0},
    {"name": "report_coupon_rate", "kind": "report", "group_index": 0,
     "inputs": ["coupon_amt", "order_amt"], "cost": 3.0},
    {"name": "report_gmv", "kind": "report", "group_index": 0,
     "inputs": ["net_amt"], "cost": 2.0}
  ],
  "execution_order": ["net_amt", "report_coupon_rate", "report_gmv"]
}
```

- `columns` 列出每个执行列选用的候选组（`group_index` 对应登记时的组下标）；
  被多个目标共用的派生列只出现一次，成本只计一次。
- `execution_order` 满足全部所选依赖：被依赖的列先执行。
- 拒绝场景：

| HTTP | error              | 场景                                                        |
|------|--------------------|-------------------------------------------------------------|
| 404  | `not_found`        | 目标列未登记（`details.unknown`）                           |
| 422  | `validation_error` | 目标不是报表列、目标重复、成本缺失 / 多余（含源列）、       |
|      |                    | 成本为负数 / NaN / 无穷、相关非源列超过 16 个               |

### 4. 查看当前血缘 `GET /lineage/graph`

返回最近一次已提交的完整血缘（节点类型 + 全部候选组）。

### 5. 健康检查 `GET /health`

## 并发与一致性设计

1. **WAL 模式**：读写不互相阻塞。
2. **登记串行化**：每个登记批次使用 `BEGIN IMMEDIATE` 先拿写锁，
   并发批次排队执行，配合 `busy_timeout` 避免 `database is locked`。
3. **查询快照隔离**：每次影响分析 / 重算规划 / 血缘查询在单连接显式事务内
   完成，SQLite 的快照读保证请求只看到**登记前或登记后**的完整血缘，
   不可能读到应用了一半的批次；重算规划全程只读，绝不修改血缘。
4. **整批原子性**：节点与候选组分两阶段写入（先全部节点、再全部边），
   写入后做全图 DFS 环检测，发现环则抛错触发 `ROLLBACK`，批次内任何数据都不留存。

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

覆盖：候选组 AND / OR 语义、递归失效与根因追溯、备用组存活不误报、
循环 / 自环整批回滚、各类 422/409/404、重复登记替换、
重算规划的最优性 / 共享列只执行一次 / 执行顺序 / 16 列上限 / 只读性，
以及在真实 uvicorn 服务上持续 4 秒的并发读写（读者只观测到完整快照，
重算规划不会混用并发登记前后的定义）。
