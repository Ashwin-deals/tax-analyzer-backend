from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from utils.constants import CATEGORY_GST, CATEGORY_NORMAL, CATEGORY_POSSIBLE_GST, CATEGORY_TDS

VALID_TAX_CATEGORIES = {CATEGORY_GST, CATEGORY_POSSIBLE_GST, CATEGORY_TDS, CATEGORY_NORMAL}


@dataclass(frozen=True)
class ClassificationDecision:
    category: str
    confidence: str
    review_recommended: bool
    reason: str
    normalized_particulars: str
    classification_source: str
    final_override_applied: bool = False


_NARRATION_KEYS = (
    "_description",
    "narration",
    "NARRATION",
    "Narration",
    "description",
    "DESCRIPTION",
    "Description",
    "particulars",
    "PARTICULARS",
    "Particulars",
    "remarks",
    "REMARKS",
    "Remarks",
    "transaction details",
    "TRANSACTION DETAILS",
    "Transaction Details",
    "transaction remarks",
    "TRANSACTION REMARKS",
    "Transaction Remarks",
    "transaction_remarks",
    "TRANSACTION_REMARKS",
    "Transaction_Remarks",
    "transactionRemarks",
    "TransactionRemarks",
    "TRANSACTIONREMARKS",
    "_raw_row_text",
    "raw_row_text",
    "RAW_ROW_TEXT",
    "_raw_extracted_row",
    "raw_extracted_row",
    "RAW_EXTRACTED_ROW",
    "pdf_raw_row_text",
    "PDF_RAW_ROW_TEXT",
)
_DEBIT_KEYS = (
    "_debit",
    "debit",
    "DEBIT",
    "Debit",
    "withdrawal",
    "WITHDRAWAL",
    "Withdrawal",
    "withdrawal amt",
    "Withdrawal Amt",
    "debit amount",
    "Debit Amount",
    "dr amount",
    "Dr Amount",
)
_CREDIT_KEYS = (
    "_credit",
    "credit",
    "CREDIT",
    "Credit",
    "deposit",
    "DEPOSIT",
    "Deposit",
    "deposit amt",
    "Deposit Amt",
    "credit amount",
    "Credit Amount",
    "cr amount",
    "Cr Amount",
)

