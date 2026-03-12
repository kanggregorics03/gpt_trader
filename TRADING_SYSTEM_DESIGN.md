Bybit 기반 멀티 LLM provider 연동 자동매매 시스템 종합 기획서
1) 목표

Bybit에서 자동매매를 수행하는 시스템을 구축한다.

LLM provider는 시장/계좌 상태를 바탕으로 “주문 후보(JSON)”를 생성하는 역할만 맡고,

사실 조회(포지션/잔고/주문)·리스크 통제·주문 실행은 코드가 전담한다.

단일 adaptive loop가 현재 상태에 따라 1시간 / 15분 / 5분 cadence를 선택해 실행한다.

매매일지(원본+요약)와 장기 요약(LongTermMemory)을 유지해 맥락/통찰을 축적한다.

현재 구현은 여기서 더 나아가 `메모리 -> 정책 후보 -> shadow/replay 평가 -> offline review 기록`을 수행한다.
즉 시스템 목표는 단순한 AI 자동매매가 아니라, 제약된 정책 공간 안에서 반복 가능한 의사결정을 복기하고 개선하는 트레이딩 운영체제다.

2) 설계 원칙

LLM은 전략가, 시스템은 경찰

LLM provider는 추천 JSON을 생성

시스템은 규정 검증 후 실행/거절

진실의 원천은 Bybit API/DB

계좌/포지션/미체결은 LLM이 “추정”하지 않는다.

생존률 최우선

회로차단기(Kill switch), 멱등성(idempotency), 쿨다운(cooldown) 필수

맥락 유지는 DB 기반 + 압축 기억

원문 일지는 DB에 전부 저장

실시간에는 “최근 요약 + 장기 요약”만 모델에 제공

3) 전체 아키텍처
3.1 모듈 구성

collector

시장 데이터 수집(OHLCV 15m/1h, 필요 시 호가/체결)

지표/특징 계산(ATR, 추세 기울기, 변동성 등)

LLM이 raw 값보다 바로 판단에 쓸 수 있도록 volume ratio, 즉시 volume surge 여부, stabilized trigger state,
open interest / funding / basis 변화율, spread / depth percentile, breakout / invalidation 거리 같은 상단 요약 필드를 함께 계산

이때 `spread_percentile_1h`, `depth_percentile_1h`, `spread_vs_1h_avg`는 in-memory rolling history를 기반으로 계산하므로,
프로세스 시작 직후나 잦은 재시작 직후에는 `null`일 수 있다.

public WebSocket은 ticker/orderbook 위주로 유지하고, closed-candle 기반 OHLCV는 5분 materialization 시점의 incremental REST fetch를 우선

즉 진행 중인 1h/4h 봉을 계속 스트리밍하기보다, 필요한 시점에만 저빈도로 가져오는 쪽을 기본값으로 둔다

public REST fallback TTL도 늘려 ticker/orderbook/funding/open interest 재호출을 줄인다

public WS는 latest-state overwrite를 유지하고, `get_diagnostics()`에서 stream age, symbol age, message count, handler timing, exchange lag를 노출해 지연을 감시한다

account_reader

잔고/포지션/미체결 주문을 Bybit에서 조회

포지션별 청산가/레버리지/증거금/명목가치와 미체결 주문 상세까지 정규화

시장 데이터는 메인넷 public API를 사용하고, 계좌 조회/주문 실행은 live 또는 demo private API를 선택할 수 있게 분리

짧은 TTL 계좌 캐시를 사용하고, 주문 실행 직후에는 invalidate 후 재조회해 stale 상태를 방지

Bybit private WebSocket으로 wallet/position/order/execution을 메모리에 유지하고, REST는 fallback + reconcile 용도로만 사용

WS health state는 `healthy / stale / degraded / reconnecting / down`

`healthy`일 때만 WS를 primary로 사용하고, `stale / degraded / reconnecting / down`에서는 REST fallback

WS 복구는 단순 재연결로 보지 않고, private 이벤트 수신 재개 + REST reconcile 성공 + 안정 구간 통과 후에만 `healthy`

다만 계정이 조용해 private 이벤트가 당장 없더라도, 시작 직후 REST reconcile이 1회 성공하고 안정 구간을 지나면 `healthy`로 승격될 수 있다.
반대로 stale/down 이후 private 이벤트가 다시 들어온 직후에는 forced reconcile 정책에 따라 한 번 더 `degraded`를 거칠 수 있다.
transport liveness는 private 계좌 변동 이벤트뿐 아니라 pybit raw websocket message / pong / open 훅으로도 갱신한다.
즉 계좌 변화가 없는 조용한 구간 자체를 stale로 보지 않고, 최근 reconcile TTL 안에서는 healthy를 유지한다.

심볼 비교는 정책/주문/포지션/체결 전 구간에서 canonical symbol로 정규화한다.
예를 들어 `ETHUSDT`, `ETH/USDT`, `ETH/USDT:USDT`는 내부적으로 같은 심볼로 취급한다.

`AccountReader.get_runtime_diagnostics()`로 account cache freshness와 private WS health / event count / lag를 한 번에 볼 수 있다

state_store (DB)

상태(리스크 상태, 최근 트레이드 요약, 장기 메모리 버전 등) 저장

orchestrator bootstrap

실행 시작점은 facade `main.py`를 유지하되, 실제 bootstrap은 세 층으로 나눈다.

- `bootstrap.py`: bootstrap facade와 helper mixin
- `bootstrap_config.py`: env -> `RuntimeConfig` 해석
- `bootstrap_dependencies.py`: 거래소/피드/엔진/스토어 조립

즉 설정 해석, 의존성 생성, runtime state 초기화를 한 메서드에 섞지 않는다.

orchestrator runtime support

runtime helper도 facade `runtime_support.py`를 유지하되 세 층으로 나눈다.

- `runtime_state.py`: risk/runtime helper, execution runtime 보조 유틸
- `cadence_policy.py`: `1h / 15m / 5m` adaptive cadence 전환 규칙과 maintenance tick
- `runtime_diagnostics.py`: websocket freshness, risk transition alert, runtime diagnostics
- cycle summary webhook은 예외/경고 외에도 사이클 종료 시 `loop/risk/decision/why/orders`를 compact하게 남긴다.

즉 상태 조작, cadence 전이, 진단/알림을 같은 mixin에 섞지 않는다.

snapshot_service

account_reader / collector / state_store를 묶어 공용 스냅샷을 조립

한 사이클에서 계좌 상태, 시장 상태, 리스크 상태, trigger / risk mode를 한 번에 계산

5분 기준 공용 스냅샷을 materialize하고, 상위 타임프레임은 새 봉이 마감될 때만 선택적으로 refresh

계좌 상태는 `SnapshotService -> AccountReader -> private WS cache`를 우선 사용하고, 주문 직후/WS 비정상/주기적 reconcile 시에만 REST 보정

또한 각 materialization 시점의 시장 핵심 필드는 `market_snapshot_history`에 별도 저장한다.
즉 future path replay는 audit 저장 빈도와 분리된 5분 시계열을 우선 사용한다.
현재 저장 필드는 최소한 다음을 포함한다.
- `current_price / mark_price / mid_price`
- `spread_bps / depth_notional_top5`
- `spread_percentile_1h / depth_percentile_1h / spread_vs_1h_avg`
- `distance_to_invalid_level`
- `volume_surge / fast_move / range_break / opportunity / risk trigger`
- `trend_1h`
future path 조회 시에는 history row가 일부만 있어도 그것만 반환하지 않고,
비는 timestamp 구간은 audit payload의 `market_state`를 섞어 보강한다.
즉 `history + audit fallback merge`를 기본으로 두어 partial history 때문에 반사실 평가가 비는 일을 줄인다.

