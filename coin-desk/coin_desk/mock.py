"""--mock: API 비용 없이 배선을 점검하는 가짜 LLM. 각 역할의 도구를 정해진 순서로 호출한다."""
import os
import time

from . import store
from .agents import ANALYST_OF
from .llm import Done

DELAY = float(os.getenv("COIN_DESK_MOCK_DELAY", "0.4"))  # 대시보드에서 진행 상황이 보이도록

GRIDS = {
    "trend_ma": {"fast": [10, 20, 30], "slow": [50, 100, 150]},
    "donchian_breakout": {"entry": [20, 40, 55], "exit": [10, 20]},
    "rsi_meanrev": {"rsi_n": [2, 7, 14], "entry_rsi": [10.0, 25.0], "exit_rsi": [60.0, 70.0]},
    "tsmom_voltarget": {"lookback": [30, 90, 180], "target_vol": [0.4, 0.6]},
    "vol_breakout": {"k": [0.3, 0.5, 0.7], "ma_filter": [0, 5, 10]},
}


class MockLLM:
    def run(self, *, role, label, system, user, tools, handler, **_):
        aid = label.split(":")[0]
        raw = handler

        def handler(name, inp):
            time.sleep(DELAY)
            return raw(name, inp)

        def note(text):
            store.feed("note", aid, "", f"(mock) {text}")

        try:
            if label.endswith(":consult"):
                snap = handler("market_snapshot", {})
                return {"text": f"(mock {aid}) 현재가 {snap['price']}, 30일 수익률 {snap['return_30d']}", "submitted": None}
            handler("read_inbox", {})
            if role == "analyst":
                strat = ANALYST_OF[aid]
                note(f"{strat} 파라미터 그리드를 훑어보고 IS/OOS가 같이 좋은 설정을 찾겠습니다.")
                sw = handler("param_sweep", {"strategy": strat, "grid": GRIDS[strat]})
                best = max(sw["top_by_in_sample"], key=lambda r: min(r["is_sharpe"], r["oos_sharpe"]))
                if aid == "agent_4":
                    handler("ask_agent", {"to": "agent_7", "question": "모멘텀과 추세추종의 중복을 어떻게 보나요?"})
                handler("send_message", {"to": "agent_2", "subject": f"{strat} 메모", "body": "mock 메모"})
                score = max(0.0, min(best["is_sharpe"], best["oos_sharpe"]))
                handler("submit_report", {"params": best["params"], "summary": "mock 보고", "recommended_weight": round(min(1, score / 2), 2),
                                          "confidence": 0.5, "regime_view": "mock", "risks": ["mock"]})
            if role == "cso":
                reports = handler("read_reports", {})
                note("보고서 5개를 받았습니다. 추천 비중 비례로 초안을 만들고 포트폴리오 백테스트로 확인합니다.")
                ws = {r["strategy"]: (r["recommended_weight"], r["params"]) for r in reports.values()}
                tot = sum(w for w, _ in ws.values()) or 1
                sleeves = [{"strategy": s, "params": p, "weight": round(w / tot * 0.999, 3)} for s, (w, p) in ws.items() if w > 0]
                handler("portfolio_backtest", {"sleeves": sleeves})
                handler("submit_strategy_book", {"sleeves": sleeves, "max_exposure": 1.0, "kill_drawdown": 0.25,
                                                 "rebalance_band": 0.05, "rationale": "mock: 추천 비중 비례",
                                                 "response_to_red_team": "mock 반영" if "레드팀 검토 의견" in user else ""})
            if role == "redteam":
                if "모니터링" in user:
                    handler("read_performance", {})
                    handler("submit_critique", {"verdict": "approve", "summary": "mock 모니터링 이상 없음", "issues": [], "escalate": False})
                book = handler("read_strategy_book", {})["draft"]
                note("초안의 상위 슬리브부터 파라미터 이웃 민감도와 비용 2배 스트레스를 보겠습니다.")
                for s in book["sleeves"][:2]:
                    handler("robustness_check", {"strategy": s["strategy"], "params": s["params"]})
                verdict = "revise" if "라운드 1/" in user else "approve"
                handler("submit_critique", {"verdict": verdict, "summary": f"mock {verdict}",
                                            "issues": [{"severity": "medium", "issue": "mock 지적", "evidence": "mock"}]})
            if role == "trader":
                p = handler("get_trade_proposal", {})
                if p.get("action_needed"):
                    handler("execute_rebalance", {"target_exposure": p["target_exposure"], "reason": "mock 제안대로"})
                handler("hold", {"reason": "mock 밴드 이내"})
        except Done as d:
            return {"text": "", "submitted": d.result}
        return {"text": "(mock) 제출 없음", "submitted": None}
