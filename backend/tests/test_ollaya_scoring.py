import json
import secrets
import subprocess

import pytest

from backend.app.candidate_engine import CandidateBlock
from backend.app.key_store import KeyStoreUnavailable
from backend.app.ollaya_scoring import (
    LocalOllayaScorer,
    OllayaScoringError,
    build_ollaya_scoring_input,
    confidence_to_priority,
    score_result_from_signals,
    should_auto_suggest,
    validate_ollaya_response,
)
from backend.app.projects import ProjectService


def test_input_builder_includes_only_bounded_occurrence_context_and_extraction_facts():
    text = "Opening sentence. Project Falcon starts on Monday. Unrelated private appendix text."
    candidate = {
        "id": "project",
        "term": "Project Falcon",
        "source": "pattern",
        "category": "CAPITALIZED_PHRASE",
        "occurrenceCount": 1,
        "occurrences": [{"location": "text", "start": text.index("Project Falcon"), "end": text.index("Project Falcon") + len("Project Falcon")}],
        "nerLabels": [],
    }
    nearby = {
        "id": "date",
        "category": "DATE",
        "occurrences": [{"location": "text", "start": text.index("Monday"), "end": text.index("Monday") + len("Monday")}],
    }

    features = build_ollaya_scoring_input(candidate, [CandidateBlock("text", text)], [candidate, nearby])

    assert features["schemaVersion"] == "blot_ollaya_input_v1"
    assert features["candidate"] == "Project Falcon"
    assert features["occurrenceCount"] == 1
    assert features["nearbyEntityTypes"] == ["date"]
    assert len("".join(features["contextSnippets"])) <= 512
    assert all("Unrelated private appendix" not in snippet for snippet in features["contextSnippets"])


def test_confidence_priority_boundaries_follow_scoring_plan():
    assert [confidence_to_priority(value) for value in (
        0.49, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.0
    )] == [None, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 1]
    assert confidence_to_priority(None) is None
    assert confidence_to_priority(1.01) is None


def test_signal_validation_confidence_and_automatic_suggestion_rules():
    response = {
        "model": "von:1.1",
        "answers": {
            "is_identifier": {
                "type": "choice", "choice": "Yes", "probabilities": {"Yes": 0.83, "No": 0.17}
            },
            "has_operational_significance": {
                "type": "choice", "choice": "No", "probabilities": {"Yes": 0.23, "No": 0.77}
            },
        },
    }
    signals = validate_ollaya_response(response)
    result = score_result_from_signals(signals)
    assert result["redactionConfidence"] == 0.83
    assert result["reviewPriority"] == 4
    assert result["scoreStatus"] == "complete"
    assert should_auto_suggest({**result, "level": 10, "decision": "suggested"}) is True

    no_result = score_result_from_signals({
        "isIdentifier": {"answer": "No", "probabilityYes": 0.2},
        "hasOperationalSignificance": {"answer": "No", "probabilityYes": 0.1},
    })
    assert no_result["redactionConfidence"] is None
    assert no_result["reviewPriority"] is None
    assert should_auto_suggest({**no_result, "level": 2, "decision": "suggested"}) is False
    assert should_auto_suggest({**no_result, "level": 2, "decision": "included", "pinned": True}) is True
    assert should_auto_suggest({**result, "decision": "excluded", "pinned": True}) is False


def test_contradictory_or_missing_signal_is_unavailable_not_a_no():
    response = {
        "model": "von:1.1",
        "answers": {
            "is_identifier": {
                "type": "choice", "choice": "Yes", "probabilities": {"Yes": 0.2, "No": 0.8}
            },
        },
    }
    signals = validate_ollaya_response(response)
    assert signals == {}
    assert score_result_from_signals(signals)["scoreStatus"] == "unavailable"
    with pytest.raises(OllayaScoringError):
        validate_ollaya_response({"model": "laya:en", "answers": {}})

    partial = validate_ollaya_response({
        "model": "von:1.1",
        "answers": {
            "is_identifier": {
                "type": "choice", "choice": "Yes", "probabilities": {"Yes": 0.8, "No": 0.2}
            },
        },
    })
    assert score_result_from_signals(partial)["scoreStatus"] == "partial"


