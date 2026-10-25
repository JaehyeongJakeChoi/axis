"""결정론적 백테스트 엔진. 모든 에이전트는 이 함수들을 도구로 호출해서 숫자를 얻는다(숫자를 지어내지 않도록)."""
import itertools

import numpy as np
import pandas as pd

from . import config
from .strategies import get

ANN = 365  # 코인은 365일 거래


def metrics(ret: pd.Series, pos: pd.Series | None = None) -> dict:
    ret = ret.dropna()
    if len(ret) < 2:
        return {"days": len(ret)}
    eq = (1 + ret).cumprod()
    years = len(ret) / ANN
    total = float(eq.iloc[-1] - 1)
    cagr = float(eq.iloc[-1] ** (1 / years) - 1) if years > 0 and eq.iloc[-1] > 0 else -1.0
    vol = float(ret.std() * np.sqrt(ANN))
    sharpe = float(ret.mean() / ret.std() * np.sqrt(ANN)) if ret.std() > 0 else 0.0
    down = ret[ret < 0].std()
    sortino = float(ret.mean() / down * np.sqrt(ANN)) if down and down > 0 else 0.0
    mdd = float((eq / eq.cummax() - 1).min())
    out = {
        "start": str(ret.index[0].date()),
        "end": str(ret.index[-1].date()),
        "days": len(ret),
        "total_return": round(total, 4),
        "cagr": round(cagr, 4),
        "ann_vol": round(vol, 4),
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "max_drawdown": round(mdd, 4),
        "calmar": round(cagr / abs(mdd), 3) if mdd < 0 else None,
    }
    if pos is not None:
        pos = pos.loc[ret.index]
        entries = int(((pos > 0) & (pos.shift(1).fillna(0) == 0)).sum())
        out["avg_exposure"] = round(float(pos.mean()), 3)
        out["entries"] = entries
        out["entries_per_year"] = round(entries / years, 1) if years else None
        active = ret[pos > 0]
        out["win_rate_active_days"] = round(float((active > 0).mean()), 3) if len(active) else None
    return out


def split_index(idx: pd.Index) -> pd.Timestamp:
    return idx[int(len(idx) * (1 - config.OOS_FRACTION))]


def simulate(name: str, params: dict | None, df: pd.DataFrame, cost_mult: float = 1.0) -> tuple[pd.DataFrame, dict]:
    strat = get(name)
    p = strat.validate(params)
    sim = strat.simulate(df, p, config.COST_PER_SIDE * cost_mult)
    return sim, p


def _period_report(ret, pos, df, cut):
    bh = df["close"].pct_change().fillna(0.0)
    return {
        "in_sample": metrics(ret[ret.index < cut], pos[pos.index < cut]),
        "out_of_sample": metrics(ret[ret.index >= cut], pos[pos.index >= cut]),
        "full": metrics(ret, pos),
        "buy_and_hold": {
            "in_sample": metrics(bh[bh.index < cut]),
            "out_of_sample": metrics(bh[bh.index >= cut]),
        },
    }


def yearly(ret: pd.Series) -> dict:
    return {str(y): round(float((1 + r).prod() - 1), 4) for y, r in ret.groupby(ret.index.year)}


def run_backtest(name: str, params: dict | None, df: pd.DataFrame, cost_mult: float = 1.0) -> dict:
    sim, p = simulate(name, params, df, cost_mult)
    cut = split_index(df.index)
    out = {"strategy": name, "params": p, "cost_multiplier": cost_mult, "oos_starts": str(cut.date())}
    out.update(_period_report(sim["ret"], sim["pos"], df, cut))
    out["yearly_returns"] = yearly(sim["ret"])
    return out


def param_sweep(name: str, grid: dict[str, list], df: pd.DataFrame, max_combos: int = 120) -> dict:
    """인샘플 샤프 기준 상위 조합을 보여주되, OOS 성과를 같이 보여줘서 과최적화를 드러낸다."""
    keys = list(grid)
    combos = list(itertools.product(*[grid[k] for k in keys]))
    if len(combos) > max_combos:
        return {"error": f"조합이 {len(combos)}개로 상한 {max_combos}개 초과. 그리드를 줄이세요."}
    cut = split_index(df.index)
    rows = []
    for c in combos:
        params = dict(zip(keys, c))
        try:
            sim, p = simulate(name, params, df)
        except ValueError as e:
            rows.append({"params": params, "error": str(e)})
            continue
        is_m = metrics(sim["ret"][sim.index < cut])
        oos_m = metrics(sim["ret"][sim.index >= cut])
        rows.append({
            "params": p,
            "is_sharpe": is_m.get("sharpe"), "is_cagr": is_m.get("cagr"), "is_mdd": is_m.get("max_drawdown"),
            "oos_sharpe": oos_m.get("sharpe"), "oos_cagr": oos_m.get("cagr"), "oos_mdd": oos_m.get("max_drawdown"),
        })
    ok = [r for r in rows if "error" not in r]
    ok.sort(key=lambda r: r["is_sharpe"] or -99, reverse=True)
    is_s = np.array([r["is_sharpe"] for r in ok], dtype=float)
    oos_s = np.array([r["oos_sharpe"] for r in ok], dtype=float)
    corr = float(np.corrcoef(is_s, oos_s)[0, 1]) if len(ok) > 2 and is_s.std() > 0 and oos_s.std() > 0 else None
    return {
        "strategy": name,
        "combos_tested": len(combos),
        "oos_starts": str(cut.date()),
        "is_oos_sharpe_correlation": round(corr, 3) if corr is not None else None,
        "median_oos_sharpe": round(float(np.median(oos_s)), 3) if len(ok) else None,
        "top_by_in_sample": ok[:10],
        "errors": [r for r in rows if "error" in r][:3],
    }


