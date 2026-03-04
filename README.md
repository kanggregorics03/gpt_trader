# GPT Trader

Bybit 선물 계정과 Gemini API를 연결해 자동매매를 수행하는 프로젝트다.  
AI는 시장과 계좌 상태를 바탕으로 주문 후보를 생성하지만, 실제 리스크 통제, 주문 실행, 복구 판단, 감사 로그 저장은 애플리케이션 코드가 담당한다.

이 프로젝트의 목적은 "AI에게 전권을 주는 자동매매"가 아니라, 아래 구조를 갖는 통제 가능한 자동매매 시스템을 만드는 데 있다.

- 입력이 무엇이었는지 남긴다.
- AI가 무엇을 제안했는지 남긴다.
- 리스크 엔진이 왜 승인 또는 거절했는지 남긴다.
- 실제 주문과 체결이 어떻게 흘렀는지 남긴다.
- 나중에 같은 입력을 다시 넣었을 때 판단이 얼마나 달라졌는지 비교할 수 있게 한다.

즉, 단순 실행기보다 감사 가능성과 운영 복구 가능성을 우선하는 구조다.

## 핵심 개념

이 저장소는 다섯 축으로 이해하면 된다.

### 1. 의사결정 생성

`DecisionEngine`이 Gemini에 구조화된 입력을 보내고 `TradeIntent`를 받는다.  
AI는 전략 제안자 역할만 하며, 실제 주문 집행 권한은 없다.

### 2. 정책 기반 리스크 통제

`RiskEngine`이 정책 파일과 현재 리스크 상태를 바탕으로 주문 후보를 검증한다.  
이 단계에서 심볼, 주문 타입, 포지션 수, 노출 한도, 손실 상태, 모드별 상한을 모두 검사한다.

### 3. 실행과 복구

`ExecutionEngine`이 승인된 주문만 거래소에 전달한다.  
타임아웃이 나면 무조건 재주문하지 않고, 먼저 같은 `clientOrderId`로 복구를 시도한다.

### 4. 상태 저장과 감사

`StateStore`가 audit, journal, fills, ledger, replay, 상태 전이 기록을 SQLite에 저장한다.  
여기서 중요한 점은 "주문 후보 생성 근거 입력 해시"와 "주문/포지션 상태 머신"까지 저장한다는 것이다.

### 5. 장기 학습

일일 배치가 journal과 ledger를 바탕으로 장기 메모리를 생성한다.  
장기 메모리는 단순 격언이 아니라, 언제 적용하고 언제 무시해야 하는지를 포함한 구조화 규칙으로 저장된다.

## 시스템 목표

이 프로젝트는 아래 문제를 해결하려고 설계됐다.

- 같은 결론이 정말 같은 입력에서 나왔는지 추적하기 어렵다.
- 손실 구간에서도 동일한 공격성으로 주문이 나가는 경우가 있다.
- 주문 타임아웃, 부분 체결, 미확인 상태에서 복구 로직이 약해지기 쉽다.
- 모델이나 프롬프트를 바꾸면 무엇이 얼마나 바뀌었는지 직관적으로 알기 어렵다.
- 장기 메모리가 쌓여도 적용 조건이 없으면 운영 가치를 잃기 쉽다.

현재 구현은 위 문제를 아래 방식으로 다룬다.

- `decision_input_hash`로 입력 추적 가능성 강화
- `normal / trigger / cooldown / drawdown` 리스크 모드 도입
- 주문/포지션 상태 전이 테이블 저장
- replay diff를 수치와 키워드 단위로 저장
- 장기 메모리에 `apply_when`, `ignore_when` 강제

## 전체 아키텍처

```text
AccountReader ----\
                   \
MarketCollector ----> DecisionRequest ----> DecisionEngine ----> TradeIntent
                   /                                 |
StateStore -------/                                  v
                                  RiskEngine (policy + risk mode/profile)
                                                     |
                                        approved? ---+--- rejected?
                                           |                     |
                                           v                     v
                                  ExecutionEngine           Audit / Journal
                                           |
                                           v
                            receipts / fills / ledgers / state transitions
                                           |
                                           v
                                       StateStore
```

그리고 별도 배치 경로가 있다.

```text
journal + ledger stats -> MemoryUpdater -> long_term_memory
```

## 프로젝트 구조

