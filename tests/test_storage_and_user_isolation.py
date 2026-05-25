import unittest

from app.services.database import RepositoryError, repository
from app.services.storage_service import build_statement_key, s3_statement_prefix_for_user, user_folder_slug


class StorageAndUserIsolationTest(unittest.TestCase):
    def setUp(self):
        if repository.is_available:
            self.skipTest("Repository isolation test uses the in-memory fallback store")
        self._original_memory = {
            collection: {key: dict(value) for key, value in docs.items()}
            for collection, docs in repository._memory.items()
        }

    def tearDown(self):
        if hasattr(self, "_original_memory"):
            repository._memory = self._original_memory

    def test_statement_key_uses_sanitized_username_folder(self):
        self.assertEqual(user_folder_slug("Ashwin Kumar"), "ashwin-kumar")
        self.assertEqual(user_folder_slug("A sh@win!! Kumar"), "a-shwin-kumar")
        self.assertEqual(
            build_statement_key("Ashwin Kumar", "stmt123", "bank statement.csv"),
            "statements/ashwin-kumar/stmt123_bank statement.csv",
        )
        self.assertEqual(s3_statement_prefix_for_user("Ashwin Kumar"), "statements/ashwin-kumar/")

    def test_statement_history_is_filtered_by_user_id(self):
        repository._memory["statement_uploads"] = {
            "stmt-user-a": {
                "statement_id": "stmt-user-a",
                "business_id": "biz-1",
                "user_id": "user-a",
                "username": "user-a",
                "business_name": "Business A",
                "filename": "a.csv",
                "original_filename": "a.csv",
                "uploaded_at": "2026-05-25T00:00:00+00:00",
                "processing_status": "uploaded",
            },
            "stmt-user-b": {
                "statement_id": "stmt-user-b",
                "business_id": "biz-1",
                "user_id": "user-b",
                "username": "user-b",
                "business_name": "Business B",
                "filename": "b.csv",
                "original_filename": "b.csv",
                "uploaded_at": "2026-05-25T00:01:00+00:00",
                "processing_status": "uploaded",
            },
        }

        statements = repository.list_statement_uploads("biz-1", user_id="user-a")

        self.assertEqual([statement["statementId"] for statement in statements], ["stmt-user-a"])
        self.assertEqual(statements[0]["username"], "user-a")

    def test_delete_records_rejects_wrong_user(self):
        repository._memory["statement_uploads"] = {
            "stmt-owned": {
                "statement_id": "stmt-owned",
                "business_id": "biz-1",
                "user_id": "owner",
                "filename": "owned.csv",
                "uploaded_at": "2026-05-25T00:00:00+00:00",
                "processing_status": "uploaded",
            }
        }

        with self.assertRaises(RepositoryError):
            repository.delete_statement_records("stmt-owned", user_id="other-user")

        self.assertIn("stmt-owned", repository._memory["statement_uploads"])

    def test_delete_user_account_records_removes_only_that_user(self):
        repository._memory["users"] = {
            "user-a": {"user_id": "user-a", "email": "a@example.com"},
            "user-b": {"user_id": "user-b", "email": "b@example.com"},
        }
        repository._memory["businesses"] = {
            "biz-a": {"business_id": "biz-a", "user_id": "user-a", "name": "A"},
            "biz-b": {"business_id": "biz-b", "user_id": "user-b", "name": "B"},
        }
        repository._memory["email_settings"] = {
            "email-a": {"email_settings_id": "email-a", "user_id": "user-a"},
            "email-b": {"email_settings_id": "email-b", "user_id": "user-b"},
        }
        repository._memory["statement_uploads"] = {
            "stmt-a": {"statement_id": "stmt-a", "business_id": "biz-a", "user_id": "user-a"},
            "stmt-b": {"statement_id": "stmt-b", "business_id": "biz-b", "user_id": "user-b"},
        }
        repository._memory["transactions"] = {
            "txn-a": {"transaction_id": "txn-a", "statement_id": "stmt-a", "business_id": "biz-a"},
            "txn-b": {"transaction_id": "txn-b", "statement_id": "stmt-b", "business_id": "biz-b"},
        }
        repository._memory["review_items"] = {
            "review-a": {"review_item_id": "review-a", "transaction_id": "txn-a"},
            "review-b": {"review_item_id": "review-b", "transaction_id": "txn-b"},
        }
        repository._memory["corrections"] = {
            "correction-a": {"correction_id": "correction-a", "statement_id": "stmt-a"},
            "correction-b": {"correction_id": "correction-b", "statement_id": "stmt-b"},
        }

        deleted = repository.delete_user_account_records("user-a")

        self.assertEqual(deleted["users"], 1)
        self.assertNotIn("user-a", repository._memory["users"])
        self.assertNotIn("biz-a", repository._memory["businesses"])
        self.assertNotIn("stmt-a", repository._memory["statement_uploads"])
        self.assertNotIn("txn-a", repository._memory["transactions"])
        self.assertNotIn("review-a", repository._memory["review_items"])
        self.assertNotIn("correction-a", repository._memory["corrections"])

        self.assertIn("user-b", repository._memory["users"])
        self.assertIn("biz-b", repository._memory["businesses"])
        self.assertIn("stmt-b", repository._memory["statement_uploads"])
        self.assertIn("txn-b", repository._memory["transactions"])
        self.assertIn("review-b", repository._memory["review_items"])
        self.assertIn("correction-b", repository._memory["corrections"])


if __name__ == "__main__":
    unittest.main()