decision_engine (provider adapter)

입력 스냅샷+메모리를 받아 TradeIntent(JSON) 출력

Gemini / OpenAI(GPT) / DeepSeek / Anthropic(Claude)를 adapter로 선택할 수 있다

프롬프트는 `loop_mode`와 `cycle_role`을 함께 받아 루프별 제약을 직접 반영한다.

최근 규칙에서는 일관성 유지를 위해 `1h`도 완전히 자유로운 진입 루프가 아니다.
live position이 있으면 `1h`의 `cycle_role`은
`trade_consistency_management`로 강등되고, 기존 trade case를 유지하는 관리 액션만 허용한다.
즉 새로운 entry thesis를 같은 심볼 위에 덮어쓰지 않고,
기존 `trade_case_id` / `thesis_id` / `management_template_id`를 따라 `HOLD/EXIT/CANCEL/REPLACE_ENTRY`
범위 안에서만 후속 결정을 내리게 한다.
따라서 `5m`는 프롬프트 레벨에서도 신규 진입 금지, `15m`는 관리 중심, `1h`만 신규 방향성 판단이라는 계약을 공유한다.
추가로 `15m`는 단순 약손실만으로 줄이지 않고 invalidation 근접 시점에서만 thesis weaken 청산을 허용하며,
`5m`는 acute loss, stale execution failure, 연속 확인된 liquidity deterioration 같은 emergency path만 deterministic하게 처리한다.

JSON mode가 가능한 provider는 native JSON 응답을 우선 사용하고, 그렇지 않은 경우에도 JSON-only 지시와 Pydantic 검증을 함께 사용한다

DeepSeek reasoning 계열은 응답 시간이 수 분까지 길어질 수 있다.
특히 guarded 상태에서 `AI_GUARDED_ATTENTION_MODEL=deepseek-reasoner`를 쓰면 초기 사이클 지연이 커질 수 있다.

risk_engine

정책(policy.yaml) 기반 검증: 손실한도/노출/레버리지/슬리피지/쿨다운 등

최근 구현에서는 쿨다운도 고정 30분이 아니라 손실 강도, 연속 손실, 반복된 `thesis_failure_reason`을 같이 봐서 동적으로 늘어난다.
또 `drawdown`은 BUY/SELL을 완전히 막지 않되 `confidence_cap`, `entry_size_multiplier`, `failure_pattern_score`로 추가 감쇠한다.
현재 구현에서는 live runtime이 `policy_registry` 같은 offline registry를 직접 읽어 contract를 바꾸지 않는다. exposure 조정도 기본 risk profile을 그대로 사용한다.
반면 `max_leverage`, `drawdown_activate_pct`, `daily_max_loss_pct`는 자동 진화 대상이 아닌 하드 가드레일로 유지한다.

execution_engine

주문 실행/취소/정정 + 멱등성(clientOrderId) + 재시도 안전 처리

journal & audit logger

원본 입력/출력/주문/체결/결과 저장 + 사람용 요약 일지

long_term_memory_updater (Batch)

하루 1회(권장) 장기 요약 업데이트(교훈/안티패턴/국면 맵)

동시에 아래 정책 계층도 관리

- `alpha_1h` 진입 필터 후보 생성/평가
- `position_15m` FSM 관리 정책 후보 생성/평가
  - trade case의 invalidation 거리와 microstructure stress를 읽어 thesis weaken / liquidity exit 파라미터를 함께 조정
- `safety_5m` execution safety FSM 후보 생성/평가
  - emergency exit 중심 shadow divergence만 승격 근거로 사용하고 legacy `risk_reduce` 가점은 사용하지 않음
- `execution_5m` fill/slippage/latency 평가 저장과 오프라인 tuning 후보 분석
- `policy_bundle` 계층은 제거했고, candidate/promotion 기록도 legacy offline review surface로만 유지한다
- legacy management contract 정리
  - `policy_registry` legacy config에서 구 `reduce/profit_protect` 키를 제거한다
  - `candidate_policy_versions`의 legacy 15m/5m 후보는 `retired`로 내린다
  - `long_term_memory`의 obvious `scale out` / `profit protect` lesson은 soft-retire 한다
  - 사실 이력 테이블은 보존하고, 학습 입력은 `2026-03-10` cutover 이후 window로 제한한다

monitoring & alert

알림(진입/청산/손절/정지/오류), 헬스체크(오류율/지연/거절률)

4) 실행 모델과 트리거
4.1 단일 adaptive loop

현재 구현은 `1시간 루프`, `15분 루프`, `5분 루프`를 각각 스케줄하지 않는다.

대신 하나의 메인 루프가 매 사이클마다 다음 cadence를 선택한다.

우선순위:

`5m > 15m > 1h`

기본 규칙:

1시간 cadence

포지션 없음, 미체결 주문 없음, unknown order 없음, 최근 실행/청산 직후가 아님

역할: 전략 판단과 신규 진입 방향성 결정

추가 원칙:

- `AlphaPolicyEngine`은 현재 live pre-filter가 아니라 advisory signal로만 남는다
- AI는 전체 심볼 스냅샷을 보고 최종 방향성 판단을 한다
- blocked reason은 audit/execution summary에 advisory metadata로만 남긴다
- AI structured fallback이 발생해도 다음 loop contract를 `15m` 관리 모드로 바꾸지 않고, 같은 `1h` 전략 계약으로 최대 두 번 더 빠르게 재시도한다

15분 cadence

포지션 보유 중

또는 미체결 주문 존재

또는 unknown order 상태 존재

또는 최근 진입/청산 직후라 더 짧은 관리 주기가 필요한 경우

역할: 포지션 관리, 주문 정리, 감축/청산 중심

추가 원칙:

- 기본 경로는 AI가 아니라 `PositionPolicyEngine` FSM
- 상태 예시: `healthy`, `stalled`, `weakened`, `de-risk`, `exit`
- shadow 후보 FSM과 live decision의 차이를 저장해 승격 평가에 사용

5분 cadence

opportunity_trigger 활성화 + active management 대상 존재

active management 대상:

- 포지션 보유 중
- 미체결 주문 존재
- unknown order 상태 존재
- 최근 진입 직후
- 최근 청산 직후

역할: execution safety, trigger deterioration 대응, emergency de-risking

기본 경로는 AI가 아니라 `SafetyPolicyEngine` FSM이다.
5분 루프는 신규 진입을 만들지 않고 HOLD/EXIT/CANCEL만 다룬다.
즉 일반적인 profit protect나 thesis 관리 감축보다 응급 청산과 stale order 정리가 우선이다.

즉, strong trigger가 떠도 flat 상태에서 관리 대상이 없으면 5분 cadence가 바로 선점하지 않는다. 5분은 trigger와 관리 필요가 동시에 있을 때만 승격되고, trigger가 사라지면 포지션/주문 상태에 따라 15분 또는 1시간으로 강등된다.

5분 루프는 방향 예측보다 실행/안전 계층에 가깝다.
`SafetyPolicyEngine`이 먼저 HOLD/EXIT/CANCEL을 결정하고, 실제 주문이 생기면 `ExecutionPolicyEngine`이 intent를 market/limit execution profile로 결정론적으로 번역한다.

4.1.1 스냅샷 materialization

메인 루프의 decision cadence와 별개로, SnapshotService는 5분 기준 공용 스냅샷을 만든다.

업데이트 원칙:

현재가 / 오더북 / 파생상품 / 계좌 상태 / 리스크 상태

5분 materialization 때마다 갱신

5m timeframe snapshot

매 materialization 때 갱신

15m / 1h / 4h timeframe snapshot

해당 봉이 실제로 마감된 시점에만 refresh

즉, 상위 타임프레임 값은 직전 materialized 값을 재사용하고, 새 봉이 닫혔을 때만 계산을 다시 수행한다.

