"""
src/loader.py
─────────────
Reads bank statement files and returns a cleaned, normalised DataFrame.
Supported formats: XLSX/XLS, CSV, and table-based PDFs.
"""

import logging
import json
import re
import sys
from io import BytesIO
from pathlib import Path

import pandas as pd

from utils.constants import (
    DESCRIPTION_COLUMN_CANDIDATES,
    INTERNAL_COLS,
)
from utils.helpers import detect_description_column, normalize_columns

logger = logging.getLogger(__name__)

_MAX_HEADER_SCAN_ROWS = 30
SUPPORTED_EXTENSIONS = {".xlsx", ".xls", ".csv", ".pdf"}

# Tokens used to score candidate header rows
_HEADER_TOKENS = {
    "particulars", "narration", "description", "remarks",
    "transaction details", "details", "transaction narration", "reference",
    "date", "transaction date", "value date",
    "debit", "credit", "balance",
    "cheque no", "cheque no.", "chq no", "ref no",
    "withdrawal", "deposit", "amount",
}


def load_statement(file_path: str | Path, password: str | None = None) -> pd.DataFrame:
    """
    Load a bank statement file and return a cleaned, column-normalised DataFrame.

    Adds internal alias columns (_description, _debit, _credit, _date) used by the scorer.
    Original columns are preserved in full for export.
    """
    file_path = Path(file_path)

    if not file_path.exists():
        logger.error("Input file not found: %s", file_path)
        sys.exit(f"[ERROR] File not found: {file_path}")

    suffix = file_path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        sys.exit(f"[ERROR] Unsupported file type '{suffix}'. Supported formats: {supported}")

    logger.info("Loading file: %s", file_path)
    if suffix in {".xlsx", ".xls"}:
        df = _load_excel_frame(file_path, password=password)
    elif suffix == ".csv":
        df = _load_csv_frame(file_path)
    else:
        df = _load_pdf_frame(file_path, password=password)

    return _clean_and_normalize(df, file_path)


def load_excel(file_path: str | Path) -> pd.DataFrame:
    """
    Backward-compatible wrapper for existing callers.
    Prefer load_statement() for new code.
    """
    return load_statement(file_path)


def _read_excel(source, suffix: str, header: int | None = None, nrows: int | None = None) -> pd.DataFrame:
    kwargs = {"header": header}
    if nrows is not None:
        kwargs["nrows"] = nrows
    if suffix == ".xlsx":
        kwargs["engine"] = "openpyxl"
    return pd.read_excel(source, **kwargs)


def _decrypt_excel_to_buffer(file_path: Path, password: str) -> BytesIO:
    try:
        import msoffcrypto
    except ImportError:
        sys.exit(
            "[ERROR] Password-protected Excel statements require msoffcrypto-tool. "
            "Install dependencies with `pip install -r requirements.txt`."
        )

    output = BytesIO()
    try:
        with file_path.open("rb") as fh:
            office_file = msoffcrypto.OfficeFile(fh)
            office_file.load_key(password=password)
            office_file.decrypt(output)
        output.seek(0)
        return output
    except Exception as exc:
        sys.exit(f"[ERROR] Cannot decrypt Excel file {file_path.name}: {exc}")


def _load_excel_frame(file_path: Path, password: str | None = None) -> pd.DataFrame:
    header_row = _detect_excel_header_row(file_path)
    logger.info("Detected Excel header at row index: %d", header_row)
    try:
        return _read_excel(file_path, file_path.suffix.lower(), header=header_row)
    except Exception as first_exc:
        if not password:
            logger.error("Failed to read Excel: %s", first_exc)
            sys.exit(f"[ERROR] Cannot read Excel file {file_path}: {first_exc}")
        decrypted = _decrypt_excel_to_buffer(file_path, password)
        try:
            header_row = _detect_excel_header_row_in_source(decrypted, file_path.suffix.lower())
            decrypted.seek(0)
            logger.info("Detected decrypted Excel header at row index: %d", header_row)
            return _read_excel(decrypted, file_path.suffix.lower(), header=header_row)
        except Exception as exc:
            logger.error("Failed to read decrypted Excel: %s", exc)
            sys.exit(f"[ERROR] Cannot read Excel file {file_path}: {exc}")


def _load_csv_frame(file_path: Path) -> pd.DataFrame:
    for encoding in ("utf-8-sig", "utf-8", "latin1"):
        try:
            probe = pd.read_csv(file_path, header=None, nrows=_MAX_HEADER_SCAN_ROWS, encoding=encoding)
            header_row = _detect_header_row_in_frame(probe)
            logger.info("Detected CSV header at row index: %d", header_row)
            return pd.read_csv(file_path, header=header_row, encoding=encoding)
        except UnicodeDecodeError:
            continue
        except Exception as exc:
            logger.error("Failed to read CSV: %s", exc)
            sys.exit(f"[ERROR] Cannot read CSV file {file_path}: {exc}")
    sys.exit(f"[ERROR] Cannot decode CSV file {file_path}. Tried utf-8-sig, utf-8, and latin1.")


