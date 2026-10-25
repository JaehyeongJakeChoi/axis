"""8명의 에이전트: 역할, 시스템 프롬프트, 도구 정의, 도구 실행기."""
import json
from dataclasses import dataclass

from . import backtest, config, engine, market, store
from .llm import Done
from .strategies import STRATEGIES, get as get_strategy

ANALYST_OF = {s.owner: s.name for s in STRATEGIES.values()}  # agent_4 -> trend_ma ...

ROSTER = {
    "agent_1": ("트레이더 (Prop Trader)", "trader"),
    "agent_2": ("CSO / 최고전략책임자", "cso"),
    "agent_3": ("레드팀 (Red Team)", "redteam"),
    **{owner: (f"애널리스트 — {STRATEGIES[s].title}", "analyst") for owner, s in ANALYST_OF.items()},
}


def _roster_text() -> str:
    lines = []
    for aid, (title, _) in ROSTER.items():
        extra = f" (전략: {ANALYST_OF[aid]})" if aid in ANALYST_OF else ""
        lines.append(f"- {aid}: {title}{extra}")
    return "\n".join(lines)


COMMON = f"""당신은 {config.SYMBOL} 바이낸스 현물(롱온리, 레버리지 없음) 퀀트 데스크의 일원입니다.

데스크 구성:
{_roster_text()}

데스크 운영 방식: 애널리스트 5명이 각자 전략을 백테스트해 agent_2(CSO)에게 보고하고, CSO는 한 사람의 의견에 기대지 않고 다섯 보고를 절충해 전략서(StrategyBook)를 만듭니다. agent_3(레드팀)이 전략서를 비판적으로 검토하고, CSO가 수정합니다. agent_1(트레이더)은 발행된 전략서에 따라 결정론적 엔진이 계산한 목표 비중을 검토하고 집행합니다.

공통 원칙:
- 성과 숫자는 반드시 도구(백테스트 등)가 반환한 값만 인용합니다. 기억이나 추정으로 숫자를 만들지 마세요.
- 백테스트는 마지막 30%가 아웃오브샘플(OOS)입니다. 인샘플(IS)만 좋은 결과는 과최적화로 의심하세요.
- 비용(수수료 {config.FEE_RATE:.2%} + 슬리피지 {config.SLIPPAGE:.2%}, 편도)은 이미 반영돼 있습니다. 비교 기준은 같은 기간 바이앤홀드입니다.
- 다른 에이전트와 대화할 수 있습니다: ask_agent는 즉시 답을 받는 질문, send_message는 비동기 메모(상대가 나중에 read_inbox로 읽음). 질문은 꼭 필요할 때만, 구체적으로 하세요.
- 작업 시작 시 read_inbox로 받은 메시지를 확인하세요.
- 모든 출력은 한국어로 씁니다."""

