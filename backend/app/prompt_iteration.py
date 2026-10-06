from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections import OrderedDict
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .candidate_engine import CandidateBlock, blocks_for_document
from .document_adapters import ParsedDocument, parse_document
from .ollaya_scoring import SCORING_MODEL, validate_ollaya_response

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

INITIAL_PROMPT = (
    "Classify each candidate word using its context. Answer all three questions "
    "independently. Judge identifier and operational significance in context; "
    "judge common-word status from the word itself, ignoring capitalization. "
    "A word may be common and also identify an entity or have operational "
    "significance. Return a Yes/No choice and its probability for every signal."
)
WORD_PATTERN = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
SIGNALS = (
    "is_identifier",
    "is_operationally_significant",
    "is_common_word",
)
CSV_COLUMNS = (
    "word",
    "is_identifier_percent",
    "is_operationally_significant_percent",
    "is_common_word_percent",
)
SIGNAL_OUTPUT_NAMES = {
    "is_identifier": "isIdentifier",
    "is_operationally_significant": "isOperationallySignificant",
    "is_common_word": "isCommonWord",
}
SIGNAL_QUESTIONS = {
    "is_identifier": (
        (
            "Does the candidate identify a person, organization, project, location, "
            "system, codeword, or other named entity in this context?"
        ),
        "It identifies a named entity in this context.",
        "It does not identify a named entity in this context.",
    ),
    "is_operationally_significant": (
        (
            "Does the candidate identify an operational activity, capability, "
            "vulnerability, plan, or resource in this context?"
        ),
        "It identifies an operational concept in this context.",
        "It does not identify an operational concept in this context.",
    ),
    "is_common_word": (
        (
            "Is this a common English word? Ignore capitalization and judge the "
            "word itself, independently of its use as an entity in context."
        ),
        "This is a common English word.",
        "This is uncommon, invented, malformed, or not a common English word.",
    ),
}
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_PROMPT_REVISER_MODEL = "gpt-6-luna"


class PromptIterationError(Exception):
    """The prompt-iteration workflow cannot safely continue."""


class _LocalOllayaPromptScorer:
    def __init__(
        self,
        model: str = SCORING_MODEL,
        timeout_seconds: int = 60,
        log_path: Path | None = None,
    ) -> None:
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.log_path = log_path
        self.executable = shutil.which("ollaya")
        self._model_checked = False

    def score(self, word: dict[str, str], prompt: str) -> dict[str, float]:
        if not self.executable:
            raise PromptIterationError("The local Ollaya CLI is not installed or is not on PATH.")
        self._ensure_model()
        questions = _questions_for_prompt(prompt)
        command = [
            self.executable,
            "run",
            self.model,
            "--questions",
            json.dumps(questions, ensure_ascii=False, separators=(",", ":")),
            "--format",
            "json",
            "--state-json",
        ]
        request_input = json.dumps(word, ensure_ascii=False, separators=(",", ":"))
        request_payload = {
            "model": self.model,
            "questions": questions,
            "state": word,
            "format": "json",
        }
        self._log_output(
            "request", "score", command, request_payload, raw_stdin=request_input
        )
        try:
            completed = subprocess.run(
                command,
                input=request_input,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._log_output("response", "score", command, request_payload, error=exc)
            raise PromptIterationError("The local Ollaya scoring call failed or timed out.") from exc
        self._log_output("response", "score", command, request_payload, completed=completed)
        if completed.returncode != 0:
            raise PromptIterationError("Local Ollaya scoring failed.")
        try:
            response = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise PromptIterationError("Ollaya returned malformed JSON.") from exc

        signals = validate_ollaya_response(response, expected_model=self.model)
        scores: dict[str, float] = {}
        for signal in SIGNALS:
            result = signals.get(SIGNAL_OUTPUT_NAMES[signal])
            if not isinstance(result, dict):
                raise PromptIterationError(f"Ollaya did not return a valid {signal} answer.")
            scores[signal] = float(result["probabilityYes"])
        return scores

    def _ensure_model(self) -> None:
        if self._model_checked:
            return
        assert self.executable is not None
        command = [self.executable, "list"]
        request_payload = {"operation": "list_models"}
        self._log_output("request", "model_list", command, request_payload)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._log_output("response", "model_list", command, request_payload, error=exc)
            raise PromptIterationError("The local Ollaya model list could not be checked.") from exc
        self._log_output("response", "model_list", command, request_payload, completed=completed)
        installed = {
            line.split()[0]
            for line in completed.stdout.splitlines()
            if line.split() and line.split()[0] != "NAME"
        }
        if completed.returncode != 0 or self.model not in installed:
            raise PromptIterationError(f"The configured local Ollaya model {self.model!r} is not installed.")
        self._model_checked = True

    def _log_output(
        self,
        event: str,
        operation: str,
        command: Sequence[str],
        request: dict[str, Any],
        *,
        raw_stdin: str | None = None,
        completed: Any = None,
        error: BaseException | None = None,
    ) -> None:
        if self.log_path is None:
            return

        def output_text(value: Any) -> str | None:
            if value is None:
                return None
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace")
            return str(value)

        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            "operation": operation,
            "model": self.model,
            "command": list(command),
            "request": request,
            "raw_stdin": raw_stdin,
        }
        if event == "response":
            entry["response"] = {
                "returncode": getattr(completed, "returncode", None),
                "stdout": output_text(
                    getattr(completed, "stdout", None) if completed is not None else getattr(error, "stdout", None)
                ),
                "stderr": output_text(
                    getattr(completed, "stderr", None) if completed is not None else getattr(error, "stderr", None)
                ),
                "error": f"{type(error).__name__}: {error}" if error is not None else None,
            }
        try:
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError as exc:
            raise PromptIterationError(f"Could not write Ollaya output log: {self.log_path}") from exc


