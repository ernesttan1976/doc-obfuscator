import csv
import json
from io import BytesIO
from types import SimpleNamespace

import pytest

from backend.app.document_adapters import DocumentCell, ParsedDocument, WorksheetContent
from backend.app.prompt_iteration import (
    CSV_COLUMNS,
    PromptIterationError,
    _document_to_plain_text,
    _LocalOllayaPromptScorer,
    _revise_prompt_with_openai,
    iterate_prompt_for_file,
)


def test_local_ollaya_scorer_logs_raw_outputs_and_requests(tmp_path, monkeypatch):
    log_path = tmp_path / "ollaya.jsonl"
    answers = {
        "is_identifier": {"type": "choice", "choice": "Yes", "probabilities": {"Yes": 0.9}},
        "is_operationally_significant": {"type": "choice", "choice": "No", "probabilities": {"Yes": 0.1}},
        "is_common_word": {"type": "choice", "choice": "No", "probabilities": {"Yes": 0.2}},
    }
    response = json.dumps({"model": "von:1.1", "answers": answers})

    def fake_run(command, **_kwargs):
        if command[1] == "list":
            return SimpleNamespace(returncode=0, stdout="NAME\nvon:1.1\n", stderr="list diagnostic")
        return SimpleNamespace(returncode=0, stdout=response, stderr="scoring diagnostic")

    monkeypatch.setattr("backend.app.prompt_iteration.shutil.which", lambda _name: "/usr/bin/ollaya")
    monkeypatch.setattr("backend.app.prompt_iteration.subprocess.run", fake_run)
    scorer = _LocalOllayaPromptScorer(log_path=log_path)

    result = scorer.score({"candidate": "Falcon", "context": "Falcon launches at dawn."}, "Review entities")

    assert result == {
        "is_identifier": 0.9,
        "is_operationally_significant": 0.1,
        "is_common_word": 0.2,
    }
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [entry["event"] for entry in entries] == ["model_list", "score"]
    assert entries[0]["stdout"] == "NAME\nvon:1.1\n"
    assert entries[1]["stdout"] == response
    assert entries[1]["stderr"] == "scoring diagnostic"
    assert json.loads(entries[1]["request"]) == {
        "candidate": "Falcon",
        "context": "Falcon launches at dawn.",
    }


def test_iterate_prompt_for_file_reviews_corrects_and_runs_revised_prompt(tmp_path):
    source = tmp_path / "brief.txt"
    source.write_text("Falcon launches at dawn. falcon is the call sign.", encoding="utf-8")
    output_dir = tmp_path / "prompt-runs"
    observed_prompts = []
    revision_errors = []

    def score(state, prompt):
        observed_prompts.append(prompt)
        if prompt == "revised prompt":
            return {
                "is_identifier": 0.1,
                "is_operationally_significant": 0.1,
                "is_common_word": 0.9,
            }
        return {
            "is_identifier": 0.9,
            "is_operationally_significant": 0.9,
            "is_common_word": 0.1,
        }

    def revise(prompt, errors):
        revision_errors.extend(errors)
        return "revised prompt"

    answers = iter(["1", "n", "n", "y", ""])
    result = iterate_prompt_for_file(
        source,
        max_iterations=3,
        output_dir=output_dir,
        scorer=score,
        prompt_reviser=revise,
        input_fn=lambda _prompt: next(answers),
        output_fn=lambda _message: None,
    )

    assert result["final_run"] == "run002"
    assert result["incorrect_words"] == 0
    assert observed_prompts[0] != observed_prompts[-1]
    assert revision_errors[0]["word"] == "Falcon"
    assert revision_errors[0]["incorrect_signals"] == [
        "is_identifier",
        "is_operationally_significant",
        "is_common_word",
    ]

    with (output_dir / "run001.csv").open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        assert reader.fieldnames == list(CSV_COLUMNS)
        first_run_rows = list(reader)
    assert len(first_run_rows) == 8
    assert sum(row["word"] == "Falcon" for row in first_run_rows) == 1
    assert first_run_rows[0]["word"] == "Falcon"
    assert first_run_rows[0]["is_identifier_percent"] == "90.0"
    assert (output_dir / "prompt_v002.txt").read_text(encoding="utf-8").strip() == "revised prompt"

    with (output_dir / "run002-review.csv").open(encoding="utf-8", newline="") as stream:
        review = list(csv.DictReader(stream))
    assert review[0]["is_identifier_correct"] == "No"
    assert review[0]["is_common_word_correct"] == "Yes"
    assert review[0]["incorrect_signals"] == ""


