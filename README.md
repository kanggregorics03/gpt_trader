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
journal + ledger stats -> MemoryUpdater -> long_term_memory
```

## 프로젝트 구조

```text
gpt_trader/
├─ main.py                    # 메인 오케스트레이터, 단일 adaptive loop 시작점
├─ policy.yaml                # 거래/리스크 정책
├─ .env.example               # 환경 변수 예시
├─ ai/
│  ├─ decision_engine.py      # Gemini 의사결정 생성, 입력 해시 생성
│  └─ rag_engine.py           # 유사 과거 케이스 검색
├─ batch/
│  └─ memory_updater.py       # 장기 메모리 일일 배치
├─ core/
│  ├─ models.py               # Pydantic 데이터 모델
│  ├─ risk_engine.py          # 정책 검증, 리스크 모드/프로파일 계산
│  └─ snapshot_service.py     # 공용 계좌/시장/리스크 스냅샷 조립
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

### 1. SnapshotService가 공용 상태를 조립

`main.py`의 오케스트레이터는 더 이상 `1시간/15분/5분` 스케줄을 각각 돌리지 않는다.  
대신 하나의 adaptive loop가 현재 상태에 따라 다음 실행 주기를 `1h`, `15m`, `5m` 중 하나로 선택한다.

각 사이클의 시작에서는 `SnapshotService`가 아래를 한 번에 만든다.

- `AccountReader`로 계좌 상태 조회
- `StateStore`로 현재 리스크 상태 계산
- `MarketCollector`로 심볼별 시장 스냅샷 수집
- 현재 스냅샷을 바탕으로 `is_trigger_mode`, `risk_mode`, `risk_profile` 결정

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
  - WS가 복구되더라도 바로 primary로 복귀하지 않고, private 이벤트 수신 재개 + REST reconcile 1회 성공 + 안정 구간을 통과해야 다시 `healthy`
  - `get_runtime_diagnostics()`로 account cache freshness와 private WS health / event count / lag를 한 번에 확인할 수 있다
- `MarketCollector`
  - 현재가
  - ATR 15m / ATR 1h
  - 1시간 추세
  - 15분 거래량 비율과 즉시 거래량 급증 여부
  - 히스테리시스가 반영된 fast move / volume surge / range break trigger 상태
  - open interest / funding / basis의 변화율 요약
  - spread와 depth의 최근 1시간 상대 수준(percentile / avg 대비 악화 여부)
  - breakout 기준선 및 invalidation까지의 거리 요약
  - opportunity trigger 여부와 사유
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

### 4. 단일 adaptive loop와 cadence 전환

현재 구현에서 중요한 변경점은 `루프가 하나만 돈다`는 것이다.  
오케스트레이터는 한 번의 사이클이 끝날 때마다 현재 상태를 보고 다음 cadence를 고른다.

우선순위는 다음과 같다.

- `5m`
  - opportunity trigger가 활성화된 경우
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
  - trigger가 켜지면 즉시 승격
- `5m -> 15m or 1h`
  - trigger가 꺼졌다고 바로 강등하지 않고 clear streak를 만족해야 함
- `15m -> 1h`
  - flat 상태가 여러 사이클 연속 확인돼야 함

이 히스테리시스는 cadence가 `5m <-> 15m <-> 1h` 사이에서 경계값 근처에서 깜빡이는 현상을 줄이기 위한 것이다.

또한 cadence마다 역할도 다르다.

- `1h`
  - 전략 판단 중심
  - 신규 진입과 방향성 결정을 담당
- `15m`
  - 포지션/주문 관리 중심
  - 기본적으로 신규 진입보다 `REDUCE`, `EXIT`, 주문 정리 쪽으로 제한
- `5m`
  - trigger 대응 중심
  - active trigger 구간에서 더 짧은 주기로 반응
  - 기본 정책에서는 포지션이 없을 때만 첫 진입을 허용하고, 이미 포지션이 있으면 추가 trigger-mode 진입 대신 `HOLD / REDUCE / EXIT` 위주로 제한

### 5. Trigger 분리 원칙

이 프로젝트는 트리거를 두 부류로 나눈다.

- `opportunity_trigger`
  - 시장에 실제 이벤트가 발생해 더 자주 관찰하거나 대응할 가치가 있는 상황
  - 예: `fast_price_move_15m`, `volume_surge_15m`, `range_break_1h`
- `risk_trigger`
  - 거래 품질이나 포지션 보유 비용이 나빠져 신규 진입을 줄이거나 막아야 하는 상황
  - 예: `wide_spread`, `elevated_funding`

