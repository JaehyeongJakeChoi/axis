"""아침 브리핑용 매크로 수치. 기사에서 숫자를 옮겨 적지 않도록 시세를 직접 읽어 JSON 으로 출력한다.

    python3 macro.py                     # 시장 지표만
    python3 macro.py --holdings h.json   # + 보유 종목 기준 하루 기여도, 노출, 매크로 민감도

h.json 은 get_portfolio 응답(또는 snapshots 문서) 그대로: {"fx": {...} 또는 숫자, "holdings": [...], "cash": [...]}.
읽기 전용이고 주문·계좌와 무관하다.
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import server

KST = timezone(timedelta(hours=9))

# (분류, 이름, 야후 심볼, 단위) — 단위 "pct" 는 수익률(%), "bp" 는 금리(변화를 bp 로), "pt" 는 지수 수준 변화(VIX 등)
SERIES = (
    ("미국 주식", "S&P 500", "^GSPC", "pct"), ("미국 주식", "나스닥", "^IXIC", "pct"),
    ("미국 주식", "필라델피아 반도체", "^SOX", "pct"), ("미국 주식", "러셀 2000", "^RUT", "pct"),
    ("미국 주식", "S&P 500 선물", "ES=F", "pct"), ("미국 주식", "나스닥 100 선물", "NQ=F", "pct"),
    ("아시아·유럽", "코스피", "^KS11", "pct"), ("아시아·유럽", "코스닥", "^KQ11", "pct"),
    ("아시아·유럽", "닛케이 225", "^N225", "pct"), ("아시아·유럽", "상하이 종합", "000001.SS", "pct"),
    ("아시아·유럽", "항셍", "^HSI", "pct"), ("아시아·유럽", "유로스톡스 50", "^STOXX50E", "pct"),
    ("미국 금리", "미 3개월물", "^IRX", "bp"), ("미국 금리", "미 5년물", "^FVX", "bp"),
    ("미국 금리", "미 10년물", "^TNX", "bp"), ("미국 금리", "미 30년물", "^TYX", "bp"),
    ("환율", "달러 인덱스", "DX-Y.NYB", "pct"), ("환율", "원/달러", "KRW=X", "pct"),
    ("환율", "엔/달러", "JPY=X", "pct"), ("환율", "위안/달러", "CNY=X", "pct"),
    ("원자재", "WTI", "CL=F", "pct"), ("원자재", "브렌트", "BZ=F", "pct"),
    ("원자재", "금", "GC=F", "pct"), ("원자재", "구리", "HG=F", "pct"),
    ("변동성", "VIX", "^VIX", "pt"),
    ("코인", "비트코인", "BTC-USD", "pct"), ("코인", "이더리움", "ETH-USD", "pct"),
)
# 민감도 계산에 쓰는 요인: 하루 변화(수익률 또는 bp)에 대한 포트폴리오 하루 수익률의 기울기
FACTORS = (("S&P 500", "^GSPC", "pct"), ("나스닥", "^IXIC", "pct"), ("필라델피아 반도체", "^SOX", "pct"),
           ("미 10년물", "^TNX", "bp"), ("원/달러", "KRW=X", "pct"), ("WTI", "CL=F", "pct"), ("비트코인", "BTC-USD", "pct"))


def chart(sym):
    """일봉 1년 + 현재가. 실패하면 None."""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=1y&interval=1d" % server.urllib.parse.quote(sym)
    _, data, _ = server.http("GET", url)
    try:
        res = data["chart"]["result"][0]
        off = int(res["meta"].get("gmtoffset") or 0)
        closes = [(datetime.fromtimestamp(t + off, timezone.utc).strftime("%Y-%m-%d"), c)
                  for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]) if c is not None]
        t = res["meta"].get("regularMarketTime")
        day = datetime.fromtimestamp(t + off, timezone.utc).strftime("%Y-%m-%d") if t else None
        dedup = {}
        for d, c in closes:                            # 야후가 같은 날 봉을 두 번 줄 때가 있다
            dedup[d] = c
        return {"closes": sorted(dedup.items()), "last": res["meta"].get("regularMarketPrice"), "time": t, "day": day}
    except (TypeError, KeyError, IndexError, ValueError):
        return None


def change(a, b, unit):
    if a is None or b is None:
        return None
    if unit == "bp":
        return round((b - a) * 100, 1)
    if unit == "pt":
        return round(b - a, 2)
    return round((b / a - 1) * 100, 2) if a else None


def row(group, label, sym, unit, c):
    closes = c["closes"]
    last = c["last"] if c["last"] is not None else closes[-1][1]
    asof = datetime.fromtimestamp(c["time"], KST).strftime("%m-%d %H:%M") if c["time"] else closes[-1][0]
    # 현재가가 속한 거래일의 봉은 빼고, 그 앞 봉들을 기준으로 변화를 잰다
    base = [x for x in closes if x[0] != c["day"]] if c["day"] else closes[:-1]
    prev = base[-1][1] if base else None
    ago = lambda n: base[-n][1] if len(base) >= n else None
    lo, hi = min(x for _, x in closes), max(x for _, x in closes)
    return {"group": group, "label": label, "symbol": sym, "unit": unit, "last": round(last, 4), "asof_kst": asof,
            "chg_1d": change(prev, last, unit), "chg_1w": change(ago(5), last, unit), "chg_1m": change(ago(21), last, unit),
            "range_52w_pos": round((last - lo) / (hi - lo), 2) if hi > lo else None}


def fed_funds():
    """30일 연방기금 선물 근월물 가격으로 본 시장 내재 금리 = 100 − 가격."""
    c = chart("ZQ=F")
    return {"implied_rate": round(100 - c["last"], 3), "contract": "ZQ=F 근월물"} if c and c["last"] else None


def markets():
    with ThreadPoolExecutor(max_workers=8) as pool:
        charts = list(pool.map(lambda s: chart(s[2]), SERIES))
    out, missing = [], []
    for s, c in zip(SERIES, charts):
        if c and c["closes"]:
            out.append(row(*s, c))
        else:
            missing.append(s[1])
    curve = {r["symbol"]: r["last"] for r in out if r["unit"] == "bp"}
    spread = round((curve["^TNX"] - curve["^IRX"]) * 100, 1) if "^TNX" in curve and "^IRX" in curve else None
    return out, missing, spread, charts


def value_krw(h, fx):
    return h["qty"] * (h.get("price") or 0) * (fx if h["ccy"] == "USD" else 1)


def portfolio(doc, charts):
    fx = doc.get("fx")
    fx = fx.get("usdkrw") if isinstance(fx, dict) else fx
    hs = [h for h in doc.get("holdings", []) if h.get("qty") and h.get("price")]
    cash = sum(c["amount"] * (fx if c["ccy"] == "USD" else 1) for c in doc.get("cash", []))
    vals = {h["id"]: value_krw(h, fx) for h in hs}
    total = sum(vals.values()) + cash
    if total <= 0:
        return None

    # 하루 기여도: 종목 하루 손익(원)과 총자산 대비 비율
    contrib = []
    for h in hs:
        if h.get("prev_close"):
            krw = h["qty"] * (h["price"] - h["prev_close"]) * (fx if h["ccy"] == "USD" else 1)
            contrib.append({"name": h["name"], "symbol": h["symbol"], "move_pct": round((h["price"] / h["prev_close"] - 1) * 100, 2),
                            "pnl_krw": round(krw), "of_total_pct": round(krw / total * 100, 3)})
    contrib.sort(key=lambda x: x["pnl_krw"])
    day = sum(c["pnl_krw"] for c in contrib)

    by_cls = {}
    for h in hs:
        by_cls[h["cls"]] = by_cls.get(h["cls"], 0) + vals[h["id"]]
    usd = sum(vals[h["id"]] for h in hs if h["ccy"] == "USD") + sum(c["amount"] * fx for c in doc.get("cash", []) if c["ccy"] == "USD")
    top = sorted(hs, key=lambda h: -vals[h["id"]])[:5]

    # 매크로 민감도: 지난 1년 하루 변화에 대한 지금 구성의 하루 수익률 기울기(단순 회귀). 한국 장과 미국 장의 시차는 맞추지 않았다.
    with ThreadPoolExecutor(max_workers=8) as pool:
        hist = dict(zip([h["id"] for h in hs], pool.map(server._holding_history, hs)))
    fchart = {sym: next((c for s, c in zip(SERIES, charts) if s[2] == sym), None) for _, sym, _ in FACTORS}
    days = sorted({d for c in fchart.values() if c for d, _ in c["closes"]})[-253:]

    def aligned(series):
        out, last, keys, j = [], None, sorted(series), 0
        for d in days:
            while j < len(keys) and keys[j] <= d:
                last = series[keys[j]]
                j += 1
            out.append(last)
        return out
    w_total = sum(vals[i] for i in hist if hist[i]) + cash
    port = [0.0] * (len(days) - 1)
    for hid, s in hist.items():
        px = aligned(s) if s else []
        if sum(1 for x in px if x) < len(days) * 0.9:
            continue
        for t in range(len(days) - 1):
            a, b = px[t], px[t + 1]
            port[t] += vals[hid] / w_total * ((b / a - 1) if a and b else 0.0)
    sens = []
    for label, sym, unit in FACTORS:
        c = fchart.get(sym)
        if not c:
            continue
        px = aligned(dict(c["closes"]))
        f = [((b - a) * 100 if unit == "bp" else (b / a - 1) * 100) if a and b else 0.0 for a, b in zip(px, px[1:])]
        pr = [x * 100 for x in port]
        cv, vf = server._cov(pr, f), server._std(f) ** 2
        if not vf:
            continue
        beta = cv / vf
        corr = cv / (server._std(pr) * server._std(f)) if server._std(pr) else None
        step = 10 if unit == "bp" else 1
        sens.append({"factor": label, "per": "10bp" if unit == "bp" else "1%", "port_move_pct": round(beta * step, 3),
                     "krw": round(beta * step / 100 * total), "corr": round(corr, 2) if corr is not None else None})
    return {"total_krw": round(total), "day_pnl_krw": round(day), "day_pnl_pct": round(day / total * 100, 2),
            "by_class_pct": {k: round(v / total * 100, 1) for k, v in by_cls.items()} | {"cash": round(cash / total * 100, 1)},
            "usd_exposure_pct": round(usd / total * 100, 1),
            "top5": [{"name": h["name"], "weight_pct": round(vals[h["id"]] / total * 100, 1)} for h in top],
            "top5_pct": round(sum(vals[h["id"]] for h in top) / total * 100, 1),
            "day_losers": contrib[:5], "day_gainers": contrib[::-1][:5],
            "sensitivity": sens, "sensitivity_note": "지난 1년 하루 변화 기준 단순 회귀. 요인 1단위(1% 또는 10bp)일 때 포트폴리오 평균 변화. 장 시차는 보정하지 않음."}


if __name__ == "__main__":
    rows, missing, spread, charts = markets()
    out = {"generated_kst": datetime.now(KST).isoformat(timespec="minutes"), "markets": rows, "missing": missing,
           "us_10y_3m_spread_bp": spread, "fed_funds_futures": fed_funds()}
    if "--holdings" in sys.argv:
        with open(sys.argv[sys.argv.index("--holdings") + 1]) as f:
            doc = json.load(f)
        out["portfolio"] = portfolio(doc.get("data", doc), charts)
    print(json.dumps(out, ensure_ascii=False, indent=1))
