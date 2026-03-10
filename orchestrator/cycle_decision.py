from ai.decision_engine import DecisionResult
from core.models import DecisionRequest, TradeIntent
from utils.logger import log


class TradingCycleDecisionMixin:
    def _build_cycle_context(self, snapshot_bundle, loop_mode: str, execution_guards: dict) -> dict:
        if snapshot_bundle is None:
            snapshot_bundle = self.snapshot_service.build_cycle_snapshot()

        account_state = snapshot_bundle.account_state
        market_snapshots = self._normalize_market_snapshots(snapshot_bundle.market_snapshots)
        market_prices = self._normalize_market_prices(snapshot_bundle.market_prices, market_snapshots)
        risk_state = self._enrich_risk_state_for_cycle(
            snapshot_bundle.risk_state,
            account_state=account_state,
            market_snapshots=market_snapshots,
            execution_guards=execution_guards,
        )
        policy_summary = snapshot_bundle.policy_summary
        is_guarded_attention = snapshot_bundle.is_guarded_attention
        risk_mode = snapshot_bundle.risk_mode
        risk_profile = snapshot_bundle.risk_profile
        regime_key = self._derive_regime_key(market_snapshots)
        risk_state_payload = self._serialize_risk_state(risk_state)
        self._maybe_alert_risk_state_transitions(risk_state)
        self._maybe_alert_websocket_transitions()
        effective_risk_profile = self._build_effective_risk_profile(
            loop_mode,
            account_state=account_state,
            risk_profile=risk_profile,
            risk_mode=risk_mode,
            regime_key=regime_key,
        )
        decision_market_snapshots, alpha_policy_summary = self._prepare_decision_market_snapshots(
            account_state=account_state,
            market_snapshots=market_snapshots,
            loop_mode=loop_mode,
            regime_key=regime_key,
        )
        safety_state = self.safety_policy_engine.get_emergency_state(
            account_state=account_state,
            market_snapshots=market_snapshots,
            regime_key=regime_key,
        )
        return {
            "account_state": account_state,
            "market_snapshots": market_snapshots,
            "market_prices": market_prices,
            "risk_state": risk_state,
            "policy_summary": policy_summary,
            "is_guarded_attention": is_guarded_attention,
            "is_safety_mode": bool(safety_state.get("active")),
            "safety_state": safety_state,
            "risk_mode": risk_mode,
            "risk_profile": risk_profile,
            "regime_key": regime_key,
            "risk_state_payload": risk_state_payload,
            "effective_risk_profile": effective_risk_profile,
            "entry_headroom": self._build_entry_headroom_summary(
                account_state=account_state,
                market_prices=market_prices,
                risk_profile=effective_risk_profile,
            ),
            "decision_market_snapshots": decision_market_snapshots,
            "alpha_policy_summary": alpha_policy_summary,
            "loop_mode": loop_mode,
        }

    def _build_cycle_decision_request(
        self,
        *,
        cycle_context: dict,
        risk_state_override,
        recent_journal_context,
        long_term_memory,
        similar_past_cases,
    ) -> DecisionRequest:
        decision_market_snapshots = cycle_context["decision_market_snapshots"]
        return DecisionRequest(
            policy_summary=cycle_context["policy_summary"],
            account_state=cycle_context["account_state"],
            market_snapshot=(
                next(iter(decision_market_snapshots.values()))
                if len(decision_market_snapshots) == 1
                else None
            ),
            market_snapshots=decision_market_snapshots,
            risk_state=risk_state_override,
            entry_headroom=cycle_context["entry_headroom"],
            recent_journal_context=recent_journal_context,
            long_term_memory=long_term_memory,
            similar_past_cases=similar_past_cases,
            is_guarded_attention=cycle_context["is_guarded_attention"],
            risk_mode=cycle_context["risk_mode"],
            risk_profile=cycle_context["effective_risk_profile"],
            loop_mode=cycle_context["loop_mode"],
            cycle_role=cycle_context["effective_risk_profile"].get("cycle_role"),
        )

    def _resolve_cycle_decision(self, cycle_context: dict) -> dict:
        account_state = cycle_context["account_state"]
        market_snapshots = cycle_context["market_snapshots"]
        risk_state = cycle_context["risk_state"]
        risk_mode = cycle_context["risk_mode"]
        loop_mode = cycle_context["loop_mode"]

        skip_reason = self._get_deterministic_skip_reason(
            account_state=account_state,
            market_snapshots=market_snapshots,
            risk_state=risk_state,
            risk_mode=risk_mode,
            risk_profile=cycle_context["effective_risk_profile"],
        )
        if (
            not skip_reason
            and loop_mode == "1h"
            and not self._has_live_positions(account_state)
            and not self._has_live_open_orders(account_state)
            and not cycle_context["decision_market_snapshots"]
        ):
            skip_reason = "alpha_policy_filtered_all"

        decision_risk_state_payload = {
            **cycle_context["risk_state_payload"],
            "alpha_policy_blocked_symbols": (
                cycle_context["alpha_policy_summary"].get("blocked_symbols") or {}
            ),
        }
        cycle_context["decision_risk_state_payload"] = decision_risk_state_payload

        request = None
        position_policy_result = None
        safety_policy_result = None
        if loop_mode == "15m" and (
            self._has_live_positions(account_state) or self._has_live_open_orders(account_state)
        ):
            position_policy_result = self.position_policy_engine.evaluate(
                account_state=account_state,
                market_snapshots=market_snapshots,
                risk_mode=risk_mode,
                loop_mode=loop_mode,
                regime_key=cycle_context["regime_key"],
            )
        if loop_mode == "5m":
            safety_policy_result = self.safety_policy_engine.evaluate(
                account_state=account_state,
                market_snapshots=market_snapshots,
                risk_mode=risk_mode,
                loop_mode=loop_mode,
                regime_key=cycle_context["regime_key"],
            )

        if safety_policy_result and not safety_policy_result.get("escalate_to_ai"):
            request = self._build_cycle_decision_request(
                cycle_context=cycle_context,
                risk_state_override=risk_state,
                recent_journal_context=[],
                long_term_memory=[],
                similar_past_cases=[],
            )
            input_hash = self.ai_engine._build_candidate_input_hash(request)
            decision_result = self._build_policy_decision_result(
                decision_payload=safety_policy_result.get("decision_result") or {},
                input_hash=input_hash,
                model_name="safety_fsm",
            )
        elif position_policy_result and not position_policy_result.get("escalate_to_ai"):
            request = self._build_cycle_decision_request(
                cycle_context=cycle_context,
                risk_state_override=risk_state,
                recent_journal_context=[],
                long_term_memory=[],
                similar_past_cases=[],
            )
            input_hash = self.ai_engine._build_candidate_input_hash(request)
            decision_result = self._build_policy_decision_result(
                decision_payload=position_policy_result.get("decision_result") or {},
                input_hash=input_hash,
                model_name="position_fsm",
            )
        elif skip_reason:
            request = self._build_cycle_decision_request(
                cycle_context=cycle_context,
                risk_state_override=decision_risk_state_payload,
                recent_journal_context=[],
                long_term_memory=[],
                similar_past_cases=[],
            )
            input_hash = self.ai_engine._build_candidate_input_hash(request)
            log.info(f"Skipping AI decision call: {skip_reason}")
            rationale_items = self._build_skip_rationale_items(
                skip_reason=skip_reason,
                risk_state=decision_risk_state_payload,
                market_snapshots=market_snapshots,
                risk_profile=cycle_context["risk_profile"],
            )
            decision_result = DecisionResult(
                intent=TradeIntent(
                    decision="HOLD",
                    orders=[],
                    rationale=[f"Deterministic HOLD: {skip_reason}."],
                    rationale_items=rationale_items,
                    risk_notes=[skip_reason],
                    confidence=1.0,
                ),
                prompt="",
                raw_response="",
                model_name="deterministic_skip",
                input_hash=input_hash,
                cache_hit=False,
                usage_metadata={"skipped": True, "reason": skip_reason},
            )
        else:
            context_budget = self._build_context_budget()
            raw_recent_journal_context = self.state_store.get_recent_journal_context(
                limit=context_budget["journal_fetch_limit"],
                candidate_limit=max(
                    context_budget["journal_fetch_limit"] * 4,
                    context_budget["journal_keep_limit"] * 10,
                ),
                anchor_limit=max(
                    context_budget.get("journal_anchor_limit", 0),
                    context_budget["journal_keep_limit"] * 4,
                ),
            )
            raw_recent_trade_cases = self.state_store.get_recent_trade_cases(
                limit=max(
                    context_budget["journal_fetch_limit"],
                    context_budget["journal_keep_limit"] * 4,
                )
            )
            raw_long_term_memory = self.state_store.get_long_term_memory()
            recent_journal_context = self._select_recent_journal_context(
                raw_recent_journal_context,
                market_snapshots=cycle_context["decision_market_snapshots"],
                risk_mode=risk_mode,
                limit=context_budget["journal_keep_limit"],
                max_rationale_items=context_budget["max_rationale_items_per_journal"],
                max_evidence_per_item=context_budget["max_evidence_per_rationale_item"],
                trade_cases=raw_recent_trade_cases,
                account_state=account_state,
                risk_state=risk_state,
            )
            long_term_memory = self._select_long_term_memory_context(
                raw_long_term_memory,
                market_snapshots=cycle_context["decision_market_snapshots"],
                risk_mode=risk_mode,
                limit=context_budget["memory_keep_limit"],
                max_ignore_when=context_budget["max_ignore_when_per_memory"],
            )
            similar_past_cases = self._build_similar_past_cases(
                cycle_context["decision_market_snapshots"],
                account_state=account_state,
                risk_state=risk_state,
                is_guarded_attention=cycle_context["is_guarded_attention"],
                limit=(
                    context_budget["similar_case_keep_limit"]
                    if cycle_context["is_guarded_attention"]
                    else context_budget.get("similar_case_normal_keep_limit", 2)
                ),
            )
            log.info(
                "Context budget applied: journals {}->{}, trade_cases={}, memory {}->{}, similar_cases={}",
                len(raw_recent_journal_context),
                len(recent_journal_context),
                len(raw_recent_trade_cases),
                len(raw_long_term_memory),
                len(long_term_memory),
                len(similar_past_cases),
            )
            request = self._build_cycle_decision_request(
                cycle_context=cycle_context,
                risk_state_override=risk_state,
                recent_journal_context=recent_journal_context,
                long_term_memory=long_term_memory,
                similar_past_cases=similar_past_cases,
            )
            decision_result = self.ai_engine.generate_decision(request)

        intent = self._enrich_intent_metadata(
            decision_result.intent,
            input_hash=decision_result.input_hash,
            loop_mode=loop_mode,
            risk_mode=risk_mode,
            market_snapshots=market_snapshots,
        )
        intent, execution_policy_summary = self.execution_policy_engine.apply_to_intent(
            intent=intent,
            market_snapshots=market_snapshots,
            account_state=account_state,
            loop_mode=loop_mode,
            risk_mode=risk_mode,
            is_guarded_attention=cycle_context["is_guarded_attention"],
        )
        return {
            "request": request,
            "decision_result": decision_result,
            "intent": intent,
            "execution_policy_summary": execution_policy_summary,
        }
