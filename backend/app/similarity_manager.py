from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import platform
import shutil
import threading
import uuid
from pathlib import Path

from .candidate_engine import CandidateBlock, propose_contextual_variants
from .model_manager import ModelFile, ModelManagerError, _hashes_for_file
from .pinned_download import (
    DownloadCancelled,
    partial_download_size,
    resumable_download,
    urlopen,
)

SIMILARITY_MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
SIMILARITY_MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
SIMILARITY_MODEL_LICENSE = "Apache-2.0"
SIMILARITY_MODEL_DESCRIPTION = "Local English sentence embeddings for contextual suggestions"
SIMILARITY_FILES = (
    ModelFile("1_Pooling/config.json", 190, git_sha1="d1514c3162bbe87b343f565fadc62e6c06f04f03"),
    ModelFile("config.json", 612, git_sha1="72b987fd805cfa2b58c4c8c952b274a11bfd5a00"),
    ModelFile("model.safetensors", 90_868_376, sha256="53aa51172d142c89d9012cce15ae4d6cc0ca6895895114379cacb4fab128d9db"),
    ModelFile("special_tokens_map.json", 112, git_sha1="e7b0375001f109a6b8873d756ad4f7bbb15fbaa5"),
    ModelFile("tokenizer.json", 466_247, git_sha1="cb202bfe2e3c98645018a6d12f182a434c9d3e02"),
    ModelFile("tokenizer_config.json", 350, git_sha1="c79f2b6a0cea6f4b564fed1938984bace9d30ff0"),
    ModelFile("vocab.txt", 231_508, git_sha1="fb140275c155a9c7c5a3b3e0e77a9e839594a938"),
)
SIMILARITY_SIZE_BYTES = sum(model_file.size for model_file in SIMILARITY_FILES)
SIMILARITY_ARTIFACT_SHA256 = hashlib.sha256(
    "\n".join(
        f"{model_file.name}:{model_file.size}:{model_file.sha256 or model_file.git_sha1}"
        for model_file in SIMILARITY_FILES
    ).encode()
).hexdigest()
SIMILARITY_BATCH_SIZE = 32
SIMILARITY_MAX_LENGTH = 128


def _runtime_info() -> dict[str, str | bool]:
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Darwin":
        apple_silicon = machine in {"arm64", "aarch64"}
        available = apple_silicon and all(
            importlib.util.find_spec(module) is not None
            for module in ("mlx", "mlx_embeddings")
        )
        return {
            "runtime": "mlx",
            "runtimeAvailable": available,
            "runtimeDevice": "CPU (MLX)",
        }

    available = all(
        importlib.util.find_spec(module) is not None for module in ("torch", "transformers")
    )
    return {
        "runtime": "pytorch",
        "runtimeAvailable": available,
        "runtimeDevice": "CPU",
    }


