# Profitability Troubleshooting

이 문서는 "시스템은 정상 동작하는데 수익률이 기대에 못 미친다"는 상황에서 원인을 체계적으로 좁히기 위한 운영용 점검 문서다.

핵심 원칙은 단순하다.

- 먼저 `전략 엣지`가 있는지 본다.
- 그 다음 `실행 비용`이 엣지를 얼마나 깎는지 본다.
- 그 다음 `리스크/사이징/거래 빈도`가 성과를 망치는지 본다.
- 마지막으로 `LLM 판단`, `데이터 품질`, `운영 안정성`을 본다.

수익률 부진을 처음부터 모델 탓으로 보면 거의 항상 진단 순서가 틀어진다.

## 1. 먼저 해야 할 분류

수익률이 안 나올 때는 원인을 아래 8개 범주 중 하나로 먼저 분류한다.

1. 전략 자체에 엣지가 없다.
2. 수수료, 슬리피지, 스프레드가 엣지를 다 먹는다.
3. 포지션 사이징과 리스크 감쇠가 맞지 않는다.
4. HOLD를 충분히 못 하고 과잉거래한다.
5. 실전 시점에 쓸 수 없는 정보나 품질 낮은 데이터에 의존한다.
6. 평가 체계가 잘못되어 과적합이나 누수가 숨어 있다.
7. WS stale, 주문 복구 실패, 계좌 상태 stale 같은 운영 결함이 손익을 깎는다.
8. LLM이 실제로 판단 품질이나 출력 안정성에서 문제를 만든다.

이 분류를 하지 않으면, 모델만 바꾸고 같은 문제를 반복하게 된다.

## 2. 권장 진단 순서

아래 순서대로 보는 것이 가장 안전하다.

1. 비용 전 성과가 있는지 확인한다.
2. 비용 후 성과가 남는지 확인한다.
3. 거래 수를 줄였을 때 더 좋아지는지 확인한다.
4. 특정 심볼, 레짐, 시간대에 손실이 몰리는지 본다.
5. 운영 장애가 손익에 섞여 있지 않은지 본다.
6. 그 다음에야 LLM이 baseline보다 실제로 나쁜지 확인한다.

순서를 뒤집으면 원인 분리가 잘 안 된다.

## 3. 빠른 응급 대응

손익이 나쁜 상태에서 아래 중 하나가 보이면 먼저 거래 강도를 낮추는 편이 맞다.

- 최근 24시간 `Daily Kill Switch Activated`가 발생했다.
- `Drawdown Mode Entered`가 반복되는데도 신규 진입이 많다.
- `WebSocket Stale`가 잦고 `Recovered`까지 시간이 오래 걸린다.
- `Execution Timeout`, `Recovery Sweep`, `Unknown order`가 증가한다.
- 비용 전 성과는 약한데 거래 수만 많다.

즉시 해볼 것:

1. `max_order_notional_pct_balance`를 낮춘다.
2. trigger mode 신규 진입을 잠시 축소한다.
3. 심볼 수를 줄인다.
4. 문제 구간의 실거래를 멈추고 demo 또는 축소 size로 재관찰한다.

## 4. 원인별 해석 가이드

### 4.1 전략 엣지 부족

전형적 증상:

- 비용 전 추정 손익도 약하다.
- 승률이 높아도 손익비가 나쁘다.
- 심볼별, 레짐별로 봐도 일관된 우위가 없다.
- replay나 rationale은 그럴듯하지만 실제 outcome과 연결되지 않는다.

무엇을 볼 것인가:

- `journal.estimated_pnl_pct`
- `position_ledger.net_realized_pnl`
- `trade_cases.realized_pnl`
- `Symbol Report`
- 레짐별 손익 분해

어떻게 해석할 것인가:

