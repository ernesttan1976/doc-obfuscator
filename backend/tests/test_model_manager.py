import hashlib
import io

import pytest

from backend.app import model_manager
from backend.app.candidate_engine import CandidateBlock
from backend.app.model_manager import MODEL_THRESHOLD, LocalModelManager, ModelFile


class FakeNerModel:
    def predict_entities(self, text, labels, threshold):
        assert labels
        assert threshold == MODEL_THRESHOLD
        start = text.find("Alex Tan")
        if start < 0:
            return []
        return [{"text": "Alex Tan", "label": "person", "score": 0.91, "start": start, "end": start + 8}]


def test_model_inference_chunks_text_and_restores_block_offsets(tmp_path, monkeypatch):
    manager = LocalModelManager(tmp_path / "models")
    manager._model = FakeNerModel()
    monkeypatch.setattr(manager, "_installed_manifest_is_valid", lambda: True)
    monkeypatch.setattr(model_manager.importlib.util, "find_spec", lambda _: object())
    text = f"{'x ' * 700}Alex Tan"

    entities, truncated = manager.extract_entities((CandidateBlock("paragraph:0", text),))

    assert truncated is False
    assert entities == [{
        "text": "Alex Tan",
        "location": "paragraph:0",
        "start": text.index("Alex Tan"),
        "end": text.index("Alex Tan") + 8,
        "label": "person",
        "score": 0.91,
    }]


def test_model_inference_reports_when_text_limit_truncates(tmp_path, monkeypatch):
    manager = LocalModelManager(tmp_path / "models")
    manager._model = FakeNerModel()
    monkeypatch.setattr(manager, "_installed_manifest_is_valid", lambda: True)
    monkeypatch.setattr(model_manager.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(model_manager, "MAX_NER_TEXT_CHARS", 4)

    entities, truncated = manager.extract_entities((CandidateBlock("text", "Alex Tan"),))

    assert entities == []
    assert truncated is True


def configure_tiny_download(monkeypatch, content):
    artifact = ModelFile(
        "weights.safetensors",
        len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    monkeypatch.setattr(model_manager, "MODEL_FILES", (artifact,))
    monkeypatch.setattr(model_manager, "MODEL_SIZE_BYTES", len(content))
    monkeypatch.setattr(model_manager, "MODEL_TOTAL_SHA256", "test-artifact-set")
    monkeypatch.setattr(model_manager, "MODEL_ID", "test/model")
    monkeypatch.setattr(model_manager, "MODEL_REVISION", "0123456789abcdef")
    return artifact


def test_explicit_model_download_verifies_artifact_before_atomic_install(tmp_path, monkeypatch):
    content = b"synthetic safe model"
    configure_tiny_download(monkeypatch, content)
    monkeypatch.setattr(model_manager, "urlopen", lambda *_args, **_kwargs: io.BytesIO(content))
    manager = LocalModelManager(tmp_path / "models")

    with pytest.raises(model_manager.ModelManagerError, match="Confirm"):
        manager.start_download(False)
    assert not manager.model_directory.exists()

    status = manager.start_download(True)
    manager._worker.join(timeout=2)

    assert status["status"] in {"downloading", "ready"}
    assert manager.status()["status"] == "ready"
    assert manager.status()["installed"] is True
    assert (manager.model_directory / "weights.safetensors").read_bytes() == content


def test_explicit_model_download_discards_artifacts_with_wrong_hash(tmp_path, monkeypatch):
    artifact = configure_tiny_download(monkeypatch, b"approved weights")
    monkeypatch.setattr(model_manager, "MODEL_FILES", (
        ModelFile(artifact.name, artifact.size, sha256="0" * 64),
    ))
    monkeypatch.setattr(model_manager, "urlopen", lambda *_args, **_kwargs: io.BytesIO(b"approved weights"))
    manager = LocalModelManager(tmp_path / "models")

    manager.start_download(True)
    manager._worker.join(timeout=2)

    assert manager.status()["status"] == "failed"
    assert manager.status()["installed"] is False
    assert not manager.model_directory.exists()
