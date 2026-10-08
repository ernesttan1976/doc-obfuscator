import json
import logging
import secrets
import subprocess
import threading
import time

import pytest

from backend.app.candidate_engine import CandidateBlock
from backend.app.key_store import KeyStoreUnavailable
from backend.app.ollaya_scoring import (
    LocalOllayaScorer,
    OllayaScoringError,
    apply_common_word_filter,
    build_ollaya_scoring_input,
    confidence_to_priority,
    get_ollaya_parallel_runs,
    score_result_from_signals,
    should_auto_suggest,
    validate_ollaya_response,
)
from backend.app.projects import ProjectService


def test_input_builder_sends_bounded_context_and_candidate_metadata():
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
    features = build_ollaya_scoring_input(candidate, [CandidateBlock("text", text)])

    assert set(features) == {"candidate", "category", "source", "occurrenceCount", "context"}
    assert features["candidate"] == "Project Falcon"
    assert features["category"] == "CAPITALIZED_PHRASE"
    assert features["source"] == "pattern"
    assert features["occurrenceCount"] == 1
    assert len(features["context"]) <= 800
    assert "Unrelated private appendix" not in features["context"]


def test_input_builder_samples_representative_contexts_across_occurrences():
    sentences = [
        "Project Falcon began in the west.",
        "Ordinary Falcon sightings are frequent.",
        "Project Falcon moved to the reserve.",
        "Falcon is a common bird in this area.",
        "Project Falcon was the codename for the plan.",
    ]
    text = " ".join(sentences)
    starts = [index for index in range(len(text)) if text.startswith("Falcon", index)]
    candidate = {
        "term": "Falcon",
        "category": "WORD",
        "source": "word",
        "occurrenceCount": len(starts),
        "occurrences": [
            {"location": "text", "start": start, "end": start + len("Falcon")}
            for start in starts
        ],
        "nerLabels": ["entity"],
    }

    features = build_ollaya_scoring_input(candidate, [CandidateBlock("text", text)])

    assert features["occurrenceCount"] == 5
    assert features["nerLabels"] == ["entity"]
    assert features["context"].count("Occurrence ") == 3
    assert "began in the west" in features["context"]
    assert "moved to the reserve" in features["context"]
    assert "codename for the plan" in features["context"]
    assert "sightings are frequent" not in features["context"]
    assert "common bird in this area" not in features["context"]
    assert len(features["context"]) <= 800


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
            "is_operationally_significant": {
                "type": "choice", "choice": "No", "probabilities": {"Yes": 0.23, "No": 0.77}
            },
            "is_common_word": {
                "type": "choice", "choice": "Yes", "probabilities": {"Yes": 0.99, "No": 0.01}
            },
        },
    }
    signals = validate_ollaya_response(response)
    result = score_result_from_signals(signals)
    assert result["redactionConfidence"] == 0.83
    assert result["reviewPriority"] == 4
    assert result["scoreStatus"] == "complete"
    assert result["commonWordProbability"] == 0.99
    assert should_auto_suggest({**result, "level": 10, "decision": "suggested"}) is True

    no_result = score_result_from_signals({
        "isIdentifier": {"answer": "No", "probabilityYes": 0.2},
        "isOperationallySignificant": {"answer": "No", "probabilityYes": 0.1},
        "isCommonWord": {"answer": "No", "probabilityYes": 0.4},
    })
    assert no_result["redactionConfidence"] is None
    assert no_result["reviewPriority"] is None
    assert should_auto_suggest({**no_result, "level": 2, "decision": "suggested"}) is False
    assert should_auto_suggest({"category": "WORD", "level": 8, "scoreStatus": "unavailable"}) is False
    assert should_auto_suggest({"category": "WORD", "level": 8, "redactionConfidence": 0.7}) is True
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


