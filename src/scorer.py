"""
src/scorer.py
─────────────
Multi-signal scoring engine for transaction classification.

Architecture (v3):
  1. NORMAL_OVERRIDE_KEYWORDS  — hard utility/statutory bypass before scoring
  2. Vendor override CSV       — loaded once at startup; HARD/SOFT precedence
  3. Salary credit override    — hard override for incoming salary credits
  4. ATM override              — always NORMAL
  5. TDS scoring               — keywords, section codes, BLKNEFT, quarter-end
  6. GST scoring               — keywords, GSTIN, merchant, CMS/card, UPI, non-round
  7. Negative penalties        — soft subtraction from both scores
  8. Company suffix penalties  — TDS-only subtraction
  9. Direction penalty         — incoming credits penalised
 10. Flow classification       — behavior only: CONSUMER/BUSINESS/etc.
 11. Tax category decision     — GST / POSSIBLE_GST / TDS / NORMAL
 12. Reason string             — human-readable explanation in every result
"""

import csv
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from src.flow_classifier import (
    FLOW_BUSINESS,
    FLOW_CONSUMER,
    FLOW_SETTLEMENT,
    FLOW_SUBSCRIPTION,
    FLOW_TAX,
    FLOW_TRANSFER,
    classify_flow,
)
from src.parser import TxType, parse_transaction_type
from utils.constants import (
    AMOUNT_FLAG_ABOVE, AMOUNT_IGNORE_BELOW,
    CATEGORY_GST, CATEGORY_NORMAL, CATEGORY_TDS, CATEGORY_POSSIBLE_GST,
    COMPANY_SUFFIX_PENALTIES,
    GST_HIGH_CONFIDENCE_PATTERNS, GST_KEYWORDS, GST_WEAK_HINTS, MERCHANT_KEYWORDS,
    NEGATIVE_KEYWORDS, SERVICE_VENDOR_KEYWORDS,
    NORMAL_OVERRIDE_KEYWORDS,
    INTERNAL_CREDIT_COL, INTERNAL_DATE_COL,
    INTERNAL_DEBIT_COL, INTERNAL_DESCRIPTION_COL,
    PENALTY_INCOMING_GST, PENALTY_INCOMING_TDS,
    SCORE_CLOSE_CALL_MARGIN, SCORE_GST_CMS_CARDPMT, SCORE_GST_GATEWAY,
    SCORE_GST_GSTIN_PATTERN, SCORE_GST_KEYWORD, SCORE_GST_WEAK_HINT, SCORE_GST_NONROUND_AMT,
    SCORE_GST_UPI_DEBIT,
    SCORE_HIGH_THRESHOLD, SCORE_MEDIUM_THRESHOLD,
    SCORE_TDS_KEYWORD, SCORE_TDS_QUARTER_END,
    SCORE_TDS_SECTION_CODE, SCORE_TDS_TXTYPE_BLKNEFT,
    SCORE_UNCERTAIN_CUTOFF, TDS_KEYWORDS, TDS_SECTION_CODES,
)
from utils.helpers import normalize_text

logger = logging.getLogger(__name__)

# ── Precompiled regex patterns ────────────────────────────────────────────────

_RE_TDS_KEYWORDS: list[tuple[str, re.Pattern]] = [
    (kw, re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE))
    for kw in TDS_KEYWORDS
]

_RE_SECTION_WITH_LETTER: list[re.Pattern] = [
    re.compile(rf"\b{code}[A-Z]{{1,3}}\b", re.IGNORECASE)
    for code in TDS_SECTION_CODES
]
_RE_SECTION_CONTEXT = re.compile(
    r"(tds|section)\s*(" + "|".join(TDS_SECTION_CODES) + r")\b",
    re.IGNORECASE,
)

_RE_GST_KEYWORDS: list[tuple[str, re.Pattern]] = [
    (kw, re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE))
    for kw in GST_KEYWORDS
]

_RE_GSTIN = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][Z][A-Z0-9]\b")

_MERCHANT_SET = set(MERCHANT_KEYWORDS)
_SERVICE_VENDOR_SET = set(SERVICE_VENDOR_KEYWORDS)