```text
gpt_trader/
├─ main.py                    # 메인 오케스트레이터, 스케줄러 시작점
├─ policy.yaml                # 거래/리스크 정책
├─ .env.example               # 환경 변수 예시
├─ ai/
│  ├─ decision_engine.py      # Gemini 의사결정 생성, 입력 해시 생성
│  └─ rag_engine.py           # 유사 과거 케이스 검색
├─ batch/
│  └─ memory_updater.py       # 장기 메모리 일일 배치
├─ core/
│  ├─ models.py               # Pydantic 데이터 모델
│  └─ risk_engine.py          # 정책 검증, 리스크 모드/프로파일 계산
├─ dashboard/
│  └─ app.py                  # Flask 운영 대시보드
├─ db/
│  └─ state_store.py          # SQLite 저장/조회 계층
├─ exchange/
│  ├─ account_reader.py       # Bybit 계좌 조회
│  ├─ collector.py            # 시세/지표 수집
│  └─ execution_engine.py     # 주문 실행, 체결 추적, 상태 전이 생성
├─ tools/
│  └─ replay_audit.py         # 감사 로그 리플레이 및 diff 요약
├─ utils/
│  ├─ logger.py               # loguru 설정
│  └─ alerter.py              # 웹훅 알림
└─ tests/                     # unittest 기반 테스트
```

## 실행 흐름 상세

### 1. 계좌와 시장 상태 조회

`main.py`의 `TradingOrchestrator.run_cycle()`는 먼저 계좌 상태와 각 심볼의 시장 스냅샷을 만든다.

여기서 중요한 점은 경로가 분리되어 있다는 것이다.  
시장 데이터는 Bybit 메인넷 public API에서 받고, 계좌 조회와 주문 실행은 `BYBIT_TRADING_MODE`에 따라 live 또는 demo private API로 보낸다.

- `AccountReader`
  - 계좌 스냅샷 시각
  - 총 잔고 / 사용 가능 잔고 / 총 미실현 손익
  - 계좌 마진 비율(가능한 경우)
  - 현재 포지션 목록
    - 진입가 / 마크가 / 미실현 손익(절대값, 퍼센트)
    - 청산가 / 레버리지 / 포지션 명목가치
    - 초기증거금 / 유지증거금 / 마진 모드
  - 미체결 주문 목록
    - 방향 / 주문 타입 / 수량 / 가격
    - reduce-only / TP / SL / clientOrderId / 상태
  - 짧은 TTL 캐시를 사용해 같은 구간의 중복 private API 조회를 줄이고, 주문 실행 직후에는 캐시를 무효화
  - 가능하면 Bybit private WebSocket으로 wallet / position / order / execution을 유지하고, 주기적으로 REST reconcile
- `MarketCollector`
  - 현재가
  - ATR 15m / ATR 1h
  - 1시간 추세
  - 거래량 급증 여부
  - opportunity trigger 여부와 사유
  - risk trigger 여부와 사유
  - 가능하면 Bybit public WebSocket으로 ticker / order book / kline을 받아 REST 호출을 줄이고, 준비되지 않은 데이터만 REST fallback
  - OHLCV 롤링 캐시와 짧은 TTL 응답 캐시를 이용해 호출 수를 줄임

### 2. 현재 리스크 상태 계산

`StateStore.get_risk_state()`가 최근 audit / journal을 이용해 현재 상태를 계산한다.

- 일일 시작 잔고
- 일일 손익률
- 연속 손실 횟수
- 쿨다운 활성 여부
- 쿨다운 남은 시간
- 드로우다운 활성 여부

이 값은 단순 모니터링용이 아니라, 실제로 리스크 모드를 결정하는 입력으로 사용된다.
또한 그대로 `DecisionRequest.risk_state`에 실려 AI에도 전달된다.

### 3. 리스크 모드와 프로파일 결정

`RiskEngine`은 현재 상황을 아래 네 모드 중 하나로 분류한다.

- `normal`
- `trigger`
- `cooldown`
- `drawdown`

결정 우선순위는 다음과 같다.

1. 쿨다운이면 `cooldown`
2. 드로우다운이면 `drawdown`
3. 트리거 이벤트면 `trigger`
4. 아니면 `normal`

이후 현재 모드에 대응하는 프로파일을 읽는다.

- `max_open_positions`
- `max_order_notional_pct_balance`
- `allowed_actions`

즉, 같은 `BUY`라도 지금이 `normal`인지 `drawdown`인지에 따라 허용 여부와 크기 제한이 달라질 수 있다.

### Trigger 분리 원칙

이 프로젝트는 트리거를 두 부류로 나눈다.

