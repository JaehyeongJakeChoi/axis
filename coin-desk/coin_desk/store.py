"""상태 저장(state/): 전략서, 애널리스트 보고서, 레드팀 의견, 체결/자산 기록, 메시지 버스."""
import json
import threading
from datetime import datetime, timezone

from . import config
from .strategies import get as get_strategy

S = config.STATE_DIR
_lock = threading.RLock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(name: str, default=None):
    p = S / name
    return json.loads(p.read_text()) if p.exists() else default


def write_json(name: str, obj):
    with _lock:
        (S / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def append_jsonl(name: str, obj):
    with _lock, open(S / name, "a") as f:
        f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")


def read_jsonl(name: str, tail: int | None = None) -> list:
    p = S / name
    if not p.exists():
        return []
    rows = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    return rows[-tail:] if tail else rows


# ------------------------------------------------------------------ 통합 피드 (대시보드 회의실)
def feed(type: str, frm: str, title: str = "", body: str = "", to: str | None = None, data=None):
    """에이전트의 모든 활동(세션 시작/종료, 도구 호출, 진행 메모, 메시지, 보고, 비판, 체결)을 한 줄씩 기록."""
    append_jsonl("feed.jsonl", {"ts": now(), "type": type, "from": frm, "to": to,
                                "title": title, "body": body, "data": data})


def read_feed(offset: int = 0, max_rows: int = 2000) -> tuple[list, int]:
    """바이트 오프셋 이후의 새 피드. 처음 접속(offset=0)이면 최근 max_rows개만."""
    p = S / "feed.jsonl"
    if not p.exists():
        return [], 0
    with open(p, "rb") as f:
        f.seek(offset)
        chunk = f.read()
    end = chunk.rfind(b"\n") + 1  # 쓰는 중인 마지막 줄은 다음 폴링에서
    rows = [json.loads(line) for line in chunk[:end].decode().splitlines() if line.strip()]
    return rows[-max_rows:], offset + end


# ------------------------------------------------------------------ 전략서 (StrategyBook)
def validate_book(b: dict) -> tuple[dict, list[str]]:
    """CSO가 낸 전략서를 하드 리밋에 맞춰 검증/클램프. 오류면 ValueError."""
    H = config.HARD_LIMITS
    warnings = []
    sleeves = b.get("sleeves") or []
    if not sleeves:
        raise ValueError("sleeves가 비어 있습니다. 전량 현금이면 weight 0인 슬리브 대신 max_exposure=0을 쓰세요.")
    clean = []
    for s in sleeves:
        strat = get_strategy(s["strategy"])
        w = float(s["weight"])
        if w < 0:
            raise ValueError(f"{s['strategy']} weight 음수")
        clean.append({"strategy": strat.name, "params": strat.validate(s.get("params")), "weight": round(w, 4)})
    total = sum(s["weight"] for s in clean)
    if total > 1.0 + 1e-9:
        raise ValueError(f"가중치 합 {total:.3f} > 1.0")
    max_exp = float(b.get("max_exposure", 1.0))
    if max_exp > H["max_exposure"]:
        warnings.append(f"max_exposure {max_exp} → {H['max_exposure']}로 클램프")
        max_exp = H["max_exposure"]
    kill = float(b.get("kill_drawdown", 0.2))
    if kill > H["kill_drawdown_max"] or kill <= 0:
        warnings.append(f"kill_drawdown {kill} → {H['kill_drawdown_max']}로 클램프")
        kill = H["kill_drawdown_max"]
    band = float(b.get("rebalance_band", 0.05))
    band = min(max(band, H["rebalance_band_min"]), H["rebalance_band_max"])
    book = {
        "sleeves": clean,
        "max_exposure": max(0.0, max_exp),
        "kill_drawdown": kill,
        "rebalance_band": band,
        "rationale": b.get("rationale", ""),
        "response_to_red_team": b.get("response_to_red_team", ""),
    }
    return book, warnings


def book_card(b: dict) -> dict:
    """피드에 싣는 전략서 요약."""
    v = b.get("verified_backtest") or {}
    return {"sleeves": [{"strategy": s["strategy"], "weight": s["weight"], "params": s["params"]} for s in b["sleeves"]],
            "max_exposure": b["max_exposure"], "kill_drawdown": b["kill_drawdown"], "rebalance_band": b["rebalance_band"],
            "is_sharpe": (v.get("in_sample") or {}).get("sharpe"), "oos_sharpe": (v.get("out_of_sample") or {}).get("sharpe"),
            "mdd": (v.get("full") or {}).get("max_drawdown"), "warnings": b.get("validation_warnings", [])}


def active_book() -> dict | None:
    return read_json("strategy_book.json")


def publish_book(book: dict):
    hist = read_jsonl("strategy_book_history.jsonl")
    book = dict(book, version=len(hist) + 1, published_at=now())
    write_json("strategy_book.json", book)
    append_jsonl("strategy_book_history.jsonl", book)
    feed("book_published", "agent_2", f"전략서 v{book['version']} 발행", book.get("rationale", ""),
         to="all", data=book_card(book))
    guard = read_json("guard.json", {})
    if guard.get("halted"):
        guard.update(halted=False, resumed_at=now(), resume_reason=f"전략서 v{book['version']} 발행")
        # 킬스위치 해제 시 고점을 현재 자산으로 리셋해야 즉시 재발동하지 않는다
        guard["peak_equity"] = None
        write_json("guard.json", guard)
    return book


# ------------------------------------------------------------------ 메시지 버스 (에이전트 간 대화)
class Bus:
    """비동기 메시지함. send_message로 보내고 read_inbox로 읽는다. 전부 state/messages.jsonl에 기록."""

    def send(self, frm: str, to: str, subject: str, body: str) -> dict:
        msg = {"id": len(read_jsonl("messages.jsonl")) + 1, "ts": now(), "from": frm, "to": to,
               "subject": subject, "body": body}
        append_jsonl("messages.jsonl", msg)
        feed("message", frm, subject, body, to=to)
        print(f"  ✉️  {frm} → {to}: {subject}")
        return msg

    def inbox(self, agent: str, mark_read: bool = True) -> list:
        cursors = read_json("inbox_cursors.json", {})
        last = cursors.get(agent, 0)
        msgs = [m for m in read_jsonl("messages.jsonl")
                if m["id"] > last and m["from"] != agent and m["to"] in (agent, "all")]
        if mark_read and msgs:
            with _lock:
                cursors = read_json("inbox_cursors.json", {})
                cursors[agent] = max(m["id"] for m in msgs)
                write_json("inbox_cursors.json", cursors)
        return msgs


bus = Bus()