ROLE_PROMPTS = {
    "analyst": """역할: 애널리스트. 담당 전략 패밀리: {strategy} — {desc}
파라미터 범위: {params}

할 일: 담당 전략의 현재 최선의 설정을 찾고, 이 전략을 지금 얼마나 신뢰해야 하는지 정직하게 평가해 submit_report로 CSO에게 보고합니다.
- param_sweep으로 탐색하되 IS 샤프 1등을 그대로 고르지 마세요. OOS 성과, 파라미터 이웃의 안정성(robustness_check), 비용 2배 스트레스, 연도별 편차를 보고 '넓은 고원'에 있는 설정을 고르세요.
- 담당 전략이 바이앤홀드보다 못하거나 불안정하다면, 낮은 recommended_weight를 내는 것이 정직한 보고입니다. 자기 전략을 과장하지 마세요.
- 다른 전략과의 상관/보완 관계가 궁금하면 해당 애널리스트에게 물어볼 수 있습니다.""",
    "cso": """역할: CSO(최고전략책임자). 데스크의 매매 전략서(StrategyBook)를 결정합니다.

할 일: 애널리스트 5명의 보고(read_reports)를 종합하고 절충해 전략서를 submit_strategy_book으로 제출합니다.
- 한 애널리스트의 주장에 끌려가지 마세요. 각 보고의 근거(OOS, 강건성)를 비교하고, portfolio_backtest로 조합을 직접 검증하세요. 슬리브 간 상관이 높으면 분산 효과는 착시입니다.
- 전략서 구성: sleeves(전략, 파라미터, 가중치; 합 ≤ 1), max_exposure(최대 BTC 비중 ≤ 1), kill_drawdown(고점 대비 이 낙폭이면 전량 현금화; ≤ 0.30), rebalance_band(이 이상 비중 차이가 날 때만 매매).
- 레드팀 비판이 있으면 각 지적에 대해 수용(수정) 또는 증거 기반 반박을 response_to_red_team에 적으세요.
- 애널리스트에게 보고 내용을 추가로 질문해도 됩니다.""",
    "redteam": """역할: 레드팀. 목표는 전략서와 운용의 약점을 찾아내 손실을 막는 것입니다.

- 승인을 기본값으로 두지 마세요. 하지만 근거 없는 반대도 금지입니다 — 모든 지적에는 도구로 얻은 증거를 붙이세요.
- 점검 항목: 과최적화(IS-OOS 격차, 이웃 파라미터 민감도), 비용 2배 스트레스, 특정 연도(예: 2017, 2020-21 강세장) 의존도, 슬리브 간 상관(분산 착시), 최대낙폭 대비 킬스위치 설정, 현재 시장 국면과의 적합성, 바이앤홀드 대비 실질 우위.
- verdict: approve(중대한 문제 없음) / revise(고쳐야 할 것 있음) / reject(이대로 채택하면 위험).
- 필요하면 애널리스트나 CSO에게 직접 질문하세요. 최종 의견은 submit_critique로 제출합니다.""",
    "trader": """역할: 프랍 트레이더. 실제 주문을 집행합니다.

할 일: get_trade_proposal로 결정론적 엔진이 계산한 목표 비중을 확인하고 execute_rebalance 또는 hold로 결정합니다.
- 당신은 제안된 목표 비중 이하(0 ~ 제안값)로만 집행할 수 있습니다. 비중을 늘리는 재량은 없습니다.
- 기본값은 제안대로 집행입니다. 제안을 줄이거나 보류하려면 구체적 이유(데이터 이상, 가드 경고, 비정상적 급변동 등)가 있어야 합니다.
- action_needed가 false면 보통 hold가 맞습니다(리밸런싱 밴드 이내).
- 제안이 이상해 보이면 agent_2(CSO)에게 ask_agent로 확인하세요.""",
}

CONSULT_SUFFIX = """

[상담 모드] 다른 에이전트가 당신에게 질문했습니다. 읽기 전용 도구로 필요한 확인만 하고, 질문에 간결하고 구체적으로 답하세요(최종 답변은 텍스트로). 제출/주문 도구는 이 모드에서 쓸 수 없습니다."""


# ------------------------------------------------------------------ 도구 정의
def T(name, desc, props=None, required=None):
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": props or {}, "required": required or []}}


AGENT_IDS = list(ROSTER)
STRAT_NAMES = list(STRATEGIES)
SLEEVES_SCHEMA = {"type": "array", "items": {"type": "object", "properties": {
    "strategy": {"type": "string", "enum": STRAT_NAMES},
    "params": {"type": "object", "description": "전략 파라미터. 생략한 키는 기본값."},
    "weight": {"type": "number", "minimum": 0, "maximum": 1}}, "required": ["strategy", "weight"]}}

