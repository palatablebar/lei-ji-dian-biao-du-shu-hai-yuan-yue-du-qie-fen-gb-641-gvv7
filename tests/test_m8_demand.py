"""M8 月需量：清零周期取最高值、观察覆盖、合同容量兜底、缺证追补、幂等。

需量与电度是两条账：这里所有断言都围绕「峰值不累加」和「缺证周期如何
形成可发布值」展开，金额与需量全部 Decimal 手算，不用容差。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app import config
from app.main import create_app
from app.modules.m2_contract import contract as contract_mod
from app.modules.m8_demand import cycle as cycle_mod
from app.modules.m8_demand import publish as publish_mod
from app.modules.m8_demand import service as demand_service
from fastapi.testclient import TestClient

FLOOR_RATIO = config.DEMAND_FLOOR_RATIO
PRICE = Decimal("39.00")


def _contract(meter_id: str = "M0009", capacity: str = "800",
              multiplier: str = "1") -> contract_mod.MeterContract:
    return contract_mod.MeterContract(
        meter_id=meter_id, name=meter_id, capacity=Decimal(capacity),
        multiplier=Decimal(multiplier), unit="kWh", registers=("DEMAND",),
        replaced_at=None, replaced_by=None, tariff="两部制")


def _reading(lineno: int, meter_id: str, read_at: str, value: str,
             multiplier: str = "1") -> dict:
    return {"lineno": lineno, "meter_id": meter_id, "read_at": read_at,
            "reading": Decimal(value), "multiplier": Decimal(multiplier)}


# ---------- 周期与覆盖 ----------

def test_month_bounds_are_shanghai_half_open_month():
    start, end = cycle_mod.month_bounds("2026-02")
    # 上海 2/1 00:00 = UTC 1/31 16:00；上海 3/1 00:00 = UTC 2/28 16:00
    assert start.isoformat() == "2026-01-31T16:00:00+00:00"
    assert end.isoformat() == "2026-02-28T16:00:00+00:00"
    assert (end - start).days == 28


def test_demand_readings_bucket_by_shanghai_month():
    # UTC 1/31 17:00 已是上海 2/1 01:00，必须落进 2 月桶
    assert cycle_mod.month_of("2026-01-31T17:00:00+00:00") == "2026-02"
    assert cycle_mod.month_of("2026-01-31T15:59:00+00:00") == "2026-01"


def test_peak_is_max_never_summed():
    """需量抄见是周期内峰值：发布值取 max，绝不逐行累加。"""
    readings = [
        _reading(1, "M0009", "2026-01-10T01:00:00+00:00", "410"),
        _reading(2, "M0009", "2026-01-20T01:00:00+00:00", "455"),
        _reading(3, "M0009", "2026-01-28T01:00:00+00:00", "360"),
    ]
    cycles = cycle_mod.build_cycles(readings, ["M0009"])
    assert len(cycles) == 1
    assert cycles[0].observed_peak_kw == Decimal("455.000")
    assert cycles[0].peak_linenos == [2]
    # 朴素累加会得到 1225，必须不被采用
    assert sum((r["reading"] for r in readings), Decimal("0")) == Decimal("1225")


def test_multiplier_applied_to_peak_kw():
    readings = [_reading(1, "M0009", "2026-01-20T01:00:00+00:00", "12.5", "40")]
    cycles = cycle_mod.build_cycles(readings, ["M0009"])
    assert cycles[0].observed_peak_kw == Decimal("500.000")


def test_full_vs_partial_coverage_uses_grace_window():
    # 末条读数在 2/28（UTC 2/27 16:00），距 3/1 清零 2 天，落在 3 天宽限内 → full
    full = cycle_mod.build_cycles([
        _reading(1, "M0009", "2026-02-12T01:00:00+00:00", "260"),
        _reading(2, "M0009", "2026-02-27T16:00:00+00:00", "300"),
    ], ["M0009"])
    assert full[0].coverage_status == "full"
    # 末条停在 2/12，距清零超过宽限 → partial
    partial = cycle_mod.build_cycles([
        _reading(1, "M0009", "2026-02-03T01:00:00+00:00", "180"),
        _reading(2, "M0009", "2026-02-12T01:00:00+00:00", "260"),
    ], ["M0009"])
    assert partial[0].coverage_status == "partial"
    assert partial[0].coverage_ratio < Decimal("1")


def test_reset_does_not_quarantine_or_build_intervals():
    """清零后读数变小是正常现象：不判倒走、不进区间还原。"""
    from app.modules.m3_quality import rules as quality_rules
    contract = _contract()
    records = [
        {"lineno": 1, "meter_id": "M0009", "register": "DEMAND",
         "read_at": "2026-01-20T01:00:00+00:00", "reading": Decimal("455"),
         "read_type": "N"},
        {"lineno": 2, "meter_id": "M0009", "register": "DEMAND",
         "read_at": "2026-02-03T01:00:00+00:00", "reading": Decimal("180"),
         "read_type": "N"},
    ]
    kept, dropped = quality_rules.monotonic_quarantine(records, {"M0009": contract})
    assert dropped == []
    assert len(kept) == 2
    from app.modules.m4_intervals import restore
    intervals = restore.build_intervals(records, [], {"M0009": contract})
    assert intervals == []


# ---------- 兜底与发布 ----------

def test_capacity_floor_is_40_percent():
    floor = publish_mod.capacity_floor(_contract(capacity="800"))
    assert floor == Decimal("320.000")


def test_published_demand_takes_max_of_peak_and_floor():
    contracts = {"M0009": _contract()}
    prices = {"DEMAND": PRICE}
    # 峰值高于兜底 → 采用峰值
    peak_cycle = cycle_mod.DemandCycle(
        "M0009", "2026-01", "p", "q", reading_count=1,
        observed_peak_kw=Decimal("455.000"), coverage_status="full",
        period_days=Decimal("31"))
    row = publish_mod.publish_cycles([peak_cycle], contracts, prices)[0]
    assert row["published_demand_kw"] == Decimal("455.000")
    assert row["basis"] == "peak"
    assert row["publish_status"] == "final"
    # 峰值低于兜底 → 采用兜底
    low_cycle = cycle_mod.DemandCycle(
        "M0009", "2026-01", "p", "q", reading_count=1,
        observed_peak_kw=Decimal("260.000"), coverage_status="full",
        period_days=Decimal("31"))
    row = publish_mod.publish_cycles([low_cycle], contracts, prices)[0]
    assert row["published_demand_kw"] == Decimal("320.000")
    assert row["basis"] == "floor"
    # 无任何证据 → 兜底 + floor_no_evidence + 待追补
    missing = cycle_mod.DemandCycle(
        "M0009", "2026-03", "p", "q", coverage_status="missing",
        period_days=Decimal("31"))
    row = publish_mod.publish_cycles([missing], contracts, prices)[0]
    assert row["published_demand_kw"] == Decimal("320.000")
    assert row["basis"] == "floor_no_evidence"
    assert row["observed_peak_kw"] is None
    assert row["needs_follow_up"] is True
    assert row["follow_up_kw"] == Decimal("320.000")


def test_partial_cycle_is_provisional_even_when_peak_wins():
    contracts = {"M0009": _contract()}
    cycle = cycle_mod.DemandCycle(
        "M0009", "2026-02", "p", "q", reading_count=2,
        observed_peak_kw=Decimal("455.000"), coverage_status="partial",
        period_days=Decimal("28"), covered_days=Decimal("11.4"),
        coverage_ratio=Decimal("0.4062"))
    row = publish_mod.publish_cycles([cycle], contracts, {"DEMAND": PRICE})[0]
    assert row["publish_status"] == "provisional"
    assert row["needs_follow_up"] is True
    # 峰值已等于发布值，未观察窗口的「额外尖峰空间」登记为 0，但仍标记需补抄
    assert row["follow_up_kw"] == Decimal("0.000")


def test_demand_charge_is_published_kw_times_price():
    contracts = {"M0009": _contract()}
    cycle = cycle_mod.DemandCycle(
        "M0009", "2026-01", "p", "q", reading_count=1,
        observed_peak_kw=Decimal("455.000"), coverage_status="full",
        period_days=Decimal("31"))
    row = publish_mod.publish_cycles([cycle], contracts, {"DEMAND": PRICE})[0]
    # 455 × 39 = 17745.00
    assert row["demand_charge_yuan"] == Decimal("17745.00")
    # 兜底口径：320 × 39 = 12480.00
    missing = cycle_mod.DemandCycle(
        "M0009", "2026-03", "p", "q", coverage_status="missing",
        period_days=Decimal("31"))
    row = publish_mod.publish_cycles([missing], contracts, {"DEMAND": PRICE})[0]
    assert row["demand_charge_yuan"] == Decimal("12480.00")


def test_missing_month_inside_evidence_span_is_filled_with_floor():
    readings = [
        _reading(1, "M0009", "2026-02-12T01:00:00+00:00", "260"),
        _reading(2, "M0009", "2026-04-18T01:00:00+00:00", "430"),
    ]
    result = demand_service.analyze(
        contracts={"M0009": _contract()}, demand_readings=readings,
        prices={"DEMAND": PRICE})
    by_month = {row["month"]: row for row in result["rows"]}
    assert set(by_month) == {"2026-02", "2026-03", "2026-04"}
    hole = by_month["2026-03"]
    assert hole["coverage_status"] == "missing"
    assert hole["published_demand_kw"] == Decimal("320.000")
    assert hole["reading_count"] == 0
    # 不向证据跨度之外虚构月份
    assert "2026-01" not in by_month and "2026-05" not in by_month


# ---------- 端到端（HTTP + 落库 + 导出 + 幂等） ----------

@pytest.fixture()
def demand_client(tmp_path):
    settings = config.Settings(db_path=str(tmp_path / "demand.sqlite"),
                               sample_dir=str(config.PROJECT_ROOT / "sample_data"))
    return TestClient(create_app(settings))


def test_demand_pipeline_over_http(demand_client):
    run = demand_client.post("/api/runs",
                             json={"sample": "readings_demand.csv"}).json()
    assert run["status"] == "done"
    stage_names = [item["stage"] for item in run["stages"]]
    assert stage_names == list(config.STAGES)
    demand_stage = next(item for item in run["stages"] if item["stage"] == "demand")
    assert demand_stage["quarantine_count"] == 4   # 4 个待追补周期
    payload = demand_client.get("/api/demand").json()
    rows = {(item["meter_id"], item["month"]): item for item in payload["rows"]}
    # 完整周期最终发布
    assert rows[("M0009", "2026-04")]["publish_status"] == "final"
    # 残缺周期临时发布，峰值低于兜底时按兜底
    feb = rows[("M0009", "2026-02")]
    assert feb["publish_status"] == "provisional"
    assert feb["published_demand_kw"] == "320.000"
    assert feb["follow_up_kw"] == "60.000"
    # 缺证周期整月兜底，追补空间等于整个发布值
    mar = rows[("M0009", "2026-03")]
    assert mar["coverage_status"] == "missing"
    assert mar["observed_peak_kw"] is None
    assert mar["follow_up_kw"] == "320.000"
    assert payload["stats"]["missing_count"] == 1
    # 页面与导出都 200，且导出含覆盖/峰值/追补列
    assert demand_client.get("/demand").status_code == 200
    csv_text = demand_client.get("/api/export/demand.csv").text
    header = csv_text.splitlines()[0].lstrip("﻿")
    for column in ("观察覆盖", "已观测峰值kW", "发布月需量kW", "仍需追补kW",
                   "兜底需量kW", "峰值来源行号"):
        assert column in header
    assert "缺证" in csv_text and "临时发布·待追补" in csv_text


def test_rerun_same_package_adds_no_demand_rows_or_charge(demand_client):
    first = demand_client.post("/api/runs",
                               json={"sample": "readings_demand.csv"}).json()
    second = demand_client.post("/api/runs",
                                json={"sample": "readings_demand.csv"}).json()
    assert second["batch_id"] == first["batch_id"]
    assert second["idempotent_replay"] is True
    assert second["counts"] == first["counts"]
    assert second["totals"]["demand_charge"] == first["totals"]["demand_charge"]
    assert second["totals"]["demand_meter_months"] == first["totals"][
        "demand_meter_months"]
    before = first["counts"]["demand_bills"]
    assert second["counts"]["demand_bills"] == before


def test_interrupted_publish_keeps_old_batch_recoverable(demand_client, monkeypatch):
    """发布中断：第二次运行在落库阶段抛错，整体回滚，旧批次原样可恢复。"""
    first = demand_client.post("/api/runs",
                               json={"sample": "readings_demand.csv"}).json()
    old_id = first["batch_id"]
    old_counts = dict(first["counts"])

    import app.store as store_mod
    original = store_mod.Store.save_run

    def boom(self, **kwargs):
        raise RuntimeError("模拟发布中断（磁盘不可写）")

    monkeypatch.setattr(store_mod.Store, "save_run", boom)
    with pytest.raises(RuntimeError, match="模拟发布中断"):
        demand_client.post("/api/runs",
                           json={"sample": "readings_demand.csv", "mode": "force"})
    monkeypatch.setattr(store_mod.Store, "save_run", original)

    # 旧批次仍是最近完成批次，行数与需量电费不变，页面照常可读
    health = demand_client.get("/api/health").json()
    assert health["latest_batch"] == old_id
    again = demand_client.post("/api/runs",
                               json={"sample": "readings_demand.csv"}).json()
    assert again["batch_id"] == old_id
    assert again["counts"] == old_counts
    payload = demand_client.get("/api/demand").json()
    assert len(payload["rows"]) == old_counts["demand_bills"]


def test_recalc_endpoint_recomputes_from_stored_readings(demand_client):
    demand_client.post("/api/runs", json={"sample": "readings_demand.csv"})
    recalc = demand_client.post("/api/demand/recalc", json={}).json()
    assert recalc["stats"]["demand_meter_months"] == 7
    assert recalc["stats"]["follow_up_count"] == 4
