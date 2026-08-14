# 每日报告 · Telegram 推送

> 每个交易日收工后,把三个 paper 实例的当天结果推到 Telegram。
> 最后更新 2026-08-14。

## 是什么

`daily_report.py` 读三个实例各自的事件文件 + 各自的 Alpaca 账户,汇总成一条
消息(现役 volx2 / volx2-1.5x / volx2-nochase 并排),推送到 Telegram。

样例:
```
📊 Cameron 日报 · 2026-08-14
─ volx2 (2x, 现役)   ***ORVR | 今日 $X | 累计 $Y | 下单/否决/丢弃
─ volx2-1.5x         ***WUH4 | ...
─ volx2-nochase      ***SRCC | ...
```

每条含:账户尾号、equity、今日 P&L、累计 P&L、当天 watchlist、实盘进场明细、
下单/量能否决/资格丢弃计数。

## 何时发

- **自动**:`cameron-report.timer` 每交易日 **15:20 UTC(11:20 ET)** 触发 ——
  即我们 11:00 ET 收工清仓后 20 分钟(留缓冲让三实例都写完日志)。
- **为什么等收工**:9:30–11:00 是交易时段,期间可能还持仓;11:00 清仓后当天
  盈亏才尘埃落定,提前发是半截数据。
- **手动**:`cd /opt/cameron && .venv/bin/python daily_report.py`(盘前跑=当天还没
  数据,基本是空的)。

## 配置(都在 `.env`)

```
TELEGRAM_BOT_TOKEN=<@BotFather /newbot 拿到的 token>
TELEGRAM_CHAT_ID=<你的 chat id>
```
- 没配这两个时,`daily_report.py` 只打印不发送(可安全随时跑)。
- **chat_id 怎么拿**:给 bot 发条消息后,`GET https://api.telegram.org/bot<TOKEN>/getUpdates`
  里的 `result[].message.chat.id`。
- ⚠️ **token 轮换**:token 是凭据,泄露就在 @BotFather 里 `/revoke` 换新的,再更新
  `.env` 里 `TELEGRAM_BOT_TOKEN=` 那行。

## 部署

- `deploy/cameron-report.service`(oneshot,跑 `daily_report.py`)
- `deploy/cameron-report.timer`(`OnCalendar=Mon-Fri *-*-* 15:20:00`,UTC)
- 安装:`cp deploy/cameron-report.* /etc/systemd/system/ && systemctl daemon-reload
  && systemctl enable --now cameron-report.timer`
- 看下次运行:`systemctl list-timers cameron-report.timer`

## 想换节奏

| 想要 | 怎么改 |
|---|---|
| 一收工(11:00 ET)立刻发 | timer 改 `15:05`,或改成 session.end 事件触发 |
| 每笔进出场实时推 | 在 `executor.buy` / 平仓处加一条 Telegram 发送(另做,消息更多) |
| 保持现状(一天一条汇总) | 不动 |
