# 资金线 · A股分钟级资金流

手机可直接打开：<https://seonkoo.github.io/fundflow-line/>

## 这是什么

每 5 分钟抓一次**分钟级资金净流入**（也就是"资金线"），画成当日累计曲线，手机随时看。

- 指标：主力 / 超大单 / 大单 / 中单 / 小单 的**当日累计净额**（东财口径，单位元，页面换算成亿元）
- 刷新：交易时段 09:30–15:00 每 5 分钟一次（GitHub Actions cron）
- 数据：静态 JSON，页面直读，零后端

## 关键结论（踩坑总结，别再走弯路）

| 事 | 结论 |
|---|---|
| 分钟资金流接口 | `fflow/kline/get?klt=1`，是**当日累计**，末点 ≈ 日线值 |
| 支持哪些周期 | **只有 `klt=1`（分钟）和 `klt=101`（日线）**，其余 klt 一律 `rc:102` 参数非法 |
| 哪台机器有数据 | **只有 `push2delay`**。push2his / push2test / push2 都返回 `klines=[]` |
| 怎么判断非交易日 | 用日线档 `klt=101` 做对照组：日线也空 → 非交易日；日线有、分钟空 → 盘中数据未生成或接口异常 |
| `fltt=2` | 快照接口必须带，否则返回缩放过的整数 |
| JSONP | 响应可能是 `callback({...});`，必须掐到最后一个 `}`，只 `find("{")` 会 Extra data 报错 |

## 文件

```
fetch.py                  抓取器（多节点自动降级）
config.json               观察池：改这个文件就行，不用动 Python
docs/index.html           单文件看板
docs/data.json            抓取产物（Actions 自动提交）
.github/workflows/fetch.yml
_selftest.py              离线自检（不联网，用真实数据做夹具）
```

## 本地跑

本机/公司网络出口通常直连不了东财 push2 系域名（TCP 握手后被 RST），需要代理：

```bash
export EM_PROXY=socks5://127.0.0.1:10811   # 或 http://127.0.0.1:xxxx
python fetch.py
python _selftest.py                         # 不联网自检
```

GitHub Actions 上直连可用，不用设代理。

## 诚实边界

- 免费额度有限：每 5 分钟一次 × 约 6.5 小时 ≈ 每天 78 次运行，跑完 `docs/data.json` 才更新，cron 实际会漂移几分钟。
- GitHub Actions 可能被东财限流或改版接口失效，页面会如实显示空白并给出判别依据，不做任何补数据/估算。
- 不同数据源"主力"阈值不同，**禁止跨源相减**。
- 不构成投资建议。