def _questions_for_prompt(prompt: str) -> dict[str, dict[str, Any]]:
    questions = {}
    for signal, (question, yes_criteria, no_criteria) in SIGNAL_QUESTIONS.items():
        questions[signal] = {
            "type": "choice",
            "instructions": f"{prompt}\n\nSignal to evaluate: {question}",
            "criteria": {"Yes": yes_criteria, "No": no_criteria},
        }
    return questions


def _extract_unique_words(blocks: Sequence[CandidateBlock]) -> list[dict[str, str]]:
    words: OrderedDict[str, dict[str, str]] = OrderedDict()
    for block in blocks:
        for match in WORD_PATTERN.finditer(block.text):
            word = match.group()
            normalized = word.casefold()
            if normalized in words:
                continue
            left = max(0, match.start() - 80)
            right = min(len(block.text), match.end() + 80)
            context = block.text[left:right].strip()
            words[normalized] = {
                "word": word,
                "context": context,
                "example_id": normalized,
            }
    return list(words.values())


def _document_to_plain_text(parsed: ParsedDocument) -> str:
    """Flatten adapter-extracted editable text to plain text before tokenization."""
    blocks = blocks_for_document(parsed)
    return "\n".join(block.text for block in blocks if block.text.strip())


def _get_yes_probability(scores: dict[str, float], signal: str) -> float:
    value = scores.get(signal)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise PromptIterationError(f"The scorer returned an invalid probability for {signal}.")
    return float(value)


def _csv_row(row: dict[str, Any]) -> dict[str, str]:
    scores = row["scores"]
    return {
        "word": row["word"],
        "is_identifier_percent": f"{_get_yes_probability(scores, SIGNALS[0]) * 100:.1f}",
        "is_operationally_significant_percent": f"{_get_yes_probability(scores, SIGNALS[1]) * 100:.1f}",
        "is_common_word_percent": f"{_get_yes_probability(scores, SIGNALS[2]) * 100:.1f}",
    }


def _prediction(scores: dict[str, float], signal: str) -> bool:
    return _get_yes_probability(scores, signal) >= 0.5


