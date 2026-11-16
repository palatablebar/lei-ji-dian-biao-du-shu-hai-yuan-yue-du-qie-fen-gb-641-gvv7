# 数据定义 meterback

本文件定义每个数字、时间、键的含义。页面只显示字段名与数值，解释在这里。

## 1 读数文件列契约

| 规范列名 | 可接受别名 | 类型 | 单位 | 可空 | 说明 |
|---|---|---|---|---|---|
| `meter_id` | 表号 / 电能表 / 电表编号 / meter | 字符串 | — | 否 | 前导零保形，`007` ≠ `7` |
| `register` | 寄存器 / 分量 / 费率 | 枚举 | — | 否 | `TOTAL` 总 / `PEAK` 峰 / `FLAT` 平 / `VALLEY` 谷 / `DEMAND` 需，也接受 总/峰/平/谷/需量/最大需量 |
| `read_at` | 抄表时间 / 抄见时间 / 时间 | 时间 | — | 否 | 带偏移按偏移换算；无偏移按 +08:00；纯日期取当日 00:00 |
| `reading` | 读数 / 示数 / 累计读数 | 十进制 | 见表内 `unit` | 否 | 累计示数，不是用量 |
| `unit` | 单位 / 计量单位 | 枚举 | — | 是 | `kWh` / `度` / `千瓦时` / `MWh` / `兆瓦时`；空则取契约单位 |
| `read_type` | 抄表类型 / 类型 / 方式 | 枚举 | — | 是 | `N`/`正常`/`实抄`、`E`/`估抄`/`估算`；空按 `N` |
| `note` | 备注 / 说明 | 字符串 | — | 是 | 原样保存，只作证据 |

`meter_id / register / read_at / reading` 四列缺失即整文件失败（`E_HEADER_INVALID`）。

## 2 表计契约（meters.csv）

| 列 | 含义 | 约束 |
|---|---|---|
| `capacity` | 满量（寄存器最大值） | > 0；翻转判定的基准 |
| `multiplier` | 综合倍率 | > 0；区间用量 = 差值 × 倍率 |
| `unit` | 契约单位 | 只能是 `kWh` / `MWh`；读数先折到它 |
| `registers` | 寄存器组合，`;` 分隔 | 子集 of TOTAL/PEAK/FLAT/VALLEY/DEMAND；DEMAND 需量寄存器不进区间还原 |
| `replaced_at` | 换表时点 | 带偏移；与 `replaced_by` 成对出现 |
| `replaced_by` | 新表号 | 必须存在于契约 |

主寄存器：有 `TOTAL` 取 `TOTAL`，否则取第一个分量。区间账与月度账只统计主寄存器，避免与峰平谷重复计数。

## 3 数值与精度

- 用量：`Decimal`，单位 kWh，最小刻度 `0.001`；量化只发生在区间用量、月度分摊、分量闭合三处。
- 金额：`Decimal`，单位元，最小刻度 `0.01`；量化只发生在账单合计与分项补差两处。
- 电价：电度 元/kWh，`prices.csv`（`TOTAL 0.65`、`PEAK 1.05`、`FLAT 0.65`、`VALLEY 0.32`）；需量 `DEMAND 39.00` 元/kW·月，只用于 M8 需量电费。
- 需量：`Decimal`，单位 kW，最小刻度 `0.001`；需量电费最小刻度 `0.01` 元，规则见第 10 节。
- 对外 JSON 与 CSV：用量三位小数、金额两位小数，全部以字符串输出，禁止 float 往返。

## 4 时间与分桶

- 入库一律 UTC（`+00:00`）；页面展示与月桶按 `Asia/Shanghai (+08:00)`。
- 区间：`[起, 止)`，允许跨月、跨年；上一段止点等于下一段起点。
- 月桶：`[月初 00:00, 下月初 00:00)`，按覆盖天数加权；天数用秒数 / 86400 折算，保留 9 位小数。
- 相邻区间不重不漏由「半开 + 首尾相接」保证，测试 `test_intervals_are_half_open_and_contiguous` 固定。