def test_configured_model_is_validated_and_reported():
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[-1] == "list":
            return subprocess.CompletedProcess(command, 0, stdout="NAME\ncustom:2 id 1GB now\n", stderr="")
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps({"model": "custom:2", "answers": {}}), stderr=""
        )

    result = LocalOllayaScorer(
        executable="/usr/local/bin/ollaya", model="custom:2", run=fake_run
    ).score_candidate({"candidate": "test"})

    assert calls[1][2] == "custom:2"
    assert result["scoringModel"] == "custom:2"
    assert result["scoreStatus"] == "unavailable"
    with pytest.raises(OllayaScoringError):
        validate_ollaya_response({"model": "custom:2", "answers": {}}, expected_model="von:1.1")


def test_local_cli_receives_and_logs_request_and_response_as_one_line(caplog):
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
                    "is_operationally_significant": {
                        "type": "choice", "choice": "No", "probabilities": {"Yes": 0.3, "No": 0.7}
                    },
                    "is_common_word": {
                        "type": "choice", "choice": "Yes", "probabilities": {"Yes": 0.7, "No": 0.3}
                    },
                },
            }),
            stderr="",
        )

    with caplog.at_level(logging.INFO, logger="backend.app.ollaya_scoring"):
        result = LocalOllayaScorer(executable="/usr/local/bin/ollaya", run=fake_run).score_candidate(
            {"candidate": "Private Project", "context": "Private context phrase"}
        )

    assert captured["command"][:3] == ["/usr/local/bin/ollaya", "run", "von:1.1"]
    questions_arg = captured["command"][captured["command"].index("--questions") + 1]
    questions = json.loads(questions_arg)
    assert set(questions) == {
        "is_identifier", "is_operationally_significant", "is_common_word"
    }
    identifier_prompt = questions["is_identifier"]["instructions"]
    assert "ordinary English word" in identifier_prompt
    assert "'abandon'" in identifier_prompt
    assert "capitalized" in identifier_prompt
    assert "clearly uses it to name" in identifier_prompt
    assert "'adapt'" in questions["is_common_word"]["instructions"]
    assert "Private Project" not in " ".join(captured["command"])
    assert json.loads(captured["input"]) == {
        "candidate": "Private Project",
        "context": "Private context phrase",
    }
    assert result["redactionConfidence"] == 0.7
    assert result["ollayaRequestVersion"] == "ollaya_request_v001"
    assert captured["timeout"] == 60
    records = [record for record in caplog.records if getattr(record, "ollaya_event", None) == "candidate_scoring_call"]
    assert len(records) == 1
    assert records[0].ollaya_model == "von:1.1"
    assert records[0].ollaya_outcome == "complete"
    assert records[0].ollaya_signal_count == 3
    log_line = records[0].getMessage()
    log_entry = json.loads(log_line)
    assert "\n" not in log_line
    assert log_entry["request"]["version"] == "ollaya_request_v001"
    assert log_entry["request"]["state"] == {
        "candidate": "Private Project",
        "context": "Private context phrase",
    }
    assert log_entry["response"]["model"] == "von:1.1"
    assert log_entry["response"]["answers"]["is_identifier"]["choice"] == "Yes"


def test_local_cli_caches_successful_scores_for_identical_inputs(caplog):
    scoring_calls = []

    def fake_run(command, **kwargs):
        if command[-1] == "list":
            return subprocess.CompletedProcess(command, 0, stdout="NAME\nvon:1.1 id 1GB now\n", stderr="")
        scoring_calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({
                "model": "von:1.1",
                "answers": {
                    "is_identifier": {
                        "type": "choice", "choice": "Yes", "probabilities": {"Yes": 0.8, "No": 0.2}
                    },
                    "is_operationally_significant": {
                        "type": "choice", "choice": "No", "probabilities": {"Yes": 0.2, "No": 0.8}
                    },
                    "is_common_word": {
                        "type": "choice", "choice": "No", "probabilities": {"Yes": 0.2, "No": 0.8}
                    },
                },
            }),
            stderr="",
        )

    scorer = LocalOllayaScorer(executable="/usr/local/bin/ollaya", run=fake_run)
    features = {"candidate": "Private Project", "context": "A short private context."}
    with caplog.at_level(logging.INFO, logger="backend.app.ollaya_scoring"):
        first = scorer.score_candidate(features)
        first["signals"]["isIdentifier"]["answer"] = "No"
        second = scorer.score_candidate({"context": features["context"], "candidate": features["candidate"]})
        scorer.score_candidate({**features, "context": "A different context."})
        scorer.clear_cache()
        scorer.score_candidate(features)

    assert len(scoring_calls) == 3
    assert second["signals"]["isIdentifier"]["answer"] == "Yes"
    records = [record for record in caplog.records if getattr(record, "ollaya_event", None) == "candidate_scoring_call"]
    assert [record.ollaya_cache_hit for record in records] == [False, True, False, False]


