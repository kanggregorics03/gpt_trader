import sqlite3
import json
import math
from typing import List, Dict
from loguru import logger

class RagEngine:
    def __init__(self, db_path: str = "data/gpt_trader.db"):
        self.db_path = db_path

    def _cosine_similarity(self, v1: List[float], v2: List[float]) -> float:
        dot_product = sum(a * b for a, b in zip(v1, v2))
        magnitude_v1 = math.sqrt(sum(a * a for a in v1))
        magnitude_v2 = math.sqrt(sum(a * a for a in v2))
        if magnitude_v1 == 0 or magnitude_v2 == 0:
            return 0.0
        return dot_product / (magnitude_v1 * magnitude_v2)

    def _extract_vector(self, market_state: Dict) -> List[float]:
        # Simple feature vector from market snapshot
        # [atr_15m, atr_1h] (scaled roughly logic could be added)
        try:
            atr_15m = float(market_state.get('atr_15m', 0.0))
            atr_1h = float(market_state.get('atr_1h', 0.0))
            # encode trend mapping
            trend = market_state.get('trend_1h', 'FLAT')
            trend_val = 1.0 if trend == 'UP' else (-1.0 if trend == 'DOWN' else 0.0)
            
            surge = 1.0 if market_state.get('recent_volume_surge') else 0.0
            
            return [atr_15m, atr_1h, trend_val, surge]
        except Exception:
            return [0.0, 0.0, 0.0, 0.0]

    def get_similar_cases(self, current_market: Dict, limit: int = 3) -> List[str]:
        """
        Retrieves top N past cases from the audit log that had similar market conditions.
        Returns formatted strings for the Gemini prompt.
        """
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # Fetch all past logs (In real production, limit to last 1000 or use proper Vector DB)
            cursor.execute("SELECT id, market_state, intent, risk_approved FROM audit_logs ORDER BY id DESC LIMIT 500")
            rows = cursor.fetchall()
            
            current_vec = self._extract_vector(current_market)
            if sum(current_vec) == 0:
                return [] # Not enough features
                
            scored_cases = []
            for row in rows:
                ms_json = row['market_state']
                if not ms_json:
                    continue
                past_market = json.loads(ms_json)
                past_vec = self._extract_vector(past_market)
                
                sim = self._cosine_similarity(current_vec, past_vec)
                scored_cases.append((sim, row))
                
            # Sort by similarity desc
            scored_cases.sort(key=lambda x: x[0], reverse=True)
            
            top_cases = []
            for sim, row in scored_cases[:limit]:
                # Format the case representation
                intent_json = json.loads(row['intent'])
                decision = intent_json.get('decision', 'UNKNOWN')
                rationale = intent_json.get('rationale', [])
                rationale_str = rationale[0] if rationale else 'No specific rationale'
                case_str = f"Past Similarity {sim:.2f}: We decided {decision}. Rationale: {rationale_str}. Risk Approved: {row['risk_approved']}"
                top_cases.append(case_str)
                
            return top_cases
            
        except Exception as e:
            logger.error(f"RAG Engine failed to get similar cases: {e}")
            return []
        finally:
            conn.close()
