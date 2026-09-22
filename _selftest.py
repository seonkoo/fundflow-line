# -*- coding: utf-8 -*-
"""离线自检：用真实抓到的东财响应做夹具，跑通 fetch.py 全流程（不联网）。"""
import json, os, sys, importlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch as F

# 2026-09-21 600667 真实分钟资金流片段（主力/小/中/大/超大，累计值，单位元）
REAL_MIN = [
    "2026-09-21 09:31,-12675755.0,13020693.0,-342878.0,-9143767.0,-3531988.0",
    "2026-09-21 09:32,-25675755.0,23020693.0,-642878.0,-16143767.0,-9531988.0",
    "2026-09-21 09:33,-38675755.0,33020693.0,-942878.0,-22143767.0,-16531988.0",
    "2026-09-21 09:34,-47675755.0,42020693.0,-1142878.0,-28143767.0,-19531988.0",
    "2026-09-21 09:35,-56675755.0,51020693.0,-1342878.0,-34143767.0,-22531988.0",
    "2026-09-21 09:36,-66347555.0,60692693.0,-1542878.0,-40143767.0,-26203788.0",
    "2026-09-21 15:00,-223547445.0,318124655.0,-94577199.0,-90496355.0,-133051090.0",
]
REAL_DAY = ["2026-09-21,-223547456.0,318124640.0,-94577200.0,-90496352.0,-133051104.0"]

def fake(url, timeout=20):
    if "fflow/kline/get" in url:
        if "klt=1&" in url or url.endswith("klt=1"):
            body = {"rc": 0, "data": {"name": "太极实业", "klines": REAL_MIN}}
        else:
            body = {"rc": 0, "data": {"name": "太极实业", "klines": REAL_DAY}}
        return "abc(" + json.dumps(body) + ");"
    if "ulist.np/get" in url:
        body = {"rc": 0, "data": {"total": 1, "diff": [
            {"f12": "600667", "f13": 1, "f14": "太极实业", "f2": 20.28, "f3": -0.78}]}}
        return json.dumps(body)
    return "{}"

F.http_get = fake
F.HOSTS = ["mock.local"]

print("== 1) secid 归一化 ==")
for c, m in [("600667", None), ("000333", None), ("588170", None), ("BK0725", None),
             ("399006", "sz"), ("000001", "sh")]:
    print("   %-8s market=%-4s -> %s" % (c, m, F.secid_of(c, m)))
assert F.secid_of("600667") == "1.600667"
assert F.secid_of("000333") == "0.000333"
assert F.secid_of("588170") == "1.588170"
assert F.secid_of("BK0725") == "90.BK0725"
print("   OK")

print("== 2) 行解析 / 斜率 ==")
rows = F.to_rows(REAL_MIN)
print("   行数=%d  首=%s  末=%s" % (len(rows), rows[0], rows[-1]))
assert len(rows) == 7 and rows[-1][1] == -223547445.0
s5 = F.slope(rows, 5)
print("   slope5=%.1f 元/分钟" % s5)
assert s5 is not None
print("   OK")

print("== 3) 主流程（写 docs/data.json） ==")
F.main()

d = json.load(open(F.OUT, encoding="utf-8"))
print("   updated_at=%s  source_host=%s  命中=%d/%d"
      % (d["updated_at"], d["source_host"], d["symbol_count"], len(d["items"]) + len(d["errors"])))
it = d["items"][0]
print("   %s(%s)  点数=%d  主力末值=%.0f  日线=%.0f  价=%s 涨跌=%s%%"
      % (it["name"], it["code"], it["points"], it["last"]["main"],
         it["day_main"], it["price"], it["pct"]))
print("   errors=%d  文件大小=%d 字节" % (len(d["errors"]), os.path.getsize(F.OUT)))
assert it["points"] == 7
assert abs(it["last"]["main"] - it["day_main"]) < 100, "分钟末值应对得上日线值"
print("\n== 全部通过 ==")
