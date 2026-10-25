"""대시보드 서버: http://127.0.0.1:8765

- GET  /                  대시보드
- GET  /api/state         에이전트 상태, 전략서, 보고서, 계좌, 가드, 비용, 작업 상태
- GET  /api/feed?offset=N 회의실 피드(증분)
- GET  /api/charts        전략서 백테스트 곡선 + 실운용 자산 곡선
- POST /api/run           {"job": research|trade|trade_nollm|monitor, "mock": bool}
- POST /api/ask           {"agent", "question", "mock"}   에이전트에게 즉답 질문
- POST /api/message       {"to", "body"}                   에이전트 메시지함에 메모
- POST /api/autopilot     {"on": bool, "mock", "trade_minutes", "research_hours", "llm_trader"}

주문을 낼 수 있는 서버이므로 127.0.0.1에만 바인딩하고, POST는 커스텀 헤더(X-Coin-Desk)와
Host 검사로 다른 웹사이트발 요청(CSRF, DNS 리바인딩)을 막는다.
"""
import json
import os
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd

from . import backtest, config, engine, market, store
from .agents import ANALYST_OF, ROSTER, ask
from .broker import make_broker
from .strategies import STRATEGIES

STATIC = Path(__file__).parent / "static"


# ------------------------------------------------------------------ 에이전트 상태 (피드를 증분 스캔)
class AgentStatus:
    def __init__(self):
        self.offset = 0
        self.lock = threading.Lock()
        self.s = {aid: {"depth": 0, "consult": False, "action": "", "ts": None} for aid in ROSTER}

    def update(self):
        with self.lock:
            rows, self.offset = store.read_feed(self.offset, max_rows=10**9)
            for r in rows:
                st = self.s.get(r["from"])
                if not st:
                    continue
                t = r["type"]
                st["ts"] = r["ts"]
                if t == "session_start":
                    st["depth"] += 1
                    st["consult"] = bool((r.get("data") or {}).get("consult"))
                    st["action"] = "질문에 답하는 중" if st["consult"] else r["title"]
                elif t == "session_end":
                    st["depth"] = max(0, st["depth"] - 1)
                    st["action"] = r["title"]
                elif t == "tool":
                    st["action"] = f"🔧 {r['title']}"
                elif t == "note":
                    st["action"] = r["body"][:140]
                elif t == "question":
                    st["action"] = f"💬 {r['to']}에게 질문 중"
                elif t in ("report", "critique", "book_draft"):
                    st["action"] = r["title"]

    def snapshot(self) -> list:
        self.update()
        now = datetime.now(timezone.utc)
        out = []
        for aid, (title, role) in ROSTER.items():
            st = self.s[aid]
            working = st["depth"] > 0
            stale = working and st["ts"] and (now - datetime.fromisoformat(st["ts"])).total_seconds() > 900
            out.append({"id": aid, "title": title, "role": role, "strategy": ANALYST_OF.get(aid),
                        "state": "stale" if stale else ("working" if working else "idle"),
                        "action": st["action"], "ts": st["ts"]})
        return out