계좌 상태 업데이트 원칙:

WS `healthy`

WS cache를 primary source로 사용

WS `stale / degraded / reconnecting / down`

REST fallback + reconcile 사용

WS recovery

`connected + subscribed + data flowing + state consistent`를 만족해야 다시 `healthy`

4.2 히스테리시스와 강등 규칙

승격은 빠르게, 강등은 천천히 한다.

`1h -> 15m`

포지션/주문/복구 필요 상태가 생기면 즉시 승격

`1h or 15m -> 5m`

같은 심볼에서 `risk_trigger` 같은 safety/emergency 신호가 있고, 동시에 포지션 / 미체결 주문 / unknown order 같은 active management 대상이 있을 때만 승격

`5m -> 15m`

safety/emergency trigger 해제 + clear streak 충족 + 포지션/주문/복구 대상 존재

단, 포지션 / 미체결 주문 / unknown order가 모두 사라진 flat 상태면 clear streak 없이 즉시 15분으로 강등

`5m -> 1h`

safety/emergency trigger 해제 + clear streak 충족 + 포지션/주문/복구 대상 없음

`15m -> 1h`

recent execution / recent close TTL이 남아 있으면 유지

TTL까지 끝난 완전 flat 상태에서는 즉시 1시간으로 복귀

이 히스테리시스는 cadence가 경계값 근처에서 `5m <-> 15m <-> 1h`로 흔들리는 현상을 줄이기 위한 것이다.

4.3 트리거 분리 원칙

트리거는 두 부류로 분리한다.

opportunity_trigger: 급변/돌파/거래량 급증처럼 관찰 빈도를 높일 이유가 있는 이벤트

risk_trigger: 스프레드 확대, 펀딩비 왜곡처럼 신규 진입 판단을 더 보수적으로 만들어야 하는 거래 품질 악화

fast_price_move_15m, volume_surge_15m, range_break_1h에는 ON/OFF 기준을 분리한 히스테리시스를 적용해 경계 구간 점멸을 줄임

5) 입력/출력 표준화(JSON)
5.1 LLM 입력(DecisionRequest)

policy_summary: 현재 정책 핵심(레버리지/한도/쿨다운 등)
  - Implementation note: `policy_summary` is now a compact digest of enforceable limits, mode profiles, re-entry guards, and stale-order controls. The prompt no longer receives a raw `policy.yaml` dump.
  - Contract note: digest는 `RiskEngine.PROMPT_POLICY_CONTRACT`에 등록된 critical path를 반드시 포함해야 하며, 누락 시 런타임 시작 자체를 실패시킨다.

account_state: 계좌 스냅샷 시각, 잔고, 총 미실현손익, 마진비율, 포지션 위험정보, 미체결 주문 상세(사실)

market_snapshot / market_snapshots: 15m/1h 캔들(마감된 캔들만), 핵심 지표/레벨 요약

현재 프로젝트는 단일 심볼 운영을 전제로 하며, `trading.symbols`에 여러 개가 있더라도 런타임에서는 첫 번째 심볼만 사용한다

market_snapshot 안에는 `fast_move_signal_active`, `volume_surge_signal_active`, `range_break_signal_active`, `risk_trigger_reasons`를 포함

단, `1h` strategy decision input에서는 `fast_move_signal_active`, `volume_surge_signal_active`, `range_break_signal_active`를 직접 쓰지 않는다.
이 값들은 short-horizon follow-through stabilization 신호로 보고, `15m/5m`와 replay/memory signature 쪽에만 남긴다.

또한 market snapshot 상단에는 `volume_ratio_15m`, `volume_surge_15m_now`,
`fast_move_signal_active`, `volume_surge_signal_active`, `range_break_signal_active`,
`oi_change_15m`, `oi_change_1h`, `funding_rate_change_1h`, `basis_change_1h`,
`spread_vs_1h_avg`, `spread_percentile_1h`, `depth_percentile_1h`,
`distance_to_15m_breakout_level`, `distance_to_1h_breakout_level`, `distance_to_invalid_level`
같은 파생 요약 필드를 추가해 LLM이 raw 값보다 change / percentile / distance / attention-state를 우선 해석하게 한다

최근 프롬프트는 입력 섹션 이름을 `market_context`가 아니라 `market_snapshots`로 통일한다.
구조화 evidence의 `source`는 `market_snapshots.*`, `risk_state.*`, `account_state.*` 같은 실제 `DecisionRequest` 경로를 가리켜야 한다.

risk_state: 일일 손익률, 연속 손실, 최근 손실 강도, 동적 쿨다운 길이, 쿨다운 잔여 시간, 반복 실패 패턴 점수/대표 사유, 드로우다운 상태, 현재 심볼의 recent close/loss 재진입 가드 상태

recent_journal_context: 같은 최근 일지 정보를 구조화 객체로 전달
  - Retrieval note: candidate rows are no longer a pure recent-only slice. Runtime mixes a recent pool with semantic anchors such as `good_hold`, `missed_opportunity_hold`, and risk/loss outcomes before reranking.
  - Learning note: reranking의 마지막 단계에서는 daily batch가 남긴 `journal_reranker_profile` artifact가 버킷별 가산/감산을 제공해 hand-tuned heuristic을 보정한다.

문자열 파싱 대신 rationale_items, evidence_quality, entry_size_cap, trigger 정보를 직접 참조할 수 있게 함

운영 시에는 context_budget으로 recent_journal_context를 압축해 토큰을 제어하되,
단순 점수 상위 N개가 아니라 `good_hold / missed_opportunity_hold / entry_action / risk_outcome` 버킷을 섞어
action bias를 줄인다

semantic validation 실패, JSON validation fallback, loop error로 만들어진 fallback HOLD는
retrieval context에서 기본 제외해 "시장 판단 HOLD"와 "시스템 오류 HOLD"가 섞이지 않게 한다
같은 기준으로 similar_past_cases의 RAG fallback audit와 그 audit에 연결된 trade case도 prompt 후보에서 제외한다
설명 품질 실패는 raw intent를 보존한 채 `explanation_valid=false`, `learning_eligible=false`로 분리 기록하고,
행동 자체는 `decision_valid`와 `execution_admissible`이 실제로 막을 때만 차단한다

long_term_memory: 장기 교훈/안티패턴/국면-전략 맵(압축본)
  - Lifecycle note: runtime retrieval and daily reconciliation now read the same lifecycle-managed memory pool, so older active rules are not silently dropped by a separate recent cap.

long_term_memory도 현재 risk_mode/시장 레짐/심볼 관련성이 높은 항목만 상위 N개를 전달하되,
`risk_control`, `opportunity`, `execution_quality`, `good_hold/missed_hold` 축이 한쪽으로만 쏠리지 않도록
카테고리 균형을 강제한다

최근에는 long_term_memory를 append-only 로그로 두지 않고,
- confidence_score로 support / cluster quality / retrieval 효과를 함께 반영하고
- decay_score + lifecycle_status로 오래 검증되지 않은 rule을 감쇠/은퇴시키고
- canonical_key + rule similarity로 비슷한 lesson을 병합하는 방향으로 운영한다

또한 memory 적용도 단순 `apply_when.regime/volatility/trend` 매칭만 보지 않는다.
최근 구조에서는 `trigger_context`, `execution_context`, `trend_context`, `hold_quality`도 구조화 필드로 저장하고,
실시간 loop가 현재 시장의 같은 signature와 직접 비교해 맞는 memory만 강하게 우선한다.
특히 opportunity memory는 trigger/execution/trend 문맥이 어긋나면 강하게 감점한다.

constraints: “JSON only”, 출력 최대 길이, 허용 행동 제한