def _review_rows(
    rows: Sequence[dict[str, Any]],
    gold_labels: dict[str, dict[str, bool]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    review = []
    errors = []
    for row in rows:
        gold = gold_labels.get(row["example_id"])
        if gold is None:
            continue
        incorrect_signals = [
            signal for signal in SIGNALS
            if _prediction(row["scores"], signal) != gold[signal]
        ]
        review.append({
            "word": row["word"],
            "context": row["context"],
            **{f"{signal}_correct": "Yes" if gold[signal] else "No" for signal in SIGNALS},
            "incorrect_signals": ";".join(incorrect_signals),
        })
        if incorrect_signals:
            errors.append({
                "word": row["word"],
                "context": row["context"],
                "predicted": {signal: _prediction(row["scores"], signal) for signal in SIGNALS},
                "correct": gold.copy(),
                "incorrect_signals": incorrect_signals,
            })
    return review, errors


def _collect_user_review(
    rows: Sequence[dict[str, Any]],
    gold_labels: dict[str, dict[str, bool]],
    *,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> None:
    for index, row in enumerate(rows, start=1):
        status = "reviewed" if row["example_id"] in gold_labels else "not yet reviewed"
        percentages = ", ".join(
            f"{signal}={_get_yes_probability(row['scores'], signal) * 100:.1f}%"
            for signal in SIGNALS
        )
        output_fn(f"{index}. {row['word']} [{status}] — {percentages}\n   {row['context']}")

    while True:
        selection = input_fn(
            "Enter the numbers of incorrectly classified words (comma-separated), "
            "or Enter if there are no new corrections: "
        ).strip().lower()
        if not selection:
            return
        if selection in {"q", "quit", "exit"}:
            raise PromptIterationError("Prompt iteration cancelled by the user.")
        try:
            selected = sorted({int(value.strip()) for value in selection.split(",")})
        except ValueError:
            output_fn("Enter comma-separated row numbers, or press Enter to continue.")
            continue
        if not selected or any(number < 1 or number > len(rows) for number in selected):
            output_fn(f"Choose row numbers between 1 and {len(rows)}.")
            continue
        break

    for number in selected:
        row = rows[number - 1]
        labels = {}
        output_fn(f"Enter the correct Yes/No labels for {row['word']!r}:")
        for signal in SIGNALS:
            while True:
                value = input_fn(f"  {signal} [y/n]: ").strip().casefold()
                if value in {"y", "yes"}:
                    labels[signal] = True
                    break
                if value in {"n", "no"}:
                    labels[signal] = False
                    break
                output_fn("Please enter y or n.")
        gold_labels[row["example_id"]] = labels


def _revise_prompt_with_openai(
    current_prompt: str,
    errors: Sequence[dict[str, Any]],
    *,
    model: str | None = None,
    timeout_seconds: int = 180,
) -> str:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise PromptIterationError("Set OPENAI_API_KEY in the repository .env file to revise prompts with OpenAI.")
    selected_model = (model or os.environ.get("PROMPT_REVISER_MODEL", "")).strip() or DEFAULT_PROMPT_REVISER_MODEL
    revision_request = {
        "current_prompt": current_prompt,
        "reviewed_errors": list(errors),
    }
    request_body = {
        "model": selected_model,
        "instructions": (
            "Improve the word-classification prompt based on the reviewed errors. Return only the revised "
            "prompt text. Preserve the three independent signals (named-entity identification, operational "
            "significance, and common-word status). Do not change the 50% decision threshold, modify any "
            "corrected labels, or infer labels from redaction decisions. Make a focused change that addresses "
            "the demonstrated error patterns."
        ),
        "input": json.dumps(revision_request, ensure_ascii=False),
        "max_output_tokens": 2048,
    }
    request = urllib.request.Request(
        OPENAI_RESPONSES_URL,
        data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            response_data = json.loads(response.read())
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise PromptIterationError("The OpenAI prompt-revision request failed or returned invalid JSON.") from exc

    revised = response_data.get("output_text", "") if isinstance(response_data, dict) else ""
    if not revised and isinstance(response_data, dict):
        output = response_data.get("output", [])
        if isinstance(output, list):
            revised = "\n".join(
                content.get("text", "")
                for item in output
                if isinstance(item, dict) and isinstance(item.get("content"), list)
                for content in item["content"]
                if isinstance(content, dict) and content.get("type") == "output_text"
            )
    if not isinstance(revised, str) or not revised.strip():
        raise PromptIterationError("OpenAI did not return revised prompt text.")
    return revised.strip()


def iterate_prompt_for_file(
    filename: str | Path,
    *,
    max_iterations: int = 5,
    output_dir: str | Path | None = None,
    scorer: Callable[[dict[str, str], str], dict[str, float]] | None = None,
    prompt_reviser: Callable[[str, Sequence[dict[str, Any]]], str] | None = None,
    input_fn: Callable[[str], str] | None = None,
    output_fn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Score unique words, collect corrections, and iteratively revise the prompt.

    Ollaya performs local classification. The default prompt rewriter sends reviewed
    error examples to the configured OpenAI model using OPENAI_API_KEY; tests or
    embedding applications can inject both model calls. Output artifacts are
    written beside the source unless output_dir is supplied.
    """
    source_path = Path(filename).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"Input file not found: {source_path}")
    if max_iterations < 1:
        raise ValueError("max_iterations must be at least 1")
    try:
        parsed = parse_document(source_path.read_bytes(), source_path.suffix)
    except OSError as exc:
        raise PromptIterationError(f"Could not read input file: {source_path}") from exc
    plain_text = _document_to_plain_text(parsed)
    word_rows = _extract_unique_words((CandidateBlock("plain-text", plain_text),))
    if not word_rows:
        raise PromptIterationError("The input file contains no words to classify.")

    if output_dir is None:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        run_directory = source_path.parent / f"{source_path.name}.prompt-iterations" / timestamp
    else:
        run_directory = Path(output_dir).expanduser().resolve()
        if run_directory.exists() and any(run_directory.iterdir()):
            raise PromptIterationError(f"Output directory is not empty: {run_directory}")
    run_directory.mkdir(parents=True, exist_ok=True)

    local_ollaya_log = run_directory / "ollaya.jsonl" if scorer is None else None
    score_word = scorer or _LocalOllayaPromptScorer(log_path=local_ollaya_log).score
    revise_prompt = prompt_reviser or _revise_prompt_with_openai
    read_input = input_fn or input
    write_output = output_fn or print
    if local_ollaya_log is not None:
        write_output(f"Ollaya request/response log: {local_ollaya_log}")
    if parsed.warnings:
        write_output("Document-to-text conversion warnings: " + "; ".join(parsed.warnings))
    prompt = INITIAL_PROMPT
    gold_labels: dict[str, dict[str, bool]] = {}
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    run_summaries = []

    for run_number in range(1, max_iterations + 1):
        run_id = f"run{run_number:03d}"
        prompt_version = run_number
        prompt_path = run_directory / f"prompt_v{prompt_version:03d}.txt"
        prompt_path.write_text(prompt + "\n", encoding="utf-8")
        rows = []
        write_output(f"\n{run_id} — prompt v{prompt_version:03d}: scoring {len(word_rows)} unique words")
        csv_path = run_directory / f"{run_id}.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS, extrasaction="raise")
            writer.writeheader()
            stream.flush()
            for word in word_rows:
                scores = score_word({"candidate": word["word"], "context": word["context"]}, prompt)
                row = {**word, "scores": scores}
                rows.append(row)
                writer.writerow(_csv_row(row))
                stream.flush()

        _collect_user_review(
            rows,
            gold_labels,
            input_fn=read_input,
            output_fn=write_output,
        )
        review_rows, errors = _review_rows(rows, gold_labels)
        review_path = run_directory / f"{run_id}-review.csv"
        review_columns = [
            "word",
            "context",
            *(f"{signal}_correct" for signal in SIGNALS),
            "incorrect_signals",
        ]
        with review_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=review_columns)
            writer.writeheader()
            writer.writerows(review_rows)

        reviewed_count = len(review_rows)
        summary = {
            "run_id": run_id,
            "prompt_version": prompt_version,
            "csv": csv_path.name,
            "review_csv": review_path.name,
            "reviewed_words": reviewed_count,
            "incorrect_words": len(errors),
            "incorrect_signals": {
                signal: sum(signal in error["incorrect_signals"] for error in errors)
                for signal in SIGNALS
            },
            "error_rate": len(errors) / reviewed_count if reviewed_count else None,
        }
        metadata = {
            **summary,
            "prompt_file": prompt_path.name,
            "model": getattr(scorer, "model", SCORING_MODEL),
            "prompt_reviser_model": (
                getattr(prompt_reviser, "model", None)
                if prompt_reviser
                else os.environ.get("PROMPT_REVISER_MODEL", "").strip() or DEFAULT_PROMPT_REVISER_MODEL
            ),
            "threshold_percent": 50,
            "source_sha256": source_hash,
            "source_format": parsed.format,
            "plain_text_character_count": len(plain_text),
            "conversion_warnings": list(parsed.warnings),
            "unsupported_part_count": len(parsed.unsupported_parts),
            "unique_word_count": len(word_rows),
        }
        (run_directory / f"{run_id}.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        run_summaries.append(summary)
        write_output(
            f"{run_id}: {summary['incorrect_words']} incorrect of {reviewed_count} reviewed "
            f"words; per-signal errors={summary['incorrect_signals']}"
        )

        if not errors or run_number == max_iterations:
            break

        prompt = revise_prompt(prompt, errors)
        if not isinstance(prompt, str) or not prompt.strip():
            raise PromptIterationError("The prompt-revision model returned an empty prompt.")
        prompt = prompt.strip()

    (run_directory / "prompt_iterations.json").write_text(
        json.dumps(run_summaries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    final = run_summaries[-1]
    return {
        "run_directory": str(run_directory),
        "runs": run_summaries,
        "final_run": final["run_id"],
        "incorrect_words": final["incorrect_words"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review and iterate Ollaya word-classification prompts.")
    parser.add_argument("filename", help="TXT, MD, CSV, XLSX, DOCX, or PPTX file to score")
    parser.add_argument("--max-iterations", type=int, default=5)
    parser.add_argument("--output-dir", help="Use a specific empty directory for run artifacts")
    args = parser.parse_args(argv)
    try:
        result = iterate_prompt_for_file(
            args.filename,
            max_iterations=args.max_iterations,
            output_dir=args.output_dir,
        )
    except (FileNotFoundError, PromptIterationError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
