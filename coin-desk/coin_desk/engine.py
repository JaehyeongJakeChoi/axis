"""결정론적 매매 엔진 + 리스크 가드. agent_1(트레이더)은 이 엔진의 제안을 검토하고 집행한다.

LLM 트레이더가 할 수 있는 것: 제안된 목표 비중 이하(0 ~ proposal)로 집행하거나 보류.
LLM 트레이더가 할 수 없는 것: 제안보다 비중을 늘리기, 하드 리밋/킬스위치 무시.
"""
from datetime import datetime, timezone

from . import config, market, store
from .strategies import get as get_strategy


def account_state(broker, px: float) -> dict:
    bal = broker.balances()
    equity = bal["quote"] + bal["base"] * px
    exposure = bal["base"] * px / equity if equity > 0 else 0.0
    return {"quote": round(bal["quote"], 2), "base": bal["base"], "price": px,
            "equity": round(equity, 2), "exposure": round(exposure, 4)}


def update_guard(acct: dict, book: dict) -> dict:
    """고점 대비 낙폭 킬스위치 + 일일 손실 한도. state/guard.json 에 유지."""
    g = store.read_json("guard.json", {})
    eq = acct["equity"]
    today = datetime.now(timezone.utc).date().isoformat()
    if not g.get("peak_equity") or eq > g["peak_equity"]:
        g["peak_equity"] = eq
    if g.get("day") != today:
        g["day"], g["day_start_equity"] = today, eq
    dd = eq / g["peak_equity"] - 1
    day_pnl = eq / g["day_start_equity"] - 1
    g["drawdown"], g["day_pnl"] = round(dd, 4), round(day_pnl, 4)
    if not g.get("halted") and dd <= -book["kill_drawdown"]:
        g.update(halted=True, halted_at=store.now(),
                 halt_reason=f"고점 대비 {dd:.1%} 낙폭 ≥ 킬스위치 {book['kill_drawdown']:.0%}")
        store.bus.send("risk_guard", "all", "🚨 킬스위치 발동", g["halt_reason"] + " — 전량 현금화, 새 전략서 발행 전까지 매수 금지")
    g["daily_loss_hit"] = day_pnl <= -config.HARD_LIMITS["daily_loss_limit"]
    store.write_json("guard.json", g)
    return g


def compute_proposal(broker, book: dict | None = None) -> dict:
    book = book or store.active_book()
    if not book:
        return {"error": "발행된 전략서가 없습니다. 먼저 research 사이클을 돌리세요."}
    df_all = market.load_daily(refresh=True)
    px = market.price()
    acct = account_state(broker, px)
    guard = update_guard(acct, book)

    sleeves, raw = [], 0.0
    for s in book["sleeves"]:
        t = get_strategy(s["strategy"]).target(df_all, s["params"])
        sleeves.append({"strategy": s["strategy"], "weight": s["weight"], "signal": t, "contribution": round(s["weight"] * t, 4)})
        raw += s["weight"] * t
    target = min(raw, book["max_exposure"])
    notes = []
    if guard.get("halted"):
        target, notes = 0.0, notes + [f"킬스위치 상태: {guard.get('halt_reason')}"]
    if guard.get("daily_loss_hit") and target > acct["exposure"]:
        target = acct["exposure"]
        notes.append("일일 손실 한도 도달 — 오늘은 비중 확대 금지")
    delta = target - acct["exposure"]
    return {
        "book_version": book.get("version"),
        "account": acct,
        "guard": {k: guard.get(k) for k in ("peak_equity", "drawdown", "day_pnl", "halted", "daily_loss_hit")},
        "sleeves": sleeves,
        "raw_target": round(raw, 4),
        "target_exposure": round(target, 4),
        "current_exposure": acct["exposure"],
        "delta": round(delta, 4),
        "rebalance_band": book["rebalance_band"],
        "action_needed": abs(delta) >= book["rebalance_band"] or (target == 0 and acct["exposure"] > 0.001),
        "notes": notes,
    }


def execute(broker, proposal: dict, target: float, reason: str, actor: str = "agent_1") -> dict:
    """목표 비중으로 리밸런싱. target은 [0, proposal.target_exposure]로 강제 클램프."""
    H = config.HARD_LIMITS
    cap = proposal["target_exposure"]
    clamped = min(max(0.0, float(target)), cap)
    acct = proposal["account"]
    px, eq = acct["price"], acct["equity"]
    delta_quote = (clamped - acct["exposure"]) * eq
    rec = {"ts": store.now(), "actor": actor, "broker": broker.name, "book_version": proposal.get("book_version"),
           "requested_target": target, "applied_target": round(clamped, 4), "proposal_target": cap,
           "exposure_before": acct["exposure"], "price": px, "equity_before": eq, "reason": reason}
    if clamped != target:
        rec["clamped"] = f"요청 {target} → {clamped} (허용 범위 0~{cap})"
    full_exit = clamped == 0 and acct["base"] > 0
    if abs(delta_quote) < H["min_order_quote"] and not full_exit:
        rec["result"] = "skip: 최소 주문 금액 미만"
    else:
        amt = min(abs(delta_quote), H["max_order_quote"])
        if delta_quote > 0:
            fill = broker.buy_quote(amt, px)
        else:
            qty = acct["base"] if full_exit and abs(delta_quote) <= H["max_order_quote"] else amt / px
            fill = broker.sell_base(qty, px)
        rec["fill"] = fill
        rec["result"] = "filled" if amt == abs(delta_quote) else f"partial: 1회 주문 상한 {H['max_order_quote']} {config.QUOTE}"
    after = account_state(broker, market.price())
    rec["exposure_after"], rec["equity_after"] = after["exposure"], after["equity"]
    store.append_jsonl("trades.jsonl", rec)
    side = rec.get("fill", {}).get("side", "")
    store.feed("trade", actor, f"{side} {rec['result']}".strip(), reason, data=rec)
    log_equity(after)
    return rec


def log_equity(acct: dict):
    store.append_jsonl("equity.jsonl", {"ts": store.now(), **acct})


def performance() -> dict:
    eq = store.read_jsonl("equity.jsonl")
    trades = store.read_jsonl("trades.jsonl")
    out = {"equity_points": len(eq), "trades": len(trades), "recent_trades": trades[-10:]}
    if eq:
        first, last = eq[0], eq[-1]
        peak = max(e["equity"] for e in eq)
        out.update(start=first["ts"], start_equity=first["equity"], now_equity=last["equity"],
                   total_return=round(last["equity"] / first["equity"] - 1, 4),
                   drawdown_from_peak=round(last["equity"] / peak - 1, 4),
                   btc_return_same_period=round(last["price"] / first["price"] - 1, 4),
                   current_exposure=last["exposure"])
    out["guard"] = store.read_json("guard.json", {})
    return out
