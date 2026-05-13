"""
src/features.py
───────────────
ML feature engineering utilities for the hybrid transaction intelligence system.

Extended to include behavioral flow signals from the Context-Aware Financial
Flow Intelligence Layer.
"""

import re
import math
import numpy as np
import pandas as pd
from typing import Optional

from src.flow_classifier import detect_behavioral_signals


def extract_vendor(narration: str) -> str:
    """
    Utility for vendor extraction from normalized text.
    """
    # Try common UPI patterns
    m = re.search(r"upi/dr/[^/]+/([^/]+)/", narration, re.IGNORECASE)
    if m:
        return m.group(1).upper()

    m = re.search(r"upi/cr/[^/]+/([^/]+)/", narration, re.IGNORECASE)
    if m:
        return m.group(1).upper()

    # Try known gateways
    m = re.search(
        r"(razorpay|bharatpe|cashfree|payu|swiggy|zomato|amazon|paytm|cms_ift)",
        narration, re.IGNORECASE
    )
    if m:
        return m.group(1).upper()

    # Fallback to first word chunk
    parts = narration.split()
    return parts[0].upper() if parts else ""


def extract_amount_features(debit: float, credit: float) -> dict:
    """
    Return structured numeric features for amount patterns.
    Used in ML feature pipelines.
    """
    amount = debit if debit > 0 else credit
    is_debit = debit > 0 and credit == 0
    is_credit = credit > 0 and debit == 0
    is_round = (amount % 100 == 0) if amount > 0 else True

    return {
        "amount":          amount,
        "log_amount":      math.log1p(amount),
        "is_debit":        int(is_debit),
        "is_credit":       int(is_credit),
        "is_round_amount": int(is_round),
        "is_high_value":   int(amount > 1_00_000),
        "is_mid_value":    int(10_000 < amount <= 1_00_000),
        "is_low_value":    int(amount <= 10_000),
        "is_above_flag":   int(amount >= 50_000),
    }


def extract_heuristic_features(gst_score: float, tds_score: float,
                                confidence: str, mode: str) -> dict:
    """
    Convert heuristic scoring outputs into ML-compatible numeric features.
    """
    conf_map = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    mode_map = {"EXPLICIT": 3, "LEARNED": 2, "HEURISTIC": 1, "ML_ASSISTED": 0}

    return {
        "gst_score":    gst_score,
        "tds_score":    tds_score,
        "score_ratio":  gst_score / (tds_score + 1),
        "confidence_n": conf_map.get(confidence, 1),
        "mode_n":       mode_map.get(mode, 1),
    }


def extract_flow_features(flow_type: str, flow_confidence: str,
                          is_commercial: bool = False) -> dict:
    """
    Encode FLOW_TYPE, flow confidence, and commercial flag as ML numeric features.
    Allows the ML model to learn from behavioral context.
    """
    flow_map = {
        "SETTLEMENT":         1,
        "BUSINESS":           2,
        "CONSUMER":           3,
        "TAX":                4,
        "TRANSFER":           5,
        "SUBSCRIPTION":       6,
        "UNKNOWN":            0,
        # Legacy aliases from the previous overloaded flow vocabulary.
        "REVENUE_SETTLEMENT": 1,
        "BUSINESS_EXPENSE":   2,
        "VENDOR_PAYMENT":     2,
        "CONSUMER_EXPENSE":   3,
        "TAX_PAYMENT":        4,
        "REFUND":             5,
        "INTERNAL_TRANSFER":  5,
        "SALARY":             5,
    }
    conf_map = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}

    return {
        "flow_type_n":       flow_map.get(flow_type, 0),
        "flow_confidence_n": conf_map.get(flow_confidence, 1),
        "is_commercial":     int(is_commercial),
        # Boolean flags for the most discriminative flow types
        "is_revenue_flow":   int(flow_type in ("SETTLEMENT", "REVENUE_SETTLEMENT")),
        "is_consumer_flow":  int(flow_type in ("CONSUMER", "CONSUMER_EXPENSE")),
        "is_tax_flow":       int(flow_type in ("TAX", "TAX_PAYMENT")),
        "is_vendor_flow":    int(flow_type in ("BUSINESS", "VENDOR_PAYMENT")),
        "is_subscription":   int(flow_type == "SUBSCRIPTION"),
        "is_internal":       int(flow_type in ("TRANSFER", "INTERNAL_TRANSFER", "SALARY", "REFUND")),
        "is_business_exp":   int(flow_type in ("BUSINESS", "BUSINESS_EXPENSE")),
    }


def build_ml_features(narration: str, debit: float, credit: float,
                       gst_score: float = 0, tds_score: float = 0,
                       confidence: str = "LOW", mode: str = "HEURISTIC",
                       flow_type: str = "UNKNOWN",
                       flow_confidence: str = "LOW",
                       is_commercial: bool = False) -> dict:
    """
    Combine all feature extractors into one unified feature dict.
    Ready for use by XGBoost / LightGBM / sklearn models.
    """
    features = {}
    features.update(extract_amount_features(debit, credit))
    features.update(extract_heuristic_features(gst_score, tds_score, confidence, mode))
    features.update(extract_flow_features(flow_type, flow_confidence, is_commercial))
    features.update(detect_behavioral_signals(narration, debit, credit))
    return features
