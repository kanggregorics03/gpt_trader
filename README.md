# GPT Trader

Bybit 선물 계정과 LLM provider를 연결해 자동매매를 수행하는 프로젝트다.  
AI는 시장과 계좌 상태를 바탕으로 주문 후보를 생성하지만, 실제 리스크 통제, 주문 실행, 복구 판단, 감사 로그 저장은 애플리케이션 코드가 담당한다.  
현재 구조는 `Gemini / OpenAI(GPT) / DeepSeek / Anthropic(Claude)`를 provider adapter로 선택할 수 있다.

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

`DecisionEngine`이 provider adapter를 통해 구조화된 입력을 보내고 `TradeIntent`를 받는다.  
AI는 전략 제안자 역할만 하며, 실제 주문 집행 권한은 없다.  
JSON mode가 있는 provider는 native JSON 응답을 우선 사용하고, 없는 경우에도 JSON-only 프롬프트와 Pydantic 검증을 함께 써서 출력 안정성을 확보한다.

### 2. 정책 기반 리스크 통제

`RiskEngine`이 정책 파일과 현재 리스크 상태를 바탕으로 주문 후보를 검증한다.  
이 단계에서 심볼, 주문 타입, 포지션 수, 노출 한도, 손실 상태, 모드별 상한을 모두 검사한다.

### 3. 실행과 복구

`ExecutionEngine`이 승인된 주문만 거래소에 전달한다.  
타임아웃이 나면 무조건 재주문하지 않고, 먼저 같은 `clientOrderId`로 복구를 시도한다.

### 4. 상태 저장과 감사

`StateStore`가 audit, journal, fills, ledger, replay, 상태 전이 기록을 SQLite에 저장한다.  
여기서 중요한 점은 "주문 후보 생성 근거 입력 해시"와 "주문/포지션 상태 머신"까지 저장한다는 것이다.

### 5. 정책 진화

일일 배치는 journal, trade case, replay, shadow decision을 바탕으로 장기 메모리뿐 아니라 `정책 후보`도 만든다.  
후보 정책 평가는 shadow/replay/실행 결과로 계속 저장하지만, live runtime contract는 자동 승격으로 바꾸지 않는다.

## 시스템 목표

이 프로젝트는 아래 문제를 해결하려고 설계됐다.

- 같은 결론이 정말 같은 입력에서 나왔는지 추적하기 어렵다.
- 손실 구간에서도 동일한 공격성으로 주문이 나가는 경우가 있다.
- 주문 타임아웃, 부분 체결, 미확인 상태에서 복구 로직이 약해지기 쉽다.
- 모델이나 프롬프트를 바꾸면 무엇이 얼마나 바뀌었는지 직관적으로 알기 어렵다.
- 장기 메모리가 쌓여도 적용 조건이 없으면 운영 가치를 잃기 쉽다.

현재 구현은 위 문제를 아래 방식으로 다룬다.

- `decision_input_hash`로 입력 추적 가능성 강화
- `normal / guarded / cooldown / drawdown` 리스크 모드 도입
- 주문/포지션 상태 전이 테이블 저장
- replay diff를 수치와 키워드 단위로 저장
- 장기 메모리에 `apply_when`, `ignore_when` 강제

## 전체 아키텍처

```text
AccountReader ----\
                   \
MarketCollector ----> SnapshotService ----> DecisionRequest ----> DecisionEngine ----> TradeIntent
                   /                 |                                  |
StateStore -------/                  v                                  v
                           adaptive cadence state                RiskEngine (policy + risk mode/profile)
                           (1h / 15m / 5m)                                  |
                                                                             +---- approved? ----> ExecutionEngine
                                                                             |
                                                                             +---- rejected ----> Audit / Journal
```

그리고 별도 배치 경로가 있다.

```text
journal + trade_cases + replay + shadow -> MemoryUpdater
    -> long_term_memory
    -> candidate_policy_versions (legacy offline artifacts)
    -> policy_registry (legacy offline registry, no live promotion)
```

## 2026-03 리팩토링 상태

이번 구조 리팩토링의 목적은 의사결정 로직을 바꾸는 것이 아니라, 책임 경계를 복원하고 테스트 가능한 단위로 분해하는 것이었다.

- `TradingOrchestrator`의 공개 엔트리포인트는 유지했다.
- `StateStore`의 공개 API 시그니처는 유지했다.
- 프롬프트/학습 입력에는 여전히 `prompt-safe` 필터를 적용한다.
- raw 저장/조회와 prompt/학습용 후보 조회를 구분한다.

현재 기준으로 `main.py`와 `db/state_store.py`는 facade이고, 실제 구현은 `orchestrator/`와 `db/state_store_parts/` 아래의 책임별 mixin/module로 분리돼 있다.
최근에는 `orchestrator/runtime_support.py`도 facade로 줄이고, runtime helper를 `runtime_state.py`, `cadence_policy.py`, `runtime_diagnostics.py`로 분리했다.

의사결정 품질에 영향을 줄 수 있는 변경은 구조 리팩토링 자체가 아니라, 리팩토링 후 발견된 명시적 버그 수정으로만 반영했다.

- 스키마가 깨진 audit에 연결된 trade case는 prompt/vector candidate에서 제외
- audit row가 없는 raw trade case는 direct lookup에서 숨기지 않음
- shadow policy score는 빈 calibration summary에서 기본 보너스를 얹지 않음