_NEGATIVE_PATTERNS: list[tuple[re.Pattern, int]] = [
    (re.compile(p if is_re else re.escape(p), re.IGNORECASE), pen)
    for p, is_re, pen in NEGATIVE_KEYWORDS
]

_COMPANY_SUFFIX_PATTERNS: list[tuple[re.Pattern, int]] = [
    (re.compile(p if is_re else re.escape(p), re.IGNORECASE), pen)
    for p, is_re, pen in COMPANY_SUFFIX_PENALTIES
]

# NORMAL override patterns — utility boards, statutory payments
_NORMAL_OVERRIDE_PATTERNS: list[re.Pattern] = [
    re.compile(p if is_re else re.escape(p), re.IGNORECASE)
    for p, is_re in NORMAL_OVERRIDE_KEYWORDS
]

_HIGH_CONFIDENCE_GST_PATTERNS: list[tuple[str, re.Pattern]] = [
    (pattern, re.compile(pattern if is_re else re.escape(pattern), re.IGNORECASE))
    for pattern, is_re in GST_HIGH_CONFIDENCE_PATTERNS
]

# Strong GST keywords that block TDS priority and (when present) bypass
# NORMAL overrides and the amount-threshold guard.
# Bare 'gst' is intentionally included so narrations like '+GST' or 'Mob alrt+GST'
# are never silently skipped by the sub-₹1 amount filter.
_STRONG_GST_KWS = {
    "gst",            # catches '+GST', 'GST applicable', 'GST charges' etc.
    "igst", "cgst", "sgst", "utgst",
    "gstin",
    "gst payment", "gst challan", "gst refund",
    "tax invoice", "gst invoice",
}
# Strong TDS keywords that block NORMAL overrides and amount-threshold guard
_STRONG_TDS_KWS = {"tds", "tax deducted", "income tax", "tcs", "it refund"}

_QUARTER_END_MONTHS = {3, 6, 9, 12}

# ── Vendor overrides — loaded once at module import ───────────────────────────
# Structure: list of (pattern_lower, category, priority)
# priority: "HARD" or "SOFT"
_VENDOR_INTELLIGENCE: list[dict] = []
_LEARNING_MEMORY: dict[str, dict] = {}

def _load_vendor_intelligence() -> list[dict]:
    csv_path = Path(__file__).resolve().parent.parent / "data" / "vendor_intelligence.csv"
    intel = []
    if not csv_path.exists():
        return intel
    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader((line for line in fh if not line.lstrip().startswith("#")))
        for row in reader:
            pattern = (row.get("vendor_pattern") or row.get("pattern") or "").strip().lower()
            cat = (row.get("learned_category") or row.get("category") or "").strip().upper()
            conf = row.get("confidence", "MEDIUM").strip().upper()
            if pattern and cat:
                intel.append({"pattern": pattern, "category": cat, "confidence": conf})
    logger.debug("Loaded %d vendor intelligence patterns from %s", len(intel), csv_path)
    return intel

def _load_learning_memory() -> dict[str, dict]:
    csv_path = Path(__file__).resolve().parent.parent / "data" / "learning_memory.csv"
    mem = {}
    if not csv_path.exists():
        return mem
    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader((line for line in fh if not line.lstrip().startswith("#")))
        for row in reader:
            pattern = row.get("vendor_pattern", "").strip().lower()
            cat = row.get("corrected_category", "").strip().upper()
            try:
                count = int(row.get("count", "0").strip())
            except ValueError:
                count = 0
            if pattern and cat and count > 0:
                mem[pattern] = {"category": cat, "count": count}
    logger.debug("Loaded %d learning memory rules from %s", len(mem), csv_path)
    return mem

_VENDOR_INTELLIGENCE = _load_vendor_intelligence()
_LEARNING_MEMORY = _load_learning_memory()

# ── Load ML Model ─────────────────────────────────────────────────────────────
import joblib
import numpy as np

