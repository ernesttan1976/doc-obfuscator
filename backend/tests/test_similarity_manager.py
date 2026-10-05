import hashlib
import io
import sys
import types
from pathlib import Path

import pytest

from backend.app import similarity_manager
from backend.app.candidate_engine import CandidateBlock, analyze_candidates
from backend.app.model_manager import ModelFile
from backend.app.similarity_manager import LocalSimilarityManager


def configure_tiny_download(monkeypatch, content):
    artifact = ModelFile(
        "model.safetensors",
        len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    monkeypatch.setattr(similarity_manager, "SIMILARITY_FILES", (artifact,))
    monkeypatch.setattr(similarity_manager, "SIMILARITY_SIZE_BYTES", len(content))
    monkeypatch.setattr(similarity_manager, "SIMILARITY_ARTIFACT_SHA256", "test-artifact-set")
    monkeypatch.setattr(similarity_manager, "SIMILARITY_MODEL_ID", "test/minilm")
    monkeypatch.setattr(similarity_manager, "SIMILARITY_MODEL_REVISION", "0123456789abcdef")
    return artifact


def test_minilm_download_requires_confirmation_and_verifies_before_install(tmp_path, monkeypatch):
    content = b"synthetic MiniLM weights"
    configure_tiny_download(monkeypatch, content)
    monkeypatch.setattr(similarity_manager, "urlopen", lambda *_args, **_kwargs: io.BytesIO(content))
    manager = LocalSimilarityManager(tmp_path / "models")

    with pytest.raises(similarity_manager.ModelManagerError, match="Confirm"):
        manager.start_download(False)
    assert not manager.model_directory.exists()

    status = manager.start_download(True)
    manager._worker.join(timeout=2)

    assert status["status"] == "downloading"
    assert manager.status()["status"] == "ready"
    assert manager.status()["installed"] is True
    assert (manager.model_directory / "model.safetensors").read_bytes() == content


def test_minilm_download_discards_artifacts_with_wrong_hash(tmp_path, monkeypatch):
    artifact = configure_tiny_download(monkeypatch, b"approved MiniLM weights")
    monkeypatch.setattr(
        similarity_manager,
        "SIMILARITY_FILES",
        (ModelFile(artifact.name, artifact.size, sha256="0" * 64),),
    )
    monkeypatch.setattr(similarity_manager, "urlopen", lambda *_args, **_kwargs: io.BytesIO(b"approved MiniLM weights"))
    manager = LocalSimilarityManager(tmp_path / "models")

    manager.start_download(True)
    manager._worker.join(timeout=2)

    assert manager.status()["status"] == "failed"
    assert manager.status()["installed"] is False
    assert not manager.model_directory.exists()


def test_minilm_absent_uses_no_remote_or_implicit_model_fallback(tmp_path):
    manager = LocalSimilarityManager(tmp_path / "models")
    block = CandidateBlock("text", "Alex Tan works as a senior engineer. Jordan Lee is a senior engineer.")
    candidates, _ = analyze_candidates([block], "doc-1", "version-1")

    assert manager.propose((block,), candidates) == []
    assert not (tmp_path / "models").exists()


def test_minilm_runtime_selects_mlx_only_on_apple_silicon(monkeypatch):
    monkeypatch.setattr(similarity_manager.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(similarity_manager.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(similarity_manager.importlib.util, "find_spec", lambda _: object())

    status = similarity_manager._runtime_info()

    assert status == {
        "runtime": "mlx",
        "runtimeAvailable": True,
        "runtimeDevice": "CPU (MLX)",
    }


def test_minilm_runtime_selects_cpu_pytorch_on_windows(monkeypatch):
    monkeypatch.setattr(similarity_manager.platform, "system", lambda: "Windows")
    monkeypatch.setattr(similarity_manager.importlib.util, "find_spec", lambda _: object())

    status = similarity_manager._runtime_info()

    assert status == {
        "runtime": "pytorch",
        "runtimeAvailable": True,
        "runtimeDevice": "CPU",
    }


def test_minilm_torch_model_is_always_loaded_on_cpu(tmp_path, monkeypatch):
    loaded_devices = []

    class FakeModel:
        def to(self, device):
            loaded_devices.append(device)
            return self

        def eval(self):
            return self

    class NoCudaAccess:
        def __getattr__(self, name):
            raise AssertionError(f"CUDA must not be accessed: {name}")

    torch = types.ModuleType("torch")
    torch.cuda = NoCudaAccess()
    torch_nn = types.ModuleType("torch.nn")
    torch_functional = types.ModuleType("torch.nn.functional")
    torch_nn.functional = torch_functional
    transformers = types.ModuleType("transformers")
    transformers.AutoTokenizer = types.SimpleNamespace(
        from_pretrained=lambda *_args, **_kwargs: object()
    )
    transformers.AutoModel = types.SimpleNamespace(
        from_pretrained=lambda *_args, **_kwargs: FakeModel()
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "torch.nn", torch_nn)
    monkeypatch.setitem(sys.modules, "torch.nn.functional", torch_functional)
    monkeypatch.setitem(sys.modules, "transformers", transformers)

    manager = LocalSimilarityManager(tmp_path / "models")
    manager._load_torch_model()

    assert loaded_devices == ["cpu"]
    assert manager._device == "cpu"


def test_minilm_mlx_load_sets_cpu_device_and_converts_locally(tmp_path, monkeypatch):
    selected_devices = []
    loaded_paths = []
    mlx_package = types.ModuleType("mlx")
    mlx_core = types.ModuleType("mlx.core")
    mlx_core.cpu = object()
    mlx_core.set_default_device = selected_devices.append
    mlx_package.core = mlx_core
    mlx_embeddings = types.ModuleType("mlx_embeddings")
    mlx_embeddings.__path__ = []

    def load(path):
        loaded_paths.append(Path(path))
        return "model", "tokenizer"

    mlx_embeddings.load = load
    converter = types.ModuleType("mlx_embeddings.convert")

    def convert(*, hf_path, mlx_path, dtype):
        assert Path(hf_path) == manager.model_directory
        assert dtype == "float32"
        output = Path(mlx_path)
        output.mkdir()
        (output / "model.safetensors").write_bytes(b"converted locally")

    converter.convert = convert
    monkeypatch.setitem(sys.modules, "mlx", mlx_package)
    monkeypatch.setitem(sys.modules, "mlx.core", mlx_core)
    monkeypatch.setitem(sys.modules, "mlx_embeddings", mlx_embeddings)
    monkeypatch.setitem(sys.modules, "mlx_embeddings.convert", converter)
    manager = LocalSimilarityManager(tmp_path / "models")
    manager.model_directory.parent.mkdir(parents=True)

    manager._load_mlx_model()

    assert selected_devices == [mlx_core.cpu]
    assert manager._model == "model"
    assert manager._tokenizer == "tokenizer"
    assert loaded_paths[0].is_dir()


def test_minilm_proposals_are_local_and_separate_from_group_confirmation(tmp_path, monkeypatch):
    manager = LocalSimilarityManager(tmp_path / "models")
    monkeypatch.setattr(manager, "_installed_manifest_is_valid", lambda: True)
    monkeypatch.setattr(similarity_manager.importlib.util, "find_spec", lambda _: object())
    block = CandidateBlock(
        "text",
        "Alex Tan, the senior engineer, signed the confidential report. "
        "Jordan Lee, the senior engineer, approved the final budget.",
    )
    candidates, _ = analyze_candidates([block], "doc-1", "version-1")
    manager.embed_contexts = lambda contexts: [
        [1.0, 0.0] if "senior engineer" in context else [0.0, 1.0]
        for context in contexts
    ]

    proposals = manager.propose((block,), candidates)
    names = {candidate["term"]: candidate["id"] for candidate in candidates}

    assert any(
        {proposal["sourceId"], proposal["targetId"]} == {names["Alex Tan"], names["Jordan Lee"]}
        and proposal["method"] == "minilm"
        and proposal["confirmed"] is False
        for proposal in proposals
    )
