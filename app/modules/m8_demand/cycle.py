"""月需量周期：需量读数按月清零周期归组，取周期内最高值。

需量寄存器与电度寄存器的语义完全不同：
- 电度是累计示数，区间用量 =（本次 − 上次）× 倍率；
- 需量是「自上次清零以来的滑动窗口最高功率」，每月初清零一次，
  抄见值本身就是周期内的一个峰值，**不能逐对相减、不能跨月累加**。

因此这里只做一件事：把有效需量读数按上海时区的自然月
`[月初 00:00, 下月初 00:00)` 归组，在组内取最高值作为已观测峰值，
再依据观察覆盖情况给出周期判定（完整 / 残缺 / 缺证）。
"""
from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from ... import config


def month_bounds(month: str) -> tuple[dt.datetime, dt.datetime]:
    """月份 'YYYY-MM' → 上海时区的 [月初, 下月初)，再转 UTC 返回。"""
    year, month_no = (int(part) for part in month.split("-"))
    start_local = dt.datetime(year, month_no, 1, 0, 0, 0, tzinfo=config.CHINA_TZ)
    if month_no == 12:
        end_local = dt.datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=config.CHINA_TZ)
    else:
        end_local = dt.datetime(year, month_no + 1, 1, 0, 0, 0,
                                tzinfo=config.CHINA_TZ)
    return start_local.astimezone(config.UTC), end_local.astimezone(config.UTC)


def month_of(read_at_utc: str) -> str:
    """UTC 入库时间 → 上海时区所属月桶 'YYYY-MM'（与 M5 月桶同源）。"""
    instant = dt.datetime.fromisoformat(read_at_utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=config.UTC)
    local = instant.astimezone(config.CHINA_TZ)
    return local.strftime("%Y-%m")


def coverage_days(month: str) -> Decimal:
    """该清零周期的完整天数（观察覆盖比例的分母）。"""
    year, month_no = (int(part) for part in month.split("-"))
    return Decimal(calendar.monthrange(year, month_no)[1])


@dataclass
class DemandCycle:
    """一只表一个清零周期的需量证据与判定。"""
    meter_id: str
    month: str
    period_start: str
    period_end: str
    reading_count: int = 0
    observed_peak_kw: Decimal | None = None
    observed_at: str | None = None
    peak_linenos: list[int] = field(default_factory=list)
    first_read_at: str | None = None
    last_read_at: str | None = None
    coverage_status: str = "missing"      # full / partial / missing
    covered_days: Decimal = Decimal("0")
    period_days: Decimal = Decimal("0")
    coverage_ratio: Decimal = Decimal("0")
    basis: str = ""                       # peak / floor / floor_no_evidence

    def mark(self) -> str:
        return {
            "full": "K_DEMAND_FULL",
            "partial": "K_DEMAND_PARTIAL",
            "missing": "K_DEMAND_MISSING",
        }[self.coverage_status]


def _parse(read_at: str) -> dt.datetime:
    instant = dt.datetime.fromisoformat(read_at)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=config.UTC)
    return instant.astimezone(config.UTC)


def build_cycles(demand_readings: list[dict],
                 meter_ids: list[str]) -> list[DemandCycle]:
    """按 (表号, 月) 聚合需量读数。

    只对「出现过需量证据的月份」生成周期行；读数已在 M1/M3 完成
    单位折算与倍率前的归一，这里再乘综合倍率折成 kW。
    月份范围取每只表各自证据的首末月，不凭空铺整段日历。
    """
    grouped: dict[tuple, list[dict]] = {}
    for row in demand_readings:
        grouped.setdefault((row["meter_id"], month_of(row["read_at"])), []).append(row)

    cycles: list[DemandCycle] = []
    for meter_id in meter_ids:
        months = sorted(month for (mid, month) in grouped if mid == meter_id)
        for month in months:
            rows = sorted(grouped[(meter_id, month)],
                          key=lambda item: (item["read_at"], item["lineno"]))
            start, end = month_bounds(month)
            cycle = DemandCycle(
                meter_id=meter_id, month=month,
                period_start=start.isoformat(), period_end=end.isoformat(),
                period_days=coverage_days(month))
            multiplier = rows[0]["multiplier"]
            peak: Decimal | None = None
            peak_at: str | None = None
            peak_linenos: list[int] = []
            for row in rows:
                value = (row["reading"] * multiplier).quantize(config.DEMAND_QUANT)
                if peak is None or value > peak:
                    peak = value
                    peak_at = row["read_at"]
                    peak_linenos = [row["lineno"]]
                elif value == peak:
                    peak_linenos.append(row["lineno"])
                cycle.reading_count += 1
            cycle.observed_peak_kw = peak
            cycle.observed_at = peak_at
            cycle.peak_linenos = peak_linenos
            cycle.first_read_at = rows[0]["read_at"]
            cycle.last_read_at = rows[-1]["read_at"]
            _classify_coverage(cycle, [_parse(row["read_at"]) for row in rows], end)
            cycles.append(cycle)
    cycles.sort(key=lambda item: (item.meter_id, item.month))
    return cycles


def _classify_coverage(cycle: DemandCycle, instants: list[dt.datetime],
                       period_end_utc: dt.datetime) -> None:
    """观察覆盖判定：最后一条读数是否贴近下月清零点。

    - full：最后读数 ≥ 下月初 − 宽限天数，认为观察一直延续到清零；
    - partial：周期内有读数但末条读数过早，清零前一段无证据；
    - missing：周期内一条有效读数都没有（在 build_cycles 里不会生成，
      该分支留给外部补建的缺证周期）。
    """
    if not instants:
        cycle.coverage_status = "missing"
        cycle.covered_days = Decimal("0")
        cycle.coverage_ratio = Decimal("0")
        return
    last_instant = max(instants)
    grace = dt.timedelta(days=int(config.DEMAND_GRACE_DAYS))
    if last_instant >= period_end_utc - grace:
        cycle.coverage_status = "full"
    else:
        cycle.coverage_status = "partial"
    start = _parse(cycle.period_start)
    observed_span = (last_instant - start).total_seconds() / 86400.0
    covered = Decimal(str(observed_span)).quantize(Decimal("0.001"))
    if covered > cycle.period_days:
        covered = cycle.period_days
    if covered < Decimal("0"):
        covered = Decimal("0")
    cycle.covered_days = covered
    cycle.coverage_ratio = (covered / cycle.period_days).quantize(Decimal("0.0001"))