@pytest.mark.parametrize(
    ("parsed", "expected"),
    [
        (
            ParsedDocument(format="DOCX", encoding="xml-utf-8", source=b"", text="Alpha\nBeta"),
            "Alpha\nBeta",
        ),
        (
            ParsedDocument(format="PPTX", encoding="xml-utf-8", source=b"", text="Slide one\nSlide two"),
            "Slide one\nSlide two",
        ),
        (
            ParsedDocument(
                format="XLSX",
                encoding="xml-utf-8",
                source=b"",
                sheets=(WorksheetContent("Data", (DocumentCell("A1", "Alpha"), DocumentCell("B1", "Beta"))),),
            ),
            "Alpha\nBeta",
        ),
    ],
)
def test_supported_office_content_is_flattened_to_plain_text(parsed, expected):
    assert _document_to_plain_text(parsed) == expected


def test_iterate_prompt_for_file_accepts_correct_classification_without_rewriting(tmp_path):
    source = tmp_path / "brief.txt"
    source.write_text("Project", encoding="utf-8")
    answers = iter([""])
    revise_calls = []

    result = iterate_prompt_for_file(
        source,
        output_dir=tmp_path / "prompt-runs",
        scorer=lambda _state, _prompt: {
            "is_identifier": 0.2,
            "is_operationally_significant": 0.3,
            "is_common_word": 0.8,
        },
        prompt_reviser=lambda *_args: revise_calls.append(True) or "unused",
        input_fn=lambda _prompt: next(answers),
        output_fn=lambda _message: None,
    )

    assert result["runs"][0]["reviewed_words"] == 0
    assert result["runs"][0]["incorrect_words"] == 0
    assert revise_calls == []


def test_iterate_prompt_for_file_rejects_nonempty_output_directory(tmp_path):
    source = tmp_path / "brief.txt"
    source.write_text("Project", encoding="utf-8")
    output_dir = tmp_path / "prompt-runs"
    output_dir.mkdir()
    (output_dir / "keep.txt").write_text("existing", encoding="utf-8")

    with pytest.raises(PromptIterationError, match="not empty"):
        iterate_prompt_for_file(source, output_dir=output_dir, scorer=lambda *_args: {})


def test_openai_prompt_rewriter_sends_reviewed_errors_to_configured_model(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data)
        response = {
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "Revised prompt\n"}]}
            ]
        }
        return BytesIO(json.dumps(response).encode("utf-8"))

    monkeypatch.setenv("OPENAI_API_KEY", "test-api-key")
    monkeypatch.setenv("PROMPT_REVISER_MODEL", "gpt-6-luna-test")
    monkeypatch.setattr("backend.app.prompt_iteration.urllib.request.urlopen", fake_urlopen)

    errors = [{"word": "Falcon", "context": "Falcon launches at dawn", "incorrect_signals": ["is_identifier"]}]
    revised = _revise_prompt_with_openai("Current prompt", errors, timeout_seconds=42)

    assert revised == "Revised prompt"
    assert captured["url"] == "https://api.openai.com/v1/responses"
    assert captured["authorization"] == "Bearer test-api-key"
    assert captured["timeout"] == 42
    assert captured["body"]["model"] == "gpt-6-luna-test"
    revision_input = json.loads(captured["body"]["input"])
    assert revision_input == {"current_prompt": "Current prompt", "reviewed_errors": errors}


def test_openai_prompt_rewriter_requires_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(PromptIterationError, match="OPENAI_API_KEY"):
        _revise_prompt_with_openai("Current prompt", [])
