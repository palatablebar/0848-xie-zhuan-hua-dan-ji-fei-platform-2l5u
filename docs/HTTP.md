# 接口

- `GET /`：工作台。
- `GET /api/catalog`：阶段、字段和样例。
- `POST /api/stages/{stage}`：按阶段返回处理结果。
- `POST /api/runs`：执行完整批次并保存草稿。
- `GET /api/runs/{id}`：读取批次。
- `POST /api/runs/{id}/review`：签发或暂缓，提交 `decision`、`comment`、`expected_revision`。
- `GET /api/runs/{id}/evidence`：下载已签发证据。
- `GET /api/examples/{profile}`：下载样例。
- `GET /healthz`：健康检查。

## 家庭套餐共享分钟（同一账期）

共享赠送分钟池按 `plan_id` + `period` 隔离；同号码同向话单先按“重叠合并 +
每段六十秒向上取整”得到计费分钟，再参与分配。未签发话单在剩余额度内按每分钟
价值从高到低分配，使应收最小；已签发话单锁定的赠送分钟不会被后续优化挪用。

- `POST /api/family/plans`（兼容 `/api/plans`、`/api/family-plans`、`/api/quotas`）：
  建立套餐，字段 `plan_id/family_id`、`period`、`gift_minutes/shared_minutes`。
- `POST /api/allocate`（兼容 `/api/quote`、`/api/optimize`）：纯试算最优分配，
  提交 `budget` 或 `total_minutes` 与 `lines[]`。
- `POST /api/issue`（兼容 `/api/batches`、`/api/issuances`、
  `/api/plans/{id}/issue`）：签发并原子扣减；支持 `idempotency_key`
  （或 `Idempotency-Key` 请求头）。同键重放不重复扣减；`fail:true` 模拟签发失败，
  扣减随事务回滚、额度恢复，随后可用同一幂等键重试。
- `GET/POST /api/workbench?plan_id=&period=`（兼容 `/api/reconcile`、
  `/api/plans/{id}/workbench`）：核对各号码计费/赠送/应付分钟、应收分，以及套餐
  已扣减与余量，并返回守恒检查 `checks`/`balanced`。
- `GET /family`：家庭套餐额度核对工作台页面。

并发提交由 `BEGIN IMMEDIATE`、条件更新
（`gift-granted-reserved >= ?`）与 `CHECK(granted<=gift)` 共同保证不会超扣。

