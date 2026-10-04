from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .candidate_engine import (
    CandidateError,
    analyze_candidates,
    blocks_for_document,
    decide_candidate,
)
from .document_adapters import parse_document
from .key_store import KeyStoreUnavailable, ProjectKeyStore
from .local_crypto import atomic_write_private, decrypt_state, encrypt_state

MANIFEST_NAME = "project.json"
DATABASE_NAME = "project.sqlite3"
STATE_NAME = "private-state.enc"
STATE_VERSION = 1
MAX_DOCUMENT_BYTES = 100 * 1024 * 1024
SUPPORTED_DOCUMENT_EXTENSIONS = {".docx", ".pptx", ".txt", ".md", ".csv", ".xlsx"}


class ProjectError(Exception):
    """Project workspace operation failed with a user-actionable cause."""


@dataclass(frozen=True)
class ProjectSummary:
    project_id: str
    name: str
    directory: Path

    def to_public_dict(self) -> dict[str, str]:
        return {
            "id": self.project_id,
            "name": self.name,
            "status": "ready",
            "graphProtection": "encrypted",
        }


@dataclass(frozen=True)
class DocumentSummary:
    document_id: str
    name: str
    extension: str
    version_id: str

    def to_public_dict(self) -> dict[str, str]:
        return {
            "id": self.document_id,
            "name": self.name,
            "type": self.extension.lstrip(".").upper(),
            "versionId": self.version_id,
            "status": "original",
        }


