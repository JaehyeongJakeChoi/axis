#!/usr/bin/env python3
"""
portfolio-desk: 업비트·키움증권 잔고와 현재가를 읽어 주는 로컬 MCP 서버.

- 읽기 전용이다. 잔고·예수금·시세·환율 조회 TR만 호출하고 주문 API는 어디에도 없다.
- API 키는 이 파일 옆의 api-keys.txt 에서만 읽는다. 키는 응답이나 로그에 절대 넣지 않는다.
- 표준 라이브러리만 쓴다 (Python 3.9+). 설치할 패키지가 없다.

실행
  python3 server.py           MCP 서버 (stdio) — Claude 앱이 띄운다
  python3 server.py --check   연결 상태만 점검 (금액은 출력하지 않는다)
  python3 server.py --backfill  업비트 과거 체결 내역을 한 번에 받아 실현 손익 장부를 채운다
  python3 server.py --signals   위험 신호 세 가지(집중·낙폭·오늘 움직임)를 JSON 으로 (퍼센트만, 금액 없음)
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(HERE, "api-keys.txt")
TOKEN_FILE = os.path.join(HERE, ".kiwoom_token.json")
LEDGER_FILE = os.path.join(HERE, "ledger.json")
ALERT_FILE = os.path.join(HERE, "alert-settings.json")   # 대시보드의 알림 설정 사본 (장중 점검 스크립트가 읽는다)
SENT_FILE = os.path.join(HERE, "alerts-sent.json")       # 이미 보낸 알림 (같은 알림을 두 번 보내지 않게)
SIGNAL_RISK_FILE = os.path.join(HERE, "signals-risk.json")  # 위험 신호용 위험 지표 요약 (비율만, 금액 없음). 장중 점검·--signals 가 다시 계산하지 않게
SIGNAL_STATE_FILE = os.path.join(HERE, "signals-state.json")  # 신호마다 마지막으로 알린 단계 (green|yellow|red, 금액 없음)
# 단계가 내려가는 판정에만 두는 완충 (기준값 근처에서 깜빡이지 않게). 단위는 각 기준과 같다. 올라가는 판정은 즉시 한다.
SIGNAL_HYSTERESIS = {"concentration": 1.0,        # 위험 기여 %p
                     "concentration_ratio": 0.05,  # 위험÷비중 배수
                     "cohesion": 0.02,             # 평균 상관
                     "drawdown_abs": 1.0,          # 고점 대비 낙폭 %p
                     "drawdown": 1.0,              # 1년 최대 낙폭 대비 %p
                     "daily_loss": 0.05,           # VaR95 배수 (초과 횟수는 정수라 완충 없음)
                     "crypto_corr": 0.02,          # 상관
                     "crypto_ratio": 0.05}         # 위험 기여가 비중의 (1 − 이 값) 배 아래로 내려가야 해제
# 위험 신호 기준 (alert-settings.json 의 signals 에서 바꾼다). 근거: [문헌] 연구·기관 자료, [설계] 이 대시보드의 운용 원칙으로 정한 값, [수학] 계산으로 정한 값
SIGNAL_DEFAULT = {"push": True,                         # 단계가 바뀔 때 폰으로
                  "concentration_yellow": 15.0,         # [설계] 한 종목의 위험 기여(%) 주의
                  "concentration_red": 25.0,            # [설계] 한 종목의 위험 기여(%) 경고
                  "concentration_ratio_yellow": 1.5,    # [설계] 위험 기여 ÷ 비중 배수 주의
                  "concentration_ratio_red": 2.0,       # [설계] 위험 기여 ÷ 비중 배수 경고
                  "concentration_ratio_min_weight": 3.0,  # [설계] 배수 판정에 넣는 최소 비중(%)
                  "cohesion_min_weight": 5.0,           # [설계] 동조화를 볼 주식의 최소 비중(%), 코인 제외
                  "cohesion_window": 60.0,              # [설계] 상관 창(거래일)
                  "cohesion_yellow": 0.35,              # [설계] 평균 상관 주의
                  "cohesion_red": 0.5,                  # [설계] 평균 상관 경고
                  "drawdown_abs_yellow": 15.0,          # [설계] 고점 대비 낙폭(%) 주의
                  "drawdown_abs_red": 25.0,             # [설계] 고점 대비 낙폭(%) 경고
                  "drawdown_yellow": 50.0,              # [설계] 1년 최대 낙폭 대비(%) 주의
                  "drawdown_red": 80.0,                 # [설계] 1년 최대 낙폭 대비(%) 경고
                  "daily_yellow": 1.5,                  # [설계] 하루 손실 ÷ VaR95 주의
                  "daily_red": 2.0,                     # [설계] 하루 손실 ÷ VaR95 경고
                  "breach_window": 20.0,                # [수학] VaR 초과를 세는 창(거래일)
                  "breach_yellow": 3.0,                 # [수학] VaR95 가 맞다면 20일 중 3회 이상 초과 확률 약 7.5% (이항분포)
                  "breach_red": 5.0,                    # [수학] 같은 조건에서 5회 이상 약 0.3%
                  "crypto_window": 60.0,                # [설계] 비트코인-S&P 500 상관 창(거래일)
                  "crypto_corr_yellow": 0.3,            # [문헌 참고] IMF(2022) 비트코인-S&P 500 상관 0.36 을 참고
                  "crypto_corr_red": 0.5}               # [설계]
SIGNAL_BASIS = {"concentration_yellow": "[설계]", "concentration_red": "[설계]", "concentration_ratio_yellow": "[설계]",
                "concentration_ratio_red": "[설계]", "concentration_ratio_min_weight": "[설계]", "cohesion_min_weight": "[설계]",
                "cohesion_window": "[설계]", "cohesion_yellow": "[설계]", "cohesion_red": "[설계]",
                "drawdown_abs_yellow": "[설계]", "drawdown_abs_red": "[설계]", "drawdown_yellow": "[설계]", "drawdown_red": "[설계]",
                "daily_yellow": "[설계]", "daily_red": "[설계]",
                "breach_window": "[수학]", "breach_yellow": "[수학] VaR95가 맞다면 20일 중 3회 이상 초과 확률 약 7.5% (이항분포)",
                "breach_red": "[수학] 같은 조건에서 5회 이상 약 0.3%",
                "crypto_window": "[설계]", "crypto_corr_yellow": "[문헌 참고] IMF(2022) 비트코인-S&P 500 상관 0.36", "crypto_corr_red": "[설계]"}
ALERT_DEFAULT = {"enabled": True, "push": True, "digest": True, "day_move_pct": 5.0, "min_weight_pct": 2.0, "impact_pct": 0.3, "total_day_pct": 3.0}
LIGHT = False   # 장중 점검(--alerts)은 잔고와 시세만 읽고 체결·입출금·배당·지수 이력은 건너뛴다   # 체결 내역과 마지막 평단가 (실현 손익 계산용)
KST = timezone(timedelta(hours=9))
NY = ZoneInfo("America/New_York")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) portfolio-desk/1.0"
TIMEOUT = 8

UPBIT = "https://api.upbit.com"
KIWOOM = "https://api.kiwoom.com"

KIWOOM_INTERVAL = 0.25                 # 요청 간 최소 간격 (공식 예제 0.2초)
KIWOOM_AUTH_CODES = {8005, 8031, 8103}  # 토큰 재발급 후 1회 재시도
KIWOOM_RATE_CODES = {1700, 1701, 1702}  # 유량 초과
KIWOOM_MAX_PAGES = 20


def log(*args):
    print("[portfolio-desk]", *args, file=sys.stderr, flush=True)


class SourceError(Exception):
    pass


# ─────────────────────────────────────────
# 설정 (api-keys.txt)
# ─────────────────────────────────────────

_env = {"mtime": None, "values": {}}


def env(key):
    """api-keys.txt 값을 읽는다. 파일이 바뀌면 다시 읽으므로 키를 넣은 뒤 재시작할 필요가 없다."""
    try:
        mtime = os.path.getmtime(ENV_FILE)
    except OSError:
        mtime = None
    if mtime != _env["mtime"]:
        values = {}
        if mtime is not None:
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip().strip('"').strip("'")
        _env["mtime"], _env["values"] = mtime, values
    return _env["values"].get(key) or os.environ.get(key) or ""


# ─────────────────────────────────────────
# HTTP, 캐시, 숫자 파싱
# ─────────────────────────────────────────

def http(method, url, headers=None, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            raw, status, rh = res.read(), res.status, res.headers
    except urllib.error.HTTPError as e:
        raw, status, rh = e.read(), e.code, e.headers
    except (urllib.error.URLError, OSError) as e:
        raise SourceError("네트워크 오류: %s" % getattr(e, "reason", e))
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        parsed = None
    return status, parsed, rh


_cache = {}


def cached(key, ttl, fn):
    hit = _cache.get(key)
    now = time.time()
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = fn()
    _cache[key] = (now, value)
    return value


_qty_seen = {}


def qty_changed(name, holdings):
    """보유 수량이 지난 조회와 달라졌으면 True (첫 조회는 False). 체결이 났다는 신호로 쓴다."""
    sig = tuple(sorted((h["id"], round(h["qty"], 8)) for h in holdings))
    prev, _qty_seen[name] = _qty_seen.get(name), sig
    return prev is not None and prev != sig


def num(value, default=0.0):
    """'000000012550', '-00000196888', '+201.4700', '1,507.70' 같은 문자열 → float"""
    s = str(value if value is not None else "").strip().replace(",", "").replace("%", "")
    if s in ("", "+", "-", "."):
        return default
    try:
        return float(s)
    except ValueError:
        return default


def price_of(value):
    """키움 가격 필드의 부호는 전일 대비 방향일 뿐이라 절댓값을 쓴다."""
    return abs(num(value))


def first_text(row, keys):
    for k in keys:
        v = str(row.get(k) or "").strip()
        if v:
            return v
    return ""


# ─────────────────────────────────────────
# 업비트
# ─────────────────────────────────────────

def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def upbit_jwt(access_key, secret_key, query=""):
    header = _b64url(json.dumps({"alg": "HS512", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = {"access_key": access_key, "nonce": str(uuid.uuid4())}
    if query:   # 파라미터가 있는 요청은 쿼리 문자열의 SHA512 해시를 함께 서명한다
        claims["query_hash"] = hashlib.sha512(query.encode("utf-8")).hexdigest()
        claims["query_hash_alg"] = "SHA512"
    payload = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing = (header + "." + payload).encode("ascii")
    sig = hmac.new(secret_key.encode("utf-8"), signing, hashlib.sha512).digest()
    return header + "." + payload + "." + _b64url(sig)


def upbit_error(status, data):
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        err = data["error"]
        name, msg = err.get("name", ""), err.get("message", "")
        if name == "no_authorization_ip":
            return "업비트에 등록되지 않은 IP입니다. Open API 관리에서 현재 IP를 허용 목록에 추가하세요."
        if name in ("invalid_access_key", "jwt_verification"):
            return "업비트 API 키가 올바르지 않습니다. api-keys.txt 의 키를 확인하세요."
        if name == "out_of_scope":
            return "업비트 API 키에 '자산조회' 권한이 없습니다."
        return "업비트 오류 (%s): %s" % (name or status, msg)
    return "업비트 응답 오류 (HTTP %s)" % status


def upbit_markets():
    status, data, _ = http("GET", UPBIT + "/v1/market/all")
    if status != 200 or not isinstance(data, list):
        raise SourceError("업비트 마켓 목록 조회 실패 (HTTP %s)" % status)
    return {m.get("market"): m.get("korean_name") for m in data}


def upbit_ticker(markets):
    if not markets:
        return {}
    url = UPBIT + "/v1/ticker?markets=" + urllib.parse.quote(",".join(sorted(markets)), safe=",")
    status, data, _ = http("GET", url)
    if status != 200 or not isinstance(data, list):
        raise SourceError("업비트 시세 조회 실패 (HTTP %s)" % status)
    return {t.get("market"): t for t in data}


def upbit_accounts():
    token = upbit_jwt(env("UPBIT_ACCESS_KEY"), env("UPBIT_SECRET_KEY"))
    status, data, _ = http("GET", UPBIT + "/v1/accounts", {"Authorization": "Bearer " + token})
    if status != 200 or not isinstance(data, list):
        raise SourceError(upbit_error(status, data))
    return data


# 체결 내역 장부. 업비트는 최근 주문만 돌려주므로 본 것을 파일에 쌓아 둔다.
_ledger_lock = threading.Lock()


def ledger_load():
    try:
        with open(LEDGER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("avg", {})
    data.setdefault("fills", {})
    return data


def ledger_save(data):
    tmp = LEDGER_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, LEDGER_FILE)


def upbit_orders_page(state, end_time=None):
    """체결 완료/취소 주문 한 구간(end_time 이전 7일). 조회만 한다."""
    data, status = None, None
    for limit in (1000, 100):
        params = {"state": state, "limit": limit}
        if end_time:
            params["end_time"] = end_time
        encoded = urllib.parse.urlencode(params)
        token = upbit_jwt(env("UPBIT_ACCESS_KEY"), env("UPBIT_SECRET_KEY"), urllib.parse.unquote(encoded))
        status, data, _ = http("GET", UPBIT + "/v1/orders/closed?" + encoded,
                               {"Authorization": "Bearer " + token})
        if status == 200 and isinstance(data, list):
            return data
    raise SourceError(upbit_error(status, data))


def upbit_closed_orders():
    """최근 구간의 체결 주문(전량 체결 + 일부 체결 후 취소)."""
    return upbit_orders_page("done") + upbit_orders_page("cancel")


def ledger_merge(led, orders, names):
    """새 주문을 장부에 넣는다. 원화 마켓의 체결분만 다룬다."""
    changed = False
    for o in orders:
        uid = str(o.get("uuid") or "")
        qty = num(o.get("executed_volume"))
        market = str(o.get("market") or "")
        if not uid or qty <= 0 or not market.startswith("KRW-") or uid in led["fills"]:
            continue
        sym = market[4:]
        funds = num(o.get("executed_funds")) or qty * num(o.get("price"))
        led["fills"][uid] = {
            "id": uid, "ts": o.get("created_at"), "holding_id": "crypto:" + sym, "symbol": sym,
            "name": names.get(market) or sym, "side": "buy" if o.get("side") == "bid" else "sell",
            "qty": qty, "funds": funds, "fee": num(o.get("paid_fee")),
            "price": funds / qty if qty else None, "avg": None, "realized": None,
        }
        changed = True
    return changed


def ledger_recompute(led):
    """체결 내역을 시간순으로 따라가며 이동평균 원가와 실현 손익을 계산한다.
    실현 손익 = 매도 대금 - 수수료 - 매도 수량 x 평균 원가(매수 수수료 포함).
    장부의 매수 기록으로 설명되지 않는 매도(입금받은 코인, 원화 외 마켓에서 산 코인)는
    계좌의 마지막 평단가를 쓰고, 그것도 없으면 실현 손익을 비워 둔다."""
    pos = {}
    for f in sorted(led["fills"].values(), key=lambda f: str(f.get("ts") or "")):
        st = pos.setdefault(f["holding_id"], {"qty": 0.0, "cost": 0.0})
        qty = f["qty"]
        if f["side"] == "buy":
            st["qty"] += qty
            st["cost"] += f["funds"] + f["fee"]
            f["avg"], f["realized"] = None, None
            continue
        f.pop("note", None)
        if st["qty"] > 0 and st["qty"] >= qty * (1 - 1e-6):
            avg = st["cost"] / st["qty"]
        elif st["qty"] > 0 and st["qty"] >= qty * 0.95:
            # 매수 기록보다 조금 더 많이 판 경우(스테이킹 보상 등 무상 취득분): 초과분의 원가는 0으로 본다
            avg = st["cost"] / qty
            f["note"] = "매수 기록보다 %.2f%% 많이 매도. 초과분은 원가 0으로 계산" % ((qty / st["qty"] - 1) * 100)
        else:
            avg = led["avg"].get(f["holding_id"])
        f["avg"] = avg
        f["realized"] = (f["funds"] - f["fee"] - qty * avg) if avg else None
        if st["qty"] > 0:
            left = max(0.0, st["qty"] - qty)
            st["cost"] *= left / st["qty"]
            st["qty"] = left


def upbit_fills(names, current_avg, debug):
    """최근 체결을 장부에 합치고 전체 목록을 최신순으로 돌려준다."""
    orders = cached("upbit_orders", 60, upbit_closed_orders)
    if orders:
        debug["upbit_order_fields"] = sorted(orders[0].keys())
    with _ledger_lock:
        led = ledger_load()
        before = json.dumps(led, sort_keys=True)
        for hid, avg in current_avg.items():
            if avg:
                led["avg"][hid] = avg
        ledger_merge(led, orders, names)
        ledger_recompute(led)
        if json.dumps(led, sort_keys=True) != before:
            ledger_save(led)
        return sorted(led["fills"].values(), key=lambda f: str(f.get("ts") or ""), reverse=True)


def upbit_signed_get(path, params):
    encoded = urllib.parse.urlencode(params)
    token = upbit_jwt(env("UPBIT_ACCESS_KEY"), env("UPBIT_SECRET_KEY"), urllib.parse.unquote(encoded))
    status, data, _ = http("GET", UPBIT + path + "?" + encoded, {"Authorization": "Bearer " + token})
    return status, data


def upbit_transfers(path):
    """입금 또는 출금 내역 전체 (모든 통화). 조회만 한다."""
    rows = []
    for page in range(1, 51):
        status, data = upbit_signed_get(path, {"limit": 100, "page": page, "order_by": "asc"})
        if status != 200 or not isinstance(data, list):
            raise SourceError(upbit_error(status, data))
        rows.extend(data)
        if len(data) < 100:
            break
    return rows


def upbit_day_price(market, when_iso):
    """코인 입출금을 원화로 환산할 때 쓰는 그 무렵의 일봉 종가 (공개 시세)."""
    query = urllib.parse.urlencode({"market": market, "to": when_iso, "count": 1})
    status, data, _ = http("GET", UPBIT + "/v1/candles/days?" + query)
    if status == 200 and isinstance(data, list) and data:
        return num(data[0].get("trade_price")) or None
    return None


def mark_round_trips(items):
    """같은 코인을 출금했다가 비슷한 수량(98~115%)으로 다시 입금한 경우 round_trip 으로 표시한다.
    스테이킹처럼 자산이 잠시 거래소 밖에 있었던 것이라, 입출금 환산액으로 세면 그 사이 시세 변동이
    '새로 넣은 돈'으로 잡힌다. 늘어난 수량(보상)은 매도 때 원가 0으로 실현 손익에 들어간다."""
    out = {}
    for f in sorted(items, key=lambda f: str(f.get("ts") or "")):
        cur = f["currency"]
        if cur == "KRW":
            continue
        if f["kind"] == "withdraw":
            out.setdefault(cur, []).append(f)
            continue
        pend = out.get(cur) or []
        sent = sum(w["amount"] for w in pend)
        if sent > 0 and sent * 0.98 <= f["amount"] <= sent * 1.15:
            for w in pend:
                w["round_trip"] = True
            f["round_trip"] = True
            out[cur] = []


def upbit_flows(names):
    """입출금 내역을 장부에 합치고 (목록, 요약)을 돌려준다.
    순입금액 = 원화 입금 - 원화 출금 + 코인 입금 환산액 - 코인 출금 환산액.
    코인 입출금은 그 무렵 일봉 종가로 환산한 추정값이다."""
    deposits, withdraws = cached(
        "upbit_transfers", 600,
        lambda: (upbit_transfers("/v1/deposits"), upbit_transfers("/v1/withdraws")))
    with _ledger_lock:
        led = ledger_load()
        flows = led.setdefault("flows", {})
        changed = False
        for kind, rows, done in (("deposit", deposits, "ACCEPTED"), ("withdraw", withdraws, "DONE")):
            for r in rows:
                uid = str(r.get("uuid") or "")
                if not uid or r.get("state") != done or uid in flows:
                    continue
                currency = str(r.get("currency") or "").upper()
                amount = num(r.get("amount"))
                ts = r.get("done_at") or r.get("created_at")
                if currency == "KRW":
                    krw, est = amount, False
                else:
                    market = "KRW-" + currency
                    price = upbit_day_price(market, ts) if market in names else None
                    krw, est = (amount * price if price else None), True
                    time.sleep(0.12)
                flows[uid] = {"id": uid, "ts": ts, "kind": kind, "currency": currency,
                              "amount": amount, "krw": krw, "est": est}
                changed = True
        if changed:
            ledger_save(led)
        items = sorted((dict(f) for f in flows.values()), key=lambda f: str(f.get("ts") or ""), reverse=True)
    mark_round_trips(items)

    summary = {"deposit_krw": 0.0, "withdraw_krw": 0.0, "coin_in_krw": 0.0, "coin_out_krw": 0.0,
               "unknown": 0, "coin_count": 0, "staking_count": 0, "count": len(items)}
    for f in items:
        coin = f["currency"] != "KRW"
        if f.get("round_trip"):              # 스테이킹 등으로 나갔다 돌아온 코인은 입출금으로 세지 않는다
            summary["staking_count"] += 1
            continue
        summary["coin_count"] += 1 if coin else 0
        if f["krw"] is None:
            summary["unknown"] += 1
            continue
        key = ("coin_in_krw" if coin else "deposit_krw") if f["kind"] == "deposit" else \
              ("coin_out_krw" if coin else "withdraw_krw")
        summary[key] += f["krw"]
    summary["net"] = (summary["deposit_krw"] + summary["coin_in_krw"]
                      - summary["withdraw_krw"] - summary["coin_out_krw"])
    return items, summary


def backfill(years=9):
    """과거 체결 내역을 7일 구간씩 거슬러 올라가며 한 번에 받아 장부에 넣는다. (--backfill)"""
    names = upbit_markets()
    now = datetime.now(timezone.utc)
    weeks, found = years * 52, 0
    with _ledger_lock:
        led = ledger_load()
    for w in range(0, weeks):
        end = (now - timedelta(days=7 * w)).strftime("%Y-%m-%dT%H:%M:%SZ")
        orders = []
        for state in ("done", "cancel"):
            orders += upbit_orders_page(state, end)
            time.sleep(0.15)
        before = len(led["fills"])
        ledger_merge(led, orders, names)
        found += len(led["fills"]) - before
        if w % 26 == 25:
            print("- %d주 전까지 확인, 새 체결 %d건" % (w + 1, found), flush=True)
    with _ledger_lock:
        fresh = ledger_load()                # 그 사이 서버가 넣은 것을 잃지 않게 합친다
        fresh["fills"].update(led["fills"])
        fresh["backfilled_at"] = now.isoformat()
        ledger_recompute(fresh)
        ledger_save(fresh)
    sells = [f for f in fresh["fills"].values() if f["side"] == "sell"]
    print("- 완료: 체결 %d건, 매도 %d건 중 실현 손익 계산 %d건" % (
        len(fresh["fills"]), len(sells), sum(1 for f in sells if f["realized"] is not None)))


def fetch_upbit(debug, light=False):
    accounts = cached("upbit_accounts", 10, upbit_accounts)
    if accounts:
        debug["upbit_fields"] = sorted(accounts[0].keys())
    names = cached("upbit_markets", 6 * 3600, upbit_markets)

    holdings, cash, wanted = [], [], []
    for a in accounts:
        cur = str(a.get("currency") or "").upper()
        qty = num(a.get("balance")) + num(a.get("locked"))
        if not cur or qty <= 0:
            continue
        if cur == "KRW":
            cash.append({"id": "cash:upbit:KRW", "label": "업비트 원화", "ccy": "KRW", "amount": qty})
            continue
        market = "KRW-" + cur
        avg = num(a.get("avg_buy_price"))
        holdings.append({
            "id": "crypto:" + cur, "cls": "crypto", "symbol": cur,
            "name": names.get(market) or cur, "qty": qty,
            "avg": avg if avg > 0 and str(a.get("unit_currency") or "KRW").upper() == "KRW" else None,
            "price": None, "prev_close": None, "ccy": "KRW", "broker": "upbit",
            "_market": market if market in names else None,
        })
        if market in names:
            wanted.append(market)

    ticks = cached("upbit_ticker:" + ",".join(sorted(wanted)), 2, lambda: upbit_ticker(wanted))
    for h in holdings:
        t = ticks.get(h.pop("_market") or "")
        if t:
            h["price"] = num(t.get("trade_price")) or None
            h["prev_close"] = num(t.get("prev_closing_price")) or None

    if light or LIGHT:
        return {"holdings": holdings, "cash": cash, "fills": None, "flows": None, "flows_summary": None}
    fills = None   # 체결 내역 조회가 안 되면(권한 없음 등) 잔고는 그대로 두고 이것만 비운다
    if qty_changed("upbit", holdings):   # 수량이 바뀌었으면 체결 내역을 캐시 없이 바로 받는다
        _cache.pop("upbit_orders", None)
    try:
        fills = upbit_fills(names, {h["id"]: h["avg"] for h in holdings if h["avg"]}, debug)
    except Exception as e:
        log("업비트 체결 내역 조회 실패:", e)
    flows, flows_summary = None, None   # 입출금 내역도 권한이 없으면 비워 둔다
    try:
        flows, flows_summary = upbit_flows(names)
    except Exception as e:
        log("업비트 입출금 내역 조회 실패:", e)
    return {"holdings": holdings, "cash": cash, "fills": fills,
            "flows": flows, "flows_summary": flows_summary}


# ─────────────────────────────────────────
# 키움증권 REST API
# 모든 TR이 POST + JSON body, TR 코드는 'api-id' 헤더. 계좌는 앱키에 묶여 있다.
# ─────────────────────────────────────────

_kiwoom_lock = threading.RLock()
_kiwoom_throttle = threading.Lock()
_kiwoom_state = {"token": None, "expires": None, "fp": None, "last": 0.0}


def kiwoom_code(data):
    value = data.get("return_code", data.get("returnCode"))
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return int(text) if text.lstrip("-").isdigit() else None


def kiwoom_embedded_code(msg):
    """return_msg 안에 '[8005:Token이 유효하지 않습니다]' 형태로 실제 코드가 오는 경우"""
    s = str(msg or "")
    i = s.find("[")
    j = s.find(":", i)
    if i >= 0 and j > i and s[i + 1:j].isdigit():
        return int(s[i + 1:j])
    return None


def _kiwoom_fingerprint():
    raw = (env("KIWOOM_APP_KEY") + ":" + env("KIWOOM_SECRET_KEY")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _token_valid(expires):
    return expires is not None and datetime.now(KST) < expires - timedelta(minutes=10)


def kiwoom_issue_token():
    status, data, _ = http("POST", KIWOOM + "/oauth2/token",
                           {"Content-Type": "application/json;charset=UTF-8"},
                           {"grant_type": "client_credentials",
                            "appkey": env("KIWOOM_APP_KEY"),
                            "secretkey": env("KIWOOM_SECRET_KEY")})
    data = data if isinstance(data, dict) else {}
    code = kiwoom_code(data)
    if status >= 400 or code not in (None, 0) or not data.get("token"):
        raise SourceError("키움 토큰 발급 실패 (%s): %s" % (
            code if code is not None else status,
            data.get("return_msg") or "앱키/시크릿키와 허용 IP를 확인하세요."))
    try:
        expires = datetime.strptime(str(data["expires_dt"]), "%Y%m%d%H%M%S").replace(tzinfo=KST)
    except (KeyError, ValueError):
        expires = datetime.now(KST) + timedelta(hours=23)
    fp = _kiwoom_fingerprint()
    _kiwoom_state.update(token=data["token"], expires=expires, fp=fp)
    try:
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"token": data["token"], "expires": expires.isoformat(), "fp": fp}, f)
    except OSError as e:
        log("토큰 캐시 저장 실패:", e)


def kiwoom_token(force=False):
    with _kiwoom_lock:
        fp = _kiwoom_fingerprint()
        st = _kiwoom_state
        if not force and st["fp"] == fp and _token_valid(st["expires"]):
            return st["token"]
        if not force and os.path.exists(TOKEN_FILE):
            try:
                with open(TOKEN_FILE, "r") as f:
                    saved = json.load(f)
                expires = datetime.fromisoformat(saved["expires"])
                if saved.get("fp") == fp and _token_valid(expires):
                    st.update(token=saved["token"], expires=expires, fp=fp)
                    return st["token"]
            except (OSError, ValueError, KeyError):
                pass
        kiwoom_issue_token()
        return st["token"]


def kiwoom_request(api_id, path, body, cont_key=None):
    auth_retried, rate_retries = False, 0
    while True:
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "api-id": api_id,
            "authorization": "Bearer " + kiwoom_token(),
        }
        if cont_key:
            headers["cont-yn"] = "Y"
            headers["next-key"] = cont_key
        with _kiwoom_throttle:
            wait = KIWOOM_INTERVAL - (time.monotonic() - _kiwoom_state["last"])
            if wait > 0:
                time.sleep(wait)
            _kiwoom_state["last"] = time.monotonic()
        status, data, rh = http("POST", KIWOOM + path, headers, body)
        data = data if isinstance(data, dict) else {}
        code = kiwoom_code(data)
        embedded = kiwoom_embedded_code(data.get("return_msg"))

        if not auth_retried and (status == 401 or code in KIWOOM_AUTH_CODES
                                 or embedded in KIWOOM_AUTH_CODES):
            auth_retried = True
            kiwoom_token(force=True)
            continue
        if (status == 429 or code in KIWOOM_RATE_CODES or embedded in KIWOOM_RATE_CODES) \
                and rate_retries < 2:
            rate_retries += 1
            time.sleep(1.0 * rate_retries)
            continue
        if status >= 400 or code != 0:
            # return_code 가 없는 응답(점검 페이지 등)도 실패로 본다. 성공으로 보면 '보유 0주'로 읽힌다.
            msg = data.get("return_msg") or ("HTTP %s" % status if status >= 400
                                             else "응답 형식이 올바르지 않습니다")
            raise SourceError("키움 %s 오류 (%s): %s" % (api_id, embedded or code or status, msg))
        return data, rh


def kiwoom_call(api_id, path, body):
    return kiwoom_request(api_id, path, body)[0]


def kiwoom_list(api_id, path, body, list_key):
    rows, cont_key = [], None
    for _ in range(KIWOOM_MAX_PAGES):
        data, rh = kiwoom_request(api_id, path, body, cont_key)
        rows.extend(data.get(list_key) or [])
        nxt = rh.get("next-key") if rh else None
        if not rh or str(rh.get("cont-yn") or "").upper() != "Y" or not nxt:
            break
        cont_key = nxt
    return rows


def normalize_kr_code(code):
    """'A005930' → '005930', '005930_NX' → '005930'"""
    code = str(code).strip().upper().split("_", 1)[0]
    if len(code) == 7 and code[0] in "AJQ":
        code = code[1:]
    return code


def fetch_kiwoom_kr(debug):
    def load():
        rows = kiwoom_list("kt00018", "/api/dostk/acnt",
                           {"qry_tp": "1", "dmst_stex_tp": "KRX"}, "acnt_evlt_remn_indv_tot")
        cash = None
        try:
            dep = kiwoom_call("kt00001", "/api/dostk/acnt", {"qry_tp": "3"})
            value = dep.get("d2_entra")
            if value in (None, ""):
                value = dep.get("entr")
            cash = num(value)
        except SourceError as e:
            log("원화 예수금 조회 실패:", e)
        return rows, cash

    rows, cash_krw = cached("kiwoom_kr", 15, load)
    if rows:
        debug["kiwoom_kr_fields"] = sorted(rows[0].keys())

    agg = {}
    for r in rows:
        code = normalize_kr_code(r.get("stk_cd", ""))
        qty = num(r.get("rmnd_qty"))
        if not code or qty <= 0:
            continue
        a = agg.setdefault(code, {"qty": 0.0, "pur_amt": 0.0, "pur_pric": 0.0,
                                  "name": "", "price": 0.0, "prev": 0.0, "pnl": None, "tb": 0.0, "ts": 0.0})
        a["tb"] += num(r.get("tdy_buyq"))           # 오늘 체결된 매수·매도 수량 (거래내역에는 결제 뒤에 잡힌다)
        a["ts"] += num(r.get("tdy_sellq"))
        if str(r.get("evltv_prft") or "").strip():   # 키움이 계산한 평가손익 (예상 매도 비용 차감)
            a["pnl"] = (a["pnl"] or 0.0) + num(r.get("evltv_prft"))
        a["qty"] += qty                 # 같은 종목이 신용구분 등으로 여러 행일 수 있다
        a["pur_amt"] += num(r.get("pur_amt"))
        a["pur_pric"] = num(r.get("pur_pric")) or a["pur_pric"]
        a["name"] = str(r.get("stk_nm") or "").strip() or a["name"]
        a["price"] = price_of(r.get("cur_prc")) or a["price"]
        a["prev"] = price_of(r.get("pred_close_pric")) or a["prev"]

    holdings = []
    for code, a in agg.items():
        avg = a["pur_amt"] / a["qty"] if a["pur_amt"] else a["pur_pric"]
        holdings.append({
            "id": "kr:" + code, "cls": "kr", "symbol": code, "name": a["name"] or code,
            "qty": a["qty"], "avg": avg or None, "price": a["price"] or None,
            "prev_close": a["prev"] or None, "ccy": "KRW", "broker": "kiwoom", "pnl_krw": a["pnl"],
            "today_buy": a["tb"], "today_sell": a["ts"],
        })
    cash = []
    if cash_krw:
        cash.append({"id": "cash:kiwoom:KRW", "label": "키움 원화 예수금",
                     "ccy": "KRW", "amount": cash_krw})
    return {"holdings": holdings, "cash": cash}


def fetch_kiwoom_us(debug):
    def load():
        rows = kiwoom_list("ust21070", "/api/us/acnt", {"stex_tp": "", "stk_cd": ""}, "result_list")
        cash = None
        try:
            dep = kiwoom_call("ust21160", "/api/us/acnt", {})
            # dN_usd_fx_entr 는 누적값이라 마지막 값이 미결제 매매를 모두 반영한다
            for key in ("d4_usd_fx_entr", "d3_usd_fx_entr", "d2_usd_fx_entr",
                        "d1_usd_fx_entr", "d0_usd_fx_entr"):
                if str(dep.get(key) or "").strip():
                    cash = num(dep.get(key))
                    break
        except SourceError as e:
            log("달러 예수금 조회 실패:", e)
        return rows, cash

    rows, cash_usd = cached("kiwoom_us", 15, load)
    if rows:
        debug["kiwoom_us_fields"] = sorted(rows[0].keys())

    agg, fx_rate = {}, 0.0
    for r in rows:
        ccy = str(r.get("crnc_code") or "USD").strip().upper() or "USD"
        symbol = str(r.get("stk_cd") or "").strip().upper()
        qty = num(r.get("poss_qty"))
        if not symbol or qty <= 0 or ccy != "USD":
            continue
        a = agg.setdefault(symbol, {"qty": 0.0, "avg": 0.0, "avg_krw": 0.0, "price": 0.0, "name": "", "pnl": None, "tb": 0.0, "ts": 0.0})
        a["tb"] += num(r.get("tdy_cntr_buyq"))      # 오늘 체결된 매수·매도 수량 (거래내역에는 결제 뒤에 잡힌다)
        a["ts"] += num(r.get("tdy_cntr_sellq"))
        if str(r.get("pl_amt_krw") or "").strip():   # 키움이 계산한 원화 평가손익
            a["pnl"] = (a["pnl"] or 0.0) + num(r.get("pl_amt_krw"))
        a["qty"] += qty
        a["avg"] = num(r.get("frgn_stk_book_uv")) or a["avg"]
        a["avg_krw"] = num(r.get("frgn_stk_book_uv_krw")) or a["avg_krw"]   # 매입 당시 환율이 반영된 원화 평단가
        fx_rate = num(r.get("exch_rate")) or fx_rate
        a["price"] = price_of(r.get("now_pric")) or a["price"]
        a["name"] = first_text(r, ("frgn_stk_nm", "stk_nm", "stk_kor_nm", "kor_stk_nm", "stk_eng_nm")) or a["name"]

    holdings = [{
        "id": "us:" + symbol, "cls": "us", "symbol": symbol, "name": a["name"] or symbol,
        "qty": a["qty"], "avg": a["avg"] or None, "avg_krw": a["avg_krw"] or None, "price": a["price"] or None,
        "prev_close": None, "ccy": "USD", "broker": "kiwoom", "pnl_krw": a["pnl"],
        "today_buy": a["tb"], "today_sell": a["ts"],
    } for symbol, a in agg.items()]
    cash = []
    if cash_usd:
        cash.append({"id": "cash:kiwoom:USD", "label": "키움 달러 예수금",
                     "ccy": "USD", "amount": cash_usd})
    return {"holdings": holdings, "cash": cash, "fx_rate": fx_rate or None}


def kiwoom_fx():
    rows = kiwoom_list("ust21120", "/api/us/acnt", {"cmsn_incl_tp": "0", "exrt_tp": "0"}, "result_list")
    for r in rows:
        if str(r.get("crnc_code") or "").strip().upper() == "USD":
            rate = num(r.get("crnc_rt"))
            if rate > 0:
                return rate
    raise SourceError("키움 환율 응답에 USD가 없습니다")


# ── 키움 실현손익과 입출금 (조회 전용 TR: ust21530, ka10073, kt00015) ──

KIWOOM_FLOW_WORDS = ("이체입금", "이체출금", "대체입금", "대체출금")   # 계좌 밖에서 들어오고 나간 돈만 센다
# 배당·이자 적요. 세금 출금은 해당 수입에서 빼는 항목이다. 쿠폰 같은 이벤트 입금은 넣지 않는다.
KIWOOM_INCOME_KIND = (("배당세", "tax"), ("이자세금", "tax"), ("배당", "dividend"), ("분배금", "dividend"),
                      ("이자", "interest"), ("이용료", "interest"))


def kiwoom_list_or_empty(api_id, path, body, key):
    try:
        return kiwoom_list(api_id, path, body, key)
    except SourceError as e:
        if "조회내역이 없습니다" in str(e) or "조회 내역이 없습니다" in str(e):
            return []
        raise


def _ymd(d):
    return d.strftime("%Y%m%d")


def _day_iso(ymd):
    return "%s-%s-%sT00:00:00+09:00" % (ymd[:4], ymd[4:6], ymd[6:8])


def kiwoom_us_realized(start, end):
    """미국주식 실현손익 (기간 6개월 이내). 원화 기준: 매도금액 - 매입금액 - 비용, 환율 변동 포함."""
    body = {"strt_dt": _ymd(start), "end_dt": _ymd(end), "fc_krw_tp": "1"}
    rows = kiwoom_list_or_empty("ust21530", "/api/us/acnt", body, "result_list")
    usd = {}                                         # 같은 조회를 달러 기준(fc_krw_tp=0)으로 한 번 더 받아 짝지운다
    try:
        for r in kiwoom_list_or_empty("ust21530", "/api/us/acnt", dict(body, fc_krw_tp="0"), "result_list"):
            usd.setdefault((str(r.get("sell_dt") or "").strip(), str(r.get("stk_cd") or "").strip().upper(),
                            num(r.get("sell_qty"))), []).append(r)
    except SourceError:
        usd = {}
    out = []
    for r in rows:
        qty, day = num(r.get("sell_qty")), str(r.get("sell_dt") or "").strip()
        code = str(r.get("stk_cd") or "").strip().upper()
        if qty <= 0 or len(day) != 8 or not code:
            continue
        funds = num(r.get("sell_amt"))
        row = {"day": day, "cls": "us", "symbol": code, "name": str(r.get("frgn_stk_nm") or code).strip(),
               "qty": qty, "funds": funds, "fee": num(r.get("cmsn_tax")), "price": funds / qty,
               "avg": num(r.get("buy_amt")) / qty, "realized": num(r.get("pl_amt"))}
        twin = usd.get((day, code, qty))
        if twin:
            u = twin.pop(0)
            row.update({"funds_usd": num(u.get("sell_amt")), "price_usd": num(u.get("sell_amt")) / qty,
                        "avg_usd": num(u.get("buy_amt")) / qty, "realized_usd": num(u.get("pl_amt"))})
        out.append(row)
    return out


def kiwoom_kr_realized(start, end):
    """국내주식 일자별 종목별 실현손익 (기간 약 3개월 이내)."""
    rows = kiwoom_list_or_empty("ka10073", "/api/dostk/acnt",
                                {"stk_cd": "", "strt_dt": _ymd(start), "end_dt": _ymd(end)}, "dt_stk_rlzt_pl")
    out = []
    for r in rows:
        qty, day = num(r.get("cntr_qty")), str(r.get("dt") or "").strip()
        code = normalize_kr_code(r.get("stk_cd", ""))
        if qty <= 0 or len(day) != 8 or not code:
            continue
        price = price_of(r.get("cntr_pric"))
        out.append({"day": day, "cls": "kr", "symbol": code, "name": str(r.get("stk_nm") or code).strip(),
                    "qty": qty, "funds": qty * price, "fee": num(r.get("tdy_trde_cmsn")) + num(r.get("tdy_trde_tax")),
                    "price": price, "avg": price_of(r.get("buy_uv")), "realized": num(r.get("tdy_sel_pl"))})
    return out


def kiwoom_flow_rows(start, end):
    """계좌 입출금 중 외부 이체만 (이자, 배당, 환전 정산은 제외)."""
    body = {"strt_dt": _ymd(start), "end_dt": _ymd(end), "tp": "1", "stk_cd": "", "crnc_cd": "", "gds_tp": "0",
            "frgn_stex_code": "", "dmst_stex_tp": "%", "qry_sort_tp": "2"}
    rows = kiwoom_list_or_empty("kt00015", "/api/dostk/acnt", body, "trst_ovrl_trde_prps_array")
    out, labels, income, seen = [], {}, [], {}
    for r in rows:
        memo = str(r.get("rmrk_nm") or "").strip()
        labels[memo] = labels.get(memo, 0) + 1
        day = str(r.get("trde_dt") or "").strip()
        kind = next((k for w, k in KIWOOM_INCOME_KIND if w in memo), None)
        if len(day) == 8 and kind:
            currency = str(r.get("crnc_cd") or "KRW").strip().upper() or "KRW"
            fc = currency != "KRW"
            gross = abs(num(r.get("fc_trde_amt" if fc else "trde_amt")))
            tax = abs(num(r.get("frgn_pay_txam" if fc else "incm_resi_tax")))
            net = abs(num(r.get("fc_exct_amt" if fc else "exct_amt"))) or max(0.0, gross - tax)
            if kind == "tax":                         # 세금 출금: 낸 만큼 수입에서 빠진다
                gross, tax, net = 0.0, net or gross, -(net or gross)
            key = "kwi:%s:%s" % (day, str(r.get("trde_no") or "").strip())
            seen[key] = seen.get(key, 0) + 1
            code = str(r.get("stk_cd") or "").strip().upper()
            income.append({"id": "%s:%d" % (key, seen[key]), "day": day, "kind": kind, "currency": currency,
                           "symbol": code if fc else normalize_kr_code(code) if code else "",
                           "name": str(r.get("stk_nm") or "").strip(), "gross": gross, "tax": tax, "net": net,
                           "memo": memo})
            continue
        if len(day) != 8 or not any(w in memo for w in KIWOOM_FLOW_WORDS):
            continue
        currency = str(r.get("crnc_cd") or "KRW").strip().upper() or "KRW"
        amount = abs(num(r.get("trde_amt")) or num(r.get("exct_amt")))
        out.append({"id": "kwf:%s:%s" % (day, str(r.get("trde_no") or "").strip()), "ts": _day_iso(day),
                    "kind": "withdraw" if "출금" in memo else "deposit", "currency": currency, "amount": amount,
                    "krw": amount if currency == "KRW" else None, "est": False, "memo": memo})
    return out, labels, income


def kiwoom_buy_rows(start, end):
    """주식 매수 체결(위탁종합거래내역 tp=4). 국내와 미국 모두. 금액은 원화, 미국주식은 달러 금액도 함께."""
    body = {"strt_dt": _ymd(start), "end_dt": _ymd(end), "tp": "4", "stk_cd": "", "crnc_cd": "", "gds_tp": "0",
            "frgn_stex_code": "", "dmst_stex_tp": "%", "qry_sort_tp": "2"}
    rows = kiwoom_list_or_empty("kt00015", "/api/dostk/acnt", body, "trst_ovrl_trde_prps_array")
    out, seen = [], {}
    for r in rows:
        day = str(r.get("trde_dt") or "").strip()
        qty = abs(num(r.get("trde_qty_jwa_cnt")))
        usd = str(r.get("crnc_cd") or "KRW").strip().upper() == "USD"
        code = str(r.get("stk_cd") or "").strip().upper() if usd else normalize_kr_code(r.get("stk_cd", ""))
        if len(day) != 8 or qty <= 0 or not code:
            continue
        funds = abs(num(r.get("trde_amt")) or num(r.get("exct_amt")))
        key = "kwb:%s:%s" % (day, str(r.get("trde_no") or "").strip())
        seen[key] = seen.get(key, 0) + 1
        cntr = str(r.get("cntr_dt") or "").strip()   # trde_dt 는 결제일이라 실제 체결일(cntr_dt)을 날짜로 쓴다. id 는 그대로 둔다
        row = {"id": "%s:%d" % (key, seen[key]), "day": cntr if len(cntr) == 8 else day, "cls": "us" if usd else "kr", "symbol": code,
               "name": str(r.get("stk_nm") or code).strip(), "qty": qty, "funds": funds,
               "price": funds / qty if funds else None, "fee": abs(num(r.get("cmsn")))}
        if usd:
            fc = abs(num(r.get("fc_trde_amt")))
            row.update({"funds_usd": fc, "price_usd": price_of(r.get("trde_unit")) or (fc / qty if fc else None)})
        out.append(row)
    return out


def kiwoom_rights_rows(start, end):
    """유상증자로 받은 주식(유상주입고)을 매수로 본다. 원가 = 청약 대금(유상불입출금) − 배정되지 않아 돌려받은 돈(유상청약환불입금)."""
    body = {"strt_dt": _ymd(start), "end_dt": _ymd(end), "tp": "0", "stk_cd": "", "crnc_cd": "",
            "gds_tp": "0", "frgn_stex_code": "", "dmst_stex_tp": "%", "qry_sort_tp": "2"}
    rows = kiwoom_list_or_empty("kt00015", "/api/dostk/acnt", body, "trst_ovrl_trde_prps_array")
    paid = {}
    for r in rows:
        memo, code = str(r.get("rmrk_nm") or ""), normalize_kr_code(r.get("stk_cd", ""))
        if "유상불입" in memo:
            paid[code] = paid.get(code, 0.0) + abs(num(r.get("trde_amt")))
        elif "유상청약환불" in memo:
            paid[code] = paid.get(code, 0.0) - abs(num(r.get("trde_amt")))
    out = []
    for r in rows:
        day = str(r.get("trde_dt") or "").strip()
        if "유상주입고" not in str(r.get("rmrk_nm") or "") or not (_ymd(start) <= day <= _ymd(end)):
            continue
        code, qty = normalize_kr_code(r.get("stk_cd", "")), abs(num(r.get("trde_qty_jwa_cnt")))
        if not code or qty <= 0:
            continue
        cost = max(0.0, paid.get(code, 0.0))
        out.append({"id": "kwr:%s:%s:1" % (day, str(r.get("trde_no") or "").strip()), "day": day, "cls": "kr", "symbol": code,
                    "name": str(r.get("stk_nm") or code).strip(), "qty": qty, "funds": cost,
                    "price": cost / qty if cost else None, "fee": 0.0, "rights": True})
    return out


def kiwoom_sell_rows(start, end):
    """주식 매도 체결(위탁종합거래내역 tp=5). 실현손익 조회(ka10073·ust21530)가 닿지 않는 옛 매도를 채우는 데 쓴다.
    proceeds 는 수수료·세금을 뺀 정산 금액이다(미국주식은 달러)."""
    body = {"strt_dt": _ymd(start), "end_dt": _ymd(end), "tp": "5", "stk_cd": "", "crnc_cd": "", "gds_tp": "0",
            "frgn_stex_code": "", "dmst_stex_tp": "%", "qry_sort_tp": "2"}
    rows = kiwoom_list_or_empty("kt00015", "/api/dostk/acnt", body, "trst_ovrl_trde_prps_array")
    out, seen = [], {}
    for r in rows:
        day = str(r.get("trde_dt") or "").strip()
        qty = abs(num(r.get("trde_qty_jwa_cnt")))
        usd = str(r.get("crnc_cd") or "KRW").strip().upper() == "USD"
        code = str(r.get("stk_cd") or "").strip().upper() if usd else normalize_kr_code(r.get("stk_cd", ""))
        if len(day) != 8 or qty <= 0 or not code:
            continue
        key = "kws:%s:%s" % (day, str(r.get("trde_no") or "").strip())
        seen[key] = seen.get(key, 0) + 1
        cntr = str(r.get("cntr_dt") or "").strip()
        row = {"id": "%s:%d" % (key, seen[key]), "day": cntr if len(cntr) == 8 else day, "cls": "us" if usd else "kr",
               "symbol": code, "name": str(r.get("stk_nm") or code).strip(), "qty": qty}
        # 정산 금액(exct_amt)은 같은 날 여러 체결을 마지막 행에 몰아 적으므로, 행마다 체결 금액에서 그 행의 수수료·세금을 뺀다
        if usd:
            row.update({"proceeds_usd": abs(num(r.get("fc_trde_amt"))) - abs(num(r.get("fc_cmsn"))) - abs(num(r.get("fc_trde_tax"))),
                        "price_usd": price_of(r.get("trde_unit"))})
        else:
            row.update({"proceeds": abs(num(r.get("trde_amt"))) - abs(num(r.get("tax_sum_cmsn"))), "price": price_of(r.get("trde_unit"))})
        out.append(row)
    return out


def kiwoom_merge(led, start, end, realized=None, flows=None, buys=None, income=None, sells=None):
    """[start, end] 구간의 기록을 새로 받은 것으로 바꿔 넣는다 (행에 고유 번호가 없어 구간째 교체)."""
    kw = led.setdefault("kiwoom", {})
    lo, hi = _ymd(start), _ymd(end)
    if realized is not None:
        store = kw.setdefault("realized", {})
        classes = {r["cls"] for r in realized} or {"us", "kr"}
        for key in [k for k, v in store.items() if lo <= v["day"] <= hi and v["cls"] in classes]:
            del store[key]
        seen = {}
        for r in realized:
            base = "kw:%s:%s:%s" % (r["cls"], r["day"], r["symbol"])
            seen[base] = seen.get(base, 0) + 1
            store["%s:%d" % (base, seen[base])] = r
    if buys is not None:
        store = kw.setdefault("buys", {})
        for key in [k for k, v in store.items() if lo <= k.split(":")[1] <= hi]:   # 조회는 결제일(id 속 날짜) 기준이다
            del store[key]
        for b in buys:
            store[b["id"]] = b
    if sells is not None:
        store = kw.setdefault("sells", {})
        for key in [k for k in store if lo <= k.split(":")[1] <= hi]:   # 조회는 결제일(id 속 날짜) 기준이다
            del store[key]
        for b in sells:
            store[b["id"]] = b
    if income is not None:
        store = kw.setdefault("income", {})
        for key in [k for k, v in store.items() if lo <= v["day"] <= hi]:
            del store[key]
        for i in income:
            store[i["id"]] = i
    if flows is not None:
        store = kw.setdefault("flows", {})
        for key in [k for k, v in store.items() if lo <= v["ts"][:10].replace("-", "") <= hi]:
            del store[key]
        for f in flows:
            store[f["id"]] = f


def usdkrw_history():
    """원/달러 일별 종가 (공개 시세, 약 10년치). {YYYY-MM-DD: 환율}. 하루 캐시."""
    def load():
        url = "https://query1.finance.yahoo.com/v8/finance/chart/KRW%3DX?range=10y&interval=1d"
        status, data, _ = http("GET", url)
        try:
            res = data["chart"]["result"][0]
            off = int(res["meta"].get("gmtoffset") or 0)
            out = {}
            for ts, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]):
                if c:
                    out[datetime.fromtimestamp(ts + off, timezone.utc).strftime("%Y-%m-%d")] = round(c, 2)
            return out
        except (TypeError, KeyError, IndexError, ValueError):
            log("환율 이력 조회 실패:", status)
            return {}
    return cached("usdkrw_history", 24 * 3600, load)


def _fx_on(hist, ymd):
    """그날 또는 그 이전 가장 가까운 날의 환율."""
    d = datetime.strptime(ymd, "%Y%m%d").date()
    for back in range(0, 10):
        v = hist.get((d - timedelta(days=back)).isoformat())
        if v:
            return v
    return None


def _fill_old_sells(kw, realized, buys):
    """실현손익 조회에 없는 매도(주로 2023년 초 이전)를 거래내역의 매도로 채운다.
    손익은 그때까지의 매수로 만든 이동평균 원가로 계산한다(원화, 미국주식은 매수·매도일 환율). calc=True 로 표시."""
    have = {(r["cls"], r["ts"][:10], r["symbol"]) for r in realized}
    sells = sorted((kw.get("sells") or {}).values(), key=lambda v: v["day"])
    if not sells:
        return []
    fx = usdkrw_history() if any(v["cls"] == "us" for v in sells) else {}
    ev = [(b["ts"][:10].replace("-", ""), 0, b) for b in buys] + [(v["day"], 1, v) for v in sells]
    ev.sort(key=lambda e: (e[0], e[1]))
    pos, out = {}, []
    for day, side, v in ev:
        hid = "%s:%s" % (v["cls"], v["symbol"])
        st = pos.setdefault(hid, {"qty": 0.0, "cost": 0.0})
        if side == 0:
            st["qty"] += v["qty"]
            st["cost"] += (v.get("funds") or 0) + (v.get("fee") or 0)
            continue
        avg = st["cost"] / st["qty"] if st["qty"] > 0 else None
        if (v["cls"], _day_iso(day)[:10], v["symbol"]) not in have:
            rate = _fx_on(fx, day) if v["cls"] == "us" and fx else 1
            proceeds = v.get("proceeds") if v["cls"] == "kr" else (v.get("proceeds_usd") or 0) * (rate or 0)
            known = avg is not None and st["qty"] >= v["qty"] * 0.999 and proceeds
            out.append({"id": v["id"], "ts": _day_iso(day), "holding_id": hid, "symbol": v["symbol"], "name": v["name"],
                        "cls": v["cls"], "side": "sell", "qty": v["qty"], "funds": proceeds or None, "fee": 0,
                        "price": proceeds / v["qty"] if proceeds else None, "avg": avg,
                        "realized": (proceeds - v["qty"] * avg) if known else None, "calc": True})
        if st["qty"] > 0:
            left = max(0.0, st["qty"] - v["qty"])
            st["cost"] *= left / st["qty"]
            st["qty"] = left
    return out


def kiwoom_history_view(led):
    kw = led.get("kiwoom") or {}
    realized = [{"id": key, "ts": _day_iso(v["day"]), "holding_id": "%s:%s" % (v["cls"], v["symbol"]),
                 "symbol": v["symbol"], "name": v["name"], "cls": v["cls"], "side": "sell", "qty": v["qty"],
                 "funds": v["funds"], "fee": v["fee"], "price": v["price"], "avg": v["avg"],
                 "realized": v["realized"], **{k: v[k] for k in ("funds_usd", "price_usd", "avg_usd", "realized_usd") if k in v}}
                for key, v in (kw.get("realized") or {}).items()]
    realized.sort(key=lambda f: f["ts"], reverse=True)
    flows = sorted((kw.get("flows") or {}).values(), key=lambda f: f["ts"], reverse=True)
    summary = {"deposit_krw": 0.0, "withdraw_krw": 0.0, "coin_in_krw": 0.0, "coin_out_krw": 0.0,
               "unknown": 0, "coin_count": 0, "count": len(flows)}
    for f in flows:
        if f["krw"] is None:
            summary["unknown"] += 1
        else:
            summary["deposit_krw" if f["kind"] == "deposit" else "withdraw_krw"] += f["krw"]
    summary["net"] = summary["deposit_krw"] - summary["withdraw_krw"]
    buys = [dict({k: v for k, v in b.items() if k != "day"}, ts=_day_iso(b["day"]), side="buy",
                 holding_id="%s:%s" % (b["cls"], b["symbol"])) for b in (kw.get("buys") or {}).values()]
    # 미국 주식 매수는 키움이 달러 금액만 준다. 그날 원/달러 종가로 원화를 추정해 채운다 (fx_est 표시).
    need = [b for b in buys if not b.get("funds") and b.get("funds_usd")]
    if need:
        hist = usdkrw_history()
        for b in need:
            rate = _fx_on(hist, b["ts"][:10].replace("-", "")) if hist else None
            if rate:
                b["funds"] = round(b["funds_usd"] * rate)
                b["price"] = b["funds"] / b["qty"] if b.get("qty") else None
                b["fx_est"] = rate
    realized = _fill_old_sells(kw, realized, buys) + realized
    realized.sort(key=lambda f: f["ts"], reverse=True)
    buys.sort(key=lambda f: f["ts"], reverse=True)
    income = [dict({k: v for k, v in i.items() if k != "day"}, ts=_day_iso(i["day"]))
              for i in (kw.get("income") or {}).values()]
    income.sort(key=lambda f: f["ts"], reverse=True)
    return {"realized": realized[:800], "buys": buys[:1000], "flows": flows[:400], "flows_summary": summary,
            "income": income[:600]}


def kiwoom_pending(hist, holdings, usdkrw):
    """잔고의 '오늘 체결 수량'으로 아직 거래내역에 안 잡힌 매매를 임시 행으로 만든다 (pending, 금액은 현재가 추정).
    같은 종목·방향의 실제 행이 앞뒤 하루 안에 그만큼 잡혀 있으면 만들지 않는다."""
    now = datetime.now(KST)
    out = {"buys": [], "realized": []}
    for h in holdings:
        if h["cls"] not in ("kr", "us"):
            continue
        # 미국장은 한국 시간 밤부터 새벽까지라 정오 전이면 전날 장으로 본다
        day = (now.date() - timedelta(days=1) if h["cls"] == "us" and now.hour < 12 else now.date()).strftime("%Y%m%d")
        near = {(datetime.strptime(day, "%Y%m%d").date() + timedelta(days=k)).strftime("%Y%m%d") for k in (-1, 0, 1)}
        for side, key, qty in (("buy", "buys", h.get("today_buy") or 0), ("sell", "realized", h.get("today_sell") or 0)):
            if qty <= 0:
                continue
            seen = sum(r["qty"] for r in hist.get(key) or [] if r.get("symbol") == h["symbol"] and r["ts"][:10].replace("-", "") in near)
            if seen >= qty:
                continue
            q = qty - seen
            rate = usdkrw if h["ccy"] == "USD" else 1
            price = h["price"] * rate if h.get("price") and rate else None
            out[key].append({"id": "kwp:%s:%s:%s:%s" % (day, h["cls"], h["symbol"], side), "ts": _day_iso(day),
                             "holding_id": h["id"], "symbol": h["symbol"], "name": h["name"], "cls": h["cls"],
                             "side": side, "qty": q, "funds": round(q * price) if price else None,
                             "price": price, "realized": None, "pending": True})
    return out


_kiwoom_hot = {"until": 0.0}


def kiwoom_history(changed=False):
    """최근 60일을 새로 받아 장부에 합치고 전체를 돌려준다. 과거분은 --backfill-kiwoom 이 채운다.
    오늘 체결(매수·실현손익)은 따로 자주 받는다. 보유 수량이 바뀌면(changed) 바로 받고,
    그 뒤 10분 동안은 키움 내역에 늦게 잡히는 체결을 놓치지 않게 15초마다 다시 받는다."""
    def load():
        end = datetime.now(KST).date()
        start = end - timedelta(days=59)
        flows, _, income = kiwoom_flow_rows(start, end)
        return (start, end, kiwoom_us_realized(start, end) + kiwoom_kr_realized(start, end),
                flows, kiwoom_buy_rows(start, end) + kiwoom_rights_rows(start, end), income, kiwoom_sell_rows(start, end))

    def load_today():
        day = datetime.now(KST).date()
        return day, kiwoom_us_realized(day, day) + kiwoom_kr_realized(day, day), kiwoom_buy_rows(day, day)
    start, end, realized, flows, buys, income, sells = cached("kiwoom_history", 600, load)
    if changed:
        _kiwoom_hot["until"] = time.time() + 600
        _cache.pop("kiwoom_today", None)
    day, t_realized, t_buys = cached("kiwoom_today", 15 if time.time() < _kiwoom_hot["until"] else 120, load_today)
    with _ledger_lock:
        led = ledger_load()
        before = json.dumps(led.get("kiwoom"), sort_keys=True)
        kiwoom_merge(led, start, end, realized, flows, buys, income, sells)
        if _cache["kiwoom_today"][0] >= _cache["kiwoom_history"][0]:   # 60일 분보다 새로 받은 오늘 분이면 덮는다
            kiwoom_merge(led, day, day, t_realized, buys=t_buys)
        if json.dumps(led.get("kiwoom"), sort_keys=True) != before:
            ledger_save(led)
        return kiwoom_history_view(led)


def backfill_kiwoom():
    """키움 과거 실현손익과 입출금을 허용 구간씩 거슬러 올라가며 받는다. (--backfill-kiwoom)"""
    today = datetime.now(KST).date()
    led = {"kiwoom": {}}
    end, empty, n_us = today, 0, 0
    while empty < 4 and end.year >= 2010:            # 미국: 6개월 구간
        start = end - timedelta(days=179)
        rows = kiwoom_us_realized(start, end)
        kiwoom_merge(led, start, end, realized=rows or None)
        n_us += len(rows)
        empty = 0 if rows else empty + 1
        end = start - timedelta(days=1)
    end, n_kr = today, 0
    for _ in range(40):                              # 국내: 90일 구간, 약 10년
        start = end - timedelta(days=89)
        rows = kiwoom_kr_realized(start, end)
        kiwoom_merge(led, start, end, realized=rows or None)
        n_kr += len(rows)
        end = start - timedelta(days=1)
    end, n_fl, labels, n_buy, n_inc = today, 0, {}, 0, 0
    for _ in range(12):                              # 입출금: 1년 구간
        start = end - timedelta(days=364)
        rows, seen, income = kiwoom_flow_rows(start, end)
        for k, v in seen.items():
            labels[k] = labels.get(k, 0) + v
        kiwoom_merge(led, start, end, flows=rows, income=income)
        n_inc += len(income)
        n_fl += len(rows)
        buys = kiwoom_buy_rows(start, end)             # 매수 체결: 같은 1년 구간
        buys = buys + kiwoom_rights_rows(start, end)   # 유상증자로 받은 주식도 매수로 넣는다
        kiwoom_merge(led, start, end, buys=buys)
        n_buy = n_buy + len(buys)
        kiwoom_merge(led, start, end, sells=kiwoom_sell_rows(start, end))   # 매도 체결: 옛 실현손익을 채우는 데 쓴다
        end = start - timedelta(days=1)
    with _ledger_lock:
        fresh = ledger_load()
        fresh["kiwoom"] = led["kiwoom"]
        fresh["kiwoom"]["backfilled_at"] = datetime.now(KST).isoformat()
        ledger_save(fresh)
    print("- 키움 실현손익: 미국 %d건, 국내 %d건 | 매수 체결 %d건 | 외부 입출금 %d건 | 배당·이자 %d건" % (n_us, n_kr, n_buy, n_fl, n_inc))
    print("- 입출금 적요 종류(건수):", ", ".join("%s %d" % kv for kv in sorted(labels.items(), key=lambda kv: -kv[1])))


# ─────────────────────────────────────────
# 공개 시세 (전일 종가·종목명 보완, 환율 대체)
# ─────────────────────────────────────────

BENCHMARKS = (("spx", "S&P 500", "^GSPC"), ("kospi", "코스피", "^KS11"), ("btc", "비트코인", "BTC-KRW"))


def benchmark_series():
    """벤치마크 일봉 종가 2년치. {key: {name, dates: [YYYY-MM-DD], closes: [...]}}. 6시간 캐시."""
    def load():
        out = {}
        for key, name, sym in BENCHMARKS:
            url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=2y&interval=1d" % urllib.parse.quote(sym)
            status, data, _ = http("GET", url)
            try:
                res = data["chart"]["result"][0]
                offset = int(res["meta"].get("gmtoffset") or 0)
                closes = res["indicators"]["quote"][0]["close"]
                dates, vals = [], []
                for ts, c in zip(res["timestamp"], closes):
                    if c is None:
                        continue
                    d = datetime.fromtimestamp(ts + offset, timezone.utc).strftime("%Y-%m-%d")
                    if dates and dates[-1] == d:
                        vals[-1] = round(c, 4)
                    else:
                        dates.append(d)
                        vals.append(round(c, 4))
                out[key] = {"name": name, "dates": dates, "closes": vals}
            except (TypeError, KeyError, IndexError, ValueError):
                log("벤치마크 조회 실패:", sym, status)
        return out
    return cached("benchmarks", 6 * 3600, load)


# ── 포트폴리오 위험 지표: 지금 비중을 최근 1년 일별 시세에 대입한다 (실제 성과가 아니라 지금 구성의 성격) ──

def yahoo_daily(sym, rng="2y"):
    """야후 일봉 종가. {YYYY-MM-DD: 종가}. 없으면 빈 dict. 6시간 캐시."""
    def load():
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=1d" % (urllib.parse.quote(sym), rng)
        status, data, _ = http("GET", url)
        try:
            res = data["chart"]["result"][0]
            off = int(res["meta"].get("gmtoffset") or 0)
            out = {}
            for ts, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]):
                if c:
                    out[datetime.fromtimestamp(ts + off, timezone.utc).strftime("%Y-%m-%d")] = c
            return out
        except (TypeError, KeyError, IndexError, ValueError):
            return {}
    return cached("ydaily:%s:%s" % (sym, rng), 6 * 3600, load)


def _holding_history(h):
    """보유 종목의 원화 기준 일별 종가. 미국 주식은 그날 원/달러 종가로 환산한다."""
    if h["cls"] == "kr":
        for suffix in (".KS", ".KQ"):
            d = yahoo_daily(h["symbol"] + suffix)
            if d:
                return d
        return {}
    if h["cls"] == "crypto":
        return yahoo_daily(h["symbol"] + "-KRW")
    d = yahoo_daily(h["symbol"].replace(".", "-"))
    fx = usdkrw_history()
    if not d or not fx:
        return {}
    fd = sorted(fx)
    out, j, rate = {}, 0, None
    for day in sorted(d):
        while j < len(fd) and fd[j] <= day:
            rate = fx[fd[j]]
            j += 1
        if rate:
            out[day] = d[day] * rate
    return out


def _mean(v):
    return sum(v) / len(v) if v else 0.0


def _std(v):
    if len(v) < 2:
        return 0.0
    m = _mean(v)
    return (sum((x - m) ** 2 for x in v) / (len(v) - 1)) ** 0.5


def _cov(a, b):
    ma, mb = _mean(a), _mean(b)
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (len(a) - 1)


def _metrics(r, rf, dates):
    """일간 수익률 목록 → 연 수익률, 변동성, 샤프, 소르티노, 최대 낙폭, 칼마, VaR·CVaR(95%)."""
    n = len(r)
    nav, peak, mdd, peak_at, mdd_from, mdd_to = 1.0, 1.0, 0.0, dates[0], None, None
    for x, day in zip(r, dates[1:]):
        nav *= 1 + x
        if nav > peak:
            peak, peak_at = nav, day
        if nav / peak - 1 < mdd:
            mdd, mdd_from, mdd_to = nav / peak - 1, peak_at, day
    ann = nav ** (252.0 / n) - 1
    vol = _std(r) * 252 ** 0.5
    down = (sum(min(x, 0.0) ** 2 for x in r) / n) ** 0.5 * 252 ** 0.5
    srt = sorted(r)
    k = max(1, int(n * 0.05))
    return {"ret": ann, "total": nav - 1, "vol": vol, "sharpe": (_mean(r) * 252 - rf) / vol if vol else None,
            "sortino": (_mean(r) * 252 - rf) / down if down else None, "mdd": mdd, "mdd_from": mdd_from, "mdd_to": mdd_to,
            "calmar": ann / abs(mdd) if mdd else None, "var95": -srt[k - 1], "cvar95": -_mean(srt[:k]),
            "dd_now": nav / peak - 1, "dd_peak_at": peak_at}   # 마지막 날 기준 1년 고점 대비 낙폭과 그 고점 날짜


# 보완 후보: 지금 구성과 성격이 다른 자산군. (야후 심볼, 이름, 분류, 원화 환산 여부)
COMPLEMENTS = (
    ("TLT", "미국 장기국채 ETF (TLT)", "채권", True), ("IEF", "미국 중기국채 ETF (IEF)", "채권", True),
    ("305080.KS", "TIGER 미국채10년선물", "채권", False), ("148070.KS", "KIWOOM 국고채10년", "채권", False),
    ("GLD", "금 ETF (GLD)", "금", True), ("411060.KS", "ACE KRX금현물", "금", False),
    ("USMV", "미국 최소변동성 ETF (USMV)", "저변동·배당", True), ("SCHD", "미국 배당성장 ETF (SCHD)", "저변동·배당", True),
    ("279530.KS", "KODEX 고배당", "저변동·배당", False), ("161510.KS", "PLUS 고배당주", "저변동·배당", False),
    ("XLP", "미국 필수소비재 ETF (XLP)", "방어 업종", True), ("XLV", "미국 헬스케어 ETF (XLV)", "방어 업종", True),
    ("XLU", "미국 유틸리티 ETF (XLU)", "방어 업종", True), ("VTV", "미국 가치주 ETF (VTV)", "가치", True),
    ("069500.KS", "KODEX 200", "다른 지역", False), ("EFA", "선진국(미국 외) ETF (EFA)", "다른 지역", True),
    ("EWJ", "일본 ETF (EWJ)", "다른 지역", True), ("INDA", "인도 ETF (INDA)", "다른 지역", True),
    ("DBC", "원자재 ETF (DBC)", "원자재", True), ("DBMF", "매니지드 퓨처스 ETF (DBMF)", "추세추종", True),
    ("KMLM", "매니지드 퓨처스 ETF (KMLM)", "추세추종", True),
    ("BRK-B", "버크셔 해서웨이", "방어 개별주", True), ("KO", "코카콜라", "방어 개별주", True),
    ("PG", "P&G", "방어 개별주", True), ("JNJ", "존슨앤드존슨", "방어 개별주", True), ("WMT", "월마트", "방어 개별주", True),
    ("NEE", "넥스트에라 에너지", "방어 개별주", True), ("MCD", "맥도날드", "방어 개별주", True),
    ("033780.KS", "KT&G", "방어 개별주", False), ("030200.KS", "KT", "방어 개별주", False),
)


def portfolio_risk(holdings, cash, fx, complements=True):
    """지금 비중을 최근 252거래일 시세에 대입한 위험 지표. 비중은 매일 같은 값으로 유지한다고 본다.
    complements=False 면 보완 후보 비교(야후 조회 30여 건)를 건너뛴다 (위험 신호만 필요할 때)."""
    rate = fx.get("usdkrw") or 0
    vals = {}
    for h in holdings:
        v = h["qty"] * (h.get("price") or 0) * (rate if h["ccy"] == "USD" else 1)
        if v > 0:
            vals[h["id"]] = (h, v)
    cash_krw = sum(c["amount"] * (rate if c["ccy"] == "USD" else 1) for c in cash)
    total = sum(v for _, v in vals.values()) + cash_krw
    if total <= 0:
        return None
    bench = benchmark_series()
    spx_usd, kospi = bench.get("spx"), bench.get("kospi")
    if not spx_usd or not kospi:
        return None
    fxh = usdkrw_history()
    cal = sorted(set(spx_usd["dates"]) | set(kospi["dates"]))[-253:]   # 한국·미국 중 한 곳이라도 열린 날
    with ThreadPoolExecutor(max_workers=8) as pool:
        hist = dict(zip(vals, pool.map(lambda hid: _holding_history(vals[hid][0]), vals)))

    def aligned(series):
        """달력에 맞춘 종가(앞 값으로 채움). 첫날 값이 없으면 None."""
        keys = sorted(series)
        out, j, last = [], 0, None
        for day in cal:
            while j < len(keys) and keys[j] <= day:
                last = series[keys[j]]
                j += 1
            out.append(last)
        return out
    rets = {}
    excluded = []
    for hid, (h, v) in vals.items():
        px = aligned(hist.get(hid) or {})
        have = sum(1 for x in px if x)
        if have < len(cal) * 0.9:                      # 1년 시세가 없는 종목(최근 상장, 소액 코인 등)은 빼고 나머지로 비중을 다시 맞춘다
            excluded.append({"name": h["name"], "share": v / total})
            continue
        rets[hid] = [(b / a - 1) if a and b else 0.0 for a, b in zip(px, px[1:])]
    if not rets:
        return None
    kept = sum(vals[hid][1] for hid in rets) + cash_krw
    w = {hid: vals[hid][1] / kept for hid in rets}
    n = len(cal) - 1
    port = [sum(w[hid] * rets[hid][t] for hid in rets) for t in range(n)]

    def bench_ret(series, to_krw):
        px = aligned(dict(zip(series["dates"], series["closes"])))
        if to_krw:
            fxa = aligned(fxh)
            px = [p * f if p and f else None for p, f in zip(px, fxa)]
        return [(b / a - 1) if a and b else 0.0 for a, b in zip(px, px[1:])]
    spx, ksp = bench_ret(spx_usd, True), bench_ret(kospi, False)
    irx = yahoo_daily("^IRX", "1mo")
    rf = irx[max(irx)] / 100 if irx else 0.0

    def vs(m):
        cv, vm = _cov(port, m), _std(m) ** 2
        beta = cv / vm if vm else None
        corr = cv / (_std(port) * _std(m)) if _std(port) and _std(m) else None
        rfd = rf / 252
        alpha = ((_mean(port) - rfd) - beta * (_mean(m) - rfd)) * 252 if beta is not None else None
        diff = [a - b for a, b in zip(port, m)]
        ir = _mean(diff) / _std(diff) * 252 ** 0.5 if _std(diff) else None
        up = [(a, b) for a, b in zip(port, m) if b > 0]
        dn = [(a, b) for a, b in zip(port, m) if b < 0]
        cap = lambda xs: _mean([a for a, _ in xs]) / _mean([b for _, b in xs]) if xs and _mean([b for _, b in xs]) else None
        return {"beta": beta, "corr": corr, "alpha": alpha, "ir": ir, "up": cap(up), "down": cap(dn)}

    ids = list(rets)
    sig = {i: _std(rets[i]) for i in ids}
    cov = {(i, j): _cov(rets[i], rets[j]) for i in ids for j in ids}
    var_p = sum(w[i] * w[j] * cov[(i, j)] for i in ids for j in ids)
    ret1y = {}
    for i in ids:                                    # 같은 1년 창에서 종목별 수익률 (원화 기준)
        g = 1.0
        for x in rets[i]:
            g *= 1 + x
        ret1y[i] = g - 1
    contrib = sorted(({"id": i, "name": vals[i][0]["name"], "cls": vals[i][0]["cls"], "weight": w[i] * kept / total,
                       "risk": w[i] * sum(w[j] * cov[(i, j)] for j in ids) / var_p if var_p else 0,
                       "ret1y": ret1y[i], "ret_contrib": w[i] * kept / total * ret1y[i]} for i in ids),   # 수익 기여 = 지금 비중 × 1년 수익률
                     key=lambda x: -x["risk"])
    big = [i for i in ids if vals[i][1] / total >= 0.02]
    pairs = sorted(((cov[(a, b)] / (sig[a] * sig[b]), vals[a][0]["name"], vals[b][0]["name"])
                    for x, a in enumerate(big) for b in big[x + 1:] if sig[a] and sig[b]), reverse=True)
    mine = _metrics(port, rf, cal)

    # 위험 신호용: 큰 종목 동조화, 코인-주식 상관, 최근 VaR 초과 횟수
    sset = signal_settings()

    def corr(a, b):
        sa, sb = _std(a), _std(b)
        return _cov(a, b) / (sa * sb) if sa and sb and len(a) > 2 else None
    cw = max(10, int(sset["cohesion_window"]))
    stocks = [i for i in ids if vals[i][0]["cls"] in ("kr", "us") and vals[i][1] / total * 100 >= sset["cohesion_min_weight"]]
    cpairs = []
    for x, a in enumerate(stocks):
        for b in stocks[x + 1:]:
            c60, c1y = corr(rets[a][-cw:], rets[b][-cw:]), corr(rets[a], rets[b])
            if c60 is not None:
                cpairs.append((c60, c1y, vals[a][0]["name"], vals[b][0]["name"]))
    cpairs.sort(reverse=True)
    cohesion = {"n": len(stocks), "window": cw, "mixed": len({vals[i][0]["cls"] for i in stocks}) > 1,
                "avg": _mean([q[0] for q in cpairs]) if cpairs else None,
                "avg_1y": _mean([q[1] for q in cpairs if q[1] is not None]) if cpairs else None,
                "top": {"a": cpairs[0][2], "b": cpairs[0][3], "corr": cpairs[0][0]} if cpairs else None}
    crypto = None
    btc = next((i for i in vals if vals[i][0]["cls"] == "crypto" and vals[i][0].get("symbol") == "BTC"), None)
    xw = max(10, int(sset["crypto_window"]))
    if btc is None:
        crypto = {"weight": 0.0, "risk": 0.0, "corr": None, "window": xw}
    elif btc in rets:
        fxa0 = aligned(fxh)
        bpx = [p_ / f if p_ and f else None for p_, f in zip(aligned(hist.get(btc) or {}), fxa0)]   # 비트코인 달러 가격
        bret = [(b_ / a_ - 1) if a_ and b_ else 0.0 for a_, b_ in zip(bpx, bpx[1:])]
        spx_u = bench_ret(spx_usd, False)
        ci = next((c for c in contrib if c["id"] == btc), None)
        crypto = {"weight": ci["weight"] if ci else 0.0, "risk": ci["risk"] if ci else 0.0, "window": xw,
                  "corr": corr(bret[-xw:], spx_u[-xw:]), "corr_1y": corr(bret, spx_u)}
    bw = max(5, int(sset["breach_window"]))
    breaches = sum(1 for x in port[-bw:] if -x > mine["var95"])

    # 보완 후보: 지금 구성에 10%를 섞었다면(나머지 90%는 지금 구성) 지난 1년 위험이 어떻게 달라졌는지 같은 조건으로 비교한다
    fxa = aligned(fxh)

    def cand(c):
        sym, name, group, usd = c
        px = aligned(yahoo_daily(sym))
        if usd:
            px = [p * f if p and f else None for p, f in zip(px, fxa)]
        if sum(1 for x in px if x) < len(cal) * 0.9:
            return None
        r = [(b / a - 1) if a and b else 0.0 for a, b in zip(px, px[1:])]
        mix = [0.9 * a + 0.1 * b for a, b in zip(port, r)]
        own, after = _metrics(r, rf, cal), _metrics(mix, rf, cal)
        corr = _cov(port, r) / (_std(port) * _std(r)) if _std(port) and _std(r) else None
        return {"symbol": sym, "name": name, "group": group, "corr": corr, "ret": own["ret"], "vol": own["vol"], "mdd": own["mdd"],
                "mix_vol": after["vol"], "mix_mdd": after["mdd"], "mix_sharpe": after["sharpe"]}
    with ThreadPoolExecutor(max_workers=8) as pool:
        comps = [x for x in pool.map(cand, COMPLEMENTS if complements else ()) if x]
    comps.sort(key=lambda x: x["corr"] if x["corr"] is not None else 9)
    cash_mix = _metrics([0.9 * a for a in port], rf, cal)   # 기준: 10%를 그냥 현금으로 두었을 때 (위험이 줄어드는 몫 중 '덜 들고 있어서'인 부분)
    return {"complements": comps, "cash_mix": {"vol": cash_mix["vol"], "mdd": cash_mix["mdd"], "sharpe": cash_mix["sharpe"]},"start": cal[0], "end": cal[-1], "days": n, "rf": rf, "total_krw": total,
            "mine": mine, "spx": _metrics(spx, rf, cal), "kospi": _metrics(ksp, rf, cal),
            "vs_spx": vs(spx), "vs_kospi": vs(ksp),
            "var95_krw": mine["var95"] * total, "cvar95_krw": mine["cvar95"] * total,
            "contrib": contrib[:6], "contrib_all": contrib, "cohesion": cohesion, "crypto": crypto,
            "breaches": breaches, "breach_window": bw, "div_ratio": sum(w[i] * sig[i] for i in ids) / var_p ** 0.5 if var_p else None,
            "avg_corr": _mean([p[0] for p in pairs]) if pairs else None,
            "top_pairs": [{"a": a, "b": b, "corr": c} for c, a, b in pairs[:3]],
            "excluded": excluded}


# ── 다가오는 배당: 보유 종목의 과거 배당 주기로 다음 배당락일을 추정하고, 발표된 일정이 있으면 그것을 쓴다 ──
_divcal = {"at": 0.0, "key": None, "data": None, "running": False}
_divcal_lock = threading.Lock()


def _yahoo_dividends(ysym):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=3y&interval=1mo&events=div" % urllib.parse.quote(ysym)
    status, data, _ = http("GET", url)
    try:
        res = data["chart"]["result"][0]
    except (TypeError, KeyError, IndexError):
        return None
    off = int(res["meta"].get("gmtoffset") or 0)
    events = (res.get("events") or {}).get("dividends") or {}
    rows = sorted(((datetime.fromtimestamp(e["date"] + off, timezone.utc).date(), float(e["amount"]))
                   for e in events.values() if e.get("amount")), key=lambda x: x[0])
    return {"currency": res["meta"].get("currency"), "events": rows}


def _nasdaq_declared(symbol):
    """미국 종목의 발표된 배당 일정 (배당락일, 지급일, 주당 금액). 나스닥이 모르는 종목은 None."""
    for cls in ("stocks", "etf"):
        url = "https://api.nasdaq.com/api/quote/%s/dividends?assetclass=%s" % (urllib.parse.quote(symbol), cls)
        status, data, _ = http("GET", url, {"Accept": "application/json"})
        rows = (((data or {}).get("data") or {}).get("dividends") or {}).get("rows") if isinstance(data, dict) else None
        if rows:
            out = []
            for r in rows:
                try:
                    ex = datetime.strptime(r.get("exOrEffDate"), "%m/%d/%Y").date()
                except (TypeError, ValueError):
                    continue
                try:
                    pay = datetime.strptime(r.get("paymentDate"), "%m/%d/%Y").date()
                except (TypeError, ValueError):
                    pay = None
                out.append((ex, pay, num(r.get("amount"))))
            return out
    return None


def _build_divcal(holdings):
    today = datetime.now(KST).date()
    horizon = today + timedelta(days=31)
    out = []
    for h in holdings:
        if h["cls"] not in ("us", "kr") or not h.get("qty"):
            continue
        hist = None
        if h["cls"] == "us":
            hist = _yahoo_dividends(h["symbol"].replace(".", "-").replace("/", "-"))
        else:
            for suffix in (".KS", ".KQ"):
                hist = _yahoo_dividends(h["symbol"] + suffix)
                if hist and hist["events"]:
                    break
        events = hist["events"] if hist else []
        item = None
        if h["cls"] == "us":                         # 발표된 일정이 있으면 그것이 우선이다
            for ex, pay, amt in (_nasdaq_declared(h["symbol"]) or []):
                if (today <= ex <= horizon) or (pay and ex < today <= pay <= horizon):
                    item = {"ex": ex.isoformat(), "pay": pay.isoformat() if pay else None, "per_share": amt, "basis": "declared"}
                    break
        if not item and len(events) >= 2:            # 과거 배당 간격의 중앙값으로 다음 배당락일을 추정
            gaps = sorted((b[0] - a[0]).days for a, b in zip(events[-6:], events[-5:]))
            gap = gaps[len(gaps) // 2] if gaps else 0
            last_day, last_amt = events[-1]
            if 20 <= gap <= 380 and (today - last_day).days <= gap * 1.6 + 30:
                nxt = last_day + timedelta(days=gap)
                while nxt < today - timedelta(days=3):
                    nxt += timedelta(days=gap)
                if today - timedelta(days=3) <= nxt <= horizon:
                    item = {"ex": nxt.isoformat(), "pay": None, "per_share": last_amt, "basis": "projected",
                            "interval_days": gap, "last_ex": last_day.isoformat()}
        if item:
            ccy = "USD" if h["cls"] == "us" else "KRW"
            item.update({"id": h["id"], "symbol": h["symbol"], "name": h["name"], "cls": h["cls"],
                         "currency": ccy, "qty": h["qty"], "est_amount": round(item["per_share"] * h["qty"], 4)})
            out.append(item)
    out.sort(key=lambda x: x["ex"])
    return out


def dividend_calendar(holdings):
    """보유 종목의 30일 안 배당 예정. 첫 계산은 뒤에서 돌리고, 12시간마다 새로 만든다."""
    key = ",".join(sorted("%s:%g" % (h["id"], h.get("qty") or 0) for h in holdings if h["cls"] in ("us", "kr")))
    with _divcal_lock:
        fresh = _divcal["data"] is not None and time.time() - _divcal["at"] < 12 * 3600 and _divcal["key"] == key
        if not fresh and not _divcal["running"]:
            _divcal["running"] = True
            snapshot = [dict(h) for h in holdings]

            def work():
                try:
                    data = _build_divcal(snapshot)
                    with _divcal_lock:
                        _divcal.update(at=time.time(), key=key, data=data)
                except Exception as e:
                    log("배당 일정 계산 실패:", e)
                finally:
                    with _divcal_lock:
                        _divcal["running"] = False
            threading.Thread(target=work, daemon=True).start()
        return _divcal["data"]


def yahoo_quote(symbol, ttl=10):
    def load():
        ysym = symbol.replace(".", "-").replace("/", "-")
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=1d&range=1d" % \
              urllib.parse.quote(ysym)
        status, data, _ = http("GET", url)
        try:
            meta = data["chart"]["result"][0]["meta"]
        except (TypeError, KeyError, IndexError):
            raise SourceError("%s 시세 조회 실패 (HTTP %s)" % (symbol, status))
        return {
            "price": num(meta.get("regularMarketPrice")) or None,
            "prev_close": num(meta.get("chartPreviousClose") or meta.get("previousClose")) or None,
            "name": meta.get("longName") or meta.get("shortName") or "",
            "time": meta.get("regularMarketTime"),   # 마지막 체결 시각 (epoch 초)
        }
    return cached("yahoo:" + symbol, ttl, load)


def naver_kr_prev_close(code):
    def load():
        status, data, _ = http(
            "GET", "https://polling.finance.naver.com/api/realtime/domestic/stock/" + code)
        try:
            d = data["datas"][0]
        except (TypeError, KeyError, IndexError):
            raise SourceError("%s 전일가 조회 실패 (HTTP %s)" % (code, status))
        close = num(d.get("closePrice"))
        ratio = abs(num(d.get("fluctuationsRatio")))
        direction = str((d.get("compareToPreviousPrice") or {}).get("name") or "")
        if direction in ("FALLING", "LOWER_LIMIT"):
            ratio = -ratio
        return close / (1 + ratio / 100.0) if close > 0 else None
    return cached("naver:" + code, 900, load)


def naver_kr_daily(code):
    """네이버 일봉 종가 [(YYYY-MM-DD, 종가)]. 정규장 뒤 시간외(넥스트레이드)까지 반영된 종가라 키움 현재가와 맞는다."""
    def load():
        req = urllib.request.Request("https://fchart.stock.naver.com/sise.nhn?symbol=%s&timeframe=day&count=5&requestType=0" % code,
                                     headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
                text = res.read().decode("euc-kr", "replace")
        except (urllib.error.URLError, OSError) as e:
            raise SourceError("네이버 일봉 조회 실패: %s" % getattr(e, "reason", e))
        out = []
        for row in text.split('data="')[1:]:
            f = row.split('"')[0].split("|")
            if len(f) >= 5 and num(f[4]) > 0:
                out.append(("%s-%s-%s" % (f[0][:4], f[0][4:6], f[0][6:8]), num(f[4])))
        return sorted(out)
    return cached("naverday:" + code, 600, load)


def kr_session_base(code, prev):
    """국내 종목 등락률의 기준가.
    키움 전일가는 밤사이 그날 종가로 넘어가서, 장이 닫힌 뒤·개장 전·휴장일에는 '오늘 등락'이 시간외 움직임 몇 원만 남는다.
    오늘 거래가 없었거나(개장 전, 주말, 휴장일) 시간외까지 끝난 20시 이후에는 마지막 거래일의 전 거래일 종가를 기준으로
    돌려 마지막 거래일 등락을 보여 준다. 그 밖의 시간(장중)에는 키움 값을 그대로 쓴다."""
    try:
        bars = naver_kr_daily(code)
    except SourceError as e:
        log("국내 일봉 실패", code, e)
        return prev
    if len(bars) < 2:
        return prev
    now = datetime.now(KST)
    if bars[-1][0] != now.strftime("%Y-%m-%d") or now.hour >= 20:
        return bars[-2][1]
    return prev


def fetch_fx(kiwoom_ready):
    def load():
        if kiwoom_ready:
            try:
                return {"usdkrw": kiwoom_fx(), "source": "kiwoom"}
            except SourceError as e:
                log("키움 환율 조회 실패, 공개 시세로 대체:", e)
        q = yahoo_quote("KRW=X")
        if not q["price"]:
            raise SourceError("환율 조회 실패")
        return {"usdkrw": q["price"], "source": "yahoo"}
    return cached("fx", 60, load)


# ─────────────────────────────────────────
# 포트폴리오 조립
# ─────────────────────────────────────────

_last_good = {}


def run_source(name, fn, debug, slot=None):
    slot = slot or name   # 시세만 받은 값과 전체 값을 따로 둔다 (시세만 받은 직전 값으로 체결 내역이 비지 않게)
    try:
        data = fn(debug)
        _last_good[slot] = (time.time(), data)
        return data, {"configured": True, "ok": True, "error": None,
                      "at": int(time.time() * 1000), "stale": False}
    except Exception as e:  # 한 소스의 실패가 나머지를 막지 않게 한다
        log(name, "실패:", e)
        prev = _last_good.get(slot)
        message = str(e) if isinstance(e, SourceError) else "%s: %s" % (type(e).__name__, e)
        return (prev[1] if prev else None), {
            "configured": True, "ok": False, "error": message[:300],
            "at": int(prev[0] * 1000) if prev else None, "stale": bool(prev)}


def build_portfolio(debug=None, light=False):
    """light=True 면 잔고·시세·환율만 읽는다. 대시보드의 10초 갱신이 이걸 쓰고, 체결·원장·배당·위험 지표는 가끔만 받는다."""
    debug = debug if debug is not None else {}
    light = light or LIGHT
    upbit_ready = bool(env("UPBIT_ACCESS_KEY") and env("UPBIT_SECRET_KEY"))
    kiwoom_ready = bool(env("KIWOOM_APP_KEY") and env("KIWOOM_SECRET_KEY"))
    not_set = {"configured": False, "ok": False, "error": None, "at": None, "stale": False}

    jobs = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        if upbit_ready:
            jobs["upbit"] = pool.submit(run_source, "upbit", lambda d: fetch_upbit(d, light), debug, "upbit:quotes" if light else "upbit")
        if kiwoom_ready:
            jobs["kiwoom_kr"] = pool.submit(run_source, "kiwoom_kr", fetch_kiwoom_kr, debug)
            jobs["kiwoom_us"] = pool.submit(run_source, "kiwoom_us", fetch_kiwoom_us, debug)
        fx_job = pool.submit(fetch_fx, kiwoom_ready)

        holdings, cash, sources, fills, flows, flows_summary = [], [], {}, None, None, None
        balance_fx = None
        for name in ("kiwoom_kr", "kiwoom_us", "upbit"):
            if name not in jobs:
                sources[name] = dict(not_set)
                continue
            data, status = jobs[name].result()
            sources[name] = status
            if data:
                holdings.extend(dict(h) for h in data["holdings"])
                cash.extend(dict(c) for c in data["cash"])
                balance_fx = data.get("fx_rate") or balance_fx
                if data.get("fills") is not None:
                    fills = data["fills"]
                if data.get("flows") is not None:
                    flows, flows_summary = data["flows"], data["flows_summary"]

        try:
            fx = fx_job.result()
        except Exception as e:
            log("환율 실패:", e)
            fx = {"usdkrw": None, "source": None}
        if balance_fx:   # 키움 잔고가 쓰는 환율을 그대로 써야 키움 앱의 평가금액과 맞는다
            fx = {"usdkrw": balance_fx, "source": "kiwoom"}
        fx = dict(fx)
        try:   # 전일 환율: 공개 시세의 하루 변화율을 지금 환율에 적용한다 (두 출처의 수준 차이가 환율 효과로 잡히지 않게)
            q = yahoo_quote("KRW=X", 60)
            if fx.get("usdkrw") and q["price"] and q["prev_close"]:
                fx["prev_usdkrw"] = round(fx["usdkrw"] * q["prev_close"] / q["price"], 4)
        except SourceError as e:
            log("전일 환율 실패:", e)

        # 전일 종가와 종목명 보완. 실패해도 등락률만 비고 평가금액은 그대로다.
        def fill(h):
            try:
                if h["cls"] == "us":
                    # 전일 종가와 종목명은 하루 동안 그대로라 길게 캐시한다. 키움 현재가가 없을 때만 자주 조회한다.
                    q = yahoo_quote(h["symbol"], 900 if h["price"] else 15)
                    h["prev_close"] = q["prev_close"]
                    h["price"] = h["price"] or q["price"]
                    if h["name"] == h["symbol"] and q["name"]:
                        h["name"] = q["name"]
                elif h["cls"] == "kr":
                    if not h["prev_close"]:
                        h["prev_close"] = naver_kr_prev_close(h["symbol"])
                    h["prev_close"] = kr_session_base(h["symbol"], h["prev_close"])
            except Exception as e:
                log("시세 보완 실패", h["id"], e)
        list(pool.map(fill, [h for h in holdings if h["cls"] in ("us", "kr")]))

    kiwoom_hist = None   # 키움 실현손익·입출금. 실패해도 잔고는 그대로 돌려준다.
    if light:
        return {"ts": int(time.time() * 1000), "fx": fx, "holdings": holdings, "cash": cash, "sources": sources, "view": "quotes",
                "alert_settings": alert_settings(), "push_ready": bool(env("NTFY_TOPIC"))}
    if kiwoom_ready and sources.get("kiwoom_us", {}).get("ok"):
        try:
            kiwoom_hist = kiwoom_history(qty_changed("kiwoom", [h for h in holdings if h["cls"] in ("kr", "us")]))
            pend = kiwoom_pending(kiwoom_hist, holdings, fx.get("usdkrw"))
            kiwoom_hist["buys"] = pend["buys"] + kiwoom_hist["buys"]
            kiwoom_hist["realized"] = pend["realized"] + kiwoom_hist["realized"]
        except Exception as e:
            log("키움 실현손익·입출금 조회 실패:", e)

    try:
        div_upcoming = dividend_calendar(holdings)
    except Exception as e:
        log("배당 일정 실패:", e)
        div_upcoming = None
    try:
        bench = benchmark_series()
    except Exception as e:
        log("벤치마크 실패:", e)
        bench = None
    try:
        risk = cached("risk", 600, lambda: portfolio_risk(holdings, cash, fx))   # 10분마다 비중을 다시 반영한다
    except Exception as e:
        log("위험 지표 실패:", e)
        risk = None

    out = {"ts": int(time.time() * 1000), "fx": fx, "holdings": holdings,
            "cash": cash, "sources": sources,
            # 업비트 체결 내역(최신순). None 이면 조회 불가라는 뜻이다.
            "fills": fills[:600] if fills is not None else None,
            "realized_total": sum(f["realized"] or 0 for f in fills) if fills is not None else None,
            # 업비트 입출금 내역과 순입금액 요약. None 이면 조회 불가.
            "flows": flows, "flows_summary": flows_summary,
            # 키움 실현손익(매도 건별)과 외부 입출금. None 이면 조회 불가.
            "kiwoom": kiwoom_hist,
            # 벤치마크 일봉 종가 (S&P 500, 코스피, 비트코인 원화). None 이면 조회 불가.
            "bench": bench,
            # 지금 비중을 최근 1년 시세에 대입한 위험 지표(변동성, 샤프, 베타, 낙폭 등). None 이면 계산 불가.
            "risk": risk,
            # 30일 안 배당 예정 (보유 종목). None 이면 아직 계산 중이거나 조회 불가.
            "div_upcoming": div_upcoming,
            # 이 Mac에 저장된 알림 설정과 폰 푸시 준비 여부
            "alert_settings": alert_settings(), "push_ready": bool(env("NTFY_TOPIC")), "view": "full"}
    # 위험 신호 세 가지 (위 risk 와 오늘 움직임으로 계산, 추가 조회 없음). 요약은 장중 점검·--signals 가 다시 쓴다.
    try:
        rs = save_risk_summary(risk) if risk and _all_sources_ok(out) else (risk_summary(risk) or _read_json(SIGNAL_RISK_FILE, None))
        out["signals"] = signals_payload(rs, out)
    except Exception as e:
        log("위험 신호 실패:", e)
        out["signals"] = None
    return out


def get_portfolio(view="full"):
    if view == "quotes":
        return cached("portfolio:quotes", 2, lambda: build_portfolio(light=True))
    return cached("portfolio", 2, build_portfolio)


# ─────────────────────────────────────────
# 장중 알림: 하루 등락만 보고 ntfy 로 폰에 보낸다 (launchd 가 15분마다 --alerts 로 실행)
# ─────────────────────────────────────────

def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def alert_settings():
    saved = _read_json(ALERT_FILE, {})
    out = dict(ALERT_DEFAULT)
    for k in ALERT_DEFAULT:
        if k in saved:
            out[k] = saved[k]
    targets = saved.get("targets") or []   # [{"id", "symbol", "pnl_pct" 또는 "price"+"dir", "created_at", "done_at"?}]
    for i, t in enumerate(targets):
        t.setdefault("id", "t%d" % i)
    out["targets"] = targets
    out["signals"] = signal_settings(saved)
    return out


def pnl_rate(h, fx):
    """평단 대비 수익률. 키움이 계산한 원화 평가손익이 있으면 그것(환율 포함), 없으면 가격 기준."""
    if isinstance(h.get("pnl_krw"), (int, float)):
        cost = h["qty"] * h["avg_krw"] if h.get("avg_krw") else (h["qty"] * h["avg"] * (fx if h.get("ccy") == "USD" else 1) if h.get("avg") else 0)
        if cost:
            return h["pnl_krw"] / cost
    if h.get("avg") and h.get("price"):
        return h["price"] / h["avg"] - 1
    return None


def _price_text(v, ccy):
    return "$%s" % format(round(v, 2), ",.2f") if ccy == "USD" else "%s원" % format(round(v), ",")


def target_alerts(p, cfg):
    """목표 알림: 평단 대비 수익률(pnl_pct) 또는 가격(price, dir=above|below). 도달하면 한 번만."""
    fx = (p.get("fx") or {}).get("usdkrw") or 0
    by_symbol = {(h.get("symbol") or "").upper(): h for h in p.get("holdings") or []}
    out = []
    for i, t in enumerate(cfg.get("targets") or []):
        if t.get("done_at"):
            continue
        h = by_symbol.get(str(t.get("symbol") or "").upper())
        if not h:
            continue
        if t.get("price"):
            goal, price = float(t["price"]), h.get("price")
            up = t.get("dir") != "below"
            if price and ((up and price >= goal) or (not up and price <= goal)):
                out.append({"index": i, "text": "목표가 도달: %s 현재 %s (목표 %s %s)" % (
                    alert_label(h), _price_text(price, h.get("ccy")), _price_text(goal, h.get("ccy")), "이상" if up else "이하")})
            continue
        r = pnl_rate(h, fx)
        goal = float(t.get("pnl_pct") or 0)
        if r is not None and goal and ((goal > 0 and r * 100 >= goal) or (goal < 0 and r * 100 <= goal)):
            out.append({"index": i, "text": "목표 도달: %s 평단 대비 %s (목표 %s)" % (alert_label(h), pct_short(r * 100), pct_short(goal, 0 if goal == int(goal) else 1))})
    return out


def _clean_targets(raw, old):
    """대시보드가 보낸 목표 목록을 검증한다. 조건이 같은 기존 목표는 '보냄' 기록(done_at)을 이어받는다."""
    prev = {t.get("id"): t for t in old}
    out = []
    for t in (raw if isinstance(raw, list) else [])[:50]:
        if not isinstance(t, dict):
            continue
        sym = str(t.get("symbol") or "").strip().upper()[:20]
        if not sym:
            continue
        x = {"id": str(t.get("id") or "")[:40] or "t%d%d" % (int(time.time() * 1000), len(out)), "symbol": sym,
             "created_at": str(t.get("created_at") or datetime.now(KST).isoformat())[:40]}
        try:
            if t.get("price") not in (None, "") and float(t["price"]) > 0:
                x["price"], x["dir"] = float(t["price"]), ("below" if t.get("dir") == "below" else "above")
            elif t.get("pnl_pct") not in (None, "") and float(t["pnl_pct"]) and -100 < float(t["pnl_pct"]) <= 10000:
                x["pnl_pct"] = float(t["pnl_pct"])
            else:
                continue
        except (TypeError, ValueError):
            continue
        p = prev.get(x["id"])
        if p and p.get("done_at") and all(p.get(k) == x.get(k) for k in ("symbol", "price", "dir", "pnl_pct")):
            x["done_at"] = p["done_at"]
        out.append(x)
    return out


def save_alert_settings(args):
    cur = alert_settings()
    for k, v in (args or {}).items():
        if k in ("enabled", "push", "digest"):
            cur[k] = bool(v)
        elif k in ("day_move_pct", "total_day_pct", "impact_pct", "min_weight_pct"):
            try:
                cur[k] = max(0.0, min(100.0, float(v)))
            except (TypeError, ValueError):
                pass
        elif k == "targets":
            cur["targets"] = _clean_targets(v, cur["targets"])
        elif k == "signals" and isinstance(v, dict):
            sig = dict(cur["signals"])
            for sk, sv in v.items():
                if sk == "push":
                    sig[sk] = bool(sv)
                elif sk in SIGNAL_DEFAULT:
                    try:
                        sig[sk] = max(0.0, min(1000.0, float(sv)))
                    except (TypeError, ValueError):
                        pass
            cur["signals"] = sig
    _write_json(ALERT_FILE, cur)
    return cur


def ntfy_send(message, title="Axis 알림", tags="chart_with_upwards_trend"):
    """ntfy 로 폰에 알림을 보낸다. 주제(NTFY_TOPIC)는 api-keys.txt 에 있다. 성공하면 True."""
    topic = env("NTFY_TOPIC")
    if not topic:
        log("NTFY_TOPIC 이 없어 알림을 보내지 않았습니다")
        return False
    req = urllib.request.Request("https://ntfy.sh/" + urllib.parse.quote(topic), data=message.encode("utf-8"), method="POST")
    req.add_header("Title", title.encode("utf-8").decode("latin-1"))
    req.add_header("Tags", tags)
    req.add_header("User-Agent", UA)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            return 200 <= res.status < 300
    except (urllib.error.URLError, OSError) as e:
        log("ntfy 전송 실패:", e)
        return False


def alert_label(h):
    """알림에 쓰는 종목 표기: 미국은 '노틸러스 ($NAUT)', 국내는 '삼성전자 (005930)', 코인은 '비트코인 (BTC)'."""
    sym = h.get("symbol") or ""
    tag = ("$" + sym) if h.get("cls") == "us" else sym
    name = h.get("name") or sym
    return "%s (%s)" % (name, tag) if tag and name != sym else (tag or name)


def won_short(v):
    """알림용 짧은 원화 표기: +31만원, −4,800원, +1.2억원."""
    a, s = abs(v), "+" if v > 0 else "−" if v < 0 else ""
    if a >= 1e8:
        return "%s%.2f억원" % (s, a / 1e8)
    if a >= 1e4:
        return "%s%s만원" % (s, format(round(a / 1e4), ","))
    return "%s%s원" % (s, format(round(a), ","))


def pct_short(v, d=1):
    """알림용 퍼센트 표기: +4.2%, −9.0% (원화 표기와 같은 마이너스 기호)."""
    return ("+" if v > 0 else "−" if v < 0 else "") + ("%." + str(d) + "f%%") % abs(v)


def day_view(p):
    """하루 손익. 종목 손익은 주가 움직임(지금 환율로 환산)만 담고, 미국주식의 환율 효과는 fx_day 로 따로 모은다.
    total 은 현금을 포함한 총 평가금액, 평가금액 1,000원 미만 종목은 rows 에서 뺀다."""
    fxd = p.get("fx") or {}
    fx = fxd.get("usdkrw") or 0
    fx0 = fxd.get("prev_usdkrw") or fx
    rows, total, stock, fx_day, base = [], 0.0, 0.0, 0.0, 0.0
    for h in p.get("holdings") or []:
        usd = h.get("ccy") == "USD"
        r = fx if usd else 1
        price, prev, qty = h.get("price"), h.get("prev_close"), h.get("qty") or 0
        if not price or not r:
            continue
        value = qty * price * r
        total += value
        if value < 1000 or not prev:
            continue
        d = qty * (price - prev) * r
        stock += d
        base += qty * prev * (fx0 if usd else 1)
        if usd:
            fx_day += qty * prev * (fx - fx0)
        rows.append({"h": h, "value": value, "day": d, "move": price / prev - 1})
    for c in p.get("cash") or []:
        total += (c.get("amount") or 0) * (fx if c.get("ccy") == "USD" else 1)
    return {"rows": rows, "total": total, "stock_day": stock, "fx_day": fx_day, "day": stock + fx_day, "base": base}


# ─────────────────────────────────────────
# 위험 신호 세 가지: 내 포트폴리오의 지난 1년과 비교한 사실만 (매수·매도 판단은 담지 않는다)
#   concentration  한 종목이 포트폴리오 변동(분산)에서 차지하는 몫
#   drawdown       1년 고점 대비 지금 낙폭이 지난 1년 최대 낙폭의 몇 % 인지
#   daily_move     오늘 총자산 움직임이 하루 VaR95 의 몇 배인지
# ─────────────────────────────────────────

def signal_settings(cfg=None):
    saved = (cfg if cfg is not None else _read_json(ALERT_FILE, {})).get("signals") or {}
    out = dict(SIGNAL_DEFAULT)
    for k, v in saved.items():
        if k == "push":
            out[k] = bool(v)
        elif k in SIGNAL_DEFAULT:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass
    out["_basis"] = SIGNAL_BASIS        # 기준값마다 근거 표시([문헌]·[설계]·[수학]). 설정 파일에 함께 저장된다.
    return out


RISK_SUMMARY_VER = 2


def risk_summary(risk):
    """위험 신호에 필요한 값만 (비율만, 금액 없음)."""
    if not risk or not risk.get("mine"):
        return None
    m = risk["mine"]
    keep = ("id", "name", "cls", "weight", "risk", "ret1y", "ret_contrib")
    return {"ver": RISK_SUMMARY_VER, "at": time.time(), "start": risk.get("start"), "end": risk.get("end"), "days": risk.get("days"),
            "mdd": m.get("mdd"), "mdd_from": m.get("mdd_from"), "mdd_to": m.get("mdd_to"), "port_total": m.get("total"),
            "dd_now": m.get("dd_now"), "dd_peak_at": m.get("dd_peak_at"), "var95": m.get("var95"), "vol": m.get("vol"),
            "breaches": risk.get("breaches"), "breach_window": risk.get("breach_window"),
            "cohesion": risk.get("cohesion"), "crypto": risk.get("crypto"),
            "contrib": [{k: c.get(k) for k in keep} for c in (risk.get("contrib_all") or risk.get("contrib") or [])]}


def _all_sources_ok(p):
    return all(v.get("ok") for v in (p.get("sources") or {}).values() if v.get("configured"))


def save_risk_summary(risk):
    s = risk_summary(risk)
    if s:
        try:
            _write_json(SIGNAL_RISK_FILE, s)
        except OSError as e:
            log("위험 신호 요약 저장 실패:", e)
    return s


def load_risk_summary(p, max_age=12 * 3600):
    """저장된 요약이 max_age 안이면 그것을, 아니면 보완 후보 없이 위험 지표를 다시 계산해 저장한다."""
    s = _read_json(SIGNAL_RISK_FILE, None)
    if isinstance(s, dict) and s.get("ver") == RISK_SUMMARY_VER and time.time() - (s.get("at") or 0) < max_age and s.get("var95") is not None:
        return s
    if not _all_sources_ok(p):                      # 일부 계좌만 읽힌 상태로 비중을 계산해 저장하지 않는다
        return s if isinstance(s, dict) and s.get("ver") == RISK_SUMMARY_VER else None
    try:
        risk = portfolio_risk(p.get("holdings") or [], p.get("cash") or [], p.get("fx") or {}, complements=False)
    except Exception as e:
        log("위험 신호용 위험 지표 실패:", e)
        return None
    return save_risk_summary(risk)


def _josa(word, with_final, without_final):
    """받침 있는 한글로 끝나면 with_final, 아니면 without_final."""
    c = (word or " ").rstrip(" )")[-1:] or " "
    return with_final if "가" <= c <= "힣" and (ord(c) - 0xAC00) % 28 else without_final


def _md(ymd):
    try:
        y, m, d = str(ymd).split("-")
        return "%d월 %d일" % (int(m), int(d))
    except ValueError:
        return "—"


def _pct(x, d=1):
    return ("%." + str(d) + "f%%") % (x * 100)


def _pp(x):
    """수익 기여 표기: +3.1%p, −0.4%p (x 는 비율)."""
    return ("+" if x > 0 else "−" if x < 0 else "") + "%.1f%%p" % abs(x * 100)


_RANK = {"green": 0, "yellow": 1, "red": 2}
_STEP = {"yellow": "주의", "red": "경고", "green": "정상"}       # 푸시·화면 낭독용 단계 이름 (색깔 단어는 쓰지 않는다)
_TITLE = {"yellow": "Axis · 주의", "red": "Axis · 경고", "green": "Axis · 정상 복귀"}
SIGNAL_QUESTIONS = {
    "concentration": "이 종목 하나가 결과의 4분의 1을 좌우해도 괜찮다고 미리 정했는가? 다음 이벤트가 실패해도 계획이 유지되는가?",
    "cohesion": "큰 종목들이 서로 다른 이유로 움직이는가, 같은 테마에 함께 걸려 있는가?",
    "drawdown": "이런 상황에서 하겠다고 미리 적어 둔 것이 있는가? 손실을 만회하려고 위험을 늘리고 싶은가?",
    "daily_loss": "이 손실은 한 종목에서 나왔는가, 여러 종목이 같이 빠졌는가?",
    "crypto_equity": "비트코인이 지금 주식과 다르게 움직이고 있는가?",
}


def _lv(x, y, r):
    """값이 클수록 나쁜 지표의 단계."""
    return None if x is None else "red" if x >= r else "yellow" if x >= y else "green"


def _worst(*levels):
    lv = [x for x in levels if x in _RANK]
    return max(lv, key=lambda x: _RANK[x]) if lv else None


def compute_signals(rs, p, cfg=None):
    """rs: risk_summary, p: 포트폴리오 응답(하루 손실용). 신호 5개 목록. 계산할 수 없는 신호는 status=None.
    status_pad 는 내려가는 판정용(값에 완충을 더해 다시 판정한 단계). 사실만 담고 매매 판단은 담지 않는다."""
    t = signal_settings(cfg)
    H = SIGNAL_HYSTERESIS
    rs = rs or {}
    out = []

    def sig(key, label, status, pad, **kw):
        d = {"key": key, "label": label, "status": status, "status_pad": pad if status else None,
             "question": SIGNAL_QUESTIONS[key]}
        d.update(kw)
        d.setdefault("value", None)
        d.setdefault("value_text", "—")
        out.append(d)

    # 1) 위험 집중: 위험 기여(%)와 위험÷비중 배수(비중 3% 이상만) 중 더 나쁜 단계. 1년 수익 기여는 표시만.
    th1 = "위험 기여 주의 %g%%·경고 %g%% / 위험÷비중 주의 %g배·경고 %g배 (비중 %g%% 이상)" % (
        t["concentration_yellow"], t["concentration_red"], t["concentration_ratio_yellow"], t["concentration_ratio_red"], t["concentration_ratio_min_weight"])
    best = None
    for c in rs.get("contrib") or []:
        share, w = c.get("risk") or 0, c.get("weight") or 0
        ratio = share / w if w and w * 100 >= t["concentration_ratio_min_weight"] else None
        lv = _worst(_lv(share * 100, t["concentration_yellow"], t["concentration_red"]),
                    _lv(ratio, t["concentration_ratio_yellow"], t["concentration_ratio_red"]) if ratio else "green")
        pad = _worst(_lv(share * 100 + H["concentration"], t["concentration_yellow"], t["concentration_red"]),
                     _lv(ratio + H["concentration_ratio"], t["concentration_ratio_yellow"], t["concentration_ratio_red"]) if ratio else "green")
        rank = (_RANK[lv], share)
        if best is None or rank > best[0]:
            best = (rank, c, lv, pad, ratio)
    if best:
        _, c, lv, pad, ratio = best
        share, w, rc, pt = c["risk"] or 0, c["weight"] or 0, c.get("ret_contrib"), rs.get("port_total")
        name = c["name"]
        rc_txt = ", 지난 1년 수익 기여 %s" % _pp(rc) if rc is not None else ""
        ratio_txt = ", 위험÷비중 %.1f배" % ratio if ratio and ratio >= t["concentration_ratio_yellow"] else ""
        detail = "%s의 위험이 비중의 %.1f배입니다." % (name, share / w) if w else "%s의 비중을 알 수 없습니다." % name
        if pt is not None:
            detail += " 포트폴리오 전체 1년 수익률은 %s입니다." % pct_short(pt * 100)
        sig("concentration", "위험 집중", lv, pad, value=round(share * 100, 1), value_text=_pct(share), name=name,
            weight=round(w * 100, 1), ratio=round(ratio, 2) if ratio else None,
            ret_contrib_pp=round(rc * 100, 1) if rc is not None else None, port_ret_1y=round(pt * 100, 1) if pt is not None else None,
            metrics_text="위험 %s · 비중 %s%s" % (_pct(share), _pct(w), " · 1년 수익 기여 %s" % _pp(rc) if rc is not None else ""),
            threshold=th1, detail=detail,
            fact="%s%s 포트폴리오 위험의 %s를 차지합니다 (비중 %s%s%s)." % (name, _josa(name, "이", "가"), _pct(share), _pct(w), ratio_txt, rc_txt),
            short="%s %s" % (name, _pct(share)))
    else:
        sig("concentration", "위험 집중", None, None, threshold=th1, detail="위험 기여를 계산하지 못했습니다.")

    # 2) 큰 종목 동조화: 비중 5% 이상 주식끼리 60거래일 평균 상관
    co = rs.get("cohesion")
    th2 = "비중 %g%% 이상 주식의 %d일 평균 상관 주의 %g·경고 %g" % (t["cohesion_min_weight"], t["cohesion_window"], t["cohesion_yellow"], t["cohesion_red"])
    if co and co.get("n", 0) >= 2 and co.get("avg") is not None:
        a = co["avg"]
        tp = co.get("top") or {}
        detail = "비중 %g%% 이상 %d종목, %d일 평균 상관(1년 평균 %s)." % (
            t["cohesion_min_weight"], co["n"], co.get("window") or t["cohesion_window"], "%.2f" % co["avg_1y"] if co.get("avg_1y") is not None else "—")
        if tp:
            detail += " 최고 짝 %s·%s %.2f." % (tp["a"], tp["b"], tp["corr"])
        if co.get("mixed"):
            detail += " 한·미 장 시차로 낮게 잡힐 수 있습니다."
        sig("cohesion", "큰 종목 동조화", _lv(a, t["cohesion_yellow"], t["cohesion_red"]),
            _lv(a + H["cohesion"], t["cohesion_yellow"], t["cohesion_red"]), value=round(a, 2), value_text="%.2f" % a,
            n=co["n"], avg_1y=co.get("avg_1y"), top_pair=tp or None, threshold=th2, detail=detail,
            fact="비중 %g%% 이상 주식 %d종목의 최근 %d거래일 평균 상관이 %.2f입니다 (1년 평균 %s%s)." % (
                t["cohesion_min_weight"], co["n"], co.get("window") or t["cohesion_window"], a,
                "%.2f" % co["avg_1y"] if co.get("avg_1y") is not None else "—",
                ", 가장 높은 짝 %s·%s %.2f" % (tp["a"], tp["b"], tp["corr"]) if tp else ""),
            short="평균 상관 %.2f" % a)
    elif co is not None and co.get("n", 0) < 2:
        sig("cohesion", "큰 종목 동조화", "green", "green", value_text="—", threshold=th2,
            detail="비중 %g%% 이상 주식이 2종목 미만이라 볼 짝이 없습니다." % t["cohesion_min_weight"], fact="", short="대상 2종목 미만")
    else:
        sig("cohesion", "큰 종목 동조화", None, None, threshold=th2, detail="상관을 계산하지 못했습니다.")

    # 3) 낙폭: 고점 대비 절대 낙폭(15/25%)과 1년 최대 낙폭 대비 비율(50/80%) 중 더 나쁜 단계
    dd, mdd = rs.get("dd_now"), rs.get("mdd")
    th3 = "고점 대비 주의 −%g%%·경고 −%g%% / 1년 최대 낙폭의 주의 %g%%·경고 %g%%" % (
        t["drawdown_abs_yellow"], t["drawdown_abs_red"], t["drawdown_yellow"], t["drawdown_red"])
    if dd is not None and mdd:
        absdd, frac = abs(dd) * 100, abs(dd) / abs(mdd) * 100
        lv = _worst(_lv(absdd, t["drawdown_abs_yellow"], t["drawdown_abs_red"]), _lv(frac, t["drawdown_yellow"], t["drawdown_red"]))
        pad = _worst(_lv(absdd + H["drawdown_abs"], t["drawdown_abs_yellow"], t["drawdown_abs_red"]),
                     _lv(frac + H["drawdown"], t["drawdown_yellow"], t["drawdown_red"]))
        ddt = "−" + _pct(abs(dd)) if dd < 0 else "0.0%"
        sig("drawdown", "낙폭", lv, pad, value=round(dd * 100, 1), value_text=ddt, mdd=round(mdd * 100, 1),
            ratio=round(frac / 100, 2), peak_at=rs.get("dd_peak_at"), asof=rs.get("end"), threshold=th3,
            detail="1년 고점(%s) 대비 %s로, 최대 낙폭 −%s의 %d%%입니다." % (_md(rs.get("dd_peak_at")), ddt, _pct(abs(mdd)), round(frac)),
            fact="포트폴리오가 1년 고점(%s) 대비 %s입니다 (지난 1년 최대 낙폭 −%s의 %d%%)." % (_md(rs.get("dd_peak_at")), ddt, _pct(abs(mdd)), round(frac)),
            short="고점 대비 %s" % ddt)
    else:
        sig("drawdown", "낙폭", None, None, threshold=th3, detail="낙폭을 계산하지 못했습니다.")

    # 4) 하루 손실: 손실 쪽만. 오늘 손실 ÷ VaR95 (1.5/2배)와 최근 20거래일 VaR 초과 횟수(3/5회) 중 더 나쁜 단계
    var95, br = rs.get("var95"), rs.get("breaches")
    win = int(rs.get("breach_window") or t["breach_window"])
    v = day_view(p) if p else None
    move = v["day"] / v["base"] if v and v["base"] else None
    th4 = "하루 손실이 VaR95의 주의 %g배·경고 %g배 / 최근 %d거래일 VaR 초과 주의 %d회·경고 %d회" % (
        t["daily_yellow"], t["daily_red"], win, t["breach_yellow"], t["breach_red"])
    if var95 and (move is not None or br is not None):
        mult = max(0.0, -move) / var95 if move is not None else 0.0
        lv_day = _lv(mult, t["daily_yellow"], t["daily_red"])
        lv_cnt = _lv(br, t["breach_yellow"], t["breach_red"]) if br is not None else "green"
        pad_day = _lv(mult + H["daily_loss"], t["daily_yellow"], t["daily_red"])
        mv = pct_short(move * 100) if move is not None else "—"
        day_txt = ("오늘 총자산 %s(하루 VaR95 %s의 %.1f배)" % (mv, _pct(var95), mult)) if move is not None and move < 0 else "오늘 총자산 %s(손실 아님)" % mv
        sig("daily_loss", "하루 손실", _worst(lv_day, lv_cnt), _worst(pad_day, lv_cnt),
            value=round(move * 100, 2) if move is not None else None, value_text=mv, var95=round(var95 * 100, 1),
            ratio=round(mult, 2), breaches=br, breach_window=win, parts={"day": lv_day, "count": lv_cnt}, threshold=th4,
            detail="%s. 최근 %d거래일 VaR 초과 %s회." % (day_txt, win, br if br is not None else "—"),
            fact=("최근 %d거래일 중 하루 손실이 VaR95(%s)를 넘은 날이 %s회입니다 (오늘 총자산 %s)." % (win, _pct(var95), br, mv)
                  if lv_cnt != "green" and _RANK[lv_cnt] >= _RANK[lv_day or "green"] else
                  "오늘 총자산이 %s 내렸습니다 (하루 VaR95 %s의 %.1f배, 최근 %d거래일 VaR 초과 %s회)." % (
                      mv, _pct(var95), mult, win, br if br is not None else "—")),
            short="최근 %d거래일 VaR 초과 %s회" % (win, br if br is not None else "—"))
    else:
        sig("daily_loss", "하루 손실", None, None, threshold=th4, detail="하루 손실을 계산하지 못했습니다.")

    # 5) 코인-주식 상관: 비트코인과 S&P 500의 60거래일 상관, 비트코인 위험 기여 대 비중
    cr = rs.get("crypto")
    th5 = "%d일 상관 주의 %g·경고 %g, 위험 기여가 비중보다 크면 주의(상관 경고 기준과 겹치면 경고)" % (
        t["crypto_window"], t["crypto_corr_yellow"], t["crypto_corr_red"])
    if cr and cr.get("corr") is not None and cr.get("weight"):
        c, w, rk = cr["corr"], cr["weight"], cr.get("risk") or 0

        def lvc(cc, over):
            if cc >= t["crypto_corr_red"] and over:
                return "red"
            return "yellow" if cc >= t["crypto_corr_yellow"] or over else "green"
        lv = lvc(c, rk > w)
        pad = lvc(c + H["crypto_corr"], rk > w * (1 - H["crypto_ratio"]))
        sig("crypto_equity", "코인-주식 상관", lv, pad, value=round(c, 2), value_text="%.2f" % c,
            weight=round(w * 100, 1), risk=round(rk * 100, 1), threshold=th5,
            detail="비트코인과 S&P 500의 %d거래일 상관 %.2f. 비트코인 비중 %s, 위험 기여 %s." % (cr.get("window") or t["crypto_window"], c, _pct(w), _pct(rk)),
            fact="비트코인과 S&P 500의 최근 %d거래일 상관이 %.2f입니다 (비트코인 비중 %s, 위험 기여 %s)." % (
                cr.get("window") or t["crypto_window"], c, _pct(w), _pct(rk)),
            short="상관 %.2f, 위험 기여 %s·비중 %s" % (c, _pct(rk), _pct(w)))
    elif cr is not None and not cr.get("weight"):
        sig("crypto_equity", "코인-주식 상관", "green", "green", value_text="—", threshold=th5,
            detail="비트코인을 보유하고 있지 않습니다.", fact="", short="보유 없음")
    else:
        sig("crypto_equity", "코인-주식 상관", None, None, threshold=th5, detail="상관을 계산하지 못했습니다.")
    return out


def signals_payload(rs, p, cfg=None):
    return {"asof": datetime.now(KST).isoformat(timespec="minutes"), "risk_end": (rs or {}).get("end"),
            "partial": not _all_sources_ok(p) if p else True,   # 일부 계좌를 못 읽었으면 하루 손실이 불완전하다
            "signals": compute_signals(rs, p, cfg)}


def _settle(sig, prev):
    """완충을 둔 단계. 올라가는 판정은 즉시, 내려가는 판정은 완충을 더한 값(status_pad)도 내려가야 한다."""
    now = sig["status"]
    if prev not in _RANK or _RANK[now] >= _RANK[prev]:
        return now
    pad = sig.get("status_pad") if sig.get("status_pad") in _RANK else now
    return pad if _RANK[pad] < _RANK[prev] else prev


def _push_text(sg, prev, cur):
    """(제목, 본문). 색깔 단어와 대괄호 없이, 사실 한 줄 + 점검 질문 한 줄. 해제는 한 줄."""
    subj = sg["label"] + _josa(sg["label"], "이", "가")
    if _RANK[cur] > _RANK[prev]:
        return _TITLE[cur], "%s\n점검: %s" % (sg.get("fact") or sg.get("detail") or "", sg["question"])
    if cur == "green":
        return _TITLE["green"], "%s %s 단계에서 벗어났습니다 (%s)." % (subj, _STEP[prev], sg.get("short") or sg.get("value_text"))
    return _TITLE[cur], "%s %s 단계에서 %s 단계로 내려왔습니다 (%s).\n점검: %s" % (
        subj, _STEP[prev], _STEP[cur], sg.get("short") or sg.get("value_text"), sg["question"])


def signal_transitions(p, cfg, state=None, sent=None):
    """신호 단계가 지난번 알린 단계와 달라졌을 때만 알림을 만든다. (알림 목록, 새 상태) 를 돌려준다.
    state 가 None(상태 파일 없음, 첫 실행)이면 지금 단계를 저장만 하고 알리지 않는다. 오늘 이미 보낸 신호 알림
    (alerts-sent.json 의 sig:<날짜>:<신호>:<단계>)이 있으면 그 단계로 저장한다.
    하루 손실은 날이 바뀌어 오늘 손실 몫이 저절로 풀린 경우 해제 알림 없이 상태만 낮추고, VaR 초과 횟수가 줄어 내려갈 때만 알린다.
    계좌 소스가 하나라도 실패했거나 계산이 불완전하면 상태를 바꾸지 않는다(알림 없음, 새 상태 None)."""
    if not _all_sources_ok(p):
        return [], None
    rs = load_risk_summary(p)
    if not rs:
        return [], None
    sigs = compute_signals(rs, p, cfg)
    stamp = datetime.now(KST).isoformat(timespec="minutes")
    keys = {sg["key"] for sg in sigs}

    def rec(sg, status):
        r = {"status": status, "at": stamp}
        if sg.get("parts"):
            r["count"] = sg["parts"].get("count")
        return r
    if state is None:
        day = datetime.now(KST).date().isoformat()
        sent_keys = {x.get("key") for x in (sent or [])}
        new = {}
        for sg in sigs:
            if sg["status"] not in _RANK:
                continue
            told = [lv for lv in ("red", "yellow") if "sig:%s:%s:%s" % (day, sg["key"], lv) in sent_keys]
            new[sg["key"]] = rec(sg, _worst(told[0], sg["status"]) if told else sg["status"])
        return [], new
    new = {k: v for k, v in state.items() if k in keys}  # 없어진 신호(예: 예전 daily_move)는 지운다
    out = []
    for sg in sigs:
        if sg["status"] not in _RANK:                 # 계산 못 한 신호는 상태를 그대로 둔다
            continue
        old = state.get(sg["key"]) or {}
        prev = old.get("status")
        cur = _settle(sg, prev)
        if prev not in _RANK:                         # 새로 생긴 신호: 저장만
            new[sg["key"]] = rec(sg, cur)
            continue
        if cur == prev:
            if sg.get("parts") and old.get("count") != sg["parts"].get("count"):
                new[sg["key"]] = rec(sg, cur)
            continue
        new[sg["key"]] = rec(sg, cur)
        if _RANK[cur] < _RANK[prev] and sg["key"] == "daily_loss":
            was = old.get("count") if old.get("count") in _RANK else prev
            if _RANK[sg["parts"].get("count") or "green"] >= _RANK[was]:
                continue                              # 하루가 지나 오늘 손실 몫만 풀렸다: 조용히 낮춘다
        title, body = _push_text(sg, prev, cur)
        out.append({"key": sg["key"], "from": prev, "to": cur, "title": title, "text": body})
    return out, new


def _stage(x, threshold):
    """기준의 1배, 2배, 4배, 8배… 를 넘을 때마다 한 단계씩 올라간다. 기준 미만이면 0."""
    if threshold <= 0 or abs(x) < threshold:
        return 0
    s, t = 1, threshold * 2
    while abs(x) >= t and s < 8:
        s, t = s + 1, t * 2
    return s


def compute_alerts(p, cfg):
    """하루 움직임 알림 목록. 종목은 두 조건 중 하나라도 넘으면 알린다.
    1) 등락률이 day_move_pct 이상 (비중이 min_weight_pct 이상인 종목만: 작은 종목의 출렁임은 거른다)
    2) 그 종목의 오늘 손익이 총자산의 impact_pct 이상 (비중이 큰 종목은 작게 움직여도 알린다)
    stage 는 기준의 2배, 4배를 넘을 때마다 오른다. 같은 움직임이 더 커지면 다시 알리는 데 쓴다."""
    v = day_view(p)
    out, total = [], v["total"]
    if not total:
        return out
    for x in v["rows"]:
        h, w, impact = x["h"], x["value"] / total * 100, x["day"] / total * 100
        s_move = _stage(x["move"] * 100, cfg.get("day_move_pct") or 0) if w >= (cfg.get("min_weight_pct") or 0) else 0
        s_imp = _stage(impact, cfg.get("impact_pct") or 0)
        stage = max(s_move, s_imp)
        if not stage:
            continue
        out.append({"key": "d:%s:%g" % (h["id"], round(h["prev_close"], 2)), "id": h["id"], "dir": "up" if x["move"] > 0 else "down",
                    "stage": stage, "text": "%s %s · %s (총자산 %s)" % (alert_label(h), pct_short(x["move"] * 100), won_short(x["day"]), pct_short(impact, 2))})
    if v["base"]:
        rate = v["day"] / v["base"] * 100
        stage = _stage(rate, cfg.get("total_day_pct") or 0)
        if stage:
            d = "up" if v["day"] > 0 else "down"
            out.append({"key": "total:%s:%s" % (datetime.now(KST).date().isoformat(), d), "id": "total", "dir": d, "stage": stage,
                        "text": "총자산 %s · %s" % (pct_short(rate), won_short(v["day"]))})
    return out


def _us_traded_today(holdings):
    """미국 장이 오늘(뉴욕 날짜) 열렸는지: 보유 미국 종목의 마지막 체결 시각으로 본다."""
    ny = datetime.now(NY)
    for h in holdings:
        if h.get("cls") != "us":
            continue
        try:
            t = yahoo_quote(h["symbol"], 900).get("time")
        except SourceError:
            continue
        if t:
            return datetime.fromtimestamp(t, NY).date() == ny.date()
    return False


def _kr_traded_today(holdings):
    today = datetime.now(KST).strftime("%Y-%m-%d")
    for h in holdings:
        if h.get("cls") != "kr":
            continue
        try:
            bars = naver_kr_daily(h["symbol"])
        except SourceError:
            continue
        if bars:
            return bars[-1][0] == today
    return False


def market_digests(p):
    """장 마감 요약. 국내장은 15:40~18:00(한국 시간), 미국장은 16:05~18:30(뉴욕 시간) 사이에 하루 한 번.
    그 시간에 Mac이 잠들어 있으면 건너뛴다. 휴장일(그날 체결이 없는 날)도 건너뛴다."""
    v, out = day_view(p), []
    kst, ny = datetime.now(KST), datetime.now(NY)
    hs = p.get("holdings") or []
    plan = []
    if kst.weekday() < 5 and (15, 40) <= (kst.hour, kst.minute) < (18, 0) and _kr_traded_today(hs):
        plan.append(("kr", "국내장 마감", "국내주식", kst.date().isoformat()))
    if ny.weekday() < 5 and (16, 5) <= (ny.hour, ny.minute) < (18, 30) and _us_traded_today(hs):
        plan.append(("us", "미국장 마감", "미국주식", ny.date().isoformat()))
    for cls, title, label, day in plan:
        rows = sorted((x for x in v["rows"] if x["h"].get("cls") == cls), key=lambda x: x["day"])
        if not rows:
            continue
        seg = sum(x["day"] for x in rows) + (v["fx_day"] if cls == "us" else 0)
        base = sum(x["value"] - x["day"] for x in rows)
        parts = ["%s %s (%s)" % (label, won_short(seg), pct_short(seg / base * 100 if base else 0, 2))]
        if cls == "us" and abs(v["fx_day"]) >= 1:
            parts[0] += " · 환율 효과 %s" % won_short(v["fx_day"])
        fmt = lambda x: "%s %s %s" % (alert_label(x["h"]), pct_short(x["move"] * 100), won_short(x["day"]))
        if rows[-1]["day"] > 0:
            parts.append("상위 " + fmt(rows[-1]))
        if rows[0]["day"] < 0:
            parts.append("하위 " + fmt(rows[0]))
        if v["base"]:
            parts.append("총자산 오늘 %s" % pct_short(v["day"] / v["base"] * 100, 2))
        out.append({"key": "digest:%s:%s" % (cls, day), "title": "Axis " + title, "text": " · ".join(parts)})
    return out


def run_alerts():
    """--alerts: 장중 점검 (launchd 가 5분마다). 새 알림이 있으면 ntfy 로 보낸다."""
    cfg = alert_settings()
    if not cfg["enabled"] or not cfg["push"]:
        print("알림 꺼짐")
        return
    global LIGHT
    LIGHT = True
    p = build_portfolio({})
    if not any(v.get("ok") for v in (p.get("sources") or {}).values()):
        print("조회 실패")
        return
    hits = target_alerts(p, cfg)                     # 목표 알림은 따로, 도달하면 바로 보내고 끝낸다
    for hit in hits:
        if ntfy_send(hit["text"], title="Axis 목표 알림", tags="dart"):
            saved = _read_json(ALERT_FILE, {})
            if hit["index"] < len(saved.get("targets") or []):
                saved["targets"][hit["index"]]["done_at"] = datetime.now(KST).isoformat()
                _write_json(ALERT_FILE, saved)
            print(hit["text"] + " → 보냄")
    now = time.time()
    raw = _read_json(SENT_FILE, [])
    sent = [x for x in (raw if isinstance(raw, list) else []) if now - x.get("ts", 0) < 3 * 86400]
    keys = {x["key"] for x in sent}
    if cfg.get("digest", True):                      # 장 마감 요약
        for d in market_digests(p):
            if d["key"] in keys:
                continue
            if ntfy_send(d["text"], title=d["title"], tags="bell"):
                sent.append({"key": d["key"], "id": "digest", "dir": "", "stage": 1, "ts": now})
                keys.add(d["key"])
                _write_json(SENT_FILE, sent)
                print(d["title"] + " → 보냄")
    try:                                             # 위험 신호: 단계가 바뀔 때만 (올라감·내려감 모두 한 번, 신호마다 따로)
        state = _read_json(SIGNAL_STATE_FILE, None)
        state = state if isinstance(state, dict) else None
        sig, new_state = signal_transitions(p, cfg, state, sent)
        if new_state is not None:
            for a in sig:
                if not cfg["signals"].get("push", True):
                    continue
                if ntfy_send(a["text"], title=a["title"], tags="white_check_mark" if a["to"] == "green" else "warning"):
                    print(a["title"] + " → 보냄")
                else:                                # 전송에 실패한 신호는 예전 단계로 둬서 다음 점검 때 다시 보낸다
                    new_state[a["key"]] = state[a["key"]]
                    print(a["title"] + " → 전송 실패")
            if new_state != state:
                _write_json(SIGNAL_STATE_FILE, new_state)
    except Exception as e:
        log("위험 신호 알림 실패:", e)
    # 이미 보낸 알림: 같은 장(같은 키)이거나 같은 종목·같은 방향으로 20시간 안에 보낸 것 중 가장 높은 단계보다
    # 높은 단계에 들어설 때만 다시 보낸다. 예: −5% 에서 한 번, −10% 를 넘으면 한 번 더, −20% 를 넘으면 또 한 번.
    def sent_stage(a):
        return max([x.get("stage", 1) for x in sent if x["key"] == a["key"]
                    or (x.get("id") == a["id"] and x.get("dir") == a["dir"] and now - x.get("ts", 0) < 20 * 3600)] or [0])
    fresh = []
    for a in compute_alerts(p, cfg):
        before = sent_stage(a)
        if a["stage"] > before:
            fresh.append(dict(a, text=("더 커짐 · " if before else "") + a["text"]))
    if not fresh:
        print("새 알림 없음")
        return
    texts = [a["text"] for a in fresh]
    body = "\n".join(texts[:6]) + ("\n외 %d건" % (len(texts) - 6) if len(texts) > 6 else "")
    if ntfy_send(body):
        sent.extend({"key": a["key"], "id": a["id"], "dir": a["dir"], "stage": a["stage"], "ts": now} for a in fresh)
        _write_json(SENT_FILE, sent)
        print("알림 %d건 보냄" % len(fresh))
    else:
        print("전송 실패")


# ─────────────────────────────────────────
# MCP (stdio, 줄 단위 JSON-RPC)
# ─────────────────────────────────────────

TOOLS = [{
    "name": "get_portfolio",
    "title": "보유 자산과 현재가 조회",
    "description": (
        "업비트와 키움증권(국내·미국 주식)의 보유 종목, 수량, 평단가, 현재가, 전일 종가, "
        "예수금, 원/달러 환율, 업비트 체결 내역(fills)과 실현 손익을 한 번에 돌려준다. 읽기 전용이며 주문 기능은 없다. "
        "sources 에 소스별 연결 상태와 오류가 들어 있다. 가격 단위는 각 종목의 ccy(KRW 또는 USD)다. "
        "view 를 'quotes' 로 주면 잔고·시세·환율·연결 상태만 가볍게 돌려준다(체결·원장·배당·위험 지표 제외)."),
    "inputSchema": {"type": "object", "properties": {
        "view": {"type": "string", "enum": ["full", "quotes"],
                 "description": "full(기본): 전체. quotes: 잔고와 시세만."}}, "additionalProperties": False},
    "annotations": {"readOnlyHint": True, "destructiveHint": False,
                    "idempotentHint": True, "openWorldHint": True},
}, {
    "name": "save_alert_settings",
    "title": "알림 설정 저장",
    "description": ("대시보드의 알림 설정(하루 등락 기준, 총자산 대비 손익 기준, 폰 푸시·장 마감 요약 켜기/끄기, 목표 알림 목록)을 "
                    "이 Mac에 저장한다. 장중 점검 스크립트가 이 값을 쓴다. targets 를 주면 목표 목록 전체를 그것으로 바꾼다. 계좌에는 아무것도 하지 않는다."),
    "inputSchema": {"type": "object", "properties": {
        "enabled": {"type": "boolean"}, "push": {"type": "boolean"}, "digest": {"type": "boolean"},
        "day_move_pct": {"type": "number"}, "min_weight_pct": {"type": "number"},
        "impact_pct": {"type": "number"}, "total_day_pct": {"type": "number"},
        "targets": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "symbol": {"type": "string"}, "pnl_pct": {"type": "number"},
            "price": {"type": "number"}, "dir": {"type": "string", "enum": ["above", "below"]},
            "created_at": {"type": "string"}}}},
        "signals": {"type": "object", "description": "위험 신호 기준 (키 목록은 server.py 의 SIGNAL_DEFAULT): push, concentration_*, cohesion_*, drawdown_*, daily_*, breach_*, crypto_*"}},
        "additionalProperties": False},
    "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
}]

_write_lock = threading.Lock()


def send(msg):
    with _write_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def call_tool(req_id, params):
    name = (params or {}).get("name")
    if name == "save_alert_settings":
        try:
            result = save_alert_settings((params or {}).get("arguments") or {})
            send({"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text", "text": json.dumps(result)}], "structuredContent": result, "isError": False}})
        except Exception as e:
            send({"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text", "text": "저장 실패: %s" % e}], "isError": True}})
        return
    if name != "get_portfolio":
        send({"jsonrpc": "2.0", "id": req_id,
              "error": {"code": -32602, "message": "Unknown tool: %s" % name}})
        return
    try:
        args = (params or {}).get("arguments") or {}
        result = get_portfolio("quotes" if args.get("view") == "quotes" else "full")
        send({"jsonrpc": "2.0", "id": req_id, "result": {
            "content": [{"type": "text", "text": json.dumps(result)}],
            "structuredContent": result, "isError": False}})
    except Exception as e:
        log("get_portfolio 실패:", e)
        send({"jsonrpc": "2.0", "id": req_id, "result": {
            "content": [{"type": "text", "text": "조회 실패: %s" % e}], "isError": True}})


def handle(msg):
    method, req_id = msg.get("method"), msg.get("id")
    if method == "initialize":
        version = (msg.get("params") or {}).get("protocolVersion") or "2024-11-05"
        send({"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "portfolio", "version": "1.0.0"}}})
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": req_id, "result": {}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        threading.Thread(target=call_tool, args=(req_id, msg.get("params")), daemon=True).start()
    elif req_id is not None:
        send({"jsonrpc": "2.0", "id": req_id,
              "error": {"code": -32601, "message": "Method not found: %s" % method}})


def serve():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict):
            handle(msg)


def check():
    """연결 점검. 종목 수와 필드 이름만 보여 주고 금액·수량은 출력하지 않는다."""
    debug = {}
    p = build_portfolio(debug)
    labels = {"kiwoom_kr": "키움 국내주식", "kiwoom_us": "키움 미국주식", "upbit": "업비트"}
    counts = {}
    for h in p["holdings"]:
        counts[h["cls"]] = counts.get(h["cls"], 0) + 1
    cls_of = {"kiwoom_kr": "kr", "kiwoom_us": "us", "upbit": "crypto"}
    for name, label in labels.items():
        s = p["sources"][name]
        if not s["configured"]:
            print("- %s: 키가 아직 없습니다 (api-keys.txt)" % label)
        elif s["ok"]:
            print("- %s: 연결됨, 보유 %d종목" % (label, counts.get(cls_of[name], 0)))
        else:
            print("- %s: 실패 — %s" % (label, s["error"]))
    priced = sum(1 for h in p["holdings"] if h["price"])
    print("- 현재가 확인: %d / %d 종목" % (priced, len(p["holdings"])))
    print("- 환율: %s" % ("확인됨 (%s)" % p["fx"]["source"] if p["fx"]["usdkrw"] else "실패"))
    if p["fills"] is None:
        print("- 업비트 체결 내역: 조회 불가")
    else:
        sells = [f for f in p["fills"] if f["side"] == "sell"]
        print("- 업비트 체결 내역: %d건 (매도 %d건, 그중 원가를 아는 것 %d건)" % (
            len(p["fills"]), len(sells), sum(1 for f in sells if f["realized"] is not None)))
    fs = p["flows_summary"]
    if fs is None:
        print("- 업비트 입출금 내역: 조회 불가")
    else:
        print("- 업비트 입출금 내역: %d건 (코인 %d건, 원화 환산 불가 %d건)" % (fs["count"], fs["coin_count"], fs["unknown"]))
    if p.get("div_upcoming") is None:            # 점검은 한 번만 도니까 뒤에서 도는 계산을 기다린다
        for _ in range(60):
            time.sleep(1)
            if _divcal["data"] is not None and not _divcal["running"]:
                break
        p["div_upcoming"] = _divcal["data"]
    du = p.get("div_upcoming")
    print("- 30일 안 배당 예정: %s" % ("계산 실패" if du is None else "%d건 (발표 %d, 추정 %d)" % (
        len(du), sum(1 for x in du if x["basis"] == "declared"), sum(1 for x in du if x["basis"] == "projected"))))
    bn = p.get("bench") or {}
    print("- 벤치마크: %s" % (", ".join("%s %d일" % (v["name"], len(v["dates"])) for v in bn.values()) or "조회 불가"))
    kh = p["kiwoom"]
    if kh is None:
        print("- 키움 실현손익·입출금: 조회 불가 또는 키 없음")
    else:
        print("- 키움 실현손익: %d건 (미국 %d, 국내 %d) | 외부 입출금 %d건" % (
            len(kh["realized"]), sum(1 for r in kh["realized"] if r["cls"] == "us"),
            sum(1 for r in kh["realized"] if r["cls"] == "kr"), kh["flows_summary"]["count"]))
        inc = kh.get("income") or []
        print("- 키움 배당·이자: %d건 (배당 %d, 이자 %d, 세금 %d)" % (len(inc), sum(1 for i in inc if i["kind"] == "dividend"),
              sum(1 for i in inc if i["kind"] == "interest"), sum(1 for i in inc if i["kind"] == "tax")))
    for key in sorted(debug):
        print("- %s: %s" % (key, ", ".join(debug[key])))


if __name__ == "__main__":
    if "--check" in sys.argv:
        check()
    elif "--alerts" in sys.argv:
        run_alerts()
    elif "--signals" in sys.argv:                    # 아침 브리핑이 읽는 위험 신호 (퍼센트만, 금액 없음)
        LIGHT = True
        _p = build_portfolio({})
        print(json.dumps(signals_payload(load_risk_summary(_p), _p, _read_json(ALERT_FILE, {})), ensure_ascii=False, indent=1))
    elif "--push" in sys.argv:                       # --push "메시지": 아침 브리핑 같은 작업이 폰으로 한 줄 보낼 때
        msg = sys.argv[sys.argv.index("--push") + 1] if len(sys.argv) > sys.argv.index("--push") + 1 else ""
        cfg = alert_settings()
        if msg and cfg["enabled"] and cfg["push"]:
            print("보냄" if ntfy_send(msg[:300], title="Axis") else "전송 실패")
        else:
            print("보내지 않음 (메시지 없음 또는 알림 꺼짐)")
    elif "--backfill-kiwoom" in sys.argv:
        backfill_kiwoom()
    elif "--backfill" in sys.argv:
        backfill()
    else:
        serve()
