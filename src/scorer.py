"""
src/scorer.py
─────────────
Deterministic adapter around utils.classification_rules.

The shared rules module is the single source of truth for tax-category
decisions. This file keeps the historical ScoreResult shape used by the
processor, evaluator, and training-data logger.
"""

from dataclasses import dataclass, field

import pandas as pd

from src.parser import parse_transaction_type
from utils.constants import (
    CATEGORY_GST, CATEGORY_NORMAL, CATEGORY_TDS, CATEGORY_POSSIBLE_GST,
    INTERNAL_CREDIT_COL,
    INTERNAL_DEBIT_COL, INTERNAL_DESCRIPTION_COL,
    SCORE_GST_KEYWORD, SCORE_MEDIUM_THRESHOLD, SCORE_TDS_KEYWORD,
)
from utils.classification_rules import decide_classification
from utils.helpers import normalize_text

# Classification rules live in utils.classification_rules and are intentionally
# shared by uploads, cache restores, MongoDB history reads, and exports.


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class ScoreResult:
    tds_score:    int  = 0
    gst_score:    int  = 0
    category:     str  = CATEGORY_NORMAL
    confidence:   str  = "HIGH"
    classification_mode: str = "HEURISTIC"
    needs_review: bool = False
    reason:       str  = ""          # ← human-readable explanation
    vendor:       str  = ""
    transaction_type: str = ""
    normalized_text: str = ""
    ml_assist_score: float = 0.0
    ml_model_confidence: float = 0.0
    ml_assist_used: bool = False
    ml_uncertain: bool = False
    ambiguous_semantics: bool = False
    deterministic_normal: bool = False
    explicit_rule: bool = False
    flow_type:    str  = "UNKNOWN"
    flow_confidence: str = "LOW"
    flow_reason:  str  = ""
    is_commercial_flow: bool = False
    debug_parts:  list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "TAX_CATEGORY":       self.category,
            "CONFIDENCE":         self.confidence,
            "REVIEW_RECOMMENDED": self.needs_review,
            "ML_ASSIST":          f"{self.ml_model_confidence:.2%}" if self.ml_assist_used else "N/A",
            "REASON":             self.reason,
        }


# ── Main scoring function ─────────────────────────────────────────────────────

def score_transaction(row: pd.Series) -> ScoreResult:
    """
    Score a single transaction row and return a ScoreResult.
    Reads internal alias columns (_description, _debit, _credit, _date).
    """
    result = ScoreResult()

    # ── Extract values ────────────────────────────────────────────────────────
    text   = normalize_text(row.get(INTERNAL_DESCRIPTION_COL, ""))
    debit  = _safe_float(row.get(INTERNAL_DEBIT_COL))
    credit = _safe_float(row.get(INTERNAL_CREDIT_COL))
    result.normalized_text = text

    # ── Transaction type ──────────────────────────────────────────────────────
    tx_type = parse_transaction_type(text)
    result.transaction_type = tx_type.name if hasattr(tx_type, 'name') else str(tx_type)

    # Deterministic priority classifier. This is the single tax-category
    # decision path used by uploads, filters, exports, and history restores.
    return _priority_classify(result, text, debit, credit, tx_type)


# ── Deterministic priority classifier ─────────────────────────────────────────

def _priority_classify(
    result: ScoreResult,
    text: str,
    debit: float,
    credit: float,
    tx_type,
) -> ScoreResult:
    decision = decide_classification(text, debit, credit)

    return _set_priority_result(
        result,
        category=decision.category,
        confidence=decision.confidence,
        needs_review=decision.review_recommended,
        mode="EXPLICIT" if decision.category in (CATEGORY_GST, CATEGORY_TDS) else "RULE_PRIORITY",
        reason=decision.reason,
        gst_score=SCORE_GST_KEYWORD if decision.category == CATEGORY_GST else (
            SCORE_MEDIUM_THRESHOLD if decision.category == CATEGORY_POSSIBLE_GST else 0
        ),
        tds_score=SCORE_TDS_KEYWORD if decision.category == CATEGORY_TDS else 0,
        deterministic_normal=decision.category == CATEGORY_NORMAL,
        ambiguous=decision.category == CATEGORY_POSSIBLE_GST,
    )


def _set_priority_result(
    result: ScoreResult,
    *,
    category: str,
    confidence: str,
    needs_review: bool,
    mode: str,
    reason: str,
    gst_score: int = 0,
    tds_score: int = 0,
    deterministic_normal: bool = False,
    ambiguous: bool = False,
) -> ScoreResult:
    result.category = category
    result.confidence = confidence
    result.needs_review = needs_review
    result.classification_mode = mode
    result.explicit_rule = category in (CATEGORY_GST, CATEGORY_TDS)
    result.deterministic_normal = deterministic_normal
    result.ambiguous_semantics = ambiguous
    result.gst_score = gst_score
    result.tds_score = tds_score
    result.ml_assist_used = False
    result.ml_model_confidence = 0.0
    result.ml_uncertain = False
    result.reason = reason
    result.debug_parts[:] = [reason]
    return result


# ── Private helpers ───────────────────────────────────────────────────────────

def _safe_float(val) -> float:
    """
    Convert val to float, returning 0.0 for None / NaN / unconvertible values.

    Handles:
    - Indian comma-formatted amounts: '1,00,000.00'
    - Multiline Excel cells: '1,00,000.\\n00' (ICICI bank format)
    - Regular numeric strings: '35000', '2950.50'
    """
    if val is None:
        return 0.0
    try:
        v = float(val)
        return 0.0 if v != v else v   # v != v is True only for NaN
    except (TypeError, ValueError):
        pass
    # String cleaning: strip whitespace (including \n), remove Indian-style commas
    try:
        cleaned = str(val).replace("\n", "").replace("\r", "").replace(",", "").strip()
        v = float(cleaned)
        return 0.0 if v != v else v
    except (TypeError, ValueError):
        return 0.0
