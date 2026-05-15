from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.services.analysis_service import (
    AnalysisError,
    analyze_statement,
    cleanup_paths,
    export_results,
    get_summary,
    get_transactions,
    save_upload,
)

router = APIRouter(prefix="/api", tags=["statements"])


@router.get("/health")
def health_check() -> dict:
    return {"status": "ok"}


@router.post("/statements/upload")
async def upload_statement(file: UploadFile = File(...)) -> dict:
    try:
        return await save_upload(file)
    except AnalysisError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/statements/{statement_id}/analyze")
def analyze_transactions(statement_id: str) -> dict:
    try:
        return analyze_statement(statement_id)
    except AnalysisError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/statements/{statement_id}/summary")
def fetch_summary(statement_id: str) -> dict:
    try:
        return get_summary(statement_id)
    except AnalysisError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/statements/{statement_id}/transactions")
def fetch_classified_transactions(
    statement_id: str,
    category: str | None = Query(default=None),
    confidence: str | None = Query(default=None),
    review: bool | None = Query(default=None),
    search: str | None = Query(default=None),
) -> dict:
    try:
        return get_transactions(
            statement_id=statement_id,
            category=category,
            confidence=confidence,
            review=review,
            search=search,
        )
    except AnalysisError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/statements/{statement_id}/export")
def export_statement_results(
    statement_id: str,
    category: str | None = Query(default="ALL"),
) -> FileResponse:
    try:
        artifact = export_results(statement_id, category=category)
    except AnalysisError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    path = artifact.path
    media_type = (
        "application/zip"
        if path.suffix.lower() == ".zip"
        else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    filename = f"{statement_id}_results.zip" if path.suffix.lower() == ".zip" else path.name
    return FileResponse(
        path,
        filename=filename,
        media_type=media_type,
        background=BackgroundTask(cleanup_paths, artifact.cleanup_paths),
    )
