# Improvement Roadmap

이 문서는 "지금 당장 쓸 만한가"와 "아직 어디가 미완성인가"를 구분해서 적는다.

현재 상태는 demo 또는 very small size 운영에는 충분히 근접했지만, 아무 경계 없이 live full-size로 신뢰할 단계는 아니다. 아래 항목이 남아 있다.

## 1. 현재 한계

### 1.1 Learned reranker는 아직 artifact 기반 보정이다

현재 recent journal / similar case retrieval은 batch가 만든 profile artifact를 읽어 런타임 점수를 보정한다.

이건 static heuristic-only보다는 낫지만, 아직은 아래 의미다.

- 독립된 model serving이 없다.
- feature engineering이 규칙 기반이다.
- online calibration이 없다.
- artifact 품질이 batch 산출물 품질에 직접 의존한다.

즉 "learned reranker"의 초기 단계이지, 완전한 retrieval model은 아니다.

### 1.2 Shadow dataset은 생기지만 trainer는 아직 없다

현재는 `shadow_decision_dataset.jsonl`과 holdout summary를 저장한다.

하지만 아직 아래는 구현되지 않았다.

- dataset versioning 정책
- trainer pipeline
- validation/benchmark job
- model registry
- shadow model serving
- offline experiment acceptance gate

즉 학습 재료는 쌓이지만, 그 재료를 모델 실험으로 닫는 자동 파이프라인은 아직 없다.

### 1.3 최종 검증은 여전히 최근 outcome 의존적이다

구조가 좋아졌다고 해서 성과가 자동으로 보장되지는 않는다.

결국 아래 지표가 최근 구간에서 좋아져야 한다.

- `hold_opportunity_cost`
- `false_entry_cost`
- `confidence_alignment_score`
- `retrieval_counterfactual`
- 실제 realized pnl과 execution quality

즉 코드 완성도보다 최근 운영 결과가 더 중요하다.

### 1.4 Learning liveness는 감시하지만, 운영 자동 복구는 제한적이다

지금은 stale learning을 감지하고 offline artifact 신뢰도를 낮출 수 있다.

하지만 아직 아래는 약하다.

- 왜 stale이 됐는지 root-cause 자동 분류
- replay/memory batch 자동 재기동
- artifact corruption 검출
- 오래된 artifact 대신 last-known-good runtime config를 되돌리는 자동 보조

즉 "멈췄음을 안다"까지는 왔지만, "스스로 복구한다"까지는 아니다.

### 1.5 Live 검증은 아직 보수적으로 접근해야 한다

현재 구조는 운영형에 가까워졌지만, live full-size 전에 더 봐야 한다.

- 최근 2주~4주의 stability
- 레짐 전환 구간 성능
- stale learning 없는 연속 운영 기간
- replay와 actual outcome의 괴리
- drawdown 시 recovery quality

즉 지금 단계의 권장 사용 범위는 demo 또는 very small size다.

## 2. 개선 방향

### 2.1 Retrieval model 고도화

다음 단계는 artifact score 보정에서 한 단계 더 가는 것이다.

- labeled retrieval dataset export 고도화
- feature schema 고정
- lightweight ranker 학습
- offline benchmark
- shadow reranker serving
- 기존 heuristic 대비 A/B 평가

우선순위가 높다.

### 2.2 Shadow decision 학습 파이프라인 추가

현재 dataset export 다음에 필요한 건 trainer다.

- dataset version snapshot
- train/validation/holdout split 고정
- calibration score 기록
- shadow model registry
- replay 기반 acceptance gate

이 단계가 있어야 distillation 구조가 실체를 가진다.

### 2.3 운영 복구 자동화

Learning liveness 이후 다음 단계는 self-healing이다.

- replay batch stale alert
- memory updater stale alert
- artifact freshness SLO
- offline evaluation blocked 사유 기록
- batch 재시도 또는 운영 경보 자동화

운영 품질 측면에서 중요하다.

### 2.4 Live rollout discipline 강화

구조가 좋아져도 rollout discipline이 약하면 성과가 흔들린다.

- demo
- very small size
- limited live window
- post-change holdout review
- runtime config/policy rollback drill

즉 기술 개선과 배포 절차를 같이 가져가야 한다.

## 3. 권장 우선순위

1. retrieval ranker trainer 추가
2. shadow decision trainer 추가
3. liveness alert + batch recovery 자동화
4. live rollout 체크리스트와 rollback drill 표준화

## 4. 지금 시점의 현실적 평가

현재 시스템은 다음 수준으로 보는 것이 맞다.

- 프롬프트 철학 정렬: 꽤 잘 됨
- retrieval 편향 완화: 실전형 초기 단계
- memory lifecycle: 운영 가능 수준
- 학습 루프: 구조는 있음
- autonomous learning system: 아직 아님
- full-size live confidence: 아직 이르다

즉 "쓸 만하다"는 평가는 가능하지만, "완성됐다"는 평가는 아직 아니다.
