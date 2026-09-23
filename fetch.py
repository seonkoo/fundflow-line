#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
资金线抓取器 —— 每 5 分钟由 GitHub Actions 调用一次，本机也可直接跑。

数据源：东方财富 push2 系（分钟级资金流）
  ★ 关键结论（2026-09-23 盘中实测定论，别再走弯路）：
    - 分钟级资金流（fflow/kline/get?klt=1）有多台节点可用，本机直连实测：
        ✅ 有分钟档：push2delay + 数字 LB 节点 82/7/15/31/41/44/62.push2
           （这些本机出口放行，返回 200 + 完整 klines）
        ❌ 被本机出口 RST：push2 / push2his / 112.push2
        ❌ 200 但 klines=[]（接口活、无分钟档）：push2test
        ❌ 404（无此接口）：push2ex
    - 所以 HOSTS 池只保留验证过的「有分钟档」节点，多源轮询 + 自动重试，
      单节点偶发抖动/空不影响整体成功率（之前只有 push2delay 可用 → 成功率不稳）。
    - fflow/kline/get 只承认 klt=1(分钟) 与 klt=101(日线)，
      klt=5/15/30/60/102 一律 rc:102 参数非法。
    - 返回的是"当日累计"净额：末点 ≈ 日线值（实测差 11 元，舍入）。

判交易日 vs 判接口死：
  用同接口的日线档 klt=101 做对照组 ——
    日线有 + 分钟空  => 盘中数据未生成（太早）或接口异常
    日线空 + 分钟空  => 大概率非交易日
  两者都会在页面上如实标注，不做任何猜测性的补数据。

