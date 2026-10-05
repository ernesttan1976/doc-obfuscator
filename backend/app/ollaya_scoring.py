from __future__ import annotations

import json
import logging
import math
import re
import shutil
import subprocess
import time
from bisect import bisect_left
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from .candidate_engine import CandidateBlock

SCORING_METHOD = "ollaya_yes_no_v1"
SCORING_MODEL = "von:1.1"
MAX_CONTEXT_CHARS = 512
MAX_CONTEXT_SNIPPETS = 3
MAX_CONTEXT_WINDOW_CHARS = 192
DEFAULT_TIMEOUT_SECONDS = 60

_QUESTIONS = {
    "is_identifier": {
        "type": "choice",
        "instructions": (
            "Does the term identify a person, organization, project, unit, location, system, "
            "codeword, or other named entity in this context?"
        ),
        "criteria": {
            "Yes": "Yes, the term identifies one of these in the supplied context.",
            "No": "No, the term does not identify one of these in the supplied context.",
        },
    },
    "has_operational_significance": {
        "type": "choice",
        "instructions": (
            "Does the term name or distinguish an operational activity, capability, "
            "vulnerability, plan, or resource in this context?"
        ),
        "criteria": {
            "Yes": "Yes, the term has operational significance in the supplied context.",
            "No": "No, the term does not have operational significance in the supplied context.",
        },
    },
}

_SENTENCE_BOUNDARY = re.compile(r"[.!?;\n]")
_SIGNAL_NAMES = {
    "is_identifier": "isIdentifier",
    "has_operational_significance": "hasOperationalSignificance",
}
_SIGNAL_REASONS = {
    "is_identifier": "Identifies a named entity in context",
    "has_operational_significance": "Has operational significance in context",
}
_LOGGER = logging.getLogger(__name__)


class OllayaScoringError(Exception):
    """The local Ollaya CLI or its response is unavailable or invalid."""


class CandidateOccurrenceIndex:
    """Spatial index for bounded lookup of nearby candidate categories."""

    def __init__(self, candidates: Sequence[dict[str, Any]]) -> None:
        self._by_location: dict[str, list[tuple[int, str, str]]] = {}
        self._starts_by_location: dict[str, list[int]] = {}
        for candidate in candidates:
            candidate_id = str(candidate.get("id", ""))
            category = str(candidate.get("category", "")).lower()
            if not candidate_id or not category:
                continue
            indexed_occurrences = 0
            for occurrence in candidate.get("occurrences", []):
                if indexed_occurrences >= MAX_CONTEXT_SNIPPETS:
                    break
                if not isinstance(occurrence, dict):
                    continue
                location = occurrence.get("location")
                start = occurrence.get("start")
                if isinstance(location, str) and isinstance(start, int):
                    self._by_location.setdefault(location, []).append((start, candidate_id, category))
                    indexed_occurrences += 1
        for entries in self._by_location.values():
            entries.sort()
        self._starts_by_location = {
            location: [entry[0] for entry in entries]
            for location, entries in self._by_location.items()
        }

    def nearby_types(self, candidate_id: str, location: str, start: int) -> set[str]:
        entries = self._by_location.get(location, [])
        starts = self._starts_by_location.get(location, [])
        index = bisect_left(starts, start - MAX_CONTEXT_WINDOW_CHARS)
        types: set[str] = set()
        for other_start, other_id, category in entries[index : index + 100]:
            if other_start > start + MAX_CONTEXT_WINDOW_CHARS:
                break
            if other_id != candidate_id:
                types.add(category)
        return types


