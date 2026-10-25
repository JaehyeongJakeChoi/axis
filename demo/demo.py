#!/usr/bin/env python3
"""
Axis demo mode: runs the real server.py pipeline on a fictional portfolio.

Broker calls (Kiwoom, Upbit private API) are replaced with made-up holdings.
Prices, FX, benchmarks, dividends and every risk metric/signal still come from
the same public market data and the same code paths as the live dashboard.
No API keys are needed and nothing is written outside a temp directory.

  python3 demo/demo.py            MCP server (stdio), drop-in for server.py
  python3 demo/demo.py --dump     print one full get_portfolio payload as JSON
  python3 demo/demo.py --signals  print the five risk signals (percentages only)
"""

import json
import os
import random
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import server  # noqa: E402

# Keep every file the server would write (ledger, alert state, signal state) out of the repo.
_tmp = tempfile.mkdtemp(prefix="axis-demo-")
for attr in ("ENV_FILE", "TOKEN_FILE", "LEDGER_FILE", "SENT_FILE", "SIGNAL_RISK_FILE", "SIGNAL_STATE_FILE"):
    setattr(server, attr, os.path.join(_tmp, os.path.basename(getattr(server, attr))))
server.ALERT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "alert-settings.example.json")

_DEMO_ENV = {"UPBIT_ACCESS_KEY": "demo", "UPBIT_SECRET_KEY": "demo", "KIWOOM_APP_KEY": "demo", "KIWOOM_SECRET_KEY": "demo"}
server.env = lambda key: _DEMO_ENV.get(key, "")

# Fictional portfolio. avg_vs_now is the average cost as a multiple of today's price.
KR = [("005930", "삼성전자", 140, 0.82), ("000660", "SK하이닉스", 18, 0.71), ("035420", "NAVER", 22, 1.08)]
US = [("NVDA", 45, 0.64), ("MSFT", 14, 0.93), ("AAPL", 24, 0.88), ("VOO", 12, 0.85), ("COIN", 16, 1.12)]
CRYPTO = [("BTC", "비트코인", 0.11, 0.78), ("ETH", "이더리움", 1.6, 1.05)]
CASH = [("cash:kiwoom:KRW", "키움 원화 예수금", "KRW", 2_400_000), ("cash:kiwoom:USD", "키움 달러 예수금", "USD", 1_150.0),
        ("cash:upbit:KRW", "업비트 원화", "KRW", 850_000)]

KST = server.KST


def _last_two(daily):
    days = sorted(daily)
    return (daily[days[-1]], daily[days[-2]]) if len(days) >= 2 else (None, None)


def fetch_kiwoom_kr(debug):
    out = []
    for code, name, qty, k in KR:
        price, prev = _last_two(server.yahoo_daily(code + ".KS", "1mo"))
        out.append({"id": "kr:" + code, "cls": "kr", "symbol": code, "name": name, "qty": qty,
                    "avg": round(price * k) if price else None, "price": price, "prev_close": prev,
                    "ccy": "KRW", "broker": "kiwoom", "pnl_krw": None, "today_buy": 0, "today_sell": 0})
    return {"holdings": out, "cash": [{"id": c[0], "label": c[1], "ccy": c[2], "amount": c[3]} for c in CASH if c[0] == "cash:kiwoom:KRW"]}


def fetch_kiwoom_us(debug):
    fx = server.yahoo_quote("KRW=X", 60)["price"]
    out = []
    for sym, qty, k in US:
        q = server.yahoo_quote(sym, 60)
        avg = round(q["price"] * k, 2) if q["price"] else None
        out.append({"id": "us:" + sym, "cls": "us", "symbol": sym, "name": q["name"] or sym, "qty": qty,
                    "avg": avg, "avg_krw": round(avg * fx * 0.97) if avg and fx else None, "price": q["price"],
                    "prev_close": None, "ccy": "USD", "broker": "kiwoom", "pnl_krw": None, "today_buy": 0, "today_sell": 0})
    return {"holdings": out, "cash": [{"id": c[0], "label": c[1], "ccy": c[2], "amount": c[3]} for c in CASH if c[0] == "cash:kiwoom:USD"],
            "fx_rate": fx}


