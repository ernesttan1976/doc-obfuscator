from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from .pinned_download import (
    DownloadCancelled,
    partial_download_size,
    resumable_download,
    urlopen,
)

MODEL_ID = "knowledgator/gliner-multitask-large-v0.5"
MODEL_REVISION = "7a95e168036db9ec6f914c0cc6b218edbd87f310"
MODEL_LICENSE = "Apache-2.0"
MODEL_DESCRIPTION = "High-recall English entity suggestions"
MODEL_THRESHOLD = 0.45
MAX_NER_TEXT_CHARS = 250_000
NER_CHUNK_CHARS = 1_200
NER_LABELS = (
    "person",
    "organization",
    "company",
    "location",
    "address",
    "date",
    "identifier",
    "account number",
)


@dataclass(frozen=True)
class ModelFile:
    name: str
    size: int
    sha256: str | None = None
    git_sha1: str | None = None


# Git-blob IDs pin the small text/tokenizer files; the large safetensors and
# SentencePiece artifacts use their upstream LFS SHA-256 digests.
MODEL_FILES = (
    ModelFile("README.md", 14_471, git_sha1="29f592e15f822c96b2e2a6485521b426863f0c75"),
    ModelFile("added_tokens.json", 86, git_sha1="5c7599504ef0461de3e23af09cfaffc4ba589b27"),
    ModelFile("gliner_config.json", 3_756, git_sha1="f82603471286240dcb0578a81575f8af0f962e8d"),
    ModelFile("model.fp16.safetensors", 880_500_126, sha256="df1f05739d7c34dfdc886e72a153c2373553fcaa70a0b825ae05bbc0dc67a6e7"),
    ModelFile("special_tokens_map.json", 286, git_sha1="2c9cb07c8fdeeb5ac3ceafb170592e990b204dcd"),
    ModelFile("spm.model", 2_464_616, sha256="c679fbf93643d19aab7ee10c0b99e460bdbc02fedf34b92b05af343b4af586fd"),
    ModelFile("tokenizer.json", 8_657_176, git_sha1="487ffea6459e62f3a1696210b124fbb913137257"),
    ModelFile("tokenizer_config.json", 1_837, git_sha1="cdfc495421b6e30183356c0ba2c2fa72487f40da"),
)
MODEL_SIZE_BYTES = sum(model_file.size for model_file in MODEL_FILES)
MODEL_TOTAL_SHA256 = hashlib.sha256(
    "\n".join(f"{model_file.name}:{model_file.size}:{model_file.sha256 or model_file.git_sha1}" for model_file in MODEL_FILES).encode()
).hexdigest()


class ModelManagerError(Exception):
    """Model acquisition or local inference is unavailable."""


def _hashes_for_file(path: Path, size: int) -> tuple[str, str]:
    sha256 = hashlib.sha256()
    git_sha1 = hashlib.sha1(f"blob {size}\0".encode())
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            sha256.update(chunk)
            git_sha1.update(chunk)
    return sha256.hexdigest(), git_sha1.hexdigest()