def test_shutdown_terminates_active_ollaya_process_and_unblocks_scoring_thread(monkeypatch):
    process_started = threading.Event()
    process_stopped = threading.Event()
    running_processes = {}

    class BlockingProcess:
        def __init__(self, command, **_kwargs):
            self.command = command
            self.pid = 1234
            self.returncode = None
            self.is_model_list = command[-1] == "list"
            if not self.is_model_list:
                running_processes[self.pid] = self

        def communicate(self, input=None, timeout=None):
            if self.is_model_list:
                self.returncode = 0
                return "NAME\nvon:1.1 id 1GB now\n", ""
            process_started.set()
            if not process_stopped.wait(timeout):
                raise subprocess.TimeoutExpired(self.command, timeout)
            return "", ""

        def wait(self, timeout=None):
            if not process_stopped.wait(timeout):
                raise subprocess.TimeoutExpired(self.command, timeout)
            return self.returncode

        def terminate(self):
            self.returncode = -15
            process_stopped.set()

        def kill(self):
            self.returncode = -9
            process_stopped.set()

    def signal_process_group(pid, process_signal):
        process = running_processes[pid]
        if process_signal.name == "SIGTERM":
            process.terminate()
        else:
            process.kill()

    monkeypatch.setattr("backend.app.ollaya_scoring.subprocess.Popen", BlockingProcess)
    monkeypatch.setattr("backend.app.ollaya_scoring.os.killpg", signal_process_group)
    scorer = LocalOllayaScorer(executable="/usr/local/bin/ollaya", model="von:1.1")
    errors = []

    def score():
        try:
            scorer.score_candidate({"candidate": "test"})
        except OllayaScoringError as exc:
            errors.append(exc)

    worker = threading.Thread(target=score)
    worker.start()
    assert process_started.wait(1)

    scorer.shutdown()
    worker.join(1)

    assert not worker.is_alive()
    assert errors
    assert not scorer._active_processes