프롬프트는 compact JSON으로 직렬화하고, 완전히 동일한 prompt + model은 decision_cache에서 재사용

캐시 키 안정성을 위해 account_state의 고변동 필드(예: snapshot_timestamp)는 canonicalization 단계에서 제외

시장 데이터 수집은 OHLCV 롤링 캐시와 ticker/order book/funding/open interest TTL 캐시를 사용해 네트워크 부하를 줄임

RAG는 audit 저장 시 심볼별 feature vector를 함께 저장하고, 조회 시에는 심볼별 반복 스캔 대신 배치 유사도 계산으로 DB 부하를 줄임
similar_past_cases:
  - Guarded-attention state still gets the deepest analog retrieval.
  - Normal mode now keeps a small background-support case set so `good HOLD` vs `missed HOLD` judgments do not lose all analog history outside trigger loops.
  - Similar-case scoring also reads a learned `similar_case_reranker_profile` so replay/counterfactual outcome가 좋았던 retrieval signature를 약하게 우대한다.

RAG vector는 시장 13차원만 쓰지 않고, 리스크/계좌/포지션 컨텍스트 13차원을 합쳐 26차원으로 구성한다

유사도 계산 전에는 차원별 normalization, outlier clipping, weighting을 적용해 큰 스케일 축의 과도한 지배를 줄인다

5.2 LLM 출력(TradeIntent.json)

decision: HOLD/BUY/SELL/REDUCE/EXIT/CANCEL/REPLACE_ENTRY

orders[]: 주문 후보(가격/수량/TP/SL/reduce_only 포함)

`CANCEL` action은 `target_order_id` 또는 `target_client_order_id`를 함께 담아 stale working order를 정리한다.
AI가 `order_id`, `clientOrderId`, `orderLinkId` 같은 alias를 보내도 정규화 단계에서 `target_*` 필드로 흡수한다.
`CANCEL`은 실행 경로에서 `order_type`을 소비하지 않으므로, `order_type` 누락만으로 semantic fallback을 만들지 않는다.

`REPLACE_ENTRY`는 명시적 1h working-order 관리에서만 쓰는 예외 액션이다.
자동 15m/5m 관리 루프는 stale working order를 재호가하지 않고 cancel-only로 처리한다.
lineage 필드는 기존 취소/대체 흐름 이력을 보존하기 위해 남겨둔다.

추가로 `5m`는 stale order 정리나 단순 손실 감시 때문에 바로 승격되지 않는다.
현재 cadence 승격은 `unknown order`, 확인된 `liquidity_risk`, `stale_order_risk` 같은 비상 상태에만 반응하고,
단순 `acute_loss`나 stale working order 정리는 기본적으로 `15m` 관리 루프가 맡는다.

rationale[]: 근거(짧게)

rationale_items[]: 구조화 근거

각 rationale_item은 summary, why_it_matters, trade_implication, failure_mode, evidence[]를 포함

각 evidence는 signal, timeframe, operator, observed_value, threshold, units, source, confidence_contribution을 포함

`confidence_contribution`은 양수 가점뿐 아니라 음수 감점도 허용한다. 현재 범위는 `-1.0 .. 1.0`이다.

timeframe snapshot에는 previous_values를 둘 수 있고, cross_up/cross_down 검증 시 source별 직전값으로 사용한다

source는 DecisionRequest 안의 실제 경로여야 하고, 관측값과 맞지 않으면 explanation invalid 또는 action/schema invalid로 기록된다

operator도 의미 검증한다. gte/lt/cross_up/delta_abs_gte 같은 비교가 실제 관측값과 threshold에서 성립하지 않으면 action/schema 단계에서 차단된다

신규 진입에서는 trade_implication이 실제 행동 방향과 맞지 않거나, 근거 품질에 비해 confidence가 과하면 explanation invalid 또는 action/schema invalid로 분리 기록된다

프롬프트는 adverse condition도 두 층으로 나눈다.
liquidation risk, hard cooldown pressure, drawdown pressure 같은 account-risk 압박은 신규 노출을 강하게 억제할 수 있지만,
wide spread, weak depth, unfavorable funding 같은 execution-quality 악화는 우선 size/confidence penalty로만 다루고 단독 HOLD veto로 쓰지 않는다.

신규 진입에서는 구조화 근거의 품질도 리스크 판단에 반영한다

예: signal 다양성 부족, 주문 심볼 근거 누락, failure_mode 부재 시 max_order_notional cap을 추가로 축소

risk_notes[]: 위험 요인

confidence: 참고값(실행은 규정 통과 여부로 결정)

중요: 시스템은 “주문 실행 권한”을 Gemini에 주지 않는다. JSON은 추천일 뿐.

AI 출력 orders[]는 entry 의미와 관리 lineage만 담고, `execution_profile`, `urgency_score`, `time_in_force`, `post_only` 같은 실행 메타데이터는 runtime이 사후에 채운다.

또 주문 타입은 더 이상 전역 market/limit 고정이 아니다.
`ExecutionPolicyEngine`은 live에서는 의미 보존 계약만 집행한다.
`EXIT`와 `REDUCE`는 항상 `market_urgent`를 유지하고, 신규 `BUY/SELL` entry는 `MARKET -> market_urgent`, `LIMIT + immediate -> limit_aggressive`, `LIMIT + retest/value -> limit_passive`로 결정론적으로 번역한다.
또 passive limit 가격도 entry intent를 따라 다르게 계산한다. `retest`는 breakout/reclaim 구조 레벨을 우선 사용하고, `value`는 현재가 대비 passive offset을 사용한다.
프롬프트도 이 intent taxonomy를 따르며, `retest`는 구조 레벨 근거가 있을 때만, `value`는 trend/invalidation이 허용되는 passive retrace일 때만 쓰도록 제한한다.
리스크 쪽 size contract는 별도로 고정한다. `max_leverage`는 총 계정 익스포저 상한이고, `max_order_notional_pct_balance`는 현재 모드에서 허용하는 주문 후보 총 notional 상한이다. `drawdown` 등에서는 `entry_size_multiplier`, `confidence_cap`, `failure_pattern_size_multiplier`, `risk_trigger_size_multiplier`가 이 상한을 추가 감쇠할 수 있지만, offline 학습 결과가 live에서 자동으로 이 하드 가드레일을 덮어쓰지는 않는다.
추가로 size cap 계산은 `evidence_quality` 외에 `setup_quality`를 함께 반영한다. `retest`가 구조에서 너무 멀거나 `value`의 trend/invalidation/execution quality가 나쁘면 cap을 더 줄이고, 이 이슈는 execution summary에 남긴다.
학습된 execution profile은 live 주문 의미를 바꾸는 override로 쓰지 않고, 사후 평가와 파라미터 튜닝 후보 탐색용으로만 남긴다.

6) 맥락(기억) 설계: 단기/중기/장기 3층
6.1 단기(Recent)

최근 10~20 트레이드 요약: 이유/결과/실수/경고

6.2 중기(Weekly/Monthly)

최근 1~4주 성과 요약: 승률/손익비/최대낙폭/시간대별 성과

6.3 장기(LongTermMemory)

“교훈/금지 패턴”을 조건부(국면/심볼/방향) 규칙으로 저장

각 규칙은 support.n, window_days, metrics(PF, avg_R 등)를 포함

모순 처리:

모순은 삭제하지 않고 레짐(국면)으로 분기하여 공존

레짐 불확실하면 “불확실 모드”로 리스크 축소

7) 장기 요약 및 정책 진화 배치 명세
7.1 주기

매일 1회(권장): 예) KST 04:00
(주 1회도 가능하지만 통찰 업데이트가 느림)

