"""
src/processor.py
────────────────
Orchestrates scoring + transformation after loading.

Applies the multi-signal scorer to every row, adds TAX_CATEGORY / CONFIDENCE /
REVIEW_RECOMMENDED columns, drops internal alias columns before export, and
splits into per-tax-category DataFrames.
"""

import logging

import pandas as pd

from src.scorer import score_transaction
from utils.constants import (
    CATEGORY_GST, CATEGORY_NORMAL, CATEGORY_TDS, CATEGORY_POSSIBLE_GST,
    TAX_CATEGORY_ORDER,
    INTERNAL_COLS,
)
from utils.classification_rules import apply_display_classification_guard
from src.ml_pipeline import append_to_training_data

logger = logging.getLogger(__name__)


_DEBUG_NARRATION_COLUMNS = (
    "_description",
    "particulars",
    "Particulars",
    "PARTICULARS",
    "narration",
    "Narration",
    "NARRATION",
    "remarks",
    "Remarks",
    "REMARKS",
    "description",
    "Description",
    "DESCRIPTION",
    "transaction_remarks",
    "Transaction Remarks",
    "TRANSACTION REMARKS",
    "_raw_row_text",
)


def process_transactions(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """
    Score and classify all transactions. Returns a dict keyed by category.

    Keys: 'GST', 'POSSIBLE_GST', 'TDS', 'NORMAL'.
    Each value is a DataFrame with original columns + TAX_CATEGORY, CONFIDENCE,
    REVIEW_RECOMMENDED. Internal alias columns are dropped.
    """
    if df.empty:
        logger.warning("Input DataFrame is empty — nothing to process.")
        empty = pd.DataFrame()
        return {k: empty for k in TAX_CATEGORY_ORDER}

    logger.info("Scoring %d transactions …", len(df))

    # ── Apply scorer row by row ───────────────────────────────────────────────
    score_results = df.apply(score_transaction, axis=1)

    df = df.copy()
    df["TAX_CATEGORY"]       = [r.category            for r in score_results]
    df["CONFIDENCE"]         = [r.confidence          for r in score_results]
    df["REVIEW_RECOMMENDED"] = [r.needs_review        for r in score_results]
    df["ML_ASSIST"]          = [f"{r.ml_model_confidence:.2%}" if r.ml_assist_used else "N/A" for r in score_results]
    df["REASON"]             = [r.reason              for r in score_results]

    # Final deterministic guard, applied after heuristic/ML scoring and before
    # training export, cache storage, MongoDB writes, API responses, or exports.
    guarded_rows = [
        apply_display_classification_guard(row.to_dict(), final_stage=True)
        for _, row in df.iterrows()
    ]
    _log_pdf_classification_debug(df, guarded_rows)
    for col in (
        "TAX_CATEGORY",
        "CONFIDENCE",
        "REVIEW_RECOMMENDED",
        "REASON",
        "normalized_particulars",
        "classification_source",
        "final_override_applied",
        "statement_id",
    ):
        df[col] = [row.get(col) for row in guarded_rows]

    # ── Export to ML Training Dataset ─────────────────────────────────────────
    # We do this BEFORE dropping internal alias columns because ml_pipeline needs them
    append_to_training_data(df, list(score_results))

    # ── Drop internal alias columns before export ──────────────────────────────
    cols_to_drop = [c for c in INTERNAL_COLS if c in df.columns]
    df.drop(columns=cols_to_drop, inplace=True)

    # ── Log breakdown ─────────────────────────────────────────────────────────
    counts = df["TAX_CATEGORY"].value_counts().to_dict()
    review_count = int(df["REVIEW_RECOMMENDED"].sum())
    logger.info("Classification: %s | Needs review: %d", counts, review_count)

    # ── Split ─────────────────────────────────────────────────────────────────
    result = {
        CATEGORY_GST:          _filter(df, CATEGORY_GST),
        CATEGORY_POSSIBLE_GST: _filter(df, CATEGORY_POSSIBLE_GST),
        CATEGORY_TDS:          _filter(df, CATEGORY_TDS),
        CATEGORY_NORMAL:       _filter(df, CATEGORY_NORMAL),
    }

    logger.info(
        "Split — GST: %d, POSSIBLE_GST: %d, TDS: %d, NORMAL: %d",
        len(result[CATEGORY_GST]), len(result[CATEGORY_POSSIBLE_GST]),
        len(result[CATEGORY_TDS]), len(result[CATEGORY_NORMAL]),
    )
    return result


def _log_pdf_classification_debug(df: pd.DataFrame, guarded_rows: list[dict]) -> None:
    for index, (_, original_row) in enumerate(df.iterrows()):
        row_dict = original_row.to_dict()
        if str(row_dict.get("_source_format") or "").lower() != "pdf" and not row_dict.get("_raw_row_text"):
            continue
        narration_fields = {
            key: row_dict.get(key)
            for key in _DEBUG_NARRATION_COLUMNS
            if key in row_dict and pd.notna(row_dict.get(key)) and str(row_dict.get(key)).strip()
        }
        guarded = guarded_rows[index]
        logger.info(
            "FinScan PDF classification debug row=%s raw_extracted_row=%s narration_fields=%s normalized_narration=%s tax_category=%s confidence=%s review_recommended=%s",
            row_dict.get("_pdf_row_index", index + 1),
            row_dict.get("_raw_extracted_row") or row_dict.get("_raw_row_text") or row_dict,
            narration_fields,
            guarded.get("normalized_particulars"),
            guarded.get("TAX_CATEGORY"),
            guarded.get("CONFIDENCE"),
            guarded.get("REVIEW_RECOMMENDED"),
        )


def _filter(df: pd.DataFrame, category: str) -> pd.DataFrame:
    out = df[df["TAX_CATEGORY"] == category].reset_index(drop=True)
    if "ML_ASSIST" in out.columns and not out["ML_ASSIST"].replace("N/A", pd.NA).dropna().any():
        out = out.drop(columns=["ML_ASSIST"])
    return out