def test_local_cli_does_not_attempt_to_download_a_missing_model_and_logs_failed_attempt(caplog):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="NAME ID SIZE MODIFIED\nlaya:en id 1GB now\n", stderr="")

    with (
        caplog.at_level(logging.WARNING, logger="backend.app.ollaya_scoring"),
        pytest.raises(OllayaScoringError, match="model is not installed"),
    ):
        LocalOllayaScorer(executable="/local/ollaya", run=fake_run).score_candidate({"candidate": "test"})
    assert calls == [["/local/ollaya", "list"]]
    records = [record for record in caplog.records if getattr(record, "ollaya_event", None) == "candidate_scoring_call"]
    assert len(records) == 1
    assert records[0].ollaya_outcome == "unavailable"
    assert json.loads(records[0].getMessage())["response"] is None


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
                "isOperationallySignificant": {"answer": "No", "probabilityYes": 0.2},
                "isCommonWord": {"answer": "Yes", "probabilityYes": 0.8},
            })

    project_dir = tmp_path / "workspace"
    project_dir.mkdir()
    source = tmp_path / "brief.txt"
    source.write_text("Alex Tan met Jordan Lee on Monday.", encoding="utf-8")
    scorer = NoSignalScorer()
    service = ProjectService(MemoryKeyStore(), ollaya_scorer=scorer)
    service.create(project_dir, "Test workspace")
    document = service.import_documents(project_dir, [source])[0]

    streamed = []
    analysis = service.analyze_document_candidates(
        project_dir,
        document.document_id,
        ["Alex Tan"],
        on_candidate=streamed.append,
    )
    by_term = {candidate["term"]: candidate for candidate in analysis["candidates"]}
    preview = service.preview_obfuscation(project_dir, document.document_id, 10)

    assert len(scorer.payloads) == len(analysis["candidates"])
    assert len(streamed) == len(analysis["candidates"]) * 2
    assert all(candidate["scoreStatus"] == "queued" for candidate in streamed[:len(analysis["candidates"])])
    assert all(candidate["scoreStatus"] == "complete" for candidate in streamed[len(analysis["candidates"]):])
    assert analysis["ollayaStatus"] == "ready"
    assert by_term["Alex Tan"]["decision"] == "included"
    assert by_term["Alex Tan"]["pinned"] is True
    assert by_term["Jordan"]["decision"] == "excluded"
    assert by_term["Jordan"]["commonWordProbability"] == 0.8
    assert by_term["Jordan"]["scoreStatus"] == "complete"
    assert by_term["Jordan"]["redactionConfidence"] is None
    assert [match["term"] for match in preview["matches"]] == ["Alex Tan"]
    encrypted_state = (project_dir / ".blot" / "private-state.enc").read_bytes()
    assert b"Alex Tan met Jordan Lee on Monday" not in encrypted_state
    assert b'"context"' not in encrypted_state


def test_project_analysis_reuses_saved_csv_scores_without_calling_ollaya(tmp_path):
    class Scorer:
        def __init__(self):
            self.calls = 0

        def score_candidate(self, _features):
            self.calls += 1
            return score_result_from_signals({
                "isIdentifier": {"answer": "Yes", "probabilityYes": 0.91},
                "isOperationallySignificant": {"answer": "No", "probabilityYes": 0.2},
                "isCommonWord": {"answer": "No", "probabilityYes": 0.1},
            })

    project_dir = tmp_path / "workspace"
    project_dir.mkdir()
    source = tmp_path / "brief.txt"
    source.write_text("Alex Tan briefed Jordan Lee on Project Falcon.", encoding="utf-8")
    key_store = MemoryKeyStore()
    initial_scorer = Scorer()
    initial_service = ProjectService(key_store, ollaya_scorer=initial_scorer)
    initial_service.create(project_dir, "Test workspace")
    document = initial_service.import_documents(project_dir, [source])[0]

    initial = initial_service.analyze_document_candidates(project_dir, document.document_id)
    reloaded_scorer = Scorer()
    reloaded_service = ProjectService(key_store, ollaya_scorer=reloaded_scorer)
    reloaded = reloaded_service.analyze_document_candidates(project_dir, document.document_id)
    cached_call_count = reloaded_scorer.calls
    by_term = {candidate["term"]: candidate for candidate in reloaded["candidates"]}
    cleared = reloaded_service.clear_ollaya_results(project_dir, document.document_id)
    rescored = reloaded_service.analyze_document_candidates(project_dir, document.document_id)

    assert initial_scorer.calls == len(initial["candidates"])
    assert cached_call_count == 0
    assert cleared["clearedCount"] == len(initial["candidates"])
    assert reloaded_scorer.calls == len(rescored["candidates"])
    assert reloaded["ollayaStatus"] == "ready"
    assert all(candidate["scoreStatus"] == "complete" for candidate in reloaded["candidates"])
    assert by_term["Alex"]["signals"]["isIdentifier"]["probabilityYes"] == 0.91


