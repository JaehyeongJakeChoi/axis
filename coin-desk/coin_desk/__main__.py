"""CLI: uv run python -m coin_desk <command>"""
import argparse
import json

from . import backtest, config, engine, market, store
from .broker import make_broker


def build_desk(args):
    from .desk import Desk
    if args.mock:
        from .mock import MockLLM
        llm = MockLLM()
    else:
        from .llm import LLM
        llm = LLM()
    return Desk(make_broker(live_flag=args.live), llm)


def main():
    ap = argparse.ArgumentParser(prog="coin_desk", description="8-에이전트 BTC 자동매매 데스크")
    ap.add_argument("--mock", action="store_true", help="LLM 대신 가짜 정책으로 배선만 점검(API 비용 없음)")
    ap.add_argument("--live", action="store_true", help="실거래 (COIN_DESK_MODE=live 도 필요)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch", help="바이낸스 일봉 히스토리 다운로드/갱신")
    sub.add_parser("research", help="리서치 회의: 애널리스트 5명 → CSO → 레드팀 토론 → 전략서 발행")
    t = sub.add_parser("trade", help="매매 사이클 1회 (agent_1)")
    t.add_argument("--no-llm", action="store_true", help="LLM 없이 엔진 제안대로 집행")
    sub.add_parser("monitor", help="레드팀 운용 모니터링 1회")
    r = sub.add_parser("run", help="상시 운용 루프")
    r.add_argument("--trade-minutes", type=int, default=60)
    r.add_argument("--research-hours", type=float, default=24)
    r.add_argument("--no-llm-trader", action="store_true")
    a = sub.add_parser("ask", help="에이전트에게 직접 질문")
    a.add_argument("agent")
    a.add_argument("question")
    sub.add_parser("status", help="전략서, 계좌, 가드, 비용 요약")
    m = sub.add_parser("messages", help="에이전트 간 메시지/대화 로그")
    m.add_argument("--tail", type=int, default=20)
    sub.add_parser("reset-guard", help="킬스위치 수동 해제(고점 리셋)")
    w = sub.add_parser("web", help="대시보드 서버 (http://127.0.0.1:8765)")
    w.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    if args.cmd == "fetch":
        df = market.load_daily(refresh=True)
        print(f"{config.SYMBOL} 일봉 {len(df)}개: {df.index[0].date()} ~ {df.index[-1].date()}")
    elif args.cmd == "research":
        build_desk(args).research()
    elif args.cmd == "trade":
        build_desk(args).trade(use_llm=not args.no_llm)
    elif args.cmd == "monitor":
        build_desk(args).monitor()
    elif args.cmd == "run":
        build_desk(args).run_forever(args.trade_minutes, args.research_hours, not args.no_llm_trader)
    elif args.cmd == "ask":
        desk = build_desk(args)
        if args.agent not in desk.agents:
            raise SystemExit(f"에이전트는 {list(desk.agents)} 중 하나")
        from .agents import ask
        print(ask(desk, "user", args.agent, args.question, "데스크 운영자(사용자)"))
    elif args.cmd == "status":
        from .llm import usage_summary
        book = store.active_book()
        print(f"모드: {config.MODE}  심볼: {config.SYMBOL}")
        if book:
            print(f"전략서 v{book['version']} ({book['published_at']}) max_exposure={book['max_exposure']} kill_dd={book['kill_drawdown']}")
            for s in book["sleeves"]:
                print(f"  - {s['strategy']:<18} w={s['weight']:.2f} {s['params']}")
            pf = backtest.portfolio_backtest(book["sleeves"], market.completed(market.load_daily()), book["max_exposure"])
            print(f"  백테스트 샤프 IS {pf['in_sample']['sharpe']} / OOS {pf['out_of_sample']['sharpe']}, 전체 MDD {pf['full']['max_drawdown']}")
        else:
            print("발행된 전략서 없음 — research를 먼저 실행하세요.")
        perf = engine.performance()
        perf.pop("recent_trades", None)
        print("운용 성과:", json.dumps(perf, ensure_ascii=False, indent=1, default=str))
        print("LLM 비용(추정):", usage_summary())
    elif args.cmd == "messages":
        rows = sorted(store.read_jsonl("messages.jsonl") + [
            {"ts": c["ts"], "from": c["from"], "to": c["to"], "subject": "Q: " + c["question"], "body": "A: " + c["answer"]}
            for c in store.read_jsonl("conversations.jsonl")], key=lambda m: m["ts"])
        for msg in rows[-args.tail:]:
            print(f"[{msg['ts']}] {msg['from']} → {msg['to']}: {msg['subject']}\n    {msg['body'][:400]}\n")
    elif args.cmd == "web":
        from .web import serve
        serve(args.port, live_flag=args.live)
    elif args.cmd == "reset-guard":
        g = store.read_json("guard.json", {})
        g.update(halted=False, peak_equity=None, resumed_at=store.now(), resume_reason="수동 해제")
        store.write_json("guard.json", g)
        print("킬스위치 해제, 고점 리셋 완료")


if __name__ == "__main__":
    main()