ML_MODEL = None
ML_VEC = None
try:
    model_path = Path(__file__).resolve().parent.parent / "models" / "xgb_model.pkl"
    vec_path = Path(__file__).resolve().parent.parent / "models" / "tfidf_vectorizer.pkl"
    if model_path.exists() and vec_path.exists():
        ML_MODEL = joblib.load(model_path)
        ML_VEC = joblib.load(vec_path)
except Exception as e:
    logger.warning("Failed to load ML model: %s", e)

def reload_memory():
    global _VENDOR_INTELLIGENCE, _LEARNING_MEMORY
    _VENDOR_INTELLIGENCE = _load_vendor_intelligence()
    _LEARNING_MEMORY = _load_learning_memory()


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
    dbg    = result.debug_parts

    # ── Extract values ────────────────────────────────────────────────────────
    text   = normalize_text(row.get(INTERNAL_DESCRIPTION_COL, ""))
    debit  = _safe_float(row.get(INTERNAL_DEBIT_COL))
    credit = _safe_float(row.get(INTERNAL_CREDIT_COL))
    date   = row.get(INTERNAL_DATE_COL)
    amount = debit if debit > 0 else credit
    result.normalized_text = text

    # ── Transaction type ──────────────────────────────────────────────────────
    tx_type = parse_transaction_type(text)
    result.transaction_type = tx_type.name if hasattr(tx_type, 'name') else str(tx_type)

    # ── Explicit tax pre-check (before ALL other logic) ──────────────────────
    # Must happen BEFORE the amount threshold guard.
    # Narrations containing explicit GST/TDS evidence must NEVER be silently
    # discarded because of a sub-₹1 amount filter.
    _text_early = normalize_text(row.get(INTERNAL_DESCRIPTION_COL, ""))
    _has_explicit_gst = any(kw in _text_early for kw in _STRONG_GST_KWS)
    _has_explicit_tds = any(kw in _text_early for kw in _STRONG_TDS_KWS)

    # ── Amount sanity ─────────────────────────────────────────────────────────
    # Exception: if explicit tax keywords are present, bypass the amount filter.
    # Sub-₹1 bank charges with '+GST' are real tax events even if tiny.
    if 0 < amount < AMOUNT_IGNORE_BELOW and not (_has_explicit_gst or _has_explicit_tds):
        dbg.append(f"amount ₹{amount} < ₹{AMOUNT_IGNORE_BELOW} → skip scoring (no explicit tax keyword)")
        return _finalise(result, debit, credit, amount, dbg, date)

    # ── Probe for strong tax keywords (reuse pre-check already done above) ─────
    has_strong_gst = _has_explicit_gst
    has_strong_tds = _has_explicit_tds
    result.classification_mode = "EXPLICIT" if (has_strong_gst or has_strong_tds) else "HEURISTIC"
    result.explicit_rule = has_strong_gst or has_strong_tds

    # ── High-confidence business GST rules ───────────────────────────────────
    # These patterns are not literal GST text, but they represent invoice-backed
    # purchases, SaaS subscriptions, POS retail purchases, and business utility
    # payments that should be treated as GST-bearing instead of POSSIBLE_GST.
    if not has_strong_tds:
        business_gst_pattern = _high_confidence_gst_match(text)
        if business_gst_pattern:
            result.category = CATEGORY_GST
            result.classification_mode = "BUSINESS_GST_RULE"
            result.explicit_rule = True
            dbg.append(f"High-confidence business GST rule matched: {business_gst_pattern}")
            return _finalise(result, debit, credit, amount, dbg, date)

    # ── NORMAL override: utility / statutory payments ─────────────────────────
    # If a utility keyword matches AND no strong GST/TDS keyword is present,
    # immediately return NORMAL. This replaces fragile large negative penalties.
    if not has_strong_gst and not has_strong_tds:
        for pat in _NORMAL_OVERRIDE_PATTERNS:
            if pat.search(text):
                dbg.append(f"Statutory/utility payment detected")
                result.category = CATEGORY_NORMAL
                result.deterministic_normal = True
                return _finalise(result, debit, credit, amount, dbg, date)

    # ── Salary credit hard override ───────────────────────────────────────────
    _SALARY_SIGNALS = ("salary", "net salary", "payroll", "wages")
    if credit > 0 and debit == 0 and any(s in text for s in _SALARY_SIGNALS):
        dbg.append("Salary credit detected")
        result.category = CATEGORY_NORMAL
        result.deterministic_normal = True
        return _finalise(result, debit, credit, amount, dbg, date)

    if tx_type == TxType.ATM_WITHDRAWAL:
        dbg.append("ATM withdrawal detected")
        result.deterministic_normal = True
        return _finalise(result, debit, credit, amount, dbg, date)

    # ── Vendor Intelligence Layer ─────────────────────────────────────────────
    # Applied after ATM/salary hard overrides but BEFORE scoring.
    intel_cat = None
    intel_conf = None
    intel_pattern = None
    is_learned = False
    intel_flow_hint = None

    for vi in _VENDOR_INTELLIGENCE:
        if _vendor_pattern_matches(vi["pattern"], text):
            intel_pattern = vi["pattern"]
            intel_cat = vi["category"]
            intel_conf = vi["confidence"]
            result.vendor = vi["pattern"].upper()
            dbg.append(f"Vendor intelligence '{intel_pattern}' → {intel_cat} ({intel_conf})")
            break

    # Apply learning memory overrides if threshold met
    if intel_pattern and intel_pattern in _LEARNING_MEMORY:
        mem = _LEARNING_MEMORY[intel_pattern]
        if mem["count"] >= 3:  # Correction threshold
            intel_cat = mem["category"]
            intel_conf = "HIGH"
            is_learned = True
            dbg.append(f"Vendor memory suggests {intel_cat} based on {mem['count']} prior corrections")

    if intel_cat == CATEGORY_NORMAL:
        if intel_conf == "HIGH" and not has_strong_gst and not has_strong_tds:
            result.category = CATEGORY_NORMAL
            if is_learned:
                result.classification_mode = "LEARNED"
            dbg.append("Known non-tax vendor matched")
            result.deterministic_normal = True
            return _finalise(result, debit, credit, amount, dbg, date)
        # MEDIUM confidence NORMAL deferred to end of scoring
        
    if intel_cat in {"BUSINESS_PAYMENT", "BUSINESS_EXPENSE", "VENDOR_PAYMENT"}:
        intel_flow_hint = FLOW_BUSINESS
        dbg.append("Vendor intelligence provides business-flow context (not a tax category)")
        intel_cat = None

    # ── TDS scoring ───────────────────────────────────────────────────────────
    tds = 0

    for kw, pattern in _RE_TDS_KEYWORDS:
        if pattern.search(text):
            tds += SCORE_TDS_KEYWORD
            result.classification_mode = "EXPLICIT"
            result.explicit_rule = True
            dbg.append("Explicit TDS keyword found")
            break  # count once

    for pattern in _RE_SECTION_WITH_LETTER:
        match = pattern.search(text)
        if match:
            matched_code = match.group(0).upper()
            result.classification_mode = "EXPLICIT"
            result.explicit_rule = True
            if "194A" in matched_code:
                tds += 6
                dbg.append("TDS section 194A detected")
            elif "194J" in matched_code:
                tds += 8
                dbg.append("TDS section 194J detected")
            else:
                tds += 8
                dbg.append(f"TDS section {matched_code} detected")
            break
    else:
        if _RE_SECTION_CONTEXT.search(text):
            tds += SCORE_TDS_SECTION_CODE
            result.classification_mode = "EXPLICIT"
            result.explicit_rule = True
            dbg.append("Contextual TDS section code detected")

    # Bare section code (194/195/206) — no debit condition required anymore
    if tds == 0:
        if re.search(r'\b(192|193|194|195|196|206)\b', text):
            tds += 4
            dbg.append("Implicit TDS section code detected")

    if tx_type == TxType.BULK_NEFT:
        tds += SCORE_TDS_TXTYPE_BLKNEFT
        dbg.append("Bulk NEFT format detected")

    if _is_quarter_end(date):
        tds += SCORE_TDS_QUARTER_END
        dbg.append("Transaction occurred at quarter-end")

    # ── GST scoring ───────────────────────────────────────────────────────────
    gst = 0

    if _RE_GSTIN.search(text.upper()):
        gst += SCORE_GST_GSTIN_PATTERN
        result.classification_mode = "EXPLICIT"
        result.explicit_rule = True
        has_strong_gst = True
        dbg.append("Explicit GSTIN pattern detected")

    for kw, pattern in _RE_GST_KEYWORDS:
        if pattern.search(text):
            gst += SCORE_GST_KEYWORD
            result.classification_mode = "EXPLICIT"
            result.explicit_rule = True
            has_strong_gst = True
            dbg.append(f"Explicit GST keyword found: {kw}")
            break
            
    # Check weak hints (do not make explicit, just add to heuristic score)
    for kw in GST_WEAK_HINTS:
        if re.search(r'\b' + re.escape(kw) + r'\b', text, re.IGNORECASE):
            gst += SCORE_GST_WEAK_HINT
            dbg.append(f"Weak GST/tax hint found: {kw}")
            break

    # Apply Vendor Intelligence Bias
    if intel_cat == CATEGORY_GST:
        if is_learned:
            result.classification_mode = "LEARNED"
            gst += 8
        elif intel_conf == "HIGH":
            gst += 8
        else:
            gst += 4
        dbg.append(f"Vendor intelligence applied {intel_conf} GST bias")
    elif intel_cat == CATEGORY_POSSIBLE_GST:
        if is_learned:
            result.classification_mode = "LEARNED"
            gst += 8
        elif intel_conf == "HIGH":
            gst += 6
        else:
            gst += 4
        dbg.append(f"Vendor intelligence applied {intel_conf} GST bias")
    elif intel_cat == CATEGORY_TDS:
        if is_learned:
            result.classification_mode = "LEARNED"
            tds += 8
        elif intel_conf == "HIGH":
            tds += 6
        else:
            tds += 4
        dbg.append(f"Vendor intelligence applied {intel_conf} TDS bias")

    for kw in _MERCHANT_SET:
        if kw in text:
            if not result.vendor:
                result.vendor = kw.upper()
            gst += SCORE_GST_GATEWAY
            dbg.append("Possible business/merchant payment inferred")
            break

    if tx_type in (TxType.CMS, TxType.CARD_PAYMENT):
        gst += SCORE_GST_CMS_CARDPMT
        dbg.append("Possible business expense inferred from card payment")

    if tx_type == TxType.UPI_DEBIT:
        gst += SCORE_GST_UPI_DEBIT
        dbg.append("Possible merchant/business payment inferred from UPI narration")

    if debit > 0 and _is_nonround(debit):
        gst += SCORE_GST_NONROUND_AMT
        dbg.append("Non-round fractional amount")

    # ── Negative penalties ────────────────────────────────────────────────────
    neg     = _negative_penalty(text, dbg)        # both TDS and GST
    neg_tds = _company_suffix_penalty(text, dbg)  # TDS only
    tds = max(0, tds + neg + neg_tds)
    gst = max(0, gst + neg)

    # ── Direction penalty — incoming credits ──────────────────────────────────
    # Exception: commercial settlement infrastructure (escrow, aggregator, gateway)
    # must NOT have their score zeroed by the credit penalty — the penalty exists
    # to block P2P/personal credits, not commercial revenue flows.
    is_incoming = (credit > 0 and debit == 0)
    is_infra_credit = is_incoming and any(
        kw in text for kw in [
            "payment aggregator", "escrow", "merchant settlement",
            "cms_", "setdt-", "mid-", "aggregator", "acquiring",
            "razorpay", "cashfree", "payu", "ccavenue", "easebuzz", "billdesk",
        ]
    )
    if is_incoming and not is_infra_credit:
        is_refund = "refund" in text
        if not (is_refund and gst > 0):
            gst = max(0, gst + PENALTY_INCOMING_GST)
            dbg.append("Incoming credit penalty applied to GST")
        if not (tds >= 8 or is_refund):
            tds = max(0, tds + PENALTY_INCOMING_TDS)
            dbg.append("Incoming credit penalty applied to TDS")
    elif is_infra_credit:
        dbg.append("Incoming credit penalty SKIPPED — commercial settlement infrastructure detected")

    # ── Flow Type Classification ───────────────────────────────────────────────
    # FLOW_TYPE is behavioral context only. It informs ambiguity, but never
    # becomes a TAX_CATEGORY.
    flow = classify_flow(
        text=text,
        debit=debit,
        credit=credit,
        amount=amount,
        tx_type_name=result.transaction_type,
        date=date,
        vendor=result.vendor,
    )
    if intel_flow_hint and flow.flow_type not in (FLOW_CONSUMER, FLOW_TAX):
        flow.flow_type = FLOW_SETTLEMENT if credit > 0 and debit == 0 else FLOW_BUSINESS
        flow.is_commercial = True
        flow.flow_reason = "Vendor intelligence marked this as commercial flow"

    result.flow_type          = flow.flow_type
    result.flow_confidence    = flow.flow_confidence
    result.flow_reason        = flow.flow_reason
    result.is_commercial_flow = flow.is_commercial

    # ── Service/vendor semantic intelligence ──────────────────────────────────
    # Business/service semantics increase POSSIBLE_GST likelihood, but never
    # upgrade to definite GST without explicit GST evidence.
    has_service_vendor_hint = (
        debit > 0
        and flow.flow_type in (FLOW_BUSINESS, FLOW_SUBSCRIPTION)
        and flow.flow_type != FLOW_CONSUMER
        and _has_service_vendor_signal(text)
    )
    if has_service_vendor_hint and not has_strong_gst:
        gst += SCORE_GST_WEAK_HINT
        result.ambiguous_semantics = True
        dbg.append("Service/vendor business semantics → POSSIBLE_GST candidate")

    result.tds_score = tds
    result.gst_score = gst

    # ── Category decision ─────────────────────────────────────────────────────
    # Priority 1: Explicit GST dominates all other semantics.
    if has_strong_gst or (_RE_GSTIN.search(text.upper())):
        result.category = CATEGORY_GST
        result.classification_mode = "EXPLICIT"
        result.explicit_rule = True
        dbg.append("Explicit GST evidence → GST")

    # Priority 2: Strong TDS — unless explicit GST is present.
    elif tds >= SCORE_HIGH_THRESHOLD and not has_strong_gst:
        result.category = CATEGORY_TDS
        dbg.append(f"TDS high tds={tds} ≥ {SCORE_HIGH_THRESHOLD}")

    # Priority 3: Medium TDS wins over weaker GST when no explicit GST exists.
    elif tds >= SCORE_MEDIUM_THRESHOLD and tds >= gst:
        result.category = CATEGORY_TDS
        dbg.append(f"TDS medium tds={tds}")

    # Priority 4: Heuristic GST evidence is ambiguous tax interpretation.
    elif gst >= SCORE_MEDIUM_THRESHOLD:
        result.category = CATEGORY_POSSIBLE_GST
        result.ambiguous_semantics = True
        dbg.append(f"POSSIBLE_GST — heuristic GST/business signals gst={gst}")

    # Priority 5: Close-call tax ambiguity stays in the nearest tax category and
    # is surfaced through REVIEW_RECOMMENDED, not a separate UNCERTAIN category.
    elif tds >= SCORE_UNCERTAIN_CUTOFF and gst >= SCORE_UNCERTAIN_CUTOFF:
        result.category = CATEGORY_TDS if tds >= gst else CATEGORY_POSSIBLE_GST
        result.ambiguous_semantics = True
        dbg.append(f"Competing tax signals tds={tds} gst={gst}")

    # Priority 6: Commercial flow backstop. This preserves business/settlement
    # understanding as POSSIBLE_GST without creating business-shaped tax labels.
    elif flow.is_commercial and flow.flow_type in (FLOW_BUSINESS, FLOW_SETTLEMENT, FLOW_SUBSCRIPTION):
        result.category = CATEGORY_POSSIBLE_GST
        result.ambiguous_semantics = True
        dbg.append(f"Commercial flow semantics → POSSIBLE_GST candidate (flow={flow.flow_type})")

    # Priority 7: Soft vendor override (SOFT priority, no strong tax signal).
    elif intel_cat == CATEGORY_NORMAL and intel_conf == "MEDIUM":
        result.category = CATEGORY_NORMAL
        result.deterministic_normal = True
        dbg.append("MEDIUM vendor intelligence override applied → NORMAL")

    # Default: no meaningful signals → NORMAL
    else:
        result.category = CATEGORY_NORMAL
        result.deterministic_normal = flow.flow_type in (FLOW_CONSUMER, FLOW_TRANSFER) or (tds == 0 and gst == 0)
        dbg.append(f"NORMAL — no signals tds={tds} gst={gst}")

    return _finalise(result, debit, credit, amount, dbg, date)