- 비용 전에도 약하면 실행 문제가 아니라 전략 문제다.
- 평균 손익이 작고 거래 수가 많다면 신호 품질이 아니라 노이즈 거래일 가능성이 높다.
- 일부 심볼과 일부 레짐에서만 플러스면 전체 전략이 아니라 부분 전략이다.

바로 해볼 것:

1. 심볼 universe를 줄인다.
2. 시간대를 줄인다.
3. 추세 정렬과 참여 확장이 동시에 있는 구간만 남긴다.
4. breakout 미확인 구간은 HOLD를 기본값으로 만든다.
5. baseline rule과 비교해 실제 개선이 있는지 다시 본다.

실험 아이디어:

1. 최근 100개 decision을 `all trades`, `trigger trades only`, `trend-aligned only`로 나눠 손익을 비교한다.
2. `HOLD`를 더 많이 했을 때 가정 손익이 어떻게 바뀌는지 replay로 본다.
3. 손실 심볼 하나를 제외한 결과와 전체 결과를 비교한다.

피해야 할 오해:

- rationale이 설득력 있어 보여도 엣지가 있다는 뜻은 아니다.
- 승률만 높다고 좋은 전략은 아니다.

### 4.2 실행 비용 과다

전형적 증상:

- 비용 전 성과는 플러스인데 비용 후 급격히 나빠진다.
- 짧은 holding time 구간에서만 성과가 무너진다.
- spread percentile, slippage bps가 높을수록 손실이 커진다.

무엇을 볼 것인가:

- `position_ledger.fee_cost`
- `trade_cases.slippage_bps`
- `trade_cases.slippage_cost`
- `market_snapshot.order_book.spread_bps`
- `spread_percentile_1h`
- `depth_percentile_1h`

어떻게 해석할 것인가:

- 비용 전 플러스, 비용 후 마이너스면 의사결정보다 execution quality filter가 우선이다.
- market order 위주, 짧은 보유 시간, 얕은 depth 조합이면 비용 민감도가 높다.

바로 해볼 것:

1. spread/depth filter를 강화한다.
2. size를 줄인다.
3. fast move 직후 추격 진입을 줄인다.
4. trigger loop의 신규 진입 조건을 더 보수적으로 만든다.
5. limit entry가 가능한 정책인지 별도로 검토한다.

실험 아이디어:

1. `spread_percentile_1h` 상위 구간을 제외한 경우 손익을 다시 계산한다.
2. `max_order_notional_pct_balance`를 절반으로 낮춘 주간과 기존 주간을 비교한다.
3. holding time이 매우 짧은 거래를 제외했을 때 손익이 개선되는지 본다.

피해야 할 오해:

- 모델이 방향을 맞춘다고 해도 execution이 나쁘면 수익은 사라진다.
- 수수료만 보고 슬리피지를 빼먹으면 원인 분리가 틀어진다.

### 4.3 사이징/리스크 감쇠 문제

전형적 증상:

- 방향은 자주 맞는데 MDD가 크다.
- 손실 구간에서 손실폭이 급격히 커진다.
- drawdown mode, cooldown, failure pattern score가 자주 켜지는데도 계좌 손상이 크다.

무엇을 볼 것인가:

- `risk_mode`
- `entry_size_cap`
- `failure_pattern_score`
- `cooldown_minutes_effective`
- `drawdown_active`
- `daily_pnl_pct`
- 모드별 평균 진입 size

어떻게 해석할 것인가:

- 이 경우 진입 정확도보다 `못 맞을 때 얼마나 작게 잃는지`가 문제다.
- drawdown이나 cooldown이 자주 켜지면 이미 리스크 엔진이 "크기와 빈도를 줄여라"라고 말하고 있는 셈이다.

바로 해볼 것:

1. `max_order_notional_pct_balance`를 낮춘다.
2. drawdown mode의 `entry_size_multiplier`를 더 낮춘다.
3. `confidence_cap`을 더 보수적으로 둔다.
4. 연속 손실 시 `Cooldown Extended`가 더 빨리 켜지게 만든다.
5. trigger mode와 normal mode의 size 차이를 더 벌린다.

