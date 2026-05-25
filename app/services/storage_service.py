from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import quote

from app.core.config import settings

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:  # pragma: no cover - optional until S3 storage is enabled.
    boto3 = None
    BotoCoreError = ClientError = Exception


@dataclass(frozen=True)
class StorageConfig:
    access_key_configured: bool
    secret_key_configured: bool
    region: str
    bucket_name: str
    original_statements_enabled: bool = False
    generated_reports_enabled: bool = False

    @property
    def configured(self) -> bool:
        return bool(
            self.access_key_configured
            and self.secret_key_configured
            and self.region
            and self.bucket_name
        )


def get_storage_config() -> StorageConfig:
    configured = bool(
        settings.aws_access_key_id
        and settings.aws_secret_access_key
        and settings.aws_region
        and settings.s3_bucket_name
    )
    return StorageConfig(
        access_key_configured=bool(settings.aws_access_key_id),
        secret_key_configured=bool(settings.aws_secret_access_key),
        region=settings.aws_region,
        bucket_name=settings.s3_bucket_name,
        original_statements_enabled=configured,
    )


class S3StorageError(Exception):
    """Raised when a known S3 object cannot be deleted safely."""


def _s3_client():
    config = get_storage_config()
    if not config.configured:
        raise S3StorageError("S3 is not fully configured")
    if boto3 is None:
        raise S3StorageError("boto3 is not installed")
    return boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )


def _listify(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def s3_keys_for_statement(statement: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for field in (
        "original_s3_key",
        "statement_s3_key",
        "s3_object_key",
        "s3_key",
        "report_s3_key",
        "export_s3_key",
    ):
        keys.extend(_listify(statement.get(field)))

    for field in (
        "generated_report_keys",
        "generated_report_s3_keys",
        "report_s3_keys",
        "export_s3_keys",
        "s3_report_keys",
    ):
        keys.extend(_listify(statement.get(field)))

    deduped: list[str] = []
    seen: set[str] = set()
    for key in keys:
        if key not in seen:
            seen.add(key)
            deduped.append(key)
    return deduped


def delete_s3_objects(keys: list[str]) -> dict[str, Any]:
    deduped_keys = []
    seen: set[str] = set()
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            deduped_keys.append(key)

    if not deduped_keys:
        return {"attempted": False, "deletedKeys": [], "errors": []}

    client = _s3_client()
    deleted: list[str] = []
    for index in range(0, len(deduped_keys), 1000):
        batch = deduped_keys[index : index + 1000]
        try:
            response = client.delete_objects(
                Bucket=settings.s3_bucket_name,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": False},
            )
        except (BotoCoreError, ClientError) as exc:
            raise S3StorageError(f"S3 delete failed: {exc}") from exc

        errors = response.get("Errors", [])
        if errors:
            message = "; ".join(f"{item.get('Key')}: {item.get('Message')}" for item in errors)
            raise S3StorageError(f"S3 delete failed for one or more objects: {message}")

        deleted.extend(item.get("Key") for item in response.get("Deleted", []) if item.get("Key"))

    return {"attempted": True, "deletedKeys": deleted, "errors": []}


def user_folder_slug(value: str | None) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^a-z0-9._-]", "", text)
    text = re.sub(r"-{2,}", "-", text).strip("-._")
    return text or "user"


def build_statement_key(username: str, statement_id: str, filename: str) -> str:
    safe_filename = filename.replace("/", "_").replace("\\", "_")
    return f"statements/{user_folder_slug(username)}/{statement_id}_{safe_filename}"


def s3_statement_prefix_for_user(username: str | None) -> str:
    return f"statements/{user_folder_slug(username)}/"


def delete_s3_prefix(prefix: str) -> dict[str, Any]:
    clean_prefix = (prefix or "").strip()
    if not clean_prefix:
        return {"attempted": False, "prefix": clean_prefix, "deletedKeys": [], "errors": []}

    client = _s3_client()
    deleted: list[str] = []
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=settings.s3_bucket_name, Prefix=clean_prefix):
            keys = [item.get("Key") for item in page.get("Contents", []) if item.get("Key")]
            if not keys:
                continue
            batch_result = delete_s3_objects(keys)
            deleted.extend(batch_result.get("deletedKeys", []))
    except S3StorageError:
        raise
    except (BotoCoreError, ClientError) as exc:
        raise S3StorageError(f"S3 folder delete failed: {exc}") from exc

    return {"attempted": True, "prefix": clean_prefix, "deletedKeys": deleted, "errors": []}


def delete_user_statement_folder(username: str | None) -> dict[str, Any]:
    return delete_s3_prefix(s3_statement_prefix_for_user(username))


def build_future_statement_key(username: str, statement_id: str, filename: str) -> str:
    return build_statement_key(username, statement_id, filename)


def build_future_report_key(business_id: str, statement_id: str, filename: str) -> str:
    safe_filename = filename.replace("/", "_").replace("\\", "_")
    return f"businesses/{business_id}/reports/{statement_id}/{safe_filename}"


def build_s3_url(bucket: str | None, region: str | None, key: str | None) -> str | None:
    if not bucket or not key:
        return None
    encoded_key = quote(str(key), safe="/")
    if region and region != "us-east-1":
        return f"https://{bucket}.s3.{region}.amazonaws.com/{encoded_key}"
    return f"https://{bucket}.s3.amazonaws.com/{encoded_key}"


def upload_statement_bytes(
    *,
    content: bytes,
    business_id: str,
    user_id: str,
    username: str,
    business_name: str,
    statement_id: str,
    filename: str,
    original_filename: str | None = None,
    content_type: str | None = None,
) -> dict[str, Any]:
    key = build_statement_key(username, statement_id, filename)
    client = _s3_client()
    extra_args: dict[str, Any] = {
        "Metadata": {
            "user_id": user_id,
            "username": username,
            "business_id": business_id,
            "business_name": business_name,
            "statement_id": statement_id,
            "original_filename": original_filename or filename,
        }
    }
    if content_type:
        extra_args["ContentType"] = content_type
    try:
        client.put_object(
            Bucket=settings.s3_bucket_name,
            Key=key,
            Body=content,
            **extra_args,
        )
    except (BotoCoreError, ClientError) as exc:
        raise S3StorageError(f"S3 upload failed: {exc}") from exc

    return {
        "bucket": settings.s3_bucket_name,
        "key": key,
        "region": settings.aws_region,
        "url": build_s3_url(settings.s3_bucket_name, settings.aws_region, key),
    }


def download_s3_object_to_file(key: str, destination) -> None:
    client = _s3_client()
    try:
        with open(destination, "wb") as fh:
            client.download_fileobj(settings.s3_bucket_name, key, fh)
    except (OSError, BotoCoreError, ClientError) as exc:
        raise S3StorageError(f"S3 download failed: {exc}") from exc