def _load_pdf_frame(file_path: Path, password: str | None = None) -> pd.DataFrame:
    try:
        import pdfplumber
    except ImportError as exc:
        logger.error("pdfplumber import failed: %s", exc)
        sys.exit(
            "[ERROR] PDF support requires pdfplumber. Install dependencies with "
            "`pip install -r requirements.txt`."
        )

    rows: list[list[str]] = []
    try:
        with pdfplumber.open(file_path, password=password) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables() or []
                for table in tables:
                    for row in table:
                        cleaned = [_normalize_pdf_cell(cell) for cell in row]
                        if any(cleaned):
                            rows.append(cleaned)

                if not tables:
                    text = page.extract_text() or ""
                    rows.extend(_rows_from_pdf_text(text))
    except Exception as exc:
        logger.error("Failed to read PDF: %s", exc)
        sys.exit(f"[ERROR] Cannot read PDF file {file_path}: {exc}")

    if not rows:
        sys.exit(
            f"[ERROR] No transaction table could be extracted from {file_path.name}. "
            "Scanned or password-protected PDFs may need OCR/manual conversion."
        )

    width = max(len(row) for row in rows)
    normalized_rows = [row + [""] * (width - len(row)) for row in rows]
    raw = pd.DataFrame(normalized_rows)
    header_row = _detect_header_row_in_frame(raw)
    logger.info("Detected PDF table header at row index: %d", header_row)

    headers = raw.iloc[header_row].astype(str).str.strip().tolist()
    df = raw.iloc[header_row + 1:].copy()
    df.columns = _dedupe_columns(headers)
    raw_values = df.astype(object).where(pd.notna(df), "").values.tolist()
    df["_source_format"] = "pdf"
    df["_pdf_row_index"] = list(range(1, len(df) + 1))
    df["_raw_extracted_row"] = [
        json.dumps([_normalize_pdf_cell(value) for value in row], ensure_ascii=True)
        for row in raw_values
    ]
    df["_raw_row_text"] = [
        _normalize_pdf_cell(" | ".join(str(value) for value in row if _normalize_pdf_cell(value)))
        for row in raw_values
    ]
    return df


def _clean_and_normalize(df: pd.DataFrame, file_path: Path) -> pd.DataFrame:
    # Clean column names
    df.columns = _dedupe_columns(df.columns.astype(str).str.strip().tolist())

    # Drop fully-empty rows and unnamed columns (artefacts above data)
    df.dropna(how="all", inplace=True)
    df = df.loc[:, ~df.columns.astype(str).str.match(r"^Unnamed")]
    df.reset_index(drop=True, inplace=True)

    # ── Column normalisation (Fix #1) ─────────────────────────────────────────
    df = normalize_columns(df)

    desc_col = detect_description_column(df, DESCRIPTION_COLUMN_CANDIDATES)
    if desc_col is None:
        logger.warning(
            "No recognisable description column found. "
            "All transactions will be interpreted with low textual context. Columns: %s", list(df.columns),
        )
    else:
        logger.info("Using '%s' as the description column.", desc_col)

    logger.info(
        "Loaded %d rows, %d columns from %s.",
        len(df), len(df.columns) - len(INTERNAL_COLS), file_path.name,
    )
    return df


def _detect_excel_header_row(file_path: Path) -> int:
    """
    Score each of the first _MAX_HEADER_SCAN_ROWS rows and return the index
    of the row best matching known financial column keywords.
    """
    try:
        probe = _read_excel(file_path, file_path.suffix.lower(), header=None, nrows=_MAX_HEADER_SCAN_ROWS)
    except Exception:
        return 0

    return _detect_header_row_in_frame(probe)


def _detect_excel_header_row_in_source(source, suffix: str) -> int:
    try:
        source.seek(0)
        probe = _read_excel(source, suffix, header=None, nrows=_MAX_HEADER_SCAN_ROWS)
    except Exception:
        return 0
    return _detect_header_row_in_frame(probe)


def _detect_header_row_in_frame(probe: pd.DataFrame) -> int:
    best_row, best_score = 0, 0

    for row_idx, row in probe.iterrows():
        row_values = [str(v).lower().strip() for v in row if pd.notna(v)]
        score = _score_header_values(row_values)
        if score > best_score:
            best_score = score
            best_row   = int(row_idx)  # type: ignore[arg-type]

    if best_score == 0:
        logger.debug("No header row detected by scoring; defaulting to row 0.")

    return best_row


def _score_header_values(values: list[str]) -> int:
    score = 0
    for value in values:
        normalized = " ".join(value.replace("\n", " ").split())
        for token in _HEADER_TOKENS:
            if token == normalized or token in normalized:
                score += 1
                break
    return score


def _dedupe_columns(columns: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    result: list[str] = []
    for idx, col in enumerate(columns):
        base = col.strip() or f"Column_{idx + 1}"
        count = seen.get(base, 0)
        result.append(base if count == 0 else f"{base}_{count + 1}")
        seen[base] = count + 1
    return result


def _normalize_pdf_cell(value) -> str:
    if value is None:
        return ""
    text = str(value).replace("\u00a0", " ").replace("\u200b", "")
    text = re.sub(r"[\r\n]+", " ", text)
    text = re.sub(r"(?i)\bG\s*[\.\s]\s*S\s*[\.\s]*T\b", "GST", text)
    text = re.sub(r"(?i)\bC\s*[\.\s]\s*G\s*[\.\s]\s*S\s*[\.\s]*T\b", "CGST", text)
    text = re.sub(r"(?i)\bS\s*[\.\s]\s*G\s*[\.\s]\s*S\s*[\.\s]*T\b", "SGST", text)
    text = re.sub(r"(?i)\bI\s*[\.\s]\s*G\s*[\.\s]\s*S\s*[\.\s]*T\b", "IGST", text)
    text = re.sub(r"(?i)\bG\s*[\.\s]\s*S\s*[\.\s]\s*T\s*[\.\s]\s*I\s*[\.\s]\s*N\b", "GSTIN", text)
    text = re.sub(r"\+\s*GST\b", "+GST", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _rows_from_pdf_text(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        parts = [_normalize_pdf_cell(p) for p in re.split(r"\s{2,}", line) if _normalize_pdf_cell(p)]
        if len(parts) >= 3:
            rows.append(parts)
    return rows