def robustness_check(name: str, params: dict | None, df: pd.DataFrame) -> dict:
    """파라미터 ±20% 이웃, 비용 2배, 연도별 성과 — 레드팀의 핵심 무기."""
    strat = get(name)
    base_p = strat.validate(params)
    base = run_backtest(name, base_p, df)
    neighbors = []
    for k, spec in strat.params.items():
        for f in (0.8, 1.2):
            v = base_p[k] * f
            v = min(max(v, spec.lo), spec.hi)
            if spec.kind == "int":
                v = int(round(v))
            if v == base_p[k]:
                continue
            q = dict(base_p, **{k: v})
            try:
                r = run_backtest(name, q, df)
            except ValueError:
                continue
            neighbors.append({"changed": f"{k}={v}", "full_sharpe": r["full"]["sharpe"],
                              "oos_sharpe": r["out_of_sample"]["sharpe"], "full_mdd": r["full"]["max_drawdown"]})
    stress = run_backtest(name, base_p, df, cost_mult=2.0)
    ns = [n["full_sharpe"] for n in neighbors]
    return {
        "strategy": name,
        "params": base_p,
        "base_full_sharpe": base["full"]["sharpe"],
        "base_oos_sharpe": base["out_of_sample"]["sharpe"],
        "neighbor_sharpe_min": min(ns) if ns else None,
        "neighbor_sharpe_max": max(ns) if ns else None,
        "neighbors": neighbors,
        "double_cost_full_sharpe": stress["full"]["sharpe"],
        "double_cost_oos_cagr": stress["out_of_sample"]["cagr"],
        "yearly_returns": base["yearly_returns"],
        "negative_years": [y for y, r in base["yearly_returns"].items() if r < 0],
    }


def portfolio_curve(sleeves: list[dict], df: pd.DataFrame, max_exposure: float = 1.0) -> pd.DataFrame:
    """대시보드 차트용: 전략서 누적 자산과 바이앤홀드 누적 자산(시작=1)."""
    total_w = sum(float(s["weight"]) for s in sleeves) or 1.0
    scale = min(1.0, max_exposure / total_w)
    ret = sum(simulate(s["strategy"], s.get("params"), df)[0]["ret"] * float(s["weight"]) * scale for s in sleeves)
    bh = df["close"].pct_change().fillna(0.0)
    return pd.DataFrame({"strategy": (1 + ret).cumprod(), "buy_and_hold": (1 + bh).cumprod()})


def portfolio_backtest(sleeves: list[dict], df: pd.DataFrame, max_exposure: float = 1.0) -> dict:
    """슬리브(전략별 자본 배분) 가중합. 총 가중치는 max_exposure로 스케일 캡."""
    if not sleeves:
        return {"error": "sleeves가 비었습니다."}
    total_w = sum(float(s["weight"]) for s in sleeves)
    if total_w <= 0:
        return {"error": "가중치 합이 0입니다."}
    scale = min(1.0, max_exposure / total_w)
    rets, poss, names = [], [], []
    for s in sleeves:
        sim, _ = simulate(s["strategy"], s.get("params"), df)
        w = float(s["weight"]) * scale
        rets.append(sim["ret"] * w)
        poss.append(sim["pos"] * w)
        names.append(s["strategy"])
    ret = sum(rets)
    pos = sum(poss)
    cut = split_index(df.index)
    raw = pd.concat([r for r in rets], axis=1, keys=[f"{i}:{n}" for i, n in enumerate(names)])
    corr = raw[(raw != 0).any(axis=1)].corr().round(2)
    out = {"sleeves": sleeves, "effective_scale": round(scale, 3), "oos_starts": str(cut.date())}
    out.update(_period_report(ret, pos, df, cut))
    out["yearly_returns"] = yearly(ret)
    out["sleeve_return_correlation"] = corr.to_dict()
    return out
