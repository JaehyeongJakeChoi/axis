"""다섯 가지 전략 패밀리. agent_4~8이 각각 하나씩 담당한다.

각 전략은 두 함수를 가진다.
- simulate(df, params, cost) -> DataFrame[ret, pos]: 완결 일봉 기준 일간 순수익률(비용 차감)과 보유 비중
- target(df_all, params) -> float: 지금 이 순간 목표 비중(0~1). df_all은 진행 중 캔들을 포함한다.

현물 롱온리이므로 비중은 0~1.
"""
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from .market import completed


@dataclass
class Param:
    lo: float
    hi: float
    kind: str = "int"  # int | float


@dataclass
class Strategy:
    name: str
    owner: str
    title: str
    description: str
    params: dict[str, Param]
    defaults: dict
    simulate: Callable = field(repr=False)
    target: Callable = field(repr=False)

    def validate(self, params: dict | None) -> dict:
        out = dict(self.defaults)
        for k, v in (params or {}).items():
            if k not in self.params:
                raise ValueError(f"{self.name}: 알 수 없는 파라미터 '{k}' (허용: {list(self.params)})")
            p = self.params[k]
            v = float(v)
            if not (p.lo <= v <= p.hi):
                raise ValueError(f"{self.name}.{k}={v} 범위 밖 [{p.lo}, {p.hi}]")
            out[k] = int(round(v)) if p.kind == "int" else v
        return out

    def describe(self) -> dict:
        return {
            "name": self.name,
            "owner": self.owner,
            "title": self.title,
            "description": self.description,
            "params": {k: {"lo": p.lo, "hi": p.hi, "kind": p.kind} for k, p in self.params.items()},
            "defaults": self.defaults,
        }


def _from_positions(df: pd.DataFrame, pos: pd.Series, cost: float) -> pd.DataFrame:
    """종가 t에 결정한 비중을 t->t+1 동안 보유. 비중 변화량만큼 편도 비용 차감."""
    r = df["close"].pct_change().fillna(0.0)
    held = pos.shift(1).fillna(0.0)
    turnover = held.diff().abs()
    turnover.iloc[0] = abs(held.iloc[0])
    return pd.DataFrame({"ret": held * r - turnover * cost, "pos": held})


def _rsi(close: pd.Series, n: int) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


# ---------------------------------------------------------------- agent_4: 추세추종 (이동평균 교차)
def _trend_pos(df, p):
    fast = df["close"].rolling(p["fast"]).mean()
    slow = df["close"].rolling(p["slow"]).mean()
    pos = (fast > slow).astype(float)
    if p["slow"] <= p["fast"]:
        pos[:] = 0.0
    return pos


def trend_simulate(df, p, cost):
    return _from_positions(df, _trend_pos(df, p), cost)


def trend_target(df_all, p):
    return float(_trend_pos(completed(df_all), p).iloc[-1])


# ---------------------------------------------------------------- agent_5: 돌파 (돈치안 채널)
def _breakout_pos(df, p):
    hi = df["high"].rolling(p["entry"]).max().shift(1)
    lo = df["low"].rolling(p["exit"]).min().shift(1)
    close = df["close"].to_numpy()
    pos = np.zeros(len(df))
    inpos = 0.0
    for i in range(len(df)):
        if np.isnan(hi.iloc[i]) or np.isnan(lo.iloc[i]):
            pos[i] = 0.0
            continue
        if inpos == 0.0 and close[i] > hi.iloc[i]:
            inpos = 1.0
        elif inpos == 1.0 and close[i] < lo.iloc[i]:
            inpos = 0.0
        pos[i] = inpos
    return pd.Series(pos, index=df.index)


def breakout_simulate(df, p, cost):
    return _from_positions(df, _breakout_pos(df, p), cost)


def breakout_target(df_all, p):
    return float(_breakout_pos(completed(df_all), p).iloc[-1])


# ---------------------------------------------------------------- agent_6: 평균회귀 (RSI 과매도 매수)
def _meanrev_pos(df, p):
    rsi = _rsi(df["close"], p["rsi_n"]).to_numpy()
    sma = df["close"].rolling(p["trend_ma"]).mean().to_numpy()
    close = df["close"].to_numpy()
    pos = np.zeros(len(df))
    inpos, held = 0.0, 0
    for i in range(len(df)):
        trend_ok = (not p["trend_filter"]) or (not np.isnan(sma[i]) and close[i] > sma[i])
        if inpos == 0.0 and rsi[i] < p["entry_rsi"] and trend_ok:
            inpos, held = 1.0, 0
        elif inpos == 1.0:
            held += 1
            if rsi[i] > p["exit_rsi"] or held >= p["max_hold"]:
                inpos = 0.0
        pos[i] = inpos
    return pd.Series(pos, index=df.index)


def meanrev_simulate(df, p, cost):
    return _from_positions(df, _meanrev_pos(df, p), cost)


def meanrev_target(df_all, p):
    return float(_meanrev_pos(completed(df_all), p).iloc[-1])