# ── Finalise: confidence + Review_Recommended + Reason ───────────────────────

def _finalise(result: ScoreResult, debit: float, credit: float,
              amount: float, dbg: list, date=None) -> ScoreResult:
    tds, gst = result.tds_score, result.gst_score
    top = max(tds, gst)

    # Ensure early-return paths still carry FLOW_TYPE. Flow is behavioral context
    # and does not alter the tax category by itself.
    if not result.flow_reason:
        flow = classify_flow(
            text=result.normalized_text,
            debit=debit,
            credit=credit,
            amount=amount,
            tx_type_name=result.transaction_type,
            date=date,
            vendor=result.vendor,
        )
        result.flow_type          = flow.flow_type
        result.flow_confidence    = flow.flow_confidence
        result.flow_reason        = flow.flow_reason
        result.is_commercial_flow = flow.is_commercial

    # Confidence represents overall classification certainty, not a duplicate
    # flow confidence signal.
    if result.explicit_rule or result.classification_mode == "EXPLICIT":
        result.confidence = "HIGH"
    elif result.deterministic_normal and result.category == CATEGORY_NORMAL:
        result.confidence = "HIGH"
    elif result.category == CATEGORY_POSSIBLE_GST:
        result.confidence = "MEDIUM" if top >= SCORE_MEDIUM_THRESHOLD or result.is_commercial_flow else "LOW"
    elif top >= SCORE_HIGH_THRESHOLD:
        result.confidence = "HIGH"
    elif top >= SCORE_MEDIUM_THRESHOLD or result.is_commercial_flow:
        result.confidence = "MEDIUM"
    else:
        result.confidence = "LOW"

    # ── ML Assistance Layer ───────────────────────────────────────────────────
    # ML may intervene only for ambiguous heuristic NORMAL/POSSIBLE_GST cases.
    # Explicit rules and deterministic classifications stay fully rule-owned.
    ml_eligible = (
        result.classification_mode == "HEURISTIC"
        and not result.explicit_rule
        and not result.deterministic_normal
        and result.category in (CATEGORY_NORMAL, CATEGORY_POSSIBLE_GST)
        and (result.ambiguous_semantics or result.confidence in ("LOW", "MEDIUM"))
    )

    if ml_eligible and ML_MODEL is not None and ML_VEC is not None:
        try:
            texts = [result.normalized_text]
            X_text = ML_VEC.transform(texts).toarray()
            X_num = np.array([[result.gst_score, result.tds_score, np.log1p(debit + credit)]])
            X = np.hstack((X_text, X_num))
            
            pred_prob = ML_MODEL.predict_proba(X)[0]
            classes = list(getattr(ML_MODEL, "classes_", [0, 1]))
            probs = {cls: float(prob) for cls, prob in zip(classes, pred_prob)}
            possible_prob = probs.get(1, float(pred_prob[-1]))
            normal_prob = probs.get(0, float(pred_prob[0]))
            ml_pred = CATEGORY_POSSIBLE_GST if possible_prob >= normal_prob else CATEGORY_NORMAL
            ml_conf = max(possible_prob, normal_prob)

            if ml_conf < 0.70:
                result.ml_uncertain = True
                dbg.append("ML uncertainty on ambiguous transaction")
            elif (
                result.category == CATEGORY_NORMAL
                and ml_pred == CATEGORY_POSSIBLE_GST
                and possible_prob >= 0.78
            ):
                result.category = CATEGORY_POSSIBLE_GST
                result.classification_mode = "ML_ASSISTED"
                result.ml_assist_used = True
                result.ml_model_confidence = possible_prob
                result.confidence = "HIGH" if possible_prob >= 0.92 else "MEDIUM"
                result.ambiguous_semantics = True
                dbg.append(f"ML assisted ambiguity resolution → POSSIBLE_GST ({possible_prob:.2%})")
            elif result.category == CATEGORY_POSSIBLE_GST and ml_pred == CATEGORY_NORMAL and normal_prob >= 0.78:
                result.ml_uncertain = True
                dbg.append("ML disagreed with heuristic POSSIBLE_GST")
        except Exception as e:
            logger.debug("ML assistance skipped: %s", e)

    # Review_Recommended
    # Explicit GST/TDS and deterministic NORMAL do not require review. Low
    # confidence, ambiguous service/vendor semantics, and ML uncertainty do.
    score_gap = abs(tds - gst)
    if result.explicit_rule and result.category in (CATEGORY_GST, CATEGORY_TDS):
        result.needs_review = False
    elif result.category == CATEGORY_NORMAL and result.deterministic_normal:
        result.needs_review = False
    else:
        result.needs_review = (
            result.confidence == "LOW"
            or result.ambiguous_semantics
            or result.ml_uncertain
            or (
                result.category in (CATEGORY_POSSIBLE_GST, CATEGORY_TDS)
                and result.confidence == "MEDIUM"
                and score_gap <= SCORE_CLOSE_CALL_MARGIN
            )
        )

    # ── Reason string — human-readable, pipe-separated signal list ────────────
    # Clean up empty strings and remove duplicates while preserving order
    clean_dbg = []
    for d in dbg:
        if d and d not in clean_dbg:
            clean_dbg.append(d)
            
    result.reason = " | ".join(clean_dbg) if clean_dbg else "No distinct signals found"

    logger.debug(
        "[%s|%s|review=%s] tds=%d gst=%d | %s",
        result.category, result.confidence, result.needs_review,
        tds, gst, result.reason,
    )
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


