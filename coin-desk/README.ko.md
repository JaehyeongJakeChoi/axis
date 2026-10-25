# coin-desk — 8-에이전트 BTC 자동매매 데스크

바이낸스 BTC/USDT 현물(롱온리)을 매매하는 멀티 에이전트 시스템. 에이전트는 Claude API(`claude-opus-5-5`)로 동작한다.

## 조직도

| 에이전트 | 역할 | 하는 일 |
|---|---|---|
| agent_1 | 프랍 트레이더 | 엔진이 계산한 목표 비중을 검토하고 주문 집행. **제안 이하로만** 집행 가능(비중 확대 재량 없음) |
| agent_2 | CSO | 애널리스트 5명의 보고를 절충해 전략서(StrategyBook) 작성, 레드팀 비판에 대응 |
| agent_3 | 레드팀 | 전략서를 증거 기반으로 비판(approve / revise / reject), 운용 중 상시 모니터링, 필요하면 긴급 재검토 요청 |
| agent_4 | 애널리스트 | 추세추종 — 이동평균 교차 (`trend_ma`) |
| agent_5 | 애널리스트 | 채널 돌파 — 돈치안 (`donchian_breakout`) |
| agent_6 | 애널리스트 | 평균회귀 — RSI 과매도 (`rsi_meanrev`) |
| agent_7 | 애널리스트 | 시계열 모멘텀 + 변동성 타기팅 (`tsmom_voltarget`) |
| agent_8 | 애널리스트 | 변동성 돌파 — 래리 윌리엄스 (`vol_breakout`) |

**에이전트 간 대화**: 모든 에이전트는 `ask_agent`(즉답 질문 — 상대 에이전트가 자기 도구로 확인하고 답함)와 `send_message`/`read_inbox`(비동기 메모)를 쓴다. 대화는 전부 `state/messages.jsonl`, `state/conversations.jsonl`에 남는다.

## 흐름

```
리서치 회의 (기본 24시간마다)
  agent_4~8 병렬: 파라미터 스윕 → 강건성 점검 → submit_report (백테스트 수치는 시스템이 자동 첨부)
  agent_2: 보고서 종합 + 포트폴리오 백테스트 → 전략서 초안
  agent_3: 검토 → revise면 agent_2가 수정 (기본 2라운드)
  최종 reject면 max_exposure 50% 감축 후 발행

매매 사이클 (기본 60분마다)
  엔진: 슬리브별 신호 × 가중치 → 목표 비중 → 리스크 가드 적용
  agent_1: 검토 후 execute_rebalance / hold

레드팀 모니터링 (12시간마다) → escalate 시 긴급 리서치 회의
```

## 숫자를 지어내지 못하게

- 모든 성과 수치는 결정론적 백테스트 엔진(`backtest.py`)이 계산한다. 보고서와 전략서를 제출하면 시스템이 백테스트를 다시 돌려 수치를 붙인다.
- 마지막 30% 기간은 아웃오브샘플(OOS)이다. 스윕 결과는 IS와 OOS를 나란히 보여준다.
- 비용: 수수료 0.1%와 슬리피지 0.05%(편도)를 반영한다.

## 안전장치 (LLM이 넘을 수 없는 하드 리밋, `config.py`)

- 현물 롱온리, 최대 비중 100% (레버리지 없음)
- 킬스위치: 고점 대비 낙폭이 전략서의 `kill_drawdown`(상한 30%)에 닿으면 LLM을 거치지 않고 전량 청산한다. 새 전략서가 발행될 때까지 매수하지 않는다.
- 일일 손실이 5%를 넘으면 그날은 비중을 늘리지 않는다.
- 1회 주문 상한은 1,000 USDT(`COIN_DESK_MAX_ORDER`)다. 큰 리밸런싱은 여러 사이클에 나눠 체결된다.
- 리밸런싱 밴드 이내의 차이는 매매하지 않는다.

## 실행

