# 税务稽查案件与复议流程

纯Python标准库实现的税务稽查案件与复议流程原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、补税、滞纳金、处罚和证据完整性和冲突检查。
- `src/payments.py`：复核账单分期计划与付款冲销规则。
- `src/repository.py`：SQLite建表、事务和查询。
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
- `GET /api/records/{id}`：记录详情，已制定计划时附带`payment_plan`（计划总额、每期余额和结清状态）。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `POST /api/records/{id}/schedule`：复核人员把复核金额冻结为应缴账单并一次提交全部账期，请求体为`{"expected_version":1,"data":{"installments":[{"amount":1000,"due_date":"2026-10-31"}]}}`。
- `POST /api/records/{id}/payments`：企业登记付款，先冲抵最早未结清的一期，请求体为`{"expected_version":1,"data":{"amount":1000}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 分期缴款

- 案件进入`reviewed`后，复核人员（`reviewer`）可调用`schedule`把复核金额冻结为应缴账单，并一次提交全部账期；每个案件只能制定一次计划。
- 各期金额必须大于0，到期日采用`YYYY-MM-DD`格式且依次靠后，各期合计必须等于账单金额。
- 付款（`taxpayer_rep`）在`reviewed`/`appealed`状态受理，只冲抵最早未结清的一期；超出该期余额的付款整笔拒绝入账，不产生任何变更。
- 账期结清后固定，全部结清后计划固定，不再受理付款；案件结案（`closed`）后也不再受理。
- 计划与账期持久化在`payment_plans`/`installments`表，制定与每笔付款均写入审计时间线，服务重启后详情仍可核对。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突以及分期计划与付款冲销。
