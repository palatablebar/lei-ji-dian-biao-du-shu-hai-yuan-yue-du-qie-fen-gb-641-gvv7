"""M8 编排：从已落库读数里挑需量序列，出月需量与需量电费。

与 M5 一样只读当前批次的落库数据，页面、接口、导出、批次运行共用同一份
计算结果，保证「页面看到的」和「导出/入账的」逐位一致。
"""
from __future__ import annotations

from decimal import Decimal

from ... import config
from ..m2_contract import contract as contract_mod
from . import cycle as cycle_mod
from . import publish as publish_mod


def _demand_meters(contracts: dict[str, contract_mod.MeterContract]) -> list[str]:
    return sorted(meter_id for meter_id, contract in contracts.items()
                  if "DEMAND" in contract.registers)


def _demand_readings(store, batch_id: str,
                     multipliers: dict[str, Decimal]) -> list[dict]:
    rows = []
    for row in store.load_readings(batch_id):
        if row["register"] != "DEMAND":
            continue
        rows.append({
            "lineno": row["lineno"],
            "meter_id": row["meter_id"],
            "read_at": row["read_at"],
            "reading": Decimal(str(row["reading_norm"])),
            "multiplier": multipliers.get(row["meter_id"], Decimal("1")),
        })
    return rows


def _fill_missing_cycles(cycles: list[cycle_mod.DemandCycle],
                         meter_ids: list[str]) -> list[cycle_mod.DemandCycle]:
    """在每只需量表证据首末月之间补「缺证周期」（整月无一条有效读数）。

    只补证据跨度内的空洞，不向首尾之外虚构月份；缺证周期发布值走
    合同容量兜底，状态 provisional，等抄表包补齐后重算。
    """
    existing: dict[tuple, cycle_mod.DemandCycle] = {
        (item.meter_id, item.month): item for item in cycles}
    extra: list[cycle_mod.DemandCycle] = []
    for meter_id in meter_ids:
        months = sorted(month for (mid, month) in existing if mid == meter_id)
        if len(months) < 2:
            continue
        year, month_no = (int(part) for part in months[0].split("-"))
        end_year, end_month = (int(part) for part in months[-1].split("-"))
        cursor = (year, month_no)
        while cursor <= (end_year, end_month):
            month = f"{cursor[0]:04d}-{cursor[1]:02d}"
            if (meter_id, month) not in existing:
                start, end = cycle_mod.month_bounds(month)
                hole = cycle_mod.DemandCycle(
                    meter_id=meter_id, month=month,
                    period_start=start.isoformat(), period_end=end.isoformat(),
                    period_days=cycle_mod.coverage_days(month),
                    coverage_status="missing")
                extra.append(hole)
            if cursor[1] == 12:
                cursor = (cursor[0] + 1, 1)
            else:
                cursor = (cursor[0], cursor[1] + 1)
    return sorted(cycles + extra, key=lambda item: (item.meter_id, item.month))


def analyze(*, contracts: dict[str, contract_mod.MeterContract],
            demand_readings: list[dict],
            prices: dict[str, Decimal] | None = None) -> dict:
    """纯函数入口：契约 + 需量读数 → 周期、发布行、合计（测试直接用）。"""
    meter_ids = _demand_meters(contracts)
    cycles = cycle_mod.build_cycles(demand_readings, meter_ids)
    cycles = _fill_missing_cycles(cycles, meter_ids)
    prices = prices or {"DEMAND": config.DEFAULT_PRICES["DEMAND"]}
    rows = publish_mod.publish_cycles(cycles, contracts, prices)
    return {"cycles": cycles, "rows": rows,
            "stats": publish_mod.summarize(rows), "prices": prices}


def run(settings, store, batch_id: str) -> dict:
    contracts = contract_mod.load(settings)
    prices = contract_mod.load_prices(settings)
    multipliers = {meter_id: contract.multiplier
                   for meter_id, contract in contracts.items()}
    readings = _demand_readings(store, batch_id, multipliers)
    result = analyze(contracts=contracts, demand_readings=readings, prices=prices)
    result["batch_id"] = batch_id
    result["contracts"] = contracts
    return result


def payload(result: dict) -> dict:
    return {
        "batch_id": result["batch_id"],
        "stats": {key: (str(value) if hasattr(value, "as_tuple") else value)
                  for key, value in result["stats"].items()},
        "rows": [row_payload(row) for row in result["rows"]],
    }


def row_payload(row: dict) -> dict:
    out = dict(row)
    for key in ("covered_days", "period_days", "coverage_ratio", "capacity_kw",
                "floor_kw", "published_demand_kw", "demand_price",
                "demand_charge_yuan", "follow_up_kw"):
        out[key] = str(row[key])
    if row.get("observed_peak_kw") is not None:
        out["observed_peak_kw"] = str(row["observed_peak_kw"])
    out["peak_linenos"] = list(row.get("peak_linenos") or [])
    return out