실험 아이디어:

1. 동일 기간에 size만 절반으로 낮춘 결과를 비교한다.
2. drawdown mode에서 신규 진입을 완전히 막았을 때와 부분 허용했을 때를 비교한다.
3. 손절 거리가 넓은 거래만 따로 묶어 손익을 본다.

피해야 할 오해:

- 손실이 크다고 곧바로 신호가 나쁘다는 뜻은 아니다.
- size 문제는 좋은 전략도 쉽게 망가뜨린다.

### 4.4 과잉거래

전형적 증상:

- 거래 횟수는 많은데 계좌는 제자리거나 우하향이다.
- HOLD 비율이 낮다.
- 비슷한 fingerprint의 재진입이 자주 보인다.
- 작은 손실이 누적되고 큰 수익이 드물다.

무엇을 볼 것인가:

- `decision_breakdown`
- `recent_journal_context`
- `decision_fingerprints`
- `decision_cooldown_blocked`
- 모드별 거래 수

어떻게 해석할 것인가:

- 성과가 안 나올 때 가장 흔한 구조적 원인 중 하나다.
- LLM이 틀린 것보다 "굳이 안 해도 될 거래를 너무 많이 하는 것"일 수 있다.

바로 해볼 것:

1. HOLD 기준을 더 명확히 한다.
2. repeated fingerprint block을 강화한다.
3. decision cooldown을 늘린다.
4. `range_break_trigger_active=false`인 상태의 추격 진입을 줄인다.
5. trigger는 감시 신호로 쓰고 진입은 별도 확인 조건을 요구한다.

실험 아이디어:

1. confidence 하위 구간 거래를 제거한 가상 결과를 본다.
2. 하루 거래 수 상한을 낮춘 주와 기존 주를 비교한다.
3. trigger mode에서 신규 진입을 잠시 끄고 관리 전용으로 돌려 본다.

피해야 할 오해:

- 많은 거래가 많은 기회를 의미하지는 않는다.
- 설명이 많고 근거가 많아 보여도 기대값이 낮은 거래일 수 있다.

### 4.5 데이터 품질과 stale state 문제

전형적 증상:

- 백테스트와 회고에서는 좋아 보이는데 실전에서만 성과가 나쁘다.
- stale WS, fallback 증가, market fetch warning이 반복된다.
- indicator는 좋아 보였는데 실제 체결 구간 execution quality가 나빴다.

무엇을 볼 것인가:

- public/private WS health diagnostics
- `WebSocket Stale` / `WebSocket Recovered` alerts
- `market_data_cache` freshness
- `order_book`, `ticker`, `funding`, `open_interest` 수집 시점
- REST reconcile 빈도

어떻게 해석할 것인가:

- 실전 시점에 알 수 없는 정보가 섞였거나 freshness가 낮으면 판단 품질과 무관하게 성과가 무너질 수 있다.
- stale 상태에서 trigger 대응을 계속하면 노이즈 반응만 늘어난다.

바로 해볼 것:

1. stale 구간에서는 신규 진입을 더 엄격히 막는다.
2. REST fallback TTL과 reconcile 정책을 다시 본다.
3. WS stale 임계치를 점검해 너무 늦게 감지하지 않는지 확인한다.
4. order book 기반 지표의 freshness가 낮으면 의사결정 가중치를 낮춘다.
5. stale 상태에서 체결된 거래만 따로 분리해 성과를 본다.

실험 아이디어:

1. `WebSocket Stale` 발생 직후 10~15분 내 신규 진입을 제외한 가상 성과를 계산한다.
2. stale alerts가 없는 날과 있는 날의 손익을 분리해 본다.
3. REST fallback 비중이 높은 기간과 낮은 기간을 비교한다.

피해야 할 오해:

- 모델이 보수적으로 보여도 입력 데이터가 늦으면 이미 판단은 틀어질 수 있다.

### 4.6 평가 체계 문제

전형적 증상:

- 구간별 성과 편차가 심하다.
- 특정 기간만 좋고 최근에는 무너진다.
- 샘플 수가 적은데 rule이나 lesson을 과하게 일반화한다.
- replay는 좋아 보이는데 실거래와 연결되지 않는다.

무엇을 볼 것인가:

- `replay_runs`
- `retrieval_evaluations`
- `long_term_memory`
- `candidate_lessons`
- 최근 `24h / 7d / latest 50` semantic fallback
- in-sample vs recent out-of-sample 비교

어떻게 해석할 것인가:

- 이 경우 "모델이 좋아졌다"는 착시가 많다.
- 반드시 최근 구간, 비용 반영 결과, 실전 체결 결과를 분리해서 본다.
- memory rule은 설명력이 아니라 반복성과 holdout 적합성이 중요하다.

바로 해볼 것:

1. 손익 검토를 최근 구간 중심으로 다시 나눈다.
2. 심볼별, 레짐별 샘플 수가 충분한지 확인한다.
3. 장기 메모리 pruning 기준을 더 엄격히 한다.
4. 정책 변경은 한 번에 하나만 적용한다.

실험 아이디어:

1. 최근 2주, 최근 1달, 최근 3달을 분리해 성과를 비교한다.
2. memory rule이 적용된 거래와 적용되지 않은 거래를 따로 본다.
3. replay에서 더 보수적인 결정을 했을 때 성과가 나아지는지 본다.

피해야 할 오해:

- 좋은 회고 결과가 곧 좋은 실전 결과는 아니다.
- 샘플 수가 적은 승률은 거의 항상 과신을 유도한다.

### 4.7 운영 안정성 문제

전형적 증상:

- 의사결정은 괜찮아 보이는데 실제 체결 상태가 꼬인다.
- unknown order, timeout unresolved, recovery sweep이 잦다.
- 계좌 상태와 audit 상태가 샘플 검증에서 어긋난다.

무엇을 볼 것인가:

- `order_runtime_state`
- `order_state_transitions`
- `position_state_transitions`
- `Recovery Sweep` alerts
- `Execution Timeout` alerts
- `Trading Loop Error` alerts

어떻게 해석할 것인가:

- 이건 전략이 아니라 시스템 결함이다.
- 전략 개선보다 먼저 운영 일관성을 회복해야 한다.
- 손익 분석 전에 "실제로 무슨 포지션을 들고 있었는지"부터 맞아야 한다.

바로 해볼 것:

1. unknown order lifecycle 원인을 줄인다.
2. timeout이 난 주문의 후속 복구 흐름을 샘플링한다.
3. account state와 Bybit 실제 상태를 샘플 대조한다.
4. recovery sweep 의존도가 높으면 신규 진입 강도를 낮춘다.
5. stale/recovery 이후 즉시 거래가 재개되는 조건을 재점검한다.

실험 아이디어:

1. timeout이 발생한 날과 그렇지 않은 날의 손익을 비교한다.
2. unknown order가 있었던 거래만 따로 모아 손익을 본다.
3. recovery 이후 첫 거래들의 성과를 따로 본다.

피해야 할 오해:

- 전략이 좋아도 운영 계층이 꼬이면 PnL은 무의미해진다.

### 4.8 LLM 자체 문제

전형적 증상:

- semantic validation fallback이 급증한다.
- 같은 입력에서 decision flip이 잦다.
- JSON/schema 오류가 잦다.
- rationale evidence와 실제 source 값이 자주 불일치한다.
- baseline rule보다 명확히 나쁜 의사결정이 반복된다.

무엇을 볼 것인가:

