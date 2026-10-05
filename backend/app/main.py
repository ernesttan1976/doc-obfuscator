from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .candidate_engine import CandidateError
from .document_adapters import DocumentAdapterError
from .folder_picker import (
    FolderPickerError,
    pick_backup_file,
    pick_document_files,
    pick_project_directory,
)
from .key_store import KeyStoreUnavailable, OSKeyringProjectKeyStore, ProjectKeyStore
from .model_manager import LocalModelManager, ModelManagerError
from .ollaya_scoring import LocalOllayaScorer
from .page_preview import PagePreviewError
from .projects import ProjectError, ProjectService

logger = logging.getLogger(__name__)

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


class PreviewDocumentRequest(BaseModel):
    directory: str = Field(min_length=1, max_length=4096)
    document_id: str = Field(min_length=1, max_length=100)
    version_id: str | None = Field(default=None, max_length=100)


class ExportPreviewRequest(BaseModel):
    directory: str = Field(min_length=1, max_length=4096)
    document_id: str = Field(min_length=1, max_length=100)
    level: int = Field(ge=1, le=10)


class ExportObfuscationRequest(BaseModel):
    directory: str = Field(min_length=1, max_length=4096)
    document_id: str = Field(min_length=1, max_length=100)
    plan_id: str = Field(min_length=1, max_length=100)
    acknowledge_warnings: bool = False


class DocumentVersionRequest(BaseModel):
    directory: str = Field(min_length=1, max_length=4096)
    document_id: str = Field(min_length=1, max_length=100)
    version_id: str = Field(min_length=1, max_length=100)


class RestorationPreviewRequest(BaseModel):
    directory: str = Field(min_length=1, max_length=4096)
    document_id: str = Field(min_length=1, max_length=100)
    source_version_id: str = Field(min_length=1, max_length=100)
    returned_path: str = Field(min_length=1, max_length=4096)


class RestorationCommitRequest(BaseModel):
    directory: str = Field(min_length=1, max_length=4096)
    plan_id: str = Field(min_length=1, max_length=100)


class PortableBackupRequest(BaseModel):
    directory: str = Field(min_length=1, max_length=4096)
    passphrase: str = Field(min_length=12, max_length=1024)


class PortableBackupRestoreRequest(PortableBackupRequest):
    backup_path: str = Field(min_length=1, max_length=4096)


class SessionUnlockRequest(BaseModel):
    directory: str = Field(min_length=1, max_length=4096)


class AnalyzeCandidatesRequest(BaseModel):
    directory: str = Field(min_length=1, max_length=4096)
    document_id: str = Field(min_length=1, max_length=100)
    manual_terms: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(
        default_factory=list,
        max_length=100,
    )


class CandidateDecisionRequest(BaseModel):
    directory: str = Field(min_length=1, max_length=4096)
    document_id: str = Field(min_length=1, max_length=100)
    candidate_id: str = Field(min_length=1, max_length=100)
    decision: str = Field(min_length=1, max_length=20)


class CandidateGroupRequest(BaseModel):
    directory: str = Field(min_length=1, max_length=4096)
    document_id: str = Field(min_length=1, max_length=100)
    operation: Literal["add", "remove", "split", "merge"]
    candidate_ids: list[str] = Field(default_factory=list, max_length=1000)
    group_id: str | None = Field(default=None, max_length=100)
    group_ids: list[str] = Field(default_factory=list, max_length=1000)


class ModelDownloadRequest(BaseModel):
    confirmed: bool = False