검증 기준선은 repo-local `.venv`에서 아래 명령으로 고정한다.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -q
```

검증 기준선은 repo-local `.venv`의 전체 unittest 스위트다.

## 프로젝트 구조

```text
gpt_trader/
├─ main.py                    # 메인 엔트리포인트 facade
├─ policy.yaml                # 거래/리스크 정책
├─ .env.example               # 환경 변수 예시
├─ ai/
│  ├─ decision_engine.py      # provider-agnostic 의사결정 생성, 입력 해시 생성
│  ├─ llm_provider.py         # Gemini / OpenAI / DeepSeek / Anthropic adapter
│  └─ rag_engine.py           # 유사 과거 케이스 검색
├─ batch/
│  └─ memory_updater.py       # 장기 메모리 + 오프라인 정책 후보 평가 배치
├─ core/
│  ├─ alpha_policy.py         # 1h 진입 필터 정책 엔진
│  ├─ execution_policy.py     # market/limit 및 execution profile 정책 엔진
│  ├─ models.py               # Pydantic 데이터 모델
│  ├─ position_policy.py      # 15m 포지션 FSM 정책 엔진
│  ├─ risk_engine.py          # 정책 검증, 리스크 모드/프로파일 계산
│  ├─ safety_policy.py        # 5m execution safety FSM 정책 엔진
│  └─ snapshot_service.py     # 공용 계좌/시장/리스크 스냅샷 조립
├─ orchestrator/              # 오케스트레이터 내부 구현 분해
│  ├─ bootstrap.py            # bootstrap facade
│  ├─ bootstrap_config.py     # RuntimeConfig와 startup config 오류 정의
│  ├─ bootstrap_dependencies.py # 외부 의존성 조립과 초기 wiring
│  ├─ runtime_support.py      # runtime facade
│  ├─ runtime_state.py        # risk/runtime helper, execution runtime 유틸
│  ├─ cadence_policy.py       # adaptive cadence 전환 규칙과 maintenance tick
│  ├─ runtime_diagnostics.py  # websocket/risk transition 진단과 알림
│  ├─ cycle_*.py              # cycle 실행/리스크/감사/주문 흐름
│  ├─ context_*.py            # recent journal / memory / similar case 선택
│  └─ trade_case_*.py         # trade case retrieval / annotation / snapshot
├─ dashboard/
│  └─ app.py                  # Flask 운영 대시보드
├─ db/
│  ├─ state_store.py          # SQLite facade
│  └─ state_store_parts/      # schema / journal / trade case / runtime / query 분해
├─ exchange/
│  ├─ account_reader.py       # Bybit 계좌 조회
│  ├─ collector.py            # 시세/지표 수집
│  └─ execution_engine.py     # 주문 실행, 체결 추적, 상태 전이 생성
├─ tools/
│  └─ replay_audit.py         # 감사 로그 리플레이 및 diff 요약
├─ utils/
│  ├─ logger.py               # loguru 설정
│  ├─ symbols.py              # 거래 심볼 정규화 유틸
│  └─ alerter.py              # 웹훅 알림
└─ tests/                     # unittest 기반 테스트
```

## 실행 흐름 상세

### 1. SnapshotService가 공용 상태를 조립

`main.py`의 오케스트레이터는 더 이상 `1시간/15분/5분` 스케줄을 각각 돌리지 않는다.  
대신 하나의 adaptive loop가 현재 상태에 따라 다음 실행 주기를 `1h`, `15m`, `5m` 중 하나로 선택한다.

각 사이클의 시작에서는 `SnapshotService`가 아래를 한 번에 만든다.

- `AccountReader`로 계좌 상태 조회
- `StateStore`로 현재 리스크 상태 계산
- `MarketCollector`로 심볼별 시장 스냅샷 수집
- 현재 스냅샷을 바탕으로 `is_guarded_attention`, `risk_mode`, `risk_profile` 결정
  - 여기서 `is_guarded_attention`은 더 이상 opportunity/attention 신호가 아니라 `risk_trigger` 기반 guarded state 플래그다

즉, 정보 수집과 판단을 분리해 같은 사이클 안에서 중복 조회를 줄이는 구조다.
계좌 상태는 `SnapshotService -> AccountReader -> private WS cache` 경로를 우선 사용하고, REST는 fallback + reconcile 시에만 호출한다.

중요한 점은 `SnapshotService`가 단순히 "실행할 때마다 전부 다시 조회"하지 않는다는 것이다.

- 5분마다 공용 스냅샷을 materialize
- 현재가 / 오더북 / 파생상품 / 계좌 상태는 5분 스냅샷 시점마다 갱신
- `5m` 타임프레임은 매 materialization 때 갱신
- `15m`, `1h`, `4h` 타임프레임은 해당 봉이 실제로 마감됐을 때만 다시 계산

계좌 상태는 아래 경우에만 REST reconcile로 보정한다.

- 주문 실행 직후 pre/post trade reconcile
- WS health가 `stale`, `degraded`, `reconnecting`, `down`
- unknown order 복구가 필요한 경우
- 주기적 reconcile TTL이 지난 경우

즉, 상위 타임프레임 값은 "새 봉이 닫혔는가"를 기준으로 선택적으로 refresh되고, 그 외 시점에는 직전 materialized 값을 재사용한다.

여기서 중요한 점은 경로가 분리되어 있다는 것이다.  
시장 데이터는 Bybit 메인넷 public API에서 받고, 계좌 조회와 주문 실행은 `BYBIT_TRADING_MODE`에 따라 live 또는 demo private API로 보낸다.

공용 스냅샷을 만드는 구성 요소는 다음과 같다.

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
  - 짧은 TTL 캐시를 사용해 같은 구간의 중복 private API 조회를 줄이되, private WS가 비정상이면 캐시보다 REST reconcile을 우선
  - Bybit private WebSocket으로 `wallet / position / order / execution`을 메모리에 유지하고, REST는 fallback + reconcile 용도로만 사용
  - WS health state는 `healthy / stale / degraded / reconnecting / down`
  - `healthy`일 때만 WS를 primary source로 사용하고, 나머지 상태에서는 REST fallback 사용
  - 시작 직후 계정이 조용해 private 이벤트가 아직 없어도, REST reconcile이 1회 성공하고 안정 구간을 지나면 `healthy`로 승격될 수 있다
  - transport liveness는 private 계좌 변동 이벤트뿐 아니라 pybit raw message / pong / open 훅으로도 갱신한다
  - 따라서 private event가 없다는 이유만으로 바로 `stale`로 보지 않고, 최근 reconcile TTL 안에서는 `healthy`를 유지한다
  - 반대로 stale/down 이후 private 이벤트가 다시 들어온 직후에는 forced reconcile 정책에 따라 한 번 더 `degraded`를 거칠 수 있다
  - 주문/포지션/체결 심볼은 `ETHUSDT`, `ETH/USDT`, `ETH/USDT:USDT` 같은 표현 차이를 canonical symbol로 정규화한 뒤 비교한다
  - `get_runtime_diagnostics()`로 account cache freshness와 private WS health / event count / lag를 한 번에 확인할 수 있다
- `MarketCollector`
  - 현재가
  - ATR 15m / ATR 1h
  - 1시간 추세
  - 15분 거래량 비율과 즉시 거래량 급증 여부
  - 히스테리시스가 반영된 fast move / volume surge / range break signal 상태
  - 다만 이 short-horizon signal은 15m/5m follow-through 판단용이고, 1h decision payload에서는 제거된다
  - open interest / funding / basis의 변화율 요약
  - spread와 depth의 최근 1시간 상대 수준(percentile / avg 대비 악화 여부)
    - 이 percentile 계열은 in-memory rolling history가 쌓여야 계산된다
    - 프로세스 시작 직후나 잦은 재시작 직후에는 `spread_percentile_1h`, `depth_percentile_1h`, `spread_vs_1h_avg`가 `null`일 수 있다
  - materialization 시점마다 `market_snapshot_history`에도 별도 시계열 row를 남긴다
    - 최소한 `current_price`, `mark_price`, `mid_price`, `spread_bps`, `depth_notional_top5`
    - `spread_percentile_1h`, `depth_percentile_1h`, `spread_vs_1h_avg`
    - `distance_to_invalid_level`
    - `volume_surge / fast_move / range_break / risk trigger` 상태
    - `trend_1h`
  - 따라서 replay의 미래 가격 경로는 audit 저장 빈도에만 의존하지 않고, 5분 materialization 기반 시계열을 우선 읽는다
  - breakout 기준선 및 invalidation까지의 거리 요약
  - follow-through / breakout trigger 상태
  - risk trigger 여부와 사유
  - public WebSocket은 `ticker + 얕은 order book` 위주로 유지하고, closed-candle 기반인 OHLCV는 기본적으로 5분 materialization 시점의 incremental REST fetch에 더 의존
  - 즉 `1h / 4h` 진행 중 봉을 계속 스트리밍하는 것보다, 봉 마감 시점과 5분 cadence에 맞춘 저빈도 fetch를 우선한다
  - ticker / order book / funding / open interest의 REST fallback TTL을 늘려 불필요한 재호출을 줄임
  - 이 구조에서는 높은 WS throttle 자체보다 `bounded lag + 정합성 + 적은 호출 수`가 더 중요하다
  - public WS는 latest-state overwrite를 유지하고, `get_diagnostics()` / `get_runtime_diagnostics()`로 stream age, symbol age, handler timing, exchange lag를 노출해 큐잉 지연을 감시한다

### 2. 현재 리스크 상태 계산

`StateStore.get_risk_state()`가 최근 audit / journal을 이용해 현재 상태를 계산한다.

- 일일 시작 잔고
- 일일 손익률
- 연속 손실 횟수
- 최근 손실 강도
- 동적 쿨다운 길이와 구성 요소
- 쿨다운 활성 여부
- 쿨다운 남은 시간
- 반복 실패 패턴 점수 / 대표 실패 사유
- 드로우다운 활성 여부
- 심볼별 recent close / recent loss 기반 재진입 가드 상태

이 값은 단순 모니터링용이 아니라, 실제로 리스크 모드를 결정하는 입력으로 사용된다.
`StateStore.get_risk_state()`가 만든 기본 필드에 더해, 오케스트레이터는 현재 단일 운용 심볼에 대한
recent close / recent loss 재진입 가드를 같은 `RiskStateSnapshot` 타입으로 덧붙여
`DecisionRequest.risk_state`와 audit/RAG 컨텍스트에 일관되게 싣는다.

### 3. 리스크 모드와 프로파일 결정

`RiskEngine`은 현재 상황을 아래 네 모드 중 하나로 분류한다.

- `normal`
- `guarded`
- `cooldown`
- `drawdown`

결정 우선순위는 다음과 같다.

1. 쿨다운이면 `cooldown`
2. 드로우다운이면 `drawdown`
3. risk trigger 기반 guarded 조건이면 `guarded`
4. 아니면 `normal`

이후 현재 모드에 대응하는 프로파일을 읽는다.

- `max_open_positions`
- `max_order_notional_pct_balance`
- `allowed_actions`

즉, 같은 `BUY`라도 지금이 `normal`인지 `drawdown`인지에 따라 허용 여부와 크기 제한이 달라질 수 있다.  
최근 구현에서는 `drawdown`이 신규 진입을 완전히 막지는 않되, `entry_size_multiplier`, `confidence_cap`, `failure_pattern_score`를 이용해 size cap을 한 번 더 줄인다.

현재 구현에서는 live runtime이 레짐별 `policy_registry` champion을 직접 읽어
`max_order_notional_pct_balance`나 `entry_size_multiplier`를 override하지 않는다.
또한 shadow policy 기록과 자동 promotion 경로는 비활성화되어, live 정책은 `policy.yaml`과 코드 계약에 고정된다.
노출 조정은 기본 risk profile을 그대로 사용한다.
반면 아래 안전 가드레일은 자동 진화 대상이 아니다.

- `max_leverage`
- `drawdown_activate_pct`
- `daily_max_loss_pct`
- `cooldown` 최대 한도
- `max_consecutive_losses`의 일중 집계 기준

### 4. 단일 adaptive loop와 cadence 전환

현재 구현에서 중요한 변경점은 `루프가 하나만 돈다`는 것이다.  
오케스트레이터는 한 번의 사이클이 끝날 때마다 현재 상태를 보고 다음 cadence를 고른다.

우선순위는 다음과 같다.

- `5m`
  - risk/emergency trigger가 활성화되어 있고
  - 동시에 포지션 / 미체결 주문 / unknown order 같은 active management 대상이 있는 경우
- `15m`
  - 포지션이 있거나
  - 미체결 주문이 있거나
  - unknown order 상태가 있거나
  - 최근 실행/청산 직후인 경우
- `1h`
  - 위 조건이 모두 없을 때

승격은 빠르게, 강등은 천천히 한다.

- `1h -> 15m`
  - 포지션/주문/복구 필요 상태가 생기면 즉시 승격
- `1h or 15m -> 5m`
  - risk/emergency trigger 조건이 충족되고
  - 동시에 active management 대상이 있으면 즉시 승격
- `5m -> 15m or 1h`
  - active management 대상이 남아 있으면 clear streak를 만족한 뒤 강등
  - 포지션 / 미체결 주문 / unknown order가 모두 사라진 flat 상태에서는 clear streak 없이 즉시 강등
- `15m -> 1h`
  - recent execution / recent close TTL이 남아 있으면 유지
  - TTL까지 끝난 완전 flat 상태에서는 즉시 `1h`로 복귀

이 히스테리시스는 cadence가 `5m <-> 15m <-> 1h` 사이에서 경계값 근처에서 깜빡이는 현상을 줄이기 위한 것이다.

또한 cadence마다 역할도 다르다.

- `1h`
  - 전략 판단 중심
  - 신규 진입과 방향성 결정을 담당
  - `AlphaPolicyEngine`은 현재 live pre-filter가 아니라 advisory 요약으로만 남고, AI에는 전체 심볼 스냅샷을 그대로 보낸다
  - AI structured fallback이 발생하면 최대 2회까지 `1h` 전략 계약을 유지한 채 빠른 재시도 스케줄만 적용할 수 있다
- `15m`
  - 포지션/주문 관리 중심
  - 기본적으로 신규 진입보다 `EXIT`, `CANCEL`, 주문 정리 쪽으로 제한
  - 작은 음수 손익만으로 바로 꺾지 않고, 하드 손절 또는 invalidation 근접 같은 thesis 약화 신호가 있을 때만 청산한다
  - 기본 경로는 AI가 아니라 `PositionPolicyEngine` FSM이며, 예외시만 AI가 다시 본다
- `5m`
  - execution safety 중심
  - risk/emergency trigger 구간에서 더 짧은 주기로 실행 품질과 급변 리스크를 감시
  - 기본 경로는 AI가 아니라 `SafetyPolicyEngine` FSM이며, 신규 진입은 만들지 않고 `HOLD / EXIT / CANCEL`만 다룬다
  - acute account-loss, stale order execution 문제, 연속 확인된 liquidity deterioration 같은 emergency path만 다룬다
  - 포지션이 사라진 뒤에는 recent execution / close만으로 `5m`를 유지하지 않고 `15m` 관리 주기로 즉시 내린다

이 구조는 `1h = alpha loop`, `15m = position management loop`, `5m = execution safety loop`라는 역할 분리를 따른다.

### 5. Attention / Risk Signal 분리 원칙

이 프로젝트는 시장 attention 신호와 risk trigger를 분리한다.

- `attention_signal`
  - 시장에 실제 이벤트가 발생해 더 자주 관찰하거나 대응할 가치가 있는 상황
  - 예: `fast_price_move_15m`, `volume_surge_15m`, `range_break_1h`
- `risk_trigger`
  - 거래 품질이나 포지션 보유 비용이 나빠져 신규 진입을 줄이거나 막아야 하는 상황
  - 예: `wide_spread`, `elevated_funding`

중요한 점은 `guarded`가 더 이상 opportunity/attention 계열 신호로 켜지지 않는다는 것이다.  
현재는 `risk_trigger`가 있을 때만 guarded state로 본다. 즉 guarded는 위험/비상 상황용 보수 상태다.

현재 구현에서는 별도의 `Trigger (5m)` 작업이 따로 스케줄되지 않는다.  
`5m` 승격은 `risk_trigger` 같은 safety/emergency 신호와 포지션 / 미체결 주문 / unknown order 같은 active management 대상이 함께 있을 때만 의미가 있다.

`fast_price_move_15m`, `volume_surge_15m`, `range_break_1h`에는 심볼별 히스테리시스를 적용한다.  
이 값들은 시장 follow-through / breakout 상태를 나타내는 관찰 정보로 남지만, 더 이상 guarded 진입 조건으로 직접 쓰이지 않는다.

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

### 6. AI 입력 구성

`DecisionRequest`는 아래를 포함한다.

- `policy_summary`
- `account_state`
- `market_snapshot` 또는 `market_snapshots`
- `risk_state`
- `recent_journal_context`
  - 최근 journal 문맥을 구조화 객체로 전달
  - `rationale_items`, `evidence_quality`, `entry_size_cap`, trigger 정보까지 포함
  - 단순 점수 상위 N개가 아니라 `good_hold / missed_opportunity_hold / entry_action / risk_outcome` 버킷을 섞는 방식으로 편향을 줄인다.
  - semantic validation 실패나 loop error로 생긴 fallback `HOLD`는 기본 컨텍스트에서 제외한다.
- `long_term_memory`
  - `risk_control / opportunity / execution_quality / hold_quality` 축을 함께 보고 균형 있게 선별한다.
- `similar_past_cases`
  - schema/semantic validation fallback audit와 그 audit에 연결된 trade case는 프롬프트 retrieval 후보에서 제외한다.
- `is_guarded_attention`
- `risk_mode`
- `risk_profile`

여기서 중요한 변경점 세 가지가 있다.

첫째, AI는 현재 리스크 모드와 프로파일을 직접 입력으로 받는다.  
다만 최근 계약에서는 모델 판단 자율성을 넓히기 위해, 이 입력은 hard execution constraint와 sizing context로 우선 쓰고
설명 품질 문제만으로 행동을 `HOLD`로 덮어쓰지 않게 분리했다.

둘째, AI는 `risk_state`의 구체 수치도 함께 받는다.  
즉, 단순히 `drawdown` 같은 라벨만 보는 것이 아니라, 현재 일일 손익률, 연속 손실, 쿨다운 잔여 시간, 동적 쿨다운 길이, 반복 실패 패턴 점수, 현재 심볼의 재진입 가드 상태까지 보고 위험도를 해석할 수 있다.

셋째, 시장 스냅샷은 `fast_move_signal_active`, `volume_surge_signal_active`, `range_break_signal_active`, `risk_trigger_reasons`를 전달한다.  
즉, follow-through / breakout 상태와 실제 위험 신호를 구분해서 본다.

또한 시장 스냅샷은 raw 값만 나열하지 않고, `oi_change_15m`, `oi_change_1h`, `funding_rate_change_1h`,
`basis_change_1h`, `spread_percentile_1h`, `depth_percentile_1h`, `spread_vs_1h_avg`,
`distance_to_15m_breakout_level`, `distance_to_1h_breakout_level`, `distance_to_invalid_level`처럼
LLM이 바로 해석하기 쉬운 파생 필드를 상단 요약으로 함께 전달한다.
여기서 `trend_alignment`는 최근 `15m/1h/4h` 다수결과 `1h/4h` 우선 정렬로 완화되었고,
`distance_to_invalid_level`은 신규 진입 veto가 아니라 sizing/stop-definition 입력으로 다루는 것이 현재 계약이다.

최근 프롬프트는 입력 섹션 이름을 `market_context`가 아니라 `market_snapshots`로 통일한다.
구조화 evidence의 `source`도 `market_snapshots.*`, `risk_state.*`, `account_state.*` 같은 실제 `DecisionRequest` 경로를 가리켜야 한다.

`volume_surge_15m_now`와 `volume_surge_signal_active`는 의도적으로 분리한다.  
전자는 현재 15분 봉의 즉시 조건이고, 후자는 히스테리시스가 반영된 안정화된 trigger 상태다.

넷째, 장기 메모리는 더 이상 단순 문장 리스트만이 아니다.  
규칙, 지원 근거 수, 적용 조건, 무시 조건을 가진 구조화 레코드로 전달될 수 있다.

다섯째, 현재 프로젝트는 단일 심볼 운영을 전제로 한다.  
`trading.symbols`에 여러 개를 적더라도 런타임에서는 첫 번째 심볼만 사용하고, `max_open_positions`도 1로 정규화한다.

여섯째, RAG는 최근 audit를 심볼별로 반복 스캔하지 않는다.  
현재는 단일 심볼만 운용하지만, audit 저장 시 feature vector를 심볼 키로 남겨 이후 복기와 확장 시에도 같은 구조를 재사용한다.

일곱째, RAG feature vector는 단순 시장 13차원만 쓰지 않는다.  
시장 13차원 + 의사결정 컨텍스트 13차원(리스크 상태, 계좌 상태, 심볼 포지션/오더 상태)으로 확장한 26차원 벡터를 사용한다.

여덟째, 코사인 유사도 계산 전 차원별 정규화와 가중치를 적용한다.  
즉 ATR 같은 절대 스케일이 큰 축이 유사도 방향을 과도하게 지배하지 않도록, scale normalization + clipping + weighting 후 코사인을 계산한다.

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
단, `account_state.snapshot_timestamp` 같은 고변동 필드는 캐시 안정성을 위해 canonicalization 시 제외한다.

이 해시는 `decision_input_hash`로 audit에 저장된다.

이 값이 중요한 이유:

- "이 결정이 어떤 입력에서 나왔는지"를 변조나 혼동 없이 추적할 수 있다.
- 프롬프트나 모델이 바뀌어도 입력 자체가 같았는지 확인할 수 있다.
- 운영 중 비슷한 사건을 비교할 때 기준점을 제공한다.

해시는 의도적으로 "주문 후보 생성의 핵심 사실 입력"에 집중하고 있다.  
즉, 최근 journal이나 장기 메모리 같은 보조 문맥보다, 정책/시장/계좌/활성 리스크 컨텍스트의 핵심 조합을 기준으로 남긴다.

## 정책 진화 계층

현재 구현은 단순 `메모리 축적`이 아니라 제한된 정책 공간 안에서 자동 진화하는 구조다.

- `alpha_1h`
  - 레짐별 진입 필터 후보 생성
  - live entry와 후보 필터의 차이를 shadow로 저장
  - 평가 결과는 offline 비교용으로만 유지
- `position_15m`
  - FSM 관리 정책 후보 생성
  - recent trade case의 invalidation 거리와 microstructure stress를 읽어 `invalidation_reduce_distance_atr`, liquidity exit 임계치를 함께 튜닝
  - shadow decision과 실제 outcome을 비교
  - 평가 결과는 offline 비교용으로만 유지
- `execution_5m`
  - 실제 fill/slippage/latency 결과를 오프라인 분석용으로 저장
  - live policy domain이 아니라 post-trade observability surface로만 유지
- `safety_5m`
  - 5분 safety FSM 후보 생성
  - acute exit 후보만 shadow로 비교하며, 과거 `risk_reduce` 보너스는 더 이상 승격 점수에 반영하지 않음
  - live safety decision과 shadow candidate 차이를 저장
  - 손실 회피 효과는 offline 비교 지표로만 본다
`policy_bundle` 계층은 제거했다.

- live loop는 bundle override를 전혀 주입하지 않는다
- `policy_registry`는 live runtime source가 아니라 offline 평가/이력 저장소로만 남긴다
- 학습 결과도 candidate/evaluation 단위로만 남기고 bundle 상태는 더 이상 저장하지 않는다

## 의사결정 모델

AI 출력은 `TradeIntent`다.

프롬프트도 이제 `loop_mode`와 `cycle_role`을 직접 입력으로 받는다.
즉 `1h`에서는 전략 판단, `15m`에서는 포지션 관리, `5m`에서는 execution safety 제약을 프롬프트 레벨에서도 명시적으로 따른다.

추가로 live position이 남아 있으면 `1h`도 예외 없이
`trade_consistency_management` 역할로 내려간다.
이때는 기존 `trade_case_id` / `thesis_id` / `management_template_id`를 유지한 채
`HOLD/EXIT/CANCEL/REPLACE_ENTRY`만 허용하고 새 진입 thesis는 만들지 않는다.

- `decision`
  - `HOLD`, `BUY`, `SELL`, `REDUCE`, `EXIT`, `CANCEL`, `REPLACE_ENTRY`
- `orders`
  - 각 주문 후보의 심볼, 액션, 주문 타입, 수량, 가격, `reduce_only`, TP, SL
  - `CANCEL` 액션일 때는 `target_order_id` 또는 `target_client_order_id`를 함께 담아 working order cleanup에 사용한다
  - `REPLACE_ENTRY`는 현재 자동 관리 루프가 아니라 명시적 1h working-order 관리 상황에서만 사용한다
  - AI 응답 스키마에는 `execution_profile`, `urgency_score`, `time_in_force`, `post_only`, `limit_offset_bps`, `max_slippage_bps`를 노출하지 않는다. 실행 메타데이터는 runtime이 채운다
  - `EXIT`와 `REDUCE`는 실행 레이어에서 항상 시장가 성격을 유지한다. 포지션 정리를 passive limit로 늦추지 않는다
  - 신규 `BUY/SELL` entry는 `entry_style`로 의미를 명시한다. `immediate`는 지금 진입, `retest`는 breakout/reclaim 레벨 재진입 대기, `value`는 일반 눌림 대기다
  - live execution layer는 학습 추천을 참고해 의미를 바꾸지 않는다. `MARKET` entry는 `market_urgent`, `LIMIT + immediate`는 `limit_aggressive`, `LIMIT + retest/value`는 `limit_passive`로 결정론적으로 번역한다
  - passive limit 가격도 intent별로 다르다. `retest`는 breakout level 근처 구조 가격을 우선 쓰고, `value`는 현재가 기준 passive offset을 사용하되 spread/ATR가 커지면 offset을 제한적으로 넓힌다
  - 프롬프트는 `retest`를 구조 레벨 근거가 있을 때만, `value`를 trend/invalidation이 여전히 허용되는 passive retrace일 때만 쓰도록 더 명확히 고정한다
  - 리스크 엔진은 entry 총량도 고정 규칙으로 제한한다. `max_leverage`는 계정 총 익스포저 상한이고, `max_order_notional_pct_balance`는 현재 모드에서 허용하는 주문 후보 총 notional의 상한이다. `drawdown`에서는 `entry_size_multiplier`, `confidence_cap`, `failure_pattern_size_multiplier`, `risk_trigger_size_multiplier`가 이 cap을 추가로 감쇠한다
  - 같은 cap 계산에서도 `evidence_quality` 외에 `setup_quality`를 추가로 본다. `retest`가 구조에서 너무 멀거나 `value`의 trend/invalidation/execution quality가 나쁘면 size cap을 더 줄인다
  - execution learning은 현재 live override 용도가 아니라 사후 분석과 파라미터 튜닝 후보 탐색 용도로만 남긴다
- `rationale`
  - 왜 그런 결정을 내렸는지에 대한 짧은 근거
- `rationale_items`
  - 기계 검증 가능한 구조화 근거
  - 각 항목은 `summary`, `why_it_matters`, `trade_implication`, `failure_mode`, `evidence[]`를 가진다
- `evidence[]`는 `signal`, `timeframe`, `operator`, `observed_value`, `threshold`, `units`, `source`, `confidence_contribution`을 포함한다
- `confidence_contribution`은 양수 가점뿐 아니라 음수 감점도 허용한다. 현재 범위는 `-1.0 .. 1.0`이다.
  - timeframe snapshot에는 `previous_values`가 포함될 수 있고, `cross_up/cross_down` 검증 시 source별 직전값으로 사용된다
- `risk_notes`
  - 감지한 위험 요인
- `confidence`
  - 0.0 ~ 1.0

현재 프롬프트는 아래 성격을 가진다.

- 전략가 역할만 수행
- 유효한 JSON만 반환
- 리스크 우회 금지
- 영어 텍스트 강제
- guarded 상태에서는 신규 진입보다 리스크 축소 우선
- retrieval context는 advisory only이며, hold-heavy retrieval이 현재 결정을 기계적으로 누르지 않게 한다
- `trend_alignment='mixed'`는 caution flag이지 단독 veto가 아니다
- `distance_to_invalid_level`은 size/stop-definition 입력이지 단독 veto가 아니다
- `risk_trigger`가 있으면 스프레드와 펀딩 같은 거래 품질 악화를 별도 위험 신호로 해석
- liquidation / hard cooldown / drawdown 같은 account-risk 압박은 신규 노출을 강하게 꺼리게 하지만,
  spread / depth / funding 같은 execution-quality 악화는 우선 size/confidence 감산으로 처리한다
- `rationale_items.evidence[].source`는 실제 `DecisionRequest` 경로를 가리켜야 하며, 관측값과 어긋나면 explanation invalid 또는 schema fallback으로 기록된다
- `operator`도 의미 검증한다. 즉 `gte`, `lt`, `cross_up`, `delta_abs_gte` 같은 비교가 실제 관측값과 threshold에서 성립하지 않으면 action/schema 단계에서 차단된다
- 신규 진입에서는 `trade_implication`이 실제 행동 방향과 맞지 않거나, 근거 품질 대비 confidence가 과하면 explanation invalid 또는 action/schema invalid로 분리 기록된다
- 신규 진입에서 `rationale_items`가 있으면 evidence 품질도 본다. 신호 다양성, symbol 커버리지, failure_mode 유무가 약하면 entry size cap을 더 낮춘다.
- 프롬프트에는 `entry_headroom`도 들어간다. 이는 남은 entry budget, pending entry notional, leverage 여유를 compact하게 요약한 입력이다.

최근 결정 결과에는 아래 품질 플래그가 함께 저장된다.

- `decision_valid`
- `explanation_valid`
- `execution_admissible`
- `learning_eligible`

즉 모델의 raw intent, 실행 가능 여부, 학습 적격성을 분리해서 본다.
설명 품질 문제는 기본적으로 `explanation_valid=false`, `learning_eligible=false`로 남기고,
행동 자체는 risk/execution gate가 막을 때만 실제 주문에서 차단하는 것이 현재 계약이다.
`fallback_safe_intent`가 발생해도 다음 cycle을 `15m` 관리 loop로 바꾸지 않고, 같은 `1h` 전략 계약으로 최대 두 번 더 빠르게 재시도한다.

## 리스크 관리 상세

`RiskEngine.evaluate_intent()`는 아래 순서로 검증한다.

### 1. 안전 행동 예외

- `HOLD`는 항상 허용
- `REDUCE`, `EXIT`, `CANCEL`은 일반적으로 리스크 축소 또는 주문 정리 행동으로 취급

### 2. 손실 기반 차단

신규 진입 행동인 `BUY`, `SELL`에 대해 아래를 검사한다.

- 일일 손실 한도 초과
- 연속 손실 횟수 초과
- 쿨다운 활성 여부

### 3. 모드별 행동 허용 여부

현재 리스크 프로파일의 `allowed_actions`에 현재 결정이 포함되는지 확인한다.

예:

- `cooldown`에서는 `BUY`, `SELL`을 막고 `HOLD`, `REDUCE`, `EXIT`, `CANCEL`만 허용할 수 있다.

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
- `guarded`에서는 축소
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
- 실행 동시성/중복 실행 가드

특히 `mode_profiles`가 운영 편의성에 중요하다.  
상황이 바뀌었을 때 코드 수정 없이 YAML 테이블만 조정하면 되기 때문이다.

실제 예시는 아래처럼 둘 수 있다.

```yaml
trading:
  symbols:
    - BTCUSDT
  max_leverage: 3
  order_type: "market"

