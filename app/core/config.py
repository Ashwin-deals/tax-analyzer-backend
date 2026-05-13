import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BACKEND_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(BACKEND_ROOT / ".env")


def _csv_env(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "Tax Analyzer API")
    environment: str = os.getenv("APP_ENV", "development")
    cors_origins: list[str] = None
    upload_dir: Path = Path(os.getenv("UPLOAD_DIR", BACKEND_ROOT / "data" / "input" / "uploads"))
    analysis_dir: Path = Path(os.getenv("ANALYSIS_DIR", BACKEND_ROOT / "data" / "output" / "api"))
    export_dir: Path = Path(os.getenv("EXPORT_DIR", BACKEND_ROOT / "data" / "output" / "exports"))
    max_upload_mb: int = int(os.getenv("MAX_UPLOAD_MB", "50"))

    def __post_init__(self):
        object.__setattr__(
            self,
            "cors_origins",
            _csv_env("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"),
        )
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.analysis_dir.mkdir(parents=True, exist_ok=True)
        self.export_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
