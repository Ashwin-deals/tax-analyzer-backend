import unittest

import pandas as pd

from src.scorer import score_transaction


def classify(narration, debit=100.0, credit=0.0):
    result = score_transaction(
        pd.Series(
            {
                "_description": narration,
                "_debit": debit,
                "_credit": credit,
                "_date": "2026-05-25",
            }
        )
    )
    return result.category, result.confidence, result.needs_review


class PriorityClassifierTest(unittest.TestCase):
    def test_explicit_gst_is_always_gst(self):
        for narration in (
            "Mob alrt Chg Mar-25+GST",
            "Mob alrt Chg Mar-25 + GST",
            "Business Expression Jfee+GST",
            "Cash wdl Chg 11-29SEP25+GST",
            "NEFT chg 28Oct25+GST",
        ):
            self.assertEqual(classify(narration), ("GST", "HIGH", False))

    def test_explicit_gst_wins_before_tds(self):
        self.assertEqual(classify("TDS recovery with GST charge"), ("GST", "HIGH", False))

    def test_explicit_tds_is_tds_without_review(self):
        self.assertEqual(classify("TDS deducted by customer"), ("TDS", "HIGH", False))
        self.assertEqual(classify("Tax deducted at source"), ("TDS", "HIGH", False))

    def test_settlement_exclusions_are_normal(self):
        self.assertEqual(
            classify(
                "NEFT/AXISCN1290413143/RAZORPAY PAYMENTS PVT LTD PAYMENT AGGREGATOR ESCROW ACCOUNT",
                debit=0,
                credit=1000,
            ),
            ("NORMAL", "HIGH", False),
        )
        self.assertEqual(
            classify("CMS_IFT CARD PMT MID-90096587 SETDT-21032026IDFC", debit=0, credit=1000),
            ("NORMAL", "HIGH", False),
        )

    def test_possible_gst_requires_review(self):
        self.assertEqual(classify("Razorpay platform fee"), ("POSSIBLE_GST", "MEDIUM", True))
        self.assertEqual(classify("software subscription invoice"), ("POSSIBLE_GST", "MEDIUM", True))

    def test_default_normal_is_high_confidence_no_review(self):
        self.assertEqual(classify("UPI/DR/123/gpay/Payment"), ("NORMAL", "HIGH", False))
        self.assertEqual(classify("Amazon payment"), ("NORMAL", "HIGH", False))

    def test_same_rows_repeat_identically(self):
        narrations = [
            "Mob alrt Chg Mar-25+GST",
            "CMS_IFT CARD PMT MID-90096587 SETDT-21032026IDFC",
            "Razorpay platform fee",
            "UPI/DR/123/gpay/Payment",
        ]
        expected = [classify(text) for text in narrations]
        for _ in range(5):
            self.assertEqual([classify(text) for text in narrations], expected)


if __name__ == "__main__":
    unittest.main()