# ------------------------------------------------------------------ 작업 실행기
class Runner:
    def __init__(self, live_flag: bool):
        self.live_flag = live_flag
        self.cycle_lock = threading.Lock()  # 리서치/매매/모니터링은 한 번에 하나
        self.job = None
        self.last = None
        self.auto = None

    def _desk(self, mock: bool):
        from .desk import Desk
        if mock:
            from .mock import MockLLM
            llm = MockLLM()
        else:
            from .llm import LLM
            llm = LLM()
        return Desk(make_broker(self.live_flag), llm)

    def _report_error(self, name: str, e: BaseException):
        msg = str(e) if isinstance(e, SystemExit) else repr(e)
        self.last = {"name": name, "finished": store.now(), "ok": False, "error": msg}
        store.feed("error", "desk", f"{name} 실패", msg)

    def run(self, name: str, mock: bool) -> dict:
        jobs = {"research": lambda d: d.research(), "trade": lambda d: d.trade(use_llm=True),
                "trade_nollm": lambda d: d.trade(use_llm=False), "monitor": lambda d: d.monitor()}
        if name not in jobs:
            return {"error": f"알 수 없는 작업 {name}"}
        if not self.cycle_lock.acquire(blocking=False):
            return {"error": "다른 사이클이 실행 중입니다 (자동 운용 포함)."}
        self.job = {"name": name, "mock": mock, "started": store.now()}

        def work():
            try:
                jobs[name](self._desk(mock))
                self.last = {"name": name, "finished": store.now(), "ok": True}
            except BaseException as e:  # LLM()의 SystemExit(키 없음)도 화면에 보여준다
                traceback.print_exc()
                self._report_error(name, e)
            finally:
                self.job = None
                self.cycle_lock.release()

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def ask(self, agent: str, question: str, mock: bool) -> dict:
        if agent not in ROSTER:
            return {"error": "알 수 없는 에이전트"}

        def work():
            try:
                ask(self._desk(mock), "user", agent, question, "데스크 운영자(사용자)")
            except BaseException as e:
                traceback.print_exc()
                self._report_error(f"{agent} 질문", e)

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def autopilot(self, on: bool, mock: bool, trade_minutes: int, research_hours: float, llm_trader: bool) -> dict:
        if on:
            if self.auto:
                return {"error": "이미 자동 운용 중입니다."}
            stop = threading.Event()
            try:
                desk = self._desk(mock)
            except BaseException as e:
                self._report_error("자동 운용", e)
                return {"error": str(e)}

            def loop():
                try:
                    desk.run_forever(trade_minutes, research_hours, llm_trader, stop=stop, lock=self.cycle_lock)
                finally:
                    self.auto = None

            self.auto = {"stop": stop, "mock": mock, "trade_minutes": trade_minutes, "research_hours": research_hours,
                         "llm_trader": llm_trader, "started": store.now()}
            threading.Thread(target=loop, daemon=True).start()
        elif self.auto:
            self.auto["stop"].set()
            store.feed("phase", "desk", "자동 운용 중지 요청 — 진행 중인 사이클이 끝나면 멈춥니다")
        return {"ok": True}

    def status(self) -> dict:
        auto = {k: v for k, v in self.auto.items() if k != "stop"} if self.auto else None
        return {"job": self.job, "last": self.last, "autopilot": auto}


# ------------------------------------------------------------------ 데이터 조립
_cache = {}


def cached(key: str, ttl: float, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def state(agents: AgentStatus, runner: Runner) -> dict:
    book = store.active_book()
    draft = store.read_json("book_draft.json")
    reports = store.read_json("reports.json", {})
    critiques = store.read_jsonl("critiques.jsonl", tail=1)
    perf = engine.performance()
    from .llm import usage_summary

    def mkt():
        df = market.load_daily(refresh=True)
        return market.snapshot(df, market.price())

    try:
        snap = cached("market", 30, mkt)
    except Exception as e:
        snap = {"error": repr(e)}
    return {
        "mode": config.MODE, "symbol": config.SYMBOL, "quote": config.QUOTE,
        "has_api_key": bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")
                            or os.getenv("ANTHROPIC_PROFILE") or (Path.home() / ".config" / "anthropic").exists()),
        "models": config.MODELS,
        "runner": runner.status(),
        "agents": agents.snapshot(),
        "book": dict(store.book_card(book), version=book.get("version"), published_at=book.get("published_at"),
                     rationale=book.get("rationale"), red_team_final=book.get("red_team_final")) if book else None,
        "draft_pending": bool(draft and (not book or draft.get("drafted_at", "") > book.get("published_at", ""))),
        "reports": {aid: {"strategy": r["strategy"], "params": r["params"], "recommended_weight": r["recommended_weight"],
                          "confidence": r["confidence"], "signal": r.get("current_signal"), "ts": r["ts"],
                          "oos_sharpe": r["verified_backtest"]["out_of_sample"].get("sharpe"),
                          "is_sharpe": r["verified_backtest"]["in_sample"].get("sharpe"),
                          "mdd": r["verified_backtest"]["full"].get("max_drawdown")}
                    for aid, r in reports.items()},
        "critique": critiques[-1] if critiques else None,
        "performance": {k: v for k, v in perf.items() if k != "recent_trades"},
        "trades": perf["recent_trades"][::-1],
        "usage": usage_summary(),
        "market": snap,
        "limits": config.HARD_LIMITS,
        "strategies": {n: s.title for n, s in STRATEGIES.items()},
    }