- provider별 fallback rate
- 동일 입력 재실행 시 decision consistency
- `rationale_items.evidence`와 stored request source 값 비교
- replay에서 더 보수적인 prompt가 성과를 개선하는지 여부
- provider/model별 `HOLD` 비율과 trade frequency

어떻게 해석할 것인가:

- 구조화 출력 실패, 숫자 해석 오류, 같은 입력에서의 흔들림은 모델 문제일 가능성이 높다.
- 반대로 비용 전 손익이 약하거나 과잉거래가 핵심이면 모델 교체만으로는 해결이 잘 안 된다.

바로 해볼 것:

1. 더 강한 모델이나 더 안정적인 provider로 바꿔 본다.
2. JSON-only와 schema validation 실패율을 본다.
3. HOLD 우선 규칙과 추격 금지 규칙을 강화한다.
4. trigger mode와 normal mode에 다른 모델을 쓴다면 역할 분리가 실제로 맞는지 본다.
5. semantic validation 실패 원인을 분류해서 프롬프트, 모델, 후처리 문제를 분리한다.

실험 아이디어:

1. 같은 입력 배치에 대해 provider별 decision consistency를 비교한다.
2. `BUY/SELL`보다 `HOLD`를 더 쉽게 내는 프롬프트 버전과 비교한다.
3. reasoning 품질보다 실제 trade outcome과 alignment가 높은 모델을 고른다.

피해야 할 오해:

- "더 똑똑한 모델"이 항상 더 좋은 성과를 내지는 않는다.
- 설명력이 좋은 모델과 수익성이 좋은 모델은 다를 수 있다.

## 5. 실제 체크리스트

### 5.1 하루 안에 먼저 볼 것

1. 최근 7일 비용 전 손익과 비용 후 손익 차이를 계산한다.
2. 최근 7일 거래 횟수와 HOLD 비율을 확인한다.
3. 심볼별 손익과 레짐별 손익을 분리해서 본다.
4. 최근 24시간 `WebSocket Stale`, `Execution Timeout`, `Recovery Sweep`이 있었는지 본다.
5. `Drawdown Mode Entered`, `Cooldown Extended`, `Daily Kill Switch Activated` 발생 빈도를 본다.

### 5.2 질문으로 좁히기

질문:

- 비용 전에도 마이너스인가
- 비용 후에만 마이너스인가
- 특정 심볼만 문제인가
- 특정 시간대나 레짐에서만 문제인가
- stale/recovery 이후 거래가 유독 나쁜가
- HOLD 비율이 지나치게 낮은가
- fallback과 decision flip이 동시에 늘었는가

판단:

- 비용 전부터 약하면 전략 문제다.
- 비용 후에만 나빠지면 실행과 거래 빈도 문제다.
- 특정 심볼, 시간대, 레짐에만 몰리면 필터 문제다.
- 장애 직후 거래가 나쁘면 운영 문제다.
- consistency까지 흔들리면 모델 문제를 함께 본다.

## 6. 수익률이 안 나올 때 실제로 해볼 수 있는 것

### 6.1 엣지가 약한 것 같을 때

1. 거래 심볼을 절반으로 줄인다.
2. 최근 손실이 큰 심볼 하나를 1주일 제외한다.
3. 추세 정렬과 OI 확장이 동시에 있을 때만 신규 진입을 허용한다.
4. `distance_to_invalid_level`이 넓으면 HOLD로 강제한다.
5. range break 미확인 상태의 추격 시장가 진입을 금지한다.

### 6.2 실행 비용이 문제일 때

1. `max_order_notional_pct_balance`를 낮춘다.
2. `spread_percentile_1h` 상위 구간에서는 신규 진입을 막는다.
3. depth가 얕은 심볼이나 시간대를 제외한다.
4. 보유 시간을 늘릴 수 없는 초단기 진입을 줄인다.
5. fast move 직후 시장가 진입 대신 대기 또는 limit 위주 정책을 검토한다.

### 6.3 과잉거래가 문제일 때

