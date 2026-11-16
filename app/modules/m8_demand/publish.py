"""缺证周期的可发布月需量与需量电费。

账期不等人，但残缺周期直接发布会低估尖峰。规则在「合同容量兜底」与
「已观测峰值」之间取可复算的发布值：

1. 合同容量兜底 = 合同容量 × DEMAND_FLOOR_RATIO（通行的 40% 下限），
   这是即使一条证据都没有也必须计的最低月需量，保证发布值不会低于
   两部制合同义务；
2. 已观测峰值 = 该清零周期内所有有效需量读数折 kW 后的最高值
   （需量抄见本身就是周期内峰值，取 max，绝不累加）；
3. 发布月需量 = max(已观测峰值, 合同容量兜底)。
   - 完整周期：峰值通常高于兜底，采用峰值（K_DEMAND_PEAK），状态 final；
   - 残缺周期：先按 max(峰值, 兜底) 临时发布以赶上账期（provisional），
     清零前未观察到的窗口仍可能藏着更高尖峰，页面与导出标注追补；
   - 缺证周期：无任何读数，发布值就是兜底（K_DEMAND_MISSING），
     状态 provisional，待证据补齐后重算。
4. 需量电费 = 发布月需量 × 需量电价（元/kW·月），按 0.01 元四舍五入。

追补不改写历史行：补齐证据后重跑同一抄表包会生成新批次，
旧批次（含旧的临时发布值）保留可恢复；同输入重跑命中幂等批次，
不新增任何需量行或电费。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from ... import config
from ..m2_contract import contract as contract_mod
from . import cycle as cycle_mod


def capacity_floor(contract: contract_mod.MeterContract) -> Decimal:
    """合同容量兜底需量（kW）= 容量 × 兜底比例，按需量刻度取整。"""
    return (contract.capacity * config.DEMAND_FLOOR_RATIO).quantize(
        config.DEMAND_QUANT)


def demand_price(prices: dict[str, Decimal]) -> Decimal:
    return prices.get("DEMAND", config.DEFAULT_PRICES["DEMAND"])


def publish_cycles(cycles: list[cycle_mod.DemandCycle],
                   contracts: dict[str, contract_mod.MeterContract],
                   prices: dict[str, Decimal]) -> list[dict]:
    """把证据周期变成可发布的月需量行（含电费、覆盖、追补标记）。"""
    price = demand_price(prices)
    rows: list[dict] = []
    for cycle in cycles:
        contract = contracts[cycle.meter_id]
        floor_kw = capacity_floor(contract)
        peak = cycle.observed_peak_kw
        if peak is None:
            published = floor_kw
            basis = "floor_no_evidence"
            basis_flag = "K_DEMAND_MISSING"
        elif peak >= floor_kw:
            published = peak
            basis = "peak"
            basis_flag = "K_DEMAND_PEAK"
        else:
            published = floor_kw
            basis = "floor"
            basis_flag = "K_DEMAND_FLOOR"
        published = published.quantize(config.DEMAND_QUANT)
        amount = (published * price).quantize(config.MONEY_QUANT,
                                              rounding=ROUND_HALF_UP)
        # 追补口径分两层，避免把「兜底已计部分」误当成还会再涨的尖峰：
        # - follow_up_kw：未观察窗口里仍可能出现、高于已观测峰值的尖峰空间，
        #   即 发布值 − 已观测峰值；峰值口径下发布值就是峰值，空间为 0，
        #   但周期仍需补抄确认，needs_follow_up 保持为真；
        # - 兜底口径下峰值缺失或低于兜底，发布值本身就是先垫的合同义务，
        #   真实峰值补齐后可能高于它，差额空间登记为待追补。
        if cycle.coverage_status == "full":
            follow_up_kw = Decimal("0.000")
        elif peak is None:
            follow_up_kw = published
        else:
            follow_up_kw = (published - peak).quantize(config.DEMAND_QUANT)
        rows.append({
            "meter_id": cycle.meter_id,
            "month": cycle.month,
            "period_start": cycle.period_start,
            "period_end": cycle.period_end,
            "coverage_status": cycle.coverage_status,
            "publish_status": "final" if cycle.coverage_status == "full"
            else "provisional",
            "reading_count": cycle.reading_count,
            "covered_days": cycle.covered_days,
            "period_days": cycle.period_days,
            "coverage_ratio": cycle.coverage_ratio,
            "capacity_kw": contract.capacity.quantize(config.DEMAND_QUANT),
            "floor_kw": floor_kw,
            "observed_peak_kw": peak,
            "observed_at": cycle.observed_at,
            "peak_linenos": cycle.peak_linenos,
            "first_read_at": cycle.first_read_at,
            "last_read_at": cycle.last_read_at,
            "published_demand_kw": published,
            "basis": basis,
            "basis_flag": basis_flag,
            "coverage_flag": cycle.mark(),
            "demand_price": price,
            "demand_charge_yuan": amount,
            "follow_up_kw": follow_up_kw,
            "needs_follow_up": cycle.coverage_status != "full",
        })
    return rows


def summarize(rows: list[dict]) -> dict:
    """页面 KPI 与批次合计：临时/最终分开，追补条数单独计数。"""
    total_charge = sum((row["demand_charge_yuan"] for row in rows), Decimal("0"))
    provisional = [row for row in rows if row["needs_follow_up"]]
    return {
        "demand_meter_months": len(rows),
        "final_count": sum(1 for row in rows if row["publish_status"] == "final"),
        "provisional_count": len(provisional),
        "partial_count": sum(1 for row in rows
                             if row["coverage_status"] == "partial"),
        "missing_count": sum(1 for row in rows
                             if row["coverage_status"] == "missing"),
        "follow_up_count": len(provisional),
        "demand_charge_total": total_charge.quantize(config.MONEY_QUANT),
        "published_demand_total": sum(
            (row["published_demand_kw"] for row in rows), Decimal("0")
        ).quantize(config.DEMAND_QUANT),
    }
