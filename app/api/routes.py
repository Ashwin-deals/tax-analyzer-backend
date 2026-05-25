from fastapi import APIRouter, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from app.core.config import settings
from app.services.analysis_service import (
    AnalysisError,
    analyze_statement,
    cleanup_paths,
    export_results,
    get_summary,
    get_transactions,
    prepare_statement_download,
    prepare_statement_view,
    save_upload,
)
from app.services.database import AuthError, RepositoryError, repository, verify_password
from app.services.email_fetch_service import (
    EmailFetchError,
    run_email_fetch_for_user,
    save_email_settings,
    test_email_connection,
)
from app.services.storage_service import (
    S3StorageError,
    delete_s3_objects,
    delete_user_statement_folder,
    get_storage_config,
    s3_keys_for_statement,
)

router = APIRouter(prefix="/api")


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
    userId: str
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


class StatementPasswordRequest(BaseModel):
    password: str


class AccountDeleteRequest(BaseModel):
    password: str


def _token_user_id(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.startswith("session-"):
        return None
    return token.removeprefix("session-").strip() or None


def _require_user_id(user_id: str | None = None, authorization: str | None = None) -> str:
    token_user_id = _token_user_id(authorization)
    if not token_user_id:
        raise HTTPException(status_code=401, detail="Valid user session is required")
    if user_id and token_user_id and user_id != token_user_id:
        raise HTTPException(status_code=403, detail="Session does not match requested user")
    return token_user_id


def _authenticated_user(authorization: str | None = None) -> dict:
    user_id = _require_user_id(authorization=authorization)
    user = repository.get_user(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="Authenticated user was not found")
    return user


def _assert_business_access(business_id: str, user_id: str) -> dict:
    business = repository.get_business(business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    if business.get("userId") != user_id:
        raise HTTPException(status_code=403, detail="You do not have access to this business")
    return business


def _statement_record_for_user(statement_id: str, user_id: str) -> dict:
    statement = repository.get_statement_upload_record(statement_id)
    if not statement:
        raise HTTPException(status_code=404, detail="Statement not found")
    owner_user_id = statement.get("user_id")
    if owner_user_id != user_id:
        raise HTTPException(status_code=403, detail="You do not have access to this statement")
    return statement


@router.get("/health")
def health_check() -> dict:
    storage = get_storage_config()
    return {
        "status": "ok",
        "app": "FinScan",
        "database": {
            "name": settings.mongodb_database,
            "connected": repository.is_available,
        },
        "s3": {
            "configured": storage.configured,
            "bucketName": storage.bucket_name,
            "region": storage.region,
            "originalStatementsEnabled": storage.original_statements_enabled,
            "generatedReportsEnabled": storage.generated_reports_enabled,
        },
    }


@router.post("/auth/login", tags=["auth"])
def login(payload: LoginRequest) -> dict:
    try:
        return repository.login(payload.identifier, payload.password)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@router.post("/auth/register", tags=["auth"])
def register(payload: RegisterRequest) -> dict:
    try:
        return repository.register_user(
            name=payload.name,
            username=payload.username,
            email=payload.email,
            password=payload.password,
            business_name=payload.businessName,
        )
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/businesses", tags=["businesses"])
def fetch_businesses(
    user_id: str | None = Query(default=None, alias="userId"),
    authorization: str | None = Header(default=None),
) -> dict:
    resolved_user_id = _require_user_id(user_id, authorization)
    return {"businesses": repository.list_businesses(resolved_user_id)}


@router.post("/businesses", tags=["businesses"])
def create_business(payload: BusinessCreateRequest, authorization: str | None = Header(default=None)) -> dict:
    resolved_user_id = _require_user_id(payload.userId, authorization)
    business = repository.create_business(
        user_id=resolved_user_id,
        name=payload.name,
        industry=payload.industry,
    )
    return {"business": repository.public_business(business)}


@router.get("/businesses/{business_id}", tags=["businesses"])
def fetch_business_profile(
    business_id: str,
    user_id: str | None = Query(default=None, alias="userId"),
    authorization: str | None = Header(default=None),
) -> dict:
    resolved_user_id = _require_user_id(user_id, authorization)
    business = _assert_business_access(business_id, resolved_user_id)
    return {"business": business}


@router.get("/profile/email-settings", tags=["profile"])
def fetch_email_settings(authorization: str | None = Header(default=None)) -> dict:
    user = _authenticated_user(authorization)
    return {"settings": repository.get_email_settings(user["user_id"])}


@router.post("/profile/email-settings", tags=["profile"])
def update_email_settings(
    payload: EmailSettingsRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    user = _authenticated_user(authorization)
    if payload.businessId:
        _assert_business_access(payload.businessId, user["user_id"])
    try:
        settings_payload = save_email_settings(
            user_id=user["user_id"],
            gmail_address=payload.gmailAddress,
            app_password=payload.gmailAppPassword,
            auto_fetch_enabled=payload.enableAutoFetch,
            fetch_frequency=payload.fetchFrequency,
            fetch_time=payload.fetchTime,
            fetch_day=payload.fetchDay,
            statement_password_type=payload.statementPasswordType,
            statement_password=payload.statementPassword,
            business_id=payload.businessId,
        )
        return {"settings": settings_payload}
    except EmailFetchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/profile/email-settings/test", tags=["profile"])
def test_profile_email_settings(
    payload: EmailSettingsTestRequest | None = None,
    authorization: str | None = Header(default=None),
) -> dict:
    user = _authenticated_user(authorization)
    payload = payload or EmailSettingsTestRequest()
    try:
        return test_email_connection(
            user_id=user["user_id"],
            gmail_address=payload.gmailAddress,
            app_password=payload.gmailAppPassword,
        )
    except EmailFetchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/profile/email-fetch/run-now", tags=["profile"])
def run_email_fetch_now(authorization: str | None = Header(default=None)) -> dict:
    user = _authenticated_user(authorization)
    try:
        return run_email_fetch_for_user(user["user_id"])
    except EmailFetchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/profile/account", tags=["profile"])
def delete_profile_account(
    payload: AccountDeleteRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    user = _authenticated_user(authorization)
    if not verify_password(payload.password or "", user.get("password_hash")):
        raise HTTPException(status_code=401, detail="Password is required and must be correct to delete this account")

    user_id = user["user_id"]
    statements = repository.list_user_statement_records(user_id)
    explicit_s3_keys: list[str] = []
    folder_names = {
        user.get("username") or user.get("name") or user.get("email") or user_id,
    }
    for statement in statements:
        explicit_s3_keys.extend(s3_keys_for_statement(statement))
        if statement.get("username"):
            folder_names.add(statement["username"])

    storage = get_storage_config()
    s3_result: dict = {
        "attempted": False,
        "configured": storage.configured,
        "folders": [],
        "objects": {"attempted": False, "deletedKeys": [], "errors": []},
    }
    if not storage.configured:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "S3 is not configured; account deletion cannot continue safely",
                "userId": user_id,
                "s3Deleted": False,
                "mongoDeleted": False,
            },
        )

    if storage.configured:
        try:
            folder_results = [delete_user_statement_folder(name) for name in sorted(folder_names) if name]
            deleted_from_folders = {
                key
                for result in folder_results
                for key in result.get("deletedKeys", [])
            }
            remaining_keys = [key for key in explicit_s3_keys if key not in deleted_from_folders]
            object_result = delete_s3_objects(remaining_keys)
            s3_result = {
                "attempted": True,
                "configured": True,
                "folders": folder_results,
                "objects": object_result,
            }
        except S3StorageError as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "message": str(exc),
                    "userId": user_id,
                    "s3Deleted": False,
                    "mongoDeleted": False,
                },
            ) from exc

    try:
        mongo_deleted = repository.delete_user_account_records(user_id)
    except RepositoryError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "message": str(exc),
                "userId": user_id,
                "s3Deleted": s3_result,
                "mongoDeleted": False,
            },
        ) from exc

    return {
        "deleted": True,
        "message": "Account, business data, statements, and related records deleted successfully",
        "s3": s3_result,
        "mongoDeleted": mongo_deleted,
    }