def _is_nonround(amount: float) -> bool:
    return int(amount * 100) % 100 not in (0, 50)


def _is_quarter_end(date_val) -> bool:
    if date_val is None or (isinstance(date_val, float) and pd.isna(date_val)):
        return False
    try:
        if hasattr(date_val, "month"):
            return date_val.month in _QUARTER_END_MONTHS
        parsed = pd.to_datetime(date_val, dayfirst=True, errors="coerce")
        return pd.notna(parsed) and parsed.month in _QUARTER_END_MONTHS
    except Exception:
        return False


def _has_service_vendor_signal(text: str) -> bool:
    return any(signal in text for signal in _SERVICE_VENDOR_SET)


def _high_confidence_gst_match(text: str) -> str | None:
    for label, pattern in _HIGH_CONFIDENCE_GST_PATTERNS:
        if pattern.search(text):
            return label
    return None


def _vendor_pattern_matches(pattern: str, text: str) -> bool:
    if len(pattern) <= 3 and pattern.replace("_", "").isalnum():
        return re.search(rf"(?<![a-z0-9]){re.escape(pattern)}(?![a-z0-9])", text) is not None
    return pattern in text


def _negative_penalty(text: str, dbg: list) -> int:
    total = 0
    matched = False
    for pattern, penalty in _NEGATIVE_PATTERNS:
        if pattern.search(text):
            total += penalty
            matched = True
    if matched:
        dbg.append("Matched personal/transfer exclusion keyword")
    return total


def _company_suffix_penalty(text: str, dbg: list) -> int:
    """Penalty applied to TDS score ONLY — not GST."""
    total = 0
    matched = False
    for pattern, penalty in _COMPANY_SUFFIX_PATTERNS:
        if pattern.search(text):
            total += penalty
            matched = True
    if matched:
        dbg.append("Company suffix penalty applied")
    return total
