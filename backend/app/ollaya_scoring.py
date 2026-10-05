from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import shutil
import subprocess
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from copy import deepcopy
from typing import Any

from .candidate_engine import CandidateBlock

SCORING_METHOD = "ollaya_yes_no_v3"
SCORING_MODEL = "von:1.1"
MAX_CONTEXT_CHARS = 192
MAX_CONTEXT_WINDOW_CHARS = 192
DEFAULT_TIMEOUT_SECONDS = 60
MAX_SCORE_CACHE_ENTRIES = 2048

_QUESTIONS = {
    "is_identifier": {
        "type": "choice",
        "instructions": "Does the candidate identify a named entity in context?",
        "criteria": {
            "Yes": "It identifies a person, organization, project, location, system, codeword, or other named entity.",
            "No": "It does not identify a named entity.",
        },
    },
    "has_operational_significance": {
        "type": "choice",
        "instructions": "Does the candidate identify an operational concept in context?",
        "criteria": {
            "Yes": "It identifies an activity, capability, vulnerability, plan, or resource.",
            "No": "It does not identify an operational concept.",
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


def build_ollaya_scoring_input(
    candidate: dict[str, Any],
    text_blocks: Sequence[CandidateBlock],
    *,
    max_context_chars: int = MAX_CONTEXT_CHARS,
) -> dict[str, Any]:
    """Build a compact, transient payload with one bounded occurrence context."""
    term = str(candidate.get("term", ""))
    block_by_location = {block.location: block.text for block in text_blocks}
    remaining = max(0, min(max_context_chars, MAX_CONTEXT_CHARS))
    for occurrence in candidate.get("occurrences", []):
        if remaining <= 0:
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
        if not snippet:
            continue
        return {"candidate": term, "context": snippet[:remaining]}
    return {"candidate": term}


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
                result = score_result_from_signals(validate_ollaya_response(response))
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
        return response

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


def _score_cache_key(model: str, features: dict[str, Any]) -> str:
    request = json.dumps(
        {
            "method": SCORING_METHOD,
            "model": model,
            "questions": _QUESTIONS,
            "state": features,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(request.encode("utf-8")).hexdigest()
