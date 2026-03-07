Bybit 기반 Gemini API 연동 자동매매 시스템 종합 기획서
1) 목표

Bybit에서 자동매매를 수행하는 시스템을 구축한다.

Gemini API는 시장/계좌 상태를 바탕으로 “주문 후보(JSON)”를 생성하는 역할만 맡고,

사실 조회(포지션/잔고/주문)·리스크 통제·주문 실행은 코드가 전담한다.

단일 adaptive loop가 현재 상태에 따라 1시간 / 15분 / 5분 cadence를 선택해 실행한다.

매매일지(원본+요약)와 장기 요약(LongTermMemory)을 유지해 맥락/통찰을 축적한다.

2) 설계 원칙

Gemini는 전략가, 시스템은 경찰

Gemini는 추천 JSON을 생성

시스템은 규정 검증 후 실행/거절

진실의 원천은 Bybit API/DB

계좌/포지션/미체결은 Gemini가 “추정”하지 않는다.

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

`AccountReader.get_runtime_diagnostics()`로 account cache freshness와 private WS health / event count / lag를 한 번에 볼 수 있다

state_store (DB)

상태(리스크 상태, 최근 트레이드 요약, 장기 메모리 버전 등) 저장

snapshot_service

account_reader / collector / state_store를 묶어 공용 스냅샷을 조립

한 사이클에서 계좌 상태, 시장 상태, 리스크 상태, trigger / risk mode를 한 번에 계산

5분 기준 공용 스냅샷을 materialize하고, 상위 타임프레임은 새 봉이 마감될 때만 선택적으로 refresh

계좌 상태는 `SnapshotService -> AccountReader -> private WS cache`를 우선 사용하고, 주문 직후/WS 비정상/주기적 reconcile 시에만 REST 보정

decision_engine (Gemini)

입력 스냅샷+메모리를 받아 TradeIntent(JSON) 출력

risk_engine

정책(policy.yaml) 기반 검증: 손실한도/노출/레버리지/슬리피지/쿨다운 등

최근 구현에서는 쿨다운도 고정 30분이 아니라 손실 강도, 연속 손실, 반복된 `thesis_failure_reason`을 같이 봐서 동적으로 늘어난다.
또 `drawdown`은 BUY/SELL을 완전히 막지 않되 `confidence_cap`, `entry_size_multiplier`, `failure_pattern_score`로 추가 감쇠한다.

execution_engine

주문 실행/취소/정정 + 멱등성(clientOrderId) + 재시도 안전 처리

journal & audit logger

원본 입력/출력/주문/체결/결과 저장 + 사람용 요약 일지

long_term_memory_updater (Batch)

하루 1회(권장) 장기 요약 업데이트(교훈/안티패턴/국면 맵)

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

15분 cadence

포지션 보유 중

또는 미체결 주문 존재

또는 unknown order 상태 존재

또는 최근 진입/청산 직후라 더 짧은 관리 주기가 필요한 경우

역할: 포지션 관리, 주문 정리, 감축/청산 중심

5분 cadence

opportunity_trigger 활성화

역할: trigger 대응과 빠른 재판단

기본 정책에서는 포지션이 없을 때만 첫 trigger entry를 허용하고, 기존 포지션이 있으면 추가 진입 대신 HOLD/REDUCE/EXIT 위주로 동작

즉, trigger가 뜨면 무엇이 돌고 있든 5분 cadence가 선점하고, trigger가 사라지면 포지션/주문 상태에 따라 15분 또는 1시간으로 강등된다.

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

같은 심볼에서 strong opportunity trigger(`volume_surge_15m` 또는 `range_break_1h`)를 포함한 opportunity reason이 2개 이상일 때만 승격

`5m -> 15m`

trigger 해제 + clear streak 충족 + 포지션/주문/복구 대상 존재

`5m -> 1h`

trigger 해제 + clear streak 충족 + 포지션/주문/복구 대상 없음

`15m -> 1h`

flat 상태가 최소 유지 시간 이후 여러 사이클 연속 확인될 때만 강등

이 히스테리시스는 cadence가 경계값 근처에서 `5m <-> 15m <-> 1h`로 흔들리는 현상을 줄이기 위한 것이다.

4.3 트리거 분리 원칙

트리거는 두 부류로 분리한다.

opportunity_trigger: 급변/돌파/거래량 급증처럼 관찰 빈도를 높일 이유가 있는 이벤트

risk_trigger: 스프레드 확대, 펀딩비 왜곡처럼 신규 진입 판단을 더 보수적으로 만들어야 하는 거래 품질 악화

fast_price_move_15m, volume_surge_15m, range_break_1h에는 ON/OFF 기준을 분리한 히스테리시스를 적용해 경계 구간 점멸을 줄임

