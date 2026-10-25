"""바이낸스 현물 공개 시세 API (키 불필요). 일봉(00:00 UTC 기준)을 data/ 에 캐시한다."""
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

from . import config

_session = requests.Session()


def _get(path: str, params: dict, base: str = config.BINANCE_DATA_URL):
    for attempt in range(5):
        r = _session.get(f"{base}{path}", params=params, timeout=10)
        if r.status_code in (418, 429):
            time.sleep(float(r.headers.get("Retry-After", 1 + attempt)))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Binance rate limit: {path}")


def _klines_to_df(rows: list) -> pd.DataFrame:
    df = pd.DataFrame(
        {
            "date": pd.to_datetime([r[0] for r in rows], unit="ms"),
            "open": [float(r[1]) for r in rows],
            "high": [float(r[2]) for r in rows],
            "low": [float(r[3]) for r in rows],
            "close": [float(r[4]) for r in rows],
            "volume": [float(r[5]) for r in rows],
        }
    )
    return df.set_index("date").sort_index()


def fetch_daily(symbol: str = config.SYMBOL, start: str = "2017-01-01") -> pd.DataFrame:
    """전체 일봉 히스토리(1000개씩 페이지). 마지막 행은 진행 중인 오늘 캔들."""
    frames = []
    start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    while True:
        rows = _get("/api/v3/klines", {"symbol": symbol, "interval": "1d", "startTime": start_ms, "limit": 1000})
        if not rows:
            break
        frames.append(_klines_to_df(rows))
        start_ms = rows[-1][0] + 86_400_000
        if len(rows) < 1000:
            break
        time.sleep(0.2)
    df = pd.concat(frames).sort_index()
    return df[~df.index.duplicated(keep="last")]


def load_daily(refresh: bool = True, symbol: str = config.SYMBOL) -> pd.DataFrame:
    """캐시를 읽고, refresh=True면 최근 캔들만 갱신한다. 진행 중 캔들 포함."""
    path = config.DATA_DIR / f"{symbol}_1d.csv"
    if path.exists():
        df = pd.read_csv(path, index_col="date", parse_dates=True)
        if refresh:
            recent = _klines_to_df(_get("/api/v3/klines", {"symbol": symbol, "interval": "1d", "limit": 30}))
            df = pd.concat([df, recent])
            df = df[~df.index.duplicated(keep="last")].sort_index()
    else:
        df = fetch_daily(symbol)
    df.to_csv(path, index_label="date")
    return df


def completed(df: pd.DataFrame) -> pd.DataFrame:
    """진행 중인 오늘(UTC) 캔들을 제외한 완결 일봉."""
    today = pd.Timestamp(datetime.now(timezone.utc).date())
    return df[df.index < today]


def price(symbol: str = config.SYMBOL) -> float:
    return float(_get("/api/v3/ticker/price", {"symbol": symbol})["price"])


def snapshot(df: pd.DataFrame, px: float) -> dict:
    c = completed(df)["close"]
    rets = c.pct_change()
    ath = float(df["high"].max())

    def chg(n):
        return round(px / float(c.iloc[-n]) - 1, 4) if len(c) > n else None

    return {
        "symbol": config.SYMBOL,
        "price": px,
        "quote": config.QUOTE,
        "as_of_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "return_7d": chg(7),
        "return_30d": chg(30),
        "return_90d": chg(90),
        "return_365d": chg(365),
        "realized_vol_30d_ann": round(float(rets.tail(30).std() * np.sqrt(365)), 4),
        "realized_vol_365d_ann": round(float(rets.tail(365).std() * np.sqrt(365)), 4),
        "price_vs_sma50": round(px / float(c.tail(50).mean()) - 1, 4),
        "price_vs_sma200": round(px / float(c.tail(200).mean()) - 1, 4),
        "drawdown_from_ath": round(px / ath - 1, 4),
        "history_start": str(c.index[0].date()),
        "history_days": len(c),
    }
