import csv
import logging
from pathlib import Path
import pandas as pd

logger = logging.getLogger(__name__)

TRAINING_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "training_dataset.csv"
TRAINING_DATA_COLS = [
    "narration",
    "normalized_text",
    "vendor",
    "transaction_type",
    "debit",
    "credit",
    "gst_score",
    "tds_score",
    "classification_mode",
    "predicted_category",
    "actual_category",
    "confidence",
    "review_recommended",
    "reason"
]

def append_to_training_data(df: pd.DataFrame, score_results: list):
    """
    Append processed transactions to the training dataset.
    This preserves the baseline predictions for future ML training.
    """
    if df.empty or not score_results:
        return

    # Ensure the file and headers exist
    file_exists = TRAINING_DATA_PATH.exists()
    
    rows = []
    # Internal cols have already been dropped or we just use original DF
    # We need the original narration. Let's assume the processor passes it.
    from utils.constants import INTERNAL_DESCRIPTION_COL, INTERNAL_DEBIT_COL, INTERNAL_CREDIT_COL
    
    for idx, row in df.iterrows():
        res = score_results[idx]
        
        narration = str(row.get(INTERNAL_DESCRIPTION_COL, ""))
        
        # If the df has an Actual_Category (e.g. from evaluation_sample), we can populate actual_category
        # Otherwise, actual_category is empty until user corrects it.
        actual_cat = str(row.get("Actual_Category", ""))
        
        row_dict = {
            "narration": narration,
            "normalized_text": res.normalized_text,
            "vendor": res.vendor,
            "transaction_type": res.transaction_type,
            "debit": row.get(INTERNAL_DEBIT_COL, 0.0),
            "credit": row.get(INTERNAL_CREDIT_COL, 0.0),
            "gst_score": res.gst_score,
            "tds_score": res.tds_score,
            "classification_mode": res.classification_mode,
            "predicted_category": res.category,
            "actual_category": actual_cat,
            "confidence": res.confidence,
            "review_recommended": str(res.needs_review),
            "reason": res.reason
        }
        rows.append(row_dict)

    with TRAINING_DATA_PATH.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRAINING_DATA_COLS)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)
    logger.debug("Appended %d rows to training_dataset.csv", len(rows))

def log_user_correction(vendor_pattern: str, corrected_category: str):
    """
    When a user submits a correction, append it as a targeted training row.
    """
    file_exists = TRAINING_DATA_PATH.exists()
    
    row_dict = {
        "narration": vendor_pattern,
        "normalized_text": vendor_pattern.lower(),
        "vendor": vendor_pattern.upper(),
        "transaction_type": "USER_CORRECTION",
        "debit": 0.0,
        "credit": 0.0,
        "gst_score": 0,
        "tds_score": 0,
        "classification_mode": "LEARNED",
        "predicted_category": "",
        "actual_category": corrected_category,
        "confidence": "HIGH",
        "review_recommended": "False",
        "reason": "Explicit user feedback correction"
    }

    with TRAINING_DATA_PATH.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRAINING_DATA_COLS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row_dict)
    logger.debug("Logged user correction for vendor '%s' to training_dataset.csv", vendor_pattern)