class LocalSimilarityManager:
    """Explicitly acquire and run a pinned MiniLM model on CPU only."""

    def __init__(self, models_directory: Path | str | None = None) -> None:
        root = Path(models_directory or (Path.home() / ".blot" / "models")).expanduser()
        self.model_directory = root / "similarity" / "all-minilm-l6-v2"
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self._job: dict[str, object] = {"status": "idle", "downloadedBytes": 0}
        self._model = None
        self._tokenizer = None
        self._model_lock = threading.Lock()
        self._verified = False
        self._verification_lock = threading.Lock()

    def status(self) -> dict[str, object]:
        with self._lock:
            job = dict(self._job)
        runtime = _runtime_info()
        return {
            **job,
            "modelId": SIMILARITY_MODEL_ID,
            "description": SIMILARITY_MODEL_DESCRIPTION,
            "revision": SIMILARITY_MODEL_REVISION,
            "license": SIMILARITY_MODEL_LICENSE,
            "sizeBytes": SIMILARITY_SIZE_BYTES,
            "installed": self._installed_manifest_is_valid(),
            **runtime,
        }

    def start_download(self, confirmed: bool) -> dict[str, object]:
        if confirmed is not True:
            raise ModelManagerError("Confirm the MiniLM download before starting it.")
        with self._lock:
            if self._job.get("status") == "downloading":
                raise ModelManagerError("The MiniLM download is already running.")
            if self._installed_manifest_is_valid():
                self._job = {"status": "ready", "downloadedBytes": SIMILARITY_SIZE_BYTES}
                return self.status()
            if self.model_directory.exists():
                raise ModelManagerError("An incomplete MiniLM directory exists; remove or move it before retrying.")
            job_id = str(uuid.uuid4())
            partial_directory = self.model_directory.parent / f".{self.model_directory.name}.partial"
            self._cancel.clear()
            self._job = {
                "jobId": job_id,
                "status": "downloading",
                "downloadedBytes": partial_download_size(SIMILARITY_FILES, partial_directory),
                "totalBytes": SIMILARITY_SIZE_BYTES,
                "error": None,
            }
            self._worker = threading.Thread(target=self._download_worker, args=(job_id,), daemon=True)
            self._worker.start()
        return self.status()

    def cancel_download(self) -> dict[str, object]:
        with self._lock:
            if self._job.get("status") != "downloading":
                raise ModelManagerError("There is no MiniLM download to cancel.")
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
                SIMILARITY_FILES,
                temporary_directory,
                lambda artifact: (
                    f"https://huggingface.co/{SIMILARITY_MODEL_ID}/resolve/"
                    f"{SIMILARITY_MODEL_REVISION}/{artifact.name}"
                ),
                self._cancel,
                update_progress,
                opener=urlopen,
            )
            if self._cancel.is_set():
                raise DownloadCancelled
            manifest = {
                "modelId": SIMILARITY_MODEL_ID,
                "revision": SIMILARITY_MODEL_REVISION,
                "license": SIMILARITY_MODEL_LICENSE,
                "files": [
                    {
                        "name": model_file.name,
                        "size": model_file.size,
                        "sha256": model_file.sha256,
                        "gitSha1": model_file.git_sha1,
                    }
                    for model_file in SIMILARITY_FILES
                ],
                "artifactSetSha256": SIMILARITY_ARTIFACT_SHA256,
            }
            (temporary_directory / "blot-model-manifest.json").write_text(
                json.dumps(manifest, separators=(",", ":")), encoding="utf-8"
            )
            os.replace(temporary_directory, self.model_directory)
            with self._lock:
                if self._job.get("jobId") == job_id:
                    self._job = {"jobId": job_id, "status": "ready", "downloadedBytes": SIMILARITY_SIZE_BYTES}
        except DownloadCancelled:
            with self._lock:
                if self._job.get("jobId") == job_id:
                    self._job = {
                        "jobId": job_id,
                        "status": "cancelled",
                        "downloadedBytes": partial_download_size(SIMILARITY_FILES, temporary_directory),
                    }
        except Exception:  # noqa: BLE001 - remote/parser details must not reach the UI
            with self._lock:
                if self._job.get("jobId") == job_id:
                    self._job = {
                        "jobId": job_id,
                        "status": "failed",
                        "downloadedBytes": partial_download_size(SIMILARITY_FILES, temporary_directory),
                        "error": "Download failed or a MiniLM artifact did not match its pinned integrity check.",
                    }

    def _installed_manifest_is_valid(self) -> bool:
        try:
            manifest = json.loads(
                (self.model_directory / "blot-model-manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return False
        if (
            manifest.get("modelId") != SIMILARITY_MODEL_ID
            or manifest.get("revision") != SIMILARITY_MODEL_REVISION
            or manifest.get("artifactSetSha256") != SIMILARITY_ARTIFACT_SHA256
        ):
            return False
        return all(
            (self.model_directory / model_file.name).is_file()
            and (self.model_directory / model_file.name).stat().st_size == model_file.size
            for model_file in SIMILARITY_FILES
        )

    def _verify_installed_files(self) -> None:
        with self._verification_lock:
            if self._verified:
                return
            if not self._installed_manifest_is_valid():
                raise ModelManagerError("The local MiniLM files are incomplete or have an invalid manifest.")
            for model_file in SIMILARITY_FILES:
                sha256, git_sha1 = _hashes_for_file(self.model_directory / model_file.name, model_file.size)
                if model_file.sha256 and sha256 != model_file.sha256:
                    raise ModelManagerError("A local MiniLM file failed its SHA-256 integrity check.")
                if model_file.git_sha1 and git_sha1 != model_file.git_sha1:
                    raise ModelManagerError("A local MiniLM file failed its Git blob integrity check.")
            self._verified = True

    def propose(self, blocks: tuple[CandidateBlock, ...], candidates: list[dict[str, object]]) -> list[dict[str, object]]:
        if not self._installed_manifest_is_valid():
            return []
        runtime = _runtime_info()
        if not runtime["runtimeAvailable"]:
            if runtime["runtime"] == "mlx":
                message = "Install the macOS Apple-Silicon MiniLM runtime with `uv sync --extra minilm-macos`."
            else:
                message = "Install the Windows MiniLM runtime with `uv sync --extra minilm-windows`."
            raise ModelManagerError(message)
        return propose_contextual_variants(
            candidates,
            blocks,
            self.embed_contexts,
            similarity_matrix=self._similarity_matrix,
        )

    def _similarity_matrix(self, vectors: list[list[float]]) -> list[list[float]]:
        torch = getattr(self, "_torch", None)
        if torch is None:
            return [
                [sum(left * right for left, right in zip(first, second, strict=True)) for second in vectors]
                for first in vectors
            ]
        tensor = torch.tensor(vectors, dtype=torch.float32, device="cpu")
        return (tensor @ tensor.T).tolist()

    def embed_contexts(self, contexts: list[str]) -> list[list[float]]:
        if not contexts:
            return []
        runtime = _runtime_info()
        if not runtime["runtimeAvailable"]:
            raise ModelManagerError("The platform-specific local MiniLM runtime is not installed.")
        if self._model is None:
            with self._model_lock:
                if self._model is None:
                    try:
                        self._verify_installed_files()
                        if runtime["runtime"] == "mlx":
                            self._load_mlx_model()
                        else:
                            self._load_torch_model()
                    except Exception as exc:
                        if isinstance(exc, ModelManagerError):
                            raise
                        raise ModelManagerError(
                            f"The local MiniLM model could not load with {runtime['runtime']}."
                        ) from exc

        vectors: list[list[float]] = []
        try:
            for offset in range(0, len(contexts), SIMILARITY_BATCH_SIZE):
                batch = contexts[offset : offset + SIMILARITY_BATCH_SIZE]
                if runtime["runtime"] == "mlx":
                    encoded = self._tokenizer.batch_encode_plus(
                        batch,
                        padding=True,
                        truncation=True,
                        max_length=SIMILARITY_MAX_LENGTH,
                        return_tensors="mlx",
                    )
                    output = self._model(**encoded)
                    self._mx.eval(output.text_embeds)
                    batch_vectors = output.text_embeds.tolist()
                else:
                    encoded = self._tokenizer(
                        batch,
                        padding=True,
                        truncation=True,
                        max_length=SIMILARITY_MAX_LENGTH,
                        return_tensors="pt",
                    )
                    encoded = {key: value.to(self._device) for key, value in encoded.items()}
                    with self._torch.inference_mode():
                        output = self._model(**encoded)
                        mask = encoded["attention_mask"].unsqueeze(-1).to(output.last_hidden_state.dtype)
                        pooled = (output.last_hidden_state * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1e-9)
                        normalized = self._functional.normalize(pooled, p=2, dim=1)
                    batch_vectors = normalized.tolist()
                if any(not math.isfinite(value) for vector in batch_vectors for value in vector):
                    raise ModelManagerError("The local MiniLM model returned invalid embeddings.")
                vectors.extend(batch_vectors)
        except ModelManagerError:
            raise
        except Exception as exc:
            raise ModelManagerError("Local MiniLM inference failed on the selected platform runtime.") from exc
        return vectors

    def _load_mlx_model(self) -> None:
        import mlx.core as mx

        mx.set_default_device(mx.cpu)
        mlx_directory = self.model_directory.with_name(f"{self.model_directory.name}-mlx")
        mlx_manifest = mlx_directory / "blot-mlx-manifest.json"
        if not self._mlx_cache_is_valid(mlx_manifest):
            if mlx_directory.exists():
                raise ModelManagerError(
                    "The cached MLX MiniLM conversion is incomplete; remove its `-mlx` directory and retry."
                )
            temporary_directory = mlx_directory.with_name(f".{mlx_directory.name}.partial")
            if temporary_directory.exists():
                shutil.rmtree(temporary_directory)
            try:
                from mlx_embeddings.convert import convert

                convert(
                    hf_path=str(self.model_directory),
                    mlx_path=str(temporary_directory),
                    dtype="float32",
                )
                (temporary_directory / "blot-mlx-manifest.json").write_text(
                    json.dumps(
                        {
                            "modelId": SIMILARITY_MODEL_ID,
                            "revision": SIMILARITY_MODEL_REVISION,
                            "artifactSetSha256": SIMILARITY_ARTIFACT_SHA256,
                        },
                        separators=(",", ":"),
                    ),
                    encoding="utf-8",
                )
                if not (temporary_directory / "model.safetensors").is_file():
                    raise ModelManagerError("MLX conversion did not produce model weights.")
                os.replace(temporary_directory, mlx_directory)
            except Exception as exc:
                if isinstance(exc, ModelManagerError):
                    raise
                raise ModelManagerError(
                    "The local MiniLM files could not be converted for MLX offline."
                ) from exc

        from mlx_embeddings import load

        self._model, self._tokenizer = load(str(mlx_directory))
        self._mx = mx

    def _mlx_cache_is_valid(self, manifest_path: Path) -> bool:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return (
            manifest.get("modelId") == SIMILARITY_MODEL_ID
            and manifest.get("revision") == SIMILARITY_MODEL_REVISION
            and manifest.get("artifactSetSha256") == SIMILARITY_ARTIFACT_SHA256
            and manifest_path.with_name("model.safetensors").is_file()
        )

    def _load_torch_model(self) -> None:
        import torch
        from torch.nn import functional
        from transformers import AutoModel, AutoTokenizer

        self._tokenizer = AutoTokenizer.from_pretrained(
            str(self.model_directory), local_files_only=True, trust_remote_code=False
        )
        self._device = "cpu"
        self._model = AutoModel.from_pretrained(
            str(self.model_directory),
            local_files_only=True,
            trust_remote_code=False,
            use_safetensors=True,
        ).to(self._device)
        self._model.eval()
        self._torch = torch
        self._functional = functional
