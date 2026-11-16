# 样例数据说明

读数样例共用同一条代码路径，没有任何特判分支。默认样例是 `readings_dirty.csv`。

| 文件 | 覆盖的真实问题 |
|---|---|
| `readings_dirty.csv` | UTF-8 BOM、GB18030 无关但与中文列混写、行序打乱、中间重复表头、千分位、全角数字、空值哨兵、负数、倒走、同键取值冲突、超满量、换表后仍抄旧表、单位混写、时区 `Z` 与 `+08:00` 混写 |
| `readings_clean.csv` | 只保留有效行与一条估抄，作为同路径的干净对照 |
| `readings_gbk.csv` | GB18030 字节 + 中文表头（电能表 / 寄存器 / 抄表时间 / 读数 / 单位 / 抄表类型）+ `度` 单位别名 |
| `readings_truncated.csv` | 末行被截断且文件末尾没有换行符 |
| `readings_empty.csv` | 0 字节 |
| `readings_header_only.csv` | 只有表头 |
| `readings_blank.csv` | 表头之后全是空行 |
| `readings_bad_header.csv` | 表头缺 `register` / `read_at` |
| `readings_demand.csv` | DEMAND 需量寄存器：月清零后回落、完整/残缺/缺证（整月空洞）三类覆盖、峰值低于 40% 兜底、kW 单位 |

`meters.csv` 是表计契约：容量、综合倍率、单位、寄存器组合、换表事件（M0004 → M0005）。
M0001、M0008 同时带电度与 DEMAND 寄存器，M0009 是只抄需量的锻造车间（容量 800 kW，兜底 320 kW）。
`prices.csv` 是电价：峰 1.05 / 平 0.65 / 谷 0.32 元每 kWh，DEMAND 39.00 元每 kW·月。

电度样例数值是手工设计的，期望值写死在 `tests/fixtures/expected.py`：脏样例 60 行读数、
45 有效 + 1 估抄 + 14 隔离，区间 32 段，月度合计 4499.900 kWh，账单 3261.96 元。
需量样例 `readings_demand.csv` 的发布值与追补口径断言在 `tests/test_m8_demand.py`。