- `opportunity_trigger`
  - 시장에 실제 이벤트가 발생해 더 자주 관찰하거나 대응할 가치가 있는 상황
  - 예: `fast_price_move_15m`, `volume_surge_15m`, `range_break_1h`
- `risk_trigger`
  - 거래 품질이나 포지션 보유 비용이 나빠져 신규 진입을 줄이거나 막아야 하는 상황
  - 예: `wide_spread`, `elevated_funding`

중요한 점은 `trigger mode` 자체는 기본적으로 `opportunity_trigger`를 기준으로 켜진다는 것이다.  
반면 `risk_trigger`는 AI와 리스크 엔진에 전달되어 더 보수적인 행동을 유도하고, 필요하면 신규 진입을 직접 차단한다.

`fast_price_move_15m`, `volume_surge_15m`, `range_break_1h`에는 심볼별 히스테리시스를 적용한다.  
즉, 경계값 부근에서는 직전 상태를 참고해 매 루프마다 `trigger mode`가 깜빡이는 현상을 줄인다.

현재 구현 기준의 대표 조건은 다음과 같다.

- `fast_price_move_15m`
  - 15분 변동률이 고정 0.5%와 15분 ATR% 기반 임계값 중 더 큰 값을 넘을 때 ON
  - OFF는 `max(0.3%, ON 임계값의 60%)` 미만일 때로 별도 판정
- `volume_surge_15m`
  - 최근 15분 거래량 비율이 20봉 평균 대비 1.8배 이상일 때 ON
  - 한 번 켜지면 1.35배 미만으로 내려갈 때 OFF
- `range_break_1h`
  - 1시간 20봉 범위를 ATR 기반 버퍼 이상으로 실제 이탈했을 때 ON
  - 한 번 켜지면 직전 범위 안으로 더 깊게 재진입하기 전까지 유지
- `wide_spread`
  - 호가 스프레드가 10bps 이상일 때
- `elevated_funding`
  - 펀딩비 절대값이 0.0005 이상일 때

### 4. AI 입력 구성

`DecisionRequest`는 아래를 포함한다.

- `policy_summary`
- `account_state`
- `market_snapshot` 또는 `market_snapshots`
- `risk_state`
- `recent_journal_summaries`
- `long_term_memory`
- `similar_past_cases`
- `is_trigger_mode`
- `risk_mode`
- `risk_profile`

여기서 중요한 변경점 세 가지가 있다.

첫째, AI는 현재 리스크 모드와 프로파일을 직접 입력으로 받는다.  
즉, 리스크 엔진이 나중에 막기만 하는 구조가 아니라, 애초에 더 보수적인 제안을 하도록 유도한다.

둘째, AI는 `risk_state`의 구체 수치도 함께 받는다.  
즉, 단순히 `drawdown` 같은 라벨만 보는 것이 아니라, 현재 일일 손익률, 연속 손실, 쿨다운 잔여 시간까지 보고 위험도를 해석할 수 있다.

셋째, 시장 스냅샷 안에서 `opportunity_trigger_reasons`와 `risk_trigger_reasons`를 분리해 전달한다.  
즉, “시장이 움직였다”와 “지금 거래 품질이 나쁘다”를 같은 신호로 취급하지 않는다.

넷째, 장기 메모리는 더 이상 단순 문장 리스트만이 아니다.  
규칙, 지원 근거 수, 적용 조건, 무시 조건을 가진 구조화 레코드로 전달될 수 있다.

다섯째, 멀티 심볼 운영에서는 심볼별로 AI를 여러 번 부르지 않고 한 사이클에 `market_snapshots` 전체를 묶어 포트폴리오 단일 의사결정을 생성한다.  
같은 계좌 상태, 장기 메모리, journal 문맥을 반복 전송하지 않기 때문에 API 비용이 줄고, 심볼 간 우선순위 비교도 한 번에 가능해진다.

여섯째, RAG는 최근 audit를 심볼별로 반복 스캔하지 않는다.  
audit 저장 시 시장 feature vector를 함께 저장하고, 조회 시에는 여러 심볼의 유사 케이스를 한 번의 DB 스캔으로 배치 계산한다.

### 5. 주문 후보 생성 근거 입력 해시

`DecisionEngine`은 아래 입력을 canonical JSON으로 직렬화한 뒤 SHA-256 해시를 만든다.

- 정책 요약
- 계좌 상태
- 시장 스냅샷
- 현재 리스크 상태
- 현재 리스크 모드
- 현재 리스크 프로파일