risk_limits:
  daily_max_loss_pct: 10.0
  drawdown_activate_pct: 5.0
  max_consecutive_losses: 3
  cooldown_minutes: 30
  dynamic_cooldown:
    enabled: true
    base_minutes: 15
    repeated_failure_add_minutes: 15
  max_open_positions: 1
  mode_profiles:
    normal:
      max_open_positions: 1
      max_order_notional_pct_balance: 100
      allowed_actions: [HOLD, BUY, SELL, REDUCE, EXIT, CANCEL, REPLACE_ENTRY]
    guarded:
      max_open_positions: 1
      max_order_notional_pct_balance: 0
      allowed_actions: [HOLD, REDUCE, EXIT, CANCEL, REPLACE_ENTRY]
    cooldown:
      max_open_positions: 1
      max_order_notional_pct_balance: 0
      allowed_actions: [HOLD, REDUCE, EXIT, CANCEL, REPLACE_ENTRY]
    drawdown:
      max_open_positions: 1
      max_order_notional_pct_balance: 20
      entry_size_multiplier: 0.75
      confidence_cap: 0.55
      allowed_actions: [HOLD, BUY, SELL, REDUCE, EXIT, CANCEL, REPLACE_ENTRY]

intervals:
  strategy_loop_minutes: 60
  position_loop_minutes: 15
  trigger_loop_minutes: 5

