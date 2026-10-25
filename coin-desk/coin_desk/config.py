"""전역 설정. 환경변수로 덮어쓸 수 있고, HARD_LIMITS는 어떤 에이전트도 넘을 수 없는 하드 리밋이다."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = Path(os.getenv("COIN_DESK_STATE", ROOT / "state"))
DATA_DIR = Path(os.getenv("COIN_DESK_DATA", ROOT / "data"))

SYMBOL = os.getenv("COIN_DESK_SYMBOL", "BTCUSDT")
BASE_ASSET = os.getenv("COIN_DESK_BASE", "BTC")
QUOTE = os.getenv("COIN_DESK_QUOTE", "USDT")

# 바이낸스 엔드포인트. 시세는 항상 실서버 데이터(testnet 시세는 실제와 다름), 주문은 모드별로.
BINANCE_DATA_URL = "https://api.binance.com"
BINANCE_LIVE_URL = "https://api.binance.com"
BINANCE_TESTNET_URL = "https://testnet.binance.vision"

# 거래 비용 (바이낸스 현물 기본 수수료 0.1% + 슬리피지 가정)
FEE_RATE = float(os.getenv("COIN_DESK_FEE", "0.001"))
SLIPPAGE = float(os.getenv("COIN_DESK_SLIPPAGE", "0.0005"))
COST_PER_SIDE = FEE_RATE + SLIPPAGE

# 백테스트: 마지막 30% 구간을 아웃오브샘플(OOS)로 떼어둔다
OOS_FRACTION = 0.3

# 모델 / effort (역할별로 환경변수로 교체 가능)
DEFAULT_MODEL = os.getenv("COIN_DESK_MODEL", "claude-opus-5-5")
MODELS = {
    "trader": os.getenv("COIN_DESK_MODEL_TRADER", DEFAULT_MODEL),
    "cso": os.getenv("COIN_DESK_MODEL_CSO", DEFAULT_MODEL),
    "redteam": os.getenv("COIN_DESK_MODEL_REDTEAM", DEFAULT_MODEL),
    "analyst": os.getenv("COIN_DESK_MODEL_ANALYST", DEFAULT_MODEL),
}
EFFORT = {
    "trader": os.getenv("COIN_DESK_EFFORT_TRADER", "low"),
    "cso": os.getenv("COIN_DESK_EFFORT_CSO", "high"),
    "redteam": os.getenv("COIN_DESK_EFFORT_REDTEAM", "high"),
    "analyst": os.getenv("COIN_DESK_EFFORT_ANALYST", "medium"),
}
# 서버측 refusal fallback (프록시가 거부하면 COIN_DESK_FALLBACKS=0)
USE_FALLBACKS = os.getenv("COIN_DESK_FALLBACKS", "1") == "1"
MAX_TOOL_ITERS = int(os.getenv("COIN_DESK_MAX_TOOL_ITERS", "25"))

# 리서치 회의: CSO <-> 레드팀 수정 라운드 수
DEBATE_ROUNDS = int(os.getenv("COIN_DESK_DEBATE_ROUNDS", "2"))

# 하드 리밋 — 전략서(StrategyBook)가 이 값을 넘으면 검증에서 거부/클램프
HARD_LIMITS = {
    "max_exposure": 1.0,  # 현물만, 레버리지 없음
    "kill_drawdown_max": 0.30,  # 전략서가 정할 수 있는 킬스위치 낙폭 상한
    "daily_loss_limit": float(os.getenv("COIN_DESK_DAILY_LOSS", "0.05")),
    "max_order_quote": float(os.getenv("COIN_DESK_MAX_ORDER", "1000")),  # 1회 주문 상한 (USDT)
    "min_order_quote": 10.0,  # 바이낸스 최소 주문 5 USDT + 여유
    "rebalance_band_min": 0.02,
    "rebalance_band_max": 0.25,
}

PAPER_START_QUOTE = float(os.getenv("COIN_DESK_PAPER_USDT", "10000"))

# 실행 모드: paper(모의) | testnet(바이낸스 테스트넷 실주문) | live(실거래)
# live는 COIN_DESK_MODE=live 환경변수와 CLI --live 플래그가 둘 다 있어야만 켜진다.
MODE = os.getenv("COIN_DESK_MODE", "paper")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")

STATE_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