프롬프트는 compact JSON으로 직렬화되고, 완전히 같은 `prompt + model` 조합은 SQLite `decision_cache`에서 재사용된다.  
즉, 정보량을 줄이지 않으면서도 토큰 비용과 중복 API 호출을 낮춘다.

이 해시는 `decision_input_hash`로 audit에 저장된다.

이 값이 중요한 이유:

- "이 결정이 어떤 입력에서 나왔는지"를 변조나 혼동 없이 추적할 수 있다.
- 프롬프트나 모델이 바뀌어도 입력 자체가 같았는지 확인할 수 있다.
- 운영 중 비슷한 사건을 비교할 때 기준점을 제공한다.

해시는 의도적으로 "주문 후보 생성의 핵심 사실 입력"에 집중하고 있다.  
즉, 최근 journal이나 장기 메모리 같은 보조 문맥보다, 정책/시장/계좌/활성 리스크 컨텍스트의 핵심 조합을 기준으로 남긴다.

## 의사결정 모델

AI 출력은 `TradeIntent`다.

- `decision`
  - `HOLD`, `BUY`, `SELL`, `REDUCE`, `EXIT`
- `orders`
  - 각 주문 후보의 심볼, 액션, 주문 타입, 수량, 가격, `reduce_only`, TP, SL
- `rationale`
  - 왜 그런 결정을 내렸는지에 대한 짧은 근거
- `risk_notes`
  - 감지한 위험 요인
- `confidence`
  - 0.0 ~ 1.0

현재 프롬프트는 아래 성격을 가진다.

- 전략가 역할만 수행
- 유효한 JSON만 반환
- 리스크 우회 금지
- 영어 텍스트 강제
- 트리거 모드에서는 신규 진입보다 리스크 축소 우선
- `risk_trigger`가 있으면 스프레드와 펀딩 같은 거래 품질 악화를 별도 위험 신호로 해석

## 리스크 관리 상세

`RiskEngine.evaluate_intent()`는 아래 순서로 검증한다.

### 1. 안전 행동 예외

- `HOLD`는 항상 허용
- `REDUCE`, `EXIT`는 일반적으로 리스크 축소 행동으로 취급

### 2. 손실 기반 차단

신규 진입 행동인 `BUY`, `SELL`에 대해 아래를 검사한다.

- 일일 손실 한도 초과
- 연속 손실 횟수 초과
- 쿨다운 활성 여부

### 3. 모드별 행동 허용 여부

현재 리스크 프로파일의 `allowed_actions`에 현재 결정이 포함되는지 확인한다.

예:

- `cooldown`에서는 `BUY`, `SELL`을 막고 `HOLD`, `REDUCE`, `EXIT`만 허용할 수 있다.

### 4. 최대 포지션 수

신규 진입이 현재 모드의 `max_open_positions`를 넘기는지 검사한다.

### 5. 심볼 / 주문 타입 / reduce-only 검증

- 허용 심볼인지
- 정책상 허용된 주문 타입인지
- `REDUCE`, `EXIT` 주문이 `reduce_only=true`인지

### 6. 최대 레버리지 노출 검사

현재 포지션 노출 + 신규 주문 후보 노출이 정책상 최대 레버리지 한도를 넘는지 확인한다.

### 7. 모드별 주문 후보 크기 한도 검사

`max_order_notional_pct_balance`를 이용해 현재 모드에서 허용하는 주문 후보 총 노출을 제한한다.

예:

- `normal`에서는 상대적으로 크게 허용
- `trigger`에서는 축소
- `drawdown`에서는 더 보수적으로 축소
- `cooldown`에서는 신규 진입이 사실상 0으로 제한될 수 있음

## 정책 파일 예시

현재 `policy.yaml`은 아래 개념을 포함한다.

- 거래 심볼 목록
- 최대 레버리지
- 주문 타입
- 일일 손실 한도
- 드로우다운 진입 기준
- 연속 손실 기준
- 쿨다운 시간
- 최대 동시 포지션 수
- 모드별 리스크 프로파일
- 루프 주기

특히 `mode_profiles`가 운영 편의성에 중요하다.  
상황이 바뀌었을 때 코드 수정 없이 YAML 테이블만 조정하면 되기 때문이다.

실제 예시는 아래처럼 둘 수 있다.

