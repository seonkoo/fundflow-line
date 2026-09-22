#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
资金线抓取器 —— 每 5 分钟由 GitHub Actions 调用一次。

数据源：东方财富 push2 系
  ★ 关键结论（2026-09-22 实测定论，别再走弯路）：
    - 分钟级资金流（fflow/kline/get?klt=1）**只有 push2delay 这一台节点有**。
      push2his / push2test / push2 都返回 klines=[]（接口活、数据空）。
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
import urllib.parse
import urllib.request

# 东财网页 JS 里的 ut（与 akshare 的 b2884a... 返回一致，两个都能用）
UT = "fa5fd1943c7b386f172d6893dbfba10b"

# ★ 主机优先级：push2delay 是唯一有分钟资金流的节点，必须排第一
HOSTS = [
    "push2delay.eastmoney.com",
    "push2his.eastmoney.com",
    "push2test.eastmoney.com",
    "push2.eastmoney.com",
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
def try_hosts(fn):
    """在 HOSTS 上逐个试 fn(host)，返回第一个非空结果，同时记住成功的 host。"""
    errs = []
    for host in HOSTS:
        try:
            r = fn(host)
        except Exception as e:
            errs.append("%s:%s" % (host, str(e)[:70]))
            continue
        if r:
            return host, r, errs
        errs.append("%s:空" % host)
    return None, None, errs


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

    # 1) 先批量拿名称（任一台可达的节点都行）
    names = {}
    try:
        host_q, q, _ = try_hosts(
            lambda h: quote(h, [secid_map[x["code"]] for x in items_cfg]))
        if q:
            for x in items_cfg:
                r = q.get(x["code"])
                if r and r.get("f14"):
                    names[x["code"]] = (r.get("f14"), r.get("f2"), r.get("f3"))
            log("名称/快照取自 %s，命中 %d/%d" % (host_q, len(names), len(items_cfg)))
    except Exception as e:
        log("批量快照失败 %s" % str(e)[:80])

    items, errors = [], []
    ok_host = None
    for x in items_cfg:
        code = x["code"]
        sid = secid_map[code]

        # 2) 分钟档
        h1, kmin, e1 = try_hosts(lambda hh, s=sid: fflow(hh, s, 1)[0])
        rows = to_rows(kmin or [])
        if h1:
            ok_host = h1

        # 3) 对照组：日线档（用来区分"非交易日"和"接口没数据"）
        h2, kday, _ = try_hosts(lambda hh, s=sid: fflow(hh, s, 101)[0])
        day_rows = to_rows(kday or [])
        day_last = day_rows[-1][1] if day_rows else None

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
                "rows": rows,
            })
        else:
            errors.append({
                "code": code, "name": x.get("name"),
                "secid": sid, "day_main": day_last,
                "reason": "分钟档无数据" + ("（日线档有数据→盘中或未生成）"
                                      if day_last is not None else "（日线档也无数据→大概率非交易日）"),
                "hosts_tried": e1,
            })
        time.sleep(0.25)

    tz = datetime.timezone(datetime.timedelta(hours=8))
    now = datetime.datetime.now(tz)
    data = {
        "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_iso": now.isoformat(),
        "tz": "Asia/Shanghai(+08:00)",
        "source_host": ok_host,
        "source_note": "东方财富 push2delay /api/qt/stock/fflow/kline/get?klt=1（当日累计，单位：元）",
        "symbol_count": len(items),
        "items": items,
        "errors": errors,
        "log": LOG[-40:],
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    log("完成：命中 %d/%d，主节点=%s，产物 %d 字节"
        % (len(items), len(items_cfg), ok_host, os.path.getsize(OUT)))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("FATAL %s" % str(e)[:200])
        sys.exit(1)
