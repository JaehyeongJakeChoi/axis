"""데스크 오케스트레이터: 리서치 회의, 매매 사이클, 레드팀 모니터링, 상시 운용 루프."""
import json
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from . import config, engine, market, store
from .agents import ANALYST_OF, ROSTER, Agent

ANALYST_TASK = """정기 전략 리서치 회의입니다. 담당 전략을 분석해 submit_report로 CSO에게 보고하세요.
이전 보고서(read_reports)가 있다면 무엇이 바뀌었는지도 summary에 적으세요."""

CSO_TASK = """애널리스트 보고가 모두 들어왔습니다. 보고서들을 검토·절충해 전략서 초안을 submit_strategy_book으로 제출하세요.
현재 발행된 전략서(read_strategy_book)와 실제 운용 성과(read_performance), 최근 레드팀 의견(read_critiques)도 참고하세요."""

REDTEAM_TASK = """CSO가 전략서 초안을 제출했습니다(read_strategy_book의 draft). 비판적으로 검토하고 submit_critique로 의견을 내세요. (검토 라운드 {r}/{n})"""

CSO_REVISE_TASK = """레드팀 검토 의견이 도착했습니다:
{critique}

각 지적에 대해 수용(수정) 또는 증거 기반 반박을 하고, 수정한 전략서를 submit_strategy_book으로 다시 제출하세요(response_to_red_team 필수). 현재 초안은 read_strategy_book의 draft입니다."""

TRADER_TASK = "정기 매매 사이클입니다. get_trade_proposal로 제안을 확인하고 execute_rebalance 또는 hold로 결정하세요."

MONITOR_TASK = """운용 모니터링 사이클입니다. 발행된 전략서의 실제 운용 성과, 최근 체결, 리스크 가드 상태, 현재 시장 국면을 점검하세요.
백테스트 기대와 실제 운용의 괴리가 크거나, 국면 변화로 전략을 즉시 재검토해야 한다면 escalate=true로 submit_critique를 제출하세요."""