1. confidence 하한을 올린다.
2. HOLD를 기본값으로 두고 진입 조건을 줄인다.
3. repeated fingerprint cooldown을 늘린다.
4. trigger는 관찰 승격 용도로만 쓰고, 실제 진입은 별도 확인 조건을 요구한다.
5. 하루 거래 수 상한을 둔다.

### 6.4 사이징이 문제일 때

1. trigger mode size를 normal보다 크게 줄인다.
2. drawdown mode 신규 진입을 더 작게 하거나 잠시 금지한다.
3. 연속 손실 시 dynamic cooldown이 더 빨리 길어지게 한다.
4. 손절 거리가 넓은 거래는 size를 추가로 감산한다.
5. 연속 승리 뒤 size 증가 같은 공격적 로직이 있으면 비활성화한다.

### 6.5 데이터 품질이 문제일 때

1. stale 상태에서 신규 진입을 더 보수적으로 막는다.
2. REST fallback 비율이 높은 날은 결과를 따로 분리해 본다.
3. order book freshness가 낮으면 execution quality 판단 가중치를 낮춘다.
4. OHLCV 업데이트 시점과 decision 시점을 다시 대조한다.
5. private WS 복구 직후 account reconcile 완료 전까지 신규 진입을 제한한다.

### 6.6 운영 안정성이 문제일 때

1. unknown order가 발생한 거래를 전부 샘플링한다.
2. timeout receipt와 실제 체결 결과를 대조한다.
3. recovery sweep 이후 첫 거래 성과를 따로 본다.
4. alert는 많지 않은데 실제 장애가 많다면 알림 임계치를 조정한다.
5. audit의 position/open order와 실제 거래소 상태를 매일 샘플 대조한다.

### 6.7 LLM 문제가 의심될 때

1. 더 강한 모델과 현재 모델을 동일 입력으로 비교한다.
2. provider별 JSON 안정성과 decision consistency를 비교한다.
3. HOLD 우선 프롬프트와 기존 프롬프트를 비교한다.
4. rationale evidence source mismatch 사례를 모아 프롬프트를 고친다.
5. trigger mode는 빠른 모델, base loop는 더 안정적인 모델처럼 역할 분리를 다시 점검한다.

## 7. 통제된 실험 순서

수익률이 안 나올 때는 변경을 한 번에 여러 개 넣지 않는 편이 맞다.

1. 현재 기준선 기간을 정한다.
2. 문제 유형 하나만 고른다.
3. 파라미터 하나만 바꾼다.
4. demo 또는 축소 size로 관찰한다.
5. 비용 전/후 손익, 거래 수, HOLD 비율, 장애 알림을 같이 본다.
6. 개선이 확인되면 다음 항목으로 넘어간다.

권장 순서:

1. size 축소
2. 거래 수 축소
3. 심볼 축소
4. trigger 조건 강화
5. stale/운영 필터 강화
6. 마지막에 모델 교체 또는 프롬프트 교체

## 8. 무엇을 모델 탓으로 봐야 하나

아래가 보이면 모델/provider 문제를 먼저 볼 수 있다.

- semantic validation fallback 급증
- 같은 입력에서 decision flip 증가
- JSON/schema 실패 증가
- rationale evidence와 실제 source 값 불일치 증가
- baseline rule보다 명확히 나쁜 의사결정

아래라면 모델 탓보다 다른 원인이 크다.

- 비용 전부터 손익이 약함
- slippage와 fee가 수익을 먹음
- 특정 심볼이나 특정 레짐만 손실 집중
- 과잉거래
- 운영 장애 다발

## 9. 권장 운영 루틴

일간:

1. `net_realized_pnl`, `estimated_pnl_pct`, `fee_cost`, `slippage_bps`를 본다.
2. `WebSocket Stale`, `Execution Timeout`, `Recovery Sweep`을 본다.
3. `Cooldown Extended`, `Drawdown Mode Entered`, `Daily Kill Switch Activated` 빈도를 본다.
4. 최근 audit 5~10개에서 evidence quality와 size cap이 이상하지 않은지 본다.

