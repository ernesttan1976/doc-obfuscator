from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, load_dotenv

from .candidate_engine import CandidateBlock

DOTENV_PATH = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(DOTENV_PATH)


def _load_scoring_model(dotenv_path: Path) -> str:
    """Read the configured model from .env; do not silently substitute a model tag."""
    model = dotenv_values(dotenv_path).get("OLLAYA_MODEL")
    if not isinstance(model, str) or not model.strip():
        raise RuntimeError(f"Set a non-empty OLLAYA_MODEL in {dotenv_path}.")
    return model.strip()


SCORING_METHOD = "ollaya_yes_no_v6"
SCORING_MODEL = _load_scoring_model(DOTENV_PATH)
OLLAYA_REQUESTS_DIRECTORY = Path(__file__).resolve().parents[2] / "ollaya_requests"
MAX_CONTEXT_CHARS = 800
MAX_CONTEXT_WINDOW_CHARS = 240
MAX_CONTEXT_SAMPLES = 3
DEFAULT_TIMEOUT_SECONDS = 60
MAX_SCORE_CACHE_ENTRIES = 2048


def get_ollaya_parallel_runs() -> int:
    """Return the configured maximum number of concurrent Ollaya candidate runs."""
    try:
        return max(1, int(os.environ.get("OLLAYA_PARALLEL_RUNS", "1").strip()))
    except (AttributeError, ValueError):
        return 1


_SENTENCE_BOUNDARY = re.compile(r"[.!?;\n]")
_SIGNAL_NAMES = {
    "is_identifier": "isIdentifier",
    "is_operationally_significant": "isOperationallySignificant",
    "is_common_word": "isCommonWord",
}
_REDACTION_SIGNAL_NAMES = {
    "is_identifier": "isIdentifier",
    "is_operationally_significant": "isOperationallySignificant",
}
_SIGNAL_REASONS = {
    "is_identifier": "Identifies a named entity in context",
    "is_operationally_significant": "Has operational significance in context",
}
_LOGGER = logging.getLogger(__name__)


class OllayaScoringError(Exception):
    """The local Ollaya CLI or its response is unavailable or invalid."""


_REQUEST_VERSION_PATTERN = re.compile(r"^ollaya_request_v(\d{3,})\.json$")
_EXPECTED_QUESTION_NAMES = {
    "is_identifier",
    "is_operationally_significant",
    "is_common_word",
}


