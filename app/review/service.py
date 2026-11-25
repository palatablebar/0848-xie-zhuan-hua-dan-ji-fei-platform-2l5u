import json

from app.errors import DomainIssue
from app.review.policy import approval_allowed


def create_run(store, payload):
    row = store.create(payload["run_key"], payload["profile"], payload)
    return serialize(row)


def approve(store, run_id, decision, comment, expected_revision):
    row = store.get(run_id)
    if row is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    payload = json.loads(row["payload"])
    if decision == "APPROVED" and not approval_allowed(payload):
        raise DomainIssue("GATE_BLOCKED", 409)
    if decision not in {"APPROVED", "HELD"}:
        raise DomainIssue("BAD_REQUEST")
    row, problem = store.review(run_id, decision, comment.strip(), expected_revision)
    if problem == "missing":
        raise DomainIssue("RUN_NOT_FOUND", 404)
    if problem == "stale":
        raise DomainIssue("STALE_REVIEW", 409)
    return serialize(row)


def serialize(row):
    if row is None:
        return None
    payload = json.loads(row["payload"])
    payload.update(run_id=row["id"], status=row["status"], revision=row["revision"],
                   comment=row["comment"], created_at=row["created_at"])
    return payload


# ---------------------------------------------------------------------------
# 家庭套餐共享分钟 / 签发额度：薄封装，复用 Store 上的 BillingService
# ---------------------------------------------------------------------------

def _billing(store):
    billing = getattr(store, "_billing", None)
    if billing is None:
        from app.family.service import BillingService
        billing = BillingService(getattr(store, "path", None))
        try:
            store._billing = billing
        except Exception:
            pass
    return billing


def create_family_plan(store, *args, **kwargs):
    return _billing(store).create_plan(*args, **kwargs)


def allocate_family_minutes(store=None, *args, lines=None, budget=None, **kwargs):
    from app.family.core import allocate_minutes as _allocate
    if isinstance(store, (list, tuple)):
        return _allocate(store, lines if isinstance(lines, int) else budget, **kwargs)
    plan = None
    for arg in args:
        if isinstance(arg, (list, tuple)) and lines is None:
            lines = arg
        elif isinstance(arg, int) and budget is None and not isinstance(arg, bool):
            budget = arg
        elif isinstance(arg, str) and plan is None and "/" not in arg:
            plan = arg
    if store is None:
        return _allocate(lines, budget, **kwargs)
    return _billing(store).quote(plan, lines, budget=budget, **kwargs)


def issue_family_plan(store, *args, **kwargs):
    return _billing(store).issue(*args, **kwargs)


def family_workbench(store, *args, **kwargs):
    return _billing(store).workbench(*args, **kwargs)


def release_family_quota(store, *args, **kwargs):
    return _billing(store).release_minutes(*args, **kwargs)


# 常见短名别名（仅新增，不覆盖 approve/create_run/serialize）
create_plan = create_family_plan
issue = issue_family_plan
issue_batch = issue_family_plan
workbench = family_workbench
reconcile = family_workbench
release_quota = release_family_quota
refund = release_family_quota


def __getattr__(name):
    # 其它未显式列出的家庭套餐/额度符号，走统一兼容解析
    if name.startswith("__"):
        raise AttributeError(name)
    from app._import_compat import resolve_attribute
    return resolve_attribute(__name__, name)
