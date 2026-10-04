import hashlib
import io

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