```bash
cd coin-desk
uv sync
export ANTHROPIC_API_KEY=...        # Claude API 키
```

```bash
uv run python -m coin_desk --mock research   # API 비용 없이 전체 배선 점검
uv run python -m coin_desk research          # 실제 리서치 회의 → 전략서 발행
uv run python -m coin_desk trade             # 매매 1회 (기본: 페이퍼 트레이딩)
uv run python -m coin_desk run               # 상시 운용 루프
uv run python -m coin_desk status            # 전략서, 계좌, 가드, LLM 비용
uv run python -m coin_desk messages          # 에이전트 간 대화 로그
uv run python -m coin_desk ask agent_3 "지금 전략서의 가장 큰 약점은?"
```

### 대시보드

```bash
uv run python -m coin_desk web               # http://127.0.0.1:8765
```

- **조직도**: 에이전트 8명의 실시간 상태(작업 중/대기)와 지금 하는 일. 클릭하면 그 에이전트 관련 대화만 보이도록 피드를 거르고, 대화 상대로 선택된다.
- **회의실**: 리서치 회의 단계, 에이전트의 진행 메모(💭), 에이전트끼리 주고받은 질문과 답, 보고서 카드, 전략서 초안과 발행, 레드팀 판정과 지적, 체결이 실시간으로 올라온다. `도구 호출`을 켜면 백테스트 호출까지 보인다.
- **직접 대화**: 아래 입력창에서 에이전트를 골라 *질문하기*(즉답, API 비용 발생)나 *메모 남기기*(다음 작업 때 read_inbox로 읽음)를 할 수 있다. 예: CSO에게 "다음 회의 때 2022년 하락장을 따로 봐줘"
- **실행 버튼**: 전략 회의, 매매 1회, 레드팀 점검, 자동 운용 시작/중지. `모의 LLM`을 체크하면 API 비용 없이 흐름만 돌린다.
- **오른쪽 패널**: 현재 전략서, 전략서 백테스트와 BTC 보유 비교 차트(OOS 구간 음영), 애널리스트 보고 요약, 계좌, 킬스위치까지의 낙폭, 최근 체결.

CLI로 `run` 루프를 따로 돌려도 대시보드는 같은 `state/` 폴더를 읽기 때문에 그대로 실시간으로 보인다. 서버는 127.0.0.1에만 열리고, 다른 웹사이트에서 보낸 요청은 막는다(CSRF/Host 검사).

### 모드: paper → testnet → live 순서로

| 모드 | 설정 | 주문 |
|---|---|---|
| paper (기본) | 없음 | `state/paper_account.json`에 모의 체결 (시작 10,000 USDT) |
| testnet | `COIN_DESK_MODE=testnet` + 테스트넷 키 | testnet.binance.vision에 실제 주문 (가상 자금) |
| live | `COIN_DESK_MODE=live` + `--live` 플래그 + 실계좌 키 | **실제 자금** |

```bash
export BINANCE_API_KEY=... BINANCE_API_SECRET=...
```

바이낸스 API 키는 **현물 거래만 허용하고 출금 권한은 반드시 끄고**, IP 화이트리스트를 걸어서 발급할 것.

## 비용 (Claude API)

모든 역할의 기본 모델은 Claude Opus 5.5다. effort는 CSO와 레드팀 high, 애널리스트 medium, 트레이더 low. 리서치 회의 1회는 수 달러, 매매 사이클 1회는 몇 센트 수준으로 예상한다(실측은 `status`). 모델과 effort는 역할별 환경변수(`COIN_DESK_MODEL_ANALYST`, `COIN_DESK_EFFORT_TRADER` 등)로 바꿀 수 있다. `run --no-llm-trader`를 쓰면 매매 사이클에서 LLM을 쓰지 않는다.

## 주의

이 프로그램은 투자 조언이 아니다. 백테스트 성과가 미래 수익을 보장하지 않는다. 실거래 전에 페이퍼와 테스트넷에서 충분히 돌려 볼 것.