daily_batch:
  enabled: true
  run_hour_local: 4
  run_minute_local: 0
  replay_timeout_seconds: 1800
  memory_timeout_seconds: 1800
  deepseek_memory_timeout_seconds: 5400

execution_guards:
  loop_lock_ttl_seconds: 240
  decision_cooldown_minutes: 10
  entry_mutex_ttl_seconds: 180
  unknown_order_block_minutes: 30
  recovery_sweep_minutes: 5
  symbol_reentry_close_cooldown_minutes: 10
  symbol_reentry_loss_cooldown_minutes: 30
  symbol_reentry_loss_threshold_pct: 0.0

context_budget:
  journal_fetch_limit: 24
  journal_anchor_limit: 24
  journal_keep_limit: 6
  memory_keep_limit: 8
  similar_case_keep_limit: 5
  similar_case_normal_keep_limit: 2
  max_rationale_items_per_journal: 2
  max_evidence_per_rationale_item: 3
  max_ignore_when_per_memory: 2
```

해석 예:

- `daily_max_loss_pct: 10.0`
  - 하루 손익률이 `-10.0%` 이하로 내려가면 신규 진입은 kill-switch에 걸린다.
  - 이 값은 일중 계좌 가드레일이고, 개별 트레이드 손실 한도는 별도로 둔다.
- `position_policy.losing_exit_pct`, `safety_policy.loss_exit_pct`
  - 15m/5m deterministic 손절은 포지션의 `account_unrealized_pnl_pct`를 기준으로 본다.
  - 현재 기본 per-trade 손실 한도는 `-3.0%`다.
  - 즉 `미실현손익 / 총자본 * 100` 기준이며, 레버리지로 증폭된 `margin ROE%`를 그대로 손절 트리거로 쓰지 않는다.
- `position_policy.invalidation_reduce_distance_atr`
  - 15m 관리 루프는 단순히 약손실이라고 줄이지 않는다.
  - 보유 시간이 지난 뒤 `distance_to_invalid_level`이 이 ATR 임계값 안으로 들어왔을 때만 thesis weaken 청산을 허용한다.
- `position_policy.microstructure_exit_confirmations`, `safety_policy.microstructure_exit_confirmations`
  - 15m/5m liquidity exit는 단일 snapshot 노이즈로 발동하지 않는다.
  - 최근 market snapshot history에서 연속으로 spread/depth 악화와 risk trigger가 확인될 때만 deterministic exit로 승격한다.
- `max_consecutive_losses: 3`
  - 현재 UTC 일자(`00:00 UTC` 이후) 안에서 손실 종료가 3번 연속 누적되면 신규 진입은 kill-switch에 걸린다.
  - 전일 손실 streak는 다음 UTC day boundary에서 리셋된다.
- `drawdown_activate_pct: 5.0`
  - hard stop 전에 먼저 `drawdown` 모드로 진입해 공격성을 줄인다.
- `guarded.max_order_notional_pct_balance: 0`
  - guarded 상태에서는 신규 진입을 허용하지 않는다. 이 상태는 진입 모드가 아니라 경계/관리 상태다.
- `cooldown.allowed_actions`
  - 쿨다운 구간에서는 신규 진입 없이 축소/청산 위주 행동만 허용할 수 있다.
- `dynamic_cooldown`
  - 작은 손실 1회는 짧게, 큰 손실이나 반복 실패 패턴은 더 길게 쉬도록 쿨다운 시간을 자동으로 늘린다.
- `execution_guards`
  - 루프 전역 락, 심볼별 entry mutex, 반복 판단 쿨다운, unknown 주문 상태 감시/복구 주기, recent close/loss 뒤 심볼 재진입 유예 시간을 정책에서 조정한다.
  - recent symbol re-entry guard는 `완전히 flat`일 때만 강하게 적용된다. 포지션, working order, active trade case가 남아 있으면 재진입 가드는 cycle risk state에 실리지 않는다.
  - startup 시 policy validator가 단일 심볼 구성, stale reprice 값 범위, re-entry cooldown 관계를 함께 검증한다.
- `daily_batch`
  - replay batch와 memory updater는 스케줄 시각에 백그라운드로 시작되고, 메인 trading loop를 블록하지 않는다.
  - 각 subprocess는 timeout을 가지며, hang 되어도 adaptive loop와 recovery sweep은 계속 돈다.
  - 수동 실행(`python -m batch.memory_updater`)과 별개로 자동 스케줄의 시각과 timeout만 제어한다.
- `context_budget`
  - Retrieval note: recent journal candidates now mix a recent slice with semantic anchor rows such as `good_hold`, `missed_opportunity_hold`, and loss/risk outcomes.
  - Lifecycle note: runtime `long_term_memory` retrieval and batch reconciliation now read the same full lifecycle-managed pool instead of separate recent caps.
  - Support note: `similar_past_cases` stays dense in guarded state, but normal mode still receives a small background-support case set.
  - Policy note: `policy_summary` is now a compact digest of enforceable limits, mode profiles, and execution guards rather than a raw `policy.yaml` dump.
  - Contract note: critical policy paths are registered in `RiskEngine.PROMPT_POLICY_CONTRACT`. Startup fails if the prompt digest cannot carry those fields.
  - Learning note: runtime retrieval scores are additionally adjusted by batch-produced `journal_reranker_profile` and `similar_case_reranker_profile` artifacts instead of relying on static heuristics alone.
  - 최근 journal/long_term_memory/similar case를 전부 보내지 않고, 관련성 높은 상위 컨텍스트만 압축해 전달한다.
  - recent_journal_context는 action 사례만 몰리지 않도록 hold/action/fallback 버킷을 분산 선택한다.
  - 단, explanation invalid 또는 learning ineligible audit는 prompt-safe retrieval에서 기본 제외한다.
  - similar_past_cases는 시스템 오류성 fallback audit, validation fallback audit, 그리고 그 audit에 연결된 trade case를 기본 제외한다.
  - long_term_memory는 `risk_control`, `opportunity`, `execution_quality`를 한쪽으로 쏠리지 않게 섞고, retired/저신뢰 항목은 기본 제외한다.

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

### 실행 안전 가드

`run_cycle` 실행 전후에는 아래 운영 가드를 적용한다.

- 전역 실행 락(`runtime_locks`)
  - 루프 시작 시 `loop_lock` 획득 실패면 해당 루프를 스킵해 다중 인스턴스/중복 실행을 차단한다.
- 결정 쿨다운 + fingerprint(`decision_fingerprints`)
  - 최근 N분 내 같은 `symbol + action + size_band`가 있으면 반복 진입 주문을 차단한다.
- Pending Exposure Reserve
  - 미체결 진입 주문을 가상 포지션처럼 포함해 `max_open_positions`, 노출/레버리지, 주문 크기 한도를 보수적으로 계산한다.
- Pre-trade Reconciliation
  - 주문 직전 `positions/open_orders`를 강제 재조회한 뒤 intent를 다시 리스크 검증한다.
- Per-symbol Entry Mutex(`entry_mutex`)
  - 심볼별 `entry in-flight` 락이 살아 있으면 동일 심볼 신규 진입을 차단한다.
- Order Runtime Unknown Guard
  - `unknown_order_block_minutes` 안에 `unknown` 상태 주문이 남아 있으면 신규 진입을 막는다.
- Recovery Sweep Job
  - 주기적으로 unknown 주문 상태를 재조회해 `completed/cancelled/failed/partial_filled`로 복구 시도한다.

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
- `market_vectors`
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
  "risk_mode": "guarded",
  "risk_profile": {
    "max_open_positions": 1,
    "max_order_notional_pct_balance": 0,
    "allowed_actions": ["HOLD", "REDUCE", "EXIT", "CANCEL", "REPLACE_ENTRY"]
  },
  "market_state": {
    "symbols": ["BTCUSDT"],
    "market_snapshots": {"BTCUSDT": {"symbol": "BTCUSDT"}},
    "trigger_symbols": ["BTCUSDT"],
    "risk_trigger_symbols": [],
    "rag_context": {
      "risk_state": {
        "daily_pnl_pct": -1.2,
        "consecutive_losses": 1,
        "cooldown_active": false
      },
      "account_state": {
        "total_balance": 1000.0,
        "available_balance": 760.0
      }
    }
  },
  "market_vectors": {
    "BTCUSDT": [0.24, 0.18, 1.0, 1.0, "... 26d ..."]
  },
  "execution_summary": {
    "loop_type": "Adaptive (5m)",
    "guarded_attention": true,
    "executed_order_count": 1
  }
}
```

여기서 `consecutive_losses`는 누적 전체 이력이 아니라 현재 UTC 거래일 안에서 아직 끊기지 않은 손실 streak다.

### `journal`

사람이 읽기 쉬운 요약 로그다.

- 결정
- rationale
- rationale_json
- confidence
- risk reason
- 추정 손익률
- 손실 신호 여부
- `trade_case_ids`, `case_statuses`, `case_summary`

