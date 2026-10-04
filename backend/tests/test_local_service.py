import io
import os
import re
import secrets
import sqlite3
import time
import zipfile
from types import SimpleNamespace

import httpx
import pytest

from backend.app import folder_picker
from backend.app import main as main_module
from backend.app.key_store import KeyStoreUnavailable
from backend.app.local_crypto import EncryptedStateError, decrypt_state, encrypt_state
from backend.app.main import create_app
from backend.app.projects import ProjectError, ProjectService


class MemoryKeyStore:
    def __init__(self):
        self.keys = {}

    def get_or_create(self, project_id):
        if project_id not in self.keys:
            self.keys[project_id] = secrets.token_bytes(32)
        return self.keys[project_id]

    def get(self, project_id):
        if project_id not in self.keys:
            raise KeyStoreUnavailable("missing test key")
        return self.keys[project_id]

    def delete(self, project_id):
        self.keys.pop(project_id, None)


def local_client(tmp_path, key_store=None):
    app = create_app(tmp_path, key_store=key_store, models_directory=tmp_path / "models")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 54123))
    return app, httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765")


def minimal_docx(text="Office preview content"):
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'/>",
        )
        archive.writestr(
            "word/document.xml",
            "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
            f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>",
        )
    return package.getvalue()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_health_is_loopback_only_and_exposes_no_document_data(tmp_path):
    _, client_context = local_client(tmp_path)
    async with client_context as client:
        response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "blot-local", "version": "0.1.0"}


@pytest.mark.anyio
async def test_bootstrap_token_is_uncached_and_required_for_private_api(tmp_path):
    _, client_context = local_client(tmp_path)
    async with client_context as client:
        bootstrap = await client.get("/api/bootstrap", headers={"Origin": "http://localhost:5173"})

        assert bootstrap.status_code == 200
        assert bootstrap.headers["cache-control"] == "no-store, private"
        token = bootstrap.json()["localAppToken"]
        assert len(token) >= 40
        assert (await client.get("/api/session")).status_code == 401
        session = await client.get("/api/session", headers={"X-Local-App-Token": token})

    assert session.json() == {
        "status": "ready",
        "storage": "os-credential-store",
        "idleTimeoutSeconds": 900,
    }


