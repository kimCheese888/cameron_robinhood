# 每日 Ross Cameron 对比 · 工作流

> 目的:每天把 bot 的选股 watchlist 和 Ross Cameron 当天 YouTube 视频里
> 点名的票做对比,看重叠、看差异,持续校准我们的选股。
> 最后更新 2026-08-10。

## 为什么要人工抓字幕这一环

服务器(Vultr 机房 IP)被 YouTube 封禁字幕接口(`RequestBlocked` —— 云厂商
IP 一律挡)。实测:RSS 可抓、YouTube 网页可达,但 `youtube-transcript-api`
和转录站 `youtubetotranscript.com` 从服务器都取不到字幕。

**方案:字幕用浏览器(住宅 IP,不被封)抓,其余全自动。**

## 每日流程

### 1. 找到当天的 Ross 视频(自动,RSS)
```bash
curl -s "https://www.youtube.com/feeds/videos.xml?channel_id=UCBayuhgYpKNbhJxfExYkPfA" \
  | grep -oE "<title>[^<]*</title>|<yt:videoId>[^<]*</yt:videoId>|<published>[^<]*</published>"
```
认这类标题:`There's ONE Stock On Watch for ...`(盘前 watchlist)、
`+NNN% Short Squeeze for Day NN!` / `BIGGEST RED DAY ...`(复盘)。

### 2. 抓字幕(浏览器,住宅 IP)
打开转录站,复制全文存成文件:
```
https://youtubetotranscript.com/transcript?v=<VIDEO_ID>
```
(由 Claude 用 Chrome 工具抓取 → 存为 `ross_<DATE>.txt`;可用 /schedule
定时触发这个会话。)

### 3. 跑对比(服务器,自动)
```bash
.venv/bin/python ross_compare.py ross_2026-08-10.txt \
  --day 2026-08-10 --video-id l7Z5aqS4zpk --title "There's ONE Stock ..."
```

## 输出

- **overlap**(可靠):我们 watchlist 里的票,有哪些 Ross 也点名了 —— 逐字
  匹配我们的已知代码,准确。
- **ross_candidates**(尽力而为):从字幕里抽的大写代码,去掉行话
  (US/AI/IRA/ETF…)。auto-caption 会把代码拆开(如 `ZJYL`→`ZJ Y L`),
  所以会漏一些、也可能带噪,**当参考、人工复核**。
- 写入 `ross_compare.csv`(逐日一行)+ `events.jsonl` 的 `ross.compare` 事件。

## 怎么读这份对比

1. **overlap 高** → 我们的 5-Pillars 选股在捞和 Ross 同一类票,universe 对。
   (2026-08-10 实测:NAMI 重叠 —— Ross 周五重仓过、我们周一也选进来了。)
2. **ross_only(他有我们没有)** → 查扫描器为什么漏(float 上限?价格?
   gap 不够?)。如 2026-08-10 的 ZJYL,float 20M 卡在我们上限。
3. **our_only(我们有他没有)** → 未必是错;但如果我们老选一些 Ross 明确
   回避的"暴涨后背面"票,说明缺"衰竭过滤"(见 TODO T9)。

## 已知局限

- 依赖每天有人/有会话用浏览器抓字幕(不是纯 cron)。
- Ross 讲的是**昨天做的/今天想做的**,时间轴和我们当天盘中不完全对齐 ——
  这是"选股 universe + 方向判断"的对比,不是逐笔成交对比。
- 想彻底全自动:给 `youtube-transcript-api` 配住宅代理(月付),见
  ROLLOUT 里的可选升级。
