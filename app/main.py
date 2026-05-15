import sys

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import BACKEND_ROOT, settings


if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.api.routes import router
from app.services.analysis_service import cleanup_runtime_storage


app = FastAPI(title=settings.app_name)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from fastapi.staticfiles import StaticFiles

app.include_router(router)


@app.on_event("startup")
def cleanup_temporary_storage() -> None:
    cleanup_runtime_storage()

if settings.frontend_dist.exists() and settings.frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=settings.frontend_dist, html=True), name="frontend")
else:
    @app.get("/")
    def root() -> dict:
        return {
            "name": settings.app_name,
            "environment": settings.environment,
            "docs": "/docs",
            "frontend_status": f"Frontend build not found at '{settings.frontend_dist}'. Build frontend or set FRONTEND_DIST in .env.",
        }
