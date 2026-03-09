# Refactor Verification

- Standard test command: `.\.venv\Scripts\python.exe -m unittest discover -q`
- Standard environment: repo-local `.venv`
- Verification completed on 2026-03-09

## Refactor scope

- `main.py` -> facade 유지, 오케스트레이터 구현은 `orchestrator/` 아래로 분리
- `db/state_store.py` -> facade 유지, 저장/조회 구현은 `db/state_store_parts/` 아래로 분리
- `orchestrator/bootstrap.py` -> facade 유지, 설정/의존성 조립은 `bootstrap_config.py`, `bootstrap_dependencies.py`로 분리
- `orchestrator/runtime_support.py` -> facade 유지, runtime helper는 `runtime_state.py`, `cadence_policy.py`, `runtime_diagnostics.py`로 분리
- 공개 엔트리포인트와 public API 시그니처는 유지

## Decision-quality invariants

- 프롬프트와 학습 입력은 `prompt-safe` 필터를 유지한다.
- schema validation fallback, semantic validation error, system fallback 성격의 audit는 prompt 후보에서 제외한다.
- raw lookup과 prompt-safe lookup은 분리한다.
- shadow policy score는 빈 calibration summary에서 임의 보너스를 더하지 않는다.

## Bugs fixed during verification

1. `tests.test_logger.TestLogger.test_json_logger_writes_json_lines`
   - file sink UTF-8 명시와 테스트 UTF-8 읽기로 Windows codec 문제 해결
2. `tests.test_state_store.TestStateStore.test_trade_case_round_trip_and_query`
   - missing audit row를 `invalid`가 아니라 `unknown`으로 취급해 raw trade case lookup 복구
3. `tests.test_execution_policy_learning.TestExecutionPolicyLearning.test_memory_updater_evaluates_alpha_shadow_as_avoided_loss`
   - 빈 calibration summary에서 기본 bonus 제거
4. `tests.test_execution_policy_learning.TestExecutionPolicyLearning.test_memory_updater_evaluates_safety_shadow_as_avoided_loss`
   - 빈 calibration summary에서 기본 bonus 제거
5. `tests.test_state_store.TestStateStore.test_prompt_trade_case_sources_exclude_cases_linked_to_ineligible_audits`
   - vector candidate 조회가 audit linkage를 통해 prompt-safe eligibility를 확인하도록 수정

## Current status

- `.\.venv\Scripts\python.exe -m unittest discover -q`
- Result: all `272` tests passing
