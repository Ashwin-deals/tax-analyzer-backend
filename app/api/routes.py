from fastapi import APIRouter, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from app.services import auth_service
from app.services.email_fetch_service import EmailFetchError, run_email_fetch_now as run_email_fetch
from src import email_fetcher
from app.services.analysis_service import (
    AnalysisError,
    analyze_statement,
    cleanup_paths,
    dashboard_analytics_for_business,
    delete_statement,
    delete_user_statements,
    export_results,
    get_statement_analytics,
    get_original_statement_file,
    get_statement_preview_file,
    get_summary,
    get_transactions,
    list_statement_history,
    save_upload,
    tax_summary_for_user,
)

router = APIRouter(prefix="/api", tags=["statements"])


class LoginRequest(BaseModel):
    identifier: str
    password: str


class RegisterRequest(BaseModel):
    name: str
    username: str
    email: str
    password: str
    businessName: str


class BusinessCreateRequest(BaseModel):
    userId: str | None = None
    name: str
    industry: str | None = None


class EmailSettingsRequest(BaseModel):
    enableAutoFetch: bool = False
    gmailAddress: str | None = None
    gmailAppPassword: str | None = None
    fetchFrequency: str | None = None
    fetchTime: str | None = None
    fetchDay: str | None = None
    statementPasswordType: str | None = None
    statementPassword: str | None = None
    businessId: str | None = None


class EmailSettingsTestRequest(BaseModel):
    gmailAddress: str | None = None
    gmailAppPassword: str | None = None


class AccountDeleteRequest(BaseModel):
    password: str


class StatementPasswordRequest(BaseModel):
    password: str


def _require_user_id(authorization: str | None = None) -> str:
    try:
        return auth_service.require_user_id(authorization)
    except auth_service.AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def _analysis_status_code(exc: AnalysisError, default: int = 400) -> int:
    return 403 if "access" in str(exc).lower() else default


@router.get("/health")
def health_check() -> dict:
    return {"status": "ok"}


@router.post("/auth/login", tags=["auth"])
def login(payload: LoginRequest) -> dict:
    try:
        return auth_service.login(payload.identifier, payload.password)
    except auth_service.AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@router.post("/auth/register", tags=["auth"])
def register(payload: RegisterRequest) -> dict:
    try:
        return auth_service.register_user(
            name=payload.name,
            username=payload.username,
            email=payload.email,
            password=payload.password,
            business_name=payload.businessName,
        )
    except auth_service.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/businesses", tags=["businesses"])
def fetch_businesses(authorization: str | None = Header(default=None)) -> dict:
    user_id = _require_user_id(authorization)
    return {"businesses": auth_service.list_businesses(user_id)}


@router.post("/businesses", tags=["businesses"])
def create_business(payload: BusinessCreateRequest, authorization: str | None = Header(default=None)) -> dict:
    user_id = _require_user_id(authorization)
    if payload.userId and payload.userId != user_id:
        raise HTTPException(status_code=403, detail="Session does not match requested user")
    try:
        return {"business": auth_service.create_business(user_id, payload.name, payload.industry)}
    except auth_service.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/businesses/{business_id}/statements", tags=["businesses"])
def fetch_statement_history(business_id: str, authorization: str | None = Header(default=None)) -> dict:
    user_id = _require_user_id(authorization)
    return list_statement_history(business_id, user_id)


@router.get("/businesses/{business_id}/analytics", tags=["businesses"])
def fetch_dashboard_analytics(business_id: str, authorization: str | None = Header(default=None)) -> dict:
    user_id = _require_user_id(authorization)
    return dashboard_analytics_for_business(business_id, user_id)


@router.get("/dashboard/tax-summary", tags=["dashboard"])
def fetch_dashboard_tax_summary(authorization: str | None = Header(default=None)) -> dict:
    user_id = _require_user_id(authorization)
    return tax_summary_for_user(user_id)


@router.get("/profile/email-settings", tags=["profile"])
def fetch_email_settings(authorization: str | None = Header(default=None)) -> dict:
    user_id = _require_user_id(authorization)
    return {"settings": auth_service.get_email_settings(user_id)}


@router.post("/profile/email-settings", tags=["profile"])
def save_email_settings(payload: EmailSettingsRequest, authorization: str | None = Header(default=None)) -> dict:
    user_id = _require_user_id(authorization)
    return {"settings": auth_service.save_email_settings(user_id, payload.dict())}


@router.post("/profile/email-settings/test", tags=["profile"])
def test_email_settings(payload: EmailSettingsTestRequest, authorization: str | None = Header(default=None)) -> dict:
    user_id = _require_user_id(authorization)
    saved_settings = auth_service.get_private_email_settings(user_id)
    gmail_address = payload.gmailAddress or saved_settings.get("gmailAddress") or ""
    gmail_password = payload.gmailAppPassword or saved_settings.get("gmailAppPassword") or ""
    if gmail_password == "********":
        gmail_password = saved_settings.get("gmailAppPassword") or ""
    if not gmail_address or not gmail_password:
        raise HTTPException(status_code=400, detail="Gmail address and app password are required.")
    try:
        email_fetcher.test_connection(gmail_address, gmail_password)
    except email_fetcher.GmailFetchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"message": "Gmail connection successful."}


