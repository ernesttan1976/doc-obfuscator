import csv
import subprocess

import pytest

from backend.app.document_adapters import DocumentCell, ParsedDocument, WorksheetContent
from backend.app.prompt_iteration import (
    CSV_COLUMNS,
    PromptIterationError,
    _document_to_plain_text,
    _revise_prompt_locally,
    iterate_prompt_for_file,
)


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


def test_local_prompt_rewriter_ignores_remote_ollama_host(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="Revised prompt\n", stderr="")

    monkeypatch.setenv("OLLAMA_HOST", "https://example.invalid")
    monkeypatch.setattr("backend.app.prompt_iteration.shutil.which", lambda _name: "/usr/bin/ollama")
    monkeypatch.setattr("backend.app.prompt_iteration.subprocess.run", fake_run)

    revised = _revise_prompt_locally("Current prompt", [], model="test-model")

    assert revised == "Revised prompt"
    assert captured["command"] == ["/usr/bin/ollama", "run", "test-model"]
    assert "OLLAMA_HOST" not in captured["env"]
