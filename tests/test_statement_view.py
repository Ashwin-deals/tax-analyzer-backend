import unittest

from pypdf import PdfReader, PdfWriter

from app.services.analysis_service import (
    StatementPasswordNeeded,
    _decrypt_pdf_for_view,
    _statement_password_for_view,
    _tabular_preview_html,
    cleanup_paths,
)
from app.services.secret_service import encrypt_secret


class StatementViewTest(unittest.TestCase):
    def setUp(self):
        self.source = None
        self.decrypted = None
        self.preview = None

    def tearDown(self):
        cleanup_paths(path for path in (self.source, self.decrypted, self.preview) if path)

    def _encrypted_pdf(self):
        from app.core.config import settings

        self.source = settings.analysis_dir / "test-encrypted-view.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        writer.encrypt("secret")
        with self.source.open("wb") as fh:
            writer.write(fh)
        return self.source

    def test_decrypt_pdf_for_view_creates_temporary_unlocked_copy(self):
        source = self._encrypted_pdf()

        self.decrypted = _decrypt_pdf_for_view(source, "secret", "stmt-test", "statement.pdf")

        reader = PdfReader(str(self.decrypted))
        self.assertFalse(reader.is_encrypted)
        self.assertEqual(len(reader.pages), 1)

    def test_decrypt_pdf_for_view_rejects_wrong_password(self):
        source = self._encrypted_pdf()

        with self.assertRaises(StatementPasswordNeeded):
            _decrypt_pdf_for_view(source, "wrong", "stmt-test", "statement.pdf")

    def test_statement_view_prefers_statement_level_saved_password(self):
        encrypted = encrypt_secret("statement-secret", label="statement password")
        record = {
            "statement_id": "stmt-password",
            "user_id": "user-password",
            "encrypted_statement_password": encrypted,
        }
        metadata = {
            "statementId": "stmt-password",
            "userId": "user-password",
        }

        self.assertEqual(_statement_password_for_view(record, metadata), "statement-secret")

    def test_csv_statement_view_creates_html_preview(self):
        from app.core.config import settings

        self.source = settings.analysis_dir / "test-statement-preview.csv"
        self.source.write_text("Date,Narration,Debit\n2026-05-25,UPI Payment,100\n", encoding="utf-8")

        self.preview = _tabular_preview_html(self.source, "stmt-csv", "statement.csv")
        html_text = self.preview.read_text(encoding="utf-8")

        self.assertTrue(self.preview.name.endswith("-preview.html"))
        self.assertIn("statement.csv", html_text)
        self.assertIn("UPI Payment", html_text)

    def test_excel_statement_view_creates_html_preview(self):
        import pandas as pd

        from app.core.config import settings

        self.source = settings.analysis_dir / "test-statement-preview.xlsx"
        pd.DataFrame(
            [
                ["Date", "Narration", "Credit"],
                ["2026-05-25", "Card Settlement", "250"],
            ]
        ).to_excel(self.source, index=False, header=False)

        self.preview = _tabular_preview_html(self.source, "stmt-xlsx", "statement.xlsx")
        html_text = self.preview.read_text(encoding="utf-8")

        self.assertTrue(self.preview.name.endswith("-preview.html"))
        self.assertIn("statement.xlsx", html_text)
        self.assertIn("Card Settlement", html_text)


if __name__ == "__main__":
    unittest.main()
