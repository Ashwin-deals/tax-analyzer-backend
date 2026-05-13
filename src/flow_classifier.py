"""
src/flow_classifier.py
──────────────────────
Context-Aware Financial Flow Intelligence Layer.

Classifies transactions by FINANCIAL BEHAVIOR (flow type) independently
from their TAX_CATEGORY. This is a second classification dimension.

FLOW_TYPE answers: "What operational financial behavior is this?"
TAX_CATEGORY answers: "What is the tax interpretation?"

Both dimensions are orthogonal and complement each other.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from utils.constants import SERVICE_VENDOR_KEYWORDS

# ── FLOW_TYPE constants ────────────────────────────────────────────────────────
FLOW_CONSUMER     = "CONSUMER"      # Personal/consumer spend
FLOW_BUSINESS     = "BUSINESS"      # Operational commercial spend/vendor payout
FLOW_SETTLEMENT   = "SETTLEMENT"    # Incoming settlement/commercial revenue
FLOW_TRANSFER     = "TRANSFER"      # Self-transfer/refund/salary/bank movement
FLOW_TAX          = "TAX"           # Government/tax payment
FLOW_SUBSCRIPTION = "SUBSCRIPTION"  # Recurring software/service billing
FLOW_UNKNOWN      = "UNKNOWN"       # Unresolved behavior

# Backward-compatible constant aliases. Their values intentionally collapse to
# the simplified FLOW_TYPE vocabulary above.
FLOW_REVENUE_SETTLEMENT = FLOW_SETTLEMENT
FLOW_BUSINESS_EXPENSE   = FLOW_BUSINESS
FLOW_CONSUMER_EXPENSE   = FLOW_CONSUMER
FLOW_TAX_PAYMENT        = FLOW_TAX
FLOW_VENDOR_PAYMENT     = FLOW_BUSINESS
FLOW_REFUND             = FLOW_TRANSFER
FLOW_INTERNAL_TRANSFER  = FLOW_TRANSFER
FLOW_SALARY             = FLOW_TRANSFER

# ── Payment infrastructure vocabulary ─────────────────────────────────────────
# Strongest commercial signals — clear financial infrastructure indicators.
# Kept narrow intentionally to avoid false positives on personal NEFT transfers.
_PAYMENT_INFRA_SIGNALS = [
    "payment aggregator",
    "escrow account",         # very specific, not just 'escrow'
    "merchant settlement",
    "acquiring",
    "mid-",                   # merchant ID prefix e.g. MID-123456
    "setdt-",                 # settlement date suffix e.g. SETDT-20240915
    "aggregator",
    "payment gateway",
]

# ── Personal finance signals (exclusion layer) ───────────────────────────────
# These fire FIRST and prevent personal finance transactions from being
# classified as commercial — they are unambiguously personal/individual finance.
_PERSONAL_FINANCE_SIGNALS = [
    "home loan",
    "vehicle loan",
    "personal loan",
    "motorcycle loan",
    "gold loan",
    "auto loan",
    "car loan",
    "loan emi",
    "loan repayment",
    "school fee",
    "college fee",
    "tuition fee",
    "lic premium",
    "insurance premium",
    "term insurance",
    "health insurance",
    "mutual fund",
    " sip ",
    " ppf ",
    " nps ",
    "nps tier",
    "national pension",
    "sukanya samriddhi",
    "fixed deposit",
    "fd opened",
    "chit fund",
    "property sale",
    "rera ",
    "credit card bill",
    "credit card payment",
    "dividend received",
    "medical reimbursement",
    "gratuity payment",
    "advance rent",
    "zerodha broking",
    "equity settlement t plus",
    "provident fund",
]

# ── Revenue / settlement signals ───────────────────────────────────────────────
# Credit-side commercial flows and gateway settlement identifiers.
_SETTLEMENT_SIGNALS = [
    "razorpay",
    "cashfree",
    "payu",
    "ccavenue",
    "easebuzz",
    "billdesk",
    "instamojo",
    "stripe",
    "paypal",
    "zaakpay",
    "juspay",
    "payment settlement",
    "settlement",
    "payout",
    "transfer credit",
    "neft cr",
    "rtgs cr",
]

# ── Business expense signals (debit-side operational spend) ───────────────────
_BUSINESS_EXPENSE_SIGNALS = [
    "vendor payment",
    "procurement",
    "office supply",
    "aws ",
    "azure ",
    "gcp ",
    "google cloud",
    "hosting",
    "domain",
    "server",
    "software license",
    "advertising",
    "facebook ads",
    "google ads",
    "logistics",
    "courier",
    "raw material",
    "professional fee",
    "consultancy",
    "retainer",
    "b2b payment",
] + SERVICE_VENDOR_KEYWORDS

# ── Consumer/personal expense signals ─────────────────────────────────────────
_CONSUMER_SIGNALS = [
    "swiggy",
    "zomato",
    "blinkit",
    "dunzo",
    "amazon",
    "flipkart",
    "myntra",
    "ajio",
    "meesho",
    "netflix",
    "spotify",
    "hotstar",
    "prime video",
    "zee5",
    "uber",
    "ola",
    "rapido",
    "restaurant",
    "grocery",
    "salon",
    "spa",
    "gym",
    "paytm mall",
]

# ── Tax payment signals ────────────────────────────────────────────────────────
_TAX_PAYMENT_SIGNALS = [
    "gst payment",
    "gst challan",
    "tax payment",
    "income tax",
    "tds payment",
    "advance tax",
    "self assessment",
    "nsdl",
    "tin-nsdl",
    "gstn",
    "gst portal",
    "challan 280",
    "challan 281",
    "itns",
]

# ── Vendor payment signals ─────────────────────────────────────────────────────
_VENDOR_SIGNALS = [
    "bharatpe",
    "phonepe business",
    "contractor",
    "freelancer",
    "commission",
    "brokerage",
    "service charge",
    "supplier payment",
    "trade payment",
]

# ── Refund signals ─────────────────────────────────────────────────────────────
_REFUND_SIGNALS = [
    "refund",
    "reversal",
    "cashback",
    "rebate",
    "credit note",
    "chargeback",
    "return credit",
    "cancelled",
]

# ── Internal transfer signals ──────────────────────────────────────────────────
_INTERNAL_SIGNALS = [
    "self transfer",
    "own account",
    "internal transfer",
    "sweep in",
    "sweep out",
    "fd transfer",
    "rd transfer",
    "/p2p/",
    "imps-opm",
    "neft/p2p",
    "opening balance",
    "closing balance",
    "between accounts",
]

# ── Subscription signals ───────────────────────────────────────────────────────
_SUBSCRIPTION_SIGNALS = [
    "subscription",
    "monthly plan",
    "annual plan",
    "renewal",
    "auto-debit",
    "standing instruction",
    "recurring",
    "mandate",
    "nach ",
    "ecs debit",
    "saas",
    "cloud plan",
]

# ── Salary signals ─────────────────────────────────────────────────────────────
_SALARY_SIGNALS = [
    "salary",
    "net salary",
    "payroll",
    "wages",
    "salary credit",
    "monthly salary",
    "staff payment",
    "hrms",
    "compensation",
]


@dataclass
class FlowResult:
    flow_type:       str = FLOW_UNKNOWN
    flow_confidence: str = "LOW"      # HIGH / MEDIUM / LOW
    flow_reason:     str = ""
    direction:       str = "UNKNOWN"  # DEBIT / CREDIT / NEUTRAL
    is_commercial:   bool = False      # True if any commercial/business signal found


def classify_flow(
    text: str,
    debit: float,
    credit: float,
    amount: float,
    tx_type_name: str = "",
    date=None,
    vendor: str = "",
    recurrence_count: int = 0,
) -> FlowResult:
    """
    Classify the FLOW_TYPE of a transaction.

    Parameters
    ----------
    text             : normalized narration text
    debit            : debit amount (0 if credit)
    credit           : credit amount (0 if debit)
    amount           : max(debit, credit)
    tx_type_name     : TxType enum name string
    date             : transaction date
    vendor           : extracted vendor string
    recurrence_count : how many times this vendor/pattern appeared before

    Returns
    -------
    FlowResult with flow_type, confidence, reason, direction, is_commercial
    """
    result  = FlowResult()
    reasons = []
    t       = text.lower()

    is_debit  = debit > 0 and credit == 0
    is_credit = credit > 0 and debit == 0
    result.direction = "DEBIT" if is_debit else "CREDIT" if is_credit else "NEUTRAL"

    # ── Priority 0a: Personal finance exclusion ───────────────────────────────
    # Fires BEFORE commercial detection to protect personal finance transactions.
    if _check_any(t, _PERSONAL_FINANCE_SIGNALS):
        result.flow_type       = FLOW_CONSUMER_EXPENSE
        result.flow_confidence = "HIGH"
        result.is_commercial   = False
        result.flow_reason     = f"Personal finance ({_which(t, _PERSONAL_FINANCE_SIGNALS)}) → consumer expense"
        return result

    # ── Priority 0b: Payment infrastructure (strongest commercial signal) ──────
    # Unambiguous commercial financial infrastructure patterns.
    has_infra = _check_any(t, _PAYMENT_INFRA_SIGNALS)
    if has_infra:
        result.is_commercial = True
        infra_matched = _which(t, _PAYMENT_INFRA_SIGNALS)
        if is_credit:
            result.flow_type       = FLOW_REVENUE_SETTLEMENT
            result.flow_confidence = "HIGH"
            reasons.append(f"Payment infrastructure detected ({infra_matched}) → credit = merchant settlement")
        else:
            result.flow_type       = FLOW_BUSINESS_EXPENSE
            result.flow_confidence = "HIGH"
            reasons.append(f"Payment infrastructure detected ({infra_matched}) → debit = business expense")
        result.flow_reason = " | ".join(reasons)
        return result

    # ── Priority 1: ATM withdrawal ─────────────────────────────────────────────
    if tx_type_name == "ATM_WITHDRAWAL":
        result.flow_type       = FLOW_CONSUMER_EXPENSE
        result.flow_confidence = "HIGH"
        result.flow_reason     = "ATM withdrawal → consumer expense"
        return result

    # ── Priority 2: Salary credit ──────────────────────────────────────────────
    if any(s in t for s in _SALARY_SIGNALS) and is_credit:
        result.flow_type       = FLOW_SALARY
        result.flow_confidence = "HIGH"
        result.flow_reason     = "Salary/payroll credit detected"
        return result

    # ── Priority 3: Tax payment ────────────────────────────────────────────────
    if _check_any(t, _TAX_PAYMENT_SIGNALS):
        result.flow_type       = FLOW_TAX_PAYMENT
        result.flow_confidence = "HIGH"
        result.is_commercial   = True
        result.flow_reason     = f"Tax/government payment detected ({_which(t, _TAX_PAYMENT_SIGNALS)})"
        return result

    # ── Priority 3a: Explicit GST on bank / service charges ───────────────────
    # Narrations like 'Mob alrt Chg +GST', 'SMS charges +GST', 'Annual fee+GST'
    # are service charges with GST applied: BUSINESS_EXPENSE from a flow view.
    _GST_CHARGE_KWS = ("gst", "cgst", "sgst", "igst", "gstin", "tax invoice")
    if any(k in t for k in _GST_CHARGE_KWS):
        result.flow_type       = FLOW_BUSINESS_EXPENSE
        result.flow_confidence = "HIGH"
        result.is_commercial   = True
        result.flow_reason     = "Explicit GST on service charge → business expense (tax-inclusive fee)"
        return result

    # ── Priority 4: Refund signals ─────────────────────────────────────────────
    if _check_any(t, _REFUND_SIGNALS):
        result.flow_type       = FLOW_REFUND
        result.flow_confidence = "HIGH"
        result.flow_reason     = "Refund/reversal pattern detected"
        return result

    # ── Priority 5: Internal transfer ─────────────────────────────────────────
    if _check_any(t, _INTERNAL_SIGNALS):
        result.flow_type       = FLOW_INTERNAL_TRANSFER
        result.flow_confidence = "HIGH"
        result.flow_reason     = "Internal/self-transfer pattern detected"
        return result

    # ── Priority 6: Direction-aware gateway classification ────────────────────
    # Credit + payment gateway name → REVENUE_SETTLEMENT
    has_gateway = _check_any(t, _SETTLEMENT_SIGNALS)
    if has_gateway:
        result.is_commercial = True
        gw = _which(t, _SETTLEMENT_SIGNALS)
        if is_credit:
            result.flow_type       = FLOW_REVENUE_SETTLEMENT
            result.flow_confidence = "HIGH"
            reasons.append(f"Credit + gateway ({gw}) → merchant settlement")
            if recurrence_count >= 3:
                reasons.append(f"Recurrence ({recurrence_count}x) confirms settlement pattern")
        else:
            result.flow_type       = FLOW_BUSINESS_EXPENSE
            result.flow_confidence = "MEDIUM"
            reasons.append(f"Debit + gateway ({gw}) → operational business spend")
        result.flow_reason = " | ".join(reasons)
        return result

    # ── Priority 7: Subscription detection ────────────────────────────────────
    if is_debit and _check_any(t, _SUBSCRIPTION_SIGNALS):
        result.flow_type       = FLOW_SUBSCRIPTION
        result.flow_confidence = "HIGH"
        result.is_commercial   = _check_any(t, SERVICE_VENDOR_KEYWORDS)
        result.flow_reason     = "Subscription/recurring billing pattern"
        return result

    # ── Priority 8: Consumer signals ──────────────────────────────────────────
    if _check_any(t, _CONSUMER_SIGNALS):
        result.flow_type       = FLOW_CONSUMER_EXPENSE
        result.flow_confidence = "HIGH"
        result.flow_reason     = f"Consumer spending pattern ({_which(t, _CONSUMER_SIGNALS)})"
        return result

    # ── Priority 9: Business expense (debit-side) ─────────────────────────────
    if is_debit and _check_any(t, _BUSINESS_EXPENSE_SIGNALS):
        result.is_commercial   = True
        result.flow_type       = FLOW_BUSINESS_EXPENSE
        result.flow_confidence = "MEDIUM"
        result.flow_reason     = "Business/operational expense pattern detected"
        return result

    # ── Priority 10: Vendor payment (BharatPe, contractor) ────────────────────
    if is_debit and _check_any(t, _VENDOR_SIGNALS):
        result.is_commercial   = True
        result.flow_type       = FLOW_VENDOR_PAYMENT
        result.flow_confidence = "MEDIUM"
        result.flow_reason     = "Vendor/supplier payment pattern detected"
        return result

    # ── Priority 11: Transaction-type-based classification ────────────────────
    if tx_type_name == "UPI_CREDIT" and is_credit:
        if recurrence_count >= 5:
            result.flow_type       = FLOW_REVENUE_SETTLEMENT
            result.flow_confidence = "MEDIUM"
            result.is_commercial   = True
            result.flow_reason     = f"Recurring UPI credit ({recurrence_count}x) → likely revenue settlement"
        else:
            result.flow_type       = FLOW_INTERNAL_TRANSFER
            result.flow_confidence = "LOW"
            result.flow_reason     = "UPI credit — low recurrence, classified as transfer"
        return result

    if tx_type_name == "UPI_DEBIT" and is_debit:
        if amount > 50_000:
            result.is_commercial   = True
            result.flow_type       = FLOW_VENDOR_PAYMENT
            result.flow_confidence = "MEDIUM"
            result.flow_reason     = "High-value UPI debit → likely vendor payment"
        else:
            result.flow_type       = FLOW_CONSUMER_EXPENSE
            result.flow_confidence = "LOW"
            result.flow_reason     = "Standard UPI debit → consumer/merchant spend"
        return result

    if tx_type_name in ("BULK_NEFT", "NEFT", "RTGS") and is_debit:
        # Only mark as vendor payment if explicit business signal exists
        # (avoid false positives on personal NEFT payments)
        if result.is_commercial or _check_any(t, _BUSINESS_EXPENSE_SIGNALS + _VENDOR_SIGNALS):
            result.is_commercial   = True
            result.flow_type       = FLOW_VENDOR_PAYMENT
            result.flow_confidence = "MEDIUM"
            result.flow_reason     = "NEFT/RTGS outgoing with business signals → vendor payment"
        else:
            result.flow_type       = FLOW_INTERNAL_TRANSFER
            result.flow_confidence = "LOW"
            result.flow_reason     = "NEFT/RTGS outgoing → classified as transfer (no business signals)"
        return result

    if tx_type_name in ("NEFT", "RTGS") and is_credit:
        # Only revenue settlement if a gateway name is present; otherwise internal
        if result.is_commercial or _check_any(t, _SETTLEMENT_SIGNALS):
            result.is_commercial   = True
            result.flow_type       = FLOW_REVENUE_SETTLEMENT
            result.flow_confidence = "LOW"
            result.flow_reason     = "NEFT/RTGS incoming with commercial signal → possible revenue inflow"
        else:
            result.flow_type       = FLOW_INTERNAL_TRANSFER
            result.flow_confidence = "LOW"
            result.flow_reason     = "NEFT/RTGS incoming → classified as transfer (no commercial signals)"
        return result

    if tx_type_name == "CARD_PAYMENT":
        result.flow_type       = FLOW_CONSUMER_EXPENSE
        result.flow_confidence = "MEDIUM"
        result.flow_reason     = "Card payment → consumer/business card spend"
        return result

    if tx_type_name == "CMS":
        result.is_commercial   = True
        result.flow_type       = FLOW_BUSINESS_EXPENSE
        result.flow_confidence = "MEDIUM"
        result.flow_reason     = "CMS transaction → commercial processing"
        return result

    # ── Default: UNKNOWN ───────────────────────────────────────────────────────
    result.flow_type       = FLOW_UNKNOWN
    result.flow_confidence = "LOW"
    result.flow_reason     = "Insufficient context for flow classification"
    return result


# ── Behavioral Pattern Intelligence for ML Features ───────────────────────────

def detect_behavioral_signals(text: str, debit: float, credit: float) -> dict:
    """
    Return a dictionary of behavioral signals for ML feature engineering.
    Covers payment infrastructure, direction, and commercial semantics.
    """
    t         = text.lower()
    is_debit  = debit > 0 and credit == 0
    is_credit = credit > 0 and debit == 0
    amount    = debit if is_debit else credit

    return {
        "is_debit":            int(is_debit),
        "is_credit":           int(is_credit),
        "is_high_value":       int(amount > 1_00_000),
        "is_mid_value":        int(10_000 < amount <= 1_00_000),
        "is_low_value":        int(amount <= 10_000),
        "has_infra_signal":    int(_check_any(t, _PAYMENT_INFRA_SIGNALS)),
        "has_gateway_signal":  int(_check_any(t, _SETTLEMENT_SIGNALS)),
        "has_consumer_signal": int(_check_any(t, _CONSUMER_SIGNALS)),
        "has_tax_signal":      int(_check_any(t, _TAX_PAYMENT_SIGNALS)),
        "has_salary_signal":   int(any(s in t for s in _SALARY_SIGNALS)),
        "has_refund_signal":   int(_check_any(t, _REFUND_SIGNALS)),
        "has_subscription":    int(_check_any(t, _SUBSCRIPTION_SIGNALS)),
        "has_vendor_signal":   int(_check_any(t, _VENDOR_SIGNALS)),
        "has_escrow":          int("escrow" in t),
        "has_aggregator":      int("aggregator" in t),
        "has_merchant_id":     int("mid-" in t or "merchant settlement" in t),
    }


# ── Helpers ────────────────────────────────────────────────────────────────────

def _check_any(text: str, signals: list[str]) -> bool:
    """Return True if any signal string appears in text."""
    return any(s in text for s in signals)


def _which(text: str, signals: list[str]) -> str:
    """Return the first matching signal string for debug output."""
    for s in signals:
        if s in text:
            return repr(s)
    return ""