주간:

1. 심볼별 성과를 분해한다.
2. trigger mode와 normal mode를 분리해 본다.
3. replay 결과와 실제 outcome 차이를 본다.
4. 최근 long-term memory가 과잉 일반화인지 검토한다.
5. 정책 조정은 한 번에 하나만 적용한다.

월간:

1. provider/model 교체 실험 결과를 baseline과 비교한다.
2. 정책 파라미터 변경 전후를 비교한다.
3. 실제 개선이 전략인지 실행인지 운영 안정성인지 분리한다.
4. 오래된 rule과 심볼 구성을 pruning 할지 검토한다.

## 10. 메모리 개선 가능성

장기 메모리가 충분히 쌓이면 수익률이 조금 나아질 가능성은 있다.  
다만 보통은 "수익을 크게 끌어올린다"기보다 "반복 손실과 과잉거래를 줄여서 분포를 안정화한다"는 쪽에 가깝다.

기대할 수 있는 개선:

- 반복 손실 패턴을 더 빨리 회피
- 특정 레짐에서만 맞는 규칙을 더 정확히 적용
- 불확실한 구간에서 `HOLD`가 늘어 과잉거래 감소
- drawdown 구간에서 공격성 축소가 더 자연스럽게 반영

전제가 되는 조건:

- `apply_when`과 `ignore_when`이 충분히 구체적일 것
- support가 낮은 rule을 오래 유지하지 않을 것
- 최근 시장 레짐과 맞지 않는 오래된 rule을 pruning 할 것
- 설명력보다 실제 성과와의 연결을 우선해서 볼 것

나중에 측정하면 좋은 memory effectiveness KPI:

### 10.1 직접 성과 지표

- `memory_on` vs `memory_off`의 `net pnl`
- 비용 차감 후 `profit factor`
- `max drawdown`
- `avg pnl per trade`
- `expectancy per trade`

좋은 신호:

- 거래 수가 줄었는데 net pnl이 유지되거나 개선된다.
- MDD가 줄었는데 기대값이 크게 훼손되지 않는다.

### 10.2 행동 변화 지표

- `HOLD` 비율 변화
- 총 거래 수 변화
- 반복 손실 fingerprint 재진입 감소율
- drawdown 구간 신규 진입 감소율
- trigger mode 신규 진입 비중 변화

좋은 신호:

- 불필요한 진입이 줄고, 관리성 의사결정은 유지된다.

### 10.3 rule 품질 지표

- memory rule이 적용된 거래의 평균 손익
- `ignore_when`에 걸려 실제로 회피된 거래 수
- support가 높은 rule과 낮은 rule의 성과 차이
- 최근 생성 rule과 오래된 rule의 성과 차이

좋은 신호:

- support가 높고 최근성 있는 rule일수록 결과가 낫다.

### 10.4 안정성 지표

- 연속 손실 길이 감소
- `Cooldown Extended` 발생 빈도 변화
- `Drawdown Mode Entered` 빈도 변화
- `Daily Kill Switch Activated` 발생 빈도 변화

좋은 신호:

- 메모리 적용 후 계좌 손상 이벤트가 드물어진다.

### 10.5 LLM 의사결정 개선 지표

- memory 적용 후 decision flip 감소
- rationale evidence와 source 값 불일치 감소
- semantic fallback 변화
- 동일 레짐에서 decision consistency 개선

좋은 신호:

- memory가 설명만 늘리는 것이 아니라 판단을 더 안정적으로 만든다.

### 10.6 좋은 메모리 보존과 나쁜 메모리 제거

장기 메모리는 쌓는 것만큼 `보존 기준`과 `제거 기준`이 중요하다.  
개선 가능성으로는 memory rule lifecycle을 명시적으로 두는 방식이 가장 유용하다.