@pytest.mark.anyio
async def test_idle_project_session_locks_private_api_until_os_store_unlock(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    app, client_context = local_client(tmp_path, MemoryKeyStore())
    async with client_context as client:
        headers = {"X-Local-App-Token": app.state.local_token}
        created = await client.post(
            "/api/projects",
            json={"name": "Project", "directory": str(root)},
            headers=headers,
        )
        assert created.status_code == 201
        app.state.session_last_activity = time.monotonic() - 901
        locked = await client.get("/api/session", headers=headers)
        denied = await client.post("/api/projects/open", json={"directory": str(root)}, headers=headers)
        unlocked = await client.post("/api/session/unlock", json={"directory": str(root)}, headers=headers)
        reopened = await client.post("/api/projects/open", json={"directory": str(root)}, headers=headers)

    assert locked.status_code == denied.status_code == 423
    assert unlocked.status_code == 200
    assert unlocked.json()["storage"] == "os-credential-store"
    assert reopened.status_code == 200


@pytest.mark.anyio
async def test_ner_model_status_is_private_and_download_requires_explicit_confirmation(tmp_path):
    app, client_context = local_client(tmp_path)
    async with client_context as client:
        denied = await client.get("/api/models/ner/status")
        headers = {"X-Local-App-Token": app.state.local_token}
        status_response = await client.get("/api/models/ner/status", headers=headers)
        unconfirmed = await client.post("/api/models/ner/download", json={}, headers=headers)

    assert denied.status_code == 401
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "idle"
    assert status_response.json()["modelId"] == "knowledgator/gliner-multitask-large-v0.5"
    assert status_response.json()["revision"] == "7a95e168036db9ec6f914c0cc6b218edbd87f310"
    assert status_response.json()["license"] == "Apache-2.0"
    assert status_response.json()["installed"] is False
    assert unconfirmed.status_code == 400
    assert not (tmp_path / "models").exists()


@pytest.mark.anyio
async def test_minilm_model_status_is_private_and_download_requires_explicit_confirmation(tmp_path):
    app, client_context = local_client(tmp_path)
    async with client_context as client:
        denied = await client.get("/api/models/minilm/status")
        headers = {"X-Local-App-Token": app.state.local_token}
        status_response = await client.get("/api/models/minilm/status", headers=headers)
        unconfirmed = await client.post("/api/models/minilm/download", json={}, headers=headers)

    assert denied.status_code == 401
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "idle"
    assert status_response.json()["modelId"] == "sentence-transformers/all-MiniLM-L6-v2"
    assert status_response.json()["revision"] == "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
    assert status_response.json()["license"] == "Apache-2.0"
    assert status_response.json()["installed"] is False
    assert unconfirmed.status_code == 400
    assert not (tmp_path / "models").exists()


@pytest.mark.anyio
async def test_untrusted_origin_is_rejected(tmp_path):
    _, client_context = local_client(tmp_path)
    async with client_context as client:
        response = await client.get("/api/bootstrap", headers={"Origin": "https://example.invalid"})

    assert response.status_code == 403


@pytest.mark.anyio
async def test_root_explains_when_frontend_build_is_missing(tmp_path):
    _, client_context = local_client(tmp_path)
    async with client_context as client:
        response = await client.get("/", follow_redirects=False)

    assert response.status_code == 503
    assert "npm run build" in response.json()["detail"]


@pytest.mark.anyio
async def test_root_serves_built_frontend_when_available(tmp_path):
    (tmp_path / "obfuscation-workspace.html").write_text("<!doctype html><title>Blot</title>")
    _, client_context = local_client(tmp_path)
    async with client_context as client:
        root = await client.get("/", follow_redirects=False)
        page = await client.get("/obfuscation-workspace.html")

    assert root.status_code == 307
    assert root.headers["location"] == "/obfuscation-workspace.html"
    assert page.status_code == 200
    assert "<title>Blot</title>" in page.text


def test_encrypted_state_hides_terms_and_detects_tampering():
    key = b"K" * 32
    state = {"name": "Cedar Planning", "mapping": {"T_001": "Alex Tan"}}
    encrypted = encrypt_state("project-123", state, key)

    assert b"Cedar Planning" not in encrypted
    assert b"Alex Tan" not in encrypted
    assert decrypt_state("project-123", encrypted, key) == state
    with pytest.raises(EncryptedStateError):
        decrypt_state("other-project", encrypted, key)


def test_project_create_open_and_encrypted_sidecar(tmp_path):
    root = tmp_path / "Cedar briefing"
    root.mkdir()
    key_store = MemoryKeyStore()
    service = ProjectService(key_store)

    created = service.create(root, "Cedar briefing")
    state_file = root / ".blot" / "private-state.enc"
    manifest = (root / ".blot" / "project.json").read_text(encoding="utf-8")
    restored = service.open(root)

    assert created.project_id == restored.project_id
    assert restored.name == "Cedar briefing"
    assert "Cedar briefing" not in manifest
    assert b"Cedar briefing" not in state_file.read_bytes()
    assert service.load_private_state(root)["mapping"] == {}
    assert (root / ".blot" / "project.sqlite3").is_file()


def test_project_refuses_overwrite_and_relative_or_unwritable_locations(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    service = ProjectService(MemoryKeyStore())
    service.create(root, "Project")

    with pytest.raises(ProjectError, match="Open it"):
        service.create(root, "Another project")
    with pytest.raises(ProjectError, match="absolute"):
        service.create("relative/path", "Project")


def test_project_open_requires_its_os_protected_key(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    ProjectService(MemoryKeyStore()).create(root, "Project")

    with pytest.raises(KeyStoreUnavailable, match="missing test key"):
        ProjectService(MemoryKeyStore()).open(root)


def test_project_import_copies_an_immutable_original_and_records_version(tmp_path):
    root = tmp_path / "project"
    source = tmp_path / "meeting notes.TXT"
    root.mkdir()
    source.write_text("Alex Tan — confidential", encoding="utf-8")
    service = ProjectService(MemoryKeyStore())
    service.create(root, "Project")

    imported = service.import_documents(root, [source])
    original = root / ".blot" / "originals" / imported[0].document_id / "original.txt"
    listed = service.list_documents(root)

    assert len(imported) == 1
    assert imported[0].name == source.name
    assert original.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert source.read_text(encoding="utf-8") == "Alex Tan — confidential"
    assert listed == imported
    if os.name != "nt":
        assert original.stat().st_mode & 0o777 == 0o400


def test_project_preview_parses_saved_text_without_changing_the_original(tmp_path):
    root = tmp_path / "project"
    source = tmp_path / "notes.md"
    root.mkdir()
    source.write_bytes(b"# Alex Tan\r\nProject Cedar\r\n")
    service = ProjectService(MemoryKeyStore())
    service.create(root, "Project")
    imported = service.import_documents(root, [source])[0]
    stored_path = root / ".blot" / "originals" / imported.document_id / "original.md"
    original_bytes = stored_path.read_bytes()

    preview = service.preview_document(root, imported.document_id)

    assert preview["format"] == "MD"
    assert preview["encoding"] == "utf-8"
    assert preview["lineEndings"] == "crlf"
    assert preview["text"] == "# Alex Tan\r\nProject Cedar\r\n"
    assert stored_path.read_bytes() == original_bytes


def test_project_preview_rejects_paths_that_escape_originals(tmp_path):
    root = tmp_path / "project"
    source = tmp_path / "notes.txt"
    root.mkdir()
    source.write_text("safe text", encoding="utf-8")
    service = ProjectService(MemoryKeyStore())
    service.create(root, "Project")
    imported = service.import_documents(root, [source])[0]
    connection = sqlite3.connect(root / ".blot" / "project.sqlite3")
    try:
        connection.execute(
            "UPDATE documents SET original_path = ? WHERE id = ?",
            ("../../notes.txt", imported.document_id),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ProjectError, match="path is invalid"):
        service.preview_document(root, imported.document_id)


def test_project_import_rejects_unsupported_files_without_copying(tmp_path):
    root = tmp_path / "project"
    source = tmp_path / "script.py"
    root.mkdir()
    source.write_text("print('not a document')", encoding="utf-8")
    service = ProjectService(MemoryKeyStore())
    service.create(root, "Project")

    with pytest.raises(ProjectError, match="Select only"):
        service.import_documents(root, [source])

    assert not (root / ".blot" / "originals").exists()


def test_project_import_rejects_files_over_100_mb_before_copying(tmp_path):
    root = tmp_path / "project"
    source = tmp_path / "large.txt"
    root.mkdir()
    with source.open("wb") as large_file:
        large_file.truncate(100 * 1024 * 1024 + 1)
    service = ProjectService(MemoryKeyStore())
    service.create(root, "Project")

    with pytest.raises(ProjectError, match="100 MB"):
        service.import_documents(root, [source])

    assert not (root / ".blot" / "originals").exists()


@pytest.mark.anyio
async def test_project_api_requires_launch_token_and_encrypts_initial_state(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    app, client_context = local_client(tmp_path, MemoryKeyStore())
    async with client_context as client:
        payload = {"name": "Synthetic Project", "directory": str(root)}
        denied = await client.post("/api/projects", json=payload)
        created = await client.post(
            "/api/projects",
            json=payload,
            headers={"X-Local-App-Token": app.state.local_token},
        )

    assert denied.status_code == 401
    assert created.status_code == 201
    assert created.json()["graphProtection"] == "encrypted"
    assert b"Synthetic Project" not in (root / ".blot" / "private-state.enc").read_bytes()


@pytest.mark.anyio
async def test_project_document_import_is_token_protected_and_returns_saved_versions(tmp_path):
    root = tmp_path / "project"
    source = tmp_path / "notes.md"
    root.mkdir()
    source.write_text("Synthetic project notes", encoding="utf-8")
    app, client_context = local_client(tmp_path, MemoryKeyStore())
    async with client_context as client:
        await client.post(
            "/api/projects",
            json={"name": "Project", "directory": str(root)},
            headers={"X-Local-App-Token": app.state.local_token},
        )
        payload = {"directory": str(root), "files": [str(source)]}
        denied = await client.post("/api/projects/documents", json=payload)
        imported = await client.post(
            "/api/projects/documents",
            json=payload,
            headers={"X-Local-App-Token": app.state.local_token},
        )
        opened = await client.post(
            "/api/projects/open",
            json={"directory": str(root)},
            headers={"X-Local-App-Token": app.state.local_token},
        )

    assert denied.status_code == 401
    assert imported.status_code == 200
    assert imported.json()["documents"][0]["status"] == "original"
    assert opened.json()["documents"] == imported.json()["documents"]


@pytest.mark.anyio
async def test_document_preview_is_token_protected_and_returns_parsed_content(tmp_path):
    root = tmp_path / "project"
    source = tmp_path / "brief.md"
    office_source = tmp_path / "brief.docx"
    root.mkdir()
    source.write_bytes(b"Alex Tan\r\nProject Cedar\r\n")
    office_source.write_bytes(minimal_docx())
    app, client_context = local_client(tmp_path, MemoryKeyStore())
    async with client_context as client:
        headers = {"X-Local-App-Token": app.state.local_token}
        await client.post(
            "/api/projects",
            json={"name": "Project", "directory": str(root)},
            headers=headers,
        )
        imported = await client.post(
            "/api/projects/documents",
            json={"directory": str(root), "files": [str(source)]},
            headers=headers,
        )
        imported_office = await client.post(
            "/api/projects/documents",
            json={"directory": str(root), "files": [str(office_source)]},
            headers=headers,
        )
        document_id = imported.json()["documents"][0]["id"]
        payload = {"directory": str(root), "document_id": document_id}
        denied = await client.post("/api/projects/document-preview", json=payload)
        preview = await client.post("/api/projects/document-preview", json=payload, headers=headers)
        office_id = imported_office.json()["documents"][0]["id"]
        office_preview = await client.post(
            "/api/projects/document-preview",
            json={"directory": str(root), "document_id": office_id},
            headers=headers,
        )

    assert denied.status_code == 401
    assert preview.status_code == 200
    assert preview.json()["format"] == "MD"
    assert preview.json()["text"] == "Alex Tan\r\nProject Cedar\r\n"
    assert office_preview.status_code == 200
    assert office_preview.json()["format"] == "DOCX"
    assert office_preview.json()["text"] == "Office preview content"
    assert office_preview.json()["previewSections"] == [
        {"part": "word/document.xml", "text": "Office preview content"}
    ]


@pytest.mark.anyio
async def test_candidate_api_persists_encrypted_version_scoped_graph_and_pinned_decisions(tmp_path):
    root = tmp_path / "project"
    source = tmp_path / "brief.md"
    root.mkdir()
    source.write_text(
        "Alex Tan and Alex Tann met on May 7, 2026. Contact alex@example.test.",
        encoding="utf-8",
    )
    app, client_context = local_client(tmp_path, MemoryKeyStore())
    async with client_context as client:
        headers = {"X-Local-App-Token": app.state.local_token}
        await client.post("/api/projects", json={"name": "Project", "directory": str(root)}, headers=headers)
        imported = await client.post(
            "/api/projects/documents",
            json={"directory": str(root), "files": [str(source)]},
            headers=headers,
        )
        document_id = imported.json()["documents"][0]["id"]
        request = {"directory": str(root), "document_id": document_id}
        denied = await client.post("/api/projects/document-candidates", json=request)
        analysis = await client.post(
            "/api/projects/document-candidates",
            json={**request, "manual_terms": ["Alex Tan"]},
            headers=headers,
        )
        candidates = analysis.json()["candidates"]
        email = next(candidate for candidate in candidates if candidate["term"] == "alex@example.test")
        decision = await client.post(
            "/api/projects/candidate-decision",
            json={**request, "candidate_id": email["id"], "decision": "excluded"},
            headers=headers,
        )
        repeated = await client.post(
            "/api/projects/document-candidates",
            json=request,
            headers=headers,
        )
        manual = next(candidate for candidate in candidates if candidate["term"].casefold() == "alex tan")
        variant = next(candidate for candidate in candidates if candidate["term"] == "Alex Tann")
        first_group = await client.post(
            "/api/projects/candidate-groups",
            json={**request, "operation": "add", "candidate_ids": [manual["id"], variant["id"]]},
            headers=headers,
        )
        first_group_id = first_group.json()["groups"][0]["id"]
        second_group = await client.post(
            "/api/projects/candidate-groups",
            json={**request, "operation": "add", "candidate_ids": [email["id"]]},
            headers=headers,
        )
        second_group_id = next(
            group["id"] for group in second_group.json()["groups"] if group["id"] != first_group_id
        )
        merged = await client.post(
            "/api/projects/candidate-groups",
            json={**request, "operation": "merge", "group_ids": [first_group_id, second_group_id]},
            headers=headers,
        )
        merged_group = merged.json()["groups"][0]
        split = await client.post(
            "/api/projects/candidate-groups",
            json={
                **request,
                "operation": "split",
                "group_id": merged_group["id"],
                "candidate_ids": [variant["id"]],
            },
            headers=headers,
        )
        split_group = next(
            group for group in split.json()["groups"] if group["candidateIds"] == [variant["id"]]
        )
        removed = await client.post(
            "/api/projects/candidate-groups",
            json={
                **request,
                "operation": "remove",
                "group_id": split_group["id"],
                "candidate_ids": [variant["id"]],
            },
            headers=headers,
        )
        absent_phrase = await client.post(
            "/api/projects/document-candidates",
            json={**request, "manual_terms": ["not in the document"]},
            headers=headers,
        )

    assert denied.status_code == 401
    assert analysis.status_code == 200
    assert analysis.json()["versionId"] == imported.json()["documents"][0]["versionId"]
    assert analysis.json()["nerCandidateCount"] == 0
    assert analysis.json()["nerTruncated"] is False
    assert analysis.json()["nerWarning"] is None
    assert analysis.json()["similarityProposalCount"] == 0
    assert analysis.json()["similarityWarning"] is None
    assert analysis.json()["groups"] == []
    assert analysis.json()["proposals"]
    assert all(proposal["confirmed"] is False for proposal in analysis.json()["proposals"])
    assert manual["decision"] == "included"
    assert manual["pinned"] is True
    assert decision.status_code == 200
    assert decision.json()["decision"] == "excluded"
    assert decision.json()["pinned"] is True
    persisted_email = next(
        candidate for candidate in repeated.json()["candidates"] if candidate["id"] == email["id"]
    )
    assert persisted_email["decision"] == "excluded"
    assert persisted_email["pinned"] is True
    assert first_group.status_code == second_group.status_code == merged.status_code == 200
    assert len(first_group.json()["groups"]) == 1
    assert all(group["confirmed"] is True for group in merged.json()["groups"])
    assert variant["id"] in split_group["candidateIds"]
    assert all(variant["id"] not in group["candidateIds"] for group in removed.json()["groups"])
    assert absent_phrase.status_code == 400
    encrypted_state = (root / ".blot" / "private-state.enc").read_bytes()
    assert b"alex@example.test" not in encrypted_state
    assert b"Alex Tan" not in encrypted_state


@pytest.mark.anyio
async def test_obfuscation_preview_requires_explicit_ack_and_saves_private_immutable_version(tmp_path):
    root = tmp_path / "project"
    source = tmp_path / "brief.md"
    root.mkdir()
    source.write_bytes(b"Alex Tan contact alex@example.test. Project Cedar remains. Existing [[T_123]] remains.")
    original_bytes = source.read_bytes()
    app, client_context = local_client(tmp_path, MemoryKeyStore())
    async with client_context as client:
        headers = {"X-Local-App-Token": app.state.local_token}
        await client.post("/api/projects", json={"name": "Project", "directory": str(root)}, headers=headers)
        imported = await client.post(
            "/api/projects/documents",
            json={"directory": str(root), "files": [str(source)]},
            headers=headers,
        )
        document = imported.json()["documents"][0]
        request = {"directory": str(root), "document_id": document["id"]}
        analysis = await client.post(
            "/api/projects/document-candidates",
            json={**request, "manual_terms": ["Alex Tan"]},
            headers=headers,
        )
        email = next(item for item in analysis.json()["candidates"] if item["category"] == "EMAIL")
        await client.post(
            "/api/projects/candidate-decision",
            json={**request, "candidate_id": email["id"], "decision": "excluded"},
            headers=headers,
        )
        broad_preview_response = await client.post(
            "/api/projects/export-preview",
            json={**request, "level": 1},
            headers=headers,
        )
        denied = await client.post("/api/projects/export-preview", json={**request, "level": 1})
        preview_response = await client.post(
            "/api/projects/export-preview",
            json={**request, "level": 3},
            headers=headers,
        )
        all_excluded = await client.post(
            "/api/projects/export-preview",
            json={**request, "level": 10},
            headers=headers,
        )
        broad_preview = broad_preview_response.json()
        preview = preview_response.json()
        unacknowledged = await client.post(
            "/api/projects/export",
            json={**request, "plan_id": preview["planId"]},
            headers=headers,
        )
        exported = await client.post(
            "/api/projects/export",
            json={**request, "plan_id": preview["planId"], "acknowledge_warnings": True},
            headers=headers,
        )
        downloaded = await client.post(
            "/api/projects/document-version-download",
            json={**request, "version_id": exported.json()["id"]},
            headers=headers,
        )
        version_preview = await client.post(
            "/api/projects/document-preview",
            json={**request, "version_id": exported.json()["id"]},
            headers=headers,
        )
        reopened = await client.post(
            "/api/projects/open",
            json={"directory": str(root)},
            headers=headers,
        )

    assert denied.status_code == 401
    assert preview_response.status_code == 200
    assert all_excluded.status_code == 400
    assert "No candidates have priority above this cutoff" in all_excluded.json()["detail"]
    assert preview["requiresAcknowledgement"] is True
    assert preview["preexistingPlaceholderCount"] == 1
    assert len(preview["matches"]) == 2
    assert {match["term"] for match in preview["matches"]} == {"Alex Tan", "Project Cedar"}
    assert any(match["term"] == "Project Cedar" for match in broad_preview["matches"])
    assert all(match["term"] != "alex@example.test" for match in preview["matches"])
    generated_tokens = re.findall(r"\[\[T_[0-9a-f]{6}\]\]", preview["preview"]["text"])
    assert len(generated_tokens) == 2
    assert unacknowledged.status_code == 409
    assert exported.status_code == 200
    version = exported.json()
    assert version["kind"] == "obfuscated"
    assert version["parentVersionId"] == document["versionId"]
    assert downloaded.status_code == 200
    assert version_preview.status_code == 200
    assert version_preview.json()["text"].startswith("[[T_")
    reopened_document = reopened.json()["documents"][0]
    assert any(version["id"] == exported.json()["id"] for version in reopened_document["versions"])
    assert b"Alex Tan" not in downloaded.content
    assert b"alex@example.test" in downloaded.content
    assert b"[[T_123]]" in downloaded.content
    assert source.read_bytes() == original_bytes
    stored_original = root / ".blot" / "originals" / document["id"] / "original.md"
    assert stored_original.read_bytes() == original_bytes
    assert (root / ".blot" / "outputs" / document["id"] / f"{version['id']}.md").read_bytes() == downloaded.content
    encrypted_state = (root / ".blot" / "private-state.enc").read_bytes()
    assert b"Alex Tan" not in encrypted_state
    assert b"alex@example.test" not in encrypted_state
    persisted_versions = app.state.project_service.list_documents(root)[0].versions
    assert any(item["id"] == version["id"] for item in persisted_versions)


@pytest.mark.anyio
async def test_returned_office_restoration_restores_only_exact_project_tokens(tmp_path):
    root = tmp_path / "project"
    source = tmp_path / "brief.docx"
    returned = tmp_path / "agent-return.docx"
    backup_path = tmp_path / "project.blotbackup"
    restored_root = tmp_path / "restored-project"
    root.mkdir()
    restored_root.mkdir()
    source.write_bytes(minimal_docx("Alex Tan"))
    original = source.read_bytes()
    app, client_context = local_client(tmp_path, MemoryKeyStore())
    async with client_context as client:
        headers = {"X-Local-App-Token": app.state.local_token}
        await client.post("/api/projects", json={"name": "Project", "directory": str(root)}, headers=headers)
        imported = await client.post(
            "/api/projects/documents",
            json={"directory": str(root), "files": [str(source)]},
            headers=headers,
        )
        document = imported.json()["documents"][0]
        request = {"directory": str(root), "document_id": document["id"]}
        analysis = await client.post(
            "/api/projects/document-candidates",
            json={**request, "manual_terms": ["Alex Tan"]},
            headers=headers,
        )
        assert analysis.status_code == 200, analysis.text
        export_preview = await client.post(
            "/api/projects/export-preview",
            json={**request, "level": 1},
            headers=headers,
        )
        token = export_preview.json()["matches"][0]["token"]
        exported = await client.post(
            "/api/projects/export",
            json={**request, "plan_id": export_preview.json()["planId"], "acknowledge_warnings": True},
            headers=headers,
        )
        assert exported.status_code == 200

        foreign = "[[T_foreign123]]"
        altered = "[[T_modified!]]"
        returned.write_bytes(
            minimal_docx(
                f"Agent moved {token} and repeated {token}; changed case {token.lower()}; "
                f"foreign {foreign}; altered {altered}."
            )
        )
        restoration = await client.post(
            "/api/projects/restore-preview",
            json={
                **request,
                "source_version_id": exported.json()["id"],
                "returned_path": str(returned),
            },
            headers=headers,
        )
        assert restoration.status_code == 200
        preview = restoration.json()
        stale_preview = await client.post(
            "/api/projects/restore-preview",
            json={
                **request,
                "source_version_id": exported.json()["id"],
                "returned_path": str(returned),
            },
            headers=headers,
        )
        private_state = app.state.project_service.load_private_state(root)
        previous_term = private_state["mapping"][token]["term"]
        private_state["mapping"][token]["term"] = "Changed during restoration review"
        app.state.project_service.save_private_state(root, private_state)
        stale_commit = await client.post(
            "/api/projects/restore",
            json={"directory": str(root), "plan_id": stale_preview.json()["planId"]},
            headers=headers,
        )
        private_state["mapping"][token]["term"] = previous_term
        app.state.project_service.save_private_state(root, private_state)
        restored = await client.post(
            "/api/projects/restore",
            json={"directory": str(root), "plan_id": preview["planId"]},
            headers=headers,
        )
        restored_preview = await client.post(
            "/api/projects/document-preview",
            json={**request, "version_id": restored.json()["id"]},
            headers=headers,
        )
        downloaded = await client.post(
            "/api/projects/document-version-download",
            json={**request, "version_id": restored.json()["id"]},
            headers=headers,
        )
        portable_backup = await client.post(
            "/api/projects/backup",
            json={"directory": str(root), "passphrase": "correct horse battery staple"},
            headers=headers,
        )
        backup_path.write_bytes(portable_backup.content)
        portable_restore = await client.post(
            "/api/projects/backup/restore",
            json={
                "directory": str(restored_root),
                "backup_path": str(backup_path),
                "passphrase": "correct horse battery staple",
            },
            headers=headers,
        )
        restored_project_document = portable_restore.json()["documents"][0]
        portable_restore_preview = await client.post(
            "/api/projects/restore-preview",
            json={
                "directory": str(restored_root),
                "document_id": restored_project_document["id"],
                "source_version_id": exported.json()["id"],
                "returned_path": str(returned),
            },
            headers=headers,
        )

    assert preview["report"]["restoredCount"] == 2
    assert preview["report"]["unresolvedCount"] == 3
    assert preview["report"]["unknownOrForeignCount"] == 1
    assert preview["report"]["alteredCount"] == 2
    assert "Alex Tan" in preview["preview"]["text"]
    assert token not in preview["preview"]["text"]
    assert token.lower() in preview["preview"]["text"]
    assert foreign in preview["preview"]["text"]
    assert altered in preview["preview"]["text"]
    assert restored.status_code == 200, restored.text
    assert stale_commit.status_code == 409
    assert restored_preview.status_code == 200, restored_preview.text
    assert downloaded.status_code == 200, downloaded.text
    assert portable_backup.status_code == portable_restore.status_code == 200
    assert portable_restore_preview.status_code == 200, portable_restore_preview.text
    assert b"Alex Tan" not in portable_backup.content
    assert portable_restore.json()["id"] != app.state.project_service.open(root).project_id
    assert portable_restore_preview.json()["report"] == preview["report"]
    if os.name != "nt":
        restored_original = restored_root / ".blot" / "originals" / document["id"] / "original.docx"
        assert restored_original.stat().st_mode & 0o222 == 0
    assert restored.json()["kind"] == "restored"
    assert restored_preview.json()["restoreReport"] == preview["report"]
    assert b"Alex Tan" in downloaded.content
    assert token.encode() not in downloaded.content
    assert foreign.encode() in downloaded.content
    assert altered.encode() in downloaded.content
    assert source.read_bytes() == original


@pytest.mark.anyio
async def test_export_plan_is_invalidated_when_candidate_decisions_change(tmp_path):
    root = tmp_path / "project"
    source = tmp_path / "brief.txt"
    root.mkdir()
    source.write_text("Alex Tan is listed here.", encoding="utf-8")
    app, client_context = local_client(tmp_path, MemoryKeyStore())
    async with client_context as client:
        headers = {"X-Local-App-Token": app.state.local_token}
        await client.post("/api/projects", json={"name": "Project", "directory": str(root)}, headers=headers)
        imported = await client.post(
            "/api/projects/documents",
            json={"directory": str(root), "files": [str(source)]},
            headers=headers,
        )
        request = {
            "directory": str(root),
            "document_id": imported.json()["documents"][0]["id"],
        }
        analysis = await client.post(
            "/api/projects/document-candidates",
            json={**request, "manual_terms": ["Alex Tan"]},
            headers=headers,
        )
        preview = await client.post(
            "/api/projects/export-preview",
            json={**request, "level": 1},
            headers=headers,
        )
        candidate = analysis.json()["candidates"][0]
        await client.post(
            "/api/projects/candidate-decision",
            json={**request, "candidate_id": candidate["id"], "decision": "excluded"},
            headers=headers,
        )
        stale_export = await client.post(
            "/api/projects/export",
            json={**request, "plan_id": preview.json()["planId"]},
            headers=headers,
        )

    assert preview.status_code == 200
    assert stale_export.status_code == 409
    assert "review decisions changed" in stale_export.json()["detail"]
    assert not (root / ".blot" / "outputs").exists()


def test_confirmed_group_decisions_propagate_without_overriding_a_pinned_exclusion(tmp_path):
    root = tmp_path / "project"
    source = tmp_path / "brief.txt"
    root.mkdir()
    source.write_text("Alex Tan met Alex Tann.", encoding="utf-8")
    service = ProjectService(MemoryKeyStore())
    service.create(root, "Project")
    imported = service.import_documents(root, [source])[0]
    analysis = service.analyze_document_candidates(root, imported.document_id, ["Alex Tan"])
    first = next(item for item in analysis["candidates"] if item["term"] == "Alex Tan")
    second = next(item for item in analysis["candidates"] if item["term"] == "Alex Tann")
    group = service.update_candidate_groups(
        root,
        imported.document_id,
        "add",
        [first["id"], second["id"]],
    )[0]

    service.set_candidate_decision(root, imported.document_id, second["id"], "excluded")
    service.set_candidate_decision(root, imported.document_id, first["id"], "included")
    after_include = service.analyze_document_candidates(root, imported.document_id)
    included_first = next(item for item in after_include["candidates"] if item["id"] == first["id"])
    excluded_peer = next(item for item in after_include["candidates"] if item["id"] == second["id"])
    service.set_candidate_decision(root, imported.document_id, first["id"], "excluded")
    after_exclude = service.analyze_document_candidates(root, imported.document_id)

    assert group["candidateIds"] == [first["id"], second["id"]]
    assert included_first["decision"] == "included"
    assert excluded_peer["decision"] == "excluded"
    assert excluded_peer["pinned"] is True
    assert all(item["decision"] == "excluded" for item in after_exclude["candidates"] if item["id"] in group["candidateIds"])


@pytest.mark.anyio
async def test_native_folder_picker_is_token_protected_and_returns_only_user_selection(tmp_path, monkeypatch):
    selected = tmp_path / "local project"
    monkeypatch.setattr(main_module, "pick_project_directory", lambda: selected)
    app, client_context = local_client(tmp_path)
    async with client_context as client:
        denied = await client.get("/api/dialogs/project-folder")
        accepted = await client.get(
            "/api/dialogs/project-folder",
            headers={"X-Local-App-Token": app.state.local_token},
        )

    assert denied.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json() == {"cancelled": False, "directory": str(selected)}


@pytest.mark.anyio
async def test_native_document_picker_is_token_protected_and_returns_selected_files(tmp_path, monkeypatch):
    selected = [tmp_path / "notes.md", tmp_path / "plan.docx"]
    monkeypatch.setattr(main_module, "pick_document_files", lambda: selected)
    app, client_context = local_client(tmp_path)
    async with client_context as client:
        denied = await client.get("/api/dialogs/document-files")
        accepted = await client.get(
            "/api/dialogs/document-files",
            headers={"X-Local-App-Token": app.state.local_token},
        )

    assert denied.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json() == {"cancelled": False, "files": [str(path) for path in selected]}


@pytest.mark.anyio
async def test_native_backup_picker_is_token_protected(tmp_path, monkeypatch):
    selected = tmp_path / "project.blotbackup"
    monkeypatch.setattr(main_module, "pick_backup_file", lambda: selected)
    app, client_context = local_client(tmp_path)
    async with client_context as client:
        denied = await client.get("/api/dialogs/backup-file")
        accepted = await client.get(
            "/api/dialogs/backup-file",
            headers={"X-Local-App-Token": app.state.local_token},
        )

    assert denied.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json() == {"cancelled": False, "path": str(selected)}


@pytest.mark.parametrize(
    ("platform", "output"),
    [
        ("darwin", "/tmp/notes.md\n/tmp/brief.docx\n"),
        ("win32", "C:\\Users\\Example\\notes.md\r\nC:\\Users\\Example\\brief.docx\r\n"),
    ],
)
def test_native_document_picker_builds_multi_select_dialog(platform, output, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(folder_picker.sys, "platform", platform)
    monkeypatch.setattr(folder_picker.subprocess, "run", fake_run)

    selected = folder_picker.pick_document_files()

    assert [str(path) for path in selected] == output.splitlines()
    command, options = calls[0]
    assert options["timeout"] == 180
    if platform == "win32":
        assert "-STA" in command
        assert "Multiselect = $true" in command[-1]
        assert ";" in command[-1]
    else:
        assert "multiple selections allowed" in " ".join(command)