중요한 점은 `trigger mode` 자체가 단순히 `opportunity_trigger` 하나로 켜지지는 않는다는 것이다.  
반면 `risk_trigger`는 AI와 리스크 엔진에 전달되어 더 보수적인 행동을 유도한다.  
현재 기본 구현에서는 `risk_trigger`를 신규 진입의 하드 차단으로 쓰기보다, 승인 사유에 advisory로 남겨 판단 맥락을 강화한다.

현재 구현에서는 별도의 `Trigger (5m)` 작업이 따로 스케줄되지 않는다.  
대신 공용 스냅샷에서 같은 심볼에 대해 `opportunity_trigger_reasons`가 2개 이상이면서, 그 안에 `volume_surge_15m` 또는 `range_break_1h` 같은 strong trigger가 포함될 때만 다음 cadence가 `5m`로 승격된다. fast move 단독으로는 승격되지 않는다.

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

### 6. AI 입력 구성

`DecisionRequest`는 아래를 포함한다.

- `policy_summary`
- `account_state`
- `market_snapshot` 또는 `market_snapshots`
- `risk_state`
- `recent_journal_context`
  - 최근 journal 문맥을 구조화 객체로 전달
  - `rationale_items`, `evidence_quality`, `entry_size_cap`, trigger 정보까지 포함
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

또한 시장 스냅샷은 raw 값만 나열하지 않고, `oi_change_15m`, `oi_change_1h`, `funding_rate_change_1h`,
`basis_change_1h`, `spread_percentile_1h`, `depth_percentile_1h`, `spread_vs_1h_avg`,
`distance_to_15m_breakout_level`, `distance_to_1h_breakout_level`, `distance_to_invalid_level`처럼
LLM이 바로 해석하기 쉬운 파생 필드를 상단 요약으로 함께 전달한다.

`volume_surge_15m_now`와 `volume_surge_trigger_active`는 의도적으로 분리한다.  
전자는 현재 15분 봉의 즉시 조건이고, 후자는 히스테리시스가 반영된 안정화된 trigger 상태다.

넷째, 장기 메모리는 더 이상 단순 문장 리스트만이 아니다.  
규칙, 지원 근거 수, 적용 조건, 무시 조건을 가진 구조화 레코드로 전달될 수 있다.

다섯째, 멀티 심볼 운영에서는 심볼별로 AI를 여러 번 부르지 않고 한 사이클에 `market_snapshots` 전체를 묶어 포트폴리오 단일 의사결정을 생성한다.  
같은 계좌 상태, 장기 메모리, journal 문맥을 반복 전송하지 않기 때문에 API 비용이 줄고, 심볼 간 우선순위 비교도 한 번에 가능해진다.

여섯째, RAG는 최근 audit를 심볼별로 반복 스캔하지 않는다.  
audit 저장 시 심볼별 feature vector를 함께 저장하고, 조회 시에는 여러 심볼의 유사 케이스를 한 번의 DB 스캔으로 배치 계산한다.

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

## 의사결정 모델

AI 출력은 `TradeIntent`다.

- `decision`
  - `HOLD`, `BUY`, `SELL`, `REDUCE`, `EXIT`
- `orders`
  - 각 주문 후보의 심볼, 액션, 주문 타입, 수량, 가격, `reduce_only`, TP, SL
- `rationale`
  - 왜 그런 결정을 내렸는지에 대한 짧은 근거
- `rationale_items`
  - 기계 검증 가능한 구조화 근거
  - 각 항목은 `summary`, `why_it_matters`, `trade_implication`, `failure_mode`, `evidence[]`를 가진다
  - `evidence[]`는 `signal`, `timeframe`, `operator`, `observed_value`, `threshold`, `units`, `source`, `confidence_contribution`을 포함한다
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
- 트리거 모드에서는 신규 진입보다 리스크 축소 우선
- `risk_trigger`가 있으면 스프레드와 펀딩 같은 거래 품질 악화를 별도 위험 신호로 해석
- `rationale_items.evidence[].source`는 실제 `DecisionRequest` 경로를 가리켜야 하며, 관측값과 어긋나면 보수적으로 `HOLD` fallback
- `operator`도 의미 검증한다. 즉 `gte`, `lt`, `cross_up`, `delta_abs_gte` 같은 비교가 실제 관측값과 threshold에서 성립하지 않으면 `HOLD` fallback
- 신규 진입에서는 `trade_implication`이 실제 행동 방향과 맞지 않거나, 근거 품질 대비 confidence가 과하면 보수적으로 `HOLD` fallback
- 신규 진입에서 `rationale_items`가 있으면 evidence 품질도 본다. 신호 다양성, symbol 커버리지, failure_mode 유무가 약하면 entry size cap을 더 낮춘다.

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
- 실행 동시성/중복 실행 가드

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