# ---------------------------------------------------------------- agent_7: 시계열 모멘텀 + 변동성 타기팅
def _tsmom_pos(df, p):
    c = df["close"]
    mom = c / c.shift(p["lookback"]) - 1
    vol = c.pct_change().rolling(p["vol_window"]).std() * np.sqrt(365)
    scale = (p["target_vol"] / vol).clip(upper=1.0)
    raw = (mom > 0).astype(float) * scale
    # 잔진동 매매를 줄이기 위해 10% 단위로 반올림
    return (raw.fillna(0.0) * 10).round() / 10


def tsmom_simulate(df, p, cost):
    return _from_positions(df, _tsmom_pos(df, p), cost)


def tsmom_target(df_all, p):
    return float(_tsmom_pos(completed(df_all), p).iloc[-1])


# ---------------------------------------------------------------- agent_8: 변동성 돌파 (래리 윌리엄스)
def vb_simulate(df, p, cost):
    """당일 시가 + k*전일 레인지를 돌파하면 진입, 다음 날 시가(≈당일 종가)에 청산."""
    prev_range = (df["high"] - df["low"]).shift(1)
    entry = df["open"] + p["k"] * prev_range
    ma_ok = df["open"] > df["close"].rolling(p["ma_filter"]).mean().shift(1) if p["ma_filter"] > 0 else True
    trig = (df["high"] >= entry) & ma_ok & prev_range.notna()
    fill = np.maximum(entry, df["open"])
    ret = np.where(trig, df["close"] / fill - 1 - 2 * cost, 0.0)
    return pd.DataFrame({"ret": ret, "pos": trig.astype(float)}, index=df.index)


def vb_target(df_all, p):
    """진행 중인 오늘 캔들의 고가가 돌파선을 넘었으면 1 (다음 09:00 KST 캔들에서 자동 0)."""
    today = df_all.iloc[-1]
    done = completed(df_all)
    if len(done) == len(df_all) or len(done) < max(2, p["ma_filter"] + 1):
        return 0.0
    y = done.iloc[-1]
    entry = today["open"] + p["k"] * (y["high"] - y["low"])
    if p["ma_filter"] > 0 and today["open"] <= done["close"].tail(p["ma_filter"]).mean():
        return 0.0
    return 1.0 if today["high"] >= entry else 0.0


STRATEGIES: dict[str, Strategy] = {
    s.name: s
    for s in [
        Strategy(
            "trend_ma", "agent_4", "추세추종 (이동평균 교차)",
            "단기 이동평균이 장기 이동평균 위에 있으면 보유. 큰 추세를 먹고 횡보장에서 손실.",
            {"fast": Param(5, 100), "slow": Param(20, 300)},
            {"fast": 20, "slow": 100}, trend_simulate, trend_target,
        ),
        Strategy(
            "donchian_breakout", "agent_5", "채널 돌파 (돈치안)",
            "종가가 직전 entry일 최고가를 돌파하면 진입, 직전 exit일 최저가를 이탈하면 청산.",
            {"entry": Param(10, 120), "exit": Param(5, 60)},
            {"entry": 55, "exit": 20}, breakout_simulate, breakout_target,
        ),
        Strategy(
            "rsi_meanrev", "agent_6", "평균회귀 (RSI 과매도)",
            "RSI가 entry_rsi 아래면 매수, exit_rsi 위 또는 max_hold일 경과 시 청산. trend_filter=1이면 trend_ma일 이평선 위에서만 매수.",
            {"rsi_n": Param(2, 30), "entry_rsi": Param(5, 45, "float"), "exit_rsi": Param(50, 90, "float"),
             "max_hold": Param(2, 30), "trend_filter": Param(0, 1), "trend_ma": Param(50, 250)},
            {"rsi_n": 14, "entry_rsi": 30.0, "exit_rsi": 60.0, "max_hold": 10, "trend_filter": 1, "trend_ma": 200},
            meanrev_simulate, meanrev_target,
        ),
        Strategy(
            "tsmom_voltarget", "agent_7", "시계열 모멘텀 + 변동성 타기팅",
            "lookback일 수익률이 양수면 롱, 비중 = min(1, target_vol / 실현변동성). 10% 단위로 반올림.",
            {"lookback": Param(10, 365), "vol_window": Param(10, 120), "target_vol": Param(0.2, 1.5, "float")},
            {"lookback": 90, "vol_window": 30, "target_vol": 0.6}, tsmom_simulate, tsmom_target,
        ),
        Strategy(
            "vol_breakout", "agent_8", "변동성 돌파 (래리 윌리엄스)",
            "당일 시가 + k×전일 레인지 돌파 시 진입, 다음 날 09:00 KST 시가에 청산. ma_filter>0이면 시가가 해당 이평선 위일 때만.",
            {"k": Param(0.2, 1.0, "float"), "ma_filter": Param(0, 60)},
            {"k": 0.5, "ma_filter": 5}, vb_simulate, vb_target,
        ),
    ]
}


def get(name: str) -> Strategy:
    if name not in STRATEGIES:
        raise ValueError(f"알 수 없는 전략 '{name}'. 사용 가능: {list(STRATEGIES)}")
    return STRATEGIES[name]
