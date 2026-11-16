"""证据行：每个导出数字都能追回原始行号、原值、原因码与判定方法。"""
from __future__ import annotations

from decimal import Decimal

from ... import config, reasons
from ..m2_contract import contract as contract_mod

MONTHLY_HEADER = ["月份", "表号", "寄存器", "用量kWh", "金额元", "尾差分",
                  "调整原因"]
INTERVAL_HEADER = ["区间号", "表号", "寄存器", "起", "止", "天数", "上次读数",
                   "本次读数", "差值", "倍率", "单位", "用量kWh", "判定", "标记"]
READING_HEADER = ["原始行号", "分段", "表号", "寄存器", "抄表时间UTC", "原始读数",
                  "单位", "折合读数", "类型", "状态", "原因码"]
QUARANTINE_HEADER = ["原始行号", "分段", "原因码", "原因", "等级", "字段", "原始值",
                     "表号", "说明"]
# 月需量导出：把观察覆盖、采用的最高值、兜底与仍需追补部分逐列摊开，
# 与需量页同源，逐位一致；需量是 kW，电费是 元/kW·月，不与电度 kWh 混算。
DEMAND_HEADER = ["月份", "表号", "周期起UTC", "周期止UTC", "观察覆盖", "发布状态",
                 "需量证据条数", "已观察覆盖天数", "周期天数", "覆盖率",
                 "合同容量kW", "兜底需量kW", "已观测峰值kW", "峰值取证时间UTC",
                 "峰值来源行号", "发布月需量kW", "采用口径", "需量电价元/kW",
                 "需量电费元", "仍需追补kW", "追补标记", "原因码"]


def monthly_rows(allocations: list[dict], bills: list[dict],
                 contracts: dict[str, contract_mod.MeterContract]) -> list[list[str]]:
    """每行一个 (月份, 表号, 寄存器)：金额只落在该寄存器自己的分项上，可按列求和。"""
    bill_index = {(bill["month"], bill["meter_id"]): bill for bill in bills}
    amount_index: dict[tuple, str] = {}
    first_register: dict[tuple, str] = {}
    for bill in bills:
        key = (bill["month"], bill["meter_id"])
        if bill["components"]:
            first_register[key] = bill["components"][0]["register"]
        for item in bill["components"]:
            amount_index[(bill["month"], bill["meter_id"], item["register"])] = item["amount"]
    rows = []
    for item in allocations:
        key = (item["month"], item["meter_id"])
        bill = bill_index.get(key)
        amount = amount_index.get((item["month"], item["meter_id"], item["register"]), "")
        adjust = ""
        if bill is not None and bill["adjust_cents"] and \
                first_register.get(key) == item["register"]:
            adjust = str(bill["adjust_cents"])
        rows.append([
            item["month"], item["meter_id"],
            config.REGISTER_LABELS.get(item["register"], item["register"]),
            f"{item['usage_kwh']:.3f}", amount, adjust,
            "K_TOU_ADJUSTED" if item["tou_adjusted"] else "",
        ])
    return rows


def interval_rows(intervals: list[dict]) -> list[list[str]]:
    rows = []
    for item in intervals:
        rows.append([
            item["interval_id"], item["meter_id"],
            config.REGISTER_LABELS.get(item["register"], item["register"]),
            item["start_at"], item["end_at"], f"{item['days']:.9f}",
            f"{item['prev_reading']:.3f}", f"{item['cur_reading']:.3f}",
            f"{item['delta']:.3f}", f"{item['multiplier']:.3f}", item["unit"],
            f"{item['usage_kwh']:.3f}",
            _method_label(item["method"]),
            " ".join(item["flags"]),
        ])
    return rows


def reading_rows(readings: list[dict]) -> list[list[str]]:
    rows = []
    for item in readings:
        rows.append([
            str(item["lineno"]), str(item["segment"]), item["meter_id"],
            config.REGISTER_LABELS.get(item["register"], item["register"]),
            item["read_at"], item["reading_text"], item["unit_raw"] or "",
            "" if item["reading_norm"] is None else f"{Decimal(str(item['reading_norm'])):.3f}",
            item["read_type"], item["status"], item.get("code") or "",
        ])
    return rows


def quarantine_rows(quarantine: list[dict]) -> list[list[str]]:
    rows = []
    for item in quarantine:
        rows.append([
            str(item["lineno"]), str(item.get("segment") or 1), item["code"],
            reasons.label(item["code"]), item["level"], item.get("field") or "",
            item.get("raw_value") or "", item.get("meter_id") or "",
            item.get("detail") or "",
        ])
    return rows


def _method_label(method: str) -> str:
    return {"normal": "常规", "rollover": "满量翻转",
            "replacement_first": "新表首读", "replacement_last": "换表末读"}.get(
                method, method)


COVERAGE_TEXT = {"full": "完整", "partial": "残缺", "missing": "缺证"}
PUBLISH_TEXT = {"final": "最终发布", "provisional": "临时发布·待追补"}
BASIS_TEXT = {"peak": "已观测峰值", "floor": "合同容量兜底",
              "floor_no_evidence": "无证据·容量兜底"}


def demand_rows(rows: list[dict]) -> list[list[str]]:
    """月需量证据行：覆盖、峰值、兜底、发布值、追补各占一列，可独立复核。"""
    out = []
    for item in rows:
        out.append([
            item["month"], item["meter_id"], item["period_start"], item["period_end"],
            COVERAGE_TEXT.get(item["coverage_status"], item["coverage_status"]),
            PUBLISH_TEXT.get(item["publish_status"], item["publish_status"]),
            str(item["reading_count"]),
            f"{item['covered_days']:.3f}", f"{item['period_days']:.3f}",
            f"{Decimal(str(item['coverage_ratio'])):.4f}",
            f"{item['capacity_kw']:.3f}", f"{item['floor_kw']:.3f}",
            "" if item.get("observed_peak_kw") is None
            else f"{Decimal(str(item['observed_peak_kw'])):.3f}",
            item.get("observed_at") or "",
            ";".join(str(n) for n in item.get("peak_linenos", [])),
            f"{item['published_demand_kw']:.3f}",
            BASIS_TEXT.get(item["basis"], item["basis"]),
            f"{item['demand_price']:.2f}",
            f"{item['demand_charge_yuan']:.2f}",
            f"{item['follow_up_kw']:.3f}",
            "待追补" if item["needs_follow_up"] else "已闭合",
            f"{item['coverage_flag']} {item['basis_flag']}".strip(),
        ])
    return out
