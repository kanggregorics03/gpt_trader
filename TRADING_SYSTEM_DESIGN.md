Bybit 기반 Gemini API 연동 자동매매 시스템 종합 기획서
1) 목표

Bybit에서 자동매매를 수행하는 시스템을 구축한다.

Gemini API는 시장/계좌 상태를 바탕으로 “주문 후보(JSON)”를 생성하는 역할만 맡고,

사실 조회(포지션/잔고/주문)·리스크 통제·주문 실행은 코드가 전담한다.

15분/1시간 주기 실행 + 트리거 발생 시 더 자주 실행(예: 5분).

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

가능하면 Bybit public WebSocket으로 ticker/orderbook/kline을 수신하고, 부족한 데이터만 REST fallback

account_reader

잔고/포지션/미체결 주문을 Bybit에서 조회

포지션별 청산가/레버리지/증거금/명목가치와 미체결 주문 상세까지 정규화

시장 데이터는 메인넷 public API를 사용하고, 계좌 조회/주문 실행은 live 또는 demo private API를 선택할 수 있게 분리

짧은 TTL 계좌 캐시를 사용하고, 주문 실행 직후에는 invalidate 후 재조회해 stale 상태를 방지

가능하면 Bybit private WebSocket으로 wallet/position/order/execution을 유지하고, 일정 주기로 REST reconcile

state_store (DB)

상태(리스크 상태, 최근 트레이드 요약, 장기 메모리 버전 등) 저장

decision_engine (Gemini)

입력 스냅샷+메모리를 받아 TradeIntent(JSON) 출력

risk_engine

정책(policy.yaml) 기반 검증: 손실한도/노출/레버리지/슬리피지/쿨다운 등

execution_engine

주문 실행/취소/정정 + 멱등성(clientOrderId) + 재시도 안전 처리

journal & audit logger

원본 입력/출력/주문/체결/결과 저장 + 사람용 요약 일지

long_term_memory_updater (Batch)

하루 1회(권장) 장기 요약 업데이트(교훈/안티패턴/국면 맵)

monitoring & alert

알림(진입/청산/손절/정지/오류), 헬스체크(오류율/지연/거절률)

4) 실행 스케줄과 트리거
4.1 기본 스케줄(권장)

1시간 루프: 방향성/전략 모드(공격/방어) 업데이트

15분 루프: 포지션 관리(손절/익절/정정), 신규 진입은 제한적으로

4.2 트리거 모드(예: 5분)

트리거 예:

변동성 급증(ATR 급증)

손절/익절 근접

특정 레벨 돌파/이탈

미체결 주문 장시간 유지

데이터/계좌 상태 불일치 감지

트리거 모드 특징:

호출은 잦지만, 더 강한 과매매 방지 규칙 적용

신규 진입보다 리스크 관리(감축/청산/정정) 중심 운영

트리거는 두 부류로 분리:

opportunity_trigger: 급변/돌파/거래량 급증처럼 관찰 빈도를 높일 이유가 있는 이벤트

risk_trigger: 스프레드 확대, 펀딩비 왜곡처럼 신규 진입을 줄이거나 막아야 하는 거래 품질 악화

fast_price_move_15m, volume_surge_15m, range_break_1h에는 ON/OFF 기준을 분리한 히스테리시스를 적용해 경계 구간 점멸을 줄임

5) 입력/출력 표준화(JSON)
5.1 Gemini 입력(DecisionRequest)

policy_summary: 현재 정책 핵심(레버리지/한도/쿨다운 등)

account_state: 계좌 스냅샷 시각, 잔고, 총 미실현손익, 마진비율, 포지션 위험정보, 미체결 주문 상세(사실)

market_snapshot: 15m/1h 캔들(마감된 캔들만), 핵심 지표/레벨 요약

멀티 심볼 운영에서는 market_snapshots로 여러 심볼 스냅샷을 한 번에 전달해 포트폴리오 단일 판단을 생성

market_snapshot 안에는 opportunity_trigger_reasons / risk_trigger_reasons를 분리해 포함

risk_state: 일일 손익률, 연속 손실, 쿨다운, 드로우다운 상태

recent_journal_context: 같은 최근 일지 정보를 구조화 객체로 전달

문자열 파싱 대신 rationale_items, evidence_quality, entry_size_cap, trigger 정보를 직접 참조할 수 있게 함

long_term_memory: 장기 교훈/안티패턴/국면-전략 맵(압축본)

constraints: “JSON only”, 출력 최대 길이, 허용 행동 제한

프롬프트는 compact JSON으로 직렬화하고, 완전히 동일한 prompt + model은 decision_cache에서 재사용

시장 데이터 수집은 OHLCV 롤링 캐시와 ticker/order book/funding/open interest TTL 캐시를 사용해 네트워크 부하를 줄임

RAG는 audit 저장 시 시장 feature vector를 함께 저장하고, 조회 시에는 심볼별 반복 스캔 대신 배치 유사도 계산으로 DB 부하를 줄임

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

daily memory batch는 rationale_json의 evidence signal 반복 패턴과 당시 market regime를 함께 읽어 장기 규칙을 만든다

또한 evidence signal 반복에서 heuristic candidate lesson을 먼저 만들고, LLM은 이를 정제/병합하는 역할로 사용한다

9.3 리플레이(강력 권장)

과거 스냅샷으로 “그때 의사결정 재현(모의)”

중복 주문/폭주/부분체결 꼬임 같은 운영 문제를 가장 빨리 잡는 장치

리플레이 비교에는 rationale 키워드뿐 아니라 rationale_items.evidence signal 추가/삭제도 포함해, 어떤 근거 변화가 decision 변화로 이어졌는지 추적한다

운영 대시보드는 최근 replay의 evidence diff와 최근 memory의 heuristic candidate lesson seed를 함께 노출한다

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

1시간 + 15분 스케줄

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
