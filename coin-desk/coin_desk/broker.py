"""주문 집행 계층. PaperBroker(모의), BinanceBroker(testnet / live).

모든 브로커는 같은 인터페이스를 가진다:
  balances() -> {"quote": USDT, "base": BTC}
  buy_quote(quote_amount, ref_price) -> fill
  sell_base(base_qty, ref_price) -> fill
"""
import hashlib
import hmac
import json
import math
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

from . import config

STEP = 0.00001  # BTCUSDT LOT_SIZE stepSize


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _floor_step(q: float, step: float = STEP) -> float:
    return math.floor(q / step) * step


class PaperBroker:
    name = "paper"

    def __init__(self, path=config.STATE_DIR / "paper_account.json"):
        self.path = path
        if path.exists():
            self.acct = json.loads(path.read_text())
        else:
            self.acct = {"quote": config.PAPER_START_QUOTE, "base": 0.0, "created": _now()}
            self._save()

    def _save(self):
        self.path.write_text(json.dumps(self.acct, indent=2))

    def balances(self) -> dict:
        return {"quote": self.acct["quote"], "base": self.acct["base"]}

    def buy_quote(self, quote_amount: float, ref_price: float) -> dict:
        quote_amount = min(quote_amount, self.acct["quote"])
        px = ref_price * (1 + config.SLIPPAGE)
        fee = quote_amount * config.FEE_RATE
        qty = _floor_step((quote_amount - fee) / px)
        self.acct["quote"] -= quote_amount
        self.acct["base"] += qty
        self._save()
        return {"side": "BUY", "qty": qty, "quote": quote_amount, "avg_price": px, "fee_quote": fee, "ts": _now(), "broker": self.name}

    def sell_base(self, base_qty: float, ref_price: float) -> dict:
        qty = _floor_step(min(base_qty, self.acct["base"]))
        px = ref_price * (1 - config.SLIPPAGE)
        gross = qty * px
        fee = gross * config.FEE_RATE
        self.acct["base"] -= qty
        self.acct["quote"] += gross - fee
        self._save()
        return {"side": "SELL", "qty": qty, "quote": gross - fee, "avg_price": px, "fee_quote": fee, "ts": _now(), "broker": self.name}


class BinanceBroker:
    """바이낸스 현물 시장가 주문. API 키는 환경변수(BINANCE_API_KEY/SECRET)에서만 읽는다.
    키는 '현물 거래'만 허용하고 출금 권한은 반드시 끈 상태로 발급할 것."""

    def __init__(self, testnet: bool):
        if not (config.BINANCE_API_KEY and config.BINANCE_API_SECRET):
            raise RuntimeError("BINANCE_API_KEY / BINANCE_API_SECRET 환경변수가 없습니다.")
        self.testnet = testnet
        self.name = "binance-testnet" if testnet else "binance-live"
        self.base_url = config.BINANCE_TESTNET_URL if testnet else config.BINANCE_LIVE_URL
        self.s = requests.Session()
        self.s.headers["X-MBX-APIKEY"] = config.BINANCE_API_KEY

    def _signed(self, method: str, path: str, params: dict) -> dict:
        params = dict(params, timestamp=int(time.time() * 1000), recvWindow=5000)
        qs = urlencode(params)
        sig = hmac.new(config.BINANCE_API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
        r = self.s.request(method, f"{self.base_url}{path}?{qs}&signature={sig}", timeout=10)
        if r.status_code >= 400:
            raise RuntimeError(f"Binance {path} {r.status_code}: {r.text}")
        return r.json()

    def balances(self) -> dict:
        acct = self._signed("GET", "/api/v3/account", {"omitZeroBalances": "true"})
        bal = {b["asset"]: float(b["free"]) for b in acct["balances"]}
        return {"quote": bal.get(config.QUOTE, 0.0), "base": bal.get(config.BASE_ASSET, 0.0)}

    def _fill(self, side: str, resp: dict) -> dict:
        qty = float(resp.get("executedQty", 0))
        quote = float(resp.get("cummulativeQuoteQty", 0))
        fee_quote = 0.0
        for f in resp.get("fills", []):
            c = float(f["commission"])
            if f["commissionAsset"] == config.QUOTE:
                fee_quote += c
            elif f["commissionAsset"] == config.BASE_ASSET:
                fee_quote += c * float(f["price"])
        return {"side": side, "qty": qty, "quote": quote, "avg_price": quote / qty if qty else None,
                "fee_quote": fee_quote, "ts": _now(), "broker": self.name,
                "order_id": resp.get("orderId"), "status": resp.get("status")}

    def buy_quote(self, quote_amount: float, ref_price: float) -> dict:
        resp = self._signed("POST", "/api/v3/order", {
            "symbol": config.SYMBOL, "side": "BUY", "type": "MARKET",
            "quoteOrderQty": f"{quote_amount:.2f}", "newOrderRespType": "FULL"})
        return self._fill("BUY", resp)

    def sell_base(self, base_qty: float, ref_price: float) -> dict:
        qty = _floor_step(base_qty)
        resp = self._signed("POST", "/api/v3/order", {
            "symbol": config.SYMBOL, "side": "SELL", "type": "MARKET",
            "quantity": f"{qty:.5f}", "newOrderRespType": "FULL"})
        return self._fill("SELL", resp)


def make_broker(live_flag: bool = False):
    mode = config.MODE
    if mode == "live":
        if not live_flag:
            raise RuntimeError("COIN_DESK_MODE=live 이지만 --live 플래그가 없습니다. 실거래를 하려면 둘 다 필요합니다.")
        return BinanceBroker(testnet=False)
    if live_flag:
        raise RuntimeError("--live 플래그가 있지만 COIN_DESK_MODE=live 가 아닙니다.")
    if mode == "testnet":
        return BinanceBroker(testnet=True)
    return PaperBroker()
