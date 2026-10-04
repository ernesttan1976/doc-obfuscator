from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .folder_picker import (
    FolderPickerError,
    pick_document_files,
    pick_project_directory,
)
from .key_store import KeyStoreUnavailable, OSKeyringProjectKeyStore, ProjectKeyStore
from .projects import ProjectError, ProjectService

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST = PROJECT_ROOT / "dist"
DEFAULT_DEV_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _configured_dev_origins() -> tuple[str, ...]:
    configured = os.environ.get("BLOT_DEV_ORIGINS")
    if not configured:
        return DEFAULT_DEV_ORIGINS
    return tuple(origin.strip().rstrip("/") for origin in configured.split(",") if origin.strip())


def _is_loopback_request(request: Request) -> bool:
    client = request.client
    if client is None:
        return False
    try:
        return ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        return False


def _is_local_host(host: str | None) -> bool:
    return (host or "").lower().strip("[]") in LOCAL_HOSTS


def _origin_is_allowed(request: Request, allowed_origins: tuple[str, ...]) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return True

    origin = origin.rstrip("/")
    if origin in allowed_origins:
        return True

    parsed = urlsplit(origin)
    return (
        parsed.scheme == request.url.scheme
        and parsed.netloc == request.headers.get("host")
        and _is_local_host(parsed.hostname)
    )


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    directory: str = Field(min_length=1, max_length=4096)


class OpenProjectRequest(BaseModel):
    directory: str = Field(min_length=1, max_length=4096)


class ImportDocumentsRequest(BaseModel):
    directory: str = Field(min_length=1, max_length=4096)
    files: list[str] = Field(min_length=1, max_length=100)


def create_app(
    frontend_dist: Path = FRONTEND_DIST,
    key_store: ProjectKeyStore | None = None,
) -> FastAPI:
    """Create the local API and, after a frontend build, serve its static UI."""
    app = FastAPI(
        title="Blot Local Service",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    local_token = secrets.token_urlsafe(32)
    dev_origins = _configured_dev_origins()
    app.state.local_token = local_token
    app.state.project_service = ProjectService(key_store) if key_store is not None else None

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(dev_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-Local-App-Token"],
        max_age=300,
    )

    @app.middleware("http")
    async def local_api_boundary(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            if not _is_loopback_request(request) or not _is_local_host(request.url.hostname):
                return JSONResponse({"detail": "Local API accepts loopback requests only."}, status_code=403)
            if not _origin_is_allowed(request, dev_origins):
                return JSONResponse({"detail": "Request origin is not allowed."}, status_code=403)
            if request.method != "OPTIONS" and request.url.path not in {"/api/health", "/api/bootstrap"}:
                supplied = request.headers.get("x-local-app-token", "")
                if not hmac.compare_digest(supplied, local_token):
                    return JSONResponse({"detail": "A valid local app token is required."}, status_code=401)
        return await call_next(request)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "blot-local", "version": "0.1.0"}

    @app.get("/api/bootstrap")
    async def bootstrap(request: Request) -> JSONResponse:
        if not _is_loopback_request(request) or not _is_local_host(request.url.hostname):
            raise HTTPException(status_code=403, detail="Local API accepts loopback requests only.")
        response = JSONResponse({"localAppToken": local_token})
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        return response

    @app.get("/api/session")
    async def session() -> dict[str, str]:
        return {"status": "ready", "storage": "not-configured"}

    def project_service() -> ProjectService:
        if app.state.project_service is None:
            app.state.project_service = ProjectService(OSKeyringProjectKeyStore())
        return app.state.project_service

    @app.post("/api/projects", status_code=status.HTTP_201_CREATED)
    def create_project(payload: CreateProjectRequest) -> dict[str, object]:
        try:
            project = project_service().create(payload.directory, payload.name)
            return {**project.to_public_dict(), "documents": []}
        except KeyStoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ProjectError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/projects/open")
    def open_project(payload: OpenProjectRequest) -> dict[str, object]:
        try:
            service = project_service()
            project = service.open(payload.directory)
            documents = service.list_documents(payload.directory)
            return {
                **project.to_public_dict(),
                "documents": [document.to_public_dict() for document in documents],
            }
        except KeyStoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ProjectError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/dialogs/project-folder")
    def choose_project_folder() -> dict[str, str | bool]:
        try:
            selected = pick_project_directory()
        except FolderPickerError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        if selected is None:
            return {"cancelled": True}
        return {"cancelled": False, "directory": str(selected)}

    @app.get("/api/dialogs/document-files")
    def choose_document_files() -> dict[str, object]:
        try:
            selected = pick_document_files()
        except FolderPickerError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        if selected is None:
            return {"cancelled": True}
        return {"cancelled": False, "files": [str(path) for path in selected]}

    @app.post("/api/projects/documents")
    def import_project_documents(payload: ImportDocumentsRequest) -> dict[str, object]:
        try:
            documents = project_service().import_documents(payload.directory, payload.files)
            return {"documents": [document.to_public_dict() for document in documents]}
        except KeyStoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ProjectError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    index_file = frontend_dist / "obfuscation-workspace.html"

    @app.get("/")
    async def root() -> Response:
        if index_file.is_file():
            return RedirectResponse("/obfuscation-workspace.html", status_code=307)
        return JSONResponse(
            {"detail": "Frontend build is missing. Run `npm run build` from the project root."},
            status_code=503,
        )

    if frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dist, check_dir=False), name="frontend")

    return app


app = create_app()


def run() -> None:
    port = int(os.environ.get("BLOT_PORT", "8765"))
    uvicorn.run(
        "backend.app.main:app",
        host="127.0.0.1",
        port=port,
        reload=False,
        access_log=False,
        log_level=os.environ.get("BLOT_LOG_LEVEL", "warning"),
    )