@router.get("/businesses/{business_id}/statements", tags=["businesses"])
def fetch_statement_history(
    business_id: str,
    user_id: str | None = Query(default=None, alias="userId"),
    authorization: str | None = Header(default=None),
) -> dict:
    resolved_user_id = _require_user_id(user_id, authorization)
    _assert_business_access(business_id, resolved_user_id)
    return {"businessId": business_id, "statements": repository.list_statement_uploads(business_id, user_id=resolved_user_id)}


@router.get("/businesses/{business_id}/analytics", tags=["businesses"])
def fetch_dashboard_analytics(
    business_id: str,
    user_id: str | None = Query(default=None, alias="userId"),
    authorization: str | None = Header(default=None),
) -> dict:
    resolved_user_id = _require_user_id(user_id, authorization)
    _assert_business_access(business_id, resolved_user_id)
    return repository.dashboard_analytics(business_id, user_id=resolved_user_id)


@router.get("/dashboard/tax-summary", tags=["dashboard"])
def fetch_dashboard_tax_summary(authorization: str | None = Header(default=None)) -> dict:
    user = _authenticated_user(authorization)
    return repository.tax_signal_summary(user["user_id"])


@router.post("/statements/upload", tags=["statements"])
async def upload_statement(
    file: UploadFile = File(...),
    business_id: str | None = Form(default=None),
    user_id: str | None = Form(default=None),
    authorization: str | None = Header(default=None),
) -> dict:
    try:
        user = _authenticated_user(authorization)
        resolved_user_id = user["user_id"]
        if user_id and user_id != resolved_user_id:
            raise HTTPException(status_code=403, detail="Session does not match requested user")
        if not business_id:
            raise HTTPException(status_code=400, detail="Business profile is required")
        _assert_business_access(business_id, resolved_user_id)
        return await save_upload(file, business_id=business_id, user_id=resolved_user_id)
    except AnalysisError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/statements/{statement_id}/analyze", tags=["statements"])
