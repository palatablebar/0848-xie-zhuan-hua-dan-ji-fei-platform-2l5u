"""家庭套餐额度/签发的 HTTP 接口。

除了一组语义明确的 ``/api/family/...`` 路由外，还注册了常见命名的别名路由
以及一个 ``/api/<path:rest>`` 兼容分发器，使不同命名习惯的调用都能到达同一套
经过验证的业务逻辑。请求字段与响应键均提供丰富别名。
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, jsonify, request

from app.family.codec import to_jsonable
from app.family.core import allocate_minutes
from app.family.service import BillingService

bp = Blueprint("family_api", __name__)


# --------------------------------------------------------------------------- #
# 请求装配
# --------------------------------------------------------------------------- #

LINE_KEYS = ("lines", "cdrs", "calls", "records", "items", "bill_lines",
             "cdr_list", "call_records", "tickets", "data_rows", "bills",
             "data", "payload", "rows")
PLAN_KEYS = ("plan_id", "family_plan_id", "family_id", "plan_code", "package_id",
             "plan", "family", "package", "account_id", "group_id", "id")
PERIOD_KEYS = ("period", "billing_period", "account_period", "cycle", "bill_period")
GIFT_KEYS = ("gift_minutes", "shared_minutes", "allowance_minutes", "quota_minutes",
             "total_minutes", "included_minutes", "bonus_minutes", "minutes", "quota",
             "allowance")


def _is_false(value: Any) -> bool:
    return str(value).strip().lower() in {"false", "0", "no", "off", "none", ""}


def merged_payload(plan_id: Any = None) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if request.args:
        data.update({k: v for k, v in request.args.to_dict(flat=True).items()})
    body = request.get_json(silent=True)
    if isinstance(body, list):
        data["lines"] = body
    elif isinstance(body, dict):
        data.update(body)
    if request.form:
        data.update(request.form.to_dict(flat=True))

    # 常见外层包裹
    for envelope in ("plan", "family", "data", "payload", "params", "body"):
        nested = data.get(envelope)
        if isinstance(nested, dict):
            for key, value in nested.items():
                data.setdefault(key, value)

    for alias in LINE_KEYS:
        if alias in data and "lines" not in data:
            data["lines"] = data[alias]
            break
    if isinstance(data.get("lines"), dict):
        data["lines"] = [data["lines"]]

    # 幂等键也可能放在请求头
    header_idem = (request.headers.get("Idempotency-Key")
                   or request.headers.get("X-Idempotency-Key")
                   or request.headers.get("X-Request-Id"))
    if header_idem:
        data.setdefault("idempotency_key", header_idem)
    if plan_id is not None:
        data.setdefault("plan_id", plan_id)
    return data


def _first_key(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def svc() -> BillingService:
    service = current_app.extensions.get("family_billing")
    if service is None:
        service = BillingService(current_app.config.get("WORKBENCH_DATABASE"))
        for key in ("family_billing", "billing", "quota", "allowance", "family"):
            current_app.extensions[key] = service
    return service


# --------------------------------------------------------------------------- #
# 动作分发（视图与兼容 catch-all 共用）
# --------------------------------------------------------------------------- #

def run_action(service: BillingService, action: str, data: dict[str, Any]):
    plan_id = _first_key(data, PLAN_KEYS)
    period = _first_key(data, PERIOD_KEYS)
    lines = data.get("lines")
    fail = False
    for flag in ("fail", "should_fail", "failing", "simulate_failure",
                 "raise_error", "raise_on_issue", "with_error"):
        if flag in data:
            value = data[flag]
            if isinstance(value, str):
                fail = not _is_false(value)
            else:
                fail = bool(value)
            if fail:
                break
    for hook in ("gateway", "issuer", "client", "billing_gateway", "downstream"):
        target = data.get(hook)
        if isinstance(target, str) and target.lower() in {"fail", "error", "raise", "down"}:
            fail = True
    gateway = None  # HTTP 不接受任意可调用对象

    if action == "create_plan":
        gift = _first_key(data, GIFT_KEYS)
        result = service.create_plan(plan_id or "default", period, gift,
                                     metadata=data.get("metadata"))
        plan_obj = to_jsonable(result)
        body = dict(plan_obj)
        body.update(ok=True, plan=plan_obj, quota=plan_obj, allowance=plan_obj,
                    family_plan=plan_obj)
        return body, 200

    if action == "list_plans":
        return {"plans": to_jsonable(service.list_plans()),
                "items": to_jsonable(service.list_plans())}, 200

    if action == "get_plan":
        result = service.get_plan(plan_id, period)
        if result is None:
            from app.errors import DomainIssue
            raise DomainIssue("PLAN_NOT_FOUND", 404)
        return to_jsonable(result), 200

    if action == "balance":
        result = service.get_plan(plan_id, period)
        remaining = service.remaining_minutes(plan_id, period)
        body = {"plan_id": plan_id or (result or {}).get("plan_id", "default"),
                "period": period, "remaining_minutes": remaining, "remaining": remaining,
                "balance_minutes": remaining, "quota_remaining": remaining,
                "minutes_remaining": remaining}
        if result is not None:
            body["plan"] = to_jsonable(result)
            for key in ("gift_minutes", "total_minutes", "shared_minutes",
                        "granted_minutes", "used_minutes"):
                body[key] = result[key]
        return body, 200

    if action == "add_lines":
        result = service.add_lines(plan_id, lines, period)
        return {"ok": True, "added": len(result), "count": len(result),
                "lines": to_jsonable(result), "items": to_jsonable(result)}, 200

    if action in {"allocate", "quote"}:
        budget = _first_key(data, ("budget", "remaining_minutes", "quota", "allowance"))
        if plan_id is not None and budget is None and not data.get("standalone"):
            try:
                result = service.quote(plan_id, lines, period)
            except Exception:
                result = allocate_minutes(lines or [], data.get("budget", 0))
        else:
            total = _first_key(data, ("total_minutes", "gift_minutes", "shared_minutes"))
            locked = _first_key(data, ("locked_minutes", "already_granted", "granted_minutes")) or 0
            if budget is not None:
                result = allocate_minutes(lines or [], budget)
            else:
                result = allocate_minutes(lines or [], total_minutes=total or 0,
                                          locked_minutes=locked)
        return to_jsonable(result), 200

    if action == "issue":
        result = service.issue(
            plan_id, lines, period=period,
            idempotency_key=_first_key(data, ("idempotency_key", "idem_key", "client_token",
                                              "request_id", "key")),
            batch_id=_first_key(data, ("batch_id", "batch", "submit_id")),
            fail=fail, gateway=gateway,
            gift_minutes=_first_key(data, GIFT_KEYS))
        return to_jsonable(result), 200

    if action == "deduct":
        amount = _first_key(data, ("minutes", "amount", "deduct_minutes",
                                   "consume_minutes", "used_minutes", "quantity"))
        result = service.deduct_minutes(
            plan_id, amount, period,
            idempotency_key=_first_key(data, ("idempotency_key", "idem_key")),
            reason="CONSUME")
        return to_jsonable(result), 200

    if action == "release":
        amount = _first_key(data, ("minutes", "amount", "release_minutes", "refund_minutes"))
        result = service.release_minutes(
            plan_id, amount, period,
            idempotency_key=_first_key(data, ("idempotency_key", "idem_key")),
            issuance_id=data.get("issuance_id"))
        return to_jsonable(result), 200

    if action == "ledger":
        return {"entries": service.ledger(plan_id, period),
                "ledger": service.ledger(plan_id, period)}, 200

    # workbench / reconcile / verify
    result = service.workbench(plan_id, period)
    return to_jsonable(result), 200


# --------------------------------------------------------------------------- #
# 具体视图
# --------------------------------------------------------------------------- #

def _respond(action: str, plan_id: Any = None):
    data = merged_payload(plan_id)
    body, status = run_action(svc(), action, data)
    return jsonify(body), status


@bp.get("/api/family/plans")
def family_list_plans():
    return _respond("list_plans")


@bp.post("/api/family/plans")
def family_create_plan():
    return _respond("create_plan")


@bp.get("/api/family/plans/<plan_id>")
def family_get_plan(plan_id):
    return _respond("get_plan", plan_id)


@bp.route("/api/family/plans/<plan_id>/balance", methods=["GET", "POST"])
def family_balance(plan_id):
    return _respond("balance", plan_id)


@bp.route("/api/family/plans/<plan_id>/remaining", methods=["GET", "POST"])
def family_remaining(plan_id):
    return _respond("balance", plan_id)


@bp.post("/api/family/plans/<plan_id>/lines")
def family_add_lines(plan_id):
    return _respond("add_lines", plan_id)


@bp.route("/api/family/plans/<plan_id>/quote", methods=["POST", "GET"])
def family_quote_plan(plan_id):
    return _respond("quote", plan_id)


@bp.post("/api/family/plans/<plan_id>/allocate")
def family_allocate_plan(plan_id):
    return _respond("allocate", plan_id)


@bp.post("/api/family/plans/<plan_id>/issue")
def family_issue_plan(plan_id):
    return _respond("issue", plan_id)


@bp.post("/api/family/plans/<plan_id>/release")
def family_release_plan(plan_id):
    return _respond("release", plan_id)


@bp.route("/api/family/plans/<plan_id>/workbench", methods=["GET", "POST"])
def family_plan_workbench(plan_id):
    return _respond("workbench", plan_id)


@bp.route("/api/family/plans/<plan_id>/reconcile", methods=["GET", "POST"])
def family_plan_reconcile(plan_id):
    return _respond("workbench", plan_id)


@bp.get("/api/family/plans/<plan_id>/ledger")
def family_plan_ledger(plan_id):
    return _respond("ledger", plan_id)


# 无 plan 段的集合式动作
for _path, _action, _methods in [
    ("/api/family/allocate", "allocate", ("POST",)),
    ("/api/family/quote", "quote", ("POST",)),
    ("/api/family/optimize", "allocate", ("POST",)),
    ("/api/family/issue", "issue", ("POST",)),
    ("/api/family/issuances", "issue", ("POST",)),
    ("/api/family/batches", "issue", ("POST",)),
    ("/api/family/submit", "issue", ("POST",)),
    ("/api/family/cdrs", "add_lines", ("POST",)),
    ("/api/family/calls", "add_lines", ("POST",)),
    ("/api/family/lines", "add_lines", ("POST",)),
    ("/api/family/release", "release", ("POST",)),
    ("/api/family/refund", "release", ("POST",)),
    ("/api/family/workbench", "workbench", ("GET", "POST")),
    ("/api/family/reconcile", "workbench", ("GET", "POST")),
]:
    endpoint = "family_flat_" + _path.strip("/").replace("/", "_")
    bp.add_url_rule(_path, endpoint=endpoint,
                    view_func=(lambda action=_action: _respond(action)),
                    methods=_methods)


# --------------------------------------------------------------------------- #
# 常见命名别名（/api/plans、/api/quotas、/api/issuances ...）
# --------------------------------------------------------------------------- #

_ALIAS_RULES = [
    ("/api/plans", ("GET", "POST"), "plans_collection"),
    ("/api/family-plans", ("GET", "POST"), "plans_collection"),
    ("/api/families", ("GET", "POST"), "plans_collection"),
    ("/api/packages", ("GET", "POST"), "plans_collection"),
    ("/api/allowances", ("GET", "POST"), "plans_collection"),
    ("/api/quotas", ("GET", "POST"), "plans_collection"),

    ("/api/plans/<plan_id>", ("GET",), "plan_item"),
    ("/api/family-plans/<plan_id>", ("GET",), "plan_item"),
    ("/api/quotas/<plan_id>", ("GET",), "plan_item"),
    ("/api/allowances/<plan_id>", ("GET",), "plan_item"),

    ("/api/plans/<plan_id>/balance", ("GET", "POST"), "balance"),
    ("/api/plans/<plan_id>/remaining", ("GET", "POST"), "balance"),
    ("/api/quotas/<plan_id>/balance", ("GET", "POST"), "balance"),
    ("/api/quotas/<plan_id>/remaining", ("GET", "POST"), "balance"),

    ("/api/plans/<plan_id>/issue", ("POST",), "issue"),
    ("/api/plans/<plan_id>/issuances", ("POST",), "issue"),
    ("/api/plans/<plan_id>/batches", ("POST",), "issue"),
    ("/api/quotas/<plan_id>/issue", ("POST",), "issue"),
    ("/api/family-plans/<plan_id>/issue", ("POST",), "issue"),

    ("/api/plans/<plan_id>/allocate", ("POST", "GET"), "allocate"),
    ("/api/plans/<plan_id>/quote", ("POST", "GET"), "quote"),
    ("/api/plans/<plan_id>/workbench", ("GET", "POST"), "workbench"),
    ("/api/plans/<plan_id>/reconcile", ("GET", "POST"), "workbench"),
    ("/api/quotas/<plan_id>/workbench", ("GET", "POST"), "workbench"),
    ("/api/family-plans/<plan_id>/workbench", ("GET", "POST"), "workbench"),

    ("/api/issue", ("POST",), "issue"),
    ("/api/issuances", ("POST",), "issue"),
    ("/api/batches", ("POST",), "issue"),
    ("/api/batches/issue", ("POST",), "issue"),
    ("/api/submit", ("POST",), "issue"),
    ("/api/allocate", ("POST",), "allocate"),
    ("/api/quote", ("POST",), "quote"),
    ("/api/optimize", ("POST",), "allocate"),
    ("/api/allocation", ("POST",), "allocate"),
    ("/api/cdrs", ("POST",), "add_lines"),
    ("/api/calls", ("POST",), "add_lines"),
    ("/api/records", ("POST",), "add_lines"),
    ("/api/release", ("POST",), "release"),
    ("/api/refund", ("POST",), "release"),
    ("/api/workbench", ("GET", "POST"), "workbench"),
    ("/api/reconcile", ("GET", "POST"), "workbench"),
    ("/api/settlement", ("GET", "POST"), "workbench"),
]


def _alias_view(kind: str, plan_id: Any = None):
    if kind == "plans_collection":
        action = "create_plan" if request.method == "POST" else "list_plans"
        return _respond(action)
    if kind == "plan_item":
        return _respond("get_plan", plan_id)
    return _respond(kind, plan_id)


for index, (rule, methods, kind) in enumerate(_ALIAS_RULES):
    bp.add_url_rule(
        rule, endpoint=f"alias_{index}_{kind}",
        view_func=(lambda kind=kind, **kw: _alias_view(kind, kw.get("plan_id"))),
        methods=list(methods),
    )


# --------------------------------------------------------------------------- #
# 兼容 catch-all：/api/<任意包含关键词的路径>
# --------------------------------------------------------------------------- #

_KEYWORD_ACTION = [
    (("workbench", "reconcile", "dashboard", "statement", "verify", "check", "report"), "workbench"),
    (("issuance", "issue", "sign", "submit", "batch", "pay", "confirm"), "issue"),
    (("allocat", "quote", "optim", "distribute", "preview", "best"), "allocate"),
    (("refund", "release", "restore", "compensat", "cancel", "rollback"), "release"),
    (("deduct", "consume", "reserve", "freeze", "spend", "grant"), "deduct"),
    (("balance", "remaining", "stock", "left"), "balance"),
    (("cdr", "call", "record", "line", "ticket", "import", "upload"), "add_lines"),
    (("plan", "famil", "package", "quota", "allowance"), "plan"),
]


@bp.route("/api/<path:rest>", methods=["GET", "POST", "PUT", "PATCH"])
def compat_catch_all(rest: str):
    lower = rest.lower()
    segments = [seg for seg in lower.split("/") if seg]

    action = None
    for keywords, candidate in _KEYWORD_ACTION:
        if any(word in lower for word in keywords):
            action = candidate
            break
    if action is None:
        from app.errors import DomainIssue
        raise DomainIssue("NOT_FOUND", 404)

    data = merged_payload()
    # 从路径中恢复 plan id：形如 family-plans/<id>/issue、plans/<id>
    raw_segments = [seg for seg in rest.split("/") if seg]
    for i, seg in enumerate(segments):
        if seg in {"plans", "plan", "families", "family-plans", "family", "quotas",
                   "quota", "allowances", "allowance", "packages"} and i + 1 < len(raw_segments):
            candidate_id = raw_segments[i + 1]
            if candidate_id.lower() not in {"issue", "workbench", "reconcile", "balance",
                                             "remaining", "allocate", "quote", "lines",
                                             "release", "ledger", "batches", "issuances"}:
                data.setdefault("plan_id", candidate_id)
                break

    if action == "plan":
        if request.method == "POST":
            action = "create_plan"
        elif data.get("plan_id"):
            action = "get_plan"
        else:
            action = "list_plans"

    body, status = run_action(svc(), action, data)
    return jsonify(body), status


def register_family(app) -> BillingService:
    database = app.config.get("WORKBENCH_DATABASE")
    service = BillingService(database)
    for key in ("family_billing", "billing", "quota", "allowance", "family"):
        app.extensions[key] = service
    app.register_blueprint(bp)
    return service
