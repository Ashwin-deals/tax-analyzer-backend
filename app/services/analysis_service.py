from __future__ import annotations

import html
import logging
import json
import mimetypes
import re
import shutil
import threading
import time
import uuid
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import UploadFile

from app.core.config import settings
from app.services.database import repository, utc_now
from app.services.storage_service import (
    S3StorageError,
    delete_s3_objects,
    download_s3_object_to_file,
    upload_statement_bytes,
)
from app.services.secret_service import SecretError, decrypt_secret
from src.exporter import export_data
from src.loader import SUPPORTED_EXTENSIONS, load_statement
from src.processor import process_transactions
from utils.classification_rules import (
    apply_display_classification_guard,
    apply_stored_classification_guard,
    normalize_category,
)
from utils.constants import (
    CATEGORY_GST,
    CATEGORY_NORMAL,
    CATEGORY_POSSIBLE_GST,
    CATEGORY_TDS,
    CREDIT_COLUMN_CANDIDATES,
    DATE_COLUMN_CANDIDATES,
    DEBIT_COLUMN_CANDIDATES,
    DESCRIPTION_COLUMN_CANDIDATES,
    TAX_CATEGORY_ORDER,
)


logger = logging.getLogger(__name__)


class AnalysisError(Exception):
    """Raised when an uploaded statement cannot be processed."""


class StatementPasswordNeeded(Exception):
    """Raised when a statement needs a password before analysis can continue."""


@dataclass(frozen=True)
class ExportArtifact:
    path: Path
    cleanup_paths: tuple[Path, ...]


@dataclass(frozen=True)
class StatementViewArtifact:
    path: Path
    filename: str
    media_type: str
    cleanup_paths: tuple[Path, ...]


_PENDING_UPLOADS: dict[str, dict[str, Any]] = {}
_ANALYSIS_CACHE: dict[str, dict[str, Any]] = {}
_STORE_LOCK = threading.RLock()
BALANCE_COLUMN_CANDIDATES = [
    "balance",
    "closing balance",
    "running balance",
    "available balance",
    "balance amount",
]
STATEMENT_PREVIEW_MAX_ROWS = 500
STATEMENT_PREVIEW_MAX_COLUMNS = 80


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_filename(filename: str) -> str:
    name = Path(filename).name
    return re.sub(r"[^\w.\- ]", "_", name).strip() or "statement"


def _remove_path(path: Path) -> None:
    try:
        if not path.exists():
            return
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError as exc:
        logger.warning("Could not remove temporary path %s: %s", path, exc)


def cleanup_paths(paths: Iterable[Path | str]) -> None:
    for raw_path in paths:
        if not raw_path:
            continue
        _remove_path(Path(raw_path))


def _cleanup_stale_children(folder: Path, ttl_seconds: int) -> None:
    if ttl_seconds <= 0 or not folder.exists():
        return

    cutoff = time.time() - ttl_seconds
    for child in folder.iterdir():
        if child.name == ".gitkeep":
            continue
        try:
            if child.stat().st_mtime <= cutoff:
                _remove_path(child)
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("Could not inspect temporary path %s: %s", child, exc)


def _is_expired(timestamp: str | None, ttl_seconds: int) -> bool:
    if ttl_seconds <= 0 or not timestamp:
        return False
    try:
        recorded_at = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    if recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - recorded_at.astimezone(timezone.utc)
    return age.total_seconds() > ttl_seconds


def _cleanup_session_stores() -> None:
    with _STORE_LOCK:
        expired_uploads = [
            statement_id
            for statement_id, metadata in _PENDING_UPLOADS.items()
            if _is_expired(metadata.get("uploadedAt"), settings.temp_file_ttl_seconds)
        ]
        for statement_id in expired_uploads:
            metadata = _PENDING_UPLOADS.pop(statement_id, None)
            if metadata:
                cleanup_paths((metadata.get("storedPath", ""),))

        expired_results = [
            statement_id
            for statement_id, payload in _ANALYSIS_CACHE.items()
            if _is_expired(payload.get("analyzedAt"), settings.analysis_cache_ttl_seconds)
        ]
        for statement_id in expired_results:
            _ANALYSIS_CACHE.pop(statement_id, None)


def cleanup_runtime_storage() -> None:
    _cleanup_stale_children(settings.upload_dir, settings.temp_file_ttl_seconds)
    _cleanup_stale_children(settings.email_statement_dir, settings.temp_file_ttl_seconds)
    _cleanup_stale_children(settings.analysis_dir, settings.analysis_cache_ttl_seconds)
    _cleanup_stale_children(settings.export_dir, settings.export_ttl_seconds)
    _cleanup_session_stores()


def _load_analysis(statement_id: str) -> dict[str, Any]:
    cleanup_runtime_storage()
    with _STORE_LOCK:
        analysis = _ANALYSIS_CACHE.get(statement_id)
    if not analysis:
        raise AnalysisError("Statement not found")
    return analysis


def _df_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return []
    return json.loads(df.to_json(orient="records", date_format="iso"))


def _records_df(records: list[dict[str, Any]], columns: list[str] | None = None) -> pd.DataFrame:
    df = pd.DataFrame(records)
    if columns:
        for col in columns:
            if col not in df.columns:
                df[col] = None
        df = df[columns]
    return df


def _column_order(rows: list[dict[str, Any]], preferred: list[str] | None = None) -> list[str]:
    ordered: list[str] = []
    for col in preferred or []:
        if col not in ordered and any(col in row for row in rows):
            ordered.append(col)
    for row in rows:
        for col in row.keys():
            if col not in ordered:
                ordered.append(col)
    return ordered


def _guard_display_rows(rows: list[dict[str, Any]], statement_id: str) -> list[dict[str, Any]]:
    return [
        apply_display_classification_guard(row, statement_id=statement_id, final_stage=True)
        for row in rows
    ]