권장 운영은 `incremental replay -> memory batch` 순서다.
즉 배치 직전에 최근 24시간 대표 audit를 자동 replay한 뒤, 그 결과를 memory/policy 평가에 반영한다.
자동 replay 수는 고정 30건이 아니라 candidate volume에 따라 가변이고, 기본적으로 `30 -> 60` 범위 안에서 `coverage_target`을 맞추도록 늘어난다.
단, 자동 replay와 memory batch는 모든 audit를 그대로 학습 입력으로 쓰지 않는다.
`loop error`, `execution_summary.error`, `ai_usage.validation_error`,
`ai_usage.semantic_validation_error` 같은 시스템/스키마 오류 audit는 감사 로그로는 유지하되 자동 학습 입력에서는 제외한다.
runtime prompt retrieval도 같은 부류의 audit와 연결 trade case를 recent_journal_context / similar_past_cases에서 제외한다.
또한 최근 구조에서는 포지션이 없고 `HOLD`였던 audit를 replay할 때,
이후 `30m / 2h / 6h` 가격 경로를 읽어 baseline probe entry 대비 `hold_opportunity_counterfactual`을 함께 계산한다.
즉 `HOLD`가 안전했는지뿐 아니라 "좋은 기회를 놓친 HOLD였는가"도 별도로 점검한다.
이 baseline은 단순 "소액 매수 가정"이 아니라,
- trigger / breakout / trend / RSI / 단기 변화율로 방향을 추정
- available balance 기반 소액 sizing
- bid/ask + spread/slippage를 반영한 assumed fill price
- ATR 또는 invalidation 거리 기반 stop/take-profit
- future path에서 stop/take-profit hit 순서를 순차 평가

후보 우선순위는 단순 recency가 아니라 아래를 함께 반영한다.

- 큰 손실로 끝난 audit
- rejected entry / material trade audit
- retrieval 포함 여부와 최근 replay에서 관측된 retrieval 영향도
- `hold_opportunity_counterfactual` 또는 trigger-rich snapshot이 있는 `HOLD`

또한 `HOLD`는 별도 quota를 둬 trade/rejected entry가 많은 날에도 일부가 자동 replay에 계속 포함되게 한다.
- 수수료 포함 net realized move 계산
을 함께 가정한다.
미래 가격 경로는 `audit_logs.market_state`에만 의존하지 않고,
별도 `market_snapshot_history` 시계열 테이블을 우선 사용하며, 구간이 부분적으로 비면 audit payload를 fallback으로 섞어 보강한다.
또한 probe baseline 하나만 두지 않고 더 보수적인 `simple_rule_entry`도 같이 비교한다.
따라서 `missed_opportunity_hold`와 `probe_only_opportunity_hold`를 분리해,
공격적 baseline 착시를 장기 교훈으로 과잉 일반화하지 않게 한다.

7.2 방식(오염 방지 필수)

통계(승률/손익비/낙폭 등)는 코드가 계산

LLM provider는 통계 해석과 교훈 구조화만 수행

“표본 수 부족”은 hypothesis, 충분하면 rule로 승격

각 교훈은 반드시 근거(트레이드 ID/기간/표본수)를 가진다.

추가로 배치는 아래 진화 파이프라인을 수행한다.

1. trade case와 symbol journal에서 candidate lesson과 feature gap을 추출
2. 이를 `candidate_policy_versions`로 저장
3. `shadow_decisions`와 `replay_runs`는 legacy/offline 비교 표면으로만 유지한다
4. 후보 평가 결과는 live 승격이 아니라 offline registry/history 용도로만 저장한다
5. 같은 배치에서 retrieval reranker artifact와 `shadow_decision_dataset.jsonl`을 생성해 retrieval/decision 보조 모델 학습 기반을 축적
6. replay batch heartbeat, memory updater heartbeat, replay 처리율로 `learning_liveness`를 계산하고 stale 시 자동 반영을 중지
7. good hold와 missed hold는 같은 `HOLD`라도 분리 집계해 undertrading / overtrading 균형 점수의 보조 입력으로 사용한다
8. 운영 스케줄에서 replay batch와 memory updater는 백그라운드 subprocess로 비동기 실행하고 timeout을 둬서, 장기 학습이 메인 trading loop를 멈추지 못하게 한다

8) 리스크 관리(필수 안전장치)
8.1 회로차단기(Kill switch)

하루 손실 -X%면 자동 정지

연속 손실 Y회면 신규 진입 금지 + 쿨다운

슬리피지/거절률/오류율 임계치 초과 시 정지

자동 진화가 허용되지 않는 핵심 가드레일은 다음과 같다.

- `max_leverage`
- `drawdown_activate_pct`
- `daily_max_loss_pct`
- `cooldown` 상한

즉 시스템은 실효 노출과 실행 정책은 바꾸되, 생존 한도는 스스로 올리지 않는다.

8.2 멱등성(Idempotency)

모든 주문에 고유 clientOrderId 생성

네트워크 타임아웃 시 재주문 금지
→ 반드시 clientOrderId로 거래소 조회 후 상태 이어받기

8.3 쿨다운/과매매 방지

손절 직후 N분 신규 진입 금지

시간당 최대 신규 진입 횟수 제한

트리거 모드에서는 제한을 더 강화

8.4 캔들/데이터 규칙

전략 판단에는 마감된 캔들만 사용

서버 시간 동기화(거래소 시간 기준)로 쿨다운/중복 방지 정확도 확보

8.5 실행 동시성/중복 실행 가드

전역 실행 락(loop_lock)

루프 시작 시 DB 락(`runtime_locks`) 획득에 실패하면 해당 루프를 스킵

다중 인스턴스/중복 실행 즉시 차단

Decision Cooldown + Fingerprint

최근 N분 내 같은 `symbol + action + size band` 결정이면 재실행 금지

같은 판단 반복 주문 방지

Pending Exposure Reserve

미체결 진입 주문을 가상 포지션처럼 계산해 `max_open_positions`와 노출 한도에 포함

상태 반영 지연 구간에서도 보수적으로 제한

Order Lifecycle Runtime State

`submitted/partial_filled/completed/cancelled/failed/unknown` 전이를 강제 검증

유효하지 않은 전이는 `unknown`으로 강등하여 신규 진입 판단에 보수적으로 반영

Pre-trade Reconciliation

주문 직전 `positions/open_orders`를 강제 재조회하고 intent를 재검증

stale snapshot 기반 실행 방지

Per-symbol Entry Mutex

심볼별 `entry in-flight` 락(`entry_mutex`)이 살아 있으면 신규 진입 금지

동일 심볼 중복 진입 방지

Recovery Sweep Job

주기적으로 `unknown/timeout` 주문을 재탐색해 상태 정리

추적 누락 장기화 방지

9) 로그/매매일지/리플레이(재현)
9.1 원본 감사 로그(audit)

입력 스냅샷(시장/계좌)

정책 버전

LLM 요청/응답 원문

스키마 검증/리스크 판정 결과(통과/거절 + 사유)

주문 요청/응답, 체결, 슬리피지, 수수료

최종 포지션/잔고 변화

9.2 사람용 요약 일지(journal)

“왜 진입/청산/HOLD 했는지”

정책 위반으로 거절된 경우 사유

오늘의 교훈 후보(배치 업데이트에 반영)

텍스트 rationale은 사람용으로 유지하되, 구조화 근거는 journal.rationale_json에도 저장

audit에도 같은 구조화 근거를 원본 그대로 남겨 당시 판단 근거를 재현 가능하게 유지

또한 symbol_journal을 별도로 저장해, 같은 의사결정 사이클에서도 심볼별 trigger/evidence/pnl을 분리 추적한다

최근 구조에서는 journal에도 `trade_case_ids`, `case_statuses`, `case_summary`를 함께 저장해
"이번 포트폴리오 판단이 어떤 심볼 케이스를 열고/줄이고/닫았는지"를 요약 단위에서도 바로 볼 수 있게 한다