5) 입력/출력 표준화(JSON)
5.1 Gemini 입력(DecisionRequest)

policy_summary: 현재 정책 핵심(레버리지/한도/쿨다운 등)

account_state: 계좌 스냅샷 시각, 잔고, 총 미실현손익, 마진비율, 포지션 위험정보, 미체결 주문 상세(사실)

market_snapshot: 15m/1h 캔들(마감된 캔들만), 핵심 지표/레벨 요약

멀티 심볼 운영에서는 market_snapshots로 여러 심볼 스냅샷을 한 번에 전달해 포트폴리오 단일 판단을 생성

market_snapshot 안에는 opportunity_trigger_reasons / risk_trigger_reasons를 분리해 포함

또한 market snapshot 상단에는 `volume_ratio_15m`, `volume_surge_15m_now`,
`fast_move_trigger_active`, `volume_surge_trigger_active`, `range_break_trigger_active`,
`oi_change_15m`, `oi_change_1h`, `funding_rate_change_1h`, `basis_change_1h`,
`spread_vs_1h_avg`, `spread_percentile_1h`, `depth_percentile_1h`,
`distance_to_15m_breakout_level`, `distance_to_1h_breakout_level`, `distance_to_invalid_level`
같은 파생 요약 필드를 추가해 LLM이 raw 값보다 change / percentile / distance / trigger-state를 우선 해석하게 한다

risk_state: 일일 손익률, 연속 손실, 쿨다운, 드로우다운 상태

recent_journal_context: 같은 최근 일지 정보를 구조화 객체로 전달

문자열 파싱 대신 rationale_items, evidence_quality, entry_size_cap, trigger 정보를 직접 참조할 수 있게 함

운영 시에는 context_budget으로 recent_journal_context를 점수 기반 상위 N개로 압축해 토큰을 제어

long_term_memory: 장기 교훈/안티패턴/국면-전략 맵(압축본)

long_term_memory도 현재 risk_mode/시장 레짐/심볼 관련성이 높은 항목만 상위 N개를 전달

최근에는 long_term_memory를 append-only 로그로 두지 않고,
- confidence_score로 support / cluster quality / retrieval 효과를 함께 반영하고
- decay_score + lifecycle_status로 오래 검증되지 않은 rule을 감쇠/은퇴시키고
- canonical_key + rule similarity로 비슷한 lesson을 병합하는 방향으로 운영한다

constraints: “JSON only”, 출력 최대 길이, 허용 행동 제한

프롬프트는 compact JSON으로 직렬화하고, 완전히 동일한 prompt + model은 decision_cache에서 재사용

캐시 키 안정성을 위해 account_state의 고변동 필드(예: snapshot_timestamp)는 canonicalization 단계에서 제외

시장 데이터 수집은 OHLCV 롤링 캐시와 ticker/order book/funding/open interest TTL 캐시를 사용해 네트워크 부하를 줄임

RAG는 audit 저장 시 심볼별 feature vector를 함께 저장하고, 조회 시에는 심볼별 반복 스캔 대신 배치 유사도 계산으로 DB 부하를 줄임

RAG vector는 시장 13차원만 쓰지 않고, 리스크/계좌/포지션 컨텍스트 13차원을 합쳐 26차원으로 구성한다

유사도 계산 전에는 차원별 normalization, outlier clipping, weighting을 적용해 큰 스케일 축의 과도한 지배를 줄인다

5.2 Gemini 출력(TradeIntent.json)

decision: HOLD/BUY/SELL/REDUCE/EXIT

orders[]: 주문 후보(가격/수량/TP/SL/reduce_only 포함)

rationale[]: 근거(짧게)

rationale_items[]: 구조화 근거

각 rationale_item은 summary, why_it_matters, trade_implication, failure_mode, evidence[]를 포함

각 evidence는 signal, timeframe, operator, observed_value, threshold, units, source를 포함

timeframe snapshot에는 previous_values를 둘 수 있고, cross_up/cross_down 검증 시 source별 직전값으로 사용한다

source는 DecisionRequest 안의 실제 경로여야 하고, 관측값과 맞지 않으면 보수적으로 HOLD fallback

operator도 의미 검증한다. gte/lt/cross_up/delta_abs_gte 같은 비교가 실제 관측값과 threshold에서 성립하지 않으면 HOLD fallback

신규 진입에서는 trade_implication이 실제 행동 방향과 맞지 않거나, 근거 품질에 비해 confidence가 과하면 보수적으로 HOLD fallback

신규 진입에서는 구조화 근거의 품질도 리스크 판단에 반영한다

예: signal 다양성 부족, 주문 심볼 근거 누락, failure_mode 부재 시 max_order_notional cap을 추가로 축소

risk_notes[]: 위험 요인

confidence: 참고값(실행은 규정 통과 여부로 결정)

