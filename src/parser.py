"""
src/parser.py
─────────────
Extracts a structured transaction type from the narration prefix. (Fix #5)

Centralises all prefix-based parsing so the scorer and direction-awareness
logic share one source of truth instead of duplicating regex across files.
"""

import re
from enum import Enum


class TxType(str, Enum):
    UPI_CREDIT     = "UPI_CREDIT"     # incoming UPI
    UPI_DEBIT      = "UPI_DEBIT"      # outgoing UPI
    BULK_NEFT      = "BULK_NEFT"      # BLKNEFT — strong TDS signal
    NEFT           = "NEFT"           # regular NEFT
    RTGS           = "RTGS"           # large transfer
    CMS            = "CMS"            # CMS — merchant/GST signal
    IMPS           = "IMPS"           # standard IMPS transfer
    ATM_WITHDRAWAL = "ATM_WITHDRAWAL" # always NORMAL
    CARD_PAYMENT   = "CARD_PAYMENT"   # GST signal
    UNKNOWN        = "UNKNOWN"


# Precompiled patterns — order matters (most specific first). (Fix #10)
_PATTERNS: list[tuple[re.Pattern, TxType]] = [
    (re.compile(r"^upi/cr/",                         re.IGNORECASE), TxType.UPI_CREDIT),
    (re.compile(r"^upi/dr/",                         re.IGNORECASE), TxType.UPI_DEBIT),
    (re.compile(r"^upi/",                            re.IGNORECASE), TxType.UPI_DEBIT),
    (re.compile(r"^blkneft/",                        re.IGNORECASE), TxType.BULK_NEFT),
    (re.compile(r"^neft/",                           re.IGNORECASE), TxType.NEFT),
    (re.compile(r"^rtgs/",                           re.IGNORECASE), TxType.RTGS),
    (re.compile(r"^cms_",                            re.IGNORECASE), TxType.CMS),
    (re.compile(r"^imps[-/]",                        re.IGNORECASE), TxType.IMPS),
    (re.compile(r"^atm[/ ]|^atm$",                  re.IGNORECASE), TxType.ATM_WITHDRAWAL),
    (re.compile(r"card pmt|card payment|/pos/|^pos ", re.IGNORECASE), TxType.CARD_PAYMENT),
]


def parse_transaction_type(narration: str) -> TxType:
    """Return the TxType for a given (already-lowercased) narration string."""
    if not narration:
        return TxType.UNKNOWN
    for pattern, tx_type in _PATTERNS:
        if pattern.search(narration):
            return tx_type
    return TxType.UNKNOWN
