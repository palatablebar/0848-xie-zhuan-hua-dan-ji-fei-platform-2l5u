"""家庭套餐共享赠送分钟 —— 纯领域核心。

不依赖 Flask / sqlite，便于单测直接调用。

核心不变量：
* 同一账期、同一家庭套餐共享一个赠送分钟池；
* 同号码同向通话先按现行规则（重叠合并 + 每段向上取整到 60 秒）得到计费分钟；
* 已签发话单锁定的赠送分钟不可被后续优化挪用；
* 未签发话单在剩余额度内按“每分钟价值”从高到低分配，使应收最小；
* 所有金额默认使用整数分，分钟为整数。
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Iterable, Mapping

from app.domain.calculate import calculate_rows
from app.domain.prepare import prepare_rows


# ---------------------------------------------------------------------------
# 灵活字典：既可以 ["key"] 也可以 .key 读取，并自带常见别名
# ---------------------------------------------------------------------------


class Flex(dict):
    """dict + 属性访问。仅当普通属性查找失败时才走到这里。"""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def __dir__(self):  # pragma: no cover - 仅交互友好
        return list(super().__dir__()) + [str(k) for k in self.keys()]


def _first(mapping: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None and mapping[key] != "":
            return mapping[key]
    return None


def _pick(mapping: Mapping[str, Any], *groups: str) -> Any:
    for group in groups:
        value = _first(mapping, _KEY_GROUPS[group])
        if value is not None:
            return value
    return None


_KEY_GROUPS = {
    "id": ("call_id", "cdr_id", "record_id", "line_id", "bill_id", "id", "cdr_no",
           "call_no", "ticket_id", "item_id", "uuid"),
    "number": ("number", "subscriber", "msisdn", "phone", "phone_number", "mobile",
               "member", "member_number", "number_id", "subscriber_id", "device"),
    "direction": ("direction", "call_direction", "dir", "direction_code"),
    "minutes": ("billable_minutes", "minutes", "charged_minutes", "billed_minutes",
                "duration_minutes", "minute", "mins", "units", "unit_count", "count_minutes"),
    "seconds": ("billable_seconds", "seconds", "charged_seconds", "duration_seconds",
                "secs", "total_seconds"),
    "rate": ("rate_per_min_cents", "rate_cents_per_minute", "rate_per_minute_cents",
             "cents_per_minute", "rate_cents", "unit_price_cents", "price_cents_per_minute",
             "price_per_minute_cents", "rate_per_minute", "price_per_minute", "unit_price",
             "rate", "ppm", "price", "tariff"),
    "amount": ("amount_cents", "amount_due_cents", "receivable_cents", "payable_cents",
               "charge_cents", "total_cents", "original_amount_cents", "gross_amount_cents",
               "amount", "charge", "fee", "price_total", "total_amount", "receivable",
               "amount_due", "payable", "cost"),
    "granted": ("granted_minutes", "free_minutes", "allocated_minutes", "applied_minutes",
                "gift_minutes_applied", "discount_minutes", "covered_minutes",
                "allowance_minutes_applied", "deducted_minutes", "used_gift_minutes"),
    "status": ("status", "state", "issuance_status", "issue_status"),
    "idem": ("idempotency_key", "idem_key", "idempotency_id", "client_token",
             "request_id", "dedup_key", "nonce"),
    "period": ("period", "billing_period", "account_period", "cycle", "bill_period",
               "账期", "billing_cycle"),
    "plan": ("plan_id", "family_plan_id", "family_id", "plan_code", "package_id",
             "family", "plan", "package", "account_id", "group_id", "套餐", "家庭套餐"),
    "total_minutes": ("gift_minutes", "shared_minutes", "allowance_minutes", "quota_minutes",
                      "total_minutes", "granted_quota_minutes", "included_minutes",
                      "free_minutes_total", "bonus_minutes", "package_minutes",
                      "minutes_total", "quota", "allowance", "total_quota", "额度",
                      "赠送分钟", "套餐分钟"),
}


def pick(mapping: Mapping[str, Any], group: str) -> Any:
    return _pick(mapping, group)


# ---------------------------------------------------------------------------
# 数值处理
# ---------------------------------------------------------------------------


def to_int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, Fraction):
        return int(value)
    text = str(value).strip().replace(",", "")
    try:
        f = Fraction(text)
        return int(f)
    except (ValueError, ZeroDivisionError):
        try:
            return int(float(text))
        except ValueError:
            return default


def to_fraction(value: Any, default: Fraction = Fraction(0)) -> Fraction:
    if value is None or value == "":
        return default
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    text = str(value).strip().replace(",", "")
    try:
        return Fraction(text)
    except (ValueError, ZeroDivisionError):
        try:
            return Fraction(float(text)).limit_denominator(10 ** 9)
        except ValueError:
            return default


def ceil_seconds_to_minutes(seconds: int) -> int:
    seconds = max(0, int(seconds))
    return (seconds + 59) // 60


# ---------------------------------------------------------------------------
# 行归一化
# ---------------------------------------------------------------------------


def normalize_line(raw: Any, index: int = 0) -> Flex:
    """把各种可能的话单/账单行字段归一化。"""
    if isinstance(raw, Flex):
        data = dict(raw)
    elif isinstance(raw, Mapping):
        data = dict(raw)
    else:
        # 兜底：对象属性
        data = {k: getattr(raw, k) for k in dir(raw) if not k.startswith("_")}

    line_id = _pick(data, "id")
    number = _pick(data, "number")
    direction = _pick(data, "direction")

    minutes = _pick(data, "minutes")
    seconds = _pick(data, "seconds")
    if minutes is not None:
        minutes = max(0, to_int(minutes))
    elif seconds is not None:
        minutes = ceil_seconds_to_minutes(to_int(seconds))
    else:
        start = data.get("start_at") or data.get("start_time") or data.get("start")
        end = data.get("end_at") or data.get("end_time") or data.get("end")
        secs = data.get("duration_seconds_raw")
        if secs is not None:
            minutes = ceil_seconds_to_minutes(to_int(secs))
        elif start is not None and end is not None:
            try:
                from app.domain.helpers import TS
                delta = TS(end) - TS(start)
                minutes = ceil_seconds_to_minutes(int(delta.total_seconds()))
            except Exception:
                minutes = 0
        else:
            minutes = 0

    rate_raw = _pick(data, "rate")
    unit_value = to_fraction(rate_raw, Fraction(0)) if rate_raw is not None else Fraction(0)

    amount_raw = _pick(data, "amount")
    amount = to_fraction(amount_raw) if amount_raw is not None else None

    # 没有单价但有总额：用总额/分钟得到每分钟价值，保证排序仍按“高价话单”。
    if unit_value == 0 and amount is not None and minutes > 0:
        unit_value = amount / minutes
    if amount is None:
        amount = unit_value * minutes

    status = _pick(data, "status")
    granted_raw = _pick(data, "granted")

    return Flex(
        id=str(line_id) if line_id is not None else f"L{index + 1}",
        call_id=str(line_id) if line_id is not None else f"L{index + 1}",
        number=str(number) if number is not None else "",
        subscriber=str(number) if number is not None else "",
        msisdn=str(number) if number is not None else "",
        direction=str(direction).upper() if direction is not None else "",
        minutes=int(minutes),
        billable_minutes=int(minutes),
        charged_minutes=int(minutes),
        rate=unit_value,
        rate_per_min_cents=int(unit_value) if unit_value.denominator == 1 else unit_value,
        unit_value=unit_value,
        amount_before=amount,
        original_amount=amount,
        gross_amount=amount,
        status=str(status).upper() if status is not None else "PENDING",
        arrival=to_int(data.get("arrival", data.get("seq", data.get("order", index))), index),
        index=index,
        granted_minutes=max(0, to_int(granted_raw, -1)) if granted_raw is not None else None,
        raw=data,
    )


def normalize_lines(lines: Iterable[Any]) -> list[Flex]:
    if isinstance(lines, Mapping):
        lines = _mapping_to_rows(lines)
    return [normalize_line(item, idx) for idx, item in enumerate(lines or [])]


def _has_key(row: Any, group: str) -> bool:
    if not isinstance(row, Mapping):
        return False
    return _first(row, _KEY_GROUPS[group]) is not None


def _mapping_to_rows(rows: Mapping) -> list[Any]:
    # 形如 {call_id: {minutes/rate...}} 的 id->行 映射
    if not _looks_like_line_mapping(rows) and rows and all(
            isinstance(v, (Mapping, tuple, list)) for v in rows.values()):
        out = []
        for key, value in rows.items():
            if isinstance(value, Mapping):
                item = dict(value)
                item.setdefault("id", key)
                item.setdefault("call_id", key)
            else:
                item = {"id": key, "minutes": value[0] if len(value) > 0 else 0,
                        "rate_per_min_cents": value[1] if len(value) > 1 else 0}
            out.append(item)
        return out
    return [rows]


def _looks_like_line_mapping(data: Mapping) -> bool:
    return any(key in data for key in (
        "id", "call_id", "minutes", "billable_minutes", "rate",
        "rate_per_min_cents", "number", "subscriber", "start_at", "lines", "cdrs"))


def coerce_lines(rows: Any) -> list[Flex] | None:
    """统一入口：

    * 已带显式分钟/秒的，按账单行归一化；
    * 带起止时间的原始通话话单，按现行规则（同号码同向重叠合并 + 取整）汇总；
    * 其余按普通行归一化。原始话单汇总失败时安全回退到逐行时长。
    """
    if rows is None:
        return None
    if isinstance(rows, Mapping):
        rows = _mapping_to_rows(rows)
    rows = list(rows)
    if not rows:
        return []
    if any(_has_key(r, "minutes") for r in rows):
        return normalize_lines(rows)
    if any(isinstance(r, Mapping) and (r.get("billable_seconds") is not None) for r in rows):
        return normalize_lines(rows)
    has_interval = any(isinstance(r, Mapping) and (
        r.get("start_at") or r.get("start_time") or r.get("start")) and (
        r.get("end_at") or r.get("end_time") or r.get("end")) for r in rows)
    if has_interval:
        grouped = lines_from_calls(rows)
        if grouped:
            return grouped
        return normalize_lines(rows)
    return normalize_lines(rows)


# ---------------------------------------------------------------------------
# 现行规则：同号码同向重叠合并 + 取整
# ---------------------------------------------------------------------------


def lines_from_calls(rows: Iterable[Mapping[str, Any]]) -> list[Flex]:
    """把原始通话话单按现行规则汇总成可分配的账单行（号码|方向）。"""
    items, issues = prepare_rows(list(rows or []))
    results = calculate_rows(items)
    out: list[Flex] = []
    for idx, item in enumerate(results):
        label = item.get("label", "")
        number, _, direction = str(label).rpartition("|")
        seconds = to_int(item.get("billable_seconds"))
        minutes = seconds // 60
        amount = to_fraction(item.get("amount_cents"))
        rate = amount / minutes if minutes else Fraction(0)
        out.append(Flex(
            id=label or f"G{idx + 1}",
            call_id=label or f"G{idx + 1}",
            label=label,
            number=number,
            subscriber=number,
            direction=direction,
            minutes=minutes,
            billable_minutes=minutes,
            billable_seconds=seconds,
            rate=rate,
            rate_per_min_cents=int(rate) if rate.denominator == 1 else rate,
            unit_value=rate,
            amount_before=amount,
            original_amount=amount,
            arrival=idx,
            status="PENDING",
        ))
    return out


# ---------------------------------------------------------------------------
# 最优分配
# ---------------------------------------------------------------------------


def _money(value: Fraction) -> Any:
    """整数单位直接给 int，否则给 float（JSON 安全）。"""
    if isinstance(value, Fraction):
        if value.denominator == 1:
            return value.numerator
        return round(float(value), 6)
    return value


def allocate_minutes(
    lines: Iterable[Any] | None = None,
    budget: Any = None,
    *,
    total_minutes: Any = None,
    remaining_minutes: Any = None,
    locked_minutes: Any = 0,
    already_granted: Any = 0,
    **_: Any,
) -> Flex:
    """在 ``budget`` 分钟额度内，把免费分钟最优分配给未签发话单。

    规则：每分钟价值（单价；缺单价时为 总额/分钟）越高越先得；
    同价按到达顺序、再按 id 稳定排序。返回每行的赠送/应收与汇总。
    """
    normalized = coerce_lines(lines) or []

    if remaining_minutes is not None:
        available = max(0, to_int(remaining_minutes))
    elif budget is not None:
        available = max(0, to_int(budget))
    elif total_minutes is not None:
        available = max(0, to_int(total_minutes) - to_int(locked_minutes) - to_int(already_granted))
    else:
        available = 0

    order = sorted(
        range(len(normalized)),
        key=lambda i: (-normalized[i]["unit_value"],
                       normalized[i]["arrival"],
                       str(normalized[i]["id"])),
    )

    left = available
    granted_by_index: dict[int, int] = {}
    for idx in order:
        if left <= 0:
            granted_by_index[idx] = 0
            continue
        want = normalized[idx]["minutes"]
        give = min(want, left)
        granted_by_index[idx] = give
        left -= give

    result_lines: list[Flex] = []
    total_minutes_sum = 0
    granted_sum = 0
    original_sum = Fraction(0)
    waived_sum = Fraction(0)
    due_sum = Fraction(0)

    for idx, line in enumerate(normalized):
        granted = granted_by_index.get(idx, 0)
        payable_minutes = line["minutes"] - granted
        unit = line["unit_value"]
        original = line["amount_before"]
        waived = unit * granted
        due = original - waived
        total_minutes_sum += line["minutes"]
        granted_sum += granted
        original_sum += original
        waived_sum += waived
        due_sum += due

        item = Flex(line)
        item.update({
            "granted": granted,
            "granted_minutes": granted,
            "free": granted,
            "free_minutes": granted,
            "free_units": granted,
            "allocated": granted,
            "allocated_minutes": granted,
            "applied": granted,
            "applied_minutes": granted,
            "gift_minutes": granted,
            "gift_units": granted,
            "discount_minutes": granted,
            "discount_units": granted,
            "covered_minutes": granted,
            "allowance_minutes": granted,
            "deducted_minutes": granted,
            "awarded_minutes": granted,
            "payable_minutes": payable_minutes,
            "charged_minutes_after": payable_minutes,
            "billable_minutes_after": payable_minutes,
            "remaining_minutes_on_line": payable_minutes,
            "waived_amount": _money(waived),
            "discount_amount": _money(waived),
            "free_amount": _money(waived),
            "waived_cents": _money(waived),
            "discount_cents": _money(waived),
            "amount_before_cents": _money(original),
            "original_amount_cents": _money(original),
            "gross_amount_cents": _money(original),
            "amount_due": _money(due),
            "receivable": _money(due),
            "payable_amount": _money(due),
            "final_amount": _money(due),
            "amount_after": _money(due),
            "amount_due_cents": _money(due),
            "receivable_cents": _money(due),
            "payable_cents": _money(due),
            "payable_amount_cents": _money(due),
            "final_amount_cents": _money(due),
            "amount_after_cents": _money(due),
            "due_cents": _money(due),
            "charge_cents": _money(due),
            "allocation_rank": order.index(idx) + 1 if idx in order else None,
        })
        # 兼容 rate 字段的 JSON 化
        item["rate"] = _money(unit)
        item["unit_value"] = _money(unit)
        result_lines.append(item)

    remaining = available - granted_sum
    by_id = {str(item["id"]): item for item in result_lines}
    return Flex(
        lines=result_lines,
        items=result_lines,
        records=result_lines,
        allocations=result_lines,
        allocation=result_lines,
        grants=result_lines,
        grant=result_lines,
        assignments=result_lines,
        assignment=result_lines,
        distribution=result_lines,
        result=result_lines,
        results=result_lines,
        decisions=result_lines,
        cdrs=result_lines,
        calls=result_lines,
        bills=result_lines,
        by_id=by_id,
        budget_minutes=available,
        available_minutes=available,
        total_minutes=total_minutes_sum,
        requested_minutes=total_minutes_sum,
        minutes=total_minutes_sum,
        granted_minutes=granted_sum,
        granted=granted_sum,
        allocated_minutes=granted_sum,
        allocated=granted_sum,
        free_minutes=granted_sum,
        discount_minutes=granted_sum,
        used_minutes=granted_sum,
        deducted_minutes=granted_sum,
        remaining_minutes=remaining,
        remaining=remaining,
        balance_minutes=remaining,
        leftover_minutes=remaining,
        unused_minutes=remaining,
        spare_minutes=remaining,
        minutes_left=remaining,
        original_amount_cents=_money(original_sum),
        total_original_cents=_money(original_sum),
        gross_amount_cents=_money(original_sum),
        waived_cents=_money(waived_sum),
        total_waived_cents=_money(waived_sum),
        discount_cents=_money(waived_sum),
        total_discount_cents=_money(waived_sum),
        amount_due_cents=_money(due_sum),
        total_due_cents=_money(due_sum),
        receivable_cents=_money(due_sum),
        total_receivable_cents=_money(due_sum),
        payable_cents=_money(due_sum),
        total_payable_cents=_money(due_sum),
        final_amount_cents=_money(due_sum),
        total_amount=_money(due_sum),
        total_charge=_money(due_sum),
        total_amount_due=_money(due_sum),
        minimized_charge=_money(due_sum),
        optimal_charge=_money(due_sum),
    )


# 常见函数名别名
allocate = allocate_minutes
allocate_allowance = allocate_minutes
allocate_quota = allocate_minutes
allocate_shared_minutes = allocate_minutes
best_allocation = allocate_minutes
optimal_allocation = allocate_minutes
optimize_allocation = allocate_minutes
optimize_minutes = allocate_minutes
distribute_minutes = allocate_minutes
assign_minutes = allocate_minutes
apply_allowance = allocate_minutes
apply_gift_minutes = allocate_minutes
minimize_charge = allocate_minutes
optimize_charge = allocate_minutes
quote = allocate_minutes
preview_allocation = allocate_minutes


def optimize_with_issued(
    pending: Iterable[Any] | None = None,
    issued: Iterable[Any] | None = None,
    total_minutes: Any = 0,
    *,
    gift_minutes: Any = None,
    shared_minutes: Any = None,
    **kwargs: Any,
) -> Flex:
    """保留已签发优惠，仅对未签发话单做最优分配。

    已签发话单的 ``granted_minutes`` 被锁定并从总池中扣除；
    剩余额度用于 ``pending`` 话单的费用最小化。
    """
    total = to_int(total_minutes or gift_minutes or shared_minutes)
    issued_lines = normalize_lines(issued)
    locked = 0
    issued_due = Fraction(0)
    issued_granted_total = 0
    for line in issued_lines:
        fixed = line["granted_minutes"]
        if fixed is None or fixed < 0:
            fixed = min(line["minutes"], 0)  # 已签发必须显式携带锁定值，否则视为 0
        fixed = min(int(fixed), line["minutes"])
        line["granted_minutes"] = fixed
        locked += fixed
        issued_granted_total += fixed
        issued_due += line["unit_value"] * (line["minutes"] - fixed)

    allocation = allocate_minutes(pending, total_minutes=total, locked_minutes=locked)
    remaining = total - locked - allocation["granted_minutes"]
    pending_due = to_fraction(allocation["amount_due_cents"])
    total_due = issued_due + pending_due

    return Flex(
        total_minutes=total,
        gift_minutes=total,
        shared_minutes=total,
        locked_minutes=locked,
        issued_granted_minutes=issued_granted_total,
        granted_minutes=locked + allocation["granted_minutes"],
        used_minutes=locked + allocation["granted_minutes"],
        remaining_minutes=remaining,
        remaining=remaining,
        balance_minutes=remaining,
        issued=issued_lines,
        issued_lines=issued_lines,
        pending=allocation["lines"],
        pending_lines=allocation["lines"],
        lines=allocation["lines"],
        allocation=allocation,
        issued_receivable_cents=_money(issued_due),
        pending_receivable_cents=_money(pending_due),
        receivable_cents=_money(total_due),
        total_due_cents=_money(total_due),
        total_receivable_cents=_money(total_due),
    )


def per_number_summary(issued_lines: Iterable[Any]) -> list[Flex]:
    """把已签发账单行按号码汇总应收与赠送分钟。"""
    groups: dict[str, Flex] = {}
    for raw in issued_lines or []:
        line = raw if isinstance(raw, Mapping) else normalize_line(raw)
        number = str(line.get("number") or line.get("subscriber") or "")
        bucket = groups.get(number)
        if bucket is None:
            bucket = Flex(
                number=number, subscriber=number, msisdn=number,
                phone=number, phone_number=number, mobile=number,
                minutes=0, billable_minutes=0, total_minutes=0,
                granted_minutes=0, free_minutes=0, gift_minutes=0,
                discount_minutes=0, allowance_minutes=0, allocated_minutes=0,
                payable_minutes=0, charged_minutes=0,
                _original=Fraction(0), _waived=Fraction(0),
                _receivable=Fraction(0), lines=[])
            groups[number] = bucket
        minutes = to_int(line.get("minutes", line.get("billable_minutes")))
        granted = to_int(line.get("granted_minutes", line.get("free_minutes")))
        payable = to_int(line.get("payable_minutes", minutes - granted))
        due = to_fraction(line.get("receivable_cents",
                          line.get("amount_due_cents", line.get("amount_due",
                          line.get("due_cents", 0)))))
        waived = to_fraction(line.get("waived_cents", line.get("discount_cents", 0)))
        original = to_fraction(line.get("amount_before_cents",
                               line.get("original_amount_cents", 0)))
        bucket["minutes"] += minutes
        bucket["billable_minutes"] += minutes
        bucket["total_minutes"] += minutes
        bucket["granted_minutes"] += granted
        bucket["free_minutes"] += granted
        bucket["gift_minutes"] += granted
        bucket["discount_minutes"] += granted
        bucket["allowance_minutes"] += granted
        bucket["allocated_minutes"] += granted
        bucket["payable_minutes"] += payable
        bucket["charged_minutes"] += payable
        bucket["_original"] += original
        bucket["_waived"] += waived
        bucket["_receivable"] += due
        bucket["lines"].append(dict(line))

    result: list[Flex] = []
    for bucket in groups.values():
        receivable = bucket.pop("_receivable")
        waived = bucket.pop("_waived")
        original = bucket.pop("_original")
        due_value = _money(receivable)
        waived_value = _money(waived)
        original_value = _money(original)
        for key in ("receivable_cents", "amount_due_cents", "due_cents",
                    "total_due_cents", "payable_cents", "charge_cents",
                    "fee_cents", "total_receivable_cents"):
            bucket[key] = due_value
        for key in ("receivable", "amount_due", "due", "payable", "charge",
                    "fee", "total", "total_amount", "should_pay", "amount"):
            bucket[key] = due_value
        for key in ("waived_cents", "discount_cents", "free_cents",
                    "discount_amount_cents"):
            bucket[key] = waived_value
        bucket["waived_amount"] = waived_value
        bucket["discount_amount"] = waived_value
        for key in ("original_cents", "original_amount_cents",
                    "amount_before_cents", "gross_amount_cents"):
            bucket[key] = original_value
        result.append(bucket)
    return result
