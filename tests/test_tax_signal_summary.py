import unittest

from app.services.database import repository


class TaxSignalSummaryTest(unittest.TestCase):
    def setUp(self):
        if repository.is_available:
            self.skipTest("Tax summary test uses the in-memory fallback store")
        self._original_memory = {
            collection: {key: dict(value) for key, value in docs.items()}
            for collection, docs in repository._memory.items()
        }

    def tearDown(self):
        if hasattr(self, "_original_memory"):
            repository._memory = self._original_memory

    def test_tax_signal_summary_counts_only_analyzed_current_user_statements(self):
        repository._memory["users"] = {
            "user-a": {"user_id": "user-a", "email": "a@example.com"},
            "user-b": {"user_id": "user-b", "email": "b@example.com"},
        }
        repository._memory["businesses"] = {
            "biz-a": {"business_id": "biz-a", "user_id": "user-a", "name": "A"},
            "biz-b": {"business_id": "biz-b", "user_id": "user-b", "name": "B"},
        }
        repository._memory["statement_uploads"] = {
            "stmt-a": {
                "statement_id": "stmt-a",
                "business_id": "biz-a",
                "user_id": "user-a",
                "processing_status": "analyzed",
                "uploaded_at": "2026-05-25T00:00:00+00:00",
            },
            "stmt-failed": {
                "statement_id": "stmt-failed",
                "business_id": "biz-a",
                "user_id": "user-a",
                "processing_status": "failed",
                "uploaded_at": "2026-05-25T00:01:00+00:00",
            },
            "stmt-b": {
                "statement_id": "stmt-b",
                "business_id": "biz-b",
                "user_id": "user-b",
                "processing_status": "analyzed",
                "uploaded_at": "2026-05-25T00:02:00+00:00",
            },
            "stmt-deleted-soft": {
                "statement_id": "stmt-deleted-soft",
                "business_id": "biz-a",
                "user_id": "user-a",
                "processing_status": "analyzed",
                "deleted": True,
                "uploaded_at": "2026-05-25T00:03:00+00:00",
            },
        }
        repository._memory["transactions"] = {
            "txn-gst": {
                "transaction_id": "txn-gst",
                "statement_id": "stmt-a",
                "tax_category": "GST",
                "review_recommended": False,
            },
            "txn-possible": {
                "transaction_id": "txn-possible",
                "statement_id": "stmt-a",
                "tax_category": "POSSIBLE_GST",
                "review_recommended": "Yes",
            },
            "txn-normal": {
                "transaction_id": "txn-normal",
                "statement_id": "stmt-a",
                "tax_category": "NORMAL",
                "review_recommended": "No",
            },
            "txn-old-classification-only": {
                "transaction_id": "txn-old-classification-only",
                "statement_id": "stmt-a",
                "classification": "GST",
                "review_recommended": True,
            },
            "txn-failed": {
                "transaction_id": "txn-failed",
                "statement_id": "stmt-failed",
                "tax_category": "GST",
                "review_recommended": True,
            },
            "txn-other-user": {
                "transaction_id": "txn-other-user",
                "statement_id": "stmt-b",
                "tax_category": "GST",
                "review_recommended": True,
            },
            "txn-orphan": {
                "transaction_id": "txn-orphan",
                "statement_id": "stmt-deleted",
                "tax_category": "TDS",
                "review_recommended": True,
            },
            "txn-soft-deleted": {
                "transaction_id": "txn-soft-deleted",
                "statement_id": "stmt-deleted-soft",
                "tax_category": "GST",
                "review_recommended": True,
            },
        }

        summary = repository.tax_signal_summary("user-a")

        self.assertEqual(summary["statementCount"], 1)
        self.assertEqual(summary["rawTransactionCount"], 4)
        self.assertEqual(summary["reconciledTransactionCount"], 1)
        self.assertEqual(summary["invalidTaxCategoryCount"], 0)
        self.assertEqual(summary["transactionCount"], 4)
        self.assertEqual(summary["categoryTotal"], summary["transactionCount"])
        self.assertTrue(summary["countsMatchTransactions"])
        self.assertEqual(summary["taxCounts"]["GST"], 2)
        self.assertEqual(summary["taxCounts"]["POSSIBLE_GST"], 1)
        self.assertEqual(summary["taxCounts"]["TDS"], 0)
        self.assertEqual(summary["taxCounts"]["NORMAL"], 1)
        self.assertEqual(summary["pendingReviewCount"], 2)
        self.assertEqual(repository._memory["transactions"]["txn-old-classification-only"]["tax_category"], "GST")


if __name__ == "__main__":
    unittest.main()
