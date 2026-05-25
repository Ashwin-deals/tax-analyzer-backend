import unittest

import pandas as pd

from app.services import analysis_service
from app.services.database import _normalise_transaction_doc
from src import processor
from utils.constants import CATEGORY_GST, CATEGORY_NORMAL, CATEGORY_POSSIBLE_GST, CATEGORY_TDS, TAX_CATEGORY_ORDER


def _sample_frame():
    return pd.DataFrame(
        [
            {
                "Date": "2026-05-25",
                "Narration": "Mob alrt Chg Mar-25+GST",
                "Debit": 10,
                "Credit": 0,
                "_date": "2026-05-25",
                "_description": "Mob alrt Chg Mar-25+GST",
                "_debit": 10,
                "_credit": 0,
            },
            {
                "Date": "2026-05-25",
                "Narration": "CMS_IFT CARD PMT MID-90096587 SETDT-21032026IDFC",
                "Debit": 0,
                "Credit": 1000,
                "_date": "2026-05-25",
                "_description": "CMS_IFT CARD PMT MID-90096587 SETDT-21032026IDFC",
                "_debit": 0,
                "_credit": 1000,
            },
            {
                "Date": "2026-05-25",
                "Narration": "Razorpay platform fee",
                "Debit": 50,
                "Credit": 0,
                "_date": "2026-05-25",
                "_description": "Razorpay platform fee",
                "_debit": 50,
                "_credit": 0,
            },
            {
                "Date": "2026-05-25",
                "Narration": "UPI/DR/123/gpay/Payment",
                "Debit": 25,
                "Credit": 0,
                "_date": "2026-05-25",
                "_description": "UPI/DR/123/gpay/Payment",
                "_debit": 25,
                "_credit": 0,
            },
        ]
    )