symbol_journal은 더 깊게 들어가 `trade_case_id`, `case_status`, `case_opened_at`, `case_closed_at`, `holding_minutes`를 남긴다

즉 journal은 사람용 사례 요약, symbol_journal은 심볼별 사례 로그 역할을 맡는다

활성 케이스는 `active_trade_cases` 상태로 유지하고, 다음 cycle에서 같은 symbol이 이어질 때 동일 case id를 재사용한다

별도 `trade_cases` 이력 테이블도 유지한다

이 테이블은 심볼별 사례 단위로 opened/closed lifecycle, holding time, realized pnl, fee, observed MFE/MAE,
slippage, thesis_failure_reason, feature_vector를 함께 저장한다

즉 active_trade_cases는 현재 열려 있는 케이스 상태 저장소이고, trade_cases는 학습/복기용 영속 사례 저장소다

추가로 trade_case_snapshots 테이블을 두어 active/just-closed case의 cycle별 current_price, spread, volume_ratio,
unrealized_pnl_pct 경로를 저장한다

여기 저장되는 `unrealized_pnl_pct`는 포지션/증거금 기준 ROE 경로다.
최근 수정에서는 deterministic 15m/5m 손절 FSM이 별도 runtime 필드인 `account_unrealized_pnl_pct`
(`미실현손익 / 총자본 * 100`)를 우선 사용하도록 분리했다.
또한 15m thesis weaken 청산은 `distance_to_invalid_level`이 가까워졌을 때만 허용하고,
15m/5m liquidity exit는 market snapshot history에서 연속 확인이 있어야만 발동하게 해 단발성 microstructure noise 청산을 줄인다.

observed MFE/MAE는 단일 cycle 값이 아니라 이 snapshot 경로를 다시 읽어 재계산한다
가능하면 entry/exit audit의 fill timestamp를 복원해 실제 체결 구간 안쪽 경로만 우선 사용한다

또한 scheduler poll 구간에서도 active case를 경량 샘플링해 mark_price / mid_price / unrealized_pnl_pct 경로를 더 촘촘히 남긴다

trade_case_vector_index를 별도로 유지해 feature_vector를 retrieval 전용 인덱스로도 관리한다
최근 버전에서는 coarse band-hash lookup으로 후보를 먼저 좁히고, 그 다음 cosine similarity와 규칙 점수로 rerank한다
또한 retrieval_evaluations 테이블에 실제 prompt에 포함된 retrieval 결과를 남겨,
trade_case_id / similarity / final_score / retrieval_source / decision_symbol_aligned를 나중에 성과와 연결해서 볼 수 있게 한다
trade case가 닫히면 retrieval_evaluations에 outcome_realized_pnl / holding_minutes / observed MFE/MAE / slippage를 backfill한다
또한 replay_audit는 같은 request를 similar_past_cases 포함/제거 두 버전으로 돌려 replay_runs.payload.retrieval_counterfactual에 반사실 비교를 남긴다
memory batch는 replay_runs와 retrieval_evaluations를 audit_id로 연결해,
with retrieval / without retrieval 중 어느 쪽 decision이 실제 종료 손익뿐 아니라 observed MFE/MAE, slippage, holding_minutes를 반영한 outcome utility 기준에 더 잘 맞았는지도 집계한다

향후 짧은 개선 포인트:
- trade_case path는 snapshot 기반이므로, active case 중 WS execution/ticker 이벤트를 바로 적재하면 observed MFE/MAE 정밀도를 더 높일 수 있다
- retrieval candidate search는 SQLite coarse band index 기반이므로, 데이터가 커지면 ANN 전용 인덱스로 분리하는 것이 적절하다
- cluster / retrieval 평가는 현재도 자동 집계되지만, 미래 holdout 성과와 portfolio-level utility까지 연결하면 lesson 품질 검증이 더 강해진다

daily memory batch는 rationale_json의 evidence signal 반복 패턴과 당시 market regime를 함께 읽어 장기 규칙을 만든다

또한 evidence signal 반복에서 heuristic candidate lesson을 먼저 만들고, LLM은 이를 정제/병합하는 역할로 사용한다

즉 오류 audit는 운영 품질 추적에는 남기되, 시스템 버그성 audit를 trading lesson seed로 직접 승격하지 않는 것이 현재 원칙이다.
여기에 replay의 `hold_opportunity_counterfactual`도 함께 읽어,
`missed_breakout_count`, `missed_move_median_pct`, `hold_opportunity_cost`를 undertrading 신호로 집계한다.
이 값이 반복되면 risk_control뿐 아니라 anti-undertrading opportunity lesson도 candidate seed로 만든다.
반대로 closed entry case 쪽에서는 `false_entry_cost`를 함께 계산해 overtrading 압력도 본다.
즉 배치는 `undertrading_pressure`, `overtrading_pressure`, `balance_score`를 같이 유지해
한쪽 방향의 편향만 강화하지 않도록 한다.
여기서 promotion과 pressure 비교에 쓰는 `false_entry_cost`는 퍼센트 기준 정규화 비용이고,
실현 손익 금액은 `false_entry_cost_amount`로 별도 유지한다.
즉 undertrading / overtrading 비교는 항상 같은 단위끼리만 수행한다.
추가로 최근 배치는 `turnover_rate`, `capital_idle_ratio`, `avg_holding_minutes`, `avg_entry_slippage_bps`,
`drawdown_path_penalty`, `regime_capacity_penalty`, `portfolio_utility_score`도 같이 계산한다.
즉 opportunity cost만 줄였다고 live 자동 반영 대상으로 보지 않고, 실제 포트폴리오 수준 utility가 나아졌는지도 함께 본다.
또 `HOLD`도 하나의 단일 결과로 보지 않고
`good_conservative_hold`, `risk_filtered_hold`, `execution_filtered_hold`,
`structure_filtered_hold`, `probe_only_opportunity_hold`, `missed_opportunity_hold`, `unclear_hold`처럼 품질을 분리해 candidate lesson에 반영한다.
anti-undertrading lesson도 넓게 "HOLD를 줄여라"가 아니라,
dominant trigger context, execution context, trend context가 반복된 구간에서만 좁게 seed를 만든다.
그리고 이 `trigger_context`, `execution_context`, `trend_context`, `hold_quality`는 문장 설명에만 남지 않고
구조화 memory schema에도 같이 저장해, 이후 merge/revive/offline review 과정에서도 유지한다.
정책 평가는 같은 opportunity cost를 offline candidate score의 보조 메트릭으로 반영해,
지나친 `HOLD` 편향이 장기적으로 좋은 review score를 받지 못하게 한다.
최근 offline review에서는 `alpha_1h` 후보와 regime context를 볼 때
`hold_opportunity_cost`, `false_entry_cost`, `pressure_ratio`, `balance_score`뿐 아니라
`portfolio_utility_score`, `confidence_alignment_score`, `temporal_holdout_summary`도 같이 확인한다.
즉 비용 한쪽이 너무 크거나 두 압력의 비율이 지나치게 깨져 있거나,
walk-forward 성격의 holdout 구간이 무너지거나 confidence calibration이 나쁘면 score가 좋아도 live 자동 반영 대상으로 보지 않는다.
confidence calibration은 최근 journal의 `confidence`와 사후 outcome을 비교해
`calibration_gap`, `brier_score`, `high_conf_error_rate`, `alignment_score`를 계산하는 방식으로 반영한다.
또한 lesson synthesis 단계에서 LLM은 heuristic candidate seed를 넓게 새로 발명하지 않고,
seed와 매칭되는 lesson만 좁고 읽기 좋게 정제한다.
매칭된 generated lesson도 `apply_when`, `trigger_context`, `execution_context`, `trend_context`, `hold_quality`는 seed의 좁은 조건을 우선 보존한다.