## 5 空值与三态

- 哨兵清单：`-`、`--`、`—`、`/`、`N/A`、`null`、`未知`、`无`、`暂无`、`待定`、`空`、`未抄`、空串。
- `null`（无值）、`0`（有效零读数）、空串（格式缺陷）语义不同：前两者走不同原因码，空串按哨兵处理。
- 文件级三态：0 字节 → `E_EMPTY_FILE`；仅表头 → `E_HEADER_ONLY`；数据行全空 → `E_ALL_BLANK`。

## 6 去重与冲突键

- 业务键：`(meter_id, register, read_at)`（时间已归一到 UTC）。
- 同键多行且读数完全一致 → 保留行号最小的一条，其余记 `K_DUPLICATE_IDENTICAL`。
- 同键多行且读数不同 → 两条都进隔离区（`Q_DUPLICATE_CONFLICT`），带各自原值，不做「后写覆盖」。
- 不使用全行哈希：备注列变化不代表读数变化。

## 7 判定规则

| 判定 | 条件 | 结果 |
|---|---|---|
| 常规 | 本次 ≥ 上次 | 差值 = 本次 − 上次 |
| 满量翻转 | 本次 < 上次 且 上次 ≥ 满量 × 0.9 且 补满量后差值 ≤ 满量 | 差值 = 满量 − 上次 + 本次 |
| 倒走 | 本次 < 上次 且 不满足翻转前提 | `Q_REVERSE_READING`，整行隔离 |
| 换表末段 | 旧表最后一条读数（≤ 换表时点） | 区间止点 = 换表时点 |
| 新表首读 | 新表第一条读数 | 区间 = [换表时点, 首读时间)，用量从 0 起算 |
| 估抄 | `read_type = E` | 不作锚点、不参与单调判定，落在区间内时给区间打 `K_ESTIMATE_SKIPPED` |

## 8 尾差处理

1. 月度切分：按天数权重得到精确份额，各自向下取整后，余数按小数部分从大到小逐份补 0.001，保证每段之和等于区间用量。
2. 分量闭合：同月「总表 −（峰+平+谷）」若不等于 0，差额并入当月用量最大的分量（并列取 峰 > 平 > 谷），并标 `K_TOU_ADJUSTED`。
3. 账单闭合：账单额 = 先合计再四舍五入；分项金额各自向下截尾，差额按小数部分从大到小逐分补到分项，补完分项之和等于账单额，差额记 `adjust_cents`。

## 9 原因码

文件级 `E_*`：`E_EMPTY_FILE`、`E_HEADER_ONLY`、`E_ALL_BLANK`、`E_HEADER_INVALID`、`E_DECODE`、`E_SAMPLE_UNKNOWN`、`E_CONTRACT_INVALID`、`E_BATCH_NOT_FOUND`。

行级 `Q_*`：`Q_COLUMN_COUNT`、`Q_TRUNCATED_ROW`、`Q_MISSING_READING`、`Q_BAD_READING`、`Q_BAD_TIME`、`Q_UNKNOWN_METER`、`Q_BAD_REGISTER`、`Q_BAD_UNIT`、`Q_BAD_READ_TYPE`、`Q_NEGATIVE_READING`、`Q_EXCEEDS_CAPACITY`、`Q_REVERSE_READING`、`Q_DUPLICATE_CONFLICT`、`Q_METER_RETIRED`。

标记 `K_*`：`K_ROLLOVER`、`K_REPLACEMENT_FIRST`、`K_ESTIMATE_SKIPPED`、`K_CHARSET_FALLBACK`、`K_BOM_STRIPPED`、`K_SEGMENT_SPLIT`、`K_TRUNCATED_TAIL`、`K_TOU_ADJUSTED`、`K_BILL_ROUNDED`、`K_UNIT_ASSUMED`、`K_UNIT_CONVERTED`、`K_DUPLICATE_IDENTICAL`。