class FinalClassificationGuardTest(unittest.TestCase):
    def setUp(self):
        with analysis_service._STORE_LOCK:
            analysis_service._ANALYSIS_CACHE.clear()

    def tearDown(self):
        with analysis_service._STORE_LOCK:
            analysis_service._ANALYSIS_CACHE.clear()

    def test_stale_cache_is_guarded_before_filters_and_summary(self):
        statement_id = "stmt-cache-guard"
        stale_row = {
            "Date": "2026-05-25",
            "Narration": "Mob alrt Chg Mar-25+GST",
            "Debit": 10,
            "Credit": 0,
            "TAX_CATEGORY": CATEGORY_NORMAL,
            "CONFIDENCE": "HIGH",
            "REVIEW_RECOMMENDED": False,
        }
        with analysis_service._STORE_LOCK:
            analysis_service._ANALYSIS_CACHE[statement_id] = {
                "statementId": statement_id,
                "businessId": "biz-1",
                "filename": "statement.csv",
                "status": "analyzed",
                "summary": {
                    "totalTransactions": 1,
                    "categoryCounts": {CATEGORY_GST: 0, CATEGORY_POSSIBLE_GST: 0, CATEGORY_TDS: 0, CATEGORY_NORMAL: 1},
                    "confidenceCounts": {"HIGH": 1},
                    "reviewTotal": 0,
                    "amountTotals": {"debit": 10, "credit": 0, "net": -10},
                },
                "transactions": {
                    CATEGORY_GST: [],
                    CATEGORY_POSSIBLE_GST: [],
                    CATEGORY_TDS: [],
                    CATEGORY_NORMAL: [stale_row],
                },
                "columns": {category: list(stale_row.keys()) for category in TAX_CATEGORY_ORDER},
            }

        gst_rows = analysis_service.get_transactions(statement_id, category=CATEGORY_GST)["transactions"]
        lowercase_gst_rows = analysis_service.get_transactions(statement_id, category="gst")["transactions"]
        normal_rows = analysis_service.get_transactions(statement_id, category=CATEGORY_NORMAL)["transactions"]
        summary = analysis_service.get_summary(statement_id)["summary"]

        self.assertEqual(len(gst_rows), 1)
        self.assertEqual(len(lowercase_gst_rows), 1)
        self.assertEqual(len(normal_rows), 0)
        self.assertEqual(gst_rows[0]["TAX_CATEGORY"], CATEGORY_GST)
        self.assertEqual(gst_rows[0]["CONFIDENCE"], "HIGH")
        self.assertFalse(gst_rows[0]["REVIEW_RECOMMENDED"])
        self.assertTrue(gst_rows[0]["final_override_applied"])
        self.assertEqual(gst_rows[0]["statement_id"], statement_id)
        self.assertEqual(summary["categoryCounts"][CATEGORY_GST], 1)
        self.assertEqual(summary["categoryCounts"][CATEGORY_NORMAL], 0)

    def test_final_override_checks_all_narration_fields(self):
        statement_id = "stmt-cache-remarks-guard"
        stale_row = {
            "Date": "2026-05-25",
            "Narration": "Bank service alert",
            "transaction_remarks": "Mob alrt Chg Mar-25+GST",
            "Debit": 10,
            "Credit": 0,
            "TAX_CATEGORY": CATEGORY_NORMAL,
            "CONFIDENCE": "HIGH",
            "REVIEW_RECOMMENDED": False,
        }
        with analysis_service._STORE_LOCK:
            analysis_service._ANALYSIS_CACHE[statement_id] = {
                "statementId": statement_id,
                "businessId": "biz-1",
                "filename": "statement.csv",
                "status": "analyzed",
                "summary": {},
                "transactions": {
                    CATEGORY_GST: [],
                    CATEGORY_POSSIBLE_GST: [],
                    CATEGORY_TDS: [],
                    CATEGORY_NORMAL: [stale_row],
                },
                "columns": {category: list(stale_row.keys()) for category in TAX_CATEGORY_ORDER},
            }

        gst_rows = analysis_service.get_transactions(statement_id, category=CATEGORY_GST)["transactions"]
        normal_rows = analysis_service.get_transactions(statement_id, category=CATEGORY_NORMAL)["transactions"]

        self.assertEqual(len(gst_rows), 1)
        self.assertEqual(len(normal_rows), 0)
        self.assertEqual(gst_rows[0]["TAX_CATEGORY"], CATEGORY_GST)
        self.assertEqual(gst_rows[0]["CONFIDENCE"], "HIGH")
        self.assertFalse(gst_rows[0]["REVIEW_RECOMMENDED"])
        self.assertTrue(gst_rows[0]["final_override_applied"])

    def test_pdf_raw_row_text_handles_broken_gst_spacing(self):
        row = {
            "_source_format": "pdf",
            "Narration": "Mob alrt Chg Apr-25",
            "Debit": 10,
            "Credit": 0,
            "_raw_row_text": "25 Apr | Mob alrt Chg Apr-25 + G ST | 10.00",
            "TAX_CATEGORY": CATEGORY_NORMAL,
            "CONFIDENCE": "HIGH",
            "REVIEW_RECOMMENDED": False,
        }

        guarded = analysis_service.apply_display_classification_guard(row, statement_id="stmt-pdf-raw")

        self.assertEqual(guarded["TAX_CATEGORY"], CATEGORY_GST)
        self.assertEqual(guarded["CONFIDENCE"], "HIGH")
        self.assertFalse(guarded["REVIEW_RECOMMENDED"])
        self.assertTrue(guarded["final_override_applied"])
        self.assertIn("gst", guarded["normalized_particulars"])

    def test_pdf_raw_extracted_row_handles_dotted_gst(self):
        stored_doc = _normalise_transaction_doc(
            {
                "statement_id": "stmt-pdf-dotted-gst",
                "transaction_date": "2026-05-25",
                "narration": "Mob alrt Chg Apr-25",
                "debit": 10,
                "credit": 0,
                "classification": CATEGORY_NORMAL,
                "confidence": "HIGH",
                "review_status": "cleared",
                "source_format": "pdf",
                "raw_extracted_row": '["25 Apr", "Mob alrt Chg Apr-25 + G.S.T", "10.00"]',
            }
        )

        self.assertEqual(stored_doc["classification"], CATEGORY_GST)
        self.assertEqual(stored_doc["confidence"], "HIGH")
        self.assertEqual(stored_doc["review_status"], "cleared")
        self.assertTrue(stored_doc["final_override_applied"])

    def test_final_override_checks_unexpected_bank_columns(self):
        row = {
            "Date": "2026-05-25",
            "Bank Particulars Split Column": "Business Expression Jfee+GST",
            "Debit": 499,
            "Credit": 0,
            "TAX_CATEGORY": CATEGORY_NORMAL,
            "CONFIDENCE": "HIGH",
            "REVIEW_RECOMMENDED": False,
            "REASON": "Priority 5: no tax signal detected",
        }

        guarded = analysis_service.apply_display_classification_guard(row, statement_id="stmt-any-column-gst")

        self.assertEqual(guarded["TAX_CATEGORY"], CATEGORY_GST)
        self.assertEqual(guarded["CONFIDENCE"], "HIGH")
        self.assertFalse(guarded["REVIEW_RECOMMENDED"])
        self.assertTrue(guarded["final_override_applied"])

    def test_all_bank_charge_gst_examples_are_forced_to_gst_from_any_column(self):
        examples = (
            "Mob alrt Chg Apr-25+GST",
            "Business Expression Jfee+GST",
            "Cash wdl Chg 11-29SEP25+GST",
            "NEFT chg 28Oct25+GST",
        )
        for narration in examples:
            with self.subTest(narration=narration):
                guarded = analysis_service.apply_display_classification_guard(
                    {
                        "Date": "2026-05-25",
                        "Unknown PDF Column": narration,
                        "Debit": 10,
                        "Credit": 0,
                        "TAX_CATEGORY": CATEGORY_NORMAL,
                    },
                    statement_id="stmt-bank-charge-gst",
                )
                self.assertEqual(guarded["TAX_CATEGORY"], CATEGORY_GST)
                self.assertEqual(guarded["CONFIDENCE"], "HIGH")
                self.assertFalse(guarded["REVIEW_RECOMMENDED"])

    def test_spaced_category_filter_is_normalized(self):
        statement_id = "stmt-spaced-category"
        rows = [
            {
                "Date": "2026-05-25",
                "Narration": "Razorpay platform fee",
                "Debit": 50,
                "Credit": 0,
                "TAX_CATEGORY": "Possible GST",
                "CONFIDENCE": "MEDIUM",
                "REVIEW_RECOMMENDED": True,
            }
        ]
        with analysis_service._STORE_LOCK:
            analysis_service._ANALYSIS_CACHE[statement_id] = {
                "statementId": statement_id,
                "businessId": "biz-1",
                "filename": "statement.csv",
                "status": "analyzed",
                "summary": {},
                "transactions": {CATEGORY_GST: [], CATEGORY_POSSIBLE_GST: rows, CATEGORY_TDS: [], CATEGORY_NORMAL: []},
                "columns": {category: list(rows[0].keys()) for category in TAX_CATEGORY_ORDER},
            }

        filtered = analysis_service.get_transactions(statement_id, category="Possible GST")["transactions"]
        summary = analysis_service.get_summary(statement_id)["summary"]

        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["TAX_CATEGORY"], CATEGORY_POSSIBLE_GST)
        self.assertEqual(summary["categoryCounts"][CATEGORY_POSSIBLE_GST], 1)

    def test_stored_history_rows_are_guarded_before_display(self):
        stored_doc = _normalise_transaction_doc(
            {
                "statement_id": "stmt-db-guard",
                "transaction_date": "2026-05-25",
                "narration": "Mob alrt Chg Mar-25+GST",
                "debit": 10,
                "credit": 0,
                "classification": CATEGORY_NORMAL,
                "confidence": "HIGH",
                "review_status": "cleared",
                "source_row": {"Narration": "Mob alrt Chg Mar-25+GST"},
            }
        )
        self.assertEqual(stored_doc["classification"], CATEGORY_GST)
        self.assertEqual(stored_doc["confidence"], "HIGH")
        self.assertEqual(stored_doc["review_status"], "cleared")
        self.assertTrue(stored_doc["final_override_applied"])

        rows = analysis_service._rows_from_stored_transactions(
            [
                {
                    "statement_id": "stmt-history-guard",
                    "transaction_date": "2026-05-25",
                    "narration": "Mob alrt Chg Mar-25+GST",
                    "debit": 10,
                    "credit": 0,
                    "classification": CATEGORY_NORMAL,
                    "confidence": "HIGH",
                    "review_status": "cleared",
                    "reason": "old result",
                    "source_row": {
                        "Narration": "Mob alrt Chg Mar-25+GST",
                        "TAX_CATEGORY": CATEGORY_NORMAL,
                        "CONFIDENCE": "HIGH",
                        "REVIEW_RECOMMENDED": False,
                    },
                }
            ]
        )

        self.assertEqual(rows[0]["TAX_CATEGORY"], CATEGORY_GST)
        self.assertEqual(rows[0]["CONFIDENCE"], "HIGH")
        self.assertFalse(rows[0]["REVIEW_RECOMMENDED"])
        self.assertTrue(rows[0]["final_override_applied"])
        self.assertEqual(rows[0]["statement_id"], "stmt-history-guard")

    def test_stored_history_override_checks_nested_source_row_fields(self):
        stored_doc = _normalise_transaction_doc(
            {
                "statement_id": "stmt-db-remarks-guard",
                "transaction_date": "2026-05-25",
                "narration": "Bank service alert",
                "debit": 10,
                "credit": 0,
                "classification": CATEGORY_NORMAL,
                "confidence": "HIGH",
                "review_status": "cleared",
                "source_row": {
                    "Narration": "Bank service alert",
                    "Transaction Remarks": "Mob alrt Chg Mar-25+GST",
                },
            }
        )

        self.assertEqual(stored_doc["classification"], CATEGORY_GST)
        self.assertEqual(stored_doc["confidence"], "HIGH")
        self.assertEqual(stored_doc["review_status"], "cleared")
        self.assertTrue(stored_doc["final_override_applied"])

    def test_same_statement_frame_repeats_identically(self):
        original_append = processor.append_to_training_data
        processor.append_to_training_data = lambda *args, **kwargs: None
        try:
            outputs = []
            for _ in range(3):
                classified = processor.process_transactions(_sample_frame())
                outputs.append(
                    {
                        category: [
                            (
                                row["Narration"],
                                row["TAX_CATEGORY"],
                                row["CONFIDENCE"],
                                bool(row["REVIEW_RECOMMENDED"]),
                            )
                            for row in classified[category].to_dict(orient="records")
                        ]
                        for category in TAX_CATEGORY_ORDER
                    }
                )
        finally:
            processor.append_to_training_data = original_append

        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[1], outputs[2])
        gst_rows = outputs[0][CATEGORY_GST]
        self.assertIn(("Mob alrt Chg Mar-25+GST", CATEGORY_GST, "HIGH", False), gst_rows)


if __name__ == "__main__":
    unittest.main()