구조화 evidence는 현재 `numeric/categorical/boolean` 스키마를 완전히 분리하지 않고 단일 구조를 유지한다.
대신 operator 규칙으로 타입을 제어한다.
- `gte/lte/gt/lt/cross_up/cross_down/delta_abs_gte` 는 숫자 evidence 전용
- `eq/note` 는 숫자/문자열/불리언 evidence 허용
즉 `rsi_14`, `spread_bps`, `volume_ratio_15m` 같은 값은 숫자 근거로,
`trend="FLAT"`, `momentum_regime="accelerating_down"`, `cooldown_active=true` 같은 값은 `eq` 또는 `note` 근거로 다룬다.

최근 구조에서는 symbol_journal도 함께 집계해 심볼 단위 evidence signal 반복 패턴을 별도로 반영한다

symbol 기반 candidate lesson의 evidence_journal_ids는 audit_log_id가 아니라 symbol_journal.id를 근거로 기록한다

정책 진화 관점에서 추가된 영속 계층:

- `execution_policy_evaluations`
  - 일반 진입/청산뿐 아니라 `CANCEL`, timeout, stale order cleanup 문맥도 함께 저장
- `candidate_policy_versions`
- `policy_registry`
  - offline candidate registry / review log
- `policy_evaluations`
- `policy_promotions`
  - offline review history
- `shadow_decisions`

이 계층은 각각 다음 용도로 쓰인다.

- alpha/position/exposure 후보 저장
- 후보 정책의 shadow/replay 성과 저장
- 후보 정책 이력 관리

최근 replay는 단순 decision diff가 아니라 `simulated_fill_ratio`, `simulated_slippage_bps`,
`simulated_latency_seconds` 같은 execution-aware metric도 함께 남긴다.

symbol_journal 조회 효율을 위해 symbol+id, timestamp+id, audit_log_id 인덱스를 유지한다

recent_journal_context 선별도 단순 recency score가 아니라 trade_cases의 symbol overlap, trigger overlap,
risk_mode 일치, regime tag, closed win/loss 결과를 함께 보는 similarity score로 확장한다

다만 최근성/행동 사례 쏠림이 그대로 프롬프트 편향이 되지 않도록,
recency 가중치는 낮추고, action bonus는 완화하며, 다중 슬롯에서는 diversity penalty를 함께 적용한다

여기에 trade_cases.feature_vector와 현재 market snapshot vector의 cosine similarity를 섞어,
태그 규칙만으로는 포착되지 않는 수치적 유사성도 retrieval에 반영한다

retrieval 흐름은

1. trade_case_vector_index에서 vector top-k 후보 검색
2. symbol overlap / trigger overlap / risk_mode / 결과 / slippage / failure reason으로 rerank

구조를 따른다

memory_updater는 journal + symbol_journal 외에도 trade_cases를 읽어 repeated failure reason,
signal failure, slippage, excursion 품질을 candidate lesson seed로 반영한다

또한 trade_case_clusters를 만들어 symbol + failure_reason + regime 조합별 반복 손실 군집을 요약하고,
이 군집을 risk_control lesson의 추가 seed로 사용한다

최근 버전에서는 이 cluster를 단순 규칙 그룹핑이 아니라 numeric feature distance 기반 online centroid clustering으로 만든다
또한 silhouette / nearest-cluster separation / temporal holdout consistency를 계산해 quality_score와 quality_label을 붙인다
quality가 낮거나 holdout에서 손실 패턴이 더 이상 확인되지 않는 cluster는 retired 처리하고, lesson seed에서 자동 제외한다
memory batch는 retrieval_evaluation_summary와 retrieval_counterfactual_summary도 함께 집계해 retrieval 품질을 장기적으로 평가한다
`retrieval_counterfactual_summary.avg_outcome_utility_delta`는 with retrieval decision이 without retrieval 대비 path quality 기준으로 얼마나 나았는지 평균 gap을 보여준다

입력 특징 예:

- spread_percentile_1h
- oi_change_15m
- volume_ratio_15m
- distance_to_invalid_level
- slippage_bps
- observed MFE / MAE
- trend / volatility encoding

9.3 리플레이(강력 권장)

과거 스냅샷으로 “그때 의사결정 재현(모의)”

중복 주문/폭주/부분체결 꼬임 같은 운영 문제를 가장 빨리 잡는 장치

리플레이 비교에는 rationale 키워드뿐 아니라 rationale_items.evidence signal 추가/삭제도 포함해, 어떤 근거 변화가 decision 변화로 이어졌는지 추적한다

운영 대시보드는 최근 replay evidence diff, 최근 memory candidate seed, 최근 symbol_journal을 함께 노출한다
또한 `Entry Styles 24h`와 `Post-Trade Labels` 요약으로 `immediate/retest/value`별 성과와 `edge_failed`, `missed_due_to_no_fill`, `risk_exit`, `stale_entry_cancelled` 라벨 빈도를 같이 노출한다

audit detail에서는 raw JSON뿐 아니라 rationale_items 요약과 evidence quality / entry size cap 요약도 카드 형태로 노출한다

source 경로는 클릭해서 저장된 DecisionRequest 내 실제 값을 바로 조회할 수 있게 한다

동시에 Stored Request JSON에서도 해당 값 위치를 강조 표시한다

latest replay가 있으면 같은 source에 대한 original observed_value와 replay observed_value 차이도 함께 보여준다

이 replay source diff는 표 형태로 요약해 source / original / replayed / delta를 한 줄씩 비교할 수 있게 한다

대시보드 상단에는 최근 audit 기준 semantic validation fallback 횟수와 최다 실패 유형도 함께 노출한다

추가로 최근 24시간 기준 context mix도 노출해,
`good_hold / missed_hold / action / fallback / other_hold` 비율을 바로 확인할 수 있게 한다

source lookup에는 current value뿐 아니라 previous value와 original/replay threshold도 함께 보여준다

semantic validation fallback은 24h / 7d / latest 50 calls 기준으로 나눠 본다

같은 카드에서 모델별 fallback rate도 함께 보여준다

daily memory batch는 evidence signal 반복뿐 아니라 evidence_quality.factor 버킷과 entry_size_cap 조정 패턴도 집계해, 낮은 근거 품질이 실제 손실과 묶이는지까지 heuristic candidate lesson에 반영한다

memory batch 모델은 실시간 의사결정 모델과 분리할 수 있고, `MEMORY_PROVIDER` / `MEMORY_MODEL`이 지정되면 장기 교훈 추출에 그 설정을 우선 사용한다

10) 정책 파일(policy.yaml) 운영

심볼/레버리지/손실한도/주문 타입/주문 빈도 제한/트리거 조건을 외부 파일로 관리

운영 중 룰 수정이 쉬워지고, 모델이 이상한 주문을 제안해도 실행기가 차단

`execution_guards` 섹션에서 loop lock TTL, decision cooldown, entry mutex TTL, unknown state block window, recovery sweep 주기를 조정

`daily_batch` 섹션에서 local schedule 시각과 replay/memory subprocess timeout을 조정하며, 이 배치는 스케줄러 메인 루프와 분리되어 동작해야 한다
특히 `MEMORY_PROVIDER=deepseek`이면 memory updater subprocess timeout은 `deepseek_memory_timeout_seconds`를 floor로 사용해 더 길게 잡는다.

11) 보안/운영

API 키는 출금 권한 OFF

가능하면 IP 화이트리스트 사용

실거래 키와 페이퍼 키 분리

장애 복구 시: 재시작 → Bybit에서 포지션/미체결 재조회 → 상태 재구성

