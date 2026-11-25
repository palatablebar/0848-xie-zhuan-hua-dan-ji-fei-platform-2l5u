"""JSON 安全转换（Flex / Decimal / Fraction / datetime / Path）。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path


def to_jsonable(value):
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, Fraction):
        return value.numerator if value.denominator == 1 else round(float(value), 6)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def json_dumps(value) -> str:
    import json
    return json.dumps(to_jsonable(value), ensure_ascii=False, sort_keys=True, default=str)
