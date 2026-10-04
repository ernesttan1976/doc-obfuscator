from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .candidate_engine import (
    CandidateBlock,
    CandidateError,
    analyze_candidates,
    blocks_for_document,
    decide_candidate,
    merge_proposals,
)
from .document_adapters import (
    PLACEHOLDER_LIKE_TEXT,
    count_supported_occurrences,
    parse_document,
    serialize_with_replacements,
)
from .key_store import KeyStoreUnavailable, ProjectKeyStore
from .local_crypto import atomic_write_private, decrypt_state, encrypt_state
from .model_manager import ModelManagerError
from .portable_backup import (
    PortableBackupError,
    build_archive,
    decrypt_backup,
    encrypt_backup,
    read_archive,
)

MANIFEST_NAME = "project.json"
DATABASE_NAME = "project.sqlite3"
STATE_NAME = "private-state.enc"
STATE_VERSION = 1
MAX_DOCUMENT_BYTES = 100 * 1024 * 1024
MAX_PENDING_RESTORE_PLANS = 5
SUPPORTED_DOCUMENT_EXTENSIONS = {".docx", ".pptx", ".txt", ".md", ".csv", ".xlsx"}
TOKEN_LIKE_TEXT = re.compile(r"\[\[T_[^\]\r\n]{1,128}\]\]", re.IGNORECASE)
VALID_PLACEHOLDER = re.compile(r"\[\[T_[A-Za-z0-9_-]{3,}\]\]", re.IGNORECASE)
MAX_RESTORE_REPORT_TOKENS = 500


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
    versions: tuple[dict[str, str], ...] = ()

    def to_public_dict(self) -> dict[str, str]:
        return {
            "id": self.document_id,
            "name": self.name,
            "type": self.extension.lstrip(".").upper(),
            "versionId": self.version_id,
            "status": "original",
            "versions": [dict(version) for version in self.versions],
        }