`rationale` 텍스트는 사람용으로 유지한다.  
동시에 `rationale_json`에는 구조화 근거를 그대로 저장해 리플레이, 메모리 생성, 사후 분석에서 재사용한다.

최근 journal 요약을 AI에 다시 넣을 때는 텍스트 이유만 보내지 않는다.  
가능한 경우 audit/ledger와 연결해 실제 결과, 수수료, risk mode, guarded state 정보를 함께 요약해 피드백 루프를 강화한다.

최근 구조에서는 journal도 단순 1회성 로그에 머무르지 않고, 같은 사이클에서 생성된 심볼별 `trade_case_id`와 `case_status` 요약을 함께 남긴다.  
즉 포트폴리오 journal만 봐도 "이번 판단이 어떤 케이스를 열었는지, 줄였는지, 닫았는지"를 빠르게 복기할 수 있다.

daily memory batch는 `rationale_json.evidence`의 반복 시그널에서 heuristic `candidate_lessons`를 먼저 만들고, 선택된 LLM provider는 이를 정제하거나 병합하는 역할을 한다.  
LLM 호출이 실패해도 candidate lesson은 fallback으로 남을 수 있다.
다만 자동 학습 입력에는 모든 audit를 그대로 쓰지 않는다. `loop error`, `execution_summary.error`,
`ai_usage.validation_error`, `ai_usage.semantic_validation_error` 같은 시스템/스키마 오류 audit는 보존하되,
memory batch와 incremental replay의 자동 학습 후보에서는 제외한다.
runtime prompt retrieval도 같은 부류의 audit와 연결 trade case를 recent_journal_context / similar_past_cases 후보에서 제외한다.
추가로 memory batch는 replay의 `hold_opportunity_counterfactual`도 읽어,
`missed_breakout_count`, `missed_move_median_pct`, `hold_opportunity_cost`를 undertrading 지표로 집계한다.
이 수치가 반복되면 "trigger는 맞았지만 너무 자주 HOLD해서 기회를 놓친다"는 opportunity lesson seed를 만들고,
policy 평가에도 같은 opportunity cost를 함께 반영한다.
동시에 최근 closed entry case의 `false_entry_cost`도 같이 집계해,
기회를 너무 놓치는 문제와 너무 자주 잘못 진입하는 문제를 한쪽으로만 과적합하지 않게 균형 점수로 본다.
즉 memory batch는 `undertrading_pressure`, `overtrading_pressure`, `balance_score`를 함께 계산하고,
장기 교훈도 `good conservative hold`, `risk filtered hold`, `execution filtered hold`,
`structure filtered hold`, `probe only opportunity hold`, `missed opportunity hold`처럼 HOLD 품질을 나눠서 해석한다.
여기서 `probe only opportunity hold`는 공격적 probe baseline은 이겼지만 더 보수적인 simple-rule baseline은 이기지 못한 경우다.
즉 baseline 착시와 실제 missed opportunity를 분리하려는 중간 계층이다.
중요한 점은 이 균형 점수가 이제 같은 단위만 비교한다는 것이다.
`hold_opportunity_cost`와 offline review gate에 쓰이는 `false_entry_cost`는 둘 다 퍼센트 기준으로 비교하고,
실현 손익 금액은 `false_entry_cost_amount`로 별도 보조 메트릭으로만 남긴다.
또한 opportunity lesson seed는 문장만 좁아진 것이 아니라,
`trigger_context`, `execution_context`, `trend_context`, `hold_quality`를 구조화 memory에도 같이 저장한다.
이 컨텍스트는 저장만 하는 메타데이터가 아니다.
실시간 loop는 `long_term_memory`를 고를 때 기존 `apply_when.regime/volatility/trend` 축뿐 아니라,
현재 시장의 `trigger_context`, `execution_context`, `trend_context` signature와 직접 비교해 맞는 memory를 우선 적용한다.
특히 opportunity memory는 attention/execution/trend 문맥이 어긋나면 강하게 감점한다.
구조화 evidence는 단일 스키마를 유지하되 타입 규칙은 강하게 둔다.
즉 `gte/lte/gt/lt/cross_up/cross_down/delta_abs_gte` 는 숫자 값만 허용하고,
`eq/note` 만 숫자/문자열/불리언 evidence를 허용한다.
예를 들어 `rsi_14`, `spread_bps` 는 숫자 evidence로, `trend="FLAT"`, `momentum_regime="accelerating_down"` 는 `eq` 기반 범주형 evidence로 남긴다.

이 배치는 signal 반복뿐 아니라 `evidence_quality.factor` 버킷과 `entry_size_cap` 조정 패턴도 같이 집계한다.  
즉 근거 품질이 낮거나 size cap이 줄어든 상황이 실제 손실과 함께 반복되는지도 candidate lesson seed에 반영한다.

추가로 `symbol_journal`도 함께 읽어 심볼 단위 evidence signal 반복 패턴을 candidate lesson에 반영한다.  
즉 포트폴리오 단일 journal만 볼 때 생기던 심볼 간 혼선을 줄이고, 심볼별 교훈 추출을 강화한다.
이때 symbol 기반 candidate의 `evidence_journal_ids`는 `audit_log_id`가 아니라 `symbol_journal.id`를 사용한다.

### `long_term_memory`

일일 배치가 생성한 장기 규칙 저장소다.

- 규칙 본문
- 버전
- 구조화 메트릭
- `confidence_score` / `validity_score` / `decay_score`
- `trigger_context` / `execution_context` / `trend_context` / `hold_quality`
- `lifecycle_status` / `retirement_reason` / `revive_count`
- `canonical_key` 기반 중복 병합 메타데이터

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

### `symbol_journal`

포트폴리오 단일 journal과 별도로 저장하는 심볼 단위 로그다.

- `audit_log_id`, `symbol`
- `decision`, `risk_reason`, `risk_mode`, `risk_approved`
- `triggered`, `risk_triggered`
- `net_realized_pnl`, `fee_cost`, `estimated_pnl_pct`
- `trade_case_id`, `case_status`
- `case_opened_at`, `case_closed_at`, `holding_minutes`
- `evidence_signals`
- `summary`, `payload`
- 인덱스: `symbol+id`, `timestamp+id`, `audit_log_id`

즉, journal과 별도로 현재 운용 심볼의 신호/성과를 케이스 단위로 분리해 분석할 수 있다.

추가로 `symbol_journal`은 심볼별 포지션 생애주기를 `trade_case_id`로 연결한다.  
예를 들어 flat -> open은 `opened`, open -> reducing은 `reduced`, open -> closed는 `closed`로 기록되어 이후 배치나 수동 복기에서 "한 번의 트레이드 사례" 단위로 묶어 볼 수 있다.

운영 중 열려 있는 케이스는 내부 `active_trade_cases` 상태에 유지하고, 다음 cycle의 symbol journal이 같은 case id를 이어받는다.

### `trade_cases`

심볼별 트레이드 사례 이력 테이블이다.

- `trade_case_id`, `symbol`, `status`, `risk_mode`
- `opened_at`, `closed_at`
- `entry_audit_log_id`, `exit_audit_log_id`
- `entry_decision`, `exit_decision`, `entry_side`
- `entry_price`, `exit_price`
- `estimated_pnl_pct`, `realized_pnl`, `fee_cost`
- `holding_minutes`
- `observed_mfe_pct`, `observed_mae_pct`
- `slippage_cost`, `slippage_bps`
- `thesis_failure_reason`
- `evidence_signals`, `trigger_reasons`, `risk_trigger_reasons`
- `feature_vector`

여기서 `observed_mfe_pct` / `observed_mae_pct`는 단일 entry/exit 값이 아니라 저장된 `trade_case_snapshots` 경로를 다시 읽어 재계산한 excursion이다.  
가능하면 `entry_audit_log_id` / `exit_audit_log_id`의 실제 fill timestamp를 복원해, 그 체결 구간 안쪽 snapshot만 우선 사용한다.  
아직 체결 틱 단위는 아니지만, cycle별 경로를 누적하므로 이전보다 훨씬 신뢰도 높은 case quality 지표로 사용한다.

최근 `journal` 선별은 이제 단순 최신순 점수보다, 이 `trade_cases`의 심볼/guarded state/risk mode/regime/결과 패턴을 같이 보고 유사한 사례를 우선 선택한다.  
여기에 `feature_vector` 기반 cosine similarity도 섞어서 규칙 태그만으로는 놓치기 쉬운 수치적 유사 케이스를 같이 반영한다.

### `trade_case_snapshots`

열려 있거나 방금 닫힌 케이스의 cycle별 경로 저장소다.

- `trade_case_id`, `timestamp`, `audit_log_id`, `symbol`
- `current_price`, `mark_price`, `mid_price`
- `spread_bps`, `volume_ratio_15m`
- `unrealized_pnl_pct`
- `payload`

이 테이블은 케이스 경과 중의 가격/체결 품질/손익률 변화를 누적해서 나중에 excursion, slippage 문맥, 실패 패턴 복기에 사용한다.  
run cycle 때뿐 아니라 scheduler poll 구간에서도 active case를 경량 샘플링해서 `mark_price`/`mid_price`/`unrealized_pnl_pct` 경로를 더 촘촘히 남긴다.
여기 저장되는 `unrealized_pnl_pct`는 포지션/증거금 기준 ROE 경로이고, 15m/5m 손절 FSM이 보는 계좌 기준 손실률은 별도 runtime 필드(`account_unrealized_pnl_pct`)로 계산한다.

### `trade_case_vector_index`

유사 사례 검색용 별도 인덱스 테이블이다.

- `trade_case_id`, `symbol`, `status`, `risk_mode`
- `opened_at`, `closed_at`
- `estimated_pnl_pct`, `realized_pnl`, `slippage_bps`
- `thesis_failure_reason`
- `vector_norm`, `feature_vector`
- `payload`

즉 `trade_cases.feature_vector`를 본 테이블에 JSON으로만 들고 있는 것이 아니라, retrieval 전용 저장소로도 분리해서 관리한다.  
최근 context 선별과 `similar_past_cases`는 이 인덱스의 coarse `band hash` 후보를 먼저 찾고, 그 뒤 cosine similarity와 규칙 점수로 rerank한다.

### `retrieval_evaluations`

실제 프롬프트에 들어간 retrieval 결과를 나중에 복기할 수 있게 남기는 평가 로그다.

- `audit_log_id`, `context_type`, `rank_index`
- `trade_case_id`, `case_symbol`, `matched_symbol`
- `decision`, `decision_symbol_aligned`
- `estimated_pnl_pct`
- `similarity`, `final_score`
- `retrieval_source`, `matched_band_count`
- `outcome_trade_case_id`, `outcome_status`
- `outcome_realized_pnl`, `outcome_holding_minutes`
- `outcome_observed_mfe_pct`, `outcome_observed_mae_pct`, `outcome_slippage_bps`
- `risk_mode`, `payload`

즉 "무슨 유사 사례를 꺼냈는가"만이 아니라, 최종 결정 심볼과 정렬되었는지, 어떤 점수로 선택되었는지, 그리고 나중에 실제 trade case 결과가 어땠는지도 함께 남긴다.

추가로 `tools/replay_audit.py`는 같은 audit request를 `similar_past_cases` 포함/제거 두 버전으로 다시 돌려 `retrieval_counterfactual` 비교를 `replay_runs.payload`에 저장할 수 있다.
memory batch의 `retrieval_counterfactual_summary`에는 `outcome_linked_count`, `with_retrieval_better_count`, `without_retrieval_better_count`, `outcome_tie_count`, `avg_outcome_utility_delta`도 같이 들어가서,
단순 손익 부호뿐 아니라 실제 trade case의 `MFE/MAE`, `slippage`, `holding_minutes`까지 반영한 outcome utility 기준으로 어느 쪽이 더 나았는지 볼 수 있다.

