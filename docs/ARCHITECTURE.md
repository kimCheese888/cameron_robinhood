# 系统架构 · Cameron ORB Autotrader

> 维护约定:改动系统结构/新增模块/改数据流时,同步更新本文件。
> 最后更新 2026-08-10。

## 一句话

盘前扫描小盘跳空股 → 开盘用 ORB(开盘区间突破)+ 量能确认在 **Alpaca 模拟盘**真实下单,
同时并行跑 5 个"影子"策略(真实行情、虚拟成交)做 A/B 对比;行情主用 **Robinhood 整合数据**,
Alpaca IEX 备用。全流程写 `events.jsonl`,网页面板可视化。

## 模块

| 文件 | 职责 | 关键接口 |
|---|---|---|
| `scanner.py` | 盘前/盘中选股(5 Pillars 过滤) | `scan()` → 候选列表 |
| `rh.py` | Robinhood 整合行情:NBBO 报价、分钟 K 线、流通盘、服务器端扫描、OAuth | `available()` `quotes()` `bars()` `last_price()` `scan_tickers()` `hod_tickers()` |
| `autotrader.py` | 主循环:一天一个 session,调度选股/建箱/触发/管理/影子/清仓 | `run_session(day)` `main()` |
| `executor.py` | 下单与风控(真实操作 Alpaca 模拟盘) | `buy()` `manage()` `flatten()` `circuit_breaker()` |
| `journal.py` | 结构化事件日志 | `event(type, msg, **data)` → `events.jsonl` |
| `dashboard.py` | Flask 面板 `localhost:8787`,读 `events.jsonl` + Alpaca 状态 | `/api/state` `/api/chart` |
| `backtest*.py` `sim.py` | 历史回测与模拟,用于策略验证/晋升决策 | — |
| `ross_compare.py` | 每日把 watchlist 对比 Ross Cameron 视频点名的票(字幕靠浏览器抓,见 `docs/ROSS_COMPARE.md`) | `compare()` → `ross_compare.csv` + `ross.compare` 事件 |
| `daily_report.py` | 收工后把三实例当天结果汇总推 Telegram(见 `docs/REPORTING.md`) | `build()` → Telegram |
| `variant_scoreboard.py` | 影子变体累计战绩表(交易数/胜率/总R) | 读 `variants.csv` |
| `filter_costs.py` / `entry_compare.py` | 一次性分析:过滤器机会成本 / 突破 vs 回踩入场 | 读 events + RH bars |

## 数据流

```
盘前 9:15   scanner.scan() ──┐
                            ├─> watchlist(前4) ──> autotrader.run_session()
  Robinhood 整合 + Alpaca IEX┘                         │
                                                        ├─ 9:30-9:35 建箱(opening_range)
                                                        ├─ 9:36+ 轮询价格(15s)
                                                        │    ├─ 破箱顶 → 待确认 → 量能确认 → executor.buy()  [实盘 volx2]
                                                        │    ├─ 5个影子:虚拟成交 → variants.csv
                                                        │    └─ hod_poll():盘中新高回踩 → executor.buy(tag=hod)
                                                        ├─ 每 4 轮 executor.manage()(TP1后止损移保本 + 熔断)
                                                        └─ 11:00 executor.flatten() 清仓 → session.end
所有决策 ──> journal.event() ──> events.jsonl ──> dashboard
```

## 策略清单

| 名称 | 状态 | 入场 | 卖出 | 说明 |
|---|---|---|---|---|
| `orb5-volx2` | **实盘** | 5min 箱体突破 + 2×开盘均量确认 | 半仓1R/半仓2R + 保本 | 当前唯一真实下单的 ORB 策略 |
| `orb5-plain` | 影子 | 5min 突破(无量能过滤) | 同上 | 7/22 前的旧实盘规则,现作对照 |
| `orb5-full2R` | 影子 | 同 plain | 不分批,全仓奔 2R | 高波动、期望偏低 |
| `orb15` | 影子 | 15min 箱体突破 | 同上 | 入场晚,回测偏亏 |
| `orb5-dip` | 影子 | 突破后**买第一次回踩** | 同上 | 最贴合 Ross;影子里唯一盈利 |
| `hod-dip` | 影子(真实下单,tag `hod-`) | 盘中新高回踩微 pullback | 同上 | 第二雷达;上线至今 0 报警(待查) |

## 风控(写死在 `executor.py`,任何策略都绕不过)

- 每笔固定风险 `RISK_PER_TRADE = $100`(仓位 = 100 ÷ 止损距离)
- 单日最大亏损 `DAILY_MAX_LOSS = $300` → 熔断:清仓 + 当天拒新单
- 最多同时持仓 `MAX_POSITIONS = 3`(2 ORB + 1 hod)
- 单笔最多 `MAX_SHARES = 4000`;止损距离 `[0.05, 0.50]`
- 每天最多开仓 4 次;11:00 ET 一律清仓
- 下单用 **bracket 单**(入场+止盈+止损绑定,进程崩了券商也执行)
- `client_order_id` 前缀区分策略:`orb-*` / `hod-*`(去重、可归因)

## 数据源

- **Robinhood 整合行情**(`agent.robinhood.com`,OAuth):NBBO、分钟 K、流通盘、服务器端 gap/HOD 扫描 —— 主
- **Alpaca IEX**(`data.alpaca.markets`):快照/bars —— 备用(约全量 2%,量能偏噪)
- **Alpaca 模拟盘**(`paper-api.alpaca.markets`):真实下单/持仓/成交/权益

## 部署与运行时

- **systemd 服务**(Linux, `/opt/cameron`):
  - `cameron-autotrader.service` → 现役 volx2(默认账户)
  - `cameron-autotrader-15x.service` → volx2-1.5x(账户 `_15X`,`CAMERON_*` 环境变量)
  - `cameron-autotrader-nochase.service` → volx2-nochase(账户 `_NOCHASE`)
  - `cameron-dashboard.service` → `dashboard.py`(`localhost:8787`,读现役实例)
  - `cameron-report.timer` → 每交易日 15:20 UTC 发日报(见 `docs/REPORTING.md`)
  - 单元文件源:`deploy/*.service` / `deploy/*.timer`
- **多账户**:环境变量 `CAMERON_ACCOUNT`(key 后缀)/ `CAMERON_INSTANCE`(状态文件后缀)
  / `CAMERON_VOL_X` / `CAMERON_NOCHASE` / `CAMERON_LIVE_ONLY`;默认空 = 现役实例不变。
  每实例独立账户、独立熔断、独立 `events{后缀}.jsonl`。
- **发布循环**:本地改 → `git push` → 服务器 `git pull` → `systemctl restart cameron-autotrader`
- **面板访问**:SSH 隧道 `ssh -L 8787:localhost:8787 root@<host>`
- **运行时文件**(均 gitignore,勿入库):
  - `.env`(Alpaca key)、`.rh_tokens.json`(RH OAuth)
  - `events.jsonl`(事件流)、`variants.csv`(影子成交)、`signals.csv`(扫描记录)
  - `.day_baseline.json`(熔断基准快照)、`.autotrader.lock`(单实例锁)
