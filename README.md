# 港口泊位与航道调度

纯Python标准库实现的港口泊位与航道调度原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、靠泊可行性、吃水安全、时间窗冲突和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `src/dg_domain.py`：危险品船进港台账的常量、输入校验与派生规则（护航拖轮数量等）。
- `src/dg_rules.py`：预占可行性评估与封航影响判断（纯函数）。
- `src/dg_repository.py`：台账表结构、一起预占事务、冲突记录与台账重放恢复。
- `src/dg_service.py`：台账用例编排：预占、放行、靠泊、作废重排、封航通知、恢复。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 危险品船进港台账

进港计划、航道区段、泊位占用和护航资源接成一套可重排台账（`dg_*`表）：

- **放行前一起预占**：`submit`在单个事务里同时检查航道区段、泊位净空和护航拖轮，全部满足才一起占位（`reserved`）；缺哪项就停住（`pending`）并把待补原因写进`pending_reasons`，一项都不占。未完成的不能放行。
- **作废重排**：封航范围（通知新增/修改）、船底富余水深或危险品类别修改后，未靠泊安排（`reserved`/`released`/`pending`）立即作废重排；已靠泊（`berthed`）安排不动，靠泊时的依据快照记入台账（`basis_retained`），沿用原依据。
- **先到先占**：两位调度员同时提交同一航道区段或泊位，检查与落库同事务串行，先到的一版生效；落后者保持草稿，冲突逐条写入`dg_conflicts`并记台账。
- **恢复重放**：每次变更与状态同事务写入`dg_ledger`完整台账；写盘失败或占位数据丢失后，`recover`从台账重放重建占位，按`(event_id, resource_code)`幂等，重放不重复占位。

计划状态机：`draft →（submit）→ reserved / pending / draft+冲突 →（release）→ released →（berth）→ berthed →（depart）→ departed`。

## 启动

```bash
python3 app.py --db ./data.db --port 8321
```

默认端口为`8321`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

### 危险品船进港台账接口

- `POST /api/dg/resources`：登记资源，请求体为`{"kind":"channel_segment|berth|tug","code":"...","attrs":{...}}`。
- `GET /api/dg/resources`：资源列表，可带`kind`参数。
- `POST /api/dg/plans`：创建进港计划，请求体为`{"reference":"...","data":{"vessel":"...","vessel_length_m":180,"dangerous_class":"3","draft_m":9.0,"ukc_required_m":0.5,"eta_hour":8,"etd_hour":20,"segments":["SEG-1"],"berth":"BERTH-A"}}`。
- `GET /api/dg/plans`、`GET /api/dg/plans/{id}`：计划列表与详情。
- `POST /api/dg/plans/{id}/submit`：一起预占，返回`reserved`/`pending`（带待补原因）/`conflict`（带冲突记录）。
- `POST /api/dg/plans/{id}/release|berth|depart`：放行、靠泊（记录依据快照）、离泊。
- `POST /api/dg/plans/{id}/modify`：修改富余水深、危险品类别等，请求体为`{"data":{...}}`；未靠泊作废重排，已靠泊沿用原依据。
- `POST /api/dg/closures`：发布封航通知，请求体为`{"notice_no":"...","segments":["SEG-1"],"start_hour":10,"end_hour":18,"reason":"..."}`，受影响计划立即作废重排。
- `POST /api/dg/closures/{id}/modify|lift`：修改封航范围（重新作废重排）/解除封航（待补计划自动重排）。
- `GET /api/dg/occupancy`：当前占位视图。
- `GET /api/dg/conflicts`：冲突记录。
- `GET /api/dg/ledger`：完整台账。
- `POST /api/dg/recover`：从台账重放恢复占位。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。台账写操作角色为`dispatcher`（或`admin`），读操作另允许`port_controller`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突，以及台账的一起预占与待补原因、作废重排与沿用原依据、并发先到先占、写盘失败恢复与重放幂等。
