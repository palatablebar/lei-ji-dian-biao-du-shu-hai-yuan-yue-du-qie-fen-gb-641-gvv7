"""不变量：守恒、幂等、月桶划分、账单闭合、读数分区。"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.modules.m5_months import weights
from app.modules.m6_batches import service
from fixtures import expected


def test_conservation_intervals_equal_months(run_result):
    totals = run_result["totals"]
    assert Decimal(totals["interval_usage"]) == Decimal(totals["month_usage"])
    assert Decimal(totals["interval_usage"]) == expected.TOTAL_USAGE


def test_idempotent_run_keeps_rows(settings, store, batch_id):
    before = store.count_rows(batch_id)
    totals_before = store.load_totals(batch_id)
    service.run(settings, store, sample="readings_dirty.csv")
    assert store.count_rows(batch_id) == before
    assert store.load_totals(batch_id) == totals_before


def test_month_slices_partition_every_interval(store, batch_id):
    for interval in store.load_intervals(batch_id):
        slices = weights.month_slices(
            dt.datetime.fromisoformat(interval["start_at"]),
            dt.datetime.fromisoformat(interval["end_at"]))
        assert sum(value for _, value in slices) == interval["days"]
        assert all(value > 0 for _, value in slices)


def test_bill_components_sum_equals_amount(store, batch_id):
    for bill in store.load_bills(batch_id):
        total = sum((Decimal(item["amount"]) for item in bill["components"]), Decimal("0"))
        assert total == bill["amount_yuan"]
        assert bill["usage_kwh"] > 0


def test_readings_partition_holds(store, batch_id):
    valid = store.load_readings(batch_id, status="valid")
    estimate = store.load_readings(batch_id, status="estimate")
    quarantine = store.load_quarantine(batch_id)
    totals = store.load_totals(batch_id)
    assert len(valid) + len(estimate) + len(quarantine) == int(totals["raw_lines"])
    assert len(valid) == expected.VALID
    assert len(estimate) == expected.ESTIMATE


def test_interval_usage_never_negative(store, batch_id):
    for interval in store.load_intervals(batch_id):
        assert interval["usage_kwh"] > 0
        assert interval["delta"] > 0
        assert interval["end_at"] > interval["start_at"]


def test_demand_charge_closed_to_published_kw(store, batch_id):
    """需量电费 = 发布月需量 × 需量电价（0.01 元四舍五入），逐行闭合。"""
    from decimal import ROUND_HALF_UP
    from app import config
    for row in store.load_demand(batch_id):
        expected = (row["published_demand_kw"] * row["demand_price"]).quantize(
            config.MONEY_QUANT, rounding=ROUND_HALF_UP)
        assert row["demand_charge_yuan"] == expected
        # 发布值永远不低于合同容量兜底，也不低于已观测峰值
        assert row["published_demand_kw"] >= row["floor_kw"]
        if row["observed_peak_kw"] is not None:
            assert row["published_demand_kw"] >= row["observed_peak_kw"]
        # 待追补标记与覆盖状态自洽
        assert row["needs_follow_up"] == (row["coverage_status"] != "full")


def test_demand_idempotent_rerun_keeps_rows(settings, store, batch_id):
    """同抄表包重跑：需量行数、发布值、电费逐位不变，不新增需量或电费。"""
    before = {(r["meter_id"], r["month"]): (r["published_demand_kw"],
                                             r["demand_charge_yuan"])
              for r in store.load_demand(batch_id)}
    service.run(settings, store, sample="readings_dirty.csv")
    after = {(r["meter_id"], r["month"]): (r["published_demand_kw"],
                                           r["demand_charge_yuan"])
             for r in store.load_demand(batch_id)}
    assert after == before
