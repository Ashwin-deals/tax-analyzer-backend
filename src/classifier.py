"""
src/classifier.py
─────────────────
Thin wrapper around the scorer for single-transaction classification.
Kept for backward compatibility and standalone unit testing.

For bulk DataFrame classification, use scorer.score_transaction() directly
via processor.py.
"""

from src.scorer import score_transaction
from utils.helpers import normalize_text
import pandas as pd

def classify_transaction(text: str) -> str:
    """
    Classify a single narration string → GST, POSSIBLE_GST, TDS, or NORMAL.
    Uses the full scoring engine (direction-neutral; no debit/credit context).
    """
    row = pd.Series({
        "_description": text,
        "_debit":  0.0,
        "_credit": 0.0,
        "_date":   None,
    })
    return score_transaction(row).category
    