class ProjectService:
    def __init__(
        self,
        key_store: ProjectKeyStore,
        entity_extractor: Callable[[tuple[CandidateBlock, ...]], tuple[list[dict[str, object]], bool]] | None = None,
        contextual_proposer: Callable[
            [tuple[CandidateBlock, ...], list[dict[str, object]]], list[dict[str, object]]
        ] | None = None,
    ) -> None:
        self.key_store = key_store
        self.entity_extractor = entity_extractor
        self.contextual_proposer = contextual_proposer
        self._export_plans: dict[str, dict[str, Any]] = {}
        self._export_lock = threading.RLock()
        self._restore_plans: dict[str, dict[str, Any]] = {}
        self._restore_lock = threading.RLock()

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
            summaries = []
            for document_id, name, extension, original_version_id in rows:
                versions = connection.execute(
                    "SELECT id, kind, status FROM versions WHERE document_id = ? ORDER BY created_at, id",
                    (document_id,),
                ).fetchall()
                public_versions = tuple(
                    {
                        "id": str(version_id),
                        "kind": str(kind),
                        "status": str(status_value),
                        "name": self._version_name(str(name), str(kind), str(version_id), str(extension)),
                        "type": str(extension).lstrip(".").upper(),
                    }
                    for version_id, kind, status_value in versions
                )
                summaries.append(
                    DocumentSummary(str(document_id), str(name), str(extension), str(original_version_id), public_versions)
                )
        except sqlite3.Error as exc:
            raise ProjectError("Project document metadata could not be read.") from exc
        finally:
            connection.close()
        return summaries

    def preview_document(
        self,
        directory: str | Path,
        document_id: str,
        version_id: str | None = None,
    ) -> dict[str, object]:
        root = self._validate_directory(directory)
        parsed, resolved_version_id, _, _, _, _, _ = self._read_document_version(root, document_id, version_id)
        preview = parsed.to_public_dict()
        report = self.load_private_state(root).get("restoreReports", {}).get(resolved_version_id)
        if report:
            preview["restoreReport"] = report
        return preview

    def preview_obfuscation(
        self,
        directory: str | Path,
        document_id: str,
        level: int,
    ) -> dict[str, object]:
        if not 1 <= level <= 10:
            raise ProjectError("Priority cutoff must be between 1 and 10.")
        root = self._validate_directory(directory)
        parsed, original_version_id, original_bytes, _, _, _, display_name = self._read_document_version(
            root, document_id, None
        )
        state = self.load_private_state(root)
        nodes, groups, edges = self._scoped_graph(state, document_id, original_version_id)
        selected = [
            node for node in nodes
            if int(node.get("level", 10)) > level and node.get("decision") != "excluded"
        ]
        if not selected:
            raise ProjectError("No candidates have priority above this cutoff. Lower the cutoff to obfuscate terms.")

        replacements, mapping, matches, output_preview = self._prepare_export_replacements(
            parsed,
            selected,
            groups,
            state,
            document_id,
            original_version_id,
        )
        warnings = self._export_warnings(parsed, output_preview)
        output_preview_public = output_preview.to_public_dict()
        graph_fingerprint = self._export_fingerprint(
            original_bytes,
            document_id,
            original_version_id,
            level,
            nodes,
            groups,
            edges,
        )
        plan_id = secrets.token_urlsafe(24)
        output_version_id = str(uuid.uuid4())
        self._prune_export_plans()
        self._export_plans[plan_id] = {
            "createdAt": time.monotonic(),
            "directory": str(root),
            "documentId": document_id,
            "sourceVersionId": original_version_id,
            "outputVersionId": output_version_id,
            "level": level,
            "fingerprint": graph_fingerprint,
            "replacements": replacements,
            "mapping": mapping,
            "warnings": warnings,
            "requiresAcknowledgement": bool(warnings or parsed.unsupported_parts),
            "matches": matches,
        }
        return {
            "planId": plan_id,
            "documentId": document_id,
            "sourceVersionId": original_version_id,
            "level": level,
            "format": parsed.format,
            "outputName": self._version_name(
                display_name, "obfuscated", output_version_id, f".{parsed.format.lower()}"
            ),
            "preview": output_preview_public,
            "matches": matches,
            "matchCount": sum(int(match["occurrenceCount"]) for match in matches),
            "warnings": warnings,
            "unsupportedPartCount": len(parsed.unsupported_parts),
            "requiresAcknowledgement": bool(warnings or parsed.unsupported_parts),
            "preexistingPlaceholderCount": parsed.preexisting_placeholder_count,
        }

    def _prepare_export_replacements(
        self,
        parsed,
        selected: list[dict[str, Any]],
        groups: list[dict[str, Any]],
        state: dict[str, Any],
        document_id: str,
        version_id: str,
    ) -> tuple[dict[str, str], dict[str, dict[str, str]], list[dict[str, Any]], Any]:
        candidate_groups = {
            str(candidate_id): str(group["id"])
            for group in groups
            for candidate_id in group.get("candidateIds", [])
        }
        existing_tokens = {
            match.group(0).casefold()
            for match in PLACEHOLDER_LIKE_TEXT.finditer(parsed.text or "")
        }
        existing_tokens.update(str(token).casefold() for token in state.get("mapping", {}))
        replacements: dict[str, str] = {}
        mapping: dict[str, dict[str, str]] = {}
        matches = []
        used_tokens: set[str] = set()
        for node in sorted(selected, key=lambda item: (str(item.get("term", "")).casefold(), str(item.get("id", "")))):
            term = node.get("term")
            candidate_id = str(node.get("id", ""))
            if not isinstance(term, str) or not term.strip() or len(term) > 256:
                continue
            token = self._new_placeholder(existing_tokens | used_tokens)
            used_tokens.add(token.casefold())
            replacements[term] = token
            mapping[token] = {
                "term": term,
                "documentId": document_id,
                "sourceVersionId": version_id,
                "candidateId": candidate_id,
                "groupId": candidate_groups.get(candidate_id, ""),
            }
            matches.append(
                {
                    "candidateId": candidate_id,
                    "term": term,
                    "token": token,
                    "occurrenceCount": int(node.get("occurrenceCount", 0)),
                    "decision": str(node.get("decision", "suggested")),
                    "groupId": candidate_groups.get(candidate_id),
                }
            )
        if not replacements:
            raise ProjectError("No valid candidate terms are selected for this version.")

        output_bytes = serialize_with_replacements(parsed, replacements)
        output_preview = parse_document(output_bytes, f".{parsed.format.lower()}")
        matches = self._retain_applied_matches(parsed, output_preview, replacements, mapping, matches)
        if len(matches) != len(used_tokens):
            output_bytes = serialize_with_replacements(parsed, replacements)
            output_preview = parse_document(output_bytes, f".{parsed.format.lower()}")
        return replacements, mapping, matches, output_preview

    @staticmethod
    def _retain_applied_matches(parsed, output_preview, replacements, mapping, matches):
        retained = []
        for match in matches:
            count = count_supported_occurrences(output_preview, str(match["token"]))
            if count:
                match["occurrenceCount"] = count
                retained.append(match)
            else:
                replacements.pop(str(match["term"]), None)
                mapping.pop(str(match["token"]), None)
        if not retained:
            raise ProjectError("Selected candidates do not match any supported editable text in this version.")
        return retained

    @staticmethod
    def _export_warnings(parsed, output_preview) -> list[str]:
        warnings = list(parsed.warnings)
        if parsed.unsupported_parts:
            warnings.append(
                f"{len(parsed.unsupported_parts)} unsupported or unhandled package parts may contain unprocessed text."
            )
        if parsed.preexisting_placeholder_count:
            warnings.append(
                f"{parsed.preexisting_placeholder_count} pre-existing placeholder-like strings were found; they are left unchanged."
            )
        preview = output_preview.to_public_dict()
        if preview.get("truncated") or preview.get("previewSectionsTruncated"):
            warnings.append(
                "The output preview is bounded or truncated; inspect the saved version before sending it externally."
            )
        return warnings
    def export_obfuscation(
        self,
        directory: str | Path,
        document_id: str,
        plan_id: str,
        acknowledge_warnings: bool,
    ) -> dict[str, object]:
        with self._export_lock:
            return self._commit_obfuscation_export(
                directory,
                document_id,
                plan_id,
                acknowledge_warnings,
            )

    def _commit_obfuscation_export(
        self,
        directory: str | Path,
        document_id: str,
        plan_id: str,
        acknowledge_warnings: bool,
    ) -> dict[str, object]:
        root = self._validate_directory(directory)
        plan = self._export_plans.get(plan_id)
        if (
            plan is None
            or plan.get("directory") != str(root)
            or plan.get("documentId") != document_id
            or time.monotonic() - float(plan.get("createdAt", 0)) > 900
        ):
            self._export_plans.pop(plan_id, None)
            raise ProjectError("This export preview expired. Review the current document and create a new preview.")
        if plan["requiresAcknowledgement"] and not acknowledge_warnings:
            raise ProjectError("Review and acknowledge the coverage warnings before exporting this copy.")

        parsed, source_version_id, original_bytes, _, _, _, display_name = self._read_document_version(
            root, document_id, None
        )
        state = self.load_private_state(root)
        nodes, groups, edges = self._scoped_graph(state, document_id, source_version_id)
        fingerprint = self._export_fingerprint(
            original_bytes,
            document_id,
            source_version_id,
            int(plan["level"]),
            nodes,
            groups,
            edges,
        )
        if source_version_id != plan["sourceVersionId"] or fingerprint != plan["fingerprint"]:
            self._export_plans.pop(plan_id, None)
            raise ProjectError("The source version or review decisions changed. Create a fresh export preview.")

        stored_tokens = {str(token).casefold() for token in state.get("mapping", {})}
        if stored_tokens.intersection(str(token).casefold() for token in plan["mapping"]):
            self._export_plans.pop(plan_id, None)
            raise ProjectError("A project placeholder collision was detected. Create a fresh export preview.")

        output_bytes = serialize_with_replacements(parsed, plan["replacements"])
        if output_bytes == original_bytes:
            raise ProjectError("No supported text changed; no obfuscated version was saved.")
        output_version_id = str(plan["outputVersionId"])
        extension = f".{parsed.format.lower()}"
        relative_path = (Path("outputs") / document_id / f"{output_version_id}{extension}").as_posix()
        output_path = root / ".blot" / relative_path
        if output_path.exists():
            raise ProjectError("The obfuscated version identifier already exists; create a fresh preview.")
        atomic_write_private(output_path, output_bytes)
        connection = sqlite3.connect(root / ".blot" / DATABASE_NAME)
        try:
            with connection:
                connection.execute(
                    "INSERT INTO versions(id, document_id, parent_version_id, kind, relative_path, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        output_version_id,
                        document_id,
                        source_version_id,
                        "obfuscated",
                        relative_path,
                        "ready",
                        datetime.now(UTC).isoformat(),
                    ),
                )
            mapping = state.setdefault("mapping", {})
            mapping.update(plan["mapping"])
            self.save_private_state(root, state)
        except Exception as exc:
            with suppress(sqlite3.Error):
                connection.execute("DELETE FROM versions WHERE id = ?", (output_version_id,))
                connection.commit()
            output_path.unlink(missing_ok=True)
            raise ProjectError("The obfuscated copy could not be committed as a new project version.") from exc
        finally:
            connection.close()
        self._export_plans.pop(plan_id, None)
        name = self._version_name(display_name, "obfuscated", output_version_id, extension)
        return {
            "id": output_version_id,
            "documentId": document_id,
            "parentVersionId": source_version_id,
            "kind": "obfuscated",
            "status": "ready",
            "name": name,
            "type": parsed.format,
        }

    def document_version_download(
        self,
        directory: str | Path,
        document_id: str,
        version_id: str,
    ) -> tuple[Path, str]:
        root = self._validate_directory(directory)
        _, _, _, path, kind, extension, display_name = self._read_document_version(
            root, document_id, version_id
        )
        if kind not in {"obfuscated", "restored"}:
            raise ProjectError("Only an approved obfuscated or restored project version can be downloaded.")
        version_name = self._version_name(display_name, kind, version_id, extension)
        return path, version_name

    def preview_restoration(
        self,
        directory: str | Path,
        document_id: str,
        source_version_id: str,
        returned_path: str | Path,
    ) -> dict[str, object]:
        root = self._validate_directory(directory)
        self.open(root)
        connection = sqlite3.connect(root / ".blot" / DATABASE_NAME)
        try:
            version = connection.execute(
                "SELECT v.parent_version_id, v.kind, d.extension "
                "FROM versions AS v JOIN documents AS d ON d.id = v.document_id "
                "WHERE v.id = ? AND v.document_id = ?",
                (source_version_id, document_id),
            ).fetchone()
        except sqlite3.Error as exc:
            raise ProjectError("The selected source version could not be read.") from exc
        finally:
            connection.close()
        if version is None or version[1] != "obfuscated":
            raise ProjectError("Choose an obfuscated version from this project to restore against.")

        original_version_id, _, extension = version
        returned_file = Path(returned_path).expanduser()
        if (
            str(extension).lower() not in {".docx", ".pptx", ".xlsx"}
            or not returned_file.is_absolute()
            or returned_file.suffix.lower() != str(extension).lower()
        ):
            raise ProjectError("Choose a returned DOCX, PPTX, or XLSX matching the selected obfuscated version.")
        try:
            returned_file = returned_file.resolve(strict=True)
            if not returned_file.is_file() or returned_file.stat().st_size > MAX_DOCUMENT_BYTES:
                raise ProjectError("The returned document is missing or exceeds the 100 MB processing limit.")
            source_bytes = returned_file.read_bytes()
        except (OSError, RuntimeError) as exc:
            raise ProjectError("The returned document could not be read.") from exc

        parsed = parse_document(source_bytes, str(extension))
        state = self.load_private_state(root)
        eligible_mapping = self._restoration_mapping(state, document_id, str(original_version_id))
        known_tokens_casefolded = {token.casefold() for token in eligible_mapping}
        exact_counts: dict[str, int] = {}
        unresolved_counts: dict[str, dict[str, object]] = {}
        unresolved_total = altered_total = unknown_or_foreign_total = 0
        unresolved_truncated = False
        for block in blocks_for_document(parsed):
            for match in TOKEN_LIKE_TEXT.finditer(block.text):
                token = match.group(0)
                if token in eligible_mapping:
                    exact_counts[token] = exact_counts.get(token, 0) + 1
                else:
                    category = (
                        "altered"
                        if token.casefold() in known_tokens_casefolded or not VALID_PLACEHOLDER.fullmatch(token)
                        else "unknownOrForeign"
                    )
                    unresolved_total += 1
                    if category == "altered":
                        altered_total += 1
                    else:
                        unknown_or_foreign_total += 1
                    if token in unresolved_counts:
                        entry = unresolved_counts[token]
                        entry["count"] = int(entry["count"]) + 1
                    elif len(unresolved_counts) < MAX_RESTORE_REPORT_TOKENS:
                        unresolved_counts[token] = {"token": token, "count": 1, "status": category}
                    else:
                        unresolved_truncated = True

        replacements = {token: eligible_mapping[token] for token in exact_counts}
        output_bytes = serialize_with_replacements(parsed, replacements, case_sensitive=True)
        output = parse_document(output_bytes, str(extension))
        report = {
            "restoredCount": sum(exact_counts.values()),
            "unresolvedTokens": list(unresolved_counts.values()),
            "unresolvedCount": unresolved_total,
            "alteredCount": altered_total,
            "unknownOrForeignCount": unknown_or_foreign_total,
            "unresolvedTokensTruncated": unresolved_truncated,
        }
        plan_id = secrets.token_urlsafe(24)
        self._prune_restore_plans()
        while len(self._restore_plans) >= MAX_PENDING_RESTORE_PLANS:
            oldest = min(
                self._restore_plans,
                key=lambda key: float(self._restore_plans[key].get("createdAt", 0)),
            )
            self._restore_plans.pop(oldest, None)
        self._restore_plans[plan_id] = {
            "createdAt": time.monotonic(),
            "directory": str(root),
            "documentId": document_id,
            "sourceVersionId": source_version_id,
            "mappingSourceVersionId": str(original_version_id),
            "mappingFingerprint": self._restoration_mapping_fingerprint(eligible_mapping),
            "returnedPath": str(returned_file),
            "sourceHash": hashlib.sha256(source_bytes).hexdigest(),
            "outputBytes": output_bytes,
            "report": report,
        }
        return {
            "planId": plan_id,
            "documentId": document_id,
            "sourceVersionId": source_version_id,
            "format": parsed.format,
            "preview": output.to_public_dict(),
            "report": report,
            "outputName": self._version_name(returned_file.name, "restored", plan_id, str(extension)),
        }

    def commit_restoration(self, directory: str | Path, plan_id: str) -> dict[str, object]:
        with self._restore_lock:
            root = self._validate_directory(directory)
            plan = self._restore_plans.get(plan_id)
            if (
                plan is None
                or plan.get("directory") != str(root)
                or time.monotonic() - float(plan.get("createdAt", 0)) > 900
            ):
                self._restore_plans.pop(plan_id, None)
                raise ProjectError("This restoration preview expired. Preview the returned document again.")
            try:
                current_bytes = Path(str(plan["returnedPath"])).read_bytes()
            except OSError as exc:
                raise ProjectError("The returned document is no longer available.") from exc
            if hashlib.sha256(current_bytes).hexdigest() != plan["sourceHash"]:
                self._restore_plans.pop(plan_id, None)
                raise ProjectError("The returned document changed after preview. Review it again before restoring.")

            document_id = str(plan["documentId"])
            source_version_id = str(plan["sourceVersionId"])
            state = self.load_private_state(root)
            current_mapping = self._restoration_mapping(
                state,
                document_id,
                str(plan["mappingSourceVersionId"]),
            )
            if self._restoration_mapping_fingerprint(current_mapping) != plan["mappingFingerprint"]:
                self._restore_plans.pop(plan_id, None)
                raise ProjectError("The project replacement map changed. Preview the returned document again.")
            restore_reports = state.setdefault("restoreReports", {})
            output_version_id = str(uuid.uuid4())
            extension = Path(str(plan["returnedPath"])).suffix.lower()
            relative_path = (Path("outputs") / document_id / f"{output_version_id}{extension}").as_posix()
            output_path = root / ".blot" / relative_path
            atomic_write_private(output_path, bytes(plan["outputBytes"]))
            restore_reports[output_version_id] = plan["report"]
            connection = sqlite3.connect(root / ".blot" / DATABASE_NAME)
            try:
                with connection:
                    connection.execute(
                        "INSERT INTO versions(id, document_id, parent_version_id, kind, relative_path, status, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            output_version_id,
                            document_id,
                            source_version_id,
                            "restored",
                            relative_path,
                            "ready",
                            datetime.now(UTC).isoformat(),
                        ),
                    )
                self.save_private_state(root, state)
            except Exception as exc:
                with suppress(sqlite3.Error):
                    connection.execute("DELETE FROM versions WHERE id = ?", (output_version_id,))
                    connection.commit()
                output_path.unlink(missing_ok=True)
                restore_reports.pop(output_version_id, None)
                raise ProjectError("The restored copy could not be committed as a new project version.") from exc
            finally:
                connection.close()
            self._restore_plans.pop(plan_id, None)
            return {
                "id": output_version_id,
                "documentId": document_id,
                "parentVersionId": source_version_id,
                "kind": "restored",
                "status": "ready",
                "name": self._version_name(Path(str(plan["returnedPath"])).name, "restored", output_version_id, extension),
                "type": extension.lstrip(".").upper(),
                "restoreReport": plan["report"],
            }

    def _prune_restore_plans(self) -> None:
        cutoff = time.monotonic() - 900
        for key, plan in tuple(self._restore_plans.items()):
            if float(plan.get("createdAt", 0)) < cutoff:
                self._restore_plans.pop(key, None)

    def clear_pending_operations(self) -> None:
        with self._export_lock:
            self._export_plans.clear()
        with self._restore_lock:
            self._restore_plans.clear()

    @staticmethod
    def _restoration_mapping(
        state: dict[str, Any], document_id: str, source_version_id: str
    ) -> dict[str, str]:
        return {
            str(token): str(value["term"])
            for token, value in state.get("mapping", {}).items()
            if isinstance(value, dict)
            and value.get("documentId") == document_id
            and value.get("sourceVersionId") == source_version_id
            and isinstance(value.get("term"), str)
        }

    @staticmethod
    def _restoration_mapping_fingerprint(mapping: dict[str, str]) -> str:
        return hashlib.sha256(
            json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def create_portable_backup(self, directory: str | Path, passphrase: str) -> bytes:
        root = self._validate_directory(directory)
        project = self.open(root)
        private_dir = root / ".blot"
        connection = sqlite3.connect(private_dir / DATABASE_NAME)
        try:
            versions = connection.execute(
                "SELECT id, document_id, kind, relative_path FROM versions ORDER BY created_at, id"
            ).fetchall()
        except sqlite3.Error as exc:
            raise ProjectError("Project version metadata could not be read for backup.") from exc
        finally:
            connection.close()

        entries: dict[str, bytes] = {
            "backup.json": json.dumps(
                {"format": 1, "name": project.name}, separators=(",", ":")
            ).encode("utf-8"),
            "project.sqlite3": (private_dir / DATABASE_NAME).read_bytes(),
            "private-state.json": json.dumps(
                self.load_private_state(root), ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8"),
        }
        for version_id, document_id, kind, relative_path in versions:
            _, _, _, path, actual_kind, _, _ = self._read_document_version(
                root, str(document_id), str(version_id)
            )
            if actual_kind != kind or Path(relative_path).as_posix() != Path(
                "originals" if kind == "original" else "outputs"
            ).joinpath(str(document_id), f"{version_id}{path.suffix}" if kind != "original" else f"original{path.suffix}").as_posix():
                raise ProjectError("The project contains an invalid document version path and cannot be backed up.")
            entries[str(relative_path)] = path.read_bytes()
        try:
            archive = build_archive(entries)
            return encrypt_backup(archive, passphrase)
        except PortableBackupError as exc:
            raise ProjectError(str(exc)) from exc

    def restore_portable_backup(
        self,
        directory: str | Path,
        backup_path: str | Path,
        passphrase: str,
    ) -> ProjectSummary:
        root = self._validate_directory(directory)
        private_dir = root / ".blot"
        if private_dir.exists():
            raise ProjectError("The selected folder already contains a project; choose an empty folder for backup restore.")
        selected_backup = Path(backup_path).expanduser()
        if not selected_backup.is_absolute():
            raise ProjectError("Choose an absolute path to the encrypted backup file.")
        try:
            selected_backup = selected_backup.resolve(strict=True)
            if not selected_backup.is_file() or selected_backup.stat().st_size > 1024 * 1024 * 1024:
                raise ProjectError("The encrypted backup is missing or exceeds the 1 GB limit.")
            archive_bytes = decrypt_backup(selected_backup.read_bytes(), passphrase)
            entries = read_archive(archive_bytes)
        except PortableBackupError as exc:
            raise ProjectError(str(exc)) from exc
        except (OSError, RuntimeError) as exc:
            raise ProjectError("The encrypted backup could not be read.") from exc

        try:
            metadata = json.loads(entries.pop("backup.json"))
            state = json.loads(entries.pop("private-state.json"))
            database = entries.pop(DATABASE_NAME)
            project_name = self._validate_name(metadata["name"])
            if metadata.get("format") != 1 or not isinstance(state, dict):
                raise ValueError("invalid backup metadata")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProjectError("The encrypted backup does not contain a supported project.") from exc

        expected_paths = self._backup_document_paths(database)
        if expected_paths != set(entries):
            raise ProjectError("The backup document inventory does not match its project database.")
        project_id = str(uuid.uuid4())
        try:
            private_dir.mkdir(mode=0o700)
            for relative_path, content in entries.items():
                self._write_backup_entry(private_dir, relative_path, content)
            database_path = private_dir / DATABASE_NAME
            atomic_write_private(database_path, database)
            connection = sqlite3.connect(database_path)
            try:
                check = connection.execute("PRAGMA integrity_check").fetchone()
                if check is None or check[0] != "ok":
                    raise ProjectError("The backup project database failed its integrity check.")
                connection.execute(
                    "UPDATE project_meta SET value = ? WHERE key = 'project_id'",
                    (project_id,),
                )
                connection.commit()
            finally:
                connection.close()
            if os.name != "nt":
                database_path.chmod(0o600)
            key = self.key_store.get_or_create(project_id)
            state_path = private_dir / STATE_NAME
            atomic_write_private(state_path, encrypt_state(project_id, state, key))
            manifest = {"version": STATE_VERSION, "projectId": project_id}
            atomic_write_private(
                private_dir / MANIFEST_NAME,
                json.dumps(manifest, separators=(",", ":")).encode("utf-8"),
            )
            self.open(root)
        except Exception as exc:
            with suppress(KeyStoreUnavailable):
                self.key_store.delete(project_id)
            self._remove_created_workspace(private_dir)
            if isinstance(exc, ProjectError):
                raise
            raise ProjectError("The encrypted backup could not be restored to this folder.") from exc
        return ProjectSummary(project_id, project_name, root)

    @staticmethod
    def _backup_document_paths(database: bytes) -> set[str]:
        with tempfile.NamedTemporaryFile(prefix="blot-backup-", suffix=".sqlite3") as temporary:
            temporary.write(database)
            temporary.flush()
            connection = sqlite3.connect(temporary.name)
            try:
                check = connection.execute("PRAGMA integrity_check").fetchone()
                if check is None or check[0] != "ok":
                    raise ProjectError("The backup project database failed its integrity check.")
                rows = connection.execute(
                    "SELECT v.kind, v.document_id, v.id, v.relative_path, d.extension "
                    "FROM versions AS v JOIN documents AS d ON d.id = v.document_id "
                    "ORDER BY v.document_id, v.id"
                ).fetchall()
            except sqlite3.Error as exc:
                raise ProjectError("The backup project database is invalid.") from exc
            finally:
                connection.close()
        paths: set[str] = set()
        for kind, document_id, version_id, relative_path, extension in rows:
            parts = Path(str(relative_path)).parts
            try:
                if str(uuid.UUID(str(document_id))) != str(document_id):
                    raise ValueError("noncanonical document id")
                if str(uuid.UUID(str(version_id))) != str(version_id):
                    raise ValueError("noncanonical version id")
            except ValueError as exc:
                raise ProjectError("The backup contains an invalid document or version identifier.") from exc
            expected_root = "originals" if kind == "original" else "outputs" if kind in {"obfuscated", "restored"} else ""
            expected_name = f"original{extension}" if kind == "original" else f"{version_id}{extension}"
            if (
                not expected_root
                or len(parts) != 3
                or parts[0] != expected_root
                or parts[1] != str(document_id)
                or parts[2] != expected_name
                or Path(str(relative_path)).suffix != extension
            ):
                raise ProjectError("The backup contains an unsafe project version path.")
            paths.add(Path(*parts).as_posix())
        return paths

    @staticmethod
    def _write_backup_entry(private_dir: Path, relative_path: str, content: bytes) -> None:
        parts = Path(relative_path).parts
        if len(parts) != 3 or parts[0] not in {"originals", "outputs"}:
            raise ProjectError("The backup contains an unsafe document path.")
        directory = private_dir.joinpath(*parts[:2])
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = private_dir.joinpath(*parts)
        atomic_write_private(destination, content)
        if parts[0] == "originals" and os.name != "nt":
            destination.chmod(0o400)

    def _scoped_graph(
        self,
        state: dict[str, Any],
        document_id: str,
        version_id: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        graph = state.get("graph", {})
        nodes = [
            node for node in graph.get("nodes", [])
            if node.get("documentId") == document_id and node.get("versionId") == version_id
        ]
        groups = [
            group for group in graph.get("groups", [])
            if group.get("documentId") == document_id and group.get("versionId") == version_id
        ]
        node_ids = {str(node.get("id", "")) for node in nodes}
        edges = [
            edge for edge in graph.get("edges", [])
            if str(edge.get("sourceId", "")) in node_ids
            and str(edge.get("targetId", "")) in node_ids
        ]
        return nodes, groups, edges

    @staticmethod
    def _export_fingerprint(
        source_bytes: bytes,
        document_id: str,
        version_id: str,
        level: int,
        nodes: list[dict[str, Any]],
        groups: list[dict[str, Any]],
        edges: list[dict[str, Any]],
    ) -> str:
        state = {
            "sourceHash": hashlib.sha256(source_bytes).hexdigest(),
            "documentId": document_id,
            "versionId": version_id,
            "level": level,
            "nodes": sorted(nodes, key=lambda item: str(item.get("id", ""))),
            "groups": sorted(groups, key=lambda item: str(item.get("id", ""))),
            "edges": sorted(edges, key=lambda item: str(item.get("id", ""))),
        }
        return hashlib.sha256(
            json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _prune_export_plans(self) -> None:
        now = time.monotonic()
        self._export_plans = {
            plan_id: plan
            for plan_id, plan in self._export_plans.items()
            if now - float(plan.get("createdAt", 0)) <= 900
        }
        while len(self._export_plans) >= 32:
            oldest = min(self._export_plans, key=lambda plan_id: self._export_plans[plan_id]["createdAt"])
            self._export_plans.pop(oldest, None)

    @staticmethod
    def _new_placeholder(existing: set[str]) -> str:
        for _ in range(20):
            token = f"[[T_{secrets.token_hex(16)}]]"
            if token.casefold() not in existing:
                return token
        raise ProjectError("A collision-resistant placeholder could not be allocated; review the document and retry.")

    @staticmethod
    def _version_name(display_name: str, kind: str, version_id: str, extension: str) -> str:
        if kind == "original":
            return display_name
        base = Path(display_name).stem
        label = "restored" if kind == "restored" else "obfuscated"
        return f"{base}.{label}-{version_id[:8]}{extension}"

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
        blocks = blocks_for_document(parsed)
        ner_warning = None
        try:
            ner_entities, ner_truncated = self.entity_extractor(blocks) if self.entity_extractor else ([], False)
        except ModelManagerError:
            ner_entities, ner_truncated = [], False
            ner_warning = "The local NER model could not run; deterministic candidate discovery continued."
        candidates, proposals = analyze_candidates(
            blocks,
            document_id,
            version_id,
            existing_nodes,
            [*retained_manual_terms, *(manual_terms or [])],
            ner_entities=ner_entities,
        )
        similarity_warning = None
        contextual_proposals = []
        try:
            if self.contextual_proposer:
                contextual_proposals = self.contextual_proposer(blocks, candidates)
        except ModelManagerError:
            similarity_warning = "The local MiniLM model could not run; RapidFuzz proposals remain available."
        proposals = merge_proposals(proposals, contextual_proposals)
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
            "nerTruncated": ner_truncated,
            "nerCandidateCount": sum(node.get("source") == "ner" for node in candidates),
            "nerWarning": ner_warning,
            "similarityProposalCount": sum("minilm" in proposal.get("scores", {}) for proposal in proposals),
            "similarityWarning": similarity_warning,
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
        self._propagate_group_decision(
            scoped_nodes,
            graph.setdefault("groups", []),
            document_id,
            version_id,
            candidate_id,
            decision,
        )
        self.save_private_state(root, state)
        return updated

    @staticmethod
    def _propagate_group_decision(nodes, groups, document_id, version_id, candidate_id, decision) -> None:
        scoped_ids = {str(node.get("id", "")) for node in nodes}
        members = {
            str(member_id)
            for group in groups
            if group.get("documentId") == document_id
            and group.get("versionId") == version_id
            and candidate_id in group.get("candidateIds", [])
            for member_id in group.get("candidateIds", [])
            if str(member_id) != candidate_id and str(member_id) in scoped_ids
        }
        for member_id in members:
            peer = next(node for node in nodes if node.get("id") == member_id)
            if decision == "included" and peer.get("decision") == "excluded" and peer.get("pinned"):
                continue
            decide_candidate(nodes, member_id, decision)

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
        parsed, version_id, _, _, _, _, _ = self._read_document_version(root, document_id, None)
        return parsed, version_id

    def _read_document_version(
        self,
        root: Path,
        document_id: str,
        version_id: str | None,
    ):
        self.open(root)
        private_dir = root / ".blot"
        connection = sqlite3.connect(private_dir / DATABASE_NAME)
        try:
            if version_id is None:
                row = connection.execute(
                    "SELECT d.extension, d.display_name, v.id, v.kind, "
                    "CASE WHEN v.kind = 'original' THEN d.original_path ELSE v.relative_path END "
                    "FROM documents AS d JOIN versions AS v ON v.document_id = d.id "
                    "WHERE d.id = ? AND v.kind = 'original'",
                    (document_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT d.extension, d.display_name, v.id, v.kind, "
                    "CASE WHEN v.kind = 'original' THEN d.original_path ELSE v.relative_path END "
                    "FROM documents AS d JOIN versions AS v ON v.document_id = d.id "
                    "WHERE d.id = ? AND v.id = ?",
                    (document_id, version_id),
                ).fetchone()
        except sqlite3.Error as exc:
            raise ProjectError("Project document metadata could not be read.") from exc
        finally:
            connection.close()
        if row is None:
            raise ProjectError("The selected document version was not found in this project.")

        extension, display_name, stored_version_id, kind, relative_path_value = row
        relative_path = Path(relative_path_value)
        expected_root = "originals" if kind == "original" else "outputs" if kind in {"obfuscated", "restored"} else ""
        expected_name = f"original{extension}" if kind == "original" else f"{stored_version_id}{extension}"
        if (relative_path.is_absolute() or len(relative_path.parts) != 3
                or relative_path.parts[0] != expected_root
                or relative_path.parts[1] != document_id
                or relative_path.parts[2] != expected_name):
            raise ProjectError("The stored project document path is invalid.")
        storage_dir = private_dir / expected_root
        document_dir = storage_dir / document_id
        original_path = private_dir / relative_path
        if (
            private_dir.is_symlink()
            or storage_dir.is_symlink()
            or document_dir.is_symlink()
            or original_path.is_symlink()
        ):
            raise ProjectError("The stored project document path is unsafe.")
        try:
            resolved_root = storage_dir.resolve(strict=True)
            resolved_path = original_path.resolve(strict=True)
            if not resolved_path.is_relative_to(resolved_root) or not resolved_path.is_file():
                raise ProjectError("The stored project document is missing or unsafe.")
            if resolved_path.stat().st_size > MAX_DOCUMENT_BYTES:
                raise ProjectError("The stored project document exceeds the 100 MB processing limit.")
            content = resolved_path.read_bytes()
        except (OSError, RuntimeError) as exc:
            raise ProjectError("The stored project document is missing or cannot be read.") from exc
        return parse_document(content, extension), str(stored_version_id), content, resolved_path, str(kind), str(extension), str(display_name)

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

                original_version = {
                    "id": version_id,
                    "kind": "original",
                    "status": "ready",
                    "name": source.name,
                    "type": extension.lstrip(".").upper(),
                }
                summary = DocumentSummary(document_id, source.name, extension, version_id, (original_version,))
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
