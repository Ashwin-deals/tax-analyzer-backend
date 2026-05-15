from __future__ import annotations

import logging
import json
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
from src.exporter import export_data
from src.loader import SUPPORTED_EXTENSIONS, load_statement
from src.processor import process_transactions
from utils.constants import (
    CATEGORY_GST,
    CATEGORY_NORMAL,
    CATEGORY_POSSIBLE_GST,
    CATEGORY_TDS,
    CREDIT_COLUMN_CANDIDATES,
    DEBIT_COLUMN_CANDIDATES,
    TAX_CATEGORY_ORDER,
)


logger = logging.getLogger(__name__)


class AnalysisError(Exception):
    """Raised when an uploaded statement cannot be processed."""


@dataclass(frozen=True)
class ExportArtifact:
    path: Path
    cleanup_paths: tuple[Path, ...]


_PENDING_UPLOADS: dict[str, dict[str, Any]] = {}
_ANALYSIS_CACHE: dict[str, dict[str, Any]] = {}
_STORE_LOCK = threading.RLock()


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


async def save_upload(file: UploadFile) -> dict[str, Any]:
    cleanup_runtime_storage()

    original_name = file.filename or "statement"
    safe_name = _safe_filename(original_name)
    suffix = Path(safe_name).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise AnalysisError(f"Unsupported file type '{suffix}'. Supported formats: {supported}")

    content = await file.read()
    max_bytes = settings.max_upload_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise AnalysisError(f"File is larger than {settings.max_upload_mb} MB")

    statement_id = uuid.uuid4().hex
    dest = settings.upload_dir / f"{statement_id}{suffix}"
    try:
        dest.write_bytes(content)
    except OSError as exc:
        cleanup_paths((dest,))
        raise AnalysisError(f"Could not temporarily store upload: {exc}") from exc

    metadata = {
        "statementId": statement_id,
        "filename": safe_name,
        "storedPath": str(dest),
        "status": "uploaded",
        "uploadedAt": _now(),
    }

    with _STORE_LOCK:
        _PENDING_UPLOADS[statement_id] = metadata
    return dict(metadata)


def analyze_statement(statement_id: str) -> dict[str, Any]:
    cleanup_runtime_storage()

    with _STORE_LOCK:
        cached = _ANALYSIS_CACHE.get(statement_id)
        metadata = _PENDING_UPLOADS.get(statement_id)

    if cached:
        return {
            "statementId": statement_id,
            "status": cached.get("status", "analyzed"),
            "summary": cached.get("summary", {}),
        }

    if not metadata:
        raise AnalysisError("Statement not found")

    file_path = Path(metadata["storedPath"])
    if not file_path.exists():
        with _STORE_LOCK:
            _PENDING_UPLOADS.pop(statement_id, None)
        raise AnalysisError("Uploaded file is missing from storage")

    try:
        raw_df = load_statement(file_path)
        classified = process_transactions(raw_df)
        summary = _summary_for(classified)
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
    return {"statementId": statement_id, "status": "analyzed", "summary": summary}


def get_summary(statement_id: str) -> dict[str, Any]:
    analysis = _load_analysis(statement_id)
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
    analysis = _load_analysis(statement_id)
    categories = [category] if category else TAX_CATEGORY_ORDER
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


def export_results(statement_id: str, category: str | None = None) -> ExportArtifact:
    analysis = _load_analysis(statement_id)
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