唯一来源：`app/reasons.py`；页面与导出的原因文案都从它取。

## 10 月需量（DEMAND 寄存器，M8）

需量与电度是两条独立的账，语义差异必须先讲清：

- 电度寄存器是**累计示数**，区间用量 =（本次 − 上次）× 倍率，可以跨区间累加；
- 需量寄存器每月初**清零**，抄见值是「自上次清零以来滑动窗口的最高功率（kW）」，
  本身就是周期内的一个峰值，**不能逐对相减、不能跨月累加、不能当电量**；
  清零后读数自然变小，既不判倒走也不判满量翻转（M3/M4 对 DEMAND 直接放行且不建区间）。

### 10.1 清零周期与观察覆盖

- 周期 = 上海时区自然月 `[月初 00:00, 下月初 00:00)`（与 M5 月桶同源），月内读数归一组。
- 已观测峰值 = 组内所有有效需量读数（× 契约倍率折 kW）的**最大值**，并列峰值保留全部来源行号。
- 观察覆盖三态：
  - `full` 完整：末条读数距下月初清零点 ≤ `DEMAND_GRACE_DAYS`（3 天），认为观察延续到清零；
  - `partial` 残缺：周期内有读数但末条过早，清零前一段无证据；
  - `missing` 缺证：证据首末月之间整月没有一条有效读数（只补跨度内空洞，不虚构首尾外月份）。

### 10.2 可发布月需量与需量电费

- 合同容量兜底 `floor_kw = capacity × DEMAND_FLOOR_RATIO`（40%，两部制需量计费通行下限）。
- 发布月需量 `published_demand_kw = max(已观测峰值, 合同容量兜底)`，按需量刻度 0.001 kW 取整：
  - `peak` 采用已观测峰值（`K_DEMAND_PEAK`）；
  - `floor` 峰值低于兜底，按兜底发布（`K_DEMAND_FLOOR`）；
  - `floor_no_evidence` 无任何证据，纯兜底（`K_DEMAND_MISSING`）。
- 发布状态：完整周期 `final`；残缺/缺证周期 `provisional`（临时发布，先赶上账期）。
- 需量电费 = 发布月需量 × 需量电价（`prices.csv` 的 `DEMAND`，元/kW·月，默认 39.00），按 0.01 元四舍五入；
  与电度电费（元/kWh）分列，不互相累加。
- 仍需追补 `follow_up_kw`：未观察窗口可能藏着更高尖峰。缺证周期登记整个发布值；
  兜底口径登记「发布值 − 已观测峰值」；峰值口径下为 0 但周期仍标待追补（需补抄确认无更高尖峰）。
  追补不虚构数字：补齐证据后重跑抄表包按新峰值重算，新旧差额在新批次体现。

### 10.3 落库与导出

- 表 `demand_bills`：一行一只表一个清零周期，覆盖天数/覆盖率、合同容量、兜底、已观测峰值
  及其取证时间与来源行号、发布值、采用口径、电价、电费、追补值与标记全部留痕。
- 导出 `GET /api/export/demand.csv`（BOM/CRLF，与需量页同源逐位一致）；接口 `GET /api/demand`、
  `POST /api/demand/recalc`；页面 `/demand`。
- 幂等与可恢复：批次键含规则版本与全部输入摘要，同抄表包重跑命中同一批次，
  `demand_bills` 行数与需量电费不变；发布在单事务内先删后插，中断整体回滚，
  旧批次保留并可通过批次历史/重跑恢复。

新增需量原因码：`K_DEMAND_FULL`、`K_DEMAND_PARTIAL`、`K_DEMAND_MISSING`、
`K_DEMAND_FLOOR`、`K_DEMAND_PEAK`、`K_DEMAND_RESET`（见 `app/reasons.py`）。