TOOLS = {
    "send_message": T("send_message", "다른 에이전트(또는 all)에게 비동기 메모를 보냅니다.", {
        "to": {"type": "string", "enum": AGENT_IDS + ["all"]}, "subject": {"type": "string"},
        "body": {"type": "string"}}, ["to", "subject", "body"]),
    "read_inbox": T("read_inbox", "나에게 온 읽지 않은 메시지를 읽습니다."),
    "ask_agent": T("ask_agent", "다른 에이전트에게 질문하고 즉시 답을 받습니다. 상대는 자기 도구로 확인 후 답합니다. 비용이 드니 꼭 필요할 때만.", {
        "to": {"type": "string", "enum": AGENT_IDS}, "question": {"type": "string"}}, ["to", "question"]),
    "market_snapshot": T("market_snapshot", "현재가, 기간 수익률, 실현변동성, 이평선 대비 위치, ATH 대비 낙폭."),
    "list_strategies": T("list_strategies", "5개 전략 패밀리의 설명, 담당자, 파라미터 범위와 기본값."),
    "run_backtest": T("run_backtest", "단일 전략 백테스트: IS/OOS/전체 성과, 바이앤홀드 비교, 연도별 수익률.", {
        "strategy": {"type": "string", "enum": STRAT_NAMES}, "params": {"type": "object"},
        "cost_multiplier": {"type": "number", "description": "비용 배수 (스트레스 테스트용, 기본 1)"}}, ["strategy"]),
    "param_sweep": T("param_sweep", "파라미터 그리드 탐색(최대 120조합). IS 샤프 상위 10개와 각각의 OOS 성과, IS-OOS 샤프 상관을 반환.", {
        "strategy": {"type": "string", "enum": STRAT_NAMES},
        "grid": {"type": "object", "description": "예: {\"fast\": [10,20,30], \"slow\": [50,100,200]}"}}, ["strategy", "grid"]),
    "robustness_check": T("robustness_check", "강건성 점검: 각 파라미터 ±20% 이웃 성과, 비용 2배 스트레스, 연도별 수익률/손실 연도.", {
        "strategy": {"type": "string", "enum": STRAT_NAMES}, "params": {"type": "object"}}, ["strategy"]),
    "portfolio_backtest": T("portfolio_backtest", "여러 전략 슬리브 조합 백테스트 + 슬리브 간 수익률 상관행렬.", {
        "sleeves": SLEEVES_SCHEMA, "max_exposure": {"type": "number"}}, ["sleeves"]),
    "read_reports": T("read_reports", "애널리스트들의 최신 보고서(자동 첨부된 백테스트 수치 포함)."),
    "read_strategy_book": T("read_strategy_book", "현재 발행된 전략서와 검토 중인 초안."),
    "read_critiques": T("read_critiques", "레드팀의 최근 검토 의견들."),
    "read_performance": T("read_performance", "실제(또는 모의) 계좌 성과, 최근 체결, 리스크 가드 상태."),
    "submit_report": T("submit_report", "CSO에게 최종 보고서 제출(세션 종료). 백테스트 수치는 시스템이 자동 첨부합니다.", {
        "params": {"type": "object", "description": "추천 파라미터"},
        "summary": {"type": "string", "description": "핵심 결론과 근거"},
        "recommended_weight": {"type": "number", "minimum": 0, "maximum": 1, "description": "포트폴리오에서 이 전략에 줄 비중 의견"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "regime_view": {"type": "string", "description": "현재 시장 국면에서 이 전략의 적합성"},
        "risks": {"type": "array", "items": {"type": "string"}}},
        ["params", "summary", "recommended_weight", "confidence", "regime_view", "risks"]),
    "submit_strategy_book": T("submit_strategy_book", "전략서 제출(세션 종료). 하드 리밋으로 검증되고 포트폴리오 백테스트가 자동 첨부됩니다.", {
        "sleeves": SLEEVES_SCHEMA,
        "max_exposure": {"type": "number", "minimum": 0, "maximum": 1},
        "kill_drawdown": {"type": "number", "minimum": 0.05, "maximum": 0.3},
        "rebalance_band": {"type": "number", "minimum": 0.02, "maximum": 0.25},
        "rationale": {"type": "string"},
        "response_to_red_team": {"type": "string"}},
        ["sleeves", "max_exposure", "kill_drawdown", "rebalance_band", "rationale"]),
    "submit_critique": T("submit_critique", "검토 의견 제출(세션 종료).", {
        "verdict": {"type": "string", "enum": ["approve", "revise", "reject"]},
        "summary": {"type": "string"},
        "issues": {"type": "array", "items": {"type": "object", "properties": {
            "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
            "issue": {"type": "string"}, "evidence": {"type": "string"}, "suggestion": {"type": "string"}},
            "required": ["severity", "issue", "evidence"]}},
        "escalate": {"type": "boolean", "description": "(운용 모니터링 시) 즉시 전략 재검토 회의가 필요하면 true"}},
        ["verdict", "summary", "issues"]),
    "get_trade_proposal": T("get_trade_proposal", "엔진이 계산한 목표 비중, 슬리브별 신호, 계좌/가드 상태."),
    "execute_rebalance": T("execute_rebalance", "목표 비중으로 리밸런싱 주문(세션 종료). 0 ~ 제안값으로 강제 클램프.", {
        "target_exposure": {"type": "number", "minimum": 0, "maximum": 1}, "reason": {"type": "string"}},
        ["target_exposure", "reason"]),
    "hold": T("hold", "이번 사이클은 주문 없이 보류(세션 종료).", {"reason": {"type": "string"}}, ["reason"]),
}

COMMON_TOOLS = ["read_inbox", "send_message", "ask_agent", "market_snapshot", "list_strategies", "run_backtest"]
ROLE_TOOLS = {
    "analyst": COMMON_TOOLS + ["param_sweep", "robustness_check", "read_reports", "submit_report"],
    "cso": COMMON_TOOLS + ["read_reports", "read_critiques", "read_strategy_book", "read_performance",
                           "portfolio_backtest", "robustness_check", "submit_strategy_book"],
    "redteam": COMMON_TOOLS + ["read_reports", "read_critiques", "read_strategy_book", "read_performance",
                               "param_sweep", "robustness_check", "portfolio_backtest", "submit_critique"],
    "trader": ["read_inbox", "send_message", "ask_agent", "market_snapshot", "read_strategy_book",
               "read_performance", "get_trade_proposal", "execute_rebalance", "hold"],
}
WRITE_TOOLS = {"submit_report", "submit_strategy_book", "submit_critique", "execute_rebalance", "hold", "ask_agent", "send_message"}


# ------------------------------------------------------------------ 에이전트
def ask(desk, frm: str, to: str, question: str, who: str) -> str:
    """frm이 to에게 즉답 질문. to는 상담 모드(읽기 전용 도구)로 답한다. 사용자도 frm='user'로 쓴다."""
    if frm == to:
        raise ValueError("자기 자신에게는 물을 수 없습니다.")
    print(f"  💬 {frm} → {to}: {question[:80]}")
    store.feed("question", frm, "", question, to=to)
    ans = desk.agents[to].run(f"{who}의 질문:\n{question}", consult=True)["text"] or "(답변 없음)"
    store.feed("answer", to, "", ans, to=frm)
    store.append_jsonl("conversations.jsonl", {"ts": store.now(), "from": frm, "to": to,
                                               "question": question, "answer": ans})
    return ans


@dataclass
class Agent:
    id: str
    title: str
    role: str
    desk: "object"

    @property
    def strategy(self):
        return ANALYST_OF.get(self.id)

    def system_prompt(self, consult: bool = False) -> str:
        role = ROLE_PROMPTS[self.role]
        if self.strategy:
            s = get_strategy(self.strategy)
            role = role.format(strategy=s.name, desc=s.description, params=s.describe()["params"])
        head = f"당신은 {self.id}, {self.title}입니다.\n\n"
        return head + COMMON + "\n\n" + role + (CONSULT_SUFFIX if consult else "")

    def tools(self, consult: bool = False) -> list:
        names = ROLE_TOOLS[self.role]
        if consult:
            names = [n for n in names if n not in WRITE_TOOLS]
        return [TOOLS[n] for n in names]

    def run(self, task: str, consult: bool = False) -> dict:
        label = f"{self.id}{':consult' if consult else ''}"
        store.feed("session_start", self.id, task.splitlines()[0][:120], data={"consult": consult})
        try:
            r = self.desk.llm.run(
                role=self.role, label=label, system=self.system_prompt(consult), user=task,
                tools=self.tools(consult), handler=lambda name, inp: self._logged(name, inp, consult))
        except BaseException as e:
            store.feed("session_end", self.id, "오류", repr(e), data={"consult": consult, "error": True})
            raise
        store.feed("session_end", self.id, "제출 완료" if r["submitted"] else "종료", r["text"][:3000],
                   data={"consult": consult})
        return r

    def _logged(self, name: str, inp: dict, consult: bool):
        if name not in ("ask_agent", "send_message"):  # 이 둘은 자체적으로 대화 이벤트를 남긴다
            brief = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)[:60]}" for k, v in inp.items()
                              if k not in ("summary", "rationale", "issues", "risks", "reason", "response_to_red_team"))
            store.feed("tool", self.id, name, brief)
        return self.handle(name, inp, consult)

    # -------------------------------------------------------------- 도구 실행
    def handle(self, name: str, inp: dict, consult: bool):
        d = self.desk
        allowed = {t["name"] for t in self.tools(consult)}
        if name not in allowed:
            raise PermissionError(f"{self.id}는 {name} 도구를 쓸 수 없습니다.")

        if name == "read_inbox":
            return store.bus.inbox(self.id) or "새 메시지 없음"
        if name == "send_message":
            return store.bus.send(self.id, inp["to"], inp["subject"], inp["body"])
        if name == "ask_agent":
            to = inp["to"]
            if to == self.id:
                raise ValueError("자기 자신에게는 물을 수 없습니다.")
            return {"from": to, "answer": ask(d, self.id, to, inp["question"], f"{self.id}({self.title})")}
        if name == "market_snapshot":
            return market.snapshot(d.df_all(), market.price())
        if name == "list_strategies":
            return [s.describe() for s in STRATEGIES.values()]
        if name == "run_backtest":
            return backtest.run_backtest(inp["strategy"], inp.get("params"), d.df(), float(inp.get("cost_multiplier", 1.0)))
        if name == "param_sweep":
            return backtest.param_sweep(inp["strategy"], inp["grid"], d.df())
        if name == "robustness_check":
            return backtest.robustness_check(inp["strategy"], inp.get("params"), d.df())
        if name == "portfolio_backtest":
            r = backtest.portfolio_backtest(inp["sleeves"], d.df(), float(inp.get("max_exposure", 1.0)))
            r.pop("sleeves", None)
            return r
        if name == "read_reports":
            return store.read_json("reports.json", {}) or "아직 보고서 없음"
        if name == "read_strategy_book":
            return {"active": store.active_book(), "draft": store.read_json("book_draft.json")}
        if name == "read_critiques":
            return store.read_jsonl("critiques.jsonl", tail=3) or "검토 의견 없음"
        if name == "read_performance":
            return engine.performance()

        if name == "submit_report":
            strat = self.strategy
            params = get_strategy(strat).validate(inp["params"])
            bt = backtest.run_backtest(strat, params, d.df())
            rb = backtest.robustness_check(strat, params, d.df())
            report = {**inp, "agent": self.id, "strategy": strat, "params": params, "ts": store.now(),
                      "verified_backtest": {k: bt[k] for k in ("in_sample", "out_of_sample", "full", "buy_and_hold")},
                      "verified_robustness": {k: rb[k] for k in ("neighbor_sharpe_min", "neighbor_sharpe_max",
                                                                 "double_cost_full_sharpe", "negative_years")},
                      "current_signal": get_strategy(strat).target(d.df_all(), params)}
            with store._lock:
                reports = store.read_json("reports.json", {})
                reports[self.id] = report
                store.write_json("reports.json", reports)
            store.append_jsonl("reports_history.jsonl", report)
            store.feed("report", self.id, f"{strat} 보고서", inp["summary"], to="agent_2", data={
                "strategy": strat, "params": params, "recommended_weight": inp["recommended_weight"],
                "confidence": inp["confidence"], "regime_view": inp["regime_view"], "risks": inp["risks"],
                "is_sharpe": bt["in_sample"].get("sharpe"), "oos_sharpe": bt["out_of_sample"].get("sharpe"),
                "oos_cagr": bt["out_of_sample"].get("cagr"), "mdd": bt["full"].get("max_drawdown"),
                "bh_oos_sharpe": bt["buy_and_hold"]["out_of_sample"].get("sharpe"),
                "neighbor_sharpe_min": rb["neighbor_sharpe_min"], "signal": report["current_signal"]})
            raise Done(report)
        if name == "submit_strategy_book":
            book, warnings = store.validate_book(inp)
            pf = backtest.portfolio_backtest(book["sleeves"], d.df(), book["max_exposure"])
            book["verified_backtest"] = {k: pf[k] for k in ("in_sample", "out_of_sample", "full", "buy_and_hold")}
            book["validation_warnings"] = warnings
            book["drafted_at"] = store.now()
            store.write_json("book_draft.json", book)
            store.feed("book_draft", self.id, "전략서 초안 제출", book["rationale"], to="agent_3",
                       data=dict(store.book_card(book), response_to_red_team=book["response_to_red_team"]))
            raise Done(book)
        if name == "submit_critique":
            c = {**inp, "agent": self.id, "ts": store.now(), "context": d.context}
            store.append_jsonl("critiques.jsonl", c)
            store.feed("critique", self.id, f"검토 의견: {inp['verdict']}", inp["summary"], to="agent_2",
                       data={"verdict": inp["verdict"], "issues": inp.get("issues", []),
                             "escalate": inp.get("escalate", False), "context": d.context})
            raise Done(c)
        if name == "get_trade_proposal":
            d.proposal = engine.compute_proposal(d.broker)
            return d.proposal
        if name in ("execute_rebalance", "hold"):
            if d.proposal is None or "error" in d.proposal:
                raise ValueError("먼저 get_trade_proposal을 호출하세요.")
            if name == "hold":
                rec = {"ts": store.now(), "actor": self.id, "result": "hold", "reason": inp["reason"],
                       "proposal_target": d.proposal["target_exposure"], "exposure": d.proposal["current_exposure"]}
                store.append_jsonl("decisions.jsonl", rec)
                store.feed("trade", self.id, "보류", inp["reason"], data=rec)
                engine.log_equity(d.proposal["account"])
                raise Done(rec)
            rec = engine.execute(d.broker, d.proposal, float(inp["target_exposure"]), inp["reason"], self.id)
            store.append_jsonl("decisions.jsonl", {k: rec[k] for k in ("ts", "actor", "result", "reason", "applied_target")})
            raise Done(rec)
        raise ValueError(f"알 수 없는 도구 {name}")