def _demo_fills(prices):
    """A year of made-up BTC/ETH trades at real historical closes, run through the real cost-basis code."""
    rng = random.Random(7)
    led = {"fills": {}, "avg": {}}
    for sym, name, _, _ in CRYPTO:
        daily = server.yahoo_daily(sym + "-KRW", "1y")
        days = sorted(daily)[:-1]
        if not days:
            continue
        for i, day in enumerate(sorted(rng.sample(days, min(14, len(days))))):
            side = "buy" if i < 4 or rng.random() < 0.55 else "sell"
            price = daily[day]
            funds = round(rng.uniform(300_000, 1_500_000), -3)
            fid = "demo-%s-%d" % (sym, i)
            led["fills"][fid] = {"id": fid, "ts": day + "T10:%02d:00+09:00" % rng.randint(0, 59), "holding_id": "crypto:" + sym,
                                 "symbol": sym, "name": name, "side": side, "qty": funds / price, "funds": funds,
                                 "fee": round(funds * 0.0005), "price": price, "avg": None, "realized": None}
        led["avg"]["crypto:" + sym] = prices.get(sym)
    server.ledger_recompute(led)
    return sorted(led["fills"].values(), key=lambda f: f["ts"], reverse=True)


def fetch_upbit(debug, light=False):
    ticks = server.upbit_ticker(["KRW-" + s for s, _, _, _ in CRYPTO])
    out, prices = [], {}
    for sym, name, qty, k in CRYPTO:
        t = ticks.get("KRW-" + sym) or {}
        price, prev = server.num(t.get("trade_price")) or None, server.num(t.get("prev_closing_price")) or None
        prices[sym] = price * k if price else None
        out.append({"id": "crypto:" + sym, "cls": "crypto", "symbol": sym, "name": name, "qty": qty,
                    "avg": round(price * k) if price else None, "price": price, "prev_close": prev,
                    "ccy": "KRW", "broker": "upbit"})
    cash = [{"id": c[0], "label": c[1], "ccy": c[2], "amount": c[3]} for c in CASH if c[0] == "cash:upbit:KRW"]
    if light:
        return {"holdings": out, "cash": cash, "fills": None, "flows": None, "flows_summary": None}
    summary = {"deposit_krw": 12_000_000.0, "withdraw_krw": 1_500_000.0, "coin_in_krw": 0.0, "coin_out_krw": 0.0,
               "unknown": 0, "coin_count": 0, "staking_count": 0, "count": 6}
    summary["net"] = summary["deposit_krw"] - summary["withdraw_krw"]
    return {"holdings": out, "cash": cash, "fills": _demo_fills(prices), "flows": [], "flows_summary": summary}


def kiwoom_history(changed=False):
    """A few made-up realized sells so the history tab has something to show."""
    now = datetime.now(KST)
    realized = []
    for i, (cls, sym, name, qty, funds, pnl) in enumerate((
            ("us", "TSLA", "Tesla", 6, 2_310_000, 412_000), ("kr", "005380", "현대차", 8, 1_880_000, -96_000),
            ("us", "AMD", "AMD", 10, 2_050_000, 655_000), ("kr", "035720", "카카오", 30, 1_260_000, -240_000))):
        day = (now - timedelta(days=9 + 23 * i)).strftime("%Y%m%d")
        realized.append({"id": "demo-kw-%d" % i, "ts": server._day_iso(day), "holding_id": "%s:%s" % (cls, sym), "symbol": sym,
                         "name": name, "cls": cls, "side": "sell", "qty": qty, "funds": funds, "fee": round(funds * 0.0025),
                         "price": funds / qty, "avg": (funds - pnl) / qty, "realized": pnl})
    summary = {"deposit_krw": 30_000_000.0, "withdraw_krw": 2_000_000.0, "coin_in_krw": 0.0, "coin_out_krw": 0.0,
               "unknown": 0, "coin_count": 0, "count": 5}
    summary["net"] = summary["deposit_krw"] - summary["withdraw_krw"]
    return {"realized": realized, "buys": [], "flows": [], "flows_summary": summary, "income": []}


def fetch_fx(kiwoom_ready):
    q = server.yahoo_quote("KRW=X", 60)
    return {"usdkrw": q["price"], "source": "yahoo"}


server.fetch_kiwoom_kr = fetch_kiwoom_kr
server.fetch_kiwoom_us = fetch_kiwoom_us
server.fetch_upbit = fetch_upbit
server.kiwoom_history = kiwoom_history
server.kiwoom_pending = lambda hist, holdings, usdkrw: {"buys": [], "realized": []}
server.fetch_fx = fetch_fx
server.ntfy_send = lambda *a, **k: False   # never push from the demo


def _dump():
    p = server.build_portfolio({})
    if p.get("div_upcoming") is None:   # the dividend calendar is computed in the background on first call
        import time
        for _ in range(60):
            time.sleep(1)
            if server._divcal["data"] is not None and not server._divcal["running"]:
                break
        p["div_upcoming"] = server._divcal["data"]
    p["demo"] = True
    return p


if __name__ == "__main__":
    if "--dump" in sys.argv:
        print(json.dumps(_dump(), ensure_ascii=False, default=str))
    elif "--signals" in sys.argv:
        p = server.build_portfolio({})
        print(json.dumps(p.get("signals"), ensure_ascii=False, indent=1))
    else:
        server.serve()