```yaml
trading:
  symbols:
    - BTCUSDT
    - ETHUSDT
  max_leverage: 3
  order_type: "market"

risk_limits:
  daily_max_loss_pct: 3.0
  drawdown_activate_pct: 1.5
  max_consecutive_losses: 3
  cooldown_minutes: 30
  max_open_positions: 2
  mode_profiles:
    normal:
      max_open_positions: 2
      max_order_notional_pct_balance: 100
      allowed_actions: [HOLD, BUY, SELL, REDUCE, EXIT]
    trigger:
      max_open_positions: 1
      max_order_notional_pct_balance: 35
      allowed_actions: [HOLD, BUY, SELL, REDUCE, EXIT]
    cooldown:
      max_open_positions: 2
      max_order_notional_pct_balance: 0
      allowed_actions: [HOLD, REDUCE, EXIT]
    drawdown:
      max_open_positions: 1
      max_order_notional_pct_balance: 20
      allowed_actions: [HOLD, BUY, SELL, REDUCE, EXIT]

intervals:
  strategy_loop_minutes: 60
  position_loop_minutes: 15
  trigger_loop_minutes: 5
```

해석 예:

- `daily_max_loss_pct: 3.0`
  - 하루 손익률이 `-3.0%` 이하로 내려가면 신규 진입은 kill-switch에 걸린다.
- `drawdown_activate_pct: 1.5`
  - hard stop 전에 먼저 `drawdown` 모드로 진입해 공격성을 줄인다.
- `trigger.max_order_notional_pct_balance: 35`
  - 변동성 이벤트 구간에서는 주문 후보 총 노출을 잔고의 35% 이내로 제한한다.
- `cooldown.allowed_actions`
  - 쿨다운 구간에서는 신규 진입 없이 축소/청산 위주 행동만 허용할 수 있다.

## 실행 엔진 상세

`ExecutionEngine`는 승인된 intent만 실행한다.

### 주문 생성

각 주문에 대해 아래를 수행한다.

- `clientOrderId` 생성
- Bybit/ccxt 형식으로 액션 변환
- `reduceOnly`, `takeProfit`, `stopLoss` 파라미터 설정
- 거래소에 주문 요청

### 타임아웃 처리

`ccxt.RequestTimeout`이 발생하면 즉시 재주문하지 않는다.

대신:

- 열린 주문 조회
- 닫힌 주문 조회
- 같은 `clientOrderId`를 가진 주문이 있는지 확인

그리고 결과를 아래 상태 중 하나로 receipt에 남긴다.

- `recovered`
- `timeout_unresolved`

### 일반 실패 처리

일반 예외가 발생하면 receipt에 `failed` 상태로 남긴다.  
즉, 실패가 로그에만 남고 데이터 상에는 사라지는 구조가 아니다.

## 주문/포지션 상태 머신

이 프로젝트의 중요한 변경점 중 하나는 주문과 포지션 상태를 별도 전이 테이블에 저장하는 것이다.

### 주문 상태

주문 상태는 대략 아래 lifecycle을 가진다.

- `none`
- `submitted`
- `partial_filled`
- `completed`
- `cancelled`
- `unknown`
- `failed`

예시:

- intent 제출: `none -> submitted`
- 일부 체결: `submitted -> partial_filled`
- 완전 체결: `submitted -> completed`
- 타임아웃 미복구: `submitted -> unknown`
- 일반 예외: `submitted -> failed`

### 포지션 상태

포지션 상태는 심볼 기준으로 아래 전이를 기록한다.

- `flat`
- `opening`
- `open`
- `reducing`
- `closed`
- `unknown`

예시:

- 신규 진입 주문 후 포지션이 생기면 `flat -> open` 또는 `flat -> opening`
- 부분 청산이면 `open -> reducing`
- 완전 청산이면 `open -> closed`
- 타임아웃 미복구면 `open -> unknown`

이 구조 덕분에 재시작 후에도 이전 실행이 어느 단계에서 멈췄는지 재구성하기 쉬워진다.

## 저장 데이터 상세

`StateStore`는 SQLite를 사용한다. 주요 테이블은 아래와 같다.

### `audit_logs`

한 번의 의사결정과 실행 루프에 대한 핵심 기록이다.

포함되는 대표 컬럼:

- `account_state`
- `market_state`
- `intent`
- `risk_approved`
- `risk_reason`
- `receipts`
- `policy_snapshot`
- `decision_input_hash`
- `risk_mode`
- `risk_profile`
- `decision_request`
- `ai_raw_response`
- `execution_summary`

샘플 row:

```json
{
  "id": 142,
  "timestamp": "2026-03-04 19:05:00",
  "intent": {
    "decision": "BUY",
    "orders": [
      {
        "symbol": "BTCUSDT",
        "action": "BUY",
        "order_type": "MARKET",
        "quantity": 0.01,
        "reduce_only": false
      }
    ],
    "confidence": 0.74
  },
  "risk_approved": 1,
  "risk_reason": "Approved",
  "decision_input_hash": "a13f2b6e...9c21",
  "risk_mode": "trigger",
  "risk_profile": {
    "max_open_positions": 1,
    "max_order_notional_pct_balance": 35,
    "allowed_actions": ["HOLD", "BUY", "SELL", "REDUCE", "EXIT"]
  },
  "execution_summary": {
    "loop_type": "Trigger (5m)",
    "trigger_mode": true,
    "executed_order_count": 1
  }
}
```

### `journal`

사람이 읽기 쉬운 요약 로그다.

- 결정
- rationale
- confidence
- risk reason
- 추정 손익률
- 손실 신호 여부

### `long_term_memory`

일일 배치가 생성한 장기 규칙 저장소다.

- 규칙 본문
- 버전
- 구조화 메트릭

샘플 row:

```json
{
  "rule": "Avoid chasing breakouts after two consecutive losses.",
  "version": 1,
  "metrics": {
    "status": "rule",
    "support": 5,
    "category": "risk_control",
    "regime": "high_volatility",
    "apply_when": {
      "regime": ["high_volatility"],
      "volatility": ["high"],
      "trend": ["up"]
    },
    "ignore_when": [
      "Ignore when breakout retest holds and volume expands."
    ],
    "evidence_journal_ids": [311, 319, 325, 331, 344]
  }
}
```

### `audit_fills`

체결 단위 데이터다.

- 주문 ID
- client order ID
- trade ID
- side
- price / amount / cost
- fee
- realized pnl

### `position_ledger`

심볼 단위 집계다.

- fill count
- total amount
- gross realized pnl
- fee cost
- net realized pnl
- balance delta
- balance delta pct

샘플 row:

```json
{
  "symbol": "BTCUSDT",
  "decision": "EXIT",
  "fill_count": 2,
  "total_amount": 0.02,
  "gross_realized_pnl": 12.5,
  "fee_cost": 1.1,
  "net_realized_pnl": 11.4,
  "balance_delta": 11.2,
  "balance_delta_pct": 1.12
}
```

### `replay_runs`

리플레이 결과 비교 저장소다.

- 원결정 / 리플레이 결정
- 결정 변경 여부
- 주문 수 변경 여부
- side 변경 여부
- size delta
- confidence delta
- rationale 키워드 추가/제거

샘플 row:

```json
{
  "audit_log_id": 142,
  "original_decision": "BUY",
  "replayed_decision": "HOLD",
  "decision_changed": true,
  "order_count_changed": true,
  "side_changed": true,
  "size_delta": -0.01,
  "confidence_delta": -0.28,
  "rationale_keywords_added": ["drawdown", "wait"],
  "rationale_keywords_removed": ["breakout", "momentum"]
}
```

### `order_state_transitions`

주문 상태 전이 저장소다.

- 심볼
- client order ID
- exchange order ID
- 이전 상태
- 다음 상태
- 이벤트 타입
- payload

샘플 row:

```json
{
  "audit_log_id": 142,
  "symbol": "BTCUSDT",
  "client_order_id": "gpt_1234abcd5678",
  "exchange_order_id": "order-9981",
  "from_state": "submitted",
  "to_state": "completed",
  "event_type": "exchange_update",
  "payload": {
    "receipt_status": "closed",
    "filled_quantity": 0.01,
    "fill_count": 1
  }
}
```

### `position_state_transitions`

포지션 상태 전이 저장소다.

- 심볼
- 이전 상태
- 다음 상태
- 이벤트 타입
- payload

샘플 row:

```json
{
  "audit_log_id": 142,
  "symbol": "BTCUSDT",
  "from_state": "flat",
  "to_state": "open",
  "event_type": "intent_buy",
  "payload": {
    "before_size": 0.0,
    "after_size": 0.01,
    "receipt_statuses": ["closed"]
  }
}
```

## 리플레이 도구

`tools/replay_audit.py`는 과거 audit의 `decision_request`를 현재 `DecisionEngine`에 다시 넣는다.

이 도구의 목적은 단순 재현이 아니다.  
"프롬프트/모델/메모리/운영 정책 변경 후 판단이 어떻게 달라졌는가"를 비교하는 것이다.