12) 비용(개요)

LLM API 비용은 호출 횟수보다 **입력/출력 토큰(추론 토큰 포함)**에 좌우됨.

비용 최적화 핵심:

실시간 루프에는 **최근 요약 + 장기 요약(압축본)**만 제공

원문 전체 일지는 DB에 저장하고, 배치에서만 길게 분석

출력은 “짧은 JSON”으로 제한

13) 유지보수 상세

이 시스템의 유지보수는 단순 재시작이 아니라, 정책/모델/데이터/실행/메모리의 다섯 축을 계속 점검하는 작업이다

일간 체크리스트:

대시보드에서 ledger net pnl, Symbol Report, Semantic Fallbacks를 먼저 본다

24h / 7d / latest 50 calls 기준 semantic fallback이 급증했는지 본다

최근 audit 5~10개의 risk_reason, evidence_quality, entry_size_cap이 비정상적으로 치우치지 않았는지 본다

Bybit 실제 포지션/미체결과 audit의 positions, open_orders, receipts가 맞는지 샘플 확인한다

timeout_unresolved, failed, market fetch warning이 반복되는지 로그를 본다

주간 체크리스트:

최근 손실 audit 10~20개를 replay로 다시 비교한다

evidence_signals_added/removed, source diff, previous value / threshold를 함께 본다

long_term_memory의 recent rule과 candidate_lessons를 읽고 과잉 일반화 여부를 확인한다

심볼별 net pnl을 보고 손실 심볼 유지 여부를 결정한다

정책 변경은 한 번에 하나만 조정하고, 변경 후에는 demo 또는 축소 size로 다시 본다

월간 체크리스트:

policy.yaml의 손실 한도, size cap, max positions, trigger threshold를 재검토한다

default / trigger / memory 모델 분리가 여전히 적절한지 본다

context_budget은 `tools/tune_context_budget.py`로 최근 audit 로그의 토큰/컨텍스트 분포를 기반으로 주기 조정한다

SQLite 파일 크기와 audit_logs / position_ledger / replay_runs 증가량을 점검한다

오래된 memory rule pruning과 audit archive 정책이 필요한지 검토한다

모델 품질 점검:

대시보드의 Semantic Fallbacks를 먼저 본다

24h / 7d / latest 50 calls 기준으로 fallback이 늘면 최근 모델 변경, 프롬프트 변경, journal 문맥 변화가 있었는지 확인한다

특정 모델의 failure rate가 높으면 default / trigger / memory 모델 분리를 다시 조정한다

fallback issue가 operator_condition_failed인지 trade_implication_action_mismatch인지 구분해 원인을 좁힌다

정책 파일 유지:

policy.yaml은 운영 중 가장 자주 손대는 파일이다

daily_max_loss_pct, max_consecutive_losses, mode_profiles의 size cap, max_open_positions, max_leverage는 시장 레짐 변화 시 재조정 대상이다

정책 변경 후에는 바로 live 전환하지 말고 demo 관찰 → replay 비교 → journal 확인 순서로 검증한다

데이터 품질 점검:

market snapshot의 timeframes, order_book, derivatives, previous_values가 비정상적으로 비지 않았는지 본다

WebSocket 장애 시 REST fallback으로 운영은 되지만 지연과 호출 수가 올라갈 수 있다

로그에서 market fetch 실패, open-order fetch fallback, semantic fallback 급증이 반복되면 데이터 품질 이슈로 본다

계좌/실행 동기화 점검:

audit의 receipts, order_state_transitions, position_state_transitions, Bybit 실제 상태가 일치하는지 본다

timeout_unresolved가 늘어나면 size 확대나 live 운영 확대를 멈추는 게 맞다

장기 메모리 유지:

long_term_memory는 많이 쌓는 것보다 나쁜 교훈을 오래 유지하지 않는 것이 중요하다

candidate_lessons, winning/losing evidence_signals, evidence_quality_patterns를 같이 본다

특정 regime에서만 맞는 룰을 전체 룰처럼 일반화하지 않도록 검토한다

DB / 로그 유지:

SQLite 파일 크기 증가 속도, audit_logs / position_ledger / replay_runs 증가량, 로그 rotation을 주기적으로 본다

운영이 길어지면 최근 30~90일은 hot storage, 그 이전은 archive하는 정책을 두는 것이 좋다

14) 수익률이 안 나올 때 해볼 것

수익률 부진은 보통 실행 품질, 과다 진입, 과도한 보수화, 특정 심볼 문제, 시장 레짐 변화 중 하나로 나뉜다

진입은 맞는데 실행이 나쁜 경우:

wide_spread / elevated_funding이 잦은 시간대를 줄인다

max_order_notional_pct_balance를 낮춰 슬리피지 영향을 줄인다

유동성 낮은 심볼을 제외한다

진입 자체가 과다한 경우:

trigger threshold를 보수화한다

guarded 상태가 너무 자주 켜지는지, cadence 조건이 과도하게 민감한지 본다

low evidence quality에서는 HOLD가 더 자주 나오도록 validator / risk 정책을 강화한다

최근 journal에서 반복 손실 signal과 anti-pattern 반영 여부를 확인한다

손실은 적은데 수익도 안 나는 경우:

REDUCE / EXIT journal을 따로 모아 너무 빨리 수익 포지션을 잘라내는지 본다

장기 메모리가 지나치게 보수적인 교훈만 강화하는지 점검한다

guarded 상태 진입이 과도해 opportunity를 지나치게 놓치지 않는지 본다

특정 심볼만 문제인 경우:

Symbol Report로 심볼별 net pnl을 본다

손실 심볼을 잠시 제외하고 관찰한다

해당 심볼의 spread_bps, funding_rate, open_interest 변동이 다른 심볼보다 거친지 확인한다

시장 레짐이 바뀐 경우:

예전 성공 audit와 최근 실패 audit를 replay로 비교한다

trend_alignment, volatility_regime, risk_trigger_reasons 차이를 본다

오래된 long-term memory rule을 약화하거나 pruning 한다

수익률 부진 시 권장 순서:

대시보드에서 ledger net pnl, Symbol Report, Semantic Fallbacks를 먼저 본다

손실 audit 10~20개를 replay로 비교한다

journal의 반복 손실 signal과 evidence quality 패턴을 확인한다

정책 변경은 한 번에 하나씩만 한다

변경 후에는 demo 또는 축소 size로 다시 관찰한다

15) 단계별 로드맵
Phase 1: MVP(안전한 자동매매 최소셋)

BTCUSDT 단일 심볼

adaptive cadence(기본 1시간, 관리 시 15분, trigger 시 5분)

TradeIntent JSON 스키마 고정

리스크 엔진(킬스위치/쿨다운/노출 제한/주문 제한)

실행 엔진(멱등성+재시도 안전)

감사 로그 + 요약 일지 + 알림

Phase 2: 장기 요약 자동화(배치)

매일 LongTermMemory 업데이트

모순 경험을 레짐 분기로 관리

hypothesis→rule 승격 규칙

Phase 3: 트리거 모드 + 유사 사례 검색(RAG)

이벤트 발생 시만 과거 유사 케이스 3~10개를 첨부

이때 유사도는 시장+컨텍스트 26차원 기준으로 계산한다

비용은 유지하면서 통찰 강화

Phase 4: 운영 최적화

모델 하이브리드(평소 Flash 계열, 중요 이벤트 Pro 계열 등)

대시보드(거절률/슬리피지/오류율/성과) 추가

16) MVP 권장 초기 기본값

심볼: BTCUSDT

최대 레버리지: 2~3x

하루 손실 제한: 2~3%

손절 후 쿨다운: 30분

트리거 모드: 신규 진입 최소화, 관리 중심

데이터: 마감 캔들만 사용
