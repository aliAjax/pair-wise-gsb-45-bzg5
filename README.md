# 危险品船进港调度台账

纯 Python 标准库实现的**危险品船进港可重排调度台账**原型：SQLite 持久化（事件溯源）、`http.server` 提供 HTTP 接口。

## 设计模型

`ledger_events`（只追加事件流）是唯一事实源，当前状态全部由事件顺序归约得到；投影表随时可整体重建。

- 计划修订链：每个进港计划有多个修订版本，状态为
  `draft 草稿 → blocked 待补 / released 已放行 → berthed 已靠泊 → completed 已完成`，
  旁路 `void 已作废`、`cancelled 已取消`。
- 三类资源：航道区段（channel）、泊位（berth）、护航拖轮（tug），在台账中登记并按半开时间窗 `[start,end)` 占位。

### 关键规则

1. **放行前一起预占**：`release` 在单事务内同时判定航道区段、泊位净空（长度+船底富余水深）、护航拖轮（按 IMDG 类别默认 1~3 艘）。任一不满足，计划停在 `blocked`，逐项返回待补原因（`closure_active / ukc_insufficient / berth_length / escort_shortage / channel_missing / berth_missing`），不留下部分占位。
2. **作废重排**：临时封航通知到达，或安全基准（富余水深/护航要求）变更后，所有受影响且**未靠泊**的已放行安排立即作废、释放占位并生成新修订版本，按新依据自动重新判定（仍不满足则带待补原因停住）；**已靠泊沿用原放行依据**，修改危险品类别/吃水/航道/泊位/时间窗等关键字段直接被拒绝。调度员手工修改未靠泊计划参数同样走“作废 → 新修订 → 重排”。
3. **并发争用**：两位调度员同时放行同一航道区段或泊位，事务以 `BEGIN IMMEDIATE` 串行化提交，先到者生效；落后者保留为 `draft` 并记录冲突（指向先到计划与资源窗），不产生占位，冲突解除后可重新放行。
4. **写盘失败恢复**：命令在提交前崩溃则整事务回滚、事件不落地；`POST /api/rebuild` 可从完整事件日志重建全部投影。事件按 `event_id` 幂等，整段重放/重试不会重复占位；写请求可用 `X-Idempotency-Key` 做安全重试。

## 模块结构

- `app.py`：参数解析、依赖组装、服务启动。
- `src/domain.py`：错误类型与输入校验原语。
- `src/ledger.py`：事件溯源归约器（计划修订、有效占位、封航与安全基准），重放幂等。
- `src/rules.py`：放行判定纯规则（冲突/缺资源/封航/净空/拖轮）与入参校验。
- `src/repository.py`：SQLite 事件表、投影物化、串行化命令提交、幂等键、重建。
- `src/service.py`：用例编排、放行预占、作废重排、权限。
- `src/http_api.py`：HTTP 路由与统一错误响应。
- `src/audit.py`：计划事件 + 系统事件（封航/基准变更）时间线。
- `static/index.html`：台账只读演示页。
- `tests/`：规则、台账归约、完整流程、并发冲突/写盘失败/幂等恢复测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8321
```

## 接口

除 `/health`、`/` 外均需 `X-User-Id`、`X-Role`（`dispatcher` / `port_controller` / `admin`）。写请求建议带 `X-Idempotency-Key`。

- `POST /api/resources`：登记资源 `{"kind":"channel|berth|tug","id":"C1",...}`（channel/berth 带 `depth_m`，berth 带 `length_m`，tug 带 `power_kn`）。
- `GET /api/resources?kind=`、`GET /api/holds?resource_type=&resource_id=`。
- `POST /api/plans`：登记进港计划 `{"plan_ref":"V-1","params":{vessel,dg_class(1~9),draft_m,length_m,channel_id,berth_id,start_hour,end_hour}}`。
- `POST /api/plans/{ref}/release`：申请放行（原子预占/待补/冲突草稿）。
- `POST /api/plans/{ref}/revise`：修改参数（body 同 params），未靠泊作废重排。
- `POST /api/plans/{ref}/berth`：靠泊（可带 `actual_draft_m`，复验富余水深；靠泊后释放航道/拖轮占位）。
- `POST /api/plans/{ref}/depart`、`POST /api/plans/{ref}/cancel`。
- `POST /api/closures`：临时封航 `{"notice_id","segments":["C1"],"start_hour","end_hour","reason"}`。
- `POST /api/safety-basis`：`{"ukc_by_class":{"1":2.0},"escort_by_class":{"2":3}}`。
- `GET /api/notices`、`GET /api/safety-basis`、`GET /api/plans?state=`、`GET /api/plans/{ref}`、`GET /api/plans/{ref}/audit`、`GET /api/stats`。
- `POST /api/rebuild`：从完整事件台账重建投影。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