execution_guards:
  loop_lock_ttl_seconds: 240
  decision_cooldown_minutes: 10
  entry_mutex_ttl_seconds: 180
  unknown_order_block_minutes: 30
  recovery_sweep_minutes: 5

context_budget:
  journal_fetch_limit: 24
  journal_keep_limit: 6
  memory_keep_limit: 8
  similar_case_keep_limit: 5
  max_rationale_items_per_journal: 2
  max_evidence_per_rationale_item: 2
  max_ignore_when_per_memory: 2
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
- `execution_guards`
  - 루프 전역 락, 심볼별 entry mutex, 반복 판단 쿨다운, unknown 주문 상태 감시/복구 주기를 정책에서 조정한다.
- `context_budget`
  - 최근 journal/long_term_memory/similar case를 전부 보내지 않고, 관련성 높은 상위 컨텍스트만 압축해 전달한다.

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
  "risk_mode": "trigger",
  "risk_profile": {
    "max_open_positions": 1,
    "max_order_notional_pct_balance": 35,
    "allowed_actions": ["HOLD", "BUY", "SELL", "REDUCE", "EXIT"]
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
    "trigger_mode": true,
    "executed_order_count": 1
  }
}
```

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
가능한 경우 audit/ledger와 연결해 실제 결과, 수수료, risk mode, trigger 정보를 함께 요약해 피드백 루프를 강화한다.

최근 구조에서는 journal도 단순 1회성 로그에 머무르지 않고, 같은 사이클에서 생성된 심볼별 `trade_case_id`와 `case_status` 요약을 함께 남긴다.  
즉 포트폴리오 journal만 봐도 "이번 판단이 어떤 케이스를 열었는지, 줄였는지, 닫았는지"를 빠르게 복기할 수 있다.

daily memory batch는 `rationale_json.evidence`의 반복 시그널에서 heuristic `candidate_lessons`를 먼저 만들고, Gemini는 이를 정제하거나 병합하는 역할을 한다.  
Gemini 호출이 실패해도 candidate lesson은 fallback으로 남을 수 있다.

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

즉, 동일 사이클에서도 `BTCUSDT`와 `ETHUSDT`를 분리해 신호/성과를 분석할 수 있다.

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

최근 `journal` 선별은 이제 단순 최신순 점수보다, 이 `trade_cases`의 심볼/trigger/risk mode/regime/결과 패턴을 같이 보고 유사한 사례를 우선 선택한다.  
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
- `evidence_signals_added`
- `evidence_signals_removed`
- `evidence_signal_count_delta`

즉, 예전엔 `BUY`였는데 지금은 `HOLD`인지, 같은 `BUY`여도 크기가 줄었는지, rationale의 중심 키워드가 무엇으로 바뀌었는지 바로 볼 수 있다.

이제 replay는 `rationale_items.evidence`도 비교한다.  
즉 어떤 evidence signal이 추가되거나 사라져서 결정이 바뀌었는지까지 추적할 수 있다.

대시보드는 최근 replay evidence diff, 최근 memory candidate seed, 최근 symbol_journal을 함께 보여준다.

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
또한 memory batch는 `retrieval_evaluation_summary`와 `retrieval_counterfactual_summary`를 함께 읽어,
retrieval이 실제 성과와 반사실 비교 기준에서 얼마나 의미 있었는지, 그리고 그 차이가 path quality 기준으로도 유지되는지 장기 메모리 판단 근거로 남긴다.

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
GEMINI_MEMORY_MODEL="gemini-2.5-pro"
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
- `GEMINI_MEMORY_MODEL`
  - 일일 memory batch에서 사용하는 모델
  - 비우면 `GEMINI_TRIGGER_MODEL -> GEMINI_DEFAULT_MODEL -> GEMINI_MODEL` 순서로 fallback
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
2. `GEMINI_DEFAULT_MODEL`, `GEMINI_TRIGGER_MODEL`, `GEMINI_MEMORY_MODEL` 역할 분리가 여전히 적절한지 본다.
3. SQLite 파일 크기, `audit_logs`, `position_ledger`, `replay_runs` 증가량을 점검한다.
4. 오래된 audit archive 정책이 필요한지 판단한다.
5. 오래된 memory rule 중 현재 시장 레짐에서 더 이상 맞지 않는 항목을 pruning 할지 검토한다.
6. 실계좌 운영이면 월간 최대 drawdown, symbol concentration, execution quality를 따로 리뷰한다.

### 1. 모델 품질 점검

- 대시보드의 `Semantic Fallbacks`를 먼저 본다.
- `24h / 7d / latest 50 calls` 기준으로 fallback 수가 올라가면 최근 모델 변경, 프롬프트 변경, journal 문맥 변화가 있었는지 확인한다.
- 특정 모델의 failure rate가 높으면:
  - `GEMINI_DEFAULT_MODEL`, `GEMINI_TRIGGER_MODEL`, `GEMINI_MEMORY_MODEL` 분리를 다시 점검
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

## 수익률이 안 나올 때 해볼 것

수익률이 안 나올 때는 모델을 바로 바꾸기보다, 먼저 "무엇이 나쁜지"를 분리해서 봐야 한다.
대체로 아래 5가지 유형 중 하나다.

### 1. 진입은 맞는데 실행이 나쁜 경우

증상:

- 방향은 대체로 맞는데 net pnl이 수수료/슬리피지 때문에 깎인다.
- replay 기준 decision은 괜찮아 보이는데 ledger net이 약하다.

해볼 것:

1. `wide_spread`, `elevated_funding`이 잦은 시간대를 줄인다.
2. `max_order_notional_pct_balance`를 낮춰 슬리피지 민감도를 줄인다.
3. 심볼 수를 줄이고 유동성 좋은 심볼만 남긴다.
4. market order만 허용 중이라면, 정책상 limit 운용이 가능한지 별도로 검토한다.

### 2. 진입 자체가 과다한 경우

증상:

- 거래 수는 많은데 기대값이 낮다.
- `BUY/SELL`은 많은데 대부분 작은 손실이 누적된다.

해볼 것:

1. trigger 조건을 더 보수화한다.
2. `mode_profiles.trigger.max_order_notional_pct_balance`와 `max_open_positions`를 더 줄인다.
3. low evidence quality에서 사실상 `HOLD`가 더 자주 나오도록 validator/risk 정책을 강화한다.
4. 최근 journal에서 같은 signal 조합이 반복 손실인지 확인하고 memory anti-pattern 반영 여부를 점검한다.

### 3. 손실은 적은데 수익도 안 나는 경우

증상:

- drawdown은 얕지만 계좌 곡선이 옆으로 간다.
- `REDUCE/EXIT`가 너무 빠르거나, 좋은 추세를 끝까지 못 먹는다.

해볼 것:

1. `EXIT`와 `REDUCE` journal을 따로 모아 "너무 빨리 줄였는지" 본다.
2. 장기 메모리가 지나치게 보수적인 교훈만 축적했는지 확인한다.
3. trigger mode에서 신규 진입 억제가 과한지 점검한다.
4. regime별로 성과를 나눠 보고, 추세장에서만 별도 정책을 둘지 검토한다.

### 4. 특정 심볼만 문제인 경우

증상:

- 전체 수익률이 낮은데, 실제로는 특정 심볼 하나가 대부분 손실을 만든다.

해볼 것:

1. 대시보드 `Symbol Report`로 심볼별 net pnl을 확인한다.
2. 손실 심볼을 잠시 제외하고 나머지 심볼만으로 관찰한다.
3. 그 심볼의 `spread_bps`, `funding_rate`, `open_interest` 변동이 다른 심볼보다 거친지 본다.
4. 심볼별로 다른 threshold가 필요한지 검토한다.

### 5. 시장 레짐이 바뀐 경우

증상:

- 과거에는 맞던 패턴이 최근 몇 주간 계속 안 맞는다.
- memory rule은 그럴듯한데 최신 audit에서는 실패가 반복된다.

해볼 것:

1. replay로 예전 성공 audit와 최근 실패 audit를 나란히 비교한다.
2. `trend_alignment`, `volatility_regime`, `risk_trigger_reasons`가 달라졌는지 확인한다.
3. trigger threshold를 ATR/volatility 기준으로 더 강하게 정규화한다.
4. 오래된 long-term memory rule을 약화하거나 manual pruning 한다.

### 수익률 부진 시 권장 점검 순서

1. 대시보드에서 `ledger net pnl`, `Symbol Report`, `Semantic Fallbacks`를 먼저 본다.
2. 손실 audit 10~20개를 replay로 다시 돌려서 decision 변화와 evidence diff를 본다.
3. journal에서 반복 손실 signal과 evidence quality 패턴을 확인한다.
4. 정책 조정은 한 번에 하나씩만 한다.
5. 변경 후에는 반드시 demo 또는 축소된 size로 재관찰한다.

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