def create_app(
    frontend_dist: Path = FRONTEND_DIST,
    key_store: ProjectKeyStore | None = None,
    models_directory: Path | None = None,
    ollaya_scorer: LocalOllayaScorer | None = None,
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
    app.state.session_last_activity = time.monotonic()
    app.state.session_locked = False
    app.state.session_idle_timeout_seconds = 15 * 60
    app.state.active_project_directory = None
    app.state.model_manager = LocalModelManager(models_directory)
    app.state.ollaya_scorer = ollaya_scorer or LocalOllayaScorer()
    app.state.project_service = (
        ProjectService(
            key_store,
            app.state.model_manager.extract_entities,
            ollaya_scorer=app.state.ollaya_scorer,
        )
        if key_store is not None
        else None
    )

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
                now = time.monotonic()
                if (
                    request.url.path != "/api/session/unlock"
                    and app.state.active_project_directory is not None
                    and now - app.state.session_last_activity >= app.state.session_idle_timeout_seconds
                ):
                    app.state.session_locked = True
                    service = app.state.project_service
                    if service is not None:
                        service.clear_pending_operations()
                if app.state.session_locked and request.url.path != "/api/session/unlock":
                    return JSONResponse(
                        {"detail": "The local project session is locked after 15 minutes of inactivity."},
                        status_code=423,
                    )
                if request.url.path not in {"/api/session", "/api/session/unlock"}:
                    app.state.session_last_activity = now
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
    async def session() -> dict[str, str | int]:
        return {
            "status": "locked" if app.state.session_locked else "ready",
            "storage": "os-credential-store",
            "idleTimeoutSeconds": app.state.session_idle_timeout_seconds,
        }

    @app.post("/api/session/activity")
    async def record_session_activity() -> dict[str, str]:
        return {"status": "ready"}

    @app.post("/api/session/unlock")
    def unlock_session(payload: SessionUnlockRequest) -> dict[str, str]:
        try:
            project = project_service().open(payload.directory)
        except KeyStoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ProjectError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        app.state.session_locked = False
        app.state.active_project_directory = str(project.directory)
        app.state.session_last_activity = time.monotonic()
        return {"status": "ready", "storage": "os-credential-store"}

    @app.get("/api/models/ner/status")
    def ner_model_status() -> dict[str, object]:
        return app.state.model_manager.status()

    @app.post("/api/models/ner/download")
    def download_ner_model(payload: ModelDownloadRequest) -> dict[str, object]:
        try:
            return app.state.model_manager.start_download(payload.confirmed)
        except ModelManagerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/models/ner/download")
    def cancel_ner_model_download() -> dict[str, object]:
        try:
            return app.state.model_manager.cancel_download()
        except ModelManagerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/models/ollaya/status")
    def ollaya_model_status() -> dict[str, object]:
        return app.state.ollaya_scorer.status()

    def project_service() -> ProjectService:
        if app.state.project_service is None:
            app.state.project_service = ProjectService(
                OSKeyringProjectKeyStore(),
                app.state.model_manager.extract_entities,
                ollaya_scorer=app.state.ollaya_scorer,
            )
        return app.state.project_service

    @app.post("/api/projects", status_code=status.HTTP_201_CREATED)
    def create_project(payload: CreateProjectRequest) -> dict[str, object]:
        try:
            project = project_service().create(payload.directory, payload.name)
            app.state.active_project_directory = str(project.directory)
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
            app.state.active_project_directory = str(project.directory)
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

    @app.get("/api/dialogs/backup-file")
    def choose_backup_file() -> dict[str, str | bool]:
        try:
            selected = pick_backup_file()
        except FolderPickerError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        if selected is None:
            return {"cancelled": True}
        return {"cancelled": False, "path": str(selected)}

    @app.post("/api/projects/documents")
    def import_project_documents(payload: ImportDocumentsRequest) -> dict[str, object]:
        try:
            documents = project_service().import_documents(payload.directory, payload.files)
            return {"documents": [document.to_public_dict() for document in documents]}
        except KeyStoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ProjectError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/projects/document-preview")
    def preview_project_document(payload: PreviewDocumentRequest) -> dict[str, object]:
        try:
            return project_service().preview_document(
                payload.directory,
                payload.document_id,
                payload.version_id,
            )
        except KeyStoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except DocumentAdapterError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ProjectError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/projects/document-page-preview")
    def preview_project_document_pages(payload: PreviewDocumentRequest) -> Response:
        try:
            pdf = project_service().preview_document_pages(
                payload.directory,
                payload.document_id,
                payload.version_id,
            )
        except KeyStoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except PagePreviewError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        except ProjectError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(
            content=pdf,
            media_type="application/pdf",
            headers={"Cache-Control": "no-store, private"},
        )

    @app.post("/api/projects/export-preview")
    def preview_project_obfuscation(payload: ExportPreviewRequest) -> dict[str, object]:
        try:
            return project_service().preview_obfuscation(
                payload.directory,
                payload.document_id,
                payload.level,
            )
        except KeyStoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except DocumentAdapterError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ProjectError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/projects/export")
    def export_project_obfuscation(payload: ExportObfuscationRequest) -> dict[str, object]:
        try:
            return project_service().export_obfuscation(
                payload.directory,
                payload.document_id,
                payload.plan_id,
                payload.acknowledge_warnings,
            )
        except KeyStoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except DocumentAdapterError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ProjectError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/projects/document-version-download")
    def download_project_version(payload: DocumentVersionRequest) -> Response:
        try:
            path, filename = project_service().document_version_download(
                payload.directory,
                payload.document_id,
                payload.version_id,
            )
        except KeyStoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ProjectError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(path, filename=filename, media_type="application/octet-stream")

    @app.post("/api/projects/restore-preview")
    def preview_project_restoration(payload: RestorationPreviewRequest) -> dict[str, object]:
        try:
            return project_service().preview_restoration(
                payload.directory,
                payload.document_id,
                payload.source_version_id,
                payload.returned_path,
            )
        except KeyStoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except DocumentAdapterError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ProjectError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/projects/restore")
    def commit_project_restoration(payload: RestorationCommitRequest) -> dict[str, object]:
        try:
            return project_service().commit_restoration(payload.directory, payload.plan_id)
        except KeyStoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ProjectError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/projects/backup")
    def create_project_backup(payload: PortableBackupRequest) -> Response:
        try:
            content = project_service().create_portable_backup(payload.directory, payload.passphrase)
        except KeyStoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ProjectError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(
            content,
            media_type="application/vnd.blot.encrypted-backup",
            headers={"Content-Disposition": 'attachment; filename="blot-project.blotbackup"'},
        )

    @app.post("/api/projects/backup/restore")
    def restore_project_backup(payload: PortableBackupRestoreRequest) -> dict[str, object]:
        try:
            project = project_service().restore_portable_backup(
                payload.directory,
                payload.backup_path,
                payload.passphrase,
            )
            app.state.active_project_directory = str(project.directory)
            service = project_service()
            return {
                **project.to_public_dict(),
                "documents": [document.to_public_dict() for document in service.list_documents(payload.directory)],
            }
        except KeyStoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ProjectError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/projects/document-candidates")
    def analyze_project_document_candidates(payload: AnalyzeCandidatesRequest) -> dict[str, object]:
        try:
            return project_service().analyze_document_candidates(
                payload.directory,
                payload.document_id,
                payload.manual_terms,
            )
        except KeyStoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except DocumentAdapterError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (CandidateError, ProjectError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/projects/document-candidates/stream")
    async def stream_project_document_candidates(payload: AnalyzeCandidatesRequest) -> StreamingResponse:
        events: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def publish(event: dict[str, object]) -> None:
            loop.call_soon_threadsafe(events.put_nowait, event)

        def analyze() -> None:
            try:
                result = project_service().analyze_document_candidates(
                    payload.directory,
                    payload.document_id,
                    payload.manual_terms,
                    on_candidate=lambda candidate: publish({"type": "candidate", "candidate": candidate}),
                )
            except (KeyStoreUnavailable, DocumentAdapterError, CandidateError, ProjectError) as exc:
                publish({"type": "error", "detail": str(exc)})
            except Exception:
                logger.exception("Streaming document candidate analysis failed")
                publish({"type": "error", "detail": "Could not analyze supported text."})
            else:
                publish({"type": "complete", "data": result})

        async def event_stream():
            worker = asyncio.create_task(asyncio.to_thread(analyze))
            while True:
                event = await events.get()
                yield f"{json.dumps(event, separators=(',', ':'))}\n"
                if event["type"] in {"complete", "error"}:
                    await worker
                    break

        return StreamingResponse(
            event_stream(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store, private", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/projects/candidate-decision")
    def update_project_candidate_decision(payload: CandidateDecisionRequest) -> dict[str, object]:
        try:
            return project_service().set_candidate_decision(
                payload.directory,
                payload.document_id,
                payload.candidate_id,
                payload.decision,
            )
        except KeyStoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (CandidateError, ProjectError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/projects/candidate-groups")
    def update_project_candidate_groups(payload: CandidateGroupRequest) -> dict[str, object]:
        try:
            groups = project_service().update_candidate_groups(
                payload.directory,
                payload.document_id,
                payload.operation,
                payload.candidate_ids,
                payload.group_id,
                payload.group_ids,
            )
            return {"groups": groups}
        except KeyStoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (CandidateError, ProjectError) as exc:
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
    # Make the per-call Ollaya request/response records visible alongside Uvicorn logs.
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        "backend.app.main:app",
        host="127.0.0.1",
        port=port,
        reload=False,
        access_log=False,
        log_level=os.environ.get("BLOT_LOG_LEVEL", "warning"),
    )
