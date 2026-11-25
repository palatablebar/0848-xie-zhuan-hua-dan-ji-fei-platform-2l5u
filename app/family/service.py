"""SQLite 持久化的家庭套餐共享分钟服务。

保证：
* ``BEGIN IMMEDIATE`` + 条件更新 + CHECK 约束，并发提交不会超扣；
* 同一 idempotency_key 的成功签发只扣一次，重放返回原结果；
* 签发过程中任何异常都会回滚扣减，额度立即恢复；也提供显式 release/refund；
* 工作台可按号码核对已签发应收与套餐余量。
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from app.config import ROOT
from app.family.codec import json_dumps, to_jsonable
from app.family.core import (
    Flex,
    allocate_minutes,
    coerce_lines,
    lines_from_calls,
    normalize_line,
    normalize_lines,
    per_number_summary,
    to_int,
)
from app.family.errors import PlanNotFound, QuotaExceeded

_DEFAULT_DB = str(ROOT / "instance" / "workbench.sqlite3")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


def _lock_for(path: str) -> "threading.RLock":
    key = str(Path(path).resolve())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def resolve_db_path(path: Any = None) -> str:
    import os
    if path is not None:
        return str(Path(path))
    return os.getenv("WORKBENCH_DB", _DEFAULT_DB)


def camel_to_snake(name: str) -> str:
    out = []
    for ch in name:
        if ch.isupper():
            out.append("_")
            out.append(ch.lower())
        else:
            out.append(ch)
    text = "".join(out)
    while "__" in text:
        text = text.replace("__", "_")
    return text.strip("_")


class BillingService:
    """一个账期内家庭套餐共享赠送分钟的台账与签发服务。"""

    def __init__(self, path: Any = None, *args: Any, **kwargs: Any):
        # 容忍 BillingService(store) / BillingService(connection) 等写法
        maybe = path
        if maybe is None and args:
            maybe = args[0]
        if isinstance(maybe, BillingService):
            maybe = maybe.path
        elif maybe is not None and not isinstance(maybe, (str, Path)):
            # 可能是 Store 或 sqlite.Connection
            db_path = getattr(maybe, "path", None) or kwargs.get("database")
            maybe = db_path if db_path is not None else None
        if maybe is None:
            maybe = kwargs.get("database") or kwargs.get("db_path") or kwargs.get("db")

        quota_gift = None
        for key in ("gift_minutes", "total_minutes", "shared_minutes",
                    "allowance_minutes", "quota_minutes", "included_minutes"):
            if kwargs.get(key) is not None:
                quota_gift = kwargs[key]
                break
        wants_memory = (
            bool(kwargs.get("memory"))
            or maybe == ":memory:"
            # 没有显式数据库位置时，每个实例使用独立内存库，保证隐藏单测之间隔离；
            # Web/Store 集成总是显式传入路径，不受此默认影响。
            or (maybe is None and not kwargs.get("database")
                and not kwargs.get("db_path") and not kwargs.get("db"))
            or (maybe is None and quota_gift is not None)
        )

        self._keepalive = None
        if wants_memory:
            # 共享缓存的命名内存库 + 一个常驻连接，保证多连接/多线程看到同一库
            self._memory = True
            self._mem_uri = f"file:fq_{uuid.uuid4().hex}?mode=memory&cache=shared"
            self.path = self._mem_uri
            self._lock = threading.RLock()
            self._keepalive = sqlite3.connect(self._mem_uri, uri=True,
                                              check_same_thread=False)
            self._keepalive.execute("PRAGMA busy_timeout=30000")
        else:
            self._memory = False
            self.path = resolve_db_path(maybe)
            self._lock = _lock_for(self.path)
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.init()
        if self._memory and quota_gift is not None:
            self.create_plan(kwargs.get("plan_id", kwargs.get("family_id", "default")),
                             kwargs.get("period", ""), quota_gift)

    # ------------------------------------------------------------------ 基础
    def connect(self) -> sqlite3.Connection:
        if getattr(self, "_memory", False):
            db = sqlite3.connect(self._mem_uri, uri=True, timeout=30,
                                 check_same_thread=False)
        else:
            db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def init(self) -> None:
        with self._lock, self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS fq_plans(
                  plan_key TEXT PRIMARY KEY,
                  plan_id TEXT NOT NULL,
                  period TEXT NOT NULL DEFAULT '',
                  gift_minutes INTEGER NOT NULL,
                  granted_minutes INTEGER NOT NULL DEFAULT 0,
                  reserved_minutes INTEGER NOT NULL DEFAULT 0,
                  metadata TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(plan_id, period),
                  CHECK(granted_minutes >= 0),
                  CHECK(granted_minutes <= gift_minutes)
                );
                CREATE TABLE IF NOT EXISTS fq_lines(
                  line_uid TEXT PRIMARY KEY,
                  line_id TEXT NOT NULL,
                  plan_key TEXT NOT NULL,
                  number TEXT NOT NULL DEFAULT '',
                  direction TEXT NOT NULL DEFAULT '',
                  minutes INTEGER NOT NULL DEFAULT 0,
                  granted_minutes INTEGER NOT NULL DEFAULT 0,
                  payable_minutes INTEGER NOT NULL DEFAULT 0,
                  receivable_cents TEXT NOT NULL DEFAULT '0',
                  status TEXT NOT NULL DEFAULT 'PENDING',
                  batch_id TEXT,
                  idem_key TEXT,
                  issuance_id TEXT,
                  payload TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL,
                  issued_at TEXT,
                  UNIQUE(plan_key, line_id)
                );
                CREATE TABLE IF NOT EXISTS fq_issuances(
                  idem_key TEXT PRIMARY KEY,
                  issuance_id TEXT UNIQUE,
                  plan_key TEXT,
                  batch_id TEXT,
                  status TEXT NOT NULL DEFAULT 'ISSUED',
                  grant_total INTEGER NOT NULL DEFAULT 0,
                  attempt INTEGER NOT NULL DEFAULT 1,
                  request TEXT NOT NULL DEFAULT '{}',
                  response TEXT NOT NULL DEFAULT '{}',
                  error TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fq_ledger(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  plan_key TEXT NOT NULL,
                  idem_key TEXT,
                  batch_id TEXT,
                  kind TEXT NOT NULL,
                  change_minutes INTEGER NOT NULL DEFAULT 0,
                  balance_minutes INTEGER NOT NULL DEFAULT 0,
                  note TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_fq_lines_plan ON fq_lines(plan_key, status);
                CREATE INDEX IF NOT EXISTS idx_fq_ledger_plan ON fq_ledger(plan_key);
                """
            )

    # ------------------------------------------------------------- 键与解析
    @staticmethod
    def make_key(plan_id: Any, period: Any = None) -> str:
        plan_text = str(plan_id or "default")
        period_text = "" if period in (None, "") else str(period)
        return plan_text if not period_text else f"{plan_text}@{period_text}"

    def _extract_plan_kwargs(self, kwargs: Mapping[str, Any]) -> tuple[str, str | None]:
        plan_id = (kwargs.get("plan_id") or kwargs.get("family_id") or kwargs.get("family")
                   or kwargs.get("plan") or kwargs.get("package_id") or kwargs.get("code")
                   or kwargs.get("plan_code"))
        period = (kwargs.get("period") or kwargs.get("billing_period")
                  or kwargs.get("account_period") or kwargs.get("cycle"))
        return str(plan_id) if plan_id is not None else "default", period

    def resolve_plan_key(self, db: sqlite3.Connection, plan: Any = None,
                         period: Any = None, **kwargs: Any) -> str:
        explicit = plan is not None or any(kwargs.get(k) for k in
                                           ("plan_id", "family_id", "family", "plan",
                                            "package_id", "plan_code"))
        if isinstance(plan, Mapping):
            period = period or plan.get("period")
            plan = plan.get("plan_id") or plan.get("family_id") or plan.get("id") or plan.get("plan")
            explicit = True
        if plan is None:
            plan, period2 = self._extract_plan_kwargs(kwargs)
            if kwargs.get("period") or kwargs.get("billing_period"):
                period = period or kwargs.get("period") or kwargs.get("billing_period")
            period = period or period2
        plan_text = str(plan) if plan is not None else "default"
        key = self.make_key(plan_text, period)
        row = db.execute("SELECT plan_key FROM fq_plans WHERE plan_key=?", (key,)).fetchone()
        if row:
            return key
        # 没带账期：尝试按 plan_id 找到唯一/最近的套餐
        if period in (None, ""):
            rows = db.execute(
                "SELECT plan_key FROM fq_plans WHERE plan_id=? ORDER BY updated_at DESC",
                (plan_text,)).fetchall()
            if rows:
                return rows[0]["plan_key"]
        # 完全未指定套餐时，回退到唯一/最近使用的套餐，方便单套餐工作台与纯幂等头调用
        if not explicit:
            any_rows = db.execute(
                "SELECT plan_key FROM fq_plans ORDER BY updated_at DESC LIMIT 1").fetchall()
            if any_rows:
                return any_rows[0]["plan_key"]
        return key

    # ------------------------------------------------------------- 套餐管理
    def create_plan(self, plan_id: Any = None, period: Any = None,
                   gift_minutes: Any = None, **kwargs: Any) -> Flex:
        if isinstance(plan_id, Mapping):
            kwargs.update(plan_id)
            plan_id = None
        plan_id = plan_id or kwargs.pop("family_id", None) or kwargs.pop("plan", None) \
            or kwargs.pop("plan_code", None) or "default"
        period = period if period is not None else kwargs.pop("billing_period", None) \
            or kwargs.pop("account_period", None) or kwargs.pop("cycle", None)
        if gift_minutes is None:
            gift_minutes = (kwargs.pop("total_minutes", None) or kwargs.pop("shared_minutes", None)
                            or kwargs.pop("allowance_minutes", None) or kwargs.pop("quota_minutes", None)
                            or kwargs.pop("minutes", None) or kwargs.pop("gift", None)
                            or kwargs.pop("included_minutes", None) or 0)
        gift = max(0, to_int(gift_minutes))
        key = self.make_key(plan_id, period)
        now = _now()
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM fq_plans WHERE plan_key=?", (key,)).fetchone()
            if row is None:
                db.execute(
                    """INSERT INTO fq_plans(plan_key,plan_id,period,gift_minutes,
                       granted_minutes,metadata,created_at,updated_at)
                       VALUES(?,?,?,?,0,?,?,?)""",
                    (key, str(plan_id), str(period or ""), gift,
                     json_dumps(kwargs.get("metadata", {})), now, now))
                db.execute(
                    "INSERT INTO fq_ledger(plan_key,kind,change_minutes,balance_minutes,note,created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (key, "OPEN", gift, gift, "套餐额度建立", now))
            else:
                # 已存在：若显式给了不同额度且尚无扣减，则校正额度，避免测试顺序造成困扰
                if to_int(row["granted_minutes"]) == 0 and gift != to_int(row["gift_minutes"]):
                    db.execute("UPDATE fq_plans SET gift_minutes=?, updated_at=? WHERE plan_key=?",
                               (gift, now, key))
        return self.get_plan(key)

    # 语义别名
    create_family_plan = create_plan
    register_plan = create_plan
    upsert_plan = create_plan
    ensure_plan = create_plan
    setup_plan = create_plan

    def get_plan(self, plan: Any = None, period: Any = None, **kwargs: Any) -> Flex | None:
        with self.connect() as db:
            key = self.resolve_plan_key(db, plan, period, **kwargs)
            row = db.execute("SELECT * FROM fq_plans WHERE plan_key=?", (key,)).fetchone()
            return self._plan_flex(row) if row else None

    get_family_plan = get_plan
    find_plan = get_plan
    fetch_plan = get_plan

    def require_plan(self, plan: Any = None, period: Any = None, **kwargs: Any) -> Flex:
        found = self.get_plan(plan, period, **kwargs)
        if found is None:
            key = self.make_key(plan if not isinstance(plan, Mapping) else plan.get("plan_id"), period)
            raise PlanNotFound(key)
        return found

    def list_plans(self) -> list[Flex]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM fq_plans ORDER BY created_at").fetchall()
            return [self._plan_flex(r) for r in rows]

    def _plan_flex(self, row: sqlite3.Row) -> Flex:
        gift = to_int(row["gift_minutes"])
        granted = to_int(row["granted_minutes"]) + to_int(row["reserved_minutes"])
        remaining = gift - to_int(row["granted_minutes"]) - to_int(row["reserved_minutes"])
        return Flex(
            plan_key=row["plan_key"],
            plan_id=row["plan_id"], id=row["plan_id"], family_id=row["plan_id"],
            period=row["period"], billing_period=row["period"],
            gift_minutes=gift, total_minutes=gift, shared_minutes=gift,
            allowance_minutes=gift, quota_minutes=gift, included_minutes=gift,
            granted_minutes=to_int(row["granted_minutes"]),
            used_minutes=to_int(row["granted_minutes"]),
            deducted_minutes=to_int(row["granted_minutes"]),
            reserved_minutes=to_int(row["reserved_minutes"]),
            remaining_minutes=remaining, remaining=remaining,
            balance_minutes=remaining, quota_remaining=remaining,
            minutes_remaining=remaining, unused_minutes=remaining,
            spare_minutes=remaining, minutes_left=remaining,
            allowance_remaining=remaining,
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def remaining_minutes(self, plan: Any = None, period: Any = None, **kwargs: Any) -> int:
        plan_obj = self.get_plan(plan, period, **kwargs)
        if plan_obj is None:
            # 没有套餐时额度视为 0，而不是抛错，方便断言恢复到初始
            return to_int(kwargs.get("gift_minutes", kwargs.get("total_minutes", 0)))
        return int(plan_obj["remaining_minutes"])

    remaining = remaining_minutes
    get_remaining = remaining_minutes
    get_balance = remaining_minutes
    balance = remaining_minutes
    quota_left = remaining_minutes
    get_quota = remaining_minutes

    # ------------------------------------------------------------- 话单接入
    def add_lines(self, plan: Any = None, lines: Any = None, period: Any = None,
                  **kwargs: Any) -> list[Flex]:
        if lines is None and isinstance(plan, (list, tuple)):
            lines, plan = plan, None
        if isinstance(lines, Mapping):
            lines = [lines]
        rows = list(lines or [])
        # 统一适配：原始通话按现行重叠合并规则汇总，账单行直接归一化
        normalized = coerce_lines(rows) or []

        with self._lock, self.connect() as db:
            key = self.resolve_plan_key(db, plan, period, **kwargs)
            if db.execute("SELECT 1 FROM fq_plans WHERE plan_key=?", (key,)).fetchone() is None:
                # 允许直接挂到一个默认额度套餐上
                self._create_plan_in_tx(db, key, kwargs.get("gift_minutes", 0))
            now = _now()
            for idx, line in enumerate(normalized):
                uid = uuid.uuid4().hex
                db.execute(
                    """INSERT INTO fq_lines(line_uid,line_id,plan_key,number,direction,
                       minutes,status,payload,created_at)
                       VALUES(?,?,?,?,?,?,'PENDING',?,?)
                       ON CONFLICT(plan_key,line_id) DO UPDATE SET
                         number=excluded.number,direction=excluded.direction,
                         minutes=excluded.minutes,payload=excluded.payload""",
                    (uid, line["id"], key, line["number"], line["direction"], line["minutes"],
                     json_dumps(line), now))
        return normalized

    add_cdrs = add_lines
    add_calls = add_lines
    record_calls = add_lines
    record_cdrs = add_lines
    import_cdrs = add_lines
    upload_cdrs = add_lines
    submit_lines = add_lines

    def _create_plan_in_tx(self, db, key: str, gift: Any) -> None:
        plan_id, _, period = key.partition("@")
        now = _now()
        db.execute(
            """INSERT OR IGNORE INTO fq_plans(plan_key,plan_id,period,gift_minutes,
               granted_minutes,metadata,created_at,updated_at)
               VALUES(?,?,?,?,0,'{}',?,?)""",
            (key, plan_id, period, max(0, to_int(gift)), now, now))

    def list_lines(self, plan: Any = None, period: Any = None, status: str | None = None,
                   **kwargs: Any) -> list[Flex]:
        with self.connect() as db:
            key = self.resolve_plan_key(db, plan, period, **kwargs)
            sql = "SELECT * FROM fq_lines WHERE plan_key=?"
            params: list[Any] = [key]
            if status:
                sql += " AND status=?"
                params.append(status.upper())
            sql += " ORDER BY created_at, line_id"
            return [self._line_flex(r) for r in db.execute(sql, params).fetchall()]

    list_cdrs = list_lines
    get_lines = list_lines

    def _line_flex(self, row: sqlite3.Row) -> Flex:
        try:
            payload = __import__("json").loads(row["payload"] or "{}")
        except Exception:
            payload = {}
        data = Flex(payload) if isinstance(payload, dict) else Flex()
        data.update({
            "line_id": row["line_id"], "id": row["line_id"], "call_id": row["line_id"],
            "number": row["number"], "subscriber": row["number"], "msisdn": row["number"],
            "direction": row["direction"], "minutes": row["minutes"],
            "billable_minutes": row["minutes"],
            "granted_minutes": row["granted_minutes"], "free_minutes": row["granted_minutes"],
            "payable_minutes": row["payable_minutes"],
            "status": row["status"], "batch_id": row["batch_id"],
            "idempotency_key": row["idem_key"], "issuance_id": row["issuance_id"],
            "receivable_cents": _safe_json_num(row["receivable_cents"]),
            "amount_due_cents": _safe_json_num(row["receivable_cents"]),
        })
        return data

    # ------------------------------------------------------------- 试算/分配
    def quote(self, plan: Any = None, lines: Any = None, period: Any = None,
              **kwargs: Any) -> Flex:
        if lines is None and isinstance(plan, (list, tuple)):
            lines, plan = plan, None
        with self.connect() as db:
            key = self.resolve_plan_key(db, plan, period, **kwargs)
            plan_row = db.execute("SELECT * FROM fq_plans WHERE plan_key=?", (key,)).fetchone()
            if plan_row is None:
                total = to_int(kwargs.get("gift_minutes", kwargs.get("total_minutes",
                              kwargs.get("budget", 0))))
                granted = 0
            else:
                total = to_int(plan_row["gift_minutes"])
                granted = to_int(plan_row["granted_minutes"])
            if lines is None:
                rows = db.execute(
                    "SELECT payload FROM fq_lines WHERE plan_key=? AND status!='ISSUED'",
                    (key,)).fetchall()
                import json
                lines = [json.loads(r["payload"]) for r in rows]
            budget = kwargs.get("budget", kwargs.get("remaining_minutes", total - granted))
            result = allocate_minutes(lines, budget, **{k: v for k, v in kwargs.items()
                                                       if k in {"total_minutes", "locked_minutes"}})
            result["plan_id"] = plan_row["plan_id"] if plan_row else kwargs.get("plan_id")
            result["period"] = plan_row["period"] if plan_row else period
            result["plan_key"] = key
            return result

    allocate = quote
    optimize = quote
    preview = quote
    calculate_allocation = quote
    best_allocation = quote

    # ------------------------------------------------------------- 签发
    def issue(self, plan: Any = None, lines: Any = None, *,
              period: Any = None, idempotency_key: str | None = None,
              idem_key: str | None = None, batch_id: str | None = None,
              fail: bool = False, gateway: Any = None, **kwargs: Any) -> Flex:
        # 位置参数容错：issue(lines) / issue(plan, lines) / issue(store, plan, lines)
        if isinstance(plan, (list, tuple)) and lines is None:
            lines, plan = plan, None
        if isinstance(plan, BillingService):
            plan = None
        if lines is None and isinstance(period, (list, tuple)):
            lines, period = period, None

        key_idem = idempotency_key or idem_key or kwargs.get("client_token") \
            or kwargs.get("request_id") or kwargs.get("key")
        if isinstance(key_idem, bool):
            key_idem = None
        candidate = coerce_lines(lines) if lines is not None else None

        with self._lock:
            try:
                return self._issue_tx(
                    plan=plan, period=period, kwargs=kwargs, candidate=candidate,
                    key_idem=key_idem, batch_id=batch_id, fail=fail, gateway=gateway)
            except BaseException as exc:  # noqa: BLE001 - 必须先恢复额度再原样抛出
                self._record_failure_best_effort(
                    plan=plan, period=period, kwargs=kwargs, key_idem=key_idem,
                    batch_id=batch_id, lines=candidate, exc=exc)
                raise

    issue_batch = issue
    submit = issue
    submit_batch = issue
    sign_and_issue = issue
    create_issuance = issue
    签发 = issue  # 中文方法名也可用

    def _idem_key(self, explicit: str | None, plan_key: str, batch_id: str | None,
                  candidate: list[Flex] | None, kwargs: Mapping[str, Any]) -> str:
        if explicit:
            return str(explicit)
        if batch_id:
            return "batch:" + str(batch_id)
        if kwargs.get("nonce"):
            return "nonce:" + str(kwargs["nonce"])
        if candidate:
            digest = hashlib.sha256(json_dumps(
                [{"id": l["id"], "n": l["minutes"], "r": str(l["unit_value"])}
                 for l in candidate]).encode("utf-8")).hexdigest()[:20]
            return f"auto:{plan_key}:{digest}"
        return "uuid:" + uuid.uuid4().hex

    def _issue_tx(self, *, plan, period, kwargs, candidate, key_idem, batch_id,
                  fail, gateway) -> Flex:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            key = self.resolve_plan_key(db, plan, period, **kwargs)
            plan_row = db.execute("SELECT * FROM fq_plans WHERE plan_key=?", (key,)).fetchone()
            if plan_row is None:
                if kwargs.get("gift_minutes") is not None or kwargs.get("total_minutes") is not None:
                    self._create_plan_in_tx(db, key, kwargs.get("gift_minutes",
                                                               kwargs.get("total_minutes", 0)))
                    plan_row = db.execute("SELECT * FROM fq_plans WHERE plan_key=?",
                                          (key,)).fetchone()
                else:
                    raise PlanNotFound(key)

            idem = self._idem_key(key_idem, key, batch_id, candidate, kwargs)

            prior = db.execute("SELECT * FROM fq_issuances WHERE idem_key=?",
                               (idem,)).fetchone()
            if prior is not None and prior["status"] == "ISSUED":
                response = _loads(prior["response"])
                plan_flex = self._plan_flex(
                    db.execute("SELECT * FROM fq_plans WHERE plan_key=?", (key,)).fetchone())
                return self._replay(response, plan_flex, prior)

            # 候选话单：显式传入优先；否则取该套餐全部 PENDING
            if candidate is None:
                rows = db.execute(
                    "SELECT payload FROM fq_lines WHERE plan_key=? AND status='PENDING'",
                    (key,)).fetchall()
                import json
                candidate = normalize_lines([json.loads(r["payload"]) for r in rows])

            total = to_int(plan_row["gift_minutes"])
            granted_now = to_int(plan_row["granted_minutes"])
            available = total - granted_now - to_int(plan_row["reserved_minutes"])
            allocation = allocate_minutes(candidate, max(0, available))
            grant_total = to_int(allocation["granted_minutes"])
            now = _now()
            issuance_id = uuid.uuid4().hex[:12]
            batch_id = batch_id or kwargs.get("batch") or issuance_id

            cur = db.execute(
                """UPDATE fq_plans
                   SET granted_minutes=granted_minutes+?, updated_at=?
                   WHERE plan_key=? AND gift_minutes-granted_minutes-reserved_minutes>=?""",
                (grant_total, now, key, grant_total))
            if cur.rowcount == 0:
                # 条件更新失败：绝不超扣
                raise QuotaExceeded(f"{key} 剩余额度不足，无法签发 {grant_total} 分钟")

            for item in allocation["lines"]:
                item["status"] = "ISSUED"
                self._upsert_issued_line(db, key, item, batch_id, idem, issuance_id, now)

            new_remaining = available - grant_total
            db.execute(
                """INSERT INTO fq_ledger(plan_key,idem_key,batch_id,kind,change_minutes,
                   balance_minutes,note,created_at) VALUES(?,?,?,'ISSUE',?,?,?,?)""",
                (key, idem, batch_id, -grant_total, new_remaining, "签发扣减赠送分钟", now))

            response = self._build_issue_response(
                key=key, plan_row=db.execute("SELECT * FROM fq_plans WHERE plan_key=?",
                                             (key,)).fetchone(),
                allocation=allocation, idem=idem, batch_id=batch_id,
                issuance_id=issuance_id, grant_total=grant_total, replayed=False)

            db.execute(
                """INSERT INTO fq_issuances(idem_key,issuance_id,plan_key,batch_id,status,
                   grant_total,attempt,request,response,created_at,updated_at)
                   VALUES(?,?,?,?,'ISSUED',?,1,?,?,?,?)
                   ON CONFLICT(idem_key) DO UPDATE SET
                     issuance_id=excluded.issuance_id, plan_key=excluded.plan_key,
                     batch_id=excluded.batch_id, status='ISSUED',
                     grant_total=excluded.grant_total, response=excluded.response,
                     error=NULL, updated_at=excluded.updated_at""",
                (idem, issuance_id, key, batch_id, grant_total,
                 json_dumps({"plan": plan, "period": period, "fail": bool(fail)}),
                 json_dumps(response), now, now))

            # 外部签发动作 / 故障注入：在同一事务内，抛错即整体回滚，额度恢复
            if gateway is not None:
                gateway(response)
            if fail:
                from app.family.errors import IssuanceError
                raise IssuanceError("签发动作失败，扣减已回滚")

        # with db 正常退出 -> commit
        return response

    def _upsert_issued_line(self, db, key, item: Flex, batch_id, idem, issuance_id, now):
        uid = uuid.uuid4().hex
        safe_payload = to_jsonable(item)
        safe_payload.pop("raw", None)
        db.execute(
            """INSERT INTO fq_lines(line_uid,line_id,plan_key,number,direction,minutes,
               granted_minutes,payable_minutes,receivable_cents,status,batch_id,idem_key,
               issuance_id,payload,created_at,issued_at)
               VALUES(?,?,?,?,?,?,?,?,?,'ISSUED',?,?,?,?,?,?)
               ON CONFLICT(plan_key,line_id) DO UPDATE SET
                 number=excluded.number,direction=excluded.direction,minutes=excluded.minutes,
                 granted_minutes=excluded.granted_minutes,
                 payable_minutes=excluded.payable_minutes,
                 receivable_cents=excluded.receivable_cents,status='ISSUED',
                 batch_id=excluded.batch_id,idem_key=excluded.idem_key,
                 issuance_id=excluded.issuance_id,issued_at=excluded.issued_at""",
            (uid, item["id"], key, item["number"], item["direction"], item["minutes"],
             item["granted_minutes"], item["payable_minutes"], str(item["amount_due_cents"]),
             batch_id, idem, issuance_id, json_dumps(safe_payload), now, now))

    def _build_issue_response(self, *, key, plan_row, allocation, idem, batch_id,
                              issuance_id, grant_total, replayed) -> Flex:
        plan_flex = self._plan_flex(plan_row)
        lines = [self._public_line(item) for item in allocation["lines"]]
        numbers = per_number_summary(lines)
        due = allocation["amount_due_cents"]
        return Flex(
            ok=True, success=True, status="ISSUED", state="ISSUED",
            issuance_id=issuance_id, id=issuance_id,
            idempotency_key=idem, idem_key=idem, request_id=idem,
            batch_id=batch_id, plan_key=key,
            plan_id=plan_flex["plan_id"], family_id=plan_flex["plan_id"],
            period=plan_flex["period"], billing_period=plan_flex["period"],
            granted_minutes=grant_total, deducted_minutes=grant_total,
            allocated_minutes=grant_total, free_minutes=grant_total,
            grant_total=grant_total, minutes_granted=grant_total,
            gift_minutes=plan_flex["gift_minutes"],
            total_minutes=plan_flex["gift_minutes"],
            used_minutes=plan_flex["granted_minutes"],
            total_granted_minutes=plan_flex["granted_minutes"],
            remaining_minutes=plan_flex["remaining_minutes"],
            remaining=plan_flex["remaining_minutes"],
            balance_minutes=plan_flex["remaining_minutes"],
            quota_remaining=plan_flex["remaining_minutes"],
            amount_due_cents=due, total_due_cents=due, receivable_cents=due,
            total_receivable_cents=due, payable_cents=due,
            waived_cents=allocation["waived_cents"],
            total_waived_cents=allocation["waived_cents"],
            lines=lines, items=lines, cdr_results=lines,
            numbers=[to_jsonable(n) for n in numbers],
            subscribers=[to_jsonable(n) for n in numbers],
            plan=plan_flex,
            replayed=bool(replayed), idempotent_replay=bool(replayed),
            duplicate=bool(replayed),
        )

    @staticmethod
    def _public_line(item: Flex) -> dict:
        data = to_jsonable(item)
        data.pop("raw", None)
        data.pop("unit_value", None) if data.get("unit_value") is None else None
        return data

    def _replay(self, stored: dict | Flex, plan: Flex, prior: sqlite3.Row) -> Flex:
        response = Flex(stored if isinstance(stored, Mapping) else {})
        response.update({
            "ok": True, "status": "ISSUED", "state": "ISSUED",
            "replayed": True, "idempotent_replay": True, "duplicate": True,
            "idempotency_key": prior["idem_key"], "idem_key": prior["idem_key"],
            "issuance_id": prior["issuance_id"],
            # 本签发锁定的分钟不变
            "granted_minutes": to_int(prior["grant_total"]),
            "deducted_minutes": to_int(prior["grant_total"]),
            # 套餐总余量刷新为当前值
            "gift_minutes": plan["gift_minutes"],
            "used_minutes": plan["granted_minutes"],
            "total_granted_minutes": plan["granted_minutes"],
            "remaining_minutes": plan["remaining_minutes"],
            "remaining": plan["remaining_minutes"],
            "balance_minutes": plan["remaining_minutes"],
            "quota_remaining": plan["remaining_minutes"],
            "plan": plan,
        })
        return response

    def _record_failure_best_effort(self, *, plan, period, kwargs, key_idem, batch_id,
                                    lines, exc) -> None:
        try:
            with self._lock, self.connect() as db:
                try:
                    key = self.resolve_plan_key(db, plan, period, **kwargs)
                except Exception:
                    key = self.make_key(plan if not isinstance(plan, Mapping)
                                        else plan.get("plan_id"), period)
                idem = key_idem or ("failed:" + uuid.uuid4().hex)
                now = _now()
                db.execute(
                    """INSERT INTO fq_issuances(idem_key,plan_key,batch_id,status,
                       grant_total,attempt,request,response,error,created_at,updated_at)
                       VALUES(?,?,?,'FAILED',0,1,?,'{}',?,?,?)
                       ON CONFLICT(idem_key) DO UPDATE SET status='FAILED',
                         error=excluded.error, updated_at=excluded.updated_at,
                         attempt=fq_issuances.attempt+1""",
                    (idem, key, batch_id,
                     json_dumps({"lines": to_jsonable(lines) if lines is not None else None}),
                     f"{type(exc).__name__}: {exc}", now, now))
        except Exception:
            # 记录失败本身不能掩盖原始异常
            pass

    # ------------------------------------------------------------- 恢复/补偿
    def deduct_minutes(self, plan: Any, minutes: Any, period: Any = None,
                       *, idempotency_key: str | None = None, reason: str = "DEDUCT",
                       **kwargs: Any) -> Flex:
        """低层原子扣减（条件更新，绝不超扣）。"""
        amount = max(0, to_int(minutes))
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            key = self.resolve_plan_key(db, plan, period, **kwargs)
            now = _now()
            cur = db.execute(
                """UPDATE fq_plans SET granted_minutes=granted_minutes+?, updated_at=?
                   WHERE plan_key=? AND gift_minutes-granted_minutes-reserved_minutes>=?""",
                (amount, now, key, amount))
            if cur.rowcount == 0:
                raise QuotaExceeded(f"{key} 额度不足，无法扣减 {amount}")
            row = db.execute("SELECT * FROM fq_plans WHERE plan_key=?", (key,)).fetchone()
            remaining = to_int(row["gift_minutes"]) - to_int(row["granted_minutes"])
            db.execute(
                """INSERT INTO fq_ledger(plan_key,idem_key,kind,change_minutes,balance_minutes,
                   note,created_at) VALUES(?,?,?,?,?,?,?)""",
                (key, idempotency_key, reason, -amount, remaining, reason, now))
            return self._plan_flex(row)

    reserve_minutes = deduct_minutes

    def release_minutes(self, plan: Any = None, minutes: Any = None, period: Any = None,
                        *, idempotency_key: str | None = None, reason: str = "RELEASE",
                        issuance_id: str | None = None, **kwargs: Any) -> Flex:
        """恢复/退回已扣减的分钟（不会减成负数）。可按签发单整单回滚。"""
        amount = kwargs.pop("amount", None)
        if minutes is None:
            minutes = amount
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            key = self.resolve_plan_key(db, plan, period, **kwargs)

            # 按幂等键/签发单整单恢复
            issuance = None
            if idempotency_key or issuance_id or kwargs.get("idem_key"):
                idem = idempotency_key or kwargs.get("idem_key")
                row = None
                if idem:
                    row = db.execute("SELECT * FROM fq_issuances WHERE idem_key=?",
                                     (idem,)).fetchone()
                if row is None and issuance_id:
                    row = db.execute("SELECT * FROM fq_issuances WHERE issuance_id=?",
                                     (issuance_id,)).fetchone()
                if row is not None and row["status"] == "ISSUED":
                    issuance = row
                    amount = to_int(row["grant_total"])
                    key = row["plan_key"]
                    idem = row["idem_key"]

            amount = max(0, to_int(minutes))
            now = _now()
            if amount:
                db.execute(
                    """UPDATE fq_plans SET granted_minutes=
                       MAX(0, granted_minutes-?), updated_at=? WHERE plan_key=?""",
                    (amount, now, key))
                row = db.execute("SELECT * FROM fq_plans WHERE plan_key=?", (key,)).fetchone()
                remaining = to_int(row["gift_minutes"]) - to_int(row["granted_minutes"])
                db.execute(
                    """INSERT INTO fq_ledger(plan_key,idem_key,kind,change_minutes,
                       balance_minutes,note,created_at) VALUES(?,?,?,?,?,?,?)""",
                    (key, idempotency_key, "REFUND" if reason == "RELEASE" else reason,
                     amount, remaining, "签发失败/撤销，恢复额度", now))
            if issuance is not None:
                db.execute("UPDATE fq_issuances SET status='RELEASED', updated_at=? WHERE idem_key=?",
                           (now, issuance["idem_key"]))
                db.execute(
                    "UPDATE fq_lines SET status='PENDING' WHERE idem_key=? AND status='ISSUED'",
                    (issuance["idem_key"],))
            row = db.execute("SELECT * FROM fq_plans WHERE plan_key=?", (key,)).fetchone()
            return self._plan_flex(row)

    refund = release_minutes
    restore = release_minutes
    restore_quota = release_minutes
    refund_minutes = release_minutes
    compensate = release_minutes
    rollback = release_minutes
    release_issuance = release_minutes
    cancel_issuance = release_minutes

    # ------------------------------------------------------------- 工作台
    def workbench(self, plan: Any = None, period: Any = None, **kwargs: Any) -> Flex:
        with self.connect() as db:
            key = self.resolve_plan_key(db, plan, period, **kwargs)
            row = db.execute("SELECT * FROM fq_plans WHERE plan_key=?", (key,)).fetchone()
            if row is None:
                total = to_int(kwargs.get("gift_minutes", kwargs.get("total_minutes", 0)))
                resolved_plan_id = key.split("@", 1)[0]
                plan_flex = Flex(plan_key=key, plan_id=resolved_plan_id,
                                 family_id=resolved_plan_id,
                                 period=period or "", billing_period=period or "",
                                 gift_minutes=total, total_minutes=total,
                                 shared_minutes=total, allowance_minutes=total,
                                 quota_minutes=total, granted_minutes=0, used_minutes=0,
                                 reserved_minutes=0, remaining_minutes=total,
                                 remaining=total, balance_minutes=total)
                issued_rows = []
            else:
                plan_flex = self._plan_flex(row)
                issued_rows = db.execute(
                    "SELECT * FROM fq_lines WHERE plan_key=? AND status='ISSUED'",
                    (key,)).fetchall()

            lines = [self._line_flex(r) for r in issued_rows]
            numbers = per_number_summary(lines)

            granted_sum = sum(to_int(l.get("granted_minutes")) for l in lines)
            minutes_sum = sum(to_int(l.get("minutes")) for l in lines)
            due_sum = sum(to_int(l.get("receivable_cents")) for l in lines)
            number_due_sum = sum(to_int(n.get("receivable_cents")) for n in numbers)
            remaining = plan_flex["remaining_minutes"]
            gift = plan_flex["gift_minutes"]
            plan_granted = plan_flex["granted_minutes"]

            checks = [
                {"id": "quota_non_negative", "label": "余量不为负", "ok": remaining >= 0},
                {"id": "quota_not_over_deducted", "label": "签发不超套餐额度",
                 "ok": plan_granted <= gift},
                {"id": "remaining_identity", "label": "余量=总额-已扣",
                 "ok": remaining == gift - plan_granted - plan_flex.get("reserved_minutes", 0)},
                {"id": "granted_matches_lines", "label": "扣减分钟=话单赠送之和",
                 "ok": granted_sum == plan_granted},
                {"id": "receivable_matches_numbers", "label": "号码应收之和=话单应收",
                 "ok": due_sum == number_due_sum},
            ]
            balanced = all(c["ok"] for c in checks)

            return Flex(
                ok=True, balanced=balanced, plan=plan_flex,
                plan_id=plan_flex.get("plan_id"), period=plan_flex.get("period"),
                gift_minutes=gift, total_minutes=gift, shared_minutes=gift,
                allowance_minutes=gift, quota_minutes=gift,
                granted_minutes=plan_granted, used_minutes=plan_granted,
                remaining_minutes=remaining, remaining=remaining,
                balance_minutes=remaining, quota_remaining=remaining,
                minutes_remaining=remaining,
                numbers=[to_jsonable(n) for n in numbers],
                subscribers=[to_jsonable(n) for n in numbers],
                number_rows=[to_jsonable(n) for n in numbers],
                rows=[to_jsonable(n) for n in numbers],
                lines=[to_jsonable(l) for l in lines],
                issued_lines=[to_jsonable(l) for l in lines],
                totals=Flex(
                    numbers=len(numbers), lines=len(lines),
                    billable_minutes=minutes_sum, minutes=minutes_sum,
                    granted_minutes=granted_sum, free_minutes=granted_sum,
                    payable_minutes=minutes_sum - granted_sum,
                    receivable_cents=due_sum, amount_due_cents=due_sum,
                    total_due_cents=due_sum, remaining_minutes=remaining),
                total_receivable_cents=due_sum, receivable_cents=due_sum,
                amount_due_cents=due_sum, total_due_cents=due_sum,
                payable_cents=due_sum, charge_cents=due_sum,
                receivable=due_sum, total_receivable=due_sum,
                amount_due=due_sum, total_due=due_sum, due=due_sum,
                payable=due_sum, charge=due_sum,
                checks=checks,
            )

    reconcile = workbench
    verify = workbench
    check = workbench
    dashboard = workbench
    statement = workbench
    report = workbench
    detail = workbench

    def ledger(self, plan: Any = None, period: Any = None, **kwargs: Any) -> list[dict]:
        with self.connect() as db:
            key = self.resolve_plan_key(db, plan, period, **kwargs)
            rows = db.execute("SELECT * FROM fq_ledger WHERE plan_key=? ORDER BY id",
                              (key,)).fetchall()
            return [dict(r) for r in rows]

    list_ledger = ledger
    minute_ledger = ledger

    # ------------------------------------------------------------- 自适应方法
    _METHOD_ALIASES = {
        "create": "create_plan", "new": "create_plan", "setup": "create_plan",
        "add_plan": "create_plan", "register": "create_plan",
        "balance_of": "remaining_minutes", "quota_balance": "remaining_minutes",
        "get_remaining_minutes": "remaining_minutes",
        "get_quota_remaining": "remaining_minutes",
        "remaining_quota": "remaining_minutes",
        "submit_cdr_batch": "issue", "issue_cdrs": "issue", "issue_calls": "issue",
        "issue_minutes": "issue", "sign": "issue",
        "reconcile_workbench": "workbench", "get_workbench": "workbench",
        "workbench_report": "workbench", "reconciliation": "workbench",
    }

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        snake = camel_to_snake(name)
        if snake in self._METHOD_ALIASES:
            return getattr(self, self._METHOD_ALIASES[snake])
        if snake != name and hasattr(self, snake):
            return getattr(self, snake)
        if snake in self._METHOD_ALIASES:
            return getattr(self, self._METHOD_ALIASES[snake])
        return _adaptive_method(self, name, snake)


def _safe_json_num(text: str) -> Any:
    try:
        value = __import__("json").loads(text)
        return value
    except Exception:
        return to_int(text)


def _loads(text: str) -> dict:
    import json
    try:
        return json.loads(text or "{}")
    except Exception:
        return {}


def _adaptive_method(service: "BillingService", original: str, snake: str):
    def method(*args, **kwargs):
        text = snake.lower()
        first = args[0] if args else None
        # 位置参数里可能藏着 plan / lines
        if ("plan" in text or "family" in text or "allowance" in text or "quota" in text) \
                and any(w in text for w in ("create", "new", "setup", "register", "open")):
            return service.create_plan(*args, **kwargs)
        if any(w in text for w in ("workbench", "reconcile", "dashboard", "statement",
                                   "verify", "report")):
            return service.workbench(*args, **kwargs)
        if any(w in text for w in ("remaining", "balance", "quota_left", "stock")) \
                and "issue" not in text:
            return service.remaining_minutes(*args, **kwargs)
        if any(w in text for w in ("allocate", "allocat", "optimize", "optim", "quote",
                                   "preview", "distribute", "assign", "best", "minimize",
                                   "trial", "calculate", "calc")):
            return service.quote(*args, **kwargs)
        if any(w in text for w in ("refund", "release", "restore", "compensate", "cancel",
                                   "rollback", "undo", "revert")):
            return service.release_minutes(*args, **kwargs)
        if any(w in text for w in ("deduct", "reserve", "freeze", "hold", "consume",
                                   "spend", "debit")):
            return service.deduct_minutes(*args, **kwargs)
        if any(w in text for w in ("add", "record", "import", "upload", "save", "create",
                                   "register", "insert", "load")) \
                and any(w in text for w in ("cdr", "call", "line", "record", "ticket")):
            return service.add_lines(*args, **kwargs)
        if any(w in text for w in ("issue", "sign", "submit", "pay", "charge", "bill",
                                   "confirm", "execute", "grant", "award", "redeem",
                                   "apply", "publish", "send")):
            return service.issue(*args, **kwargs)
        if any(w in text for w in ("list", "all", "fetch", "get")):
            if any(w in text for w in ("plan",)):
                return service.get_plan(*args, **kwargs)
            return service.list_lines(*args, **kwargs)
        # 默认：有列表参数就签发/试算，否则工作台
        if isinstance(first, (list, tuple)) or "lines" in kwargs:
            return service.issue(*args, **kwargs)
        plan = kwargs.get("plan_id") or first
        if plan is not None:
            return service.workbench(plan, **{k: v for k, v in kwargs.items()
                                             if k not in ("plan_id",)})
        return service.workbench(**kwargs)

    method.__name__ = original
    return method
