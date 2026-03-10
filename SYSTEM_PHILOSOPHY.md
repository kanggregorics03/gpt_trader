# System Philosophy

이 문서는 이 프로젝트가 무엇을 하려는 시스템인지, 어떤 편향은 의도적으로 유지하고 어떤 편향은 제거해야 하는지, 그리고 프롬프트/학습/승격 구조가 어떤 철학 위에서 돌아가는지를 짧게 정리한다.

## 1. 이 시스템의 정체

이 프로젝트는 중립적인 가격 예측기가 아니다.

- 리스크 제약 안에서 행동하는 단일 심볼 트레이딩 시스템이다.
- LLM은 주문 실행 권한을 가진 주체가 아니라, 구조화된 추천을 만드는 decision layer다.
- 실제 시스템 학습은 모델 파인튜닝보다 `retrieval + memory + replay + promotion` 구조를 통해 이뤄진다.

즉 목표는 "가장 공격적으로 많이 맞히는 모델"이 아니라, "제약을 지키면서 반복 가능한 의사결정을 개선하는 운영 시스템"이다.

## 2. 유지해야 하는 편향

모든 편향을 없애는 것이 목표가 아니다. 아래 비대칭은 의도된 편향이다.

- 손실 확대보다 기회 상실을 덜 치명적으로 본다.
- drawdown, cooldown, repeated failure 구간에서는 보수적으로 기운다.
- 신규 진입보다 축소/청산/위험 완화를 더 쉽게 허용한다.
- 리스크와 실행 품질이 나쁠 때는 "좋은 진입 아이디어"보다 "잘못된 진입 회피"를 우선한다.

이 편향은 시스템 철학의 일부이며 제거 대상이 아니다.

### 2.1 루프 역할 계약도 편향의 일부다

이 시스템은 모든 cadence가 같은 수준의 판단 권한을 갖는 구조가 아니다.

- `1h`는 기본적으로 신규 thesis를 만드는 alpha loop다.
- 다만 live position이 남아 있으면 `1h`도 새 thesis를 덮어쓰지 않고 기존 케이스 관리만 한다.
- `15m`는 thesis-aware management loop다.
- `15m`는 작은 음수 손익만으로 바로 줄이지 않고, invalidation 근접이나 time-stop 같은 관리 사유가 있을 때만 청산한다.
- `5m`는 execution safety loop다.
- `5m`는 profit protect나 일반 관리가 아니라 acute loss, stale execution failure, confirmed liquidity deterioration 같은 emergency path만 deterministic하게 처리한다.

즉 최근 수정에서 `15m`를 thesis 근접 기반으로 더 묶고, `5m`를 응급 전용으로 줄인 방향은 시스템 철학과 일치한다.

## 3. 제거해야 하는 편향

아래는 철학이 아니라 구현 우연으로 생기는 편향이라 줄여야 한다.

- action 사례만 retrieval 상단에 몰리는 편향
- 시스템 오류성 fallback HOLD가 실제 시장 판단 HOLD처럼 섞이는 편향
- recent cap 때문에 오래됐지만 중요한 memory/rule이 사라지는 편향
- raw policy dump 길이와 문서 서술이 prompt 우선순위를 왜곡하는 편향
- 배치가 멈췄는데도 예전 학습 artifact를 계속 신뢰하는 편향

## 4. 프롬프트 철학

프롬프트는 "많이 넣는 것"보다 "binding 정보만 정확하게 넣는 것"이 중요하다.

- `policy_summary`는 원문 정책 파일이 아니라 compact digest여야 한다.
- digest에는 반드시 들어가야 하는 정책 경로가 있고, 이것은 코드 계약으로 강제한다.
- recent journal, memory, similar past cases는 전부 샘플링된 보조 근거일 뿐이다.
- system/validation fallback 같은 오류성 retrieval은 prompt 컨텍스트 후보에서 기본 제외해야 한다.
- live market snapshot, risk state, enforceable policy limit가 과거 사례보다 우선이다.

요약하면 prompt는 설명 문서가 아니라 compact decision contract여야 한다.

### 4.1 Raw 조회와 Prompt-safe 조회는 분리한다

운영 시스템에는 두 종류의 조회가 있다.

- raw 저장 사실을 그대로 보여주는 조회
- 프롬프트, retrieval, 학습 입력으로 들어갈 수 있는 prompt-safe 조회

여기서 중요한 원칙은 다음과 같다.

- schema validation fallback, semantic validation error, system fallback 성격의 audit는 prompt-safe 후보에서 제외한다.
- audit row가 없다는 이유만으로 raw trade case를 숨기지는 않는다.
- 반대로 prompt-safe 후보는 audit linkage를 통해 실제로 걸러져야 한다.

즉 "보여줄 수 있는가"와 "프롬프트에 넣어도 되는가"는 같은 질문이 아니다.

### 4.2 구조 리팩토링은 Decision Contract를 바꾸지 않는다

리팩토링의 목적은 책임 경계를 분해하고 테스트 가능성을 높이는 것이지, 의사결정 의미를 바꾸는 것이 아니다.