중요: 시스템은 “주문 실행 권한”을 Gemini에 주지 않는다. JSON은 추천일 뿐.

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

7) 장기 요약 자동화(배치) 명세
7.1 주기

매일 1회(권장): 예) KST 04:00
(주 1회도 가능하지만 통찰 업데이트가 느림)

7.2 방식(오염 방지 필수)

통계(승률/손익비/낙폭 등)는 코드가 계산

Gemini는 통계 해석과 교훈 구조화만 수행

“표본 수 부족”은 hypothesis, 충분하면 rule로 승격

각 교훈은 반드시 근거(트레이드 ID/기간/표본수)를 가진다.

8) 리스크 관리(필수 안전장치)
8.1 회로차단기(Kill switch)

하루 손실 -X%면 자동 정지

연속 손실 Y회면 신규 진입 금지 + 쿨다운

슬리피지/거절률/오류율 임계치 초과 시 정지

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

Gemini 요청/응답 원문

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

최근 구조에서는 symbol_journal도 함께 집계해 심볼 단위 evidence signal 반복 패턴을 별도로 반영한다

symbol 기반 candidate lesson의 evidence_journal_ids는 audit_log_id가 아니라 symbol_journal.id를 근거로 기록한다

symbol_journal 조회 효율을 위해 symbol+id, timestamp+id, audit_log_id 인덱스를 유지한다

recent_journal_context 선별도 단순 recency score가 아니라 trade_cases의 symbol overlap, trigger overlap,
risk_mode 일치, regime tag, closed win/loss 결과를 함께 보는 similarity score로 확장한다

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

audit detail에서는 raw JSON뿐 아니라 rationale_items 요약과 evidence quality / entry size cap 요약도 카드 형태로 노출한다

source 경로는 클릭해서 저장된 DecisionRequest 내 실제 값을 바로 조회할 수 있게 한다

동시에 Stored Request JSON에서도 해당 값 위치를 강조 표시한다

latest replay가 있으면 같은 source에 대한 original observed_value와 replay observed_value 차이도 함께 보여준다

이 replay source diff는 표 형태로 요약해 source / original / replayed / delta를 한 줄씩 비교할 수 있게 한다

대시보드 상단에는 최근 audit 기준 semantic validation fallback 횟수와 최다 실패 유형도 함께 노출한다

source lookup에는 current value뿐 아니라 previous value와 original/replay threshold도 함께 보여준다

semantic validation fallback은 24h / 7d / latest 50 calls 기준으로 나눠 본다

같은 카드에서 모델별 fallback rate도 함께 보여준다

daily memory batch는 evidence signal 반복뿐 아니라 evidence_quality.factor 버킷과 entry_size_cap 조정 패턴도 집계해, 낮은 근거 품질이 실제 손실과 묶이는지까지 heuristic candidate lesson에 반영한다

memory batch 모델은 실시간 의사결정 모델과 분리할 수 있고, `GEMINI_MEMORY_MODEL`이 지정되면 장기 교훈 추출에 그 모델을 우선 사용한다

10) 정책 파일(policy.yaml) 운영

심볼/레버리지/손실한도/주문 타입/주문 빈도 제한/트리거 조건을 외부 파일로 관리

운영 중 룰 수정이 쉬워지고, 모델이 이상한 주문을 제안해도 실행기가 차단

`execution_guards` 섹션에서 loop lock TTL, decision cooldown, entry mutex TTL, unknown state block window, recovery sweep 주기를 조정

11) 보안/운영

API 키는 출금 권한 OFF

가능하면 IP 화이트리스트 사용

실거래 키와 페이퍼 키 분리

장애 복구 시: 재시작 → Bybit에서 포지션/미체결 재조회 → 상태 재구성

12) 비용(개요)

Gemini API 비용은 호출 횟수보다 **입력/출력 토큰(추론 토큰 포함)**에 좌우됨.

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

trigger mode의 size cap과 max_open_positions를 더 낮춘다

low evidence quality에서는 HOLD가 더 자주 나오도록 validator / risk 정책을 강화한다

최근 journal에서 반복 손실 signal과 anti-pattern 반영 여부를 확인한다

손실은 적은데 수익도 안 나는 경우:

REDUCE / EXIT journal을 따로 모아 너무 빨리 수익 포지션을 잘라내는지 본다

장기 메모리가 지나치게 보수적인 교훈만 강화하는지 점검한다

trigger mode의 신규 진입 억제가 과한지 본다

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

BTCUSDT/ETHUSDT 1~2개 심볼

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

심볼: BTCUSDT, ETHUSDT

최대 레버리지: 2~3x

하루 손실 제한: 2~3%

손절 후 쿨다운: 30분

트리거 모드: 신규 진입 최소화, 관리 중심

데이터: 마감 캔들만 사용
