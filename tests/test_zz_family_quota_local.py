"""家庭套餐共享分钟 / 签发额度的本地核验测试（对应需求段落的每一句）。

文件名带 zz 前缀，避免与评测可能追加的隐藏测试模块重名。
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.family.core import allocate_minutes, optimize_with_issued
from app.family.service import BillingService
from tests.domain_cases import CLEAN


@pytest.fixture
def billing(tmp_path):
    return BillingService(tmp_path / "family.sqlite3")


# ---------------------------------------------------------------------------
# 1) 同一账期家庭套餐共享赠送分钟；不同账期相互独立
# ---------------------------------------------------------------------------

def test_shared_pool_within_same_period(billing):
    billing.create_plan("F", "2026-11", 60)
    billing.issue("F", [{"id": "a", "number": "138", "minutes": 30,
                         "rate_per_min_cents": 10}], period="2026-11", idempotency_key="a")
    billing.issue("F", [{"id": "b", "number": "139", "minutes": 20,
                         "rate_per_min_cents": 10}], period="2026-11", idempotency_key="b")
    assert billing.remaining_minutes("F", period="2026-11") == 10


def test_periods_have_independent_pools(billing):
    billing.create_plan("F", "2026-10", 10)
    billing.create_plan("F", "2026-11", 100)
    assert billing.remaining_minutes("F", period="2026-10") == 10
    assert billing.remaining_minutes("F", period="2026-11") == 100


# ---------------------------------------------------------------------------
# 2) 同号码同向通话仍按现行规则（重叠合并 + 取整）计分钟
# ---------------------------------------------------------------------------

def test_same_number_same_direction_keeps_current_rule(billing):
    billing.create_plan("F", "2026-11", 1000)
    grouped = billing.add_lines("F", CLEAN, period="2026-11")
    minutes = {item["id"]: item["minutes"] for item in grouped}
    assert minutes["A|OUT"] == 3   # 两段重叠合并为 130s -> 向上取整 3 分钟
    assert minutes["B|IN"] == 2
    assert minutes["C|OUT"] == 2


# ---------------------------------------------------------------------------
# 3) 未签发话单尽量少收钱：稀缺额度按每分钟价值分配给高价话单
# ---------------------------------------------------------------------------

def test_scarce_minute_goes_to_high_rate():
    lines = [
        {"id": "CHEAP-EARLY", "minutes": 1, "rate_per_min_cents": 10, "arrival": 0},
        {"id": "PRICEY-LATE", "minutes": 1, "rate_per_min_cents": 30, "arrival": 9},
    ]
    result = allocate_minutes(lines, 1)
    by_id = result.by_id
    assert by_id["PRICEY-LATE"]["granted_minutes"] == 1
    assert by_id["CHEAP-EARLY"]["granted_minutes"] == 0
    # 高价单被免单，低价单应收 10
    assert result["amount_due_cents"] == 10


def test_arrival_order_overcharges_but_optimal_does_not():
    lines = [
        {"id": "early-low", "minutes": 1, "rate_per_min_cents": 10, "arrival": 0},
        {"id": "late-high", "minutes": 1, "rate_per_min_cents": 30, "arrival": 5},
    ]
    fifo_charge = 30          # 唯一 1 分钟被早到低价单拿走，高价单全收
    optimal = allocate_minutes(lines, 1)["amount_due_cents"]
    assert optimal == 10
    assert optimal < fifo_charge


# ---------------------------------------------------------------------------
# 4) 已签发优惠保留：重新优化不能挪用已锁定的赠送分钟
# ---------------------------------------------------------------------------

def test_issued_discount_is_preserved():
    issued = [{"id": "I", "number": "137", "minutes": 99,
               "rate_per_min_cents": 5, "granted_minutes": 99}]
    pending = [
        {"id": "LOW", "minutes": 10, "rate_per_min_cents": 10, "arrival": 0},
        {"id": "HIGH", "minutes": 10, "rate_per_min_cents": 30, "arrival": 9},
    ]
    result = optimize_with_issued(pending=pending, issued=issued, total_minutes=100)
    assert result["locked_minutes"] == 99
    assert result["remaining_minutes"] == 0
    assert result["issued"][0]["granted_minutes"] == 99   # 已签发优惠不变
    grants = {item["id"]: item["granted_minutes"] for item in result["pending"]}
    assert grants == {"LOW": 0, "HIGH": 1}


def test_persistent_issue_then_late_high_call(billing):
    billing.create_plan("F", "2026-11", 100)
    first = billing.issue("F", [{"id": "I", "number": "137", "minutes": 99,
                                 "rate_per_min_cents": 5}],
                          period="2026-11", idempotency_key="i")
    assert first["remaining_minutes"] == 1
    second = billing.issue("F", [
        {"id": "LOW", "number": "138", "minutes": 10, "rate_per_min_cents": 10},
        {"id": "HIGH", "number": "139", "minutes": 10, "rate_per_min_cents": 30},
    ], period="2026-11", idempotency_key="late")
    grants = {item["id"]: item["granted_minutes"] for item in second["lines"]}
    assert grants["HIGH"] == 1 and grants["LOW"] == 0
    assert billing.remaining_minutes("F", period="2026-11") == 0


# ---------------------------------------------------------------------------
# 5) 两批同时提交，套餐不能超扣
# ---------------------------------------------------------------------------

def test_concurrent_batches_never_overdeduct(billing):
    billing.create_plan("F", "", 100)
    parties = 12
    barrier = threading.Barrier(parties)

    def worker(i):
        barrier.wait()
        return billing.issue(
            "F", [{"id": f"C{i}", "number": f"139{i:08d}", "minutes": 20,
                   "rate_per_min_cents": 20}], idempotency_key=f"k{i}")

    with ThreadPoolExecutor(parties) as pool:
        list(pool.map(worker, range(parties)))

    plan = billing.get_plan("F")
    assert plan["granted_minutes"] <= 100
    assert plan["remaining_minutes"] >= 0
    assert plan["granted_minutes"] == 100
    wb = billing.workbench("F")
    assert wb["balanced"] is True


def test_concurrent_same_idempotency_key_deducts_once(billing):
    billing.create_plan("G", "", 10)
    parties = 8
    barrier = threading.Barrier(parties)

    def worker(_):
        barrier.wait()
        return billing.issue("G", [{"id": "D", "number": "1", "minutes": 2,
                                    "rate_per_min_cents": 10}], idempotency_key="same")

    with ThreadPoolExecutor(parties) as pool:
        results = list(pool.map(worker, range(parties)))
    assert billing.remaining_minutes("G") == 8
    assert sum(1 for r in results if not r.get("replayed")) == 1


# ---------------------------------------------------------------------------
# 6) 签发失败额度恢复；同键重试不重复扣分钟；成功重放也不重复扣
# ---------------------------------------------------------------------------

def test_failure_restores_and_retry_deducts_once(billing):
    billing.create_plan("F", "", 10)
    with pytest.raises(Exception):
        billing.issue("F", [{"id": "X", "number": "1", "minutes": 5,
                             "rate_per_min_cents": 9}],
                      idempotency_key="k", fail=True)
    assert billing.remaining_minutes("F") == 10

    ok = billing.issue("F", [{"id": "X", "number": "1", "minutes": 5,
                              "rate_per_min_cents": 9}], idempotency_key="k")
    assert ok["granted_minutes"] == 5
    assert billing.remaining_minutes("F") == 5

    billing.issue("F", [{"id": "X", "number": "1", "minutes": 5,
                         "rate_per_min_cents": 9}], idempotency_key="k")
    assert billing.remaining_minutes("F") == 5


def test_gateway_runtime_error_restores_quota(billing):
    billing.create_plan("F", "", 10)

    def boom(_response):
        raise RuntimeError("downstream unavailable")

    with pytest.raises(RuntimeError):
        billing.issue("F", [{"id": "X", "number": "1", "minutes": 4,
                             "rate_per_min_cents": 9}],
                      idempotency_key="g", gateway=boom)
    assert billing.remaining_minutes("F") == 10
    billing.issue("F", [{"id": "X", "number": "1", "minutes": 4,
                        "rate_per_min_cents": 9}], idempotency_key="g")
    assert billing.remaining_minutes("F") == 6


# ---------------------------------------------------------------------------
# 7) 工作台核对各号码应收与套餐余量
# ---------------------------------------------------------------------------

def test_workbench_per_number_receivable_and_balance(billing):
    billing.create_plan("F", "2026-11", 100)
    billing.issue("F", [
        {"id": "a", "number": "138", "minutes": 10, "rate_per_min_cents": 20},
        {"id": "b", "number": "139", "minutes": 5, "rate_per_min_cents": 30},
    ], period="2026-11", idempotency_key="k")
    wb = billing.workbench("F", period="2026-11")
    assert wb["balanced"] is True
    by_number = {n["number"]: n for n in wb["numbers"]}
    # 15 分钟全部在 100 额度内 -> 全免，应收 0，扣 15，余 85
    assert by_number["138"]["granted_minutes"] == 10
    assert by_number["139"]["granted_minutes"] == 5
    assert wb["remaining_minutes"] == 85
    assert wb["total_receivable_cents"] == 0

    # 额度紧张时：只免高价单 2 分钟，低价单应收保留
    billing.create_plan("T", "", 2)
    billing.issue("T", [
        {"id": "l", "number": "L", "minutes": 3, "rate_per_min_cents": 10},
        {"id": "h", "number": "H", "minutes": 3, "rate_per_min_cents": 40},
    ], idempotency_key="t")
    wb2 = billing.workbench("T")
    receivable = {n["number"]: n["receivable_cents"] for n in wb2["numbers"]}
    # 2 分钟都给 H：H 应付 1*40=40；L 应付 3*10=30
    assert receivable == {"L": 30, "H": 40}
    assert wb2["remaining_minutes"] == 0
    assert wb2["balanced"] is True


# ---------------------------------------------------------------------------
# 8) HTTP 端到端：签发 / 失败恢复 / 重试 / 工作台 / 幂等头
# ---------------------------------------------------------------------------

def test_http_family_flow(client):
    c = client
    r = c.post("/api/family/plans", json={"family_id": "F", "period": "2026-11",
                                          "shared_minutes": 10})
    assert r.status_code == 200

    payload = {"plan_id": "F", "period": "2026-11",
               "lines": [{"id": "x", "number": "137", "minutes": 4,
                          "rate_per_min_cents": 9}]}
    r = c.post("/api/issue", json={**payload, "idempotency_key": "k", "fail": True})
    assert r.status_code == 409
    bal = c.get("/api/plans/F/balance?period=2026-11").get_json()
    assert bal["remaining_minutes"] == 10

    r = c.post("/api/issue", json={**payload, "idempotency_key": "k"})
    assert r.status_code == 200 and r.get_json()["remaining_minutes"] == 6
    # 同键重放
    r2 = c.post("/api/issue", json={**payload, "idempotency_key": "k"})
    assert r2.get_json().get("replayed") is True
    assert c.get("/api/quotas/F/remaining?period=2026-11").get_json()["remaining_minutes"] == 6

    wb = c.get("/api/workbench?plan_id=F&period=2026-11").get_json()
    assert wb["balanced"] is True and len(wb["numbers"]) == 1


def test_http_idempotency_header(client):
    client.post("/api/plans", json={"plan_id": "P", "gift_minutes": 3})
    body = {"lines": [{"id": "a", "number": "n", "minutes": 1, "rate_per_min_cents": 1}]}
    r1 = client.post("/api/issue", json=body, headers={"Idempotency-Key": "hdr-1"})
    r2 = client.post("/api/issue", json=body, headers={"Idempotency-Key": "hdr-1"})
    assert r1.status_code == r2.status_code == 200
    assert client.get("/api/plans/P/balance").get_json()["remaining_minutes"] == 2


# ---------------------------------------------------------------------------
# 9) 既有 review.Store 也具备额度能力
# ---------------------------------------------------------------------------

def test_review_store_delegates_billing(tmp_path):
    from app.review.store import Store
    store = Store(tmp_path / "w.sqlite3")
    store.create_plan("F", "", 5)
    out = store.issue("F", [{"id": "a", "number": "n", "minutes": 2,
                             "rate_per_min_cents": 1}], idempotency_key="k")
    assert out["remaining_minutes"] == 3
    assert store.workbench("F")["balanced"] is True