def test_project_analysis_runs_ollaya_candidates_up_to_configured_parallel_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAYA_PARALLEL_RUNS", "2")

    class ConcurrentScorer:
        def __init__(self):
            self.lock = threading.Lock()
            self.active = 0
            self.peak_active = 0

        def score_candidate(self, features):
            with self.lock:
                self.active += 1
                self.peak_active = max(self.peak_active, self.active)
            try:
                time.sleep(0.05)
                return score_result_from_signals({
                    "isIdentifier": {"answer": "No", "probabilityYes": 0.1},
                    "isOperationallySignificant": {"answer": "No", "probabilityYes": 0.2},
                    "isCommonWord": {"answer": "No", "probabilityYes": 0.1},
                })
            finally:
                with self.lock:
                    self.active -= 1

    project_dir = tmp_path / "workspace"
    project_dir.mkdir()
    source = tmp_path / "brief.txt"
    source.write_text("Alex Tan met Jordan Lee near Project Falcon.", encoding="utf-8")
    scorer = ConcurrentScorer()
    service = ProjectService(MemoryKeyStore(), ollaya_scorer=scorer)
    service.create(project_dir, "Test workspace")
    document = service.import_documents(project_dir, [source])[0]

    analysis = service.analyze_document_candidates(
        project_dir,
        document.document_id,
        ["Alex Tan", "Jordan Lee", "Project Falcon"],
    )

    assert len(analysis["candidates"]) >= 2
    assert scorer.peak_active == 2
    assert all(candidate["scoreStatus"] == "complete" for candidate in analysis["candidates"])


def test_ollaya_parallel_runs_reads_env_and_falls_back_for_invalid_values(monkeypatch):
    monkeypatch.setenv("OLLAYA_PARALLEL_RUNS", "7")
    assert get_ollaya_parallel_runs() == 7

    monkeypatch.setenv("OLLAYA_PARALLEL_RUNS", "invalid")
    assert get_ollaya_parallel_runs() == 1

    monkeypatch.setenv("OLLAYA_PARALLEL_RUNS", "0")
    assert get_ollaya_parallel_runs() == 1


def test_common_word_and_signals_set_automatic_decision_without_overriding_manual_choice():
    common = {
        "decision": "suggested",
        "signals": {
            "isCommonWord": {"answer": "Yes", "probabilityYes": 0.9},
            "isIdentifier": {"answer": "No", "probabilityYes": 0.1},
            "isOperationallySignificant": {"answer": "No", "probabilityYes": 0.2},
        },
    }
    apply_common_word_filter(common)
    assert common["decision"] == "excluded"
    assert common["commonWordFilterStatus"] == "common"
    assert common["commonWordOverride"] is False

    identified = {
        "decision": "suggested",
        "signals": {
            "isCommonWord": {"answer": "Yes", "probabilityYes": 0.9},
            "isIdentifier": {"answer": "Yes", "probabilityYes": 0.8},
        },
    }
    apply_common_word_filter(identified)
    assert identified["decision"] == "included"
    assert identified["commonWordFilterStatus"] == "overridden"
    assert identified["commonWordOverride"] is True

    operational = {
        "decision": "suggested",
        "signals": {
            "isCommonWord": {"answer": "Yes", "probabilityYes": 0.9},
            "isOperationallySignificant": {"answer": "Yes", "probabilityYes": 0.8},
        },
    }
    apply_common_word_filter(operational)
    assert operational["decision"] == "included"
    assert operational["commonWordOverride"] is True

    uncommon_identifier = {
        "decision": "suggested",
        "signals": {"isIdentifier": {"answer": "Yes", "probabilityYes": 0.8}},
    }
    apply_common_word_filter(uncommon_identifier)
    assert uncommon_identifier["decision"] == "included"

    manual = {
        "decision": "included",
        "pinned": True,
        "signals": {
            "isCommonWord": {"answer": "Yes", "probabilityYes": 0.9},
            "isIdentifier": {"answer": "No", "probabilityYes": 0.1},
            "isOperationallySignificant": {"answer": "No", "probabilityYes": 0.2},
        },
    }
    apply_common_word_filter(manual)
    assert manual["decision"] == "included"

    unavailable = {"decision": "suggested", "signals": {}}
    apply_common_word_filter(unavailable)
    assert unavailable["decision"] == "suggested"
    assert unavailable["commonWordFilterStatus"] == "unavailable"