짧은 개선 메모:
- `MFE/MAE`는 여전히 tick/event 기반이 아니라 snapshot 경로 기반이라, WS 이벤트 단위 path 수집으로 더 올릴 여지가 있다.
- retrieval은 SQLite coarse index + rerank 구조라, 케이스 수가 크게 늘면 FAISS 또는 `sqlite-vss` 같은 전용 ANN 인덱스로 옮길 수 있다.
- cluster/retrieval 평가는 자동화됐지만, 미래 holdout 성과와 실포트폴리오 utility 검증을 더 붙이면 더 견고해진다.

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

### `runtime_locks`

오케스트레이터 루프의 전역 락 저장소다.

- `lock_name`, `owner`
- `acquired_at_epoch`, `expires_at_epoch`

락 만료 기반으로 비정상 종료 후에도 다음 루프가 회복 가능하다.

### `entry_mutex`

심볼별 신규 진입 실행 락 저장소다.

- `symbol`, `owner`
- `acquired_at_epoch`, `expires_at_epoch`

같은 심볼에 대해 동시/중복 진입이 발생하지 않도록 보장한다.

### `decision_fingerprints`

반복 판단 쿨다운용 fingerprint 로그다.

- `symbol`, `action`, `size_band`
- `fingerprint`, `created_at`
- `audit_log_id`, `status`

최근 N분 기준으로 동일 판단 반복 여부를 빠르게 차단한다.

### `order_runtime_state`

주문별 최신 runtime 상태 스냅샷이다.

- `order_key`, `client_order_id`, `exchange_order_id`, `symbol`
- `state`, `updated_at`, `last_audit_log_id`, `payload`

`order_state_transitions`와 함께 사용해 `unknown` 상태 감시 및 recovery sweep 기반 복구를 수행한다.

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

운영 기본값은 `수동 단건 replay + 일일 incremental replay 자동 배치`다.
자동 경로는 최근 24시간의 대표 audit를 daily memory batch 직전에 저장한다.
기본 base limit은 `30`건이지만, 후보량이 많으면 `coverage_target`에 따라 자동으로 늘어나며 기본 max limit은 `60`건이다.
또한 자동 선별은 단순 recency가 아니라 아래 신호를 함께 본다.
- 최근 replay가 없는 material trade / rejected entry
- 큰 손실로 끝난 audit
- retrieval이 실제 decision 차이를 만들었던 audit
- missed opportunity 가능성이 높은 `HOLD`
자동 경로에는 `HOLD`도 별도 quota를 둬서, trade 케이스에 밀려 전부 사라지지 않게 한다.
이 자동 replay 후보는 학습 가능 audit만 대상으로 하고, 시스템 오류 fallback/loop error audit는 자동 경로에서 제외한다.
최근 버전의 replay는 포지션이 없고 `HOLD`였던 audit에 대해서도 이후 `30m / 2h / 6h` 가격 경로를 다시 읽는다.
즉 같은 시점에 단순 baseline entry를 넣었으면 충분히 먹을 수 있었는지 `hold_opportunity_counterfactual`로 저장한다.
baseline은 `attention active + acceptable spread/depth + manageable invalidation distance`를 만족할 때만 만든다.
이때 단순 진입 여부만 가정하지 않고,
- 방향은 trend / breakout / RSI / 단기 변화율을 조합해 추정
- 크기는 available balance 기준 소액 notional로 제한
- entry는 bid/ask와 spread/slippage를 반영한 assumed fill price로 가정
- stop/take-profit은 ATR 또는 invalidation 거리를 이용해 가정
- 미래 경로에서는 stop/take-profit hit 순서를 순차적으로 평가하고
- 수수료를 반영한 net realized move를 같이 계산
한다.
리플레이는 미래 가격 경로도 더 이상 `audit_logs.market_state`에만 의존하지 않는다.
최근 구조에서는 `market_snapshot_history` 시계열 테이블을 우선 읽고,
해당 구간 데이터가 부족할 때는 audit에 저장된 `market_state`를 fallback으로 섞어 보강한다.
즉 history row가 일부만 있어도 그 row만 쓰고 끝내지 않고, 부족한 timestamp 구간은 audit path로 채운다.
또한 baseline entry 하나만 보지 않고 더 보수적인 `simple_rule_entry`도 같이 계산한다.
즉 replay는 `probe_entry vs actual HOLD`뿐 아니라 `simple_rule_entry vs actual HOLD`도 함께 저장해,
"놓친 기회"가 단순 공격적 baseline 착시인지까지 구분하려고 한다.
그리고 memory/offline evaluation은 opportunity cost만 보지 않는다.
`turnover_rate`, `capital_idle_ratio`, `avg_holding_minutes`, `avg_entry_slippage_bps`,
`drawdown_path_penalty`, `regime_capacity_penalty`, `portfolio_utility_score`도 같이 계산해
"기회를 안 놓쳤다"가 아니라 "실제로 더 나은 포트폴리오 효용이었는가"까지 본다.

### 사용법

최근 리플레이 가능한 audit 목록:

```powershell
python -m tools.replay_audit --list
```

특정 audit 리플레이:

```powershell
python -m tools.replay_audit --audit-id 12
```

리플레이 결과를 DB에 저장:

```powershell
python -m tools.replay_audit --audit-id 12 --save
```

일일 incremental replay:

```powershell
python -m tools.replay_audit --auto --save --limit 30 --max-limit 60 --hold-quota 8 --window-hours 24
```

기본 정책 값:

- `replay_batch.limit`
  - 최소 처리 기준(base limit)
- `replay_batch.max_limit`
  - 후보가 많을 때 늘어날 수 있는 상한
- `replay_batch.coverage_target`
  - 후보 pool 대비 목표 처리율
- `replay_batch.hold_quota`
  - 자동 경로에서 보장할 `HOLD` replay 수
- `replay_batch.candidate_pool_limit`
  - 하루 후보 pool을 평가할 최대 개수

### 비교 항목

현재 diff는 아래를 포함한다.

- `decision_changed`
- `order_count_changed`
- `side_changed`
- `size_delta`
- `confidence_delta`
- `rationale_keywords_added`
- `rationale_keywords_removed`
- `evidence_signals_added`
- `evidence_signals_removed`
- `evidence_signal_count_delta`
- `missed_breakout_count`
- `missed_move_median_pct`
- `hold_opportunity_cost`
- `baseline_beats_hold`
- `simple_rule_baseline_beats_hold`
- `hold_quality`

즉, 예전엔 `BUY`였는데 지금은 `HOLD`인지, 같은 `BUY`여도 크기가 줄었는지, rationale의 중심 키워드가 무엇으로 바뀌었는지 바로 볼 수 있다.
또한 최근 구조에서는 `HOLD`를 하나의 값으로만 보지 않고,
좋은 보수적 HOLD인지, 리스크/실행 품질 때문에 필터된 HOLD인지, 아니면 놓친 기회였는지도 같이 구분한다.

이제 replay는 `rationale_items.evidence`도 비교한다.  
즉 어떤 evidence signal이 추가되거나 사라져서 결정이 바뀌었는지까지 추적할 수 있다.

대시보드는 최근 replay evidence diff, 최근 memory candidate seed, 최근 symbol_journal을 함께 보여준다.
또한 `Entry Styles 24h`와 `Post-Trade Labels` 요약을 추가로 보여줘 `immediate/retest/value`별 win rate, avg pnl pct, no-fill, missed-on-no-fill과 `edge_failed`, `missed_due_to_no_fill`, `risk_exit`, `stale_entry_cancelled` 라벨 빈도를 같이 본다.

audit detail 화면은 raw JSON 외에도 `rationale_items`와 evidence quality / entry size cap 요약을 카드로 보여준다.
`rationale_items.evidence[].source`는 클릭해서 stored DecisionRequest 안의 실제 값을 바로 lookup할 수 있다.
선택한 source는 Stored Request JSON에서도 해당 값이 강조되어 보인다.
또한 audit detail에서는 latest replay 기준으로 같은 source의 original vs replay observed value 차이도 함께 볼 수 있다.
이 source diff는 `source / original / replayed / delta` 표로 요약되어 숫자 변화가 바로 보이게 구성된다.
source lookup에는 current value뿐 아니라 previous value와 original/replay threshold도 함께 보여준다.
상단 카드에는 semantic validation fallback을 `24h / 7d / latest 50 calls` 기준으로 나눠 표시한다.
같은 카드에서 fallback이 어느 모델에서 더 자주 났는지도 모델별 failure rate와 함께 표시된다.

## 장기 메모리 배치 상세

`batch/memory_updater.py`는 최근 journal과 ledger 통계뿐 아니라 `trade_cases` 이력과 `trade_case_clusters` 요약도 함께 읽어 장기 교훈을 구조화한다.

최근 버전의 `trade_case_clusters`에는 `quality_score`, `quality_label`, `evaluation.silhouette_score`, `evaluation.consistency_score`가 같이 들어가서,
군집을 그냥 묶는 데서 끝나지 않고 "이 군집이 실제로 반복성과 설명력이 있는가"를 자동 평가한다.
또한 `retired`, `retirement_reason`을 함께 계산해, holdout에서 더 이상 같은 손실 패턴이 재현되지 않거나 품질이 너무 낮은 cluster는 candidate lesson seed에서 자동 제외한다.
배치는 replay에서 집계한 `hold_opportunity_summary`와 `trading_balance_summary`도 함께 읽는다.
즉 최근 시스템이 과도한 HOLD 편향인지, 반대로 false entry cost가 더 큰 과잉 진입 상태인지도 함께 본다.
이 값은 candidate lesson 생성과 offline review gate에는 들어가지만, live 정책 자동 승격에는 사용하지 않는다.
최근 review gate는 `hold_opportunity_cost`, `false_entry_cost`, `pressure_ratio`, `balance_score`뿐 아니라
`portfolio_utility_score`, `confidence_alignment_score`, `temporal_holdout_summary`도 같이 본다.
즉 한쪽 비용이 과도하게 큰 상태이거나 두 비용의 비율이 너무 깨져 있거나,
walk-forward 성격의 holdout 구간이 무너지거나 confidence calibration이 나쁘면 score가 좋아도 live 자동 반영 대상으로 보지 않는다.
여기서 `false_entry_cost`는 review용 정규화 퍼센트 비용이고, 금액 손실 합계는 `false_entry_cost_amount`로 별도 저장한다.
또한 memory batch는 `retrieval_evaluation_summary`와 `retrieval_counterfactual_summary`를 함께 읽어,
retrieval이 실제 성과와 반사실 비교 기준에서 얼마나 의미 있었는지, 그리고 그 차이가 path quality 기준으로도 유지되는지 장기 메모리 판단 근거로 남긴다.
또한 최근 journal의 `confidence`와 사후 outcome을 비교해 `calibration_gap`, `brier_score`,
`high_conf_error_rate`, `alignment_score`를 만들고, memory confidence와 policy evaluation에 같이 반영한다.
LLM은 이제 heuristic candidate lesson을 넓게 새로 발명하기보다,
기존 seed를 좁고 읽기 좋게 정제하는 역할에 더 가깝다.
generated lesson이 seed와 매칭되지 않으면 버리고, 매칭되더라도
`apply_when`, `trigger_context`, `execution_context`, `trend_context`, `hold_quality`는 seed의 좁은 조건을 우선 보존한다.

### 기본 원칙

- 통계 계산은 코드가 한다.
- 선택된 LLM provider는 통계를 해석해 lesson을 만든다.
- 근거 수가 적으면 `hypothesis`
- 근거 수가 충분하면 `rule`
- 모든 lesson은 적용 조건과 무시 조건을 가져야 한다.

### `MemoryLesson` 필드

- `rule`
- `regime`
- `category`
- `support_count`
- `evidence_journal_ids`
- `trade_case_patterns`
- `trade_case_clusters`
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
- 최근 context mix
  - `good_hold / missed_hold / action / fallback / other_hold`
- 최근 수수료
- 최근 ledger net pnl
- 최근 audit 목록
- 최근 memory
- 최근 symbol_journal
- 최근 trade_cases
- 최근 fills
- 최근 ledger
- 최근 replay 결과
- 심볼별 성과 리포트
- 일별 ledger 리포트
- 특정 audit 상세
- 특정 audit replay 실행