def analyze_transactions(
    statement_id: str,
    user_id: str | None = Query(default=None, alias="userId"),
    authorization: str | None = Header(default=None),
) -> dict:
    try:
        resolved_user_id = _require_user_id(user_id, authorization)
        _statement_record_for_user(statement_id, resolved_user_id)
        return analyze_statement(statement_id)
    except AnalysisError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/statements/{statement_id}/password", tags=["statements"])
def retry_statement_password(
    statement_id: str,
    payload: StatementPasswordRequest,
    user_id: str | None = Query(default=None, alias="userId"),
    authorization: str | None = Header(default=None),
) -> dict:
    try:
        resolved_user_id = _require_user_id(user_id, authorization)
        _statement_record_for_user(statement_id, resolved_user_id)
        if not payload.password:
            raise HTTPException(status_code=400, detail="Statement password is required")
        return analyze_statement(statement_id, statement_password=payload.password)
    except AnalysisError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/statements/{statement_id}/summary", tags=["statements"])
def fetch_summary(
    statement_id: str,
    user_id: str | None = Query(default=None, alias="userId"),
    authorization: str | None = Header(default=None),
) -> dict:
    try:
        resolved_user_id = _require_user_id(user_id, authorization)
        _statement_record_for_user(statement_id, resolved_user_id)
        return get_summary(statement_id)
    except AnalysisError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/statements/{statement_id}/transactions", tags=["statements"])
