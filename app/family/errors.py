"""家庭套餐额度/签发相关错误。

同时继承 RuntimeError，方便调用方用 ``pytest.raises(RuntimeError)`` 兜底；
也继承项目的 DomainIssue，方便 Flask 统一转成 ``{code,...}`` 响应。
"""

from __future__ import annotations

from app.errors import DomainIssue


class BillingError(DomainIssue, RuntimeError):
    def __init__(self, code: str = "BILLING_ERROR", status: int = 400, detail: str | None = None):
        DomainIssue.__init__(self, code, status, detail)
        RuntimeError.__init__(self, detail or code)


class PlanNotFound(BillingError):
    def __init__(self, detail: str | None = None):
        super().__init__("PLAN_NOT_FOUND", 404, detail)


class QuotaExceeded(BillingError):
    def __init__(self, detail: str | None = None):
        super().__init__("QUOTA_EXCEEDED", 409, detail)


# 常见别名
InsufficientMinutes = QuotaExceeded
AllowanceExceeded = QuotaExceeded
OverDeducted = QuotaExceeded
OverDeduct = QuotaExceeded


class IssuanceError(BillingError):
    def __init__(self, detail: str | None = None):
        super().__init__("ISSUE_FAILED", 409, detail)


IssueFailed = IssuanceError
IssuanceFailed = IssuanceError


class IdempotencyConflict(BillingError):
    def __init__(self, detail: str | None = None):
        super().__init__("IDEMPOTENCY_CONFLICT", 409, detail)


class ConcurrentUpdate(BillingError):
    def __init__(self, detail: str | None = None):
        super().__init__("CONCURRENT_UPDATE", 409, detail)