대시보드 상단 `Context Mix` 카드는 최근 24시간 audit에 저장된 `execution_summary.context_selection_summary`를 집계해
실제 프롬프트 컨텍스트가 action-heavy인지, fallback이 과도한지, good hold가 충분히 회수되는지 확인하는 용도다.
good hold는 보수성이 유효했던 사례, missed hold는 기회를 놓친 사례로 따로 본다. 둘 다 retrieval의 참고 근거이지 현재 결정을 투표로 누르는 신호는 아니다.
운영 해석상 good hold는 불필요한 손실 회피 사례, missed hold는 진입 가능한 구조를 놓친 사례다.
둘 다 memory/replay에서 따로 집계하되, live prompt에서는 advisory evidence로만 사용한다.

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

### 설정 동기화

- 대시보드는 `.env`를 읽고 `DB_PATH`를 그대로 사용한다.
- `DB_PATH`를 비우면 기본값 `data/gpt_trader.db`를 사용한다.
- 새 스키마로 다시 시작할 때는 Docker bind mount의 기존 `./data/gpt_trader.db`를 그대로 재사용하지 말고 비운 뒤 시작한다.
- 상단 모델 라우팅 표시는 `AI_PROVIDER`, `MEMORY_PROVIDER`, `REPLAY_PROVIDER`와 각 모델 env를 반영한다.
- audit replay 버튼은 `REPLAY_*` 설정을 우선 사용하고, 비어 있으면 `AI_*` 설정으로 fallback 한다.

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
- `AI_PROVIDER` 또는 provider-specific key (`GEMINI_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY`)
- `AI_DEFAULT_MODEL` 또는 provider-specific default model
- `DB_PATH`
- `LOG_LEVEL`
- `LOG_FORMAT`

선택 변수:

- `ALERT_WEBHOOK_URL`
- `ALERT_WEBHOOK_FORMAT`
  - `auto` 기본값. custom receiver는 기존 JSON payload를 유지하고, Slack/Discord incoming webhook URL은 자동 감지한다.
  - `json` 또는 `generic`: 항상 `{title, message, level}` JSON으로 보낸다.
  - `slack`: Slack incoming webhook 포맷으로 보낸다.
  - `discord`: Discord webhook 포맷으로 보낸다.

예시:

```env
BYBIT_API_KEY="your_bybit_api_key_here"
BYBIT_API_SECRET="your_bybit_api_secret_here"
BYBIT_TRADING_MODE="demo"
BYBIT_TESTNET="False"
AI_PROVIDER="deepseek"
AI_API_KEY=""
AI_DEFAULT_MODEL="deepseek-reasoner"
AI_GUARDED_ATTENTION_MODEL="deepseek-reasoner"
MEMORY_PROVIDER="deepseek"
MEMORY_MODEL="deepseek-reasoner"
REPLAY_PROVIDER="deepseek"
REPLAY_MODEL="deepseek-reasoner"
GEMINI_API_KEY=""
OPENAI_API_KEY=""
DEEPSEEK_API_KEY="your_deepseek_api_key_here"
ANTHROPIC_API_KEY=""
DB_PATH="data/gpt_trader.db"
LOG_LEVEL="INFO"
LOG_FORMAT="text"
ALERT_WEBHOOK_URL=""
ALERT_WEBHOOK_FORMAT="auto"
```

웹훅 payload:

- `json` / `generic`

```json
{"title":"Execution Timeout","message":"...","level":"error"}
```

- cycle decision summary 예시

```json
{"title":"Cycle Decision","message":"loop=1h risk=guarded role=strategy decision=BUY approved=yes model=gemini-2.5-pro\nwhy=Breakout retest has volume support.\norders=BUY LIMIT retest x1","level":"info"}
```

- `slack`

```json
{"text":"*Execution Timeout*\n...\n_Level: ERROR_"}
```

- `discord`

```json
{"content":"**Execution Timeout**\n...\n`level: ERROR`"}
```

Gemini 예시는 다음처럼 바꾸면 된다.

```env
AI_PROVIDER="gemini"
AI_DEFAULT_MODEL="gemini-2.5-pro"
AI_GUARDED_ATTENTION_MODEL="gemini-2.5-pro"
GEMINI_API_KEY="your_gemini_api_key_here"
```

모델 선택:

- `AI_PROVIDER`
  - `gemini`, `openai`, `deepseek`, `anthropic`
- `AI_DEFAULT_MODEL`
  - 일반 루프에서 사용하는 기본 모델
- `AI_GUARDED_ATTENTION_MODEL`
  - guarded-attention/safety-context에서 AI가 실제 호출될 때만 따로 쓰고 싶을 때 지정
  - 비우면 `AI_DEFAULT_MODEL`을 재사용
- `MEMORY_PROVIDER`
  - 비우면 `AI_PROVIDER`를 재사용
- `MEMORY_MODEL`
  - 일일 memory batch에서 사용하는 모델
  - 비우면 provider-specific memory/trigger/default 순서로 fallback
- `REPLAY_MODEL`
  - 일일 incremental replay에서 사용하는 모델
  - 비우면 memory/default 계열 fallback을 사용
- provider-specific env (`GEMINI_*`, `OPENAI_*`, `DEEPSEEK_*`, `ANTHROPIC_*`)
  - 공통 env를 쓰지 않을 때 개별 provider 설정용
  - Gemini 단일 provider 구성을 계속 유지하고 싶을 때도 그대로 사용 가능

구조화 출력:

- Gemini는 native schema JSON 응답을 사용한다.
- OpenAI와 DeepSeek는 chat completion의 `json_object` 모드를 우선 사용한다.
- Anthropic은 JSON-only system prompt를 사용하고, 모든 provider는 마지막에 Pydantic 검증으로 `TradeIntent` 스키마를 다시 확인한다.
- DeepSeek reasoning 계열은 응답 시간이 수 분까지 길어질 수 있다. 특히 `AI_GUARDED_ATTENTION_MODEL=deepseek-reasoner`를 쓰면 첫 guarded-attention cycle이 느릴 수 있다.

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
- replay batch 설정
- daily batch 스케줄/timeout
- `MEMORY_PROVIDER=deepseek`이면 memory batch timeout은 `deepseek_memory_timeout_seconds` 이상으로 자동 상향된다.

## 실행 방법

자동매매 시작:

```powershell
python main.py
```

자동 스케줄의 replay batch + memory updater는 백그라운드 데몬 스레드에서 별도 subprocess로 실행된다.  
따라서 daily batch가 오래 걸리거나 timeout 되어도 메인 adaptive loop, snapshot materialization, recovery sweep은 계속 진행된다.

대시보드 실행:

```powershell
python -m dashboard.app
```

Dashboard startup now resolves `.env` and opens `StateStore` lazily on the first request, so importing the module for tests or tooling no longer opens the DB immediately.

대시보드의 `Learning Liveness` 카드에는 다음이 함께 표시된다.

- 마지막 replay batch 시각
- 마지막 memory update 시각
- 최근 24시간 replay candidate 처리율
- recent journal / similar case reranker artifact 준비 여부
- shadow dataset 준비 여부
- stale learning으로 인한 offline evaluation 차단 여부

`Learning Liveness`가 stale이면 daily batch는 replay/memory 산출물을 계속 저장하더라도, offline evaluation artifact는 신뢰 가능한 최신 학습 상태로 간주하지 않는다.

대시보드는 `.env`의 `DB_PATH`를 사용해 같은 SQLite를 읽는다.  
audit replay를 대시보드에서 실행할 때는 `REPLAY_PROVIDER`, `REPLAY_API_KEY`, `REPLAY_MODEL`을 우선 보고, 비어 있으면 `AI_PROVIDER`, `AI_API_KEY`, `AI_DEFAULT_MODEL` 계열 설정으로 fallback 한다.

접속 주소:

```text
http://127.0.0.1:5000
```

장기 메모리 배치 수동 실행:

```powershell
python -m batch.memory_updater
```

context budget 튜닝(최근 audit 로그 기반):

```powershell
python tools/tune_context_budget.py --lookback 400 --target-token-p75 2800
```

추천값을 정책에 반영:

```powershell
python tools/tune_context_budget.py --lookback 400 --target-token-p75 2800 --write-policy
```

## 테스트

현재 테스트는 `unittest` 기반이다.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -q
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

문서상 고정 테스트 개수는 유지하지 않는다. 기준은 repo-local unittest 스위트가 전체 통과하는 것이다.

## 운영 문서

- 수익률 부진 진단, 원인별 조치, 실험 순서는 [`PROFITABILITY_TROUBLESHOOTING.md`](./PROFITABILITY_TROUBLESHOOTING.md)를 참고한다.

## 운영 메모

- 기본 거래소 타입은 Bybit linear USDT perpetual이다.
- 시장 데이터는 항상 Bybit 메인넷 public API에서 가져온다.
- `BYBIT_TRADING_MODE=live`면 실계좌로 주문과 계좌 조회를 수행한다.
- `BYBIT_TRADING_MODE=demo`면 Bybit Demo Trading 계정으로 주문과 계좌 조회를 수행한다.
- `BYBIT_TESTNET`은 구버전 호환용 fallback이다. `BYBIT_TRADING_MODE`가 우선한다.
- 시장 판단에는 미완성 캔들을 제외한 종가 데이터가 사용된다.
- `EXIT` 의사결정에 `orders`가 비어 있으면 현재 포지션을 기준으로 reduce-only 시장가 청산 주문을 자동 생성한다.
- `CANCEL` 의사결정은 stale working order를 실제로 취소하는 실행 경로를 가진다.
- stale entry order는 15m/5m 관리 루프에서 재호가하지 않는다. 현재 계약은 cancel-only다.
- order runtime/state transition에는 `replace_group_id`, parent order id, lineage role이 함께 저장돼 원래 주문 -> 취소 -> 대체 주문 흐름을 추적할 수 있다.
- live position이 있으면 `1h` 전략 루프도 새 엔트리를 열지 않고 기존 케이스 관리만 수행한다.
- 즉 한 번 열린 트레이드는 기본적으로 같은 `trade_case_id` / `thesis_id` 아래에서 관리되고, 반대 새 thesis로 즉시 뒤집는 행동은 허용하지 않는다.
- 주문 실행 중 일반 예외가 나면 receipt에 `failed` 상태로 남는다.
- 알림 웹훅이 없으면 알림은 로그만 남기고 종료한다.

## 유지보수 상세

운영 중에는 "코드가 돌아간다"보다 "판단이 여전히 유효하다"를 계속 확인해야 한다.
이 프로젝트에서 유지보수는 크게 6가지를 주기적으로 보는 작업이다.

### 일간 체크리스트

1. 대시보드에서 `ledger net pnl`, `Symbol Report`, `Semantic Fallbacks`를 확인한다.
2. `24h / 7d / latest 50 calls` 기준 semantic fallback이 갑자기 늘었는지 본다.
3. 최근 audit 5~10개에서 `risk_reason`, `evidence_quality`, `entry_size_cap`이 이상하게 치우치지 않았는지 본다.
4. `wide_spread`, `elevated_funding`, `cooldown`, `drawdown`이 특정 시간대에 몰리는지 확인한다.
5. Bybit 실제 포지션/미체결과 audit의 `positions`, `open_orders`, `receipts`가 맞는지 샘플 확인한다.
6. `timeout_unresolved`, `failed`, market fetch warning이 반복되는지 로그를 본다.

### 주간 체크리스트

1. `tools/replay_audit.py`로 최근 손실 audit 10~20개를 다시 비교한다.
2. replay에서 `evidence_signals_added/removed`, `source diff`, `previous value / threshold`를 점검한다.
3. `long_term_memory`의 최근 rule과 `candidate_lessons`를 읽고 과잉 일반화가 없는지 확인한다.
4. 심볼별 net pnl을 보고 손실 심볼을 유지할지 제외할지 결정한다.
5. 정책 변경이 필요하면 한 번에 하나의 파라미터만 조정한다.
6. 변경 후에는 반드시 demo 또는 축소된 size로 관찰한다.