def fetch_classified_transactions(
    statement_id: str,
    user_id: str | None = Query(default=None, alias="userId"),
    category: str | None = Query(default=None),
    confidence: str | None = Query(default=None),
    review: bool | None = Query(default=None),
    search: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
) -> dict:
    try:
        resolved_user_id = _require_user_id(user_id, authorization)
        _statement_record_for_user(statement_id, resolved_user_id)
        return get_transactions(
            statement_id=statement_id,
            category=category,
            confidence=confidence,
            review=review,
            search=search,
        )
    except AnalysisError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/statements/{statement_id}", tags=["statements"])
def delete_statement(
    statement_id: str,
    user_id: str | None = Query(default=None, alias="userId"),
    authorization: str | None = Header(default=None),
) -> dict:
    resolved_user_id = _require_user_id(user_id, authorization)
    statement = _statement_record_for_user(statement_id, resolved_user_id)

    s3_keys = s3_keys_for_statement(statement)
    try:
        s3_result = delete_s3_objects(s3_keys)
    except S3StorageError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "message": str(exc),
                "statementId": statement_id,
                "s3Keys": s3_keys,
                "mongoDeleted": False,
            },
        ) from exc

    try:
        mongo_deleted = repository.delete_statement_records(statement_id, user_id=resolved_user_id)
    except RepositoryError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "message": str(exc),
                "statementId": statement_id,
                "s3Deleted": s3_result,
                "mongoDeleted": False,
            },
        ) from exc

    return {
        "statementId": statement_id,
        "deleted": True,
        "message": "Statement and related records deleted successfully",
        "s3": s3_result,
        "mongoDeleted": mongo_deleted,
    }


@router.get("/statements/{statement_id}/view", tags=["statements"])
def view_original_statement(
    statement_id: str,
    user_id: str | None = Query(default=None, alias="userId"),
    authorization: str | None = Header(default=None),
) -> FileResponse:
    resolved_user_id = _require_user_id(user_id, authorization)
    statement = _statement_record_for_user(statement_id, resolved_user_id)
    try:
        artifact = prepare_statement_view(statement)
    except AnalysisError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return FileResponse(
        artifact.path,
        filename=artifact.filename,
        media_type=artifact.media_type,
        content_disposition_type="inline",
        background=BackgroundTask(cleanup_paths, artifact.cleanup_paths),
    )


@router.get("/statements/{statement_id}/download", tags=["statements"])
def download_original_statement(
    statement_id: str,
    user_id: str | None = Query(default=None, alias="userId"),
    authorization: str | None = Header(default=None),
) -> FileResponse:
    resolved_user_id = _require_user_id(user_id, authorization)
    statement = _statement_record_for_user(statement_id, resolved_user_id)
    try:
        artifact = prepare_statement_download(statement)
    except AnalysisError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return FileResponse(
        artifact.path,
        filename=artifact.filename,
        media_type=artifact.media_type,
        content_disposition_type="attachment",
        background=BackgroundTask(cleanup_paths, artifact.cleanup_paths),
    )


@router.get("/statements/{statement_id}/analytics", tags=["statements"])
def fetch_statement_analytics(
    statement_id: str,
    user_id: str | None = Query(default=None, alias="userId"),
    authorization: str | None = Header(default=None),
) -> dict:
    try:
        resolved_user_id = _require_user_id(user_id, authorization)
        statement = _statement_record_for_user(statement_id, resolved_user_id)
        summary_payload = get_summary(statement_id)
        transactions_payload = get_transactions(statement_id)
    except AnalysisError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "statementId": statement_id,
        "statement": repository.public_statement(statement),
        "summary": summary_payload.get("summary", {}),
        "count": transactions_payload.get("count", 0),
        "transactions": transactions_payload.get("transactions", []),
    }


@router.get("/statements/{statement_id}/export", tags=["statements"])
def export_statement_results(
    statement_id: str,
    user_id: str | None = Query(default=None, alias="userId"),
    category: str | None = Query(default="ALL"),
    authorization: str | None = Header(default=None),
) -> FileResponse:
    try:
        resolved_user_id = _require_user_id(user_id, authorization)
        _statement_record_for_user(statement_id, resolved_user_id)
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