- 공개 엔트리포인트와 policy contract는 유지한다.
- prompt-safe 필터, risk policy, execution policy, shadow evaluation 의미는 유지한다.
- 동작 변경이 필요하면 구조 개선과 분리해서 명시적 버그 수정으로 다룬다.

즉 구조 변경은 decision quality를 바꾸지 않아야 하고, 품질 변화는 의도된 규칙 수정으로만 일어나야 한다.

## 5. 학습 철학

이 시스템의 학습은 크게 네 층이다.

1. `audit_logs`, `journal`, `trade_cases`, `replay_runs`가 운영 사실을 축적한다.
2. `long_term_memory`와 retrieval artifact가 운영 결과를 바탕으로 갱신된다.
3. candidate policy / bundle / shadow decision이 실제 champion 옆에서 평가된다.
4. holdout, replay, liveness 조건을 통과한 경우에만 promotion이 일어난다.

즉 "좋은 답변을 한 번 받는 것"이 아니라 "운영 결과를 반영해 보조 구조를 천천히 진화시키는 것"이 학습이다.

여기서 중요한 구분이 하나 더 있다.

- `candidate policy / promotion`은 실행 규칙을 직접 바꾸는 경로다.
- `long_term_memory`는 실행 규칙을 직접 바꾸지 않고, 이후 판단과 lesson synthesis의 보조 근거를 천천히 바꾸는 경로다.

따라서 `15m/5m` 규칙을 수정하면 정책 진화 계층은 즉시 영향을 받고,
장기 메모리 계층은 새 사례 분포가 누적되면서 간접적으로 따라온다.

운영 계약이 크게 바뀌는 cutover도 같은 원칙으로 다룬다.

- `audit_logs`, `journal`, `trade_cases`, `replay_runs`, `shadow_decisions` 같은 사실 이력은 지우지 않는다.
- 대신 champion/candidate/memory 같은 학습 표면만 migrate 또는 retire 한다.
- `2026-03-10` management contract cutover 이후 batch learning은 기본적으로 해당 날짜 이후 case만 promotion 근거로 사용한다.

여기서 replay 자동화는 균등 샘플링이 아니라 운영적으로 의미 있는 audit를 더 자주 본다는 원칙을 따른다.

- 큰 손실 audit는 더 높은 우선순위를 갖는다.
- retrieval이 실제 decision 차이를 만든 audit는 다시 보기 가치가 높다.
- `HOLD`도 missed opportunity를 보기 위해 별도 quota로 유지한다.
- 하지만 system/validation fallback audit는 여전히 자동 학습 경로에서 제외한다.

## 6. Shadow Model 원칙

실거래 decision model을 바로 fine-tune 하지 않는다.

- 먼저 high-quality dataset을 만든다.
- shadow dataset과 holdout 요약을 저장한다.
- retrieval reranker, calibrator, shadow policy 같은 주변부부터 학습시킨다.
- 실전 champion을 교체하기 전에 shadow와 replay에서 먼저 본다.

이 원칙은 drift와 설명 불가능성을 줄이기 위한 것이다.

## 7. Learning Liveness 원칙

학습 구조가 코드에 존재하는 것만으로는 충분하지 않다. 실제로 돌고 있어야 한다.

그래서 운영에서는 아래를 계속 본다.

- 마지막 replay batch 시각
- 마지막 memory update 시각
- 최근 replay candidate 처리율
- reranker artifact 준비 여부
- shadow dataset 준비 여부
- promotion blocked 여부

좋은 liveness는 단순 "replay run 개수"가 아니라, 중요한 후보군을 계속 커버하고 있는 상태를 뜻한다.
그래서 replay batch는 후보량이 많아지면 처리 수를 가변적으로 늘리고, `HOLD`도 별도 quota로 보장한다.

이 값이 stale이면 자동 승격은 멈춰야 한다. stale 상태에서의 promotion은 "학습된 시스템"이 아니라 "학습이 멈춘 시스템의 착시"다.

## 8. 좋은 상태의 정의

이 프로젝트가 건강하게 돌고 있다는 뜻은 아래가 동시에 성립하는 상태다.

- prompt가 compact contract를 유지한다.
- retrieval이 최근성만이 아니라 반례와 hold 품질을 함께 본다.
- memory가 lifecycle 기준으로 살아 있고 recent cap에 묻히지 않는다.
- replay와 batch가 꾸준히 돈다.
- stale learning이면 promotion이 자동 차단된다.
- 성과 평가는 단순 PnL이 아니라 `hold_opportunity_cost`, `false_entry_cost`, `confidence_alignment`, `retrieval_counterfactual`까지 같이 본다.

## 9. 관련 파일

- `core/risk_engine.py`
- `ai/decision_engine.py`
- `main.py`
- `db/state_store.py`
- `orchestrator/`
- `db/state_store_parts/`
- `batch/memory_updater.py`
- `tools/replay_audit.py`
- `dashboard/app.py`
- `README.md`
- `TRADING_SYSTEM_DESIGN.md`
- `PROFITABILITY_TROUBLESHOOTING.md`