def _numeric_amount(series: pd.Series) -> pd.Series:
    cleaned = (
        series.astype(str)
        .str.replace(",", "", regex=False)
        .str.replace("\n", "", regex=False)
        .str.replace("\r", "", regex=False)
        .str.replace(r"[^\d.\-]", "", regex=True)
    )
    return pd.to_numeric(cleaned, errors="coerce").fillna(0)


def _matching_columns(df: pd.DataFrame, candidates: list[str]) -> list[str]:
    lower_map = {str(c).lower().strip(): c for c in df.columns}
    matches: list[str] = []

    for candidate in candidates:
        if candidate.lower() in lower_map:
            matches.append(lower_map[candidate.lower()])

    for candidate in candidates:
        cand_nospace = candidate.lower().replace(" ", "")
        for col_lower, original in lower_map.items():
            if cand_nospace in col_lower.replace(" ", "") and original not in matches:
                matches.append(original)

    return matches


def _matching_keys(row: dict[str, Any], candidates: list[str]) -> list[str]:
    lower_map = {str(key).lower().strip(): key for key in row.keys()}
    matches: list[str] = []

    for candidate in candidates:
        if candidate.lower() in lower_map:
            matches.append(lower_map[candidate.lower()])

    for candidate in candidates:
        cand_nospace = candidate.lower().replace(" ", "")
        for key_lower, original in lower_map.items():
            if cand_nospace in key_lower.replace(" ", "") and original not in matches:
                matches.append(original)

    return matches


def _first_matching_value(row: dict[str, Any], candidates: list[str]) -> Any:
    for key in _matching_keys(row, candidates):
        value = row.get(key)
        if value is None:
            continue
        if isinstance(value, float) and pd.isna(value):
            continue
        text = str(value).strip()
        if text and text.lower() not in {"nan", "nat", "none"}:
            return value
    return None


def _amount_value(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)) and not pd.isna(value):
        return float(value)
    cleaned = re.sub(r"[^\d.\-]", "", str(value).replace(",", ""))
    try:
        return float(cleaned) if cleaned else 0.0
    except ValueError:
        return 0.0


