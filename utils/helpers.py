"""
utils/helpers.py
────────────────
Shared utility functions used across all modules.
"""

import re
import logging
import pandas as pd

from utils.constants import (
    DESCRIPTION_COLUMN_CANDIDATES,
    DEBIT_COLUMN_CANDIDATES,
    CREDIT_COLUMN_CANDIDATES,
    DATE_COLUMN_CANDIDATES,
    INTERNAL_DESCRIPTION_COL,
    INTERNAL_DEBIT_COL,
    INTERNAL_CREDIT_COL,
    INTERNAL_DATE_COL,
    TAX_CATEGORY_ORDER,
)

logger = logging.getLogger(__name__)


# ── Text normalisation ────────────────────────────────────────────────────────

def normalize_text(text) -> str:
    """Lowercase, strip, collapse whitespace, and normalise abbreviation formats.

    Pre-processing steps (applied before keyword matching):
      - T.D.S  → tds       (dotted abbreviations)
      - G.S.T  → gst
      - TDSPMT → tds pmt   (camel/run-together → space-separated)
    """
    if text is None or (isinstance(text, float) and pd.isna(text)):
        return ""
    t = str(text).strip()
    # Fix #4a — Remove dots between single uppercase letters: T.D.S → TDS
    t = re.sub(r'\b([A-Za-z])\.((?:[A-Za-z]\.)+[A-Za-z])\b',
                lambda m: m.group(0).replace('.', ''), t)
    # Fix #4b — Insert space at letter→digit and digit→letter boundaries
    t = re.sub(r'([A-Za-z])(\d)', r'\1 \2', t)
    t = re.sub(r'(\d)([A-Za-z])', r'\1 \2', t)
    return re.sub(r'\s+', ' ', t.lower().strip())


# ── Column detection ──────────────────────────────────────────────────────────

def detect_description_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    """
    Return the first matching column name (case-insensitive) or None.

    Two-pass strategy:
      1. Exact match (normalised lower-strip)
      2. Substring match: candidate appears inside column name
         e.g. candidate='withdrawal' matches column 'Withdra wal (Dr)' after
         stripping spaces from both sides.
    """
    lower_map = {c.lower().strip(): c for c in df.columns}

    # Pass 1: exact match
    for candidate in candidates:
        if candidate.lower() in lower_map:
            return lower_map[candidate.lower()]

    # Pass 2: candidate is a substring of the column name (after removing spaces)
    # This handles typo-space columns like 'Withdra wal (Dr)' matching 'withdrawal'
    for candidate in candidates:
        cand_nospace = candidate.lower().replace(" ", "")
        for col_lower, col_original in lower_map.items():
            col_nospace = col_lower.replace(" ", "")
            if cand_nospace in col_nospace:
                return col_original

    return None


# ── Column normalisation (Fix #1) ─────────────────────────────────────────────

def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add internal alias columns (_description, _debit, _credit, _date) to df.

    Original columns are untouched. Internal columns are used only by the
    scorer/classifier and are dropped before export.

    Returns the modified DataFrame (operates on a copy).
    """
    df = df.copy()
    lower_map = {c.lower().strip(): c for c in df.columns}

    def _add_alias(candidates: list[str], alias: str) -> str | None:
        for candidate in candidates:
            if candidate.lower() in lower_map:
                original = lower_map[candidate.lower()]
                df[alias] = df[original]
                logger.debug("Mapped '%s' → '%s'", original, alias)
                return original
        df[alias] = None
        logger.debug("No column found for alias '%s'; set to None.", alias)
        return None

    found = {
        "description": _add_alias(DESCRIPTION_COLUMN_CANDIDATES, INTERNAL_DESCRIPTION_COL),
        "debit":       _add_alias(DEBIT_COLUMN_CANDIDATES,       INTERNAL_DEBIT_COL),
        "credit":      _add_alias(CREDIT_COLUMN_CANDIDATES,      INTERNAL_CREDIT_COL),
        "date":        _add_alias(DATE_COLUMN_CANDIDATES,        INTERNAL_DATE_COL),
    }

    logger.info(
        "Column mapping — description: %s | debit: %s | credit: %s | date: %s",
        found["description"], found["debit"], found["credit"], found["date"],
    )
    return df


# ── Numeric coercion ──────────────────────────────────────────────────────────

def safe_numeric(series: pd.Series) -> pd.Series:
    """Coerce column to numeric, replacing un-parseable values with 0."""
    return pd.to_numeric(series, errors="coerce").fillna(0)


# ── Summary builder ───────────────────────────────────────────────────────────

def build_summary(classified_df: pd.DataFrame) -> pd.DataFrame:
    """Build a per-category summary with counts, totals, and review flags."""
    rows = []
    order = {category: idx for idx, category in enumerate(TAX_CATEGORY_ORDER)}
    if "TAX_CATEGORY" in classified_df.columns:
        category_col = "TAX_CATEGORY"
    else:
        category_col = "Tax_Category" if "Tax_Category" in classified_df.columns else "Category"

    if "REVIEW_RECOMMENDED" in classified_df.columns:
        review_col = "REVIEW_RECOMMENDED"
    else:
        review_col = "Review_Recommended" if "Review_Recommended" in classified_df.columns else "Needs_Review"

    for category, group in classified_df.groupby(category_col, sort=False):
        row: dict = {
            "Tax Category":       category,
            "Transaction Count":  len(group),
            "Review Count":       int(group.get(review_col, pd.Series([False] * len(group))).sum()),
        }
        for col in ["Debit", "Credit", "Amount", "Withdrawal Amt.", "Deposit Amt."]:
            matched = [c for c in group.columns if c.strip().lower() == col.strip().lower()]
            if matched:
                row[f"Total {matched[0]}"] = safe_numeric(group[matched[0]]).sum()
        rows.append(row)

    summary_df = pd.DataFrame(rows)
    summary_df["_order"] = summary_df["Tax Category"].map(order).fillna(99)
    summary_df = summary_df.sort_values("_order").drop(columns=["_order"])
    return summary_df.reset_index(drop=True)
