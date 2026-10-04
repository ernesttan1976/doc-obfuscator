from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .key_store import KeyStoreUnavailable, ProjectKeyStore
from .local_crypto import atomic_write_private, decrypt_state, encrypt_state

MANIFEST_NAME = "project.json"
DATABASE_NAME = "project.sqlite3"
STATE_NAME = "private-state.enc"
STATE_VERSION = 1


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
