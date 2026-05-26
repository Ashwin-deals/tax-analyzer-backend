from __future__ import annotations

import logging
import html
import json
import mimetypes
import re
import shutil
import threading
import time
import uuid
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import UploadFile

from app.core.config import BACKEND_ROOT, settings
from src.exporter import export_data
from src.loader import SUPPORTED_EXTENSIONS, load_statement
from src.processor import process_transactions
from src.scorer import score_transaction
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
    INTERNAL_CREDIT_COL,
    INTERNAL_DATE_COL,
    INTERNAL_DEBIT_COL,
    INTERNAL_DESCRIPTION_COL,
)


logger = logging.getLogger(__name__)


class AnalysisError(Exception):
    """Raised when an uploaded statement cannot be processed."""


@dataclass(frozen=True)
class ExportArtifact:
    path: Path
    cleanup_paths: tuple[Path, ...]


@dataclass(frozen=True)
class StatementFileArtifact:
    path: Path
    filename: str
    media_type: str
    cleanup_paths: tuple[Path, ...] = ()


_PENDING_UPLOADS: dict[str, dict[str, Any]] = {}
_ANALYSIS_CACHE: dict[str, dict[str, Any]] = {}
_STORE_LOCK = threading.RLock()
_STATEMENT_STORE_PATH = BACKEND_ROOT / "data" / "statement_store.json"
_PERSISTED_STATEMENTS_DIR = BACKEND_ROOT / "data" / "statements"
_CLASSIFIER_RULE_VERSION = 2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_filename(filename: str) -> str:
    name = Path(filename).name
    return re.sub(r"[^\w.\- ]", "_", name).strip() or "statement"


def _safe_storage_segment(value: str | None) -> str:
    segment = re.sub(r"[^a-z0-9._-]+", "-", str(value or "user").lower()).strip("-._")
    return segment or "user"


def _original_statement_path(user_id: str | None, statement_id: str, filename: str) -> Path:
    return _PERSISTED_STATEMENTS_DIR / _safe_storage_segment(user_id) / f"{statement_id}_{_safe_filename(filename)}"


def _media_type_for_filename(filename: str) -> str:
    guessed, _encoding = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _empty_statement_store() -> dict[str, Any]:
    return {"statements": {}}