@router.post("/profile/email-fetch/run-now", tags=["profile"])
def run_email_fetch_now(authorization: str | None = Header(default=None)) -> dict:
    user_id = _require_user_id(authorization)
    settings_doc = auth_service.get_private_email_settings(user_id)
    business_id = settings_doc.get("businessId") or auth_service.default_business_id(user_id)
    try:
        return run_email_fetch(
            user_id=user_id,
            business_id=business_id or "",
            gmail_address=settings_doc.get("gmailAddress") or "",
            gmail_app_password=settings_doc.get("gmailAppPassword") or "",
        )
    except EmailFetchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/profile/account", tags=["profile"])
def delete_account(payload: AccountDeleteRequest, authorization: str | None = Header(default=None)) -> dict:
    user_id = _require_user_id(authorization)
    try:
        auth_service.verify_account_password(user_id, payload.password)
        statement_cleanup = delete_user_statements(user_id)
        auth_service.delete_account(user_id, payload.password)
        return {"deleted": True, **statement_cleanup}
    except auth_service.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AnalysisError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/statements/upload")
async def upload_statement(
    file: UploadFile = File(...),
    business_id: str | None = Form(default=None),
    authorization: str | None = Header(default=None),
) -> dict:
    user_id = _require_user_id(authorization)
    try:
        return await save_upload(file, business_id=business_id, user_id=user_id)
    except AnalysisError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/statements/{statement_id}/analyze")
def analyze_transactions(statement_id: str, authorization: str | None = Header(default=None)) -> dict:
    user_id = _require_user_id(authorization)
    try:
        return analyze_statement(statement_id, user_id=user_id)
    except AnalysisError as exc:
        raise HTTPException(status_code=_analysis_status_code(exc), detail=str(exc)) from exc


@router.get("/statements/{statement_id}/summary")
def fetch_summary(statement_id: str, authorization: str | None = Header(default=None)) -> dict:
    user_id = _require_user_id(authorization)
    try:
        return get_summary(statement_id, user_id=user_id)
    except AnalysisError as exc:
        raise HTTPException(status_code=_analysis_status_code(exc, 404), detail=str(exc)) from exc


@router.get("/statements/{statement_id}/analytics")
def fetch_statement_analytics(statement_id: str, authorization: str | None = Header(default=None)) -> dict:
    user_id = _require_user_id(authorization)
    try:
        return get_statement_analytics(statement_id, user_id=user_id)
    except AnalysisError as exc:
        raise HTTPException(status_code=_analysis_status_code(exc, 404), detail=str(exc)) from exc


@router.post("/statements/{statement_id}/password")
def retry_statement_password(
    statement_id: str,
    _payload: StatementPasswordRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    user_id = _require_user_id(authorization)
    try:
        return analyze_statement(statement_id, user_id=user_id)
    except AnalysisError as exc:
        raise HTTPException(status_code=_analysis_status_code(exc), detail=str(exc)) from exc


@router.get("/statements/{statement_id}/transactions")
def fetch_classified_transactions(
    statement_id: str,
    category: str | None = Query(default=None),
    confidence: str | None = Query(default=None),
    review: bool | None = Query(default=None),
    search: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
) -> dict:
    user_id = _require_user_id(authorization)
    try:
        return get_transactions(
            statement_id=statement_id,
            category=category,
            confidence=confidence,
            review=review,
            search=search,
            user_id=user_id,
        )
    except AnalysisError as exc:
        raise HTTPException(status_code=_analysis_status_code(exc), detail=str(exc)) from exc


@router.delete("/statements/{statement_id}")
def delete_statement_route(statement_id: str, authorization: str | None = Header(default=None)) -> dict:
    user_id = _require_user_id(authorization)
    try:
        return delete_statement(statement_id, user_id=user_id)
    except AnalysisError as exc:
        raise HTTPException(status_code=_analysis_status_code(exc, 404), detail=str(exc)) from exc


@router.get("/statements/{statement_id}/view")
def view_original_statement(statement_id: str, authorization: str | None = Header(default=None)) -> FileResponse:
    user_id = _require_user_id(authorization)
    try:
        artifact = get_statement_preview_file(statement_id, user_id=user_id)
    except AnalysisError as exc:
        raise HTTPException(status_code=_analysis_status_code(exc, 404), detail=str(exc)) from exc
    return FileResponse(
        artifact.path,
        filename=artifact.filename,
        media_type=artifact.media_type,
        content_disposition_type="inline",
        background=BackgroundTask(cleanup_paths, artifact.cleanup_paths) if artifact.cleanup_paths else None,
    )


@router.get("/statements/{statement_id}/download")
def download_original_statement(statement_id: str, authorization: str | None = Header(default=None)) -> FileResponse:
    user_id = _require_user_id(authorization)
    try:
        artifact = get_original_statement_file(statement_id, user_id=user_id)
    except AnalysisError as exc:
        raise HTTPException(status_code=_analysis_status_code(exc, 404), detail=str(exc)) from exc
    return FileResponse(
        artifact.path,
        filename=artifact.filename,
        media_type=artifact.media_type,
        content_disposition_type="attachment",
    )


@router.get("/statements/{statement_id}/export")
def export_statement_results(
    statement_id: str,
    category: str | None = Query(default="ALL"),
    authorization: str | None = Header(default=None),
) -> FileResponse:
    user_id = _require_user_id(authorization)
    try:
        artifact = export_results(statement_id, category=category, user_id=user_id)
    except AnalysisError as exc:
        raise HTTPException(status_code=_analysis_status_code(exc), detail=str(exc)) from exc

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