产物：docs/data.json
"""

import datetime
import json
import os
import ssl
import sys
import time
import random
import urllib.parse
import urllib.request

# 东财网页 JS 里的 ut（与 akshare 的 b2884a... 返回一致，两个都能用）
UT = "fa5fd1943c7b386f172d6893dbfba10b"

# ★ 经 2026-09-23 实测验证、本机直连有分钟档的节点池（多源轮询，按此顺序）。
#   已剔除：push2/push2his/112.push2(本机RST)、push2test(200但klines空)、push2ex(404)。
#   该池对 GitHub Actions 云端出口同样适用（云端还能直连 push2，但无需单点依赖）。
HOSTS = [
    "push2delay.eastmoney.com",
    "82.push2.eastmoney.com",
    "7.push2.eastmoney.com",
    "15.push2.eastmoney.com",
    "31.push2.eastmoney.com",
    "41.push2.eastmoney.com",
    "44.push2.eastmoney.com",
    "62.push2.eastmoney.com",
]

# f51时间 f52主力净额 f53小单 f54中单 f55大单 f56超大单 (f57有时不返回)
FIELDS2 = "f51,f52,f53,f54,f55,f56"
FIELDS1 = "f1,f2,f3,f7"

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "docs", "data.json")
LOG = []


def log(s):
    line = "[%s] %s" % (datetime.datetime.now().strftime("%H:%M:%S"), s)
    print(line, flush=True)
    LOG.append(line)


# ---------- HTTP ----------
_SSL_LOOSE = ssl.create_default_context()
_SSL_LOOSE.check_hostname = False
_SSL_LOOSE.verify_mode = ssl.CERT_NONE

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


_opener = None


def _get_opener():
    """本机出口会拦东财 push2 系域名。若设了 EM_PROXY（如 socks5://127.0.0.1:10811）
       就走代理。GitHub Actions 里不设，直连即可。"""
    global _opener
    if _opener is not None:
        return _opener
    px = os.environ.get("EM_PROXY", "").strip()
    if px:
        try:
            from urllib.request import ProxyHandler, build_opener
            _opener = build_opener(ProxyHandler({"https": px, "http": px}))
            log("本机模式：走代理 %s" % px)
            return _opener
        except Exception as e:
            log("代理初始化失败 %s，回落直连" % str(e)[:60])
    _opener = urllib.request.build_opener()
    return _opener


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://data.eastmoney.com/",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    op = _get_opener()
    try:
        with op.open(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except ssl.SSLError:
        with op.open(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")


def get_json(url, timeout=20):
    """东财两种响应都要能吃：裸 JSON 和 JSONP(jsonpCallback({...});)。
       ★ 必须掐到最后一个 }，只 find('{') 会把尾部 `);` 带进去导致 Extra data 报错。"""
    txt = http_get(url, timeout)
    i, j = txt.find("{"), txt.rfind("}")
    if i < 0 or j < 0 or j <= i:
        raise ValueError("非 JSON 响应(%d字节): %s" % (len(txt), txt[:80]))
    return json.loads(txt[i:j + 1])


# ---------- secid ----------
def secid_of(code, market=None):
    """沪市 1.xxxxxx / 深市 0.xxxxxx / 板块 90.BKxxxx"""
    c = str(code).strip().upper()
    if c.startswith("BK"):
        return "90." + c
    if market == "sh":
        return "1." + c
    if market == "sz":
        return "0." + c
    head = c[:3]
    if head in ("600", "601", "603", "605", "688", "689", "510", "511", "512",
                "513", "515", "516", "518", "588", "589", "118", "110", "113"):
        return "1." + c
    if head in ("000", "001", "002", "003", "300", "301", "159", "150", "128", "127"):
        return "0." + c
    return "1." + c


# ---------- 取数 ----------
def try_hosts(fn, healthy=None, retries=3, backoff=0.3):
    """在健康池(优先)+全池(兜底) 上逐个试 fn(host)，返回第一个非空结果。

    抗抖动策略（实测本机走代理对东财节点偶发断连 Remote end closed）：
      - 异常(连接断) → 同节点退避重试 retries 次（断连是瞬时的，重连大多成功）
      - 空响应(接口活但该标的无分钟档) → 不重试本节点，直接换下一个
    健康池存在时先随机打乱试健康池（分散单节点压力），再兜底其余节点。
    返回 (命中主机, 结果, 错误链)。"""
    base = list(healthy) if healthy else list(HOSTS)
    random.shuffle(base)
    rest = [h for h in HOSTS if h not in base]
    random.shuffle(rest)
    order = base + rest
    errs = []
    first_ok = None
    for host in order:
        for attempt in range(retries + 1):
            try:
                r = fn(host)
            except Exception as e:
                if attempt < retries:
                    time.sleep(backoff)
                    continue
                errs.append("%s:%s" % (host, str(e)[:70]))
                break
            if r:
                if first_ok is None:
                    first_ok = host
                return host, r, errs
            # 空响应：本节点无该数据，不重试，换下一节点
            break
    return None, None, errs


def probe_health(probe_secid, klt=1, timeout=12, retries=3, backoff=0.3):
    """开局探测：收集所有返回"非空分钟档"的节点作为健康池（本轮多源轮询的基础）。
    对偶发断连做退避重试，避免把健康节点误判为不可用。
    返回 (health_hosts, trace)。即使一台都没有也回传空列表，由 try_hosts 全池兜底。"""
    healthy, trace = [], []
    for host in HOSTS:
        klines = None
        for attempt in range(retries + 1):
            try:
                klines, _ = fflow(host, probe_secid, klt, timeout)
                break
            except Exception:
                if attempt < retries:
                    time.sleep(backoff)
                    continue
                trace.append("%s:err" % host)
                klines = None
                break
        if klines:
            healthy.append(host)
            trace.append("%s:ok%d" % (host, len(klines)))
        elif klines is not None:
            trace.append("%s:空" % host)
    if healthy:
        log("健康节点 %d/%d：%s" % (len(healthy), len(HOSTS), ",".join(healthy)))
    else:
        log("警告：无节点返回分钟数据，本轮全池兜底（大概率非交易日/未开盘）")
    return healthy, trace


def fflow(host, secid, klt, timeout=20):
    url = ("https://%s/api/qt/stock/fflow/kline/get?lmt=0&klt=%d"
           "&fields1=%s&fields2=%s&secid=%s&ut=%s"
           % (host, klt, FIELDS1, FIELDS2, secid, UT))
    j = get_json(url, timeout)
    d = j.get("data") or {}
    return (d.get("klines") or []), d.get("name")


def quote(host, secids, timeout=20):
    """批量快照，拿名称/现价/涨跌幅，fltt=2 必须带（不带会返回缩放整数）"""
    url = ("https://%s/api/qt/ulist.np/get?secids=%s"
           "&fields=f12,f13,f14,f2,f3&fltt=2&invt=2&ut=%s"
           % (host, ",".join(secids), UT))
    j = get_json(url, timeout)
    d = j.get("data") or {}
    return {str(x.get("f12")): x for x in (d.get("diff") or [])}


def to_rows(klines):
    """klines: ["2026-09-21 09:31,-12675755.0,13020693.0,...", ...]
       → [[t, main, small, mid, big, huge], ...]"""
    out = []
    for ln in klines:
        p = ln.split(",")
        try:
            vals = [float(x) for x in p[1:]]
        except ValueError:
            continue
        row = [p[0]] + vals
        while len(row) < 6:
            row.append(0.0)
        out.append(row[:6])
    return out


def slope(rows, n=5):
    """最近 n 分钟主力净额的变化斜率（元/分钟）"""
    if len(rows) < n + 1:
        return None
    return (rows[-1][1] - rows[-1 - n][1]) / float(n)


# ---------- 主流程 ----------
def main():
    cfg_path = os.path.join(ROOT, "config.json")
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    items_cfg = [dict(x, kind="index") for x in cfg.get("indexes", [])]
    items_cfg += [dict(x, kind="stock") for x in cfg.get("symbols", [])]

    secid_map = {}
    for x in items_cfg:
        secid_map[x["code"]] = secid_of(x["code"], x.get("market"))

    # 1) 健康探测：定出本轮可用节点池（多源）
    probe_code = items_cfg[0]["code"] if items_cfg else "600667"
    healthy, probe_trace = probe_health(secid_map.get(probe_code, secid_of(probe_code)))

    # 2) 批量拿名称（健康池任意可达节点即可）
    names = {}
    try:
        host_q, q, _ = try_hosts(
            lambda h: quote(h, [secid_map[x["code"]] for x in items_cfg]), healthy=healthy)
        if q:
            for x in items_cfg:
                r = q.get(x["code"])
                if r and r.get("f14"):
                    names[x["code"]] = (r.get("f14"), r.get("f2"), r.get("f3"))
            log("名称/快照取自 %s，命中 %d/%d" % (host_q, len(names), len(items_cfg)))
    except Exception as e:
        log("批量快照失败 %s" % str(e)[:80])

    tz = datetime.timezone(datetime.timedelta(hours=8))
    today = datetime.datetime.now(tz).strftime("%Y-%m-%d")

    items, errors = [], []
    ok_host = None
    host_hits = {}
    for x in items_cfg:
        code = x["code"]
        sid = secid_map[code]

        # 3) 分钟档（健康池优先 + 全池兜底，单节点空自动换下一个）
        h1, kmin, e1 = try_hosts(lambda hh, s=sid: fflow(hh, s, 1)[0], healthy=healthy)
        rows = to_rows(kmin or [])
        if h1:
            ok_host = h1
            host_hits[h1] = host_hits.get(h1, 0) + 1

        # 4) 对照组：日线档。★ 必须比对日期 —— 盘前跑的话返回的会是"上一交易日"，
        #    只看"有没有数据"会把盘前误判成"盘中未生成"。
        h2, kday, _ = try_hosts(lambda hh, s=sid: fflow(hh, s, 101)[0], healthy=healthy)
        day_rows = to_rows(kday or [])
        day_last = day_rows[-1][1] if day_rows else None
        day_date = day_rows[-1][0].strip() if day_rows else None
        day_is_today = (day_date == today)

        nm, price, pct = names.get(code, (None, None, None))
        if rows:
            items.append({
                "code": code,
                "name": nm or x.get("name"),
                "secid": sid,
                "kind": x["kind"],
                "price": price,
                "pct": pct,
                "points": len(rows),
                "first": rows[0][0],
                "last_time": rows[-1][0],
                # 当日累计（元）
                "last": {
                    "main": rows[-1][1], "small": rows[-1][2],
                    "mid": rows[-1][3], "big": rows[-1][4], "huge": rows[-1][5],
                },
                "slope5": slope(rows, 5),
                "slope15": slope(rows, 15),
                "day_main": day_last,
                "day_date": day_date,
                "rows": rows,
            })
        else:
            # 归因分三种，如实标注，不猜、不补数据
            if day_last is None:
                why = "日线档也拉不到（节点全不可达，或该标的东财无此数据）"
            elif not day_is_today:
                why = "日线最新只有 %s，不是今天 %s → 非交易日或尚未开盘" % (day_date, today)
            else:
                why = "日线已是今天(%s)但分钟档空 → 盘中分钟数据未生成/接口异常" % day_date
            errors.append({
                "code": code, "name": x.get("name"),
                "secid": sid, "day_main": day_last, "day_date": day_date,
                "day_is_today": day_is_today,
                "reason": why,
                "hosts_tried": e1,
            })
        time.sleep(0.4)

    now = datetime.datetime.now(tz)
    data = {
        "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_iso": now.isoformat(),
        "trade_date": today,
        # 记录"连得上但没数据"的节点，跟"完全连不上"区分开
        "source_host": ok_host,
        "healthy_hosts": healthy,
        "host_hits": host_hits,
        "probe_trace": probe_trace,
        "source_note": "东方财富 push2delay + 数字LB节点(82/7/15/31/41/44/62.push2) 多源轮询 /api/qt/stock/fflow/kline/get?klt=1（当日累计，单位：元）",
        "symbol_count": len(items),
        "items": items,
        "errors": errors,
        "log": LOG[-40:],
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    log("完成：命中 %d/%d，主节点=%s，节点命中=%s，产物 %d 字节"
        % (len(items), len(items_cfg), ok_host, host_hits, os.path.getsize(OUT)))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("FATAL %s" % str(e)[:200])
        sys.exit(1)
