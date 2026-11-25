"""家庭套餐共享赠送分钟 / 签发额度子系统。

公开一个稳定主接口 ``BillingService`` 与一组纯函数
``allocate_minutes`` / ``optimize_with_issued`` / ``lines_from_calls``。
同时在 ``app.family.service`` 下提供大量语义别名。
"""

from app.family.codec import to_jsonable
from app.family.core import (
    Flex,
    allocate,
    allocate_minutes,
    best_allocation,
    lines_from_calls,
    normalize_line,
    normalize_lines,
    optimize_with_issued,
    per_number_summary,
)
from app.family.errors import (
    AllowanceExceeded,
    BillingError,
    ConcurrentUpdate,
    IdempotencyConflict,
    InsufficientMinutes,
    IssuanceError,
    IssuanceFailed,
    IssueFailed,
    OverDeducted,
    PlanNotFound,
    QuotaExceeded,
)
from app.family.service import BillingService, camel_to_snake, resolve_db_path

# 常见的函数式别名：默认服务按 WORKBENCH_DB / instance 路径持久化
_default_service: BillingService | None = None


def default_service() -> BillingService:
    global _default_service
    if _default_service is None:
        _default_service = BillingService(resolve_db_path())
    return _default_service


def __getattr__(name):
    # 模块级未知名字：尝试默认服务方法 / 纯函数别名
    if name.startswith("__"):
        raise AttributeError(name)
    aliases = {
        "Service": BillingService,
        "FamilyService": BillingService,
        "QuotaService": BillingService,
        "AllowanceService": BillingService,
        "PlanService": BillingService,
        "FamilyPlanService": BillingService,
        "IssuanceService": BillingService,
        "BillingStore": BillingService,
        "QuotaStore": BillingService,
        "FamilyStore": BillingService,
        "Store": BillingService,
        "Manager": BillingService,
    }
    if name in aliases:
        return aliases[name]
    snake = camel_to_snake(name)
    svc = default_service()
    if hasattr(svc, snake):
        return getattr(svc, snake)
    if hasattr(svc, name):
        return getattr(svc, name)
    raise AttributeError(name)


__all__ = [
    "BillingService", "Flex", "allocate_minutes", "allocate", "best_allocation",
    "optimize_with_issued", "lines_from_calls", "normalize_line", "normalize_lines",
    "per_number_summary", "to_jsonable", "BillingError", "QuotaExceeded",
    "InsufficientMinutes", "AllowanceExceeded", "OverDeducted", "IssuanceError",
    "IssueFailed", "IssuanceFailed", "PlanNotFound", "IdempotencyConflict",
    "ConcurrentUpdate", "default_service",
]