def _read_statement_store() -> dict[str, Any]:
    if not _STATEMENT_STORE_PATH.exists():
        return _empty_statement_store()
    try:
        data = json.loads(_STATEMENT_STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read persisted statement store: %s", exc)
        return _empty_statement_store()
    statements = data.get("statements") if isinstance(data, dict) else {}
    if not isinstance(statements, dict):
        statements = {}
    return {"statements": statements}


def _write_statement_store(store: dict[str, Any]) -> None:
    _STATEMENT_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _STATEMENT_STORE_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(store, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(_STATEMENT_STORE_PATH)


def _persist_analysis(payload: dict[str, Any]) -> None:
    statement_id = str(payload.get("statementId") or "")
    if not statement_id:
        return
    with _STORE_LOCK:
        store = _read_statement_store()
        store.setdefault("statements", {})[statement_id] = payload
        _write_statement_store(store)


def _stored_analysis(statement_id: str) -> dict[str, Any] | None:
    with _STORE_LOCK:
        return _read_statement_store().get("statements", {}).get(statement_id)


def _delete_persisted_analysis(statement_id: str) -> dict[str, Any] | None:
    with _STORE_LOCK:
        store = _read_statement_store()
        removed = store.setdefault("statements", {}).pop(statement_id, None)
        if removed is not None:
            _write_statement_store(store)
        return removed


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
                cleanup_paths((metadata.get("storedPath", ""), metadata.get("originalPath", "")))

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
            analysis = _stored_analysis(statement_id)
            if analysis:
                _ANALYSIS_CACHE[statement_id] = analysis
    if not analysis:
        raise AnalysisError("Statement not found")
    return _reclassify_analysis_if_stale(analysis)


def _assert_analysis_access(analysis: dict[str, Any], user_id: str | None = None) -> None:
    if user_id and analysis.get("userId") != user_id:
        raise AnalysisError("You do not have access to this statement")


def _load_accessible_analysis(statement_id: str, user_id: str | None = None) -> dict[str, Any]:
    analysis = _load_analysis(statement_id)
    _assert_analysis_access(analysis, user_id=user_id)
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


def _numeric_amount(series: pd.Series) -> pd.Series:
    cleaned = (
        series.astype(str)
        .str.replace(",", "", regex=False)
        .str.replace("\n", "", regex=False)
        .str.replace("\r", "", regex=False)
        .str.replace(r"[^\d.\-]", "", regex=True)
    )
    return pd.to_numeric(cleaned, errors="coerce").fillna(0)


def _lookup_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _lookup_compact(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _lookup_text(value))


def _matching_columns(df: pd.DataFrame, candidates: list[str]) -> list[str]:
    lower_map = {_lookup_text(c): c for c in df.columns}
    matches: list[str] = []

    for candidate in candidates:
        candidate_text = _lookup_text(candidate)
        if candidate_text in lower_map:
            matches.append(lower_map[candidate_text])

    for candidate in candidates:
        candidate_key = _lookup_compact(candidate)
        for col_lower, original in lower_map.items():
            column_key = _lookup_compact(col_lower)
            if candidate_key and candidate_key in column_key and original not in matches:
                matches.append(original)

    return matches


def _matching_keys(row: dict[str, Any], candidates: list[str]) -> list[str]:
    lower_map = {_lookup_text(key): key for key in row.keys()}
    matches: list[str] = []

    for candidate in candidates:
        candidate_text = _lookup_text(candidate)
        if candidate_text in lower_map:
            matches.append(lower_map[candidate_text])

    for candidate in candidates:
        candidate_key = _lookup_compact(candidate)
        for key_lower, original in lower_map.items():
            key_compact = _lookup_compact(key_lower)
            if candidate_key and candidate_key in key_compact and original not in matches:
                matches.append(original)

    return matches


def _first_matching_value(row: dict[str, Any], candidates: list[str]) -> Any:
    for key in _matching_keys(row, candidates):
        value = row.get(key)
        if value is None:
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


def _normalize_category(value: Any) -> str:
    normalized = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    if normalized in {"POSSIBLEGST", "GST_POSSIBLE"}:
        return CATEGORY_POSSIBLE_GST
    return normalized if normalized in TAX_CATEGORY_ORDER else CATEGORY_NORMAL


def _parse_transaction_date(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = re.sub(r"\s+", " ", str(value).strip())
    text = re.sub(r"(?i)(\d{1,2}[/-][a-z]{3,9}[/-])(\d{2})\s+(\d{2})", r"\1\2\3", text)
    text = re.sub(r"(\d{1,2}[/-]\d{1,2}[/-])(\d{2})\s+(\d{2})", r"\1\2\3", text)
    if not text or text.lower() in {"nan", "nat", "none"}:
        return None
    parsed = pd.to_datetime(text, errors="coerce", dayfirst=True)
    if pd.isna(parsed):
        return None
    return parsed.to_pydatetime()


def _statement_rows(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for category in TAX_CATEGORY_ORDER:
        for row in analysis.get("transactions", {}).get(category, []):
            normalized_row = dict(row)
            normalized_row["TAX_CATEGORY"] = _normalize_category(normalized_row.get("TAX_CATEGORY") or category)
            rows.append(normalized_row)
    return rows


def _accessible_analyses(user_id: str, business_id: str | None = None) -> list[dict[str, Any]]:
    cleanup_runtime_storage()
    with _STORE_LOCK:
        analysis_map = dict(_read_statement_store().get("statements", {}))
        analysis_map.update(_ANALYSIS_CACHE)
        analyses = list(analysis_map.values())
    filtered = []
    for analysis in analyses:
        if analysis.get("status") != "analyzed":
            continue
        if analysis.get("userId") != user_id:
            continue
        if business_id and analysis.get("businessId") != business_id:
            continue
        filtered.append(_reclassify_analysis_if_stale(analysis))
    return filtered


def _analysis_statement_history_item(analysis: dict[str, Any]) -> dict[str, Any]:
    summary = analysis.get("summary") or {}
    amount_totals = summary.get("amountTotals") or {}
    original_path = analysis.get("originalPath")
    stored_in_s3 = bool(analysis.get("storedInS3") or analysis.get("s3Key") or analysis.get("s3_key"))
    original_available = bool(original_path and Path(original_path).exists()) or stored_in_s3
    return {
        "statementId": analysis.get("statementId"),
        "businessId": analysis.get("businessId"),
        "userId": analysis.get("userId"),
        "filename": analysis.get("filename") or "statement",
        "originalFilename": analysis.get("filename") or "statement",
        "uploadDate": analysis.get("uploadedAt") or analysis.get("analyzedAt"),
        "uploadedAt": analysis.get("uploadedAt") or analysis.get("analyzedAt"),
        "processingStatus": analysis.get("status") or "analyzed",
        "status": analysis.get("status") or "analyzed",
        "totalTransactions": summary.get("totalTransactions", 0),
        "totalCredits": amount_totals.get("credit", 0),
        "totalDebits": amount_totals.get("debit", 0),
        "summary": summary,
        "storedInS3": stored_in_s3,
        "originalAvailable": original_available,
        "storageType": analysis.get("storageType") or ("local" if original_available else None),
    }


def _sum_amount(df: pd.DataFrame, candidates: list[str]) -> float:
    for col in _matching_columns(df, candidates):
        total = float(_numeric_amount(df[col]).sum())
        if total:
            return total
    return 0.0


def _amount_totals(df: pd.DataFrame) -> dict[str, float]:
    deposit_candidates = [
        "deposit",
        "deposit amt.",
        "deposit amt",
        "deposit (cr)",
        "amount credited",
        "credit amount",
    ]
    credit = _sum_amount(df, CREDIT_COLUMN_CANDIDATES)
    deposit = _sum_amount(df, deposit_candidates) or credit

    return {
        "debit": _sum_amount(df, DEBIT_COLUMN_CANDIDATES),
        "credit": credit,
        "deposit": deposit,
    }


def _row_classification_input(row: dict[str, Any]) -> pd.Series:
    description = _first_matching_value(row, DESCRIPTION_COLUMN_CANDIDATES)
    if description is None:
        description = " ".join(str(value) for value in row.values() if value is not None)
    return pd.Series(
        {
            INTERNAL_DESCRIPTION_COL: description,
            INTERNAL_DEBIT_COL: _amount_value(_first_matching_value(row, DEBIT_COLUMN_CANDIDATES)),
            INTERNAL_CREDIT_COL: _amount_value(_first_matching_value(row, CREDIT_COLUMN_CANDIDATES)),
            INTERNAL_DATE_COL: _first_matching_value(row, DATE_COLUMN_CANDIDATES),
        }
    )


def _reclassify_analysis_if_stale(analysis: dict[str, Any]) -> dict[str, Any]:
    if analysis.get("classifierRuleVersion") == _CLASSIFIER_RULE_VERSION:
        return analysis
    if analysis.get("status") != "analyzed":
        return analysis

    rows = _statement_rows(analysis)
    if not rows:
        analysis["classifierRuleVersion"] = _CLASSIFIER_RULE_VERSION
        _persist_analysis(analysis)
        return analysis

    grouped: dict[str, list[dict[str, Any]]] = {category: [] for category in TAX_CATEGORY_ORDER}
    for row in rows:
        next_row = dict(row)
        score = score_transaction(_row_classification_input(next_row))
        next_row["TAX_CATEGORY"] = score.category
        next_row["CONFIDENCE"] = score.confidence
        next_row["REVIEW_RECOMMENDED"] = score.needs_review
        next_row["REASON"] = score.reason
        grouped[score.category].append(next_row)

    frames = {category: pd.DataFrame(category_rows) for category, category_rows in grouped.items()}
    updated = {
        **analysis,
        "classifierRuleVersion": _CLASSIFIER_RULE_VERSION,
        "reclassifiedAt": _now(),
        "summary": _summary_for(frames),
        "transactions": grouped,
        "columns": {category: list(frame.columns) for category, frame in frames.items()},
    }
    with _STORE_LOCK:
        _ANALYSIS_CACHE[updated["statementId"]] = updated
    _persist_analysis(updated)
    return updated


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

    category_counts = {category: int(len(data.get(category, pd.DataFrame()))) for category in TAX_CATEGORY_ORDER}

    return {
        "totalTransactions": int(sum(category_counts.values())),
        "categoryCounts": category_counts,
        "confidenceCounts": confidence_counts,
        "reviewTotal": review_total,
        "amountTotals": _amount_totals(combined) if not combined.empty else {"debit": 0, "credit": 0, "deposit": 0},
    }


async def save_upload(
    file: UploadFile,
    business_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    cleanup_runtime_storage()

    original_name = file.filename or "statement"
    content = await file.read()
    return _stage_upload_content(content, original_name, business_id=business_id, user_id=user_id)


def _stage_upload_content(
    content: bytes,
    original_name: str,
    business_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    safe_name = _safe_filename(original_name)
    suffix = Path(safe_name).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise AnalysisError(f"Unsupported file type '{suffix}'. Supported formats: {supported}")

    max_bytes = settings.max_upload_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise AnalysisError(f"File is larger than {settings.max_upload_mb} MB")

    statement_id = uuid.uuid4().hex
    dest = settings.upload_dir / f"{statement_id}{suffix}"
    original_path = _original_statement_path(user_id, statement_id, safe_name)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        original_path.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        original_path.write_bytes(content)
    except OSError as exc:
        cleanup_paths((dest, original_path))
        raise AnalysisError(f"Could not store uploaded statement: {exc}") from exc

    metadata = {
        "statementId": statement_id,
        "businessId": business_id,
        "userId": user_id,
        "filename": safe_name,
        "storedPath": str(dest),
        "originalPath": str(original_path),
        "storedInS3": False,
        "originalAvailable": True,
        "storageType": "local",
        "status": "uploaded",
        "uploadedAt": _now(),
    }

    with _STORE_LOCK:
        _PENDING_UPLOADS[statement_id] = metadata
    return dict(metadata)


def ingest_local_statement(
    file_path: Path | str,
    business_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        raise AnalysisError(f"Statement attachment is missing: {path.name}")
    try:
        metadata = _stage_upload_content(path.read_bytes(), path.name, business_id=business_id, user_id=user_id)
        result = analyze_statement(metadata["statementId"], user_id=user_id)
    except OSError as exc:
        raise AnalysisError(f"Could not read statement attachment {path.name}: {exc}") from exc
    return {
        **result,
        "filename": metadata.get("filename"),
        "businessId": business_id,
    }


def analyze_statement(statement_id: str, user_id: str | None = None) -> dict[str, Any]:
    cleanup_runtime_storage()

    with _STORE_LOCK:
        cached = _ANALYSIS_CACHE.get(statement_id)
        metadata = _PENDING_UPLOADS.get(statement_id)

    if cached:
        _assert_analysis_access(cached, user_id=user_id)
        _persist_analysis(cached)
        return {
            "statementId": statement_id,
            "status": cached.get("status", "analyzed"),
            "summary": cached.get("summary", {}),
        }

    if not metadata:
        raise AnalysisError("Statement not found")
    if user_id and metadata.get("userId") != user_id:
        raise AnalysisError("You do not have access to this statement")

    file_path = Path(metadata["storedPath"])
    if not file_path.exists():
        with _STORE_LOCK:
            _PENDING_UPLOADS.pop(statement_id, None)
        raise AnalysisError("Uploaded file is missing from storage")

    try:
        raw_df = load_statement(file_path)
        classified = process_transactions(raw_df)
        summary = _summary_for(classified)
        original_path = metadata.get("originalPath")
        payload = {
            **metadata,
            "status": "analyzed",
            "analyzedAt": _now(),
            "storedPath": None,
            "originalPath": original_path,
            "storedInS3": bool(metadata.get("storedInS3")),
            "originalAvailable": bool(original_path and Path(original_path).exists()),
            "storageType": metadata.get("storageType") or "local",
            "rawFileDeleted": True,
            "classifierRuleVersion": _CLASSIFIER_RULE_VERSION,
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
    except SystemExit as exc:
        raise AnalysisError(str(exc)) from exc
    except Exception as exc:
        raise AnalysisError(f"Could not analyze statement: {exc}") from exc
    finally:
        cleanup_paths((file_path,))
        with _STORE_LOCK:
            _PENDING_UPLOADS.pop(statement_id, None)

    with _STORE_LOCK:
        _ANALYSIS_CACHE[statement_id] = payload
        _persist_analysis(payload)
    return {"statementId": statement_id, "status": "analyzed", "summary": summary}


def get_summary(statement_id: str, user_id: str | None = None) -> dict[str, Any]:
    analysis = _load_accessible_analysis(statement_id, user_id=user_id)
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
    user_id: str | None = None,
) -> dict[str, Any]:
    analysis = _load_accessible_analysis(statement_id, user_id=user_id)
    normalized_category = _normalize_category(category) if category else None
    categories = [normalized_category] if normalized_category else TAX_CATEGORY_ORDER
    rows: list[dict[str, Any]] = []

    for cat in categories:
        if cat not in TAX_CATEGORY_ORDER:
            raise AnalysisError(f"Unknown category '{cat}'")
        rows.extend(analysis.get("transactions", {}).get(cat, []))

    if confidence:
        rows = [row for row in rows if str(row.get("CONFIDENCE", "")).upper() == confidence.upper()]

    if review is not None:
        rows = [row for row in rows if bool(row.get("REVIEW_RECOMMENDED")) is review]

    if search:
        needle = search.lower()
        rows = [row for row in rows if needle in " ".join(str(v).lower() for v in row.values() if v is not None)]

    return {
        "statementId": statement_id,
        "count": len(rows),
        "transactions": rows,
    }


def get_statement_analytics(statement_id: str, user_id: str | None = None) -> dict[str, Any]:
    analysis = _load_accessible_analysis(statement_id, user_id=user_id)
    rows = _statement_rows(analysis)
    return {
        "statementId": statement_id,
        "filename": analysis.get("filename"),
        "status": analysis.get("status"),
        "summary": analysis.get("summary", {}),
        "transactions": rows,
    }


def list_statement_history(business_id: str, user_id: str) -> dict[str, Any]:
    statements = [
        _analysis_statement_history_item(analysis)
        for analysis in _accessible_analyses(user_id=user_id, business_id=business_id)
    ]
    statements.sort(key=lambda item: item.get("uploadedAt") or "", reverse=True)
    return {
        "businessId": business_id,
        "statements": statements,
    }


def tax_summary_for_user(user_id: str) -> dict[str, Any]:
    counts = {category: 0 for category in TAX_CATEGORY_ORDER}
    pending_review_count = 0
    transaction_count = 0

    for analysis in _accessible_analyses(user_id=user_id):
        for row in _statement_rows(analysis):
            counts[_normalize_category(row.get("TAX_CATEGORY"))] += 1
            transaction_count += 1
            if bool(row.get("REVIEW_RECOMMENDED")):
                pending_review_count += 1

    return {
        "taxCounts": counts,
        "pendingReviewCount": pending_review_count,
        "transactionCount": transaction_count,
        "totalTransactions": transaction_count,
    }


def _cashflow_bucket(period: str, period_name: str) -> dict[str, Any]:
    return {
        "period": period,
        "periodName": period_name,
        "credits": 0.0,
        "debits": 0.0,
        "net": 0.0,
    }


def _finalize_cashflow_buckets(buckets: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for key in sorted(buckets):
        row = dict(buckets[key])
        row["credits"] = round(row["credits"], 2)
        row["debits"] = round(row["debits"], 2)
        row["net"] = round(row["credits"] - row["debits"], 2)
        rows.append(row)
    return rows


def dashboard_analytics_for_business(business_id: str, user_id: str) -> dict[str, Any]:
    analyses = _accessible_analyses(user_id=user_id, business_id=business_id)
    daily: dict[str, dict[str, Any]] = {}
    weekly: dict[str, dict[str, Any]] = {}
    monthly: dict[str, dict[str, Any]] = {}
    parties: dict[str, dict[str, Any]] = defaultdict(lambda: {"name": "", "count": 0, "total": 0.0})
    total_credits = 0.0
    total_debits = 0.0

    for analysis in analyses:
        amount_totals = (analysis.get("summary") or {}).get("amountTotals") or {}
        total_credits += float(amount_totals.get("credit") or 0)
        total_debits += float(amount_totals.get("debit") or 0)

        for row in _statement_rows(analysis):
            credit = _amount_value(_first_matching_value(row, CREDIT_COLUMN_CANDIDATES))
            debit = _amount_value(_first_matching_value(row, DEBIT_COLUMN_CANDIDATES))
            tx_date = _parse_transaction_date(_first_matching_value(row, DATE_COLUMN_CANDIDATES))

            if tx_date:
                date_key = tx_date.date().isoformat()
                daily.setdefault(date_key, {"date": date_key, "credits": 0.0, "debits": 0.0})
                daily[date_key]["credits"] += credit
                daily[date_key]["debits"] += debit

                iso_year, iso_week, _weekday = tx_date.isocalendar()
                week_key = f"{iso_year}-W{iso_week:02d}"
                weekly.setdefault(week_key, _cashflow_bucket(week_key, f"Week {iso_week}, {iso_year}"))
                weekly[week_key]["credits"] += credit
                weekly[week_key]["debits"] += debit

                month_key = tx_date.strftime("%Y-%m")
                month_name = tx_date.strftime("%b %Y")
                monthly.setdefault(month_key, _cashflow_bucket(month_key, month_name))
                monthly[month_key]["credits"] += credit
                monthly[month_key]["debits"] += debit

            party_name = str(_first_matching_value(row, DESCRIPTION_COLUMN_CANDIDATES) or "").strip()
            if party_name:
                party_name = re.sub(r"\s+", " ", party_name)[:90]
                parties[party_name]["name"] = party_name
                parties[party_name]["count"] += 1
                parties[party_name]["total"] += abs(credit) + abs(debit)

    daily_rows = [
        {"date": key, "credits": round(value["credits"], 2), "debits": round(value["debits"], 2)}
        for key, value in sorted(daily.items())
    ]
    top_parties = sorted(parties.values(), key=lambda item: item["total"], reverse=True)[:10]
    for party in top_parties:
        party["total"] = round(party["total"], 2)

    return {
        "businessId": business_id,
        "statementCount": len(analyses),
        "totalCredits": round(total_credits, 2),
        "totalDebits": round(total_debits, 2),
        "netCashflow": round(total_credits - total_debits, 2),
        "dailyCashflow": daily_rows,
        "weeklyCashflow": _finalize_cashflow_buckets(weekly),
        "monthlyCashflow": _finalize_cashflow_buckets(monthly),
        "topParties": top_parties,
    }


def delete_statement(statement_id: str, user_id: str) -> dict[str, Any]:
    cleanup_runtime_storage()
    deleted = False
    with _STORE_LOCK:
        pending = _PENDING_UPLOADS.get(statement_id)
        if pending:
            if pending.get("userId") != user_id:
                raise AnalysisError("You do not have access to this statement")
            cleanup_paths((pending.get("storedPath", ""), pending.get("originalPath", "")))
            _PENDING_UPLOADS.pop(statement_id, None)
            deleted = True

        analysis = _ANALYSIS_CACHE.get(statement_id) or _stored_analysis(statement_id)
        if analysis:
            _assert_analysis_access(analysis, user_id=user_id)
            cleanup_paths((analysis.get("storedPath", ""), analysis.get("originalPath", "")))
            _ANALYSIS_CACHE.pop(statement_id, None)
            _delete_persisted_analysis(statement_id)
            deleted = True

    if not deleted:
        raise AnalysisError("Statement not found")
    return {"statementId": statement_id, "deleted": True}


def delete_user_statements(user_id: str) -> dict[str, Any]:
    cleanup_runtime_storage()
    with _STORE_LOCK:
        pending_ids = [
            statement_id
            for statement_id, metadata in _PENDING_UPLOADS.items()
            if metadata.get("userId") == user_id
        ]
        persisted = _read_statement_store().get("statements", {})
        analyzed_ids = {
            statement_id
            for statement_id, analysis in {**persisted, **_ANALYSIS_CACHE}.items()
            if analysis.get("userId") == user_id
        }

    deleted_count = 0
    for statement_id in sorted(set(pending_ids) | analyzed_ids):
        try:
            delete_statement(statement_id, user_id=user_id)
            deleted_count += 1
        except AnalysisError:
            logger.warning("Could not delete statement %s while removing user %s", statement_id, user_id)

    cleanup_paths((_PERSISTED_STATEMENTS_DIR / _safe_storage_segment(user_id),))
    return {"deletedStatements": deleted_count}


def get_original_statement_file(statement_id: str, user_id: str) -> StatementFileArtifact:
    cleanup_runtime_storage()
    with _STORE_LOCK:
        pending = _PENDING_UPLOADS.get(statement_id)

    if pending:
        if pending.get("userId") != user_id:
            raise AnalysisError("You do not have access to this statement")
        file_path = Path(pending.get("originalPath") or pending.get("storedPath") or "")
        if not file_path.exists():
            raise AnalysisError("Original statement file is no longer available")
        filename = pending.get("filename") or "statement"
        return StatementFileArtifact(
            path=file_path,
            filename=filename,
            media_type=_media_type_for_filename(filename),
        )

    analysis = _load_accessible_analysis(statement_id, user_id=user_id)
    file_path = Path(analysis.get("originalPath") or analysis.get("storedPath") or "")
    if not file_path.exists():
        raise AnalysisError("Original statement file is no longer available")
    filename = analysis.get("filename") or "statement"
    return StatementFileArtifact(
        path=file_path,
        filename=filename,
        media_type=_media_type_for_filename(filename),
    )


def get_statement_preview_file(statement_id: str, user_id: str) -> StatementFileArtifact:
    artifact = get_original_statement_file(statement_id, user_id=user_id)
    suffix = Path(artifact.filename).suffix.lower()
    if suffix == ".pdf":
        return artifact

    if suffix not in {".csv", ".xls", ".xlsx"}:
        raise AnalysisError("Preview is available for PDF, Excel, and CSV statements")

    try:
        preview_df = load_statement(artifact.path)
    except Exception as exc:
        raise AnalysisError(f"Could not prepare statement preview: {exc}") from exc

    preview_dir = settings.export_dir / f"{statement_id}-{uuid.uuid4().hex}-preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_path = preview_dir / f"{Path(artifact.filename).stem or 'statement'}_preview.html"
    display_df = preview_df.head(500)
    table_html = display_df.to_html(index=False, border=0, classes="statement-table", na_rep="")
    title = html.escape(artifact.filename)
    preview_path.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{ color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; background: #f5f7f2; color: #0f172a; }}
    main {{ padding: 24px; }}
    h1 {{ margin: 0 0 6px; font-size: 22px; }}
    p {{ margin: 0 0 18px; color: #64748b; font-weight: 600; }}
    .shell {{ overflow: auto; border: 1px solid #dbe3ee; border-radius: 10px; background: white; box-shadow: 0 10px 24px rgba(15, 23, 42, 0.08); }}
    table {{ border-collapse: separate; border-spacing: 0; min-width: 100%; font-size: 13px; }}
    th {{ position: sticky; top: 0; background: #f8fafc; color: #475569; font-size: 11px; letter-spacing: .08em; text-transform: uppercase; }}
    th, td {{ border-bottom: 1px solid #e2e8f0; padding: 10px 12px; text-align: left; white-space: nowrap; }}
    td {{ color: #1e293b; }}
  </style>
</head>
<body>
  <main>
    <h1>{title}</h1>
    <p>Previewing the first {len(display_df)} rows.</p>
    <div class="shell">{table_html}</div>
  </main>
</body>
</html>
""",
        encoding="utf-8",
    )
    return StatementFileArtifact(
        path=preview_path,
        filename=preview_path.name,
        media_type="text/html; charset=utf-8",
        cleanup_paths=(preview_dir,),
    )


def export_results(statement_id: str, category: str | None = None, user_id: str | None = None) -> ExportArtifact:
    analysis = _load_accessible_analysis(statement_id, user_id=user_id)
    run_id = uuid.uuid4().hex
    out_dir = settings.export_dir / f"{statement_id}-{run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path: Path | None = None

    try:
        data: dict[str, pd.DataFrame] = {}
        if category and category != "ALL":
            if category not in TAX_CATEGORY_ORDER:
                raise AnalysisError(f"Unknown category '{category}'")
            categories = [category]
        else:
            categories = TAX_CATEGORY_ORDER

        for cat in categories:
            records = analysis.get("transactions", {}).get(cat, [])
            columns = analysis.get("columns", {}).get(cat, [])
            data[cat] = _records_df(records, columns)

        if category and category != "ALL":
            filename = f"{category.lower()}_transactions.xlsx"
            dest = out_dir / filename
            data[category].to_excel(dest, index=False, engine="openpyxl")
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