def build_ollaya_scoring_input(
    candidate: dict[str, Any],
    text_blocks: Sequence[CandidateBlock],
    candidates: Sequence[dict[str, Any]],
    *,
    max_context_chars: int = MAX_CONTEXT_CHARS,
    occurrence_index: CandidateOccurrenceIndex | None = None,
) -> dict[str, Any]:
    """Build a bounded, transient candidate payload without unrelated document text."""
    term = str(candidate.get("term", ""))
    block_by_location = {block.location: block.text for block in text_blocks}
    contexts: list[str] = []
    index = occurrence_index or CandidateOccurrenceIndex(candidates)
    nearby_types: set[str] = set()
    remaining = max(0, min(max_context_chars, MAX_CONTEXT_CHARS))
    for occurrence in candidate.get("occurrences", []):
        if remaining <= 0 or len(contexts) >= MAX_CONTEXT_SNIPPETS:
            break
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
        if not snippet or snippet in contexts:
            continue
        snippet = snippet[:remaining]
        contexts.append(snippet)
        remaining -= len(snippet)

        nearby_types.update(index.nearby_types(str(candidate.get("id", "")), location, start))

    payload: dict[str, Any] = {
        "schemaVersion": "blot_ollaya_input_v1",
        "candidate": term,
        "occurrenceCount": max(0, int(candidate.get("occurrenceCount", 0))),
        "extractionSources": [str(candidate.get("source", "pattern"))],
        "surfaceFacts": {
            "capitalized": bool(term) and term[0].isupper(),
            "tokenCount": len(term.split()),
        },
    }
    entity_labels = candidate.get("nerLabels")
    if isinstance(entity_labels, list) and entity_labels:
        payload["entityLabels"] = [str(label) for label in entity_labels[:8] if str(label)]
    if nearby_types:
        payload["nearbyEntityTypes"] = sorted(nearby_types)[:12]
    if contexts:
        payload["contextSnippets"] = contexts
    return payload


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


def validate_ollaya_response(response: Any) -> dict[str, dict[str, str | float]]:
    """Keep individually valid typed signals; malformed or contradictory ones are unavailable."""
    if not isinstance(response, dict) or response.get("model") != SCORING_MODEL:
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


def score_result_from_signals(signals: dict[str, dict[str, str | float]]) -> dict[str, Any]:
    affirmative_sources = [
        source_name
        for source_name, public_name in _SIGNAL_NAMES.items()
        if signals.get(public_name, {}).get("answer") == "Yes"
    ]
    confidence = calculate_redaction_confidence(signals.values())
    priority = confidence_to_priority(confidence)
    valid_signal_count = len(signals)
    reasons = [_SIGNAL_REASONS[name] for name in affirmative_sources]
    return {
        "redactionConfidence": confidence,
        "reviewPriority": priority,
        "scoringMethod": SCORING_METHOD,
        "scoringModel": SCORING_MODEL,
        "signals": signals,
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
        run: Callable[..., Any] = subprocess.run,
    ) -> None:
        self.executable = executable
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._run = run
        self._model_checked = False

    def status(self) -> dict[str, Any]:
        executable = self.executable or shutil.which("ollaya")
        return {
            "configured": bool(executable),
            "model": self.model,
            "interface": "local-cli",
        }

    def score_candidate(self, features: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        outcome = "unavailable"
        signal_count = 0
        try:
            result = self._score_candidate(features)
            outcome = str(result.get("scoreStatus", "unavailable"))
            signals = result.get("signals")
            signal_count = len(signals) if isinstance(signals, dict) else 0
            return result
        finally:
            _LOGGER.log(
                logging.WARNING if outcome == "unavailable" else logging.INFO,
                "Ollaya candidate scoring call finished",
                extra={
                    "ollaya_event": "candidate_scoring_call",
                    "ollaya_model": self.model,
                    "ollaya_outcome": outcome,
                    "ollaya_duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    "ollaya_signal_count": signal_count,
                },
            )

    def _score_candidate(self, features: dict[str, Any]) -> dict[str, Any]:
        executable = self.executable or shutil.which("ollaya")
        if not executable:
            raise OllayaScoringError("The local Ollaya CLI is not installed or is not on PATH.")
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
            completed = self._run(
                command,
                input=json.dumps(features, ensure_ascii=False, separators=(",", ":")),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
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
        return score_result_from_signals(validate_ollaya_response(response))

    def _ensure_model_installed(self, executable: str) -> None:
        if self._model_checked:
            return
        try:
            completed = self._run(
                [executable, "list"],
                capture_output=True,
                text=True,
                timeout=min(self.timeout_seconds, 10),
                check=False,
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


def unavailable_score() -> dict[str, Any]:
    return {
        "redactionConfidence": None,
        "reviewPriority": None,
        "scoringMethod": SCORING_METHOD,
        "scoringModel": None,
        "signals": {},
        "reasons": [],
        "scoreStatus": "unavailable",
    }


def should_auto_suggest(candidate: dict[str, Any]) -> bool:
    """Preserve manual decisions; use an Ollaya Yes when available, otherwise legacy levels."""
    if candidate.get("decision") == "excluded":
        return False
    if candidate.get("pinned"):
        return candidate.get("decision") == "included"
    confidence = candidate.get("redactionConfidence")
    if confidence is not None:
        return True
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