_EXPLICIT_GST_RE = re.compile(
    r"(?<![a-z0-9])(?:gst|cgst|sgst|igst|utgst|gstin)(?![a-z0-9])|\btax\s+invoice\b",
    re.IGNORECASE,
)
_EXPLICIT_TDS_RE = re.compile(
    r"(?<![a-z0-9])tds(?![a-z0-9])|\btax\s+deducted(?:\s+at\s+source)?\b",
    re.IGNORECASE,
)
_BANK_CHARGE_GST_RE = re.compile(
    "|".join(
        [
            r"\b(?:chg|charge|charges|fee|fees|jfee)\b",
            r"\bjoining\s+fee\b",
            r"\bcash\s+wdl\s+chg\b",
            r"\bneft\s+chg\b",
            r"\bmob\s+alrt\s+chg\b",
            r"\bbusiness\s+expression\s+jfee\b",
        ]
    ),
    re.IGNORECASE,
)
_SETTLEMENT_RE = re.compile(
    "|".join(
        [
            r"razorpay.*payment aggregator.*escrow account",
            r"\bcms_ift\b.*\bcard pmt\b.*\bmid\b",
            r"\bcard pmt\b.*\bmid\b",
            r"\bcard payment\b.*\bmid\b",
            r"\b(?:pos|edc|mpos)\b.*\b(?:card sales?|sales?|merchant)?\s*settlement\b",
            r"\b(?:card sales?|pos sales?|merchant|payment gateway)\s+settlement\b",
            r"\b(?:settlement|escrow account|payout|payment aggregator|merchant settlement)\b",
        ]
    ),
    re.IGNORECASE,
)
_GATEWAY_RE = re.compile(
    r"\b(?:razorpay|cashfree|payu|ccavenue|easebuzz|billdesk|instamojo|stripe|paypal|bharatpe)\b",
    re.IGNORECASE,
)
_SETTLEMENT_BLOCKER_RE = re.compile(
    "|".join(
        [
            r"\b(?:gst|cgst|sgst|igst|gstin|tds|tax)\b",
            r"\b(?:charge|charges|chg|fee|fees|jfee|commission|brokerage|mdr|msf)\b",
            r"\b(?:invoice|bill)\b",
            r"\b(?:platform|processing|merchant|bank|gateway|payment\s+gateway)\s+(?:fee|fees|charge|charges)\b",
        ]
    ),
    re.IGNORECASE,
)
_GENERIC_UPI_RE = re.compile(r"^\s*upi/(?:dr|cr)\b|\b(?:gpay|google pay|paytm|phonepe|p2p transfer)\b", re.IGNORECASE)
_UPI_TAX_CONTEXT_RE = re.compile(
    "|".join(
        [
            r"\b(?:gst|cgst|sgst|igst|gstin|tds|tax)\b",
            r"\b(?:charge|charges|chg|fee|fees|jfee|commission|brokerage|mdr|msf)\b",
            r"\b(?:invoice|bill|subscription|software|saas)\b",
            r"\bprofessional\s+services?\b",
            r"\bconsult(?:ing|ancy|ant)\b",
            r"\b(?:vendor|merchant|business)\s+services?\b",
            r"\b(?:office|commercial|warehouse|shop)\s+rent\b",
        ]
    ),
    re.IGNORECASE,
)
_POSSIBLE_GST_RE = re.compile(
    "|".join(
        [
            r"\b(?:fee|fees|charge|charges|chg|jfee|commission|brokerage|mdr|msf)\b",
            r"\b(?:platform|processing|merchant|bank|gateway|payment\s+gateway)\s+(?:fee|fees|charge|charges)\b",
            r"\b(?:invoice|bill|subscription|software|saas)\b",
            r"\b(?:service|services)\s+(?:charge|charges|fee|fees|invoice|bill)\b",
            r"\b(?:professional|vendor|merchant|business)\s+services?\b",
            r"\bconsult(?:ing|ancy|ant)\b",
            r"\b(?:office|commercial|warehouse|shop)\s+rent\b",
        ]
    ),
    re.IGNORECASE,
)
_CREDIT_TRANSFER_RE = re.compile(r"^(?:neft|rtgs|upi/cr|cms_)", re.IGNORECASE)
_CLASSIFICATION_METADATA_KEYS = {
    "tax_category",
    "tax category",
    "category",
    "classification",
    "confidence",
    "review_recommended",
    "review recommended",
    "reviewrecommended",
    "review_status",
    "review status",
    "reason",
    "ml_assist",
    "ml assist",
    "normalized_particulars",
    "normalized particulars",
    "classification_source",
    "classification source",
    "final_override_applied",
    "final override applied",
    "statement_id",
    "statementid",
    "transaction_id",
    "transactionid",
}