def _load_latest_ollaya_request(
    requests_directory: Path | None = None,
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Load and validate the numerically newest immutable request manifest."""
    requests_directory = requests_directory or OLLAYA_REQUESTS_DIRECTORY
    versions = []
    try:
        for path in requests_directory.iterdir():
            match = _REQUEST_VERSION_PATTERN.fullmatch(path.name)
            if match and path.is_file():
                versions.append((int(match.group(1)), path))
    except OSError as exc:
        raise OllayaScoringError(
            f"Could not read Ollaya request versions from {requests_directory}."
        ) from exc

    if not versions:
        raise OllayaScoringError(
            f"No versioned Ollaya request manifests found in {requests_directory}."
        )

    _, latest_path = max(versions, key=lambda item: item[0])
    try:
        manifest = json.loads(latest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OllayaScoringError(
            f"Could not load latest Ollaya request manifest {latest_path.name}."
        ) from exc

    expected_version = latest_path.stem
    if not isinstance(manifest, dict) or manifest.get("version") != expected_version:
        raise OllayaScoringError(
            f"Ollaya request manifest {latest_path.name} has a mismatched version field."
        )
    format_version = manifest.get("format_version")
    if isinstance(format_version, bool) or format_version != 1:
        raise OllayaScoringError(
            f"Ollaya request manifest {latest_path.name} uses an unsupported format version."
        )
    if not isinstance(manifest.get("transport"), dict) or manifest["transport"].get("format") != "json":
        raise OllayaScoringError(
            f"Ollaya request manifest {latest_path.name} has an invalid transport format."
        )

    questions = manifest.get("questions")
    if not isinstance(questions, dict) or set(questions) != _EXPECTED_QUESTION_NAMES:
        raise OllayaScoringError(
            f"Ollaya request manifest {latest_path.name} must define exactly the three scoring questions."
        )
    for name, question in questions.items():
        criteria = question.get("criteria") if isinstance(question, dict) else None
        if (
            not isinstance(question, dict)
            or question.get("type") != "choice"
            or not isinstance(question.get("instructions"), str)
            or not question["instructions"].strip()
            or not isinstance(criteria, dict)
            or not all(
                isinstance(criteria.get(choice), str) and criteria[choice].strip()
                for choice in ("Yes", "No")
            )
        ):
            raise OllayaScoringError(
                f"Ollaya request manifest {latest_path.name} has an invalid {name} question."
            )

    return expected_version, questions


OLLAYA_REQUEST_VERSION, _QUESTIONS = _load_latest_ollaya_request()


def build_ollaya_scoring_input(
    candidate: dict[str, Any],
    text_blocks: Sequence[CandidateBlock],
    *,
    max_context_chars: int = MAX_CONTEXT_CHARS,
) -> dict[str, Any]:
    """Build a compact, transient payload with representative bounded use contexts."""
    term = str(candidate.get("term", ""))
    block_by_location = {block.location: block.text for block in text_blocks}
    raw_occurrences = candidate.get("occurrences", [])
    occurrences = raw_occurrences if isinstance(raw_occurrences, list) else []
    occurrence_count = candidate.get("occurrenceCount")
    if not isinstance(occurrence_count, int) or isinstance(occurrence_count, bool) or occurrence_count < 0:
        occurrence_count = len(occurrences)

    features: dict[str, Any] = {
        "candidate": term,
        "occurrenceCount": occurrence_count,
    }
    for key in ("category", "source"):
        value = candidate.get(key)
        if isinstance(value, str) and value:
            features[key] = value
    ner_labels = candidate.get("nerLabels")
    if isinstance(ner_labels, list):
        labels = [label for label in ner_labels if isinstance(label, str)]
        if labels:
            features["nerLabels"] = labels

    snippets: list[str] = []
    for occurrence in occurrences:
        if not isinstance(occurrence, dict):
            continue
        location = occurrence.get("location")
        start = occurrence.get("start")
        end = occurrence.get("end")
        text = block_by_location.get(location)
        if (
            not isinstance(text, str)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or start < 0
            or end <= start
            or end > len(text)
            or text[start:end].casefold() != term.casefold()
        ):
            continue

        left, right = _sentence_window(text, start, end, MAX_CONTEXT_WINDOW_CHARS)
        snippet = text[left:right].strip()
        if snippet and snippet not in snippets:
            snippets.append(snippet)

    if snippets:
        sample_indices = _spread_sample_indices(len(snippets), MAX_CONTEXT_SAMPLES)
        context = "\n---\n".join(
            f"Occurrence {index + 1}: {snippets[index]}" for index in sample_indices
        )
        context_limit = max(0, min(max_context_chars, MAX_CONTEXT_CHARS))
        if context_limit:
            features["context"] = context[:context_limit]
    return features


def _spread_sample_indices(item_count: int, sample_limit: int) -> list[int]:
    if item_count <= sample_limit:
        return list(range(item_count))
    return [round(index * (item_count - 1) / (sample_limit - 1)) for index in range(sample_limit)]


def _sentence_window(text: str, start: int, end: int, max_chars: int) -> tuple[int, int]:
    before = [match.end() for match in _SENTENCE_BOUNDARY.finditer(text, max(0, start - max_chars), start)]
    after = _SENTENCE_BOUNDARY.search(text, end, min(len(text), end + max_chars))
    left = before[-1] if before else max(0, start - max_chars // 2)
    right = after.start() + 1 if after else min(len(text), end + max_chars // 2)
    if right - left > max_chars:
        extra = max_chars - (end - start)
        left = max(left, start - extra // 2)
        right = min(right, left + max_chars)
        left = max(0, right - max_chars)
    return left, right


def calculate_redaction_confidence(signals: Iterable[dict[str, Any]]) -> float | None:
    affirmative = [
        float(signal["probabilityYes"])
        for signal in signals
        if signal.get("answer") == "Yes" and _valid_probability(signal.get("probabilityYes"))
    ]
    return max(affirmative) if affirmative else None


def confidence_to_priority(confidence: float | None) -> int | None:
    if confidence is None or not _valid_probability(confidence) or confidence < 0.50:
        return None
    if confidence >= 0.95:
        return 1
    if confidence >= 0.90:
        return 2
    if confidence >= 0.85:
        return 3
    if confidence >= 0.80:
        return 4
    if confidence >= 0.75:
        return 5
    if confidence >= 0.70:
        return 6
    if confidence >= 0.65:
        return 7
    if confidence >= 0.60:
        return 8
    if confidence >= 0.55:
        return 9
    return 10


def validate_ollaya_response(
    response: Any, expected_model: str = SCORING_MODEL
) -> dict[str, dict[str, str | float]]:
    """Keep individually valid typed signals; malformed or contradictory ones are unavailable."""
    if not isinstance(response, dict) or response.get("model") != expected_model:
        raise OllayaScoringError("Ollaya returned an unexpected model response.")
    answers = response.get("answers")
    if not isinstance(answers, dict):
        raise OllayaScoringError("Ollaya returned an invalid answer set.")
    signals: dict[str, dict[str, str | float]] = {}
    for source_name, public_name in _SIGNAL_NAMES.items():
        raw = answers.get(source_name)
        if not isinstance(raw, dict) or raw.get("type") != "choice":
            continue
        answer = raw.get("choice")
        probabilities = raw.get("probabilities")
        probability_yes = probabilities.get("Yes") if isinstance(probabilities, dict) else None
        if answer not in {"Yes", "No"} or not _valid_probability(probability_yes):
            continue
        if (answer == "Yes" and probability_yes < 0.5) or (answer == "No" and probability_yes > 0.5):
            continue
        signals[public_name] = {"answer": answer, "probabilityYes": float(probability_yes)}
    return signals


def score_result_from_signals(
    signals: dict[str, dict[str, str | float]], model: str = SCORING_MODEL
) -> dict[str, Any]:
    affirmative_sources = [
        source_name
        for source_name, public_name in _REDACTION_SIGNAL_NAMES.items()
        if signals.get(public_name, {}).get("answer") == "Yes"
    ]
    confidence = calculate_redaction_confidence(
        signals.get(public_name, {}) for public_name in _REDACTION_SIGNAL_NAMES.values()
    )
    priority = confidence_to_priority(confidence)
    valid_signal_count = len(signals)
    reasons = [_SIGNAL_REASONS[name] for name in affirmative_sources]
    common_word_signal = signals.get("isCommonWord", {})
    return {
        "redactionConfidence": confidence,
        "reviewPriority": priority,
        "scoringMethod": SCORING_METHOD,
        "ollayaRequestVersion": OLLAYA_REQUEST_VERSION,
        "scoringModel": model,
        "signals": signals,
        "commonWordProbability": common_word_signal.get("probabilityYes"),
        "reasons": reasons,
        "scoreStatus": "complete" if valid_signal_count == len(_SIGNAL_NAMES) else "partial" if valid_signal_count else "unavailable",
    }


class LocalOllayaScorer:
    """Runs Ollaya's supported local CLI; candidate data is sent only on stdin."""

    def __init__(
        self,
        executable: str | None = None,
        model: str = SCORING_MODEL,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        run: Callable[..., Any] | None = None,
    ) -> None:
        self.executable = executable
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._run = run
        self._shutdown_event = threading.Event()
        self._process_lock = threading.Lock()
        self._active_processes: set[subprocess.Popen[str]] = set()
        self._model_checked = False
        self._model_check_lock = threading.Lock()
        self._score_cache: OrderedDict[str, tuple[Any, dict[str, Any]]] = OrderedDict()
        self._score_cache_lock = threading.Lock()
        self._score_inflight: dict[str, threading.Lock] = {}

    def status(self) -> dict[str, Any]:
        executable = self.executable or shutil.which("ollaya")
        return {
            "configured": bool(executable),
            "model": self.model,
            "interface": "local-cli",
        }

    def clear_cache(self) -> None:
        with self._score_cache_lock:
            self._score_cache.clear()

    def shutdown(self) -> None:
        """Stop active Ollaya CLI process groups and prevent new calls during app exit."""
        self._shutdown_event.set()
        with self._process_lock:
            processes = list(self._active_processes)

        for process in processes:
            self._signal_process(process, signal.SIGTERM)
        for process in processes:
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self._signal_process(process, signal.SIGKILL)
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    _LOGGER.warning("Ollaya process %s did not exit after shutdown", process.pid)

    def score_candidate(self, features: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        outcome = "unavailable"
        signal_count = 0
        response_for_log: Any = None
        cache_hit = False
        try:
            response_for_log, result, cache_hit = self._score_candidate_cached(features)
            outcome = str(result.get("scoreStatus", "unavailable"))
            signals = result.get("signals")
            signal_count = len(signals) if isinstance(signals, dict) else 0
            return result
        finally:
            call_log = {
                "event": "ollaya_call",
                "model": self.model,
                "cacheHit": cache_hit,
                "outcome": outcome,
                "durationMs": round((time.perf_counter() - started) * 1000, 1),
                "validSignalCount": signal_count,
                "request": {
                    "version": OLLAYA_REQUEST_VERSION,
                    "questions": _QUESTIONS,
                    "state": features,
                    "format": "json",
                },
                "response": response_for_log,
            }
            _LOGGER.log(
                logging.WARNING if outcome == "unavailable" else logging.INFO,
                "%s",
                json.dumps(call_log, ensure_ascii=False, separators=(",", ":")),
                extra={
                    "ollaya_event": "candidate_scoring_call",
                    "ollaya_model": self.model,
                    "ollaya_outcome": outcome,
                    "ollaya_cache_hit": cache_hit,
                    "ollaya_duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    "ollaya_signal_count": signal_count,
                },
            )

    def _score_candidate_cached(
        self, features: dict[str, Any]
    ) -> tuple[Any, dict[str, Any], bool]:
        cache_key = _score_cache_key(self.model, features)
        with self._score_cache_lock:
            cached = self._score_cache.get(cache_key)
            if cached is not None:
                self._score_cache.move_to_end(cache_key)
                return deepcopy(cached[0]), deepcopy(cached[1]), True
            inflight_lock = self._score_inflight.setdefault(cache_key, threading.Lock())

        try:
            with inflight_lock:
                with self._score_cache_lock:
                    cached = self._score_cache.get(cache_key)
                    if cached is not None:
                        self._score_cache.move_to_end(cache_key)
                        return deepcopy(cached[0]), deepcopy(cached[1]), True

                response = self._score_candidate(features)
                result = score_result_from_signals(
                    validate_ollaya_response(response, expected_model=self.model),
                    model=self.model,
                )
                with self._score_cache_lock:
                    self._score_cache[cache_key] = (deepcopy(response), deepcopy(result))
                    self._score_cache.move_to_end(cache_key)
                    while len(self._score_cache) > MAX_SCORE_CACHE_ENTRIES:
                        self._score_cache.popitem(last=False)
                return response, result, False
        finally:
            with self._score_cache_lock:
                if self._score_inflight.get(cache_key) is inflight_lock:
                    del self._score_inflight[cache_key]

    def _score_candidate(self, features: dict[str, Any]) -> Any:
        executable = self.executable or shutil.which("ollaya")
        if not executable:
            raise OllayaScoringError("The local Ollaya CLI is not installed or is not on PATH.")
        self._raise_if_shutting_down()
        self._ensure_model_installed(executable)
        command = [
            executable,
            "run",
            self.model,
            "--questions",
            json.dumps(_QUESTIONS, separators=(",", ":")),
            "--format",
            "json",
            "--state-json",
        ]
        try:
            completed = self._execute(
                command,
                input=json.dumps(features, ensure_ascii=False, separators=(",", ":")),
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise OllayaScoringError("Local Ollaya scoring timed out.") from exc
        except OSError as exc:
            raise OllayaScoringError("The local Ollaya CLI could not be started.") from exc
        if completed.returncode != 0:
            raise OllayaScoringError("Local Ollaya scoring failed.")
        try:
            response = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise OllayaScoringError("Ollaya returned malformed JSON.") from exc
        return response

    def _ensure_model_installed(self, executable: str) -> None:
        if self._model_checked:
            return
        with self._model_check_lock:
            if self._model_checked:
                return
            try:
                completed = self._execute(
                    [executable, "list"],
                    timeout=min(self.timeout_seconds, 10),
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise OllayaScoringError("The local Ollaya model list could not be checked.") from exc
            if completed.returncode != 0:
                raise OllayaScoringError("The local Ollaya model list could not be checked.")
            installed_models = {
                line.split()[0]
                for line in completed.stdout.splitlines()
                if line.split() and line.split()[0] != "NAME"
            }
            if self.model not in installed_models:
                raise OllayaScoringError("The configured local Ollaya model is not installed.")
            self._model_checked = True

    def _execute(
        self,
        command: list[str],
        *,
        timeout: int,
        input: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self._raise_if_shutting_down()
        if self._run is not None:
            return self._run(
                command,
                input=input,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )

        with self._process_lock:
            self._raise_if_shutting_down()
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=(os.name == "posix"),
            )
            self._active_processes.add(process)
        try:
            try:
                stdout, stderr = process.communicate(input=input, timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                self._signal_process(process, signal.SIGKILL)
                process.communicate()
                raise exc
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        finally:
            with self._process_lock:
                self._active_processes.discard(process)

    def _raise_if_shutting_down(self) -> None:
        if self._shutdown_event.is_set():
            raise OllayaScoringError("Ollaya scoring stopped because the application is shutting down.")

    @staticmethod
    def _signal_process(process: subprocess.Popen[str], process_signal: signal.Signals) -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, process_signal)
            elif process_signal == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()
        except (OSError, ProcessLookupError):
            pass


def unavailable_score() -> dict[str, Any]:
    return {
        "redactionConfidence": None,
        "reviewPriority": None,
        "scoringMethod": SCORING_METHOD,
        "scoringModel": None,
        "signals": {},
        "commonWordProbability": None,
        "reasons": [],
        "scoreStatus": "unavailable",
    }


def apply_common_word_filter(candidate: dict[str, Any]) -> None:
    """Annotate common-word signals without treating them as user decisions."""
    signals = candidate.get("signals")
    signals = signals if isinstance(signals, dict) else {}
    common = signals.get("isCommonWord")
    identifier = signals.get("isIdentifier")
    operational = signals.get("isOperationallySignificant")
    common_yes = isinstance(common, dict) and common.get("answer") == "Yes"
    decisive_yes = any(
        isinstance(signal, dict) and signal.get("answer") == "Yes"
        for signal in (identifier, operational)
    )
    candidate["commonWordFilterStatus"] = (
        "unavailable" if not isinstance(common, dict)
        else "overridden" if common_yes and decisive_yes
        else "common" if common_yes
        else "not_common"
    )
    candidate["commonWordOverride"] = common_yes and decisive_yes
    if candidate.get("pinned"):
        return
    if decisive_yes:
        candidate["decision"] = "included"
    elif (
        common_yes
        and isinstance(identifier, dict)
        and identifier.get("answer") == "No"
        and isinstance(operational, dict)
        and operational.get("answer") == "No"
    ):
        candidate["decision"] = "excluded"


def should_auto_suggest(candidate: dict[str, Any]) -> bool:
    """Preserve manual decisions; use an Ollaya Yes when available, otherwise legacy levels."""
    if candidate.get("decision") == "excluded":
        return False
    if candidate.get("pinned"):
        return candidate.get("decision") == "included"
    confidence = candidate.get("redactionConfidence")
    if confidence is not None:
        return True
    if candidate.get("category") == "WORD":
        return False
    if candidate.get("scoreStatus") == "complete":
        return False
    return 2 <= int(candidate.get("level", 10)) <= 10


def _valid_probability(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0.0 <= value <= 1.0
    )


def _score_cache_key(model: str, features: dict[str, Any]) -> str:
    request = json.dumps(
        {
            "method": SCORING_METHOD,
            "request_version": OLLAYA_REQUEST_VERSION,
            "model": model,
            "questions": _QUESTIONS,
            "state": features,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(request.encode("utf-8")).hexdigest()