class LocalModelManager:
    def __init__(self, models_directory: Path | str | None = None) -> None:
        self.models_directory = Path(models_directory or (Path.home() / ".blot" / "models")).expanduser()
        self.model_directory = self.models_directory / "ner" / "knowledgator-gliner-multitask-large-v05"
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self._job: dict[str, object] = {"status": "idle", "downloadedBytes": 0}
        self._model = None
        self._model_lock = threading.Lock()
        self._verified = False
        self._verification_lock = threading.Lock()

    def status(self) -> dict[str, object]:
        with self._lock:
            job = dict(self._job)
        installed = self._installed_manifest_is_valid()
        return {
            **job,
            "modelId": MODEL_ID,
            "description": MODEL_DESCRIPTION,
            "revision": MODEL_REVISION,
            "license": MODEL_LICENSE,
            "sizeBytes": MODEL_SIZE_BYTES,
            "installed": installed,
            "runtimeAvailable": importlib.util.find_spec("gliner") is not None,
        }

    def start_download(self, confirmed: bool) -> dict[str, object]:
        if confirmed is not True:
            raise ModelManagerError("Confirm the model download before starting it.")
        with self._lock:
            if self._job.get("status") == "downloading":
                raise ModelManagerError("The model download is already running.")
            if self._installed_manifest_is_valid():
                self._job = {"status": "ready", "downloadedBytes": MODEL_SIZE_BYTES}
                return self.status()
            if self.model_directory.exists():
                raise ModelManagerError("An incomplete model directory exists; remove or move it before retrying.")
            job_id = str(uuid.uuid4())
            partial_directory = self.model_directory.parent / f".{self.model_directory.name}.partial"
            self._cancel.clear()
            self._job = {
                "jobId": job_id,
                "status": "downloading",
                "downloadedBytes": partial_download_size(MODEL_FILES, partial_directory),
                "totalBytes": MODEL_SIZE_BYTES,
                "error": None,
            }
            self._worker = threading.Thread(target=self._download_worker, args=(job_id,), daemon=True)
            self._worker.start()
        return self.status()

    def cancel_download(self) -> dict[str, object]:
        with self._lock:
            if self._job.get("status") != "downloading":
                raise ModelManagerError("There is no model download to cancel.")
            self._cancel.set()
        return self.status()

    def _download_worker(self, job_id: str) -> None:
        install_parent = self.model_directory.parent
        temporary_directory = install_parent / f".{self.model_directory.name}.partial"
        try:
            def update_progress(downloaded: int) -> None:
                with self._lock:
                    if self._job.get("jobId") == job_id:
                        self._job["downloadedBytes"] = downloaded

            resumable_download(
                MODEL_FILES,
                temporary_directory,
                lambda artifact: f"https://huggingface.co/{MODEL_ID}/resolve/{MODEL_REVISION}/{artifact.name}",
                self._cancel,
                update_progress,
                opener=urlopen,
            )
            if self._cancel.is_set():
                raise DownloadCancelled
            manifest = {
                "modelId": MODEL_ID,
                "revision": MODEL_REVISION,
                "license": MODEL_LICENSE,
                "files": [
                    {"name": model_file.name, "size": model_file.size, "sha256": model_file.sha256, "gitSha1": model_file.git_sha1}
                    for model_file in MODEL_FILES
                ],
                "artifactSetSha256": MODEL_TOTAL_SHA256,
            }
            (temporary_directory / "blot-model-manifest.json").write_text(
                json.dumps(manifest, separators=(",", ":")), encoding="utf-8"
            )
            os.replace(temporary_directory, self.model_directory)
            with self._lock:
                if self._job.get("jobId") == job_id:
                    self._job = {"jobId": job_id, "status": "ready", "downloadedBytes": MODEL_SIZE_BYTES}
        except DownloadCancelled:
            with self._lock:
                if self._job.get("jobId") == job_id:
                    self._job = {
                        "jobId": job_id,
                        "status": "cancelled",
                        "downloadedBytes": partial_download_size(MODEL_FILES, temporary_directory),
                    }
        except Exception:  # noqa: BLE001 - never expose remote or parser exception details to the UI
            with self._lock:
                if self._job.get("jobId") == job_id:
                    self._job = {
                        "jobId": job_id,
                        "status": "failed",
                        "downloadedBytes": partial_download_size(MODEL_FILES, temporary_directory),
                        "error": "Download failed or an artifact did not match its pinned integrity check.",
                    }

    def _installed_manifest_is_valid(self) -> bool:
        manifest_path = self.model_directory / "blot-model-manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if (
            manifest.get("modelId") != MODEL_ID
            or manifest.get("revision") != MODEL_REVISION
            or manifest.get("artifactSetSha256") != MODEL_TOTAL_SHA256
        ):
            return False
        return all(
            (self.model_directory / model_file.name).is_file()
            and (self.model_directory / model_file.name).stat().st_size == model_file.size
            for model_file in MODEL_FILES
        )

    def extract_entities(self, blocks: tuple[object, ...]) -> tuple[list[dict[str, object]], bool]:
        if not self._installed_manifest_is_valid():
            return [], False
        if not importlib.util.find_spec("gliner"):
            return [], False
        if self._model is None:
            with self._model_lock:
                if self._model is None:
                    try:
                        self._verify_installed_files()
                        from gliner import GLiNER

                        self._model = GLiNER.from_pretrained(
                            str(self.model_directory),
                            variant="fp16",
                            dtype="float32",
                            local_files_only=True,
                            map_location="cpu",
                        )
                    except Exception as exc:
                        raise ModelManagerError("The local NER model could not be loaded offline.") from exc

        predictions: list[dict[str, object]] = []
        remaining = MAX_NER_TEXT_CHARS
        truncated = False
        for block in blocks:
            text = str(getattr(block, "text", ""))
            location = str(getattr(block, "location", "text"))
            if remaining <= 0:
                truncated = True
                break
            bounded_text = text[:remaining]
            if len(bounded_text) < len(text):
                truncated = True
            remaining -= len(bounded_text)
            for chunk, offset in _text_chunks(bounded_text):
                entities = self._model.predict_entities(chunk, list(NER_LABELS), threshold=MODEL_THRESHOLD)
                for entity in entities:
                    start = int(entity.get("start", 0)) + offset
                    end = int(entity.get("end", 0)) + offset
                    if 0 <= start < end <= len(bounded_text):
                        predictions.append({
                            "text": bounded_text[start:end],
                            "location": location,
                            "start": start,
                            "end": end,
                            "label": str(entity.get("label", "entity")).lower(),
                            "score": float(entity.get("score", 0.0)),
                        })
        return predictions, truncated

    def _verify_installed_files(self) -> None:
        with self._verification_lock:
            if self._verified:
                return
            if not self._installed_manifest_is_valid():
                raise ModelManagerError("The local NER model files are incomplete or have an invalid manifest.")
            for model_file in MODEL_FILES:
                sha256, git_sha1 = _hashes_for_file(
                    self.model_directory / model_file.name,
                    model_file.size,
                )
                if model_file.sha256 and sha256 != model_file.sha256:
                    raise ModelManagerError("A local NER model file failed its SHA-256 integrity check.")
                if model_file.git_sha1 and git_sha1 != model_file.git_sha1:
                    raise ModelManagerError("A local NER tokenizer file failed its Git blob integrity check.")
            self._verified = True




def _text_chunks(text: str) -> list[tuple[str, int]]:
    chunks = []
    offset = 0
    while offset < len(text):
        end = min(offset + NER_CHUNK_CHARS, len(text))
        if end < len(text):
            boundary = text.rfind(" ", offset, end)
            if boundary > offset + NER_CHUNK_CHARS // 2:
                end = boundary
        chunks.append((text[offset:end], offset))
        offset = end
    return chunks