class ProjectService:
    def __init__(self, key_store: ProjectKeyStore) -> None:
        self.key_store = key_store

    def create(self, directory: str | Path, name: str) -> ProjectSummary:
        root = self._validate_directory(directory)
        clean_name = self._validate_name(name)
        private_dir = root / ".blot"
        manifest_path = private_dir / MANIFEST_NAME
        if private_dir.exists():
            raise ProjectError("This folder already contains a Blot workspace. Open it instead of creating over it.")

        project_id = str(uuid.uuid4())
        private_dir.mkdir(mode=0o700)
        key_created = False
        try:
            key = self.key_store.get_or_create(project_id)
            key_created = True
            self._create_database(private_dir / DATABASE_NAME, project_id)
            state = {
                "name": clean_name,
                "graph": {"nodes": [], "edges": [], "decisions": {}},
                "mapping": {},
            }
            atomic_write_private(private_dir / STATE_NAME, encrypt_state(project_id, state, key))
            manifest = {"version": STATE_VERSION, "projectId": project_id}
            atomic_write_private(
                manifest_path,
                json.dumps(manifest, separators=(",", ":")).encode("utf-8"),
            )
        except Exception:
            if key_created:
                with suppress(KeyStoreUnavailable):
                    self.key_store.delete(project_id)
            self._remove_created_workspace(private_dir)
            raise
        return ProjectSummary(project_id, clean_name, root)

    def open(self, directory: str | Path) -> ProjectSummary:
        root = self._validate_directory(directory)
        private_dir = root / ".blot"
        manifest_path = private_dir / MANIFEST_NAME
        state_path = private_dir / STATE_NAME
        if not manifest_path.is_file() or not state_path.is_file():
            raise ProjectError("No complete Blot project exists in this folder.")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("version") != STATE_VERSION:
                raise ProjectError("This project version is not supported by this Blot build.")
            project_id = str(uuid.UUID(manifest["projectId"]))
            key = self.key_store.get(project_id)
            state = decrypt_state(project_id, state_path.read_bytes(), key)
            name = self._validate_name(state["name"])
        except ProjectError:
            raise
        except KeyStoreUnavailable:
            raise
        except Exception as exc:
            raise ProjectError("The project is incomplete, locked, or its encrypted state is damaged.") from exc
        return ProjectSummary(project_id, name, root)

    def list_documents(self, directory: str | Path) -> list[DocumentSummary]:
        root = self._validate_directory(directory)
        self.open(root)
        connection = sqlite3.connect(root / ".blot" / DATABASE_NAME)
        try:
            rows = connection.execute(
                """
                SELECT d.id, d.display_name, d.extension, v.id
                FROM documents AS d
                JOIN versions AS v ON v.document_id = d.id
                WHERE v.kind = 'original'
                ORDER BY d.created_at, d.id
                """
            ).fetchall()
        except sqlite3.Error as exc:
            raise ProjectError("Project document metadata could not be read.") from exc
        finally:
            connection.close()
        return [DocumentSummary(*row) for row in rows]

    def preview_document(self, directory: str | Path, document_id: str) -> dict[str, object]:
        root = self._validate_directory(directory)
        parsed, _ = self._read_original_document(root, document_id)
        return parsed.to_public_dict()

    def analyze_document_candidates(
        self,
        directory: str | Path,
        document_id: str,
        manual_terms: list[str] | None = None,
    ) -> dict[str, object]:
        root = self._validate_directory(directory)
        parsed, version_id = self._read_original_document(root, document_id)
        state = self.load_private_state(root)
        graph = state.setdefault("graph", {"nodes": [], "edges": [], "decisions": {}})
        nodes = graph.setdefault("nodes", [])
        edges = graph.setdefault("edges", [])
        existing_nodes = [
            node for node in nodes
            if node.get("documentId") == document_id and node.get("versionId") == version_id
        ]
        retained_manual_terms = [
            str(node["term"]) for node in existing_nodes if node.get("source") == "manual"
        ]
        candidates, proposals = analyze_candidates(
            blocks_for_document(parsed),
            document_id,
            version_id,
            existing_nodes,
            [*retained_manual_terms, *(manual_terms or [])],
        )
        old_ids = {node["id"] for node in existing_nodes}
        graph["nodes"] = [node for node in nodes if node.get("id") not in old_ids] + candidates
        graph["edges"] = [
            edge for edge in edges
            if edge.get("sourceId") not in old_ids and edge.get("targetId") not in old_ids
        ] + proposals
        self.save_private_state(root, state)
        groups = graph.setdefault("groups", [])
        return {
            "documentId": document_id,
            "versionId": version_id,
            "candidates": candidates,
            "proposals": proposals,
            "groups": [
                group for group in groups
                if group.get("documentId") == document_id and group.get("versionId") == version_id
            ],
            "candidateLimitReached": len(candidates) >= 1_000,
            "proposalLimitReached": len(proposals) >= 1_000,
        }

    def set_candidate_decision(
        self,
        directory: str | Path,
        document_id: str,
        candidate_id: str,
        decision: str,
    ) -> dict[str, object]:
        root = self._validate_directory(directory)
        version_id = self._original_version_id(root, document_id)
        state = self.load_private_state(root)
        graph = state.setdefault("graph", {"nodes": [], "edges": [], "decisions": {}})
        nodes = graph.setdefault("nodes", [])
        scoped_nodes = [
            node for node in nodes
            if node.get("documentId") == document_id and node.get("versionId") == version_id
        ]
        updated = decide_candidate(scoped_nodes, candidate_id, decision)
        self.save_private_state(root, state)
        return updated

    def update_candidate_groups(
        self,
        directory: str | Path,
        document_id: str,
        operation: str,
        candidate_ids: list[str] | None = None,
        group_id: str | None = None,
        group_ids: list[str] | None = None,
    ) -> list[dict[str, object]]:
        root = self._validate_directory(directory)
        version_id = self._original_version_id(root, document_id)
        state = self.load_private_state(root)
        graph = state.setdefault("graph", {"nodes": [], "edges": [], "decisions": {}})
        groups = graph.setdefault("groups", [])
        scoped_groups = [
            group for group in groups
            if group.get("documentId") == document_id and group.get("versionId") == version_id
        ]
        scoped_nodes = {
            node["id"] for node in graph.setdefault("nodes", [])
            if node.get("documentId") == document_id and node.get("versionId") == version_id
        }
        requested_candidates = list(dict.fromkeys(candidate_ids or []))
        requested_groups = list(dict.fromkeys(group_ids or []))

        if operation in {"add", "split"} and (
            not requested_candidates or any(candidate not in scoped_nodes for candidate in requested_candidates)
        ):
            raise CandidateError("Choose existing candidates from this document version.")
        if operation == "add":
            matching = next((group for group in scoped_groups if group.get("id") == group_id), None)
            if group_id is not None and matching is None:
                raise CandidateError("The selected confirmed group was not found in this document version.")
            memberships = {
                candidate
                for group in scoped_groups if group is not matching
                for candidate in group.get("candidateIds", [])
            }
            if memberships.intersection(requested_candidates):
                raise CandidateError("A candidate already belongs to another confirmed group.")
            if matching is None:
                matching = {
                    "id": str(uuid.uuid4()),
                    "documentId": document_id,
                    "versionId": version_id,
                    "candidateIds": [],
                    "confirmed": True,
                    "source": "manual",
                }
                groups.append(matching)
            matching["candidateIds"] = list(dict.fromkeys(matching["candidateIds"] + requested_candidates))
        elif operation == "remove":
            matching = next((group for group in scoped_groups if group.get("id") == group_id), None)
            if matching is None or not requested_candidates:
                raise CandidateError("Choose an existing group and one or more of its members to remove.")
            if not set(requested_candidates).issubset(matching.get("candidateIds", [])):
                raise CandidateError("Only members of the selected group can be removed.")
            matching["candidateIds"] = [
                candidate for candidate in matching["candidateIds"] if candidate not in requested_candidates
            ]
            if not matching["candidateIds"]:
                groups.remove(matching)
        elif operation == "split":
            matching = next((group for group in scoped_groups if group.get("id") == group_id), None)
            if matching is None or not set(requested_candidates).issubset(matching.get("candidateIds", [])):
                raise CandidateError("Choose members of an existing confirmed group to split.")
            remaining = [candidate for candidate in matching["candidateIds"] if candidate not in requested_candidates]
            if not remaining:
                raise CandidateError("A split must leave at least one member in each group.")
            matching["candidateIds"] = remaining
            groups.append(
                {
                    "id": str(uuid.uuid4()),
                    "documentId": document_id,
                    "versionId": version_id,
                    "candidateIds": requested_candidates,
                    "confirmed": True,
                    "source": "manual",
                }
            )
        elif operation == "merge":
            selected = [group for group in scoped_groups if group.get("id") in requested_groups]
            if len(requested_groups) < 2 or len(selected) != len(requested_groups):
                raise CandidateError("Choose at least two confirmed groups from this document version to merge.")
            merged_members = list(
                dict.fromkeys(candidate for group in selected for candidate in group.get("candidateIds", []))
            )
            groups[:] = [group for group in groups if group not in selected]
            groups.append(
                {
                    "id": str(uuid.uuid4()),
                    "documentId": document_id,
                    "versionId": version_id,
                    "candidateIds": merged_members,
                    "confirmed": True,
                    "source": "manual",
                }
            )
        else:
            raise CandidateError("Group operation must be add, remove, split, or merge.")

        self.save_private_state(root, state)
        return [group for group in groups if group.get("documentId") == document_id and group.get("versionId") == version_id]

    def _original_version_id(self, root: Path, document_id: str) -> str:
        self.open(root)
        connection = sqlite3.connect(root / ".blot" / DATABASE_NAME)
        try:
            row = connection.execute(
                "SELECT id FROM versions WHERE document_id = ? AND kind = 'original'",
                (document_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise ProjectError("Project document metadata could not be read.") from exc
        finally:
            connection.close()
        if row is None:
            raise ProjectError("The selected project document was not found.")
        return str(row[0])

    def _read_original_document(self, root: Path, document_id: str):
        self.open(root)
        private_dir = root / ".blot"
        connection = sqlite3.connect(private_dir / DATABASE_NAME)
        try:
            row = connection.execute(
                "SELECT d.extension, d.original_path, v.id "
                "FROM documents AS d JOIN versions AS v ON v.document_id = d.id "
                "WHERE d.id = ? AND v.kind = 'original'",
                (document_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise ProjectError("Project document metadata could not be read.") from exc
        finally:
            connection.close()
        if row is None:
            raise ProjectError("The selected project document was not found.")

        extension, relative_path_value, version_id = row
        relative_path = Path(relative_path_value)
        if (
            relative_path.is_absolute()
            or len(relative_path.parts) != 3
            or relative_path.parts[0] != "originals"
            or relative_path.parts[1] != document_id
            or relative_path.parts[2] != f"original{extension}"
        ):
            raise ProjectError("The stored project document path is invalid.")
        originals_dir = private_dir / "originals"
        document_dir = originals_dir / document_id
        original_path = private_dir / relative_path
        if (
            private_dir.is_symlink()
            or originals_dir.is_symlink()
            or document_dir.is_symlink()
            or original_path.is_symlink()
        ):
            raise ProjectError("The stored project document path is unsafe.")
        try:
            resolved_originals = originals_dir.resolve(strict=True)
            resolved_path = original_path.resolve(strict=True)
            if not resolved_path.is_relative_to(resolved_originals) or not resolved_path.is_file():
                raise ProjectError("The stored project document is missing or unsafe.")
            if resolved_path.stat().st_size > MAX_DOCUMENT_BYTES:
                raise ProjectError("The stored project document exceeds the 100 MB processing limit.")
            content = resolved_path.read_bytes()
        except (OSError, RuntimeError) as exc:
            raise ProjectError("The stored project document is missing or cannot be read.") from exc
        return parse_document(content, extension), version_id

    def import_documents(self, directory: str | Path, sources: list[str | Path]) -> list[DocumentSummary]:
        root = self._validate_directory(directory)
        self.open(root)
        if not sources:
            raise ProjectError("Choose at least one document to import.")

        prepared: list[tuple[Path, str, int]] = []
        for source_value in sources:
            source = Path(source_value).expanduser()
            if not source.is_absolute():
                raise ProjectError("Selected document paths must be absolute.")
            try:
                source = source.resolve(strict=True)
                metadata = source.stat()
            except (OSError, RuntimeError) as exc:
                raise ProjectError("A selected document is missing or cannot be read.") from exc
            extension = source.suffix.lower()
            if not source.is_file() or extension not in SUPPORTED_DOCUMENT_EXTENSIONS:
                raise ProjectError("Select only DOCX, PPTX, TXT, MD, CSV, or XLSX documents.")
            if metadata.st_size > MAX_DOCUMENT_BYTES:
                raise ProjectError("A selected document exceeds the 100 MB project limit.")
            prepared.append((source, extension, metadata.st_size))

        private_dir = root / ".blot"
        originals_dir = private_dir / "originals"
        try:
            originals_dir.mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            raise ProjectError("The project originals folder could not be created.") from exc
        inserted: list[DocumentSummary] = []
        created_directories: list[Path] = []
        try:
            for source, extension, expected_size in prepared:
                document_id = str(uuid.uuid4())
                version_id = str(uuid.uuid4())
                document_dir = originals_dir / document_id
                document_dir.mkdir(mode=0o700)
                created_directories.append(document_dir)
                relative_path = Path("originals") / document_id / f"original{extension}"
                destination = private_dir / relative_path
                temporary_path: Path | None = None
                try:
                    with source.open("rb") as input_file, tempfile.NamedTemporaryFile(
                        dir=document_dir, prefix=".import-", delete=False
                    ) as output_file:
                        temporary_path = Path(output_file.name)
                        copied_size = 0
                        while chunk := input_file.read(1024 * 1024):
                            copied_size += len(chunk)
                            if copied_size > MAX_DOCUMENT_BYTES:
                                raise ProjectError("A selected document exceeds the 100 MB project limit.")
                            output_file.write(chunk)
                        output_file.flush()
                        os.fsync(output_file.fileno())
                    actual_size = temporary_path.stat().st_size
                    if actual_size != expected_size or copied_size != expected_size:
                        raise ProjectError("A selected document changed during import; no original was saved.")
                    temporary_path.chmod(0o400)
                    os.replace(temporary_path, destination)
                finally:
                    if temporary_path is not None:
                        temporary_path.unlink(missing_ok=True)

                summary = DocumentSummary(document_id, source.name, extension, version_id)
                inserted.append(summary)

            connection = sqlite3.connect(private_dir / DATABASE_NAME)
            try:
                timestamp = datetime.now(UTC).isoformat()
                with connection:
                    for summary in inserted:
                        relative_path = (
                            Path("originals") / summary.document_id / f"original{summary.extension}"
                        ).as_posix()
                        connection.execute(
                            "INSERT INTO documents(id, display_name, extension, status, original_path, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (summary.document_id, summary.name, summary.extension, "original", relative_path, timestamp),
                        )
                        connection.execute(
                            "INSERT INTO versions(id, document_id, parent_version_id, kind, relative_path, status, created_at) "
                            "VALUES (?, ?, NULL, ?, ?, ?, ?)",
                            (summary.version_id, summary.document_id, "original", relative_path, "ready", timestamp),
                        )
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as exc:
            self._remove_import_directories(created_directories)
            raise ProjectError("Could not save the selected document in this project.") from exc
        except Exception:
            self._remove_import_directories(created_directories)
            raise
        return inserted

    def load_private_state(self, directory: str | Path) -> dict[str, Any]:
        root = self._validate_directory(directory)
        private_dir = root / ".blot"
        manifest = json.loads((private_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
        project_id = str(uuid.UUID(manifest["projectId"]))
        key = self.key_store.get(project_id)
        return decrypt_state(project_id, (private_dir / STATE_NAME).read_bytes(), key)

    def save_private_state(self, directory: str | Path, state: dict[str, Any]) -> None:
        root = self._validate_directory(directory)
        private_dir = root / ".blot"
        manifest = json.loads((private_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
        project_id = str(uuid.UUID(manifest["projectId"]))
        key = self.key_store.get(project_id)
        atomic_write_private(private_dir / STATE_NAME, encrypt_state(project_id, state, key))

    @staticmethod
    def _validate_directory(directory: str | Path) -> Path:
        candidate = Path(directory).expanduser()
        if not candidate.is_absolute():
            raise ProjectError("Choose an absolute local project folder path.")
        try:
            root = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ProjectError("The selected project folder does not exist or cannot be opened.") from exc
        if not root.is_dir() or not os.access(root, os.R_OK | os.W_OK | os.X_OK):
            raise ProjectError("The selected project folder must be a readable and writable directory.")
        return root

    @staticmethod
    def _validate_name(name: str) -> str:
        clean = name.strip()
        if not clean or len(clean) > 100 or any(ord(char) < 32 for char in clean):
            raise ProjectError("Project name must contain 1–100 printable characters.")
        return clean

    @staticmethod
    def _create_database(path: Path, project_id: str) -> None:
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(
                """
                CREATE TABLE project_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE documents (
                    id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    status TEXT NOT NULL,
                    original_path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE versions (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES documents(id),
                    parent_version_id TEXT REFERENCES versions(id),
                    kind TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE jobs (
                    id TEXT PRIMARY KEY,
                    document_id TEXT REFERENCES documents(id),
                    phase TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX versions_by_document ON versions(document_id, created_at);
                CREATE INDEX jobs_by_status ON jobs(status, updated_at);
                """
            )
            connection.execute(
                "INSERT INTO project_meta(key, value) VALUES (?, ?)",
                ("project_id", project_id),
            )
            connection.execute(
                "INSERT INTO project_meta(key, value) VALUES (?, ?)",
                ("schema_version", "1"),
            )
            connection.commit()
        finally:
            connection.close()
        if os.name != "nt":
            path.chmod(0o600)

    @staticmethod
    def _remove_created_workspace(private_dir: Path) -> None:
        if not private_dir.exists():
            return
        for child in private_dir.iterdir():
            if child.is_file() or child.is_symlink():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                for nested in child.iterdir():
                    if nested.is_file() or nested.is_symlink():
                        nested.unlink(missing_ok=True)
                child.rmdir()
        private_dir.rmdir()

    @staticmethod
    def _remove_import_directories(directories: list[Path]) -> None:
        for directory in directories:
            if directory.exists():
                shutil.rmtree(directory)
