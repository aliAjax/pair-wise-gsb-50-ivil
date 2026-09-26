# 税务稽查案件与复议流程

纯Python标准库实现的税务稽查案件与复议流程原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、补税、滞纳金、处罚、证据检查，以及分期计划与缴款分配规则。
- `src/repository.py`：SQLite建表、事务和查询，含账期（installment）存储。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8326
```

默认端口为`8326`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `POST /api/records/{id}/plan`：冻结应缴账单并提交分期计划，请求体为`{"data":{"installments":[{"amount":1000.0,"due_date":"2026-10-01"}]}}`。
- `POST /api/records/{id}/payments`：登记缴款，请求体为`{"data":{"amount":1000.0}}`。
- `GET /api/records/{id}/plan`：查看分期计划、每期余额、结清状态和缴款记录。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 分期缴款

- 复核完成（状态`reviewed`）后，复核人员把复核金额冻结为应缴账单，一次提交全部各期金额和到期日；各期到期日必须依次靠后，金额合计必须等于复核金额，每个案件只能冻结一次。
- 缴款先冲最早未结清的一期，该期结清后固定不再变动；超出该期余额的多出金额拒绝入账，全部结清后再缴同样拒绝。
- 案件详情（`GET /api/records/{id}`）内嵌`installment_plan`，展示计划总额、每期余额和结清状态；账期存于`installment_plans`、`installments`、`payments`表，服务重启后仍可核对，冻结与缴款也会写入审计时间线。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