좋은 메모리로 간주할 조건:

- support가 충분하다.
- 최근 구간에서도 성과가 유지된다.
- `apply_when`이 맞는 구간에서 성과가 더 낫다.
- `ignore_when`이 실제로 나쁜 거래 회피에 기여한다.
- 다른 레짐으로 넘어가면 rule 영향이 자연스럽게 약해진다.

나쁜 메모리로 간주할 조건:

- support가 낮은데 표현만 강하다.
- 최근 구간에서 반복적으로 성과가 나쁘다.
- 특정 레짐에서만 맞는데 일반 규칙처럼 남아 있다.
- HOLD만 늘리고 실제 성과는 개선하지 못한다.
- rationale 설명에는 자주 등장하지만 실제 trade outcome과 연결되지 않는다.

보존 후보로 올리면 좋은 신호:

- 최근 `memory_on` 거래의 평균 손익이 `memory_off` 대비 낫다.
- 반복 손실 fingerprint 재진입을 실제로 줄였다.
- drawdown 구간에서 계좌 손상 이벤트를 줄였다.
- 오래된 rule보다 최신 rule이 더 일관된 성과를 보인다.

제거 또는 약화 후보로 보면 좋은 신호:

- 최근 N개 적용 사례에서 성과가 계속 나쁘다.
- `apply_when`과 실제 적용 구간이 자주 어긋난다.
- `ignore_when`이 거의 작동하지 않는다.
- support는 높지만 최근 시장에서는 설명력이 사라졌다.

나중에 추가하면 좋은 관리 방식:

1. `active`, `shadow`, `retired` 같은 memory 상태를 둔다.
2. 새 rule은 바로 강하게 쓰지 말고 `shadow` 상태로 관찰한다.
3. 최근 성과와 rule consistency가 확인되면 `active`로 승격한다.
4. 일정 기간 성과가 나쁘거나 레짐 부적합이 반복되면 `retired`로 내린다.
5. retired rule은 완전히 삭제하기보다 이유와 함께 보관해 재등장 여부를 본다.

권장 평가 축:

- `support_count`
- 최근 30일 적용 성과
- 최근 30일 미적용 대비 차이
- 레짐 적합도
- replay 일관성
- rule 적용 후 거래 수 변화

운영상 안전장치:

- support가 낮은 rule은 size 축소나 HOLD 보조 정도로만 사용한다.
- rule 하나가 직접 decision을 override하지 않게 한다.
- 오래된 rule은 recency decay를 준다.
- rule이 충돌하면 현재 시장 컨텍스트와 risk state를 우선한다.

권장 측정 방식:

1. 같은 기간의 decision input에 대해 `memory disabled`와 `memory enabled`를 replay로 비교한다.
2. 다른 파라미터는 바꾸지 않는다.
3. 전체 결과와 함께 `심볼별`, `trigger mode`, `drawdown 구간` 결과를 따로 본다.
4. memory 수가 늘어날수록 성과가 좋아지는지보다, `나쁜 거래를 줄였는지`를 먼저 본다.

주의할 점:

- memory가 많아졌다고 자동으로 좋아지지 않는다.
- 잘못된 일반화가 쌓이면 오히려 과보수화되거나 기회를 놓칠 수 있다.
- 따라서 memory는 "많이 저장하는 것"보다 "지금도 유효한 rule만 남기는 것"이 중요하다.

## 11. 한 줄 결론

수익률이 안 나올 때 가장 먼저 의심해야 할 것은 보통 `모델 지능 부족`이 아니라 `엣지 부족, 비용, 과잉거래, 사이징, 데이터 freshness, 운영 안정성`이다.

LLM은 잘못되면 성과를 망칠 수 있지만, 수익을 만드는 핵심은 결국 시스템 전체의 기대값 구조다.