def test_local_cli_receives_candidate_payload_on_stdin_not_in_command_arguments():
    captured = {}

    def fake_run(command, **kwargs):
        if command[-1] == "list":
            return subprocess.CompletedProcess(command, 0, stdout="NAME\n...\nvon:1.1 id 1GB now\n", stderr="")
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({
                "model": "von:1.1",
                "answers": {
                    "is_identifier": {
                        "type": "choice", "choice": "Yes", "probabilities": {"Yes": 0.7, "No": 0.3}
                    },
                    "has_operational_significance": {
                        "type": "choice", "choice": "No", "probabilities": {"Yes": 0.3, "No": 0.7}
                    },
                },
            }),
            stderr="",
        )

    result = LocalOllayaScorer(executable="/usr/local/bin/ollaya", run=fake_run).score_candidate(
        {"candidate": "Private Project"}
    )

    assert captured["command"][:3] == ["/usr/local/bin/ollaya", "run", "von:1.1"]
    assert "Private Project" not in " ".join(captured["command"])
    assert json.loads(captured["input"]) == {"candidate": "Private Project"}
    assert result["redactionConfidence"] == 0.7
    assert captured["timeout"] == 60


def test_local_cli_does_not_attempt_to_download_a_missing_model():
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="NAME ID SIZE MODIFIED\nlaya:en id 1GB now\n", stderr="")

    with pytest.raises(OllayaScoringError, match="model is not installed"):
        LocalOllayaScorer(executable="/local/ollaya", run=fake_run).score_candidate({"candidate": "test"})
    assert calls == [["/local/ollaya", "list"]]


class MemoryKeyStore:
    def __init__(self):
        self.keys = {}

    def get_or_create(self, project_id):
        return self.keys.setdefault(project_id, secrets.token_bytes(32))

    def get(self, project_id):
        if project_id not in self.keys:
            raise KeyStoreUnavailable("missing test key")
        return self.keys[project_id]

    def delete(self, project_id):
        self.keys.pop(project_id, None)


def test_project_analysis_scores_each_candidate_and_keeps_manual_decisions_authoritative(tmp_path):
    class NoSignalScorer:
        def __init__(self):
            self.payloads = []

        def score_candidate(self, features):
            self.payloads.append(features)
            return score_result_from_signals({
                "isIdentifier": {"answer": "No", "probabilityYes": 0.1},
                "hasOperationalSignificance": {"answer": "No", "probabilityYes": 0.2},
            })

    project_dir = tmp_path / "workspace"
    project_dir.mkdir()
    source = tmp_path / "brief.txt"
    source.write_text("Alex Tan met Jordan Lee on Monday.", encoding="utf-8")
    scorer = NoSignalScorer()
    service = ProjectService(MemoryKeyStore(), ollaya_scorer=scorer)
    service.create(project_dir, "Test workspace")
    document = service.import_documents(project_dir, [source])[0]

    analysis = service.analyze_document_candidates(project_dir, document.document_id, ["Alex Tan"])
    by_term = {candidate["term"]: candidate for candidate in analysis["candidates"]}
    preview = service.preview_obfuscation(project_dir, document.document_id, 10)

    assert len(scorer.payloads) == len(analysis["candidates"])
    assert analysis["ollayaStatus"] == "ready"
    assert by_term["Alex Tan"]["decision"] == "included"
    assert by_term["Alex Tan"]["pinned"] is True
    assert by_term["Jordan Lee"]["scoreStatus"] == "complete"
    assert by_term["Jordan Lee"]["redactionConfidence"] is None
    assert [match["term"] for match in preview["matches"]] == ["Alex Tan"]
    encrypted_state = (project_dir / ".blot" / "private-state.enc").read_bytes()
    assert b"Alex Tan met Jordan Lee on Monday" not in encrypted_state
    assert b"contextSnippets" not in encrypted_state