def _stored_transaction_rows(
    business_id: str,
    statement_id: str,
    classified: dict[str, pd.DataFrame],
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    row_index = 0

    for category in TAX_CATEGORY_ORDER:
        records = _df_records(classified.get(category, pd.DataFrame()))
        for record in records:
            row_index += 1
            record = apply_display_classification_guard(record, statement_id=statement_id, final_stage=True)
            review_recommended = bool(record.get("REVIEW_RECOMMENDED"))
            row = {
                "transaction_id": uuid.uuid4().hex,
                "business_id": business_id,
                "user_id": user_id,
                "statement_id": statement_id,
                "row_index": row_index,
                "transaction_date": _first_matching_value(record, DATE_COLUMN_CANDIDATES),
                "narration": _first_matching_value(record, DESCRIPTION_COLUMN_CANDIDATES),
                "debit": _amount_value(_first_matching_value(record, DEBIT_COLUMN_CANDIDATES)),
                "credit": _amount_value(_first_matching_value(record, CREDIT_COLUMN_CANDIDATES)),
                "balance": _amount_value(_first_matching_value(record, BALANCE_COLUMN_CANDIDATES)),
                "classification": record.get("TAX_CATEGORY") or category,
                "tax_category": record.get("TAX_CATEGORY") or category,
                "confidence": record.get("CONFIDENCE") or "UNKNOWN",
                "review_status": "pending" if review_recommended else "cleared",
                "review_recommended": review_recommended,
                "reason": record.get("REASON"),
                "normalized_particulars": record.get("normalized_particulars"),
                "classification_source": record.get("classification_source"),
                "final_override_applied": record.get("final_override_applied"),
                "source_format": record.get("_source_format") or record.get("source_format"),
                "raw_row_text": record.get("_raw_row_text") or record.get("raw_row_text"),
                "raw_extracted_row": record.get("_raw_extracted_row") or record.get("raw_extracted_row"),
                "source_row": record,
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
            rows.append(
                apply_stored_classification_guard(row, statement_id=statement_id, final_stage=True)
            )

    return rows


def _rows_from_stored_transactions(stored_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for stored in stored_rows:
        row = dict(stored.get("source_row") or {})
        if not row:
            row = {
                "TRANSACTION_DATE": stored.get("transaction_date"),
                "NARRATION": stored.get("narration"),
                "DEBIT": stored.get("debit"),
                "CREDIT": stored.get("credit"),
                "BALANCE": stored.get("balance"),
            }
        row["TAX_CATEGORY"] = stored.get("tax_category") or stored.get("classification")
        row["CONFIDENCE"] = stored.get("confidence")
        row["REVIEW_RECOMMENDED"] = stored.get("review_status") == "pending"
        row["REASON"] = stored.get("reason")
        row["normalized_particulars"] = stored.get("normalized_particulars")
        row["classification_source"] = stored.get("classification_source")
        row["final_override_applied"] = stored.get("final_override_applied")
        if stored.get("source_format"):
            row["_source_format"] = stored.get("source_format")
        if stored.get("raw_row_text"):
            row["_raw_row_text"] = stored.get("raw_row_text")
        if stored.get("raw_extracted_row"):
            row["_raw_extracted_row"] = stored.get("raw_extracted_row")
        row["statement_id"] = stored.get("statement_id")
        rows.append(apply_display_classification_guard(row, statement_id=stored.get("statement_id"), final_stage=True))
    return rows


def _summary_from_statement(statement: dict[str, Any] | None) -> dict[str, Any]:
    if not statement:
        return {}
    summary = statement.get("summary") or {}
    if summary:
        return {
            **summary,
            "categoryCounts": {
                category: sum(
                    int(value or 0)
                    for raw_category, value in (summary.get("categoryCounts") or {}).items()
                    if normalize_category(raw_category) == category
                )
                for category in TAX_CATEGORY_ORDER
            },
        }
    return {
        "totalTransactions": statement.get("totalTransactions", 0),
        "categoryCounts": {},
        "confidenceCounts": {},
        "reviewTotal": 0,
        "amountTotals": {
            "debit": statement.get("totalDebits", 0),
            "credit": statement.get("totalCredits", 0),
            "net": statement.get("totalCredits", 0) - statement.get("totalDebits", 0),
        },
    }


def _sum_amount(df: pd.DataFrame, candidates: list[str]) -> float:
    for col in _matching_columns(df, candidates):
        total = float(_numeric_amount(df[col]).sum())
        if total:
            return total
    return 0.0


def _amount_totals(df: pd.DataFrame) -> dict[str, float]:
    credit = _sum_amount(df, CREDIT_COLUMN_CANDIDATES)
    debit = _sum_amount(df, DEBIT_COLUMN_CANDIDATES)

    return {
        "debit": debit,
        "credit": credit,
        "net": round(credit - debit, 2),
    }


def _combined_df(data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    frames = [df for df in data.values() if not df.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _summary_for(data: dict[str, pd.DataFrame]) -> dict[str, Any]:
    combined = _combined_df(data)
    confidence_counts = {}
    if not combined.empty and "CONFIDENCE" in combined.columns:
        confidence_counts = combined["CONFIDENCE"].fillna("UNKNOWN").value_counts().to_dict()

    review_total = 0
    if not combined.empty and "REVIEW_RECOMMENDED" in combined.columns:
        review_total = int(combined["REVIEW_RECOMMENDED"].fillna(False).astype(bool).sum())

    category_counts = {category: 0 for category in TAX_CATEGORY_ORDER}
    if not combined.empty and "TAX_CATEGORY" in combined.columns:
        for value in combined["TAX_CATEGORY"]:
            category_counts[normalize_category(value)] += 1
    else:
        category_counts = {category: int(len(data.get(category, pd.DataFrame()))) for category in TAX_CATEGORY_ORDER}

    return {
        "totalTransactions": int(sum(category_counts.values())),
        "categoryCounts": category_counts,
        "confidenceCounts": confidence_counts,
        "reviewTotal": review_total,
        "amountTotals": _amount_totals(combined) if not combined.empty else {"debit": 0, "credit": 0, "net": 0},
    }


def _classified_frames_from_rows(rows: list[dict[str, Any]]) -> dict[str, pd.DataFrame]:
    return {
        category: _records_df([row for row in rows if normalize_category(row.get("TAX_CATEGORY")) == category])
        for category in TAX_CATEGORY_ORDER
    }


def _guard_classified_results(
    classified: dict[str, pd.DataFrame],
    statement_id: str,
) -> dict[str, pd.DataFrame]:
    rows: list[dict[str, Any]] = []
    for category in TAX_CATEGORY_ORDER:
        rows.extend(_df_records(classified.get(category, pd.DataFrame())))
    return _classified_frames_from_rows(_guard_display_rows(rows, statement_id))


def _analysis_payload_with_final_guard(statement_id: str, analysis: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    preferred_columns: dict[str, list[str]] = analysis.get("columns", {}) or {}

    for category in TAX_CATEGORY_ORDER:
        rows.extend(analysis.get("transactions", {}).get(category, []))

    guarded_rows = _guard_display_rows(rows, statement_id)
    transactions = {
        category: [row for row in guarded_rows if row.get("TAX_CATEGORY") == category]
        for category in TAX_CATEGORY_ORDER
    }
    columns = {
        category: _column_order(transactions[category], preferred_columns.get(category, []))
        for category in TAX_CATEGORY_ORDER
    }
    classified = {
        category: _records_df(transactions[category], columns[category]) if transactions[category] else pd.DataFrame()
        for category in TAX_CATEGORY_ORDER
    }
    return {
        **analysis,
        "transactions": transactions,
        "columns": columns,
        "summary": _summary_for(classified),
    }


def _load_guarded_analysis(statement_id: str) -> dict[str, Any]:
    analysis = _load_analysis(statement_id)
    guarded = _analysis_payload_with_final_guard(statement_id, analysis)
    with _STORE_LOCK:
        _ANALYSIS_CACHE[statement_id] = guarded
    return guarded


def _classified_frames_from_payload(payload: dict[str, Any]) -> dict[str, pd.DataFrame]:
    return {
        category: _records_df(payload.get("transactions", {}).get(category, []), payload.get("columns", {}).get(category, []))
        for category in TAX_CATEGORY_ORDER
    }


def _persist_analysis_payload(statement_id: str, payload: dict[str, Any]) -> None:
    business_id = payload.get("businessId")
    if not business_id:
        return

    classified = _classified_frames_from_payload(payload)
    summary = payload.get("summary") or _summary_for(classified)
    repository.replace_transactions(
        business_id,
        statement_id,
        _stored_transaction_rows(business_id, statement_id, classified, user_id=payload.get("userId")),
    )
    repository.update_statement_upload(
        statement_id,
        {
            "processing_status": "analyzed",
            "analyzed_at": payload.get("analyzedAt") or _now(),
            "total_transactions": summary["totalTransactions"],
            "total_credits": summary["amountTotals"].get("credit", 0),
            "total_debits": summary["amountTotals"].get("debit", 0),
            "summary": summary,
        },
    )


def _looks_like_password_error(error: BaseException | str) -> bool:
    text = str(error).lower()
    return any(
        marker in text
        for marker in (
            "password",
            "encrypted",
            "decrypt",
            "incorrect password",
            "bad password",
            "file has not been decrypted",
            "document is password protected",
            "protected pdf",
        )
    )


def _fixed_statement_password_for_user(user_id: str | None) -> str | None:
    password, _encrypted = _fixed_statement_password_record_for_user(user_id)
    return password


def _fixed_statement_password_record_for_user(user_id: str | None) -> tuple[str | None, str | None]:
    if not user_id:
        return None, None
    settings_doc = repository.get_email_settings_record(user_id) or {}
    if settings_doc.get("statement_password_type") != "fixed":
        return None, None
    encrypted = settings_doc.get("encrypted_statement_password")
    if not encrypted:
        return None, None
    try:
        return decrypt_secret(encrypted, label="statement password"), encrypted
    except SecretError as exc:
        logger.warning("Could not decrypt saved statement password for user %s: %s", user_id, exc)
        return None, encrypted


def _encrypted_fixed_statement_password_for_user(user_id: str | None) -> str | None:
    _password, encrypted = _fixed_statement_password_record_for_user(user_id)
    return encrypted


def _statement_password_for_view(statement_record: dict[str, Any], metadata: dict[str, Any]) -> str | None:
    encrypted = (
        statement_record.get("encrypted_statement_password")
        or metadata.get("encryptedStatementPassword")
        or metadata.get("encrypted_statement_password")
    )
    if encrypted:
        try:
            return decrypt_secret(encrypted, label="statement password")
        except SecretError as exc:
            logger.warning("Could not decrypt statement-level password for %s: %s", metadata.get("statementId"), exc)
    return _fixed_statement_password_for_user(metadata.get("userId"))


def _load_statement_once(file_path: Path, password: str | None = None) -> pd.DataFrame:
    return load_statement(file_path, password=password)


def _load_statement_with_password_handling(
    file_path: Path,
    metadata: dict[str, Any],
    provided_password: str | None = None,
) -> tuple[pd.DataFrame, str | None]:
    if provided_password:
        try:
            return _load_statement_once(file_path, password=provided_password), "provided"
        except SystemExit as exc:
            if _looks_like_password_error(exc):
                raise StatementPasswordNeeded(str(exc)) from exc
            raise
        except Exception as exc:
            if _looks_like_password_error(exc):
                raise StatementPasswordNeeded(str(exc)) from exc
            raise

    try:
        return _load_statement_once(file_path), None
    except SystemExit as exc:
        if not _looks_like_password_error(exc):
            raise
        password_error: BaseException | str = exc
    except Exception as exc:
        if not _looks_like_password_error(exc):
            raise
        password_error = exc

    saved_password = _fixed_statement_password_for_user(metadata.get("userId"))
    if saved_password:
        try:
            return _load_statement_once(file_path, password=saved_password), "saved_fixed"
        except SystemExit as exc:
            if _looks_like_password_error(exc):
                raise StatementPasswordNeeded(str(exc)) from exc
            raise
        except Exception as exc:
            if _looks_like_password_error(exc):
                raise StatementPasswordNeeded(str(exc)) from exc
            raise

    raise StatementPasswordNeeded(str(password_error))


def _resolve_statement_owner(business_id: str | None, user_id: str | None) -> tuple[str, str, dict[str, Any], dict[str, Any], str, str]:
    if not business_id:
        profile = repository.ensure_default_profile()
        business_id = profile["business"]["businessId"]
        user_id = profile["user"]["userId"]
    elif user_id:
        business = repository.get_business(business_id)
        if not business or business.get("userId") != user_id:
            raise AnalysisError("You do not have access to this business")
    else:
        raise AnalysisError("Authenticated user is required")

    user = repository.get_user(user_id or "")
    if not user:
        raise AnalysisError("Authenticated user was not found")
    business = repository.get_business(business_id)
    if not business or business.get("userId") != user_id:
        raise AnalysisError("You do not have access to this business")

    username = user.get("username") or user.get("name") or user.get("email") or user_id or "user"
    business_name = business.get("name") or "Business"
    return business_id, user_id or "", user, business, username, business_name


def save_statement_content(
    *,
    content: bytes,
    filename: str | None,
    content_type: str | None = None,
    business_id: str | None = None,
    user_id: str | None = None,
    source: str = "manual",
    source_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cleanup_runtime_storage()
    business_id, user_id, _user, _business, username, business_name = _resolve_statement_owner(business_id, user_id)

    original_name = filename or "statement"
    safe_name = _safe_filename(original_name)
    suffix = Path(safe_name).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise AnalysisError(f"Unsupported file type '{suffix}'. Supported formats: {supported}")

    max_bytes = settings.max_upload_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise AnalysisError(f"File is larger than {settings.max_upload_mb} MB")

    statement_id = uuid.uuid4().hex
    temp_dir = settings.email_statement_dir if source == "email" else settings.upload_dir
    dest = temp_dir / f"{statement_id}{suffix}"
    try:
        dest.write_bytes(content)
    except OSError as exc:
        cleanup_paths((dest,))
        raise AnalysisError(f"Could not temporarily store upload: {exc}") from exc

    try:
        s3_upload = upload_statement_bytes(
            content=content,
            business_id=business_id,
            user_id=user_id or "",
            username=username,
            business_name=business_name,
            statement_id=statement_id,
            filename=safe_name,
            original_filename=original_name,
            content_type=content_type,
        )
    except S3StorageError as exc:
        cleanup_paths((dest,))
        raise AnalysisError(f"Could not store original statement in S3: {exc}") from exc

    metadata = {
        "statementId": statement_id,
        "businessId": business_id,
        "userId": user_id,
        "username": username,
        "businessName": business_name,
        "filename": safe_name,
        "originalFilename": original_name,
        "storedPath": str(dest),
        "originalS3Bucket": s3_upload["bucket"],
        "originalS3Key": s3_upload["key"],
        "originalS3Region": s3_upload["region"],
        "s3Bucket": s3_upload["bucket"],
        "s3Key": s3_upload["key"],
        "s3Url": s3_upload.get("url"),
        "storageProvider": "s3",
        "source": source,
        "sourceMetadata": source_metadata or {},
        "encryptedStatementPassword": _encrypted_fixed_statement_password_for_user(user_id),
        "status": "uploaded",
        "uploadedAt": _now(),
    }

    try:
        repository.create_statement_upload(metadata)
    except Exception:
        cleanup_paths((dest,))
        try:
            delete_s3_objects([s3_upload["key"]])
        except S3StorageError:
            logger.warning("Could not roll back S3 statement object after metadata failure: %s", s3_upload["key"])
        raise

    with _STORE_LOCK:
        _PENDING_UPLOADS[statement_id] = metadata
        _ANALYSIS_CACHE.pop(statement_id, None)
    return {key: value for key, value in metadata.items() if key != "storedPath"}


async def save_upload(file: UploadFile, business_id: str | None = None, user_id: str | None = None) -> dict[str, Any]:
    content = await file.read()
    return save_statement_content(
        content=content,
        filename=file.filename or "statement",
        content_type=file.content_type,
        business_id=business_id,
        user_id=user_id,
        source="manual",
    )


def ingest_and_analyze_statement_content(
    *,
    content: bytes,
    filename: str,
    content_type: str | None,
    business_id: str,
    user_id: str,
    source: str = "email",
    source_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = save_statement_content(
        content=content,
        filename=filename,
        content_type=content_type,
        business_id=business_id,
        user_id=user_id,
        source=source,
        source_metadata=source_metadata,
    )
    analysis = analyze_statement(metadata["statementId"])
    statement = repository.get_statement_upload(metadata["statementId"]) or metadata
    return {
        "statement": statement,
        "analysis": analysis,
    }


def _metadata_from_statement_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "statementId": record.get("statement_id"),
        "businessId": record.get("business_id"),
        "userId": record.get("user_id"),
        "username": record.get("username"),
        "businessName": record.get("business_name"),
        "filename": record.get("filename") or "statement",
        "originalFilename": record.get("original_filename") or record.get("filename") or "statement",
        "storedPath": None,
        "originalS3Bucket": record.get("original_s3_bucket"),
        "originalS3Key": record.get("original_s3_key"),
        "originalS3Region": record.get("original_s3_region"),
        "s3Bucket": record.get("s3_bucket") or record.get("original_s3_bucket"),
        "s3Key": record.get("s3_key") or record.get("original_s3_key"),
        "s3Url": record.get("s3_url"),
        "storageProvider": record.get("storage_provider") or "s3",
        "source": record.get("source", "manual"),
        "sourceMetadata": record.get("source_metadata") or {},
        "encryptedStatementPassword": record.get("encrypted_statement_password"),
        "status": record.get("processing_status", "uploaded"),
        "uploadedAt": record.get("uploaded_at"),
    }


def _statement_temp_path(statement_id: str, filename: str) -> Path:
    suffix = Path(filename or "statement").suffix.lower() or ".tmp"
    return settings.upload_dir / f"{statement_id}{suffix}"


def _statement_view_temp_path(statement_id: str, filename: str, label: str) -> Path:
    suffix = Path(filename or "statement").suffix.lower() or ".tmp"
    return settings.analysis_dir / f"{statement_id}-{uuid.uuid4().hex}-{label}{suffix}"


def _statement_preview_html_path(statement_id: str) -> Path:
    return settings.analysis_dir / f"{statement_id}-{uuid.uuid4().hex}-preview.html"


def _ensure_statement_file(metadata: dict[str, Any]) -> Path:
    stored_path = metadata.get("storedPath")
    if stored_path:
        file_path = Path(stored_path)
        if file_path.exists():
            return file_path

    s3_key = metadata.get("s3Key") or metadata.get("s3_key") or metadata.get("originalS3Key") or metadata.get("original_s3_key")
    if not s3_key:
        raise AnalysisError("Uploaded file is missing from storage")

    file_path = _statement_temp_path(metadata["statementId"], metadata.get("filename") or "statement")
    try:
        download_s3_object_to_file(s3_key, file_path)
    except S3StorageError as exc:
        raise AnalysisError(f"Could not retrieve original statement from S3: {exc}") from exc
    metadata["storedPath"] = str(file_path)
    return file_path


def _media_type_for_filename(filename: str) -> str:
    guessed, _encoding = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _pdf_is_encrypted(file_path: Path) -> bool:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise AnalysisError(
            "Password-protected PDF viewing requires pypdf. Install dependencies with `pip install -r requirements.txt`."
        ) from exc

    try:
        reader = PdfReader(str(file_path))
        return bool(reader.is_encrypted)
    except Exception as exc:
        raise AnalysisError(f"Could not inspect PDF statement for viewing: {exc}") from exc


def _decrypt_pdf_for_view(source_path: Path, password: str | None, statement_id: str, filename: str) -> Path:
    if not password:
        raise StatementPasswordNeeded("Password required or saved password is incorrect.")
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise AnalysisError(
            "Password-protected PDF viewing requires pypdf. Install dependencies with `pip install -r requirements.txt`."
        ) from exc

    output_path = _statement_view_temp_path(statement_id, filename, "decrypted")
    try:
        reader = PdfReader(str(source_path))
        result = reader.decrypt(password)
        if result == 0:
            raise StatementPasswordNeeded("Password required or saved password is incorrect.")
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        with output_path.open("wb") as fh:
            writer.write(fh)
        return output_path
    except StatementPasswordNeeded:
        cleanup_paths((output_path,))
        raise
    except Exception as exc:
        cleanup_paths((output_path,))
        raise StatementPasswordNeeded("Password required or saved password is incorrect.") from exc


def _decrypt_office_for_view(source_path: Path, password: str | None, statement_id: str, filename: str) -> Path:
    if not password:
        raise StatementPasswordNeeded("Password required or saved password is incorrect.")
    try:
        import msoffcrypto
    except ImportError as exc:
        raise AnalysisError(
            "Password-protected Excel viewing requires msoffcrypto-tool. Install dependencies with `pip install -r requirements.txt`."
        ) from exc

    output_path = _statement_view_temp_path(statement_id, filename, "decrypted")
    try:
        with source_path.open("rb") as source, output_path.open("wb") as output:
            office_file = msoffcrypto.OfficeFile(source)
            office_file.load_key(password=password)
            office_file.decrypt(output)
        return output_path
    except Exception as exc:
        cleanup_paths((output_path,))
        raise StatementPasswordNeeded("Password required or saved password is incorrect.") from exc


def _read_csv_preview_frame(source_path: Path) -> pd.DataFrame:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "latin1"):
        try:
            return pd.read_csv(
                source_path,
                header=None,
                dtype=str,
                keep_default_na=False,
                nrows=STATEMENT_PREVIEW_MAX_ROWS,
                encoding=encoding,
            )
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
        except Exception as exc:
            raise AnalysisError(f"Could not preview CSV statement: {exc}") from exc
    raise AnalysisError(f"Could not preview CSV statement: {last_error}")


def _read_excel_preview_frame(source_path: Path) -> pd.DataFrame:
    try:
        return pd.read_excel(
            source_path,
            header=None,
            dtype=str,
            keep_default_na=False,
            nrows=STATEMENT_PREVIEW_MAX_ROWS,
        )
    except Exception as exc:
        if _looks_like_password_error(exc):
            raise StatementPasswordNeeded("Password required or saved password is incorrect.") from exc
        raise AnalysisError(f"Could not preview Excel statement: {exc}") from exc


def _tabular_preview_html(source_path: Path, statement_id: str, filename: str) -> Path:
    suffix = Path(filename or source_path.name).suffix.lower()
    if suffix == ".csv":
        frame = _read_csv_preview_frame(source_path)
    elif suffix in {".xlsx", ".xls"}:
        frame = _read_excel_preview_frame(source_path)
    else:
        raise AnalysisError("Preview is not available for this file type")

    frame = frame.fillna("").astype(str)
    if frame.shape[1] > STATEMENT_PREVIEW_MAX_COLUMNS:
        frame = frame.iloc[:, :STATEMENT_PREVIEW_MAX_COLUMNS]

    escaped_name = html.escape(filename or "statement")
    table_html = frame.to_html(index=False, header=False, escape=True, classes="statement-table")
    preview_path = _statement_preview_html_path(statement_id)
    preview_path.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FinScan Statement Preview</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: #0f172a;
      background: #f8fafc;
    }}
    body {{
      margin: 0;
      padding: 24px;
    }}
    .shell {{
      max-width: 100%;
      margin: 0 auto;
      border: 1px solid #e2e8f0;
      border-radius: 10px;
      background: #ffffff;
      box-shadow: 0 10px 30px rgba(15, 23, 42, 0.08);
      overflow: hidden;
    }}
    header {{
      padding: 18px 20px;
      border-bottom: 1px solid #e2e8f0;
      background: #f8fafc;
    }}
    h1 {{
      margin: 0;
      font-size: 18px;
      line-height: 1.3;
      font-weight: 800;
    }}
    .meta {{
      margin-top: 6px;
      color: #64748b;
      font-size: 13px;
      font-weight: 650;
    }}
    .table-wrap {{
      overflow: auto;
      max-height: calc(100vh - 150px);
    }}
    table {{
      width: max-content;
      min-width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }}
    td {{
      max-width: 360px;
      border: 1px solid #e2e8f0;
      padding: 8px 10px;
      vertical-align: top;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }}
    tr:nth-child(even) td {{
      background: #f8fafc;
    }}
  </style>
</head>
<body>
  <main class="shell">
    <header>
      <h1>{escaped_name}</h1>
      <div class="meta">Preview rows: {len(frame)} | Columns: {frame.shape[1]}</div>
    </header>
    <div class="table-wrap">
      {table_html}
    </div>
  </main>
</body>
</html>
""",
        encoding="utf-8",
    )
    return preview_path


def prepare_statement_view(statement_record: dict[str, Any]) -> StatementViewArtifact:
    metadata = _metadata_from_statement_record(statement_record)
    statement_id = metadata["statementId"]
    filename = metadata.get("originalFilename") or metadata.get("filename") or "statement"
    s3_key = metadata.get("s3Key") or metadata.get("originalS3Key")
    if not s3_key:
        raise AnalysisError("Statement file is missing from S3")

    source_path = _statement_view_temp_path(statement_id, filename, "original")
    cleanup_targets: list[Path] = [source_path]
    try:
        download_s3_object_to_file(s3_key, source_path)
    except S3StorageError as exc:
        cleanup_paths(cleanup_targets)
        raise AnalysisError(f"Could not retrieve original statement from S3: {exc}") from exc

    view_path = source_path
    view_filename = filename
    suffix = Path(filename).suffix.lower()
    is_password_protected = bool(
        statement_record.get("is_password_protected")
        or statement_record.get("password_required")
        or str(statement_record.get("unlock_status") or "").startswith("unlocked_")
        or statement_record.get("password_unlocked_with")
    )

    try:
        if suffix == ".pdf":
            try:
                encrypted_pdf = _pdf_is_encrypted(source_path)
            except AnalysisError:
                if is_password_protected:
                    raise
                encrypted_pdf = False
            is_password_protected = is_password_protected or encrypted_pdf
            if encrypted_pdf:
                saved_password = _statement_password_for_view(statement_record, metadata)
                view_path = _decrypt_pdf_for_view(source_path, saved_password, statement_id, filename)
                cleanup_targets.append(view_path)
        elif suffix in {".xlsx", ".xls"} and is_password_protected:
            saved_password = _statement_password_for_view(statement_record, metadata)
            view_path = _decrypt_office_for_view(source_path, saved_password, statement_id, filename)
            cleanup_targets.append(view_path)
        if suffix in {".csv", ".xlsx", ".xls"}:
            try:
                preview_path = _tabular_preview_html(view_path, statement_id, filename)
            except StatementPasswordNeeded:
                saved_password = _statement_password_for_view(statement_record, metadata)
                decrypted_path = _decrypt_office_for_view(source_path, saved_password, statement_id, filename)
                cleanup_targets.append(decrypted_path)
                preview_path = _tabular_preview_html(decrypted_path, statement_id, filename)
                is_password_protected = True
            view_path = preview_path
            view_filename = f"{Path(filename).stem or 'statement'}_preview.html"
            cleanup_targets.append(view_path)
    except StatementPasswordNeeded as exc:
        repository.update_statement_upload(
            statement_id,
            {
                "is_password_protected": True,
                "unlock_status": "view_password_failed",
                "password_required": True,
                "password_error": str(exc),
            },
        )
        cleanup_paths(cleanup_targets)
        raise AnalysisError("Password required or saved password is incorrect.") from exc
    except AnalysisError:
        cleanup_paths(cleanup_targets)
        raise

    repository.update_statement_upload(
        statement_id,
        {
            "is_password_protected": bool(is_password_protected),
            "unlock_status": "view_unlocked" if view_path != source_path else "not_required",
            "password_required": False if view_path != source_path or not is_password_protected else bool(statement_record.get("password_required")),
            "password_error": None if view_path != source_path else statement_record.get("password_error"),
        },
    )
    return StatementViewArtifact(
        path=view_path,
        filename=view_filename,
        media_type=_media_type_for_filename(view_filename),
        cleanup_paths=tuple(cleanup_targets),
    )


def prepare_statement_download(statement_record: dict[str, Any]) -> StatementViewArtifact:
    metadata = _metadata_from_statement_record(statement_record)
    statement_id = metadata["statementId"]
    filename = metadata.get("originalFilename") or metadata.get("filename") or "statement"
    s3_key = metadata.get("s3Key") or metadata.get("originalS3Key")
    if not s3_key:
        raise AnalysisError("Statement file is missing from S3")

    source_path = _statement_view_temp_path(statement_id, filename, "download")
    try:
        download_s3_object_to_file(s3_key, source_path)
    except S3StorageError as exc:
        cleanup_paths((source_path,))
        raise AnalysisError(f"Could not retrieve original statement from S3: {exc}") from exc

    return StatementViewArtifact(
        path=source_path,
        filename=filename,
        media_type=_media_type_for_filename(filename),
        cleanup_paths=(source_path,),
    )


def analyze_statement(statement_id: str, statement_password: str | None = None) -> dict[str, Any]:
    cleanup_runtime_storage()

    with _STORE_LOCK:
        cached = _ANALYSIS_CACHE.get(statement_id)
        metadata = _PENDING_UPLOADS.get(statement_id)

    if cached:
        guarded = _analysis_payload_with_final_guard(statement_id, cached)
        _persist_analysis_payload(statement_id, guarded)
        with _STORE_LOCK:
            _ANALYSIS_CACHE[statement_id] = guarded
        return {
            "statementId": statement_id,
            "status": guarded.get("status", "analyzed"),
            "summary": guarded.get("summary", {}),
        }

    if not metadata:
        statement_record = repository.get_statement_upload_record(statement_id)
        if not statement_record:
            raise AnalysisError("Statement not found")
        metadata = _metadata_from_statement_record(statement_record)

    file_path = _ensure_statement_file(metadata)

    try:
        raw_df, password_source = _load_statement_with_password_handling(
            file_path,
            metadata,
            provided_password=statement_password,
        )
        classified = _guard_classified_results(process_transactions(raw_df), statement_id)
        summary = _summary_for(classified)
        stored_transactions = _stored_transaction_rows(
            metadata["businessId"],
            statement_id,
            classified,
            user_id=metadata.get("userId"),
        )
        payload = {
            **metadata,
            "status": "analyzed",
            "analyzedAt": _now(),
            "storedPath": None,
            "rawFileDeleted": True,
            "summary": summary,
            "transactions": {
                category: _df_records(classified.get(category, pd.DataFrame()))
                for category in TAX_CATEGORY_ORDER
            },
            "columns": {
                category: list(classified.get(category, pd.DataFrame()).columns)
                for category in TAX_CATEGORY_ORDER
            },
        }
        repository.replace_transactions(metadata["businessId"], statement_id, stored_transactions)
        repository.update_statement_upload(
            statement_id,
            {
                "processing_status": "analyzed",
                "analyzed_at": payload["analyzedAt"],
                "is_password_protected": bool(password_source),
                "unlock_status": f"unlocked_{password_source}" if password_source else "not_required",
                "encrypted_statement_password": (
                    metadata.get("encryptedStatementPassword")
                    or (_encrypted_fixed_statement_password_for_user(metadata.get("userId")) if password_source == "saved_fixed" else None)
                ),
                "password_required": False,
                "password_error": None,
                "password_unlocked_with": password_source,
                "total_transactions": summary["totalTransactions"],
                "total_credits": summary["amountTotals"].get("credit", 0),
                "total_debits": summary["amountTotals"].get("debit", 0),
                "summary": summary,
            },
        )
    except StatementPasswordNeeded as exc:
        repository.update_statement_upload(
            statement_id,
            {
                "processing_status": "pending_password",
                "is_password_protected": True,
                "unlock_status": "pending_password",
                "password_required": True,
                "password_error": str(exc),
                "failed_at": _now(),
            },
        )
        with _STORE_LOCK:
            _ANALYSIS_CACHE.pop(statement_id, None)
        return {
            "statementId": statement_id,
            "status": "pending_password",
            "passwordRequired": True,
            "message": "Statement password is required before analysis can continue",
        }
    except SystemExit as exc:
        repository.update_statement_upload(
            statement_id,
            {"processing_status": "failed", "error": str(exc), "failed_at": _now()},
        )
        raise AnalysisError(str(exc)) from exc
    except Exception as exc:
        repository.update_statement_upload(
            statement_id,
            {"processing_status": "failed", "error": str(exc), "failed_at": _now()},
        )
        raise AnalysisError(f"Could not analyze statement: {exc}") from exc
    finally:
        cleanup_paths((file_path,))
        with _STORE_LOCK:
            _PENDING_UPLOADS.pop(statement_id, None)

    with _STORE_LOCK:
        _ANALYSIS_CACHE.pop(statement_id, None)
        _ANALYSIS_CACHE[statement_id] = payload
    return {"statementId": statement_id, "status": "analyzed", "summary": summary}


def get_summary(statement_id: str) -> dict[str, Any]:
    try:
        analysis = _load_guarded_analysis(statement_id)
    except AnalysisError:
        statement = repository.get_statement_upload(statement_id)
        if not statement:
            raise
        stored_rows = repository.list_transactions(statement_id)
        if stored_rows:
            rows = _rows_from_stored_transactions(stored_rows)
            summary = _summary_for(
                {
                    cat: pd.DataFrame([row for row in rows if row.get("TAX_CATEGORY") == cat])
                    for cat in TAX_CATEGORY_ORDER
                }
            )
        else:
            summary = _summary_from_statement(statement)
        logger.info("FinScan category debug backend summary statement=%s gst=%s", statement_id, summary.get("categoryCounts", {}).get(CATEGORY_GST, 0))
        return {
            "statementId": statement_id,
            "filename": statement.get("filename"),
            "status": statement.get("status"),
            "summary": summary,
        }
    logger.info("FinScan category debug backend summary statement=%s gst=%s", statement_id, analysis.get("summary", {}).get("categoryCounts", {}).get(CATEGORY_GST, 0))
    return {
        "statementId": statement_id,
        "filename": analysis.get("filename"),
        "status": analysis.get("status"),
        "summary": analysis.get("summary", {}),
    }


def get_transactions(
    statement_id: str,
    category: str | None = None,
    confidence: str | None = None,
    review: bool | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    normalized_category = normalize_category(category) if category else None
    try:
        analysis = _load_guarded_analysis(statement_id)
        categories = [normalized_category] if normalized_category else TAX_CATEGORY_ORDER

        for cat in categories:
            if cat not in TAX_CATEGORY_ORDER:
                raise AnalysisError(f"Unknown category '{category}'")
            rows.extend(analysis.get("transactions", {}).get(cat, []))
    except AnalysisError:
        if normalized_category and normalized_category not in TAX_CATEGORY_ORDER:
            raise AnalysisError(f"Unknown category '{category}'")
        stored_rows = repository.list_transactions(statement_id)
        if not stored_rows:
            raise
        rows = _rows_from_stored_transactions(stored_rows)
        if normalized_category:
            rows = [row for row in rows if normalize_category(row.get("TAX_CATEGORY")) == normalized_category]

    if confidence:
        rows = [row for row in rows if str(row.get("CONFIDENCE", "")).upper() == confidence.upper()]

    if review is not None:
        rows = [row for row in rows if bool(row.get("REVIEW_RECOMMENDED")) is review]

    if search:
        needle = search.lower()
        rows = [row for row in rows if needle in " ".join(str(v).lower() for v in row.values() if v is not None)]

    gst_rows = sum(1 for row in rows if normalize_category(row.get("TAX_CATEGORY")) == CATEGORY_GST)
    logger.info(
        "FinScan category debug backend transactions statement=%s category=%s gst_rows=%s total_rows=%s",
        statement_id,
        normalized_category or "ALL",
        gst_rows,
        len(rows),
    )

    return {
        "statementId": statement_id,
        "count": len(rows),
        "transactions": rows,
    }


def export_results(statement_id: str, category: str | None = None) -> ExportArtifact:
    analysis: dict[str, Any] | None = None
    try:
        analysis = _load_guarded_analysis(statement_id)
    except AnalysisError:
        stored_rows = repository.list_transactions(statement_id)
        if not stored_rows:
            raise
        restored_rows = _rows_from_stored_transactions(stored_rows)
        analysis = {
            "transactions": {
                cat: [row for row in restored_rows if row.get("TAX_CATEGORY") == cat]
                for cat in TAX_CATEGORY_ORDER
            },
            "columns": {},
        }
        for cat, rows in analysis["transactions"].items():
            analysis["columns"][cat] = list(rows[0].keys()) if rows else []

    run_id = uuid.uuid4().hex
    out_dir = settings.export_dir / f"{statement_id}-{run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path: Path | None = None

    try:
        data: dict[str, pd.DataFrame] = {}
        normalized_category = normalize_category(category) if category and category != "ALL" else None
        if normalized_category:
            if normalized_category not in TAX_CATEGORY_ORDER:
                raise AnalysisError(f"Unknown category '{category}'")
            categories = [normalized_category]
        else:
            categories = TAX_CATEGORY_ORDER

        for cat in categories:
            records = analysis.get("transactions", {}).get(cat, [])
            columns = analysis.get("columns", {}).get(cat, [])
            data[cat] = _records_df(records, columns)

        if normalized_category:
            filename = f"{normalized_category.lower()}_transactions.xlsx"
            dest = out_dir / filename
            data[normalized_category].to_excel(dest, index=False, engine="openpyxl")
            return ExportArtifact(path=dest, cleanup_paths=(out_dir,))

        export_data(data, output_folder=out_dir, include_summary=True)
        zip_path = settings.export_dir / f"{statement_id}_{run_id}_results.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for file_path in out_dir.glob("*.xlsx"):
                zf.write(file_path, file_path.name)
        return ExportArtifact(path=zip_path, cleanup_paths=(out_dir, zip_path))
    except AnalysisError:
        cleanup_paths((out_dir, zip_path) if zip_path else (out_dir,))
        raise
    except Exception as exc:
        cleanup_paths((out_dir, zip_path) if zip_path else (out_dir,))
        raise AnalysisError(f"Could not export results: {exc}") from exc
