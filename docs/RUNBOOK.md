# 运行手册 meterback

## 1 启动

```bash
cd projects/gb-641
docker compose up -d --build
curl -sS http://localhost:18200/api/health
```

页面：<http://localhost:18200/>。首次打开没有批次，选样例点「开始处理」即可跑通。

容器内端口 8000，宿主 18200；SQLite 落在命名卷 `meterback-data` 的 `/data/meterback.sqlite`。

## 2 主流程

1. 总览页选 `readings_dirty.csv`（脏样例，13 类问题），点「开始处理」。
2. 阶段卡显示八段「进 / 出 / 隔离」；任一阶段失败可点该卡的「重试」。
3. 读数诊断页看逐行诊断与隔离区，点「隔离区 CSV」下载证据。
4. 区间还原页看翻转、换表末段、新表首读与标记，可点「重算区间」。
5. 月度分摊页看每月合计、账单与尾差，可点「重算分摊」。
6. 月需量页看清零周期的观察覆盖、已观测峰值、合同容量兜底、发布值与待追补周期，可点「重算需量」。
7. 批次对账页看四账与运行清单；导出页下载六份文件（含月需量 CSV）。

命令行同一套接口：

```bash
curl -sS -X POST http://localhost:18200/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"sample":"readings_dirty.csv"}'
curl -sS http://localhost:18200/api/reconcile | python3 -m json.tool
curl -sS -o /tmp/monthly.csv http://localhost:18200/api/export/monthly.csv
```

## 3 常见操作

- **同输入重跑**：再点一次「开始处理」或 POST 同一 `sample`，命中批次键，返回同一 `batch_id`，行数与合计不变。
- **强制重算**：POST `/api/batches/{batch_id}/retry`，用同一个 `sample` 重跑并覆盖该批次的行。
- **换样例**：下拉框里其余样例分别是干净、GBK 中文表头、末行截断、空文件、仅表头、全空行、表头缺列。
- **重跑测试**：`docker compose exec -T app python -m pytest -q`。
- **清库**：`docker compose down -v` 删除命名卷；下次启动自动建表。

## 3.1 月需量发布与追补

样例 `readings_demand.csv` 不含需量；演示需量选 `readings_demand.csv`（契约里 M0008、M0009 带 DEMAND 寄存器）。

1. 月需量页每行是「一只表 × 一个清零周期」：覆盖率看 `已观察覆盖天数 / 周期天数`，
   完整周期发「最终发布」，残缺/缺证周期发「临时·待追补」。
2. 发布月需量恒等于 `max(已观测峰值, 合同容量 × 40%)`；「采用口径」列标明是峰值还是兜底，
   峰值行附取证时间与原始行号，可回读数证据 CSV 复核。
3. 待追补表列出每个临时周期的未观察窗口与追补口径：补抄该窗口后把新读数并入抄表包重跑，
   系统按新峰值重算；旧批次不删除，批次历史里仍可打开旧版（页面 `?batch_id=` 切换）。
4. 命令行核对：

```bash
curl -sS -o /tmp/demand.csv http://localhost:18200/api/export/demand.csv
# 「发布月需量kW × 需量电价」应逐行等于「需量电费元」（0.01 元四舍五入）
# 「仍需追补kW」非 0 的行数应等于阶段卡「需量」段的隔离计数与总览 KPI 待追补数
```

## 4 失败排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 422 `E_EMPTY_FILE` | 样例文件 0 字节 | 选非空样例 |
| 422 `E_HEADER_ONLY` | 只有表头 | 补数据行 |
| 422 `E_ALL_BLANK` | 数据行全空 | 检查导出是否只带格式 |
| 422 `E_HEADER_INVALID` | 缺 `meter_id/register/read_at/reading` | 按列别名表改名 |
| 422 `E_CONTRACT_INVALID` | `meters.csv` 重复表号、零倍率、换表目标缺失 | 修契约 |
| 400 `E_SAMPLE_UNKNOWN` | 样例名不在白名单 | 用页面下拉框里的名字 |
| 404 `E_BATCH_NOT_FOUND` | 还没有批次就导出 | 先跑一次处理 |
| 四账「不平」 | 逻辑或数据被改 | 看差额字段与说明，逐账核对 |

日志：`docker compose logs -f app`（uvicorn 访问日志）。运行清单里的 `input_digest` 可用来确认两次运行读的是同一份输入。

## 5 复算

1. 从导出页取 `monthly.csv` 与 `manifest.json`；
2. `manifest.json` 的 `inputs[].sha256` 与样例文件比对，确认输入未变；
3. 逐月加总 `用量kWh` 应等于 `manifest.json` 的 `counters` 与 `accounts[1]`（区间账）两侧；
4. 账单 `金额元` 之和应等于 `accounts[3]` 两侧，分项之和等于账单额；
5. `demand.csv` 每行 `发布月需量kW × 需量电价元/kW` 四舍五入后等于 `需量电费元`；
   临时发布行数 = 清单 `counters.demand_follow_up`，重跑同抄表包这些数字必须逐位不变。

## 6 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `METERBACK_DB` | `/data/meterback.sqlite` | SQLite 路径 |
| `METERBACK_SAMPLE_DIR` | `/app/sample_data` | 样例目录 |

规则版本写在 `app/config.py` 的 `RULE_VERSION`，进入运行清单；改规则必须同时加测试，否则同输入重跑会出现两个批次。