### 사용법

최근 리플레이 가능한 audit 목록:

```powershell
python tools/replay_audit.py --list
```

특정 audit 리플레이:

```powershell
python tools/replay_audit.py --audit-id 12
```

리플레이 결과를 DB에 저장:

```powershell
python tools/replay_audit.py --audit-id 12 --save
```

### 비교 항목

현재 diff는 아래를 포함한다.

- `decision_changed`
- `order_count_changed`
- `side_changed`
- `size_delta`
- `confidence_delta`
- `rationale_keywords_added`
- `rationale_keywords_removed`

즉, 예전엔 `BUY`였는데 지금은 `HOLD`인지, 같은 `BUY`여도 크기가 줄었는지, rationale의 중심 키워드가 무엇으로 바뀌었는지 바로 볼 수 있다.

## 장기 메모리 배치 상세

`batch/memory_updater.py`는 최근 journal과 ledger 통계를 바탕으로 장기 교훈을 구조화한다.

### 기본 원칙

- 통계 계산은 코드가 한다.
- Gemini는 통계를 해석해 lesson을 만든다.
- 근거 수가 적으면 `hypothesis`
- 근거 수가 충분하면 `rule`
- 모든 lesson은 적용 조건과 무시 조건을 가져야 한다.

### `MemoryLesson` 필드

- `rule`
- `regime`
- `category`
- `support_count`
- `evidence_journal_ids`
- `hypothesis`
- `apply_when`
- `ignore_when`

### 적용 조건 강제

`apply_when`은 아래 3축을 반드시 포함한다.

- `regime`
- `volatility`
- `trend`

즉, "언제 적용해야 하는가"를 명확히 강제한다.

### 무시 조건 강제

`ignore_when`은 적어도 하나 이상의 반례 조건을 갖는다.

즉, "언제 이 규칙을 믿지 말아야 하는가"도 같이 저장한다.

이 구조가 중요한 이유는, 그렇지 않으면 장기 메모리가 운영 가치가 낮은 일반론 목록으로 변하기 쉽기 때문이다.

## 대시보드

Flask 대시보드는 최근 운영 상태를 빠르게 확인하는 용도다.

### 확인 가능한 항목

- 최근 24시간 audit 수
- 리스크 거절 비율
- 루프 에러 수
- 미복구 타임아웃 수
- 최근 수수료
- 최근 ledger net pnl
- 최근 audit 목록
- 최근 memory
- 최근 fills
- 최근 ledger
- 최근 replay 결과
- 심볼별 성과 리포트
- 일별 ledger 리포트
- 특정 audit 상세
- 특정 audit replay 실행

### audit 상세에서 볼 수 있는 것

- 저장된 request
- 저장된 intent
- execution summary
- AI raw response
- replay runs
- order state transitions
- position state transitions
- `decision_input_hash`
- `risk_mode`, `risk_profile`

### 주의사항

현재 [`dashboard/app.py`](/c:/gpt_trader/dashboard/app.py)는 DB 경로를 환경 변수 대신 `data/gpt_trader.db`로 고정해서 읽는다.  
메인 실행에서 다른 `DB_PATH`를 쓰면 대시보드가 같은 DB를 보지 못할 수 있다.

## 설치 방법

### 1. Python 환경 준비

권장: Python 3.11+

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. 환경 변수 설정

`.env.example`를 복사해 `.env`를 만든다.

```powershell
Copy-Item .env.example .env
```

필수 변수:

- `BYBIT_API_KEY`
- `BYBIT_API_SECRET`
- `BYBIT_TRADING_MODE`
- `GEMINI_API_KEY`
- `GEMINI_DEFAULT_MODEL`
- `DB_PATH`
- `LOG_LEVEL`
- `LOG_FORMAT`

선택 변수:

- `ALERT_WEBHOOK_URL`

예시:

```env
BYBIT_API_KEY="your_bybit_api_key_here"
BYBIT_API_SECRET="your_bybit_api_secret_here"
BYBIT_TRADING_MODE="demo"
BYBIT_TESTNET="False"
GEMINI_API_KEY="your_gemini_api_key_here"
GEMINI_DEFAULT_MODEL="gemini-2.5-flash"
GEMINI_TRIGGER_MODEL="gemini-2.5-pro"
DB_PATH="data/gpt_trader.db"
LOG_LEVEL="INFO"
LOG_FORMAT="text"
```

모델 선택:

- `GEMINI_DEFAULT_MODEL`
  - 일반 루프에서 사용하는 기본 모델
- `GEMINI_TRIGGER_MODEL`
  - trigger mode에서만 따로 쓰고 싶을 때 지정
  - 비우면 `GEMINI_DEFAULT_MODEL`을 재사용
- `GEMINI_MODEL`
  - 레거시 단일 모델 fallback

### 3. 정책 확인

`policy.yaml`에서 아래 항목을 운영 환경에 맞게 수정한다.

- 거래 심볼
- 최대 레버리지
- 주문 타입
- 일일 손실 제한
- 드로우다운 진입 기준
- 연속 손실 제한
- 쿨다운 시간
- 최대 동시 포지션 수
- 모드별 리스크 프로파일
- 루프 주기

## 실행 방법

자동매매 시작:

```powershell
python main.py
```

대시보드 실행:

```powershell
python dashboard/app.py
```

접속 주소:

```text
http://127.0.0.1:5000
```

장기 메모리 배치 수동 실행:

```powershell
python batch/memory_updater.py
```

## 테스트

현재 테스트는 `unittest` 기반이다.

```powershell
python -m unittest discover -s tests
```

테스트가 다루는 영역:

- 리스크 엔진 정책 검증
- 모드별 리스크 프로파일 적용
- 드로우다운 / 쿨다운 판정
- StateStore 집계와 JSON 디코딩
- 입력 해시 저장
- replay diff 저장
- 주문/포지션 상태 전이 저장
- ExecutionEngine 체결 매칭과 상태 전이 생성
- 장기 메모리 통계와 조건 메타데이터 저장

## 운영 메모

- 기본 거래소 타입은 Bybit linear USDT perpetual이다.
- 시장 데이터는 항상 Bybit 메인넷 public API에서 가져온다.
- `BYBIT_TRADING_MODE=live`면 실계좌로 주문과 계좌 조회를 수행한다.
- `BYBIT_TRADING_MODE=demo`면 Bybit Demo Trading 계정으로 주문과 계좌 조회를 수행한다.
- `BYBIT_TESTNET`은 구버전 호환용 fallback이다. `BYBIT_TRADING_MODE`가 우선한다.
- 시장 판단에는 미완성 캔들을 제외한 종가 데이터가 사용된다.
- `EXIT` 의사결정에 `orders`가 비어 있으면 현재 포지션을 기준으로 reduce-only 시장가 청산 주문을 자동 생성한다.
- 주문 실행 중 일반 예외가 나면 receipt에 `failed` 상태로 남는다.
- 알림 웹훅이 없으면 알림은 로그만 남기고 종료한다.

## 권장 운영 순서

1. Bybit Demo Trading 키로 시작하고 `BYBIT_TRADING_MODE=demo`로 둔다.
2. `policy.yaml`의 손실 한도와 `mode_profiles`를 보수적으로 설정한다.
3. `python main.py`로 로그와 audit를 충분히 쌓는다.
4. `python dashboard/app.py`에서 input hash, risk mode, 상태 전이, replay diff를 확인한다.
5. `python tools/replay_audit.py --list`와 `--audit-id`로 과거 결정 변화 양상을 점검한다.
6. 장기 메모리에 저장된 `apply_when`, `ignore_when`가 실제 운영 관점에서 타당한지 검토한다.
7. 문제가 없을 때만 `BYBIT_TRADING_MODE=live` 전환을 검토한다.

## 참고 파일

- 메인 오케스트레이터: [`main.py`](/c:/gpt_trader/main.py)
- 정책 파일: [`policy.yaml`](/c:/gpt_trader/policy.yaml)
- 데이터 모델: [`core/models.py`](/c:/gpt_trader/core/models.py)
- 리스크 엔진: [`core/risk_engine.py`](/c:/gpt_trader/core/risk_engine.py)
- 상태 저장소: [`db/state_store.py`](/c:/gpt_trader/db/state_store.py)
- 의사결정 엔진: [`ai/decision_engine.py`](/c:/gpt_trader/ai/decision_engine.py)
- 실행 엔진: [`exchange/execution_engine.py`](/c:/gpt_trader/exchange/execution_engine.py)
- 메모리 배치: [`batch/memory_updater.py`](/c:/gpt_trader/batch/memory_updater.py)
- 리플레이 도구: [`tools/replay_audit.py`](/c:/gpt_trader/tools/replay_audit.py)
- 대시보드: [`dashboard/app.py`](/c:/gpt_trader/dashboard/app.py)