def normalize_particulars(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none"}:
        return ""
    text = re.sub(
        r"\b([A-Za-z])\.((?:[A-Za-z]\.)+[A-Za-z])\.?\b",
        lambda match: match.group(0).replace(".", ""),
        text,
    )
    text = re.sub(r"(?i)\bC\s*[\.\s]\s*G\s*[\.\s]\s*S\s*[\.\s]*T\b", "CGST", text)
    text = re.sub(r"(?i)\bS\s*[\.\s]\s*G\s*[\.\s]\s*S\s*[\.\s]*T\b", "SGST", text)
    text = re.sub(r"(?i)\bI\s*[\.\s]\s*G\s*[\.\s]\s*S\s*[\.\s]*T\b", "IGST", text)
    text = re.sub(r"(?i)\bG\s*[\.\s]\s*S\s*[\.\s]\s*T\s*[\.\s]\s*I\s*[\.\s]\s*N\b", "GSTIN", text)
    text = re.sub(r"(?i)\bG\s*[\.\s]\s*S\s*[\.\s]*T\b", "GST", text)
    text = re.sub(r"(?i)\+\s*GST\b", "+GST", text)
    text = re.sub(r"([A-Za-z])(\d)", r"\1 \2", text)
    text = re.sub(r"(\d)([A-Za-z])", r"\1 \2", text)
    return re.sub(r"\s+", " ", text.lower().strip())


def normalize_category(value: Any) -> str:
    text = re.sub(r"[\s\-]+", "_", str(value or "").strip().upper())
    if text == "POSSIBLEGST":
        text = CATEGORY_POSSIBLE_GST
    if text in {"POSSIBLE_GST", "GST_POSSIBLE"}:
        return CATEGORY_POSSIBLE_GST
    if text in VALID_TAX_CATEGORIES:
        return text
    return CATEGORY_NORMAL


def has_explicit_gst_signal(text: Any) -> bool:
    return _EXPLICIT_GST_RE.search(normalize_particulars(text)) is not None


def has_explicit_tds_signal(text: Any) -> bool:
    return _EXPLICIT_TDS_RE.search(normalize_particulars(text)) is not None


def _amount(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return 0.0 if value != value else float(value)
    cleaned = re.sub(r"[^\d.\-]", "", str(value).replace(",", ""))
    try:
        return float(cleaned) if cleaned else 0.0
    except ValueError:
        return 0.0


def _valid_text(value: Any) -> bool:
    if value is None:
        return False
    text = str(value).strip()
    return bool(text) and text.lower() not in {"nan", "nat", "none"}


def _first_value(row: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    lower_map = {str(key).lower().strip(): key for key in row.keys()}
    for key in keys:
        if key in row and _valid_text(row.get(key)):
            return row.get(key)
        mapped = lower_map.get(key.lower())
        if mapped is not None and _valid_text(row.get(mapped)):
            return row.get(mapped)

    source = row.get("source_row")
    if isinstance(source, Mapping):
        return _first_value(source, keys)
    return None


def _narration_values(row: Mapping[str, Any]) -> list[Any]:
    values: list[Any] = []
    lower_map = {str(key).lower().strip().replace("_", " "): key for key in row.keys()}

    for key in _NARRATION_KEYS:
        candidates = {key, key.lower(), key.lower().replace("_", " ")}
        for candidate in candidates:
            actual = row.get(candidate) if candidate in row else row.get(lower_map.get(candidate.lower().replace("_", " "), ""))
            if _valid_text(actual):
                values.append(actual)

    raw_values = [
        value
        for key, value in row.items()
        if str(key).strip().lower().replace("_", " ") not in _CLASSIFICATION_METADATA_KEYS and _valid_text(value)
    ]
    if raw_values:
        values.append(" ".join(str(value) for value in raw_values))

    source = row.get("source_row")
    if isinstance(source, Mapping):
        values.extend(_narration_values(source))

    deduped: list[Any] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        marker = text.lower()
        if marker not in seen:
            seen.add(marker)
            deduped.append(value)
    return deduped


def narration_from_row(row: Mapping[str, Any]) -> str:
    return normalize_particulars(" ".join(str(value) for value in _narration_values(row)))


def debit_from_row(row: Mapping[str, Any]) -> float:
    return _amount(_first_value(row, _DEBIT_KEYS) if isinstance(row, Mapping) else 0)


def credit_from_row(row: Mapping[str, Any]) -> float:
    return _amount(_first_value(row, _CREDIT_KEYS) if isinstance(row, Mapping) else 0)


def decide_classification(
    narration: Any,
    debit: Any = 0,
    credit: Any = 0,
    *,
    final_stage: bool = False,
) -> ClassificationDecision:
    text = normalize_particulars(narration)
    debit_amount = _amount(debit)
    credit_amount = _amount(credit)

    if _EXPLICIT_GST_RE.search(text):
        return ClassificationDecision(
            category=CATEGORY_GST,
            confidence="HIGH",
            review_recommended=False,
            reason=(
                "Priority 1: explicit GST on bank charge/fee"
                if _BANK_CHARGE_GST_RE.search(text)
                else "Priority 1: explicit GST text detected"
            ),
            normalized_particulars=text,
            classification_source="final_override:explicit_gst" if final_stage else "priority_1_explicit_gst",
            final_override_applied=final_stage,
        )

    if _EXPLICIT_TDS_RE.search(text):
        return ClassificationDecision(
            category=CATEGORY_TDS,
            confidence="HIGH",
            review_recommended=False,
            reason="Priority 2: explicit TDS text detected",
            normalized_particulars=text,
            classification_source="priority_2_explicit_tds",
        )

    if _is_settlement_exclusion(text, debit_amount, credit_amount):
        return ClassificationDecision(
            category=CATEGORY_NORMAL,
            confidence="HIGH",
            review_recommended=False,
            reason="Priority 3: settlement exclusion without tax/fee/charge signal",
            normalized_particulars=text,
            classification_source="priority_3_settlement_exclusion",
        )

    if _is_generic_upi_exclusion(text):
        return ClassificationDecision(
            category=CATEGORY_NORMAL,
            confidence="HIGH",
            review_recommended=False,
            reason="Priority 3: generic UPI/payment exclusion without tax signal",
            normalized_particulars=text,
            classification_source="priority_3_generic_payment_exclusion",
        )

    if _POSSIBLE_GST_RE.search(text):
        return ClassificationDecision(
            category=CATEGORY_POSSIBLE_GST,
            confidence="MEDIUM",
            review_recommended=True,
            reason="Priority 4: fee/charge/service/commission/invoice signal without explicit GST",
            normalized_particulars=text,
            classification_source="priority_4_possible_gst",
        )

    return ClassificationDecision(
        category=CATEGORY_NORMAL,
        confidence="HIGH",
        review_recommended=False,
        reason="Priority 5: no tax signal detected",
        normalized_particulars=text,
        classification_source="priority_5_default_normal",
    )


def decide_row_classification(row: Mapping[str, Any], *, final_stage: bool = False) -> ClassificationDecision:
    return decide_classification(
        narration_from_row(row),
        debit_from_row(row),
        credit_from_row(row),
        final_stage=final_stage,
    )


def apply_display_classification_guard(
    row: Mapping[str, Any],
    *,
    statement_id: str | None = None,
    final_stage: bool = True,
) -> dict[str, Any]:
    guarded = dict(row)
    decision = decide_row_classification(guarded, final_stage=final_stage)
    guarded["TAX_CATEGORY"] = decision.category
    guarded["CONFIDENCE"] = decision.confidence
    guarded["REVIEW_RECOMMENDED"] = decision.review_recommended
    guarded["REASON"] = decision.reason
    guarded["normalized_particulars"] = decision.normalized_particulars
    guarded["classification_source"] = decision.classification_source
    guarded["final_override_applied"] = decision.final_override_applied
    guarded["statement_id"] = statement_id or guarded.get("statement_id") or guarded.get("statementId") or ""
    return guarded


def apply_stored_classification_guard(
    row: Mapping[str, Any],
    *,
    statement_id: str | None = None,
    final_stage: bool = True,
) -> dict[str, Any]:
    guarded = dict(row)
    decision = decide_row_classification(guarded, final_stage=final_stage)
    guarded["classification"] = decision.category
    guarded["confidence"] = decision.confidence
    guarded["review_status"] = "pending" if decision.review_recommended else "cleared"
    guarded["review_recommended"] = decision.review_recommended
    guarded["reason"] = decision.reason
    guarded["normalized_particulars"] = decision.normalized_particulars
    guarded["classification_source"] = decision.classification_source
    guarded["final_override_applied"] = decision.final_override_applied
    guarded["statement_id"] = statement_id or guarded.get("statement_id") or ""

    source_row = dict(guarded.get("source_row") or {})
    if source_row:
        source_row["TAX_CATEGORY"] = decision.category
        source_row["CONFIDENCE"] = decision.confidence
        source_row["REVIEW_RECOMMENDED"] = decision.review_recommended
        source_row["REASON"] = decision.reason
        source_row["normalized_particulars"] = decision.normalized_particulars
        source_row["classification_source"] = decision.classification_source
        source_row["final_override_applied"] = decision.final_override_applied
        source_row["statement_id"] = guarded["statement_id"]
        guarded["source_row"] = source_row
    return guarded


def _is_generic_upi_exclusion(text: str) -> bool:
    if not text or _GENERIC_UPI_RE.search(text) is None:
        return False
    return _UPI_TAX_CONTEXT_RE.search(text) is None


def _is_settlement_exclusion(text: str, debit: float, credit: float) -> bool:
    if not text or _SETTLEMENT_BLOCKER_RE.search(text):
        return False

    is_credit = credit > 0 and debit == 0
    has_credit_transfer_prefix = _CREDIT_TRANSFER_RE.search(text) is not None
    has_settlement = _SETTLEMENT_RE.search(text) is not None
    has_gateway = _GATEWAY_RE.search(text) is not None
    is_card_mid = re.search(r"\b(?:cms_ift\s+)?card pmt\b.*\bmid\b", text) is not None
    is_card_payment_mid = re.search(r"\bcard payment\b.*\bmid\b", text) is not None

    if is_card_mid or is_card_payment_mid:
        return True
    if has_gateway and has_settlement:
        return True
    return has_settlement and (is_credit or has_credit_transfer_prefix)