### 월간 체크리스트

1. `policy.yaml`의 손실 한도, size cap, max positions, trigger threshold를 다시 검토한다.
2. `GEMINI_DEFAULT_MODEL`, `GEMINI_GUARDED_ATTENTION_MODEL`, `GEMINI_MEMORY_MODEL` 역할 분리가 여전히 적절한지 본다.
3. SQLite 파일 크기, `audit_logs`, `position_ledger`, `replay_runs` 증가량을 점검한다.
4. 오래된 audit archive 정책이 필요한지 판단한다.
5. 오래된 memory rule 중 현재 시장 레짐에서 더 이상 맞지 않는 항목을 pruning 할지 검토한다.
6. 실계좌 운영이면 월간 최대 drawdown, symbol concentration, execution quality를 따로 리뷰한다.

### 1. 모델 품질 점검

- 대시보드의 `Semantic Fallbacks`를 먼저 본다.
- `24h / 7d / latest 50 calls` 기준으로 fallback 수가 올라가면 최근 모델 변경, 프롬프트 변경, journal 문맥 변화가 있었는지 확인한다.
- 특정 모델의 failure rate가 높으면:
  - `GEMINI_DEFAULT_MODEL`, `GEMINI_GUARDED_ATTENTION_MODEL`, `GEMINI_MEMORY_MODEL` 분리를 다시 점검
  - 저가 모델을 실시간 루프에만 쓰고, memory batch는 상위 모델 유지
  - fallback issue가 `operator_condition_failed`인지 `trade_implication_action_mismatch`인지 구분
- replay detail에서 `current value / previous value / threshold`를 같이 보면서 모델이 숫자는 맞게 읽었는지, 교차 해석을 잘못했는지 확인한다.

### 2. 정책 파일 유지

- `policy.yaml`은 코드보다 먼저 점검하는 운영 파일이다.
- 아래 항목은 시장 레짐이 바뀌면 재조정 대상이다:
  - `risk_limits.daily_max_loss_pct`
  - `risk_limits.max_consecutive_losses`
  - `risk_limits.mode_profiles.*.max_order_notional_pct_balance`
  - `risk_limits.mode_profiles.*.max_open_positions`
  - `trading.max_leverage`
  - trigger 관련 threshold
- `risk_limits.max_consecutive_losses`는 현재 UTC day boundary 기준으로만 집계되므로, day-over-day 손실 streak를 의도했다면 코드가 아니라 운영 정책을 다시 설계해야 한다.
- 정책 변경 후에는 바로 실거래에 넣지 말고:
  1. demo 모드로 며칠 관찰
  2. replay 결과 비교
  3. journal / audit에서 거절률과 size cap 변화 확인

### 3. 데이터 품질 점검

- market snapshot의 핵심 필드가 비정상적으로 비어 있지 않은지 본다:
  - `timeframes`
  - `order_book`
  - `derivatives`
  - `previous_values`
- public/private WebSocket이 끊기면 REST fallback으로 운영은 되지만, 호출 수와 지연이 올라갈 수 있다.
- private WS health는 시작 직후 잠깐 `reconnecting`일 수 있다.
- 안정화 시간 이후에도 계속 `health=reconnecting, reconcile_due=False`가 반복되면 health 판정이나 private 이벤트 수신 경로를 점검해야 한다.
- private 계좌 변화가 없는 조용한 구간만으로 `WebSocket Stale`가 반복되던 문제는 완화되었고, 이제는 transport silence와 reconcile TTL을 함께 봐야 한다.
- `not in approved policy list`를 볼 때는 심볼 포맷 차이(`ETHUSDT` vs `ETH/USDT:USDT`)가 내부 정규화로 해소되는 구조인지 먼저 확인한다.
- 로그에서 반복적으로 다음이 보이면 데이터 품질 이슈로 본다:
  - `Failed to fetch market data`
  - `Bulk open-order fetch failed`
  - semantic fallback 급증
- 이 경우 원인을 나눠 본다:
  - 거래소 응답 문제
  - WebSocket 세션 문제
  - 특정 필드 schema drift
  - 너무 공격적인 timeout / TTL 설정

### 4. 계좌/실행 동기화 점검

- 주문 체결 후 audit와 실제 Bybit 상태가 맞는지 주기적으로 확인한다.
- 특히 아래가 어긋나면 운영 리스크가 커진다:
  - `open_orders`
  - `positions`
  - ledger net pnl
  - synced fills
- 점검 순서:
  1. 최근 audit의 `receipts`
  2. `order_state_transitions`
  3. `position_state_transitions`
  4. Bybit 실제 미체결/포지션
- timeout 관련 receipt가 늘어나면 실거래 전환이나 size 확대를 멈추는 게 맞다.

### 5. 장기 메모리 유지

- `long_term_memory`는 많이 쌓이는 것보다 "나쁜 교훈을 오래 유지하지 않는 것"이 더 중요하다.
- daily memory batch 결과는 아래를 같이 검토한다:
  - `candidate_lessons`
  - `winning_evidence_signals`
  - `losing_evidence_signals`
  - `evidence_quality_patterns`
- memory rule은 다음 경우 재검토 대상이다:
  - support는 높은데 이후 replay에서 자주 무시됨
  - 특정 regime에서만 맞고 다른 regime에서 계속 손실
  - 저품질 evidence에서 나온 anti-pattern이 너무 일반화됨
- memory batch 모델을 바꿨다면 최소 며칠은 새 rule의 tone과 과잉 일반화 여부를 직접 읽어보는 게 좋다.

### 6. DB / 로그 유지

- SQLite 단일 파일 운영이므로 아래를 주기적으로 확인한다:
  - DB 파일 크기 증가 속도
  - `audit_logs`, `position_ledger`, `replay_runs` 증가량
  - 로그 파일 rotation 정상 동작 여부
- 운영이 길어지면 archive 정책을 정하는 게 좋다:
  - 최근 30~90일 audit는 hot storage
  - 그 이전은 별도 백업
- replay와 memory를 계속 쓸 계획이면 `audit_logs`는 함부로 지우지 않는 편이 좋다.
- 최근 `HOLD` 편향만 빠르게 씻고 싶으면 전체 DB 삭제보다 부분 초기화를 먼저 권한다.
  - 보존: `audit_logs`, `journal`, `ledger`
  - 재생성: `long_term_memory`, `learning_artifacts`, `replay_runs`, `data/learning/`
  - 이 경우 런타임 프로세스를 재시작해 in-memory artifact cache도 같이 비운다.

## 수익률 진단

수익률이 기대에 못 미칠 때는 모델을 바로 교체하기보다, 원인을 `엣지 / 비용 / 과잉거래 / 사이징 / 데이터 품질 / 운영 안정성 / LLM 품질`로 분리해서 봐야 한다.  
상세 체크리스트와 원인별 개선 실험은 [`PROFITABILITY_TROUBLESHOOTING.md`](./PROFITABILITY_TROUBLESHOOTING.md)에 정리되어 있다.

프로젝트의 철학, 의도된 편향, 프롬프트 계약, 학습/승격 원칙은 [`SYSTEM_PHILOSOPHY.md`](./SYSTEM_PHILOSOPHY.md)에 따로 정리했다.
현재 한계와 다음 단계 우선순위는 [`IMPROVEMENT_ROADMAP.md`](./IMPROVEMENT_ROADMAP.md)에 정리했다.

최근 15m/5m 수정 방향도 이 철학과 같다.

- `1h`는 기본적으로 thesis 생성, live position이 있으면 새 thesis 금지
- `15m`는 thesis-aware management, 약손실 자체보다 invalidation 근접과 시간 경과를 우선
- `5m`는 execution safety emergency path만 담당
- 정책 진화는 즉시 반응하지만 `long_term_memory`는 새 사례가 누적되며 천천히 반영
- 운영 계약을 크게 바꿀 때는 기존 학습 표면을 보존하려 애쓰기보다 `data/gpt_trader.db`와 `data/learning/`을 비우고 새로 시작하는 쪽을 기본으로 둔다.

## 권장 운영 순서

1. Bybit Demo Trading 키로 시작하고 `BYBIT_TRADING_MODE=demo`로 둔다.
2. `policy.yaml`의 손실 한도와 `mode_profiles`를 보수적으로 설정한다.
3. `python main.py`로 로그와 audit를 충분히 쌓는다.
   - startup config error는 now structured startup failure로 처리되고, 명시적인 non-zero exit로 끝난다.
4. `python -m dashboard.app`에서 input hash, risk mode, 상태 전이, replay diff, context mix, learning liveness를 확인한다.
5. `python -m tools.replay_audit --list`와 `--audit-id`로 과거 결정 변화 양상을 점검한다.
6. 장기 메모리에 저장된 `apply_when`, `ignore_when`가 실제 운영 관점에서 타당한지 검토한다.
7. 문제가 없을 때만 `BYBIT_TRADING_MODE=live` 전환을 검토한다.

## 참고 파일

- 메인 엔트리포인트 facade: [`main.py`](./main.py)
- 오케스트레이터 구현: [`orchestrator/bootstrap.py`](./orchestrator/bootstrap.py)
- 정책 파일: [`policy.yaml`](./policy.yaml)
- 데이터 모델: [`core/models.py`](./core/models.py)
- 리스크 엔진: [`core/risk_engine.py`](./core/risk_engine.py)
- 상태 저장소 facade: [`db/state_store.py`](./db/state_store.py)
- 상태 저장소 구현: [`db/state_store_parts/`](./db/state_store_parts/)
- 의사결정 엔진: [`ai/decision_engine.py`](./ai/decision_engine.py)
- 실행 엔진: [`exchange/execution_engine.py`](./exchange/execution_engine.py)
- 메모리 배치: [`batch/memory_updater.py`](./batch/memory_updater.py)
- 리플레이 도구: [`tools/replay_audit.py`](./tools/replay_audit.py)
- 시스템 철학: [`SYSTEM_PHILOSOPHY.md`](./SYSTEM_PHILOSOPHY.md)
- 개선 로드맵: [`IMPROVEMENT_ROADMAP.md`](./IMPROVEMENT_ROADMAP.md)
- 대시보드: [`dashboard/app.py`](./dashboard/app.py)
## 실행 정책

주문 타입은 더 이상 전역 `market` 또는 `limit` 고정이 아니다.

- `ExecutionPolicyEngine`이 `loop_mode`, `entry/reduce/exit`, urgency, spread/depth 상태를 보고
  `MARKET`, `LIMIT_PASSIVE`, `LIMIT_AGGRESSIVE` 성격의 실행 프로필을 선택한다
- 실제 결과는 `execution_policy_evaluations`에 저장된다
- 여기에는 일반 진입/청산뿐 아니라 `CANCEL` 성공률, timeout, stale order 정리 문맥도 함께 남긴다
- 배치는 이를 집계해 offline 평가 지표와 후보 비교 기록으로만 남긴다

기본 원칙은 다음과 같다.

- 손절성 청산, 긴급 축소, emergency exit: 체결 확실성이 우선
- 일반 진입: 시장 상태가 괜찮으면 limit 우선, urgency가 높으면 market
- 부분익절: limit 우선

## 현재 안전 가드레일

자동 진화하지 않는 값은 의도적으로 남겨 둔다.

- `trading.max_leverage`
  - 현재 `3x`
  - projected exposure가 `총자산 x 3`을 넘으면 진입 거절
- `risk_limits.drawdown_activate_pct`
  - drawdown 진입 자체는 고정 가드레일
- `risk_limits.daily_max_loss_pct`
  - 일일 손실 kill switch

즉 시스템은 `실효 참여율`은 진화시키되, `레버리지 ceiling`과 `drawdown 트리거`는 진화시키지 않는다.