def charts() -> dict:
    out = {"backtest": None, "equity": []}
    book = store.active_book()
    if book:
        def bt():
            df = market.completed(market.load_daily(refresh=False))
            c = backtest.portfolio_curve(book["sleeves"], df, book["max_exposure"])
            c = pd.concat([c.iloc[::7], c.iloc[[-1]]])  # 주 1회 샘플 (실제 날짜 유지)
            c = c[~c.index.duplicated()]
            return {"version": book["version"], "oos_starts": str(backtest.split_index(df.index).date()),
                    "points": [{"d": str(i.date()), "strategy": round(r.strategy, 4), "bh": round(r.buy_and_hold, 4)}
                               for i, r in c.iterrows()]}
        out["backtest"] = cached(f"bt{book['version']}", 3600, bt)
    out["equity"] = [{"ts": e["ts"], "equity": e["equity"], "price": e["price"], "exposure": e["exposure"]}
                     for e in store.read_jsonl("equity.jsonl")[-2000:]]
    return out


# ------------------------------------------------------------------ HTTP
def make_handler(agents: AgentStatus, runner: Runner, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self):
            return self.headers.get("Host") in allowed_hosts

        def do_GET(self):
            if not self._host_ok():
                return self._json({"error": "bad host"}, 403)
            u = urlparse(self.path)
            try:
                if u.path in ("/", "/index.html"):
                    body = (STATIC / "dashboard.html").read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(body)
                elif u.path == "/api/state":
                    self._json(state(agents, runner))
                elif u.path == "/api/feed":
                    off = int(parse_qs(u.query).get("offset", ["0"])[0])
                    rows, new = store.read_feed(off, max_rows=600)
                    self._json({"events": rows, "offset": new})
                elif u.path == "/api/charts":
                    self._json(charts())
                else:
                    self._json({"error": "not found"}, 404)
            except Exception as e:
                traceback.print_exc()
                self._json({"error": repr(e)}, 500)

        def do_POST(self):
            # 커스텀 헤더 + JSON 강제 → 다른 사이트의 form/fetch는 preflight에서 막힌다
            if not self._host_ok() or self.headers.get("X-Coin-Desk") != "1" \
                    or "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._json({"error": "forbidden"}, 403)
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                mock = bool(body.get("mock"))
                u = urlparse(self.path).path
                if u == "/api/run":
                    self._json(runner.run(body.get("job", ""), mock))
                elif u == "/api/ask":
                    q = (body.get("question") or "").strip()
                    self._json(runner.ask(body.get("agent", ""), q, mock) if q else {"error": "질문이 비었습니다"})
                elif u == "/api/message":
                    to, text = body.get("to", ""), (body.get("body") or "").strip()
                    if to not in list(ROSTER) + ["all"] or not text:
                        return self._json({"error": "받는 사람/내용 확인"}, 400)
                    self._json(store.bus.send("user", to, text.splitlines()[0][:60], text))
                elif u == "/api/autopilot":
                    self._json(runner.autopilot(bool(body.get("on")), mock, int(body.get("trade_minutes", 60)),
                                                float(body.get("research_hours", 24)), bool(body.get("llm_trader", True))))
                else:
                    self._json({"error": "not found"}, 404)
            except Exception as e:
                traceback.print_exc()
                self._json({"error": repr(e)}, 500)

    return H


def serve(port: int = 8765, live_flag: bool = False):
    make_broker(live_flag)  # 모드/플래그 조합이 잘못됐으면 시작 시점에 바로 실패
    agents, runner = AgentStatus(), Runner(live_flag)
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(agents, runner, port))
    print(f"📊 대시보드: http://127.0.0.1:{port}  (모드: {config.MODE}, 상태 폴더: {config.STATE_DIR})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n종료")
