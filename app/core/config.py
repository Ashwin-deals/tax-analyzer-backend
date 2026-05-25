import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BACKEND_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(BACKEND_ROOT / ".env")


def _csv_env(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _path_env(name: str, default: str | Path) -> Path:
    raw = os.getenv(name)
    path = Path(raw) if raw else Path(default)
    return path if path.is_absolute() else (BACKEND_ROOT / path).resolve()


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "FinScan API")
    environment: str = os.getenv("APP_ENV", "development")
    cors_origins: list[str] = None
    upload_dir: Path = _path_env("UPLOAD_DIR", BACKEND_ROOT / "data" / "input" / "uploads")
    analysis_dir: Path = _path_env("ANALYSIS_DIR", BACKEND_ROOT / "data" / "output" / "api")
    export_dir: Path = _path_env("EXPORT_DIR", BACKEND_ROOT / "data" / "output" / "exports")
    frontend_dist: Path = _path_env("FRONTEND_DIST", BACKEND_ROOT.parent / "tax-analyzer-frontend" / "dist")
    email_statement_dir: Path = _path_env("EMAIL_STATEMENT_DIR", BACKEND_ROOT / "data" / "email_statements")
    max_upload_mb: int = int(os.getenv("MAX_UPLOAD_MB", "50"))
    temp_file_ttl_seconds: int = int(os.getenv("TEMP_FILE_TTL_SECONDS", "3600"))
    export_ttl_seconds: int = int(os.getenv("EXPORT_TTL_SECONDS", "3600"))
    analysis_cache_ttl_seconds: int = int(os.getenv("ANALYSIS_CACHE_TTL_SECONDS", "43200"))
    mongodb_uri: str = os.getenv("MONGODB_URI", "")
    mongodb_database: str = "taxAnalyzer"
    aws_access_key_id: str = os.getenv("AWS_ACCESS_KEY_ID", "")
    aws_secret_access_key: str = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    aws_region: str = os.getenv("AWS_REGION", "ap-south-1")
    s3_bucket_name: str = os.getenv("S3_BUCKET_NAME", "financescan-ai")
    email_encryption_secret: str = os.getenv("EMAIL_ENCRYPTION_SECRET", "")
    email_fetch_scheduler_enabled: bool = os.getenv("EMAIL_FETCH_SCHEDULER_ENABLED", "true").lower() not in {"0", "false", "no"}
    email_fetch_scheduler_interval_seconds: int = int(os.getenv("EMAIL_FETCH_SCHEDULER_INTERVAL_SECONDS", "60"))

    def __post_init__(self):
        object.__setattr__(
            self,
            "cors_origins",
            _csv_env(
                "CORS_ORIGINS",
                "http://localhost:5173,http://127.0.0.1:5173,http://localhost:5174,http://127.0.0.1:5174,http://localhost:8000,http://127.0.0.1:8000,http://localhost:8001,http://127.0.0.1:8001,http://taxanalyzer.eopsys.com",
            ),
        )
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.analysis_dir.mkdir(parents=True, exist_ok=True)
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self.email_statement_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