class Desk:
    def __init__(self, broker, llm):
        self.broker, self.llm = broker, llm
        self.agents = {aid: Agent(aid, title, role, self) for aid, (title, role) in ROSTER.items()}
        self._df_all = None
        self.proposal = None
        self.context = "idle"

    # 사이클마다 한 번 시세를 받아 모든 에이전트가 같은 데이터를 본다
    def refresh(self):
        self._df_all = market.load_daily(refresh=True)

    def df_all(self):
        if self._df_all is None:
            self.refresh()
        return self._df_all

    def df(self):
        return market.completed(self.df_all())

    # ------------------------------------------------------------------ 리서치 회의
    def research(self) -> dict | None:
        self.refresh()
        self.context = "research"
        self._phase("1단계: 애널리스트 리서치 (agent_4~8 병렬)")
        with ThreadPoolExecutor(max_workers=5) as ex:
            futs = {ex.submit(self.agents[a].run, ANALYST_TASK): a for a in ANALYST_OF}
            for f in as_completed(futs):
                a = futs[f]
                try:
                    r = f.result()
                    s = r["submitted"]
                    if s:
                        print(f"  ✅ {a} 보고: 추천 비중 {s['recommended_weight']}, 확신 {s['confidence']}, "
                              f"OOS 샤프 {s['verified_backtest']['out_of_sample'].get('sharpe')}")
                    else:
                        print(f"  ⚠️  {a} 보고서 미제출: {r['text'][:200]}")
                except Exception as e:
                    print(f"  ❌ {a} 오류: {e}")
                    traceback.print_exc()

        self._phase("2단계: CSO 전략서 초안")
        draft = self.agents["agent_2"].run(CSO_TASK)["submitted"]
        if not draft:
            print("  ❌ CSO가 전략서를 제출하지 못했습니다. 기존 전략서 유지.")
            self._phase("회의 종료: CSO 미제출 — 기존 전략서 유지")
            self.context = "idle"
            return None
        self._print_book(draft)

        critique = None
        for r in range(1, config.DEBATE_ROUNDS + 1):
            self._phase(f"3단계: 레드팀 검토 (라운드 {r}/{config.DEBATE_ROUNDS})")
            critique = self.agents["agent_3"].run(REDTEAM_TASK.format(r=r, n=config.DEBATE_ROUNDS))["submitted"]
            if not critique:
                print("  ⚠️  레드팀 의견 미제출")
                break
            self._print_critique(critique)
            if critique["verdict"] == "approve" or r == config.DEBATE_ROUNDS:
                break
            self._phase(f"CSO 수정안 (라운드 {r})")
            crit_text = json.dumps({k: critique[k] for k in ("verdict", "summary", "issues")}, ensure_ascii=False, indent=1)
            revised = self.agents["agent_2"].run(CSO_REVISE_TASK.format(critique=crit_text))["submitted"]
            if revised:
                draft = revised
                self._print_book(draft)

        # 레드팀이 최종 라운드에서도 reject하면 CSO 결정은 존중하되 최대 비중을 절반으로 줄인다
        if critique and critique["verdict"] == "reject":
            draft["max_exposure"] = round(draft["max_exposure"] / 2, 4)
            draft.setdefault("validation_warnings", []).append("레드팀 최종 reject → max_exposure 50% 감축")
            print(f"  🛡️  레드팀 최종 reject → max_exposure {draft['max_exposure']}로 감축")
        draft["red_team_final"] = {k: critique[k] for k in ("verdict", "summary")} if critique else None
        book = store.publish_book(draft)
        store.bus.send("agent_2", "all", f"📘 전략서 v{book['version']} 발행",
                       f"sleeves={[(s['strategy'], s['weight']) for s in book['sleeves']]}, "
                       f"max_exposure={book['max_exposure']}, kill_drawdown={book['kill_drawdown']}\n{book['rationale'][:500]}")
        self._phase(f"회의 종료: 전략서 v{book['version']} 발행")
        self.context = "idle"
        return book

    # ------------------------------------------------------------------ 매매 사이클
    def trade(self, use_llm: bool = True) -> dict:
        self.refresh()
        self._phase("매매 사이클")
        self.context = "trade"
        self.proposal = engine.compute_proposal(self.broker)
        p = self.proposal
        if "error" in p:
            print(f"  ⚠️  {p['error']}")
            return p
        print(f"  제안: 목표 {p['target_exposure']:.0%} / 현재 {p['current_exposure']:.0%} "
              f"(자산 {p['account']['equity']:,.2f} {config.QUOTE}, BTC {p['account']['price']:,.2f})")
        # 킬스위치는 LLM 판단을 거치지 않고 즉시 집행
        if p["guard"]["halted"] and p["current_exposure"] > 0.001:
            rec = engine.execute(self.broker, p, 0.0, "킬스위치 강제 청산", actor="risk_guard")
            print(f"  🚨 킬스위치 강제 청산: {rec['result']}")
            return rec
        if not use_llm:
            if p["action_needed"]:
                rec = engine.execute(self.broker, p, p["target_exposure"], "결정론적 집행(--no-llm)", actor="engine")
            else:
                rec = {"result": "hold", "reason": "리밸런싱 밴드 이내"}
                engine.log_equity(p["account"])
            print(f"  → {rec['result']}")
            return rec
        r = self.agents["agent_1"].run(TRADER_TASK + "\n\n(참고: 방금 계산된 제안)\n" + json.dumps(p, ensure_ascii=False, default=str))
        rec = r["submitted"] or {"result": "no-decision", "reason": r["text"][:300]}
        if not r["submitted"]:
            engine.log_equity(p["account"])
        print(f"  → agent_1 결정: {rec.get('result')} — {rec.get('reason', '')[:200]}")
        self.context = "idle"
        return rec

    # ------------------------------------------------------------------ 레드팀 상시 모니터링
    def monitor(self) -> dict | None:
        self.refresh()
        self._phase("레드팀 운용 모니터링")
        self.context = "monitor"
        c = self.agents["agent_3"].run(MONITOR_TASK)["submitted"]
        if c:
            self._print_critique(c)
            if c.get("escalate"):
                store.bus.send("agent_3", "agent_2", "🚩 긴급 재검토 요청", c["summary"])
        self.context = "idle"
        return c

    # ------------------------------------------------------------------ 상시 운용 루프
    def run_forever(self, trade_minutes: int = 60, research_hours: float = 24, use_llm_trader: bool = True,
                    stop: threading.Event | None = None, lock: threading.Lock | None = None):
        """stop이 set되면 종료. lock은 대시보드의 수동 실행과 사이클이 겹치지 않게 한다."""
        stop = stop or threading.Event()
        lock = lock or threading.Lock()
        last_monitor = datetime.now(timezone.utc)
        self._phase(f"자동 운용 시작 — {self.broker.name}, 매매 {trade_minutes}분 / 리서치 {research_hours}시간 주기")
        while not stop.is_set():
            lock.acquire()
            try:
                now = datetime.now(timezone.utc)
                book = store.active_book()
                stale = not book or now - datetime.fromisoformat(book["published_at"]) > timedelta(hours=research_hours)
                if stale:
                    self.research()
                print(f"\n[{store.now()}] 매매 사이클")
                self.trade(use_llm=use_llm_trader)
                if now - last_monitor > timedelta(hours=research_hours / 2):
                    print(f"\n[{store.now()}] 레드팀 모니터링")
                    c = self.monitor()
                    last_monitor = now
                    if c and c.get("escalate"):
                        print("  🚩 레드팀 escalate → 긴급 리서치 회의")
                        self.research()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"  ❌ 사이클 오류: {e}")
                traceback.print_exc()
                store.append_jsonl("errors.jsonl", {"ts": store.now(), "error": repr(e)})
                store.feed("error", "desk", "사이클 오류", repr(e))
            finally:
                lock.release()
            stop.wait(trade_minutes * 60)
        self._phase("자동 운용 중지")

    # ------------------------------------------------------------------ 출력
    @staticmethod
    def _phase(title: str):
        print(f"\n=== {title} ===")
        store.feed("phase", "desk", title)

    @staticmethod
    def _print_book(b: dict):
        print("  전략서 초안:")
        for s in b["sleeves"]:
            print(f"    - {s['strategy']:<18} w={s['weight']:.2f} {s['params']}")
        v = b.get("verified_backtest", {})
        print(f"    max_exposure={b['max_exposure']} kill_dd={b['kill_drawdown']} band={b['rebalance_band']}")
        if v:
            print(f"    백테스트 샤프 IS {v['in_sample'].get('sharpe')} / OOS {v['out_of_sample'].get('sharpe')}, "
                  f"MDD {v['full'].get('max_drawdown')}")
        for w in b.get("validation_warnings", []):
            print(f"    ⚠️  {w}")

    @staticmethod
    def _print_critique(c: dict):
        print(f"  레드팀 판정: {c['verdict'].upper()} — {c['summary'][:300]}")
        for i in c.get("issues", [])[:8]:
            print(f"    [{i['severity']}] {i['issue'][:160]}")
