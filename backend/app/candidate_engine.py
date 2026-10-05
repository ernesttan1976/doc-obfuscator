from __future__ import annotations

import hashlib
import heapq
import math
import re
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from itertools import pairwise

from rapidfuzz import fuzz

from .document_adapters import ParsedDocument

MAX_CANDIDATES_PER_VERSION = 1_000
MAX_CANDIDATES_PER_CATEGORY = 1_000
MAX_OCCURRENCES_PER_CANDIDATE = 2_000
MAX_PROPOSALS_PER_VERSION = 1_000
FUZZY_PROPOSAL_THRESHOLD = 88.0
CONTEXTUAL_PROPOSAL_THRESHOLD = 0.72
MAX_CONTEXTS_PER_CANDIDATE = 3
MAX_CONTEXT_WINDOW_CHARS = 192

# Priorities run from 2 (most sensitive) to 10 (least sensitive). These are
# heuristics for filtering, not a guarantee that every occurrence was found.
CANDIDATE_PRIORITY_LEVELS = {
    "EMAIL": 2,
    "PHONE": 3,
    "DATE": 10,
    "IDENTIFIER": 4,
    "CAPITALIZED_PHRASE": 8,
    "MANUAL": 2,
}
NER_LABEL_PRIORITY_LEVELS = {
    "PERSON": 2,
    "EMAIL": 2,
    "PHONE": 3,
    "LOCATION": 4,
    "ADDRESS": 4,
    "IDENTIFIER": 4,
    "ACCOUNT NUMBER": 4,
    "ORGANIZATION": 6,
    "ORG": 6,
    "COMPANY": 6,
    "DATE": 10,
    "TIME": 10,
}

_EMAIL = re.compile(
    r"(?<![\w.+-])[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+(?![\w-])",
    re.IGNORECASE,
)
_PHONE = re.compile(r"(?<!\w)\+?\d[\d ().-]{5,}\d(?!\w)")
_MONTH_DATE = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{2,4})?\b|"
    r"\b\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+\d{2,4}\b",
    re.IGNORECASE,
)
_NUMERIC_DATE = re.compile(
    r"\b(?:0?[1-9]|1[0-2])[/.-](?:0?[1-9]|[12]\d|3[01])(?:[/.-](?:\d{2}|\d{4}))\b"
)
_IDENTIFIER = re.compile(r"\b(?:[A-Z]{2,}[\w]*[-_/][A-Z0-9][A-Z0-9_-]*|[A-Z]{2,}\d{2,})\b")
_CAPITALIZED_TOKEN = re.compile(r"(?<![\w])(?:[A-Z][a-z]+(?:[’'-][A-Z]?[a-z]+)*|[A-Z]\.)(?![\w])")
_COMMON_SENTENCE_STARTERS = {"a", "an", "at", "contact", "for", "from", "in", "on", "owner", "please", "reference", "the", "to"}
_NORMALIZE = re.compile(r"[^\w]+", re.UNICODE)


@dataclass(frozen=True)
class CandidateBlock:
    location: str
    text: str


class CandidateError(Exception):
    """A candidate request cannot be applied to the selected document version."""


def blocks_for_document(parsed: ParsedDocument) -> tuple[CandidateBlock, ...]:
    """Expose editable text blocks with stable adapter locations for extraction."""
    if parsed.format == "CSV":
        return tuple(
            CandidateBlock(f"row:{row_index},column:{column_index}", value)
            for row_index, row in enumerate(parsed.rows, start=1)
            for column_index, value in enumerate(row, start=1)
            if value
        )
    if parsed.format == "XLSX":
        return tuple(
            CandidateBlock(f"sheet:{sheet.name},cell:{cell.location}", cell.text)
            for sheet in parsed.sheets
            for cell in sheet.cells
            if cell.text
        )
    if parsed.text is None:
        return ()
    if parsed.format in {"DOCX", "PPTX"}:
        return tuple(
            CandidateBlock(f"paragraph:{index}", paragraph)
            for index, paragraph in enumerate(parsed.text.splitlines())
            if paragraph
        )
    return (CandidateBlock("text", parsed.text),) if parsed.text else ()


def analyze_candidates(
    blocks: Iterable[CandidateBlock],
    document_id: str,
    version_id: str,
    existing_nodes: Iterable[dict[str, object]] = (),
    manual_terms: Iterable[str] = (),
    ner_entities: Iterable[dict[str, object]] = (),
    on_candidate: Callable[[dict[str, object]], None] | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Extract conservative local candidates and unconfirmed fuzzy proposals."""
    blocks = tuple(blocks)
    preserved = {
        str(node.get("term", "")).casefold(): node
        for node in existing_nodes
        if node.get("documentId") == document_id and node.get("versionId") == version_id
    }
    collected: dict[str, dict[str, object]] = {}
    category_counts: dict[str, int] = {}

    def report_candidate(discovered: dict[str, object]) -> None:
        if on_candidate is None:
            return
        term = str(discovered["term"])
        category = str(discovered["category"])
        source = str(discovered["source"])
        location = str(discovered["location"])
        start = int(discovered["start"])
        end = int(discovered["end"])
        normalized_term = " ".join(term.split()).casefold()
        level = _priority_level(category, set(discovered["nerLabels"]), float(discovered["nerScore"]))
        on_candidate(
            {
                "id": _candidate_id(version_id, normalized_term),
                "documentId": document_id,
                "versionId": version_id,
                "term": term,
                "category": category,
                "level": level,
                "source": source,
                "nerLabels": discovered["nerLabels"],
                "nerScore": discovered["nerScore"],
                "occurrences": [{"location": location, "start": start, "end": end}],
                "occurrenceCount": discovered["occurrenceCount"],
                "occurrencesTruncated": False,
                "decision": "included" if source == "manual" else "suggested",
                "pinned": source == "manual",
                "scoreStatus": "scanning",
            }
        )

    for block in blocks:
        for category, pattern in (
            ("EMAIL", _EMAIL),
            ("PHONE", _PHONE),
            ("DATE", _MONTH_DATE),
            ("DATE", _NUMERIC_DATE),
            ("IDENTIFIER", _IDENTIFIER),
        ):
            for match in pattern.finditer(block.text):
                term = match.group(0).strip()
                if len(term) > 256:
                    continue
                if category == "PHONE" and not 7 <= sum(char.isdigit() for char in term) <= 15:
                    continue
                if category == "PHONE" and _overlaps_date(block.text, match.start(), match.end()):
                    continue
                _add_occurrence(
                    collected,
                    category_counts,
                    term,
                    category,
                    block.location,
                    match.start(),
                    match.end(),
                    on_candidate=report_candidate,
                )
        for start, end in _capitalized_phrase_matches(block.text):
            _add_occurrence(
                collected,
                category_counts,
                block.text[start:end],
                "CAPITALIZED_PHRASE",
                block.location,
                start,
                end,
                on_candidate=report_candidate,
            )

    block_text_by_location = {block.location: block.text for block in blocks}
    for entity in ner_entities:
        term = str(entity.get("text", "")).strip()
        location = str(entity.get("location", ""))
        start = entity.get("start")
        end = entity.get("end")
        label = str(entity.get("label", "entity")).strip().lower()
        score = entity.get("score", 0.0)
        if (
            not term
            or len(term) > 256
            or not isinstance(start, int)
            or not isinstance(end, int)
            or start < 0
            or end <= start
            or location not in block_text_by_location
            or block_text_by_location[location][start:end] != term
        ):
            continue
        _add_occurrence(
            collected,
            category_counts,
            term,
            "NER_ENTITY",
            location,
            start,
            end,
            source="ner",
            ner_label=label,
            ner_score=float(score),
            on_candidate=report_candidate,
        )

    for term in manual_terms:
        clean_term = " ".join(term.split())
        if not clean_term or len(clean_term) > 256 or any(ord(char) < 32 for char in clean_term):
            raise CandidateError("Manually selected phrases must contain 1–256 printable characters.")
        found = False
        for block in blocks:
            for match in re.finditer(re.escape(clean_term), block.text, re.IGNORECASE):
                found = True
                _add_occurrence(
                    collected,
                    category_counts,
                    match.group(0),
                    "MANUAL",
                    block.location,
                    match.start(),
                    match.end(),
                    on_candidate=report_candidate,
                )
        if not found:
            raise CandidateError("The selected phrase is not present in supported text for this version.")

    nodes: list[dict[str, object]] = []
    ordered_records = sorted(
        collected.items(),
        key=lambda item: (
            not bool(item[1]["manual"]),
            _category_priority(str(item[1]["category"])),
            item[0],
        ),
    )
    for normalized_term, record in ordered_records:
        previous = preserved.get(normalized_term, {})
        term = str(record["term"])
        node_id = _candidate_id(version_id, normalized_term)
        occurrences = record["occurrences"]
        assert isinstance(occurrences, list)
        nodes.append(
            {
                "id": node_id,
                "documentId": document_id,
                "versionId": version_id,
                "term": term,
                "category": record["category"],
                "level": _priority_level(
                    str(record["category"]),
                    record["nerLabels"],
                    float(record["nerScore"]),
                ),
                "source": (
                    "manual" if record["manual"] else "ner" if "ner" in record["sources"] else "pattern"
                ),
                "nerLabels": sorted(record["nerLabels"]),
                "nerScore": record["nerScore"],
                "occurrences": occurrences[:MAX_OCCURRENCES_PER_CANDIDATE],
                "occurrenceCount": record["occurrenceCount"],
                "occurrencesTruncated": record["occurrenceCount"] > MAX_OCCURRENCES_PER_CANDIDATE,
                "decision": previous.get("decision", "included" if record["manual"] else "suggested"),
                "pinned": bool(previous.get("pinned", record["manual"])),
            }
        )
        if len(nodes) >= MAX_CANDIDATES_PER_VERSION:
            break

    return nodes, propose_variants(nodes)


def propose_variants(nodes: list[dict[str, object]]) -> list[dict[str, object]]:
    """Suggest lexical variants; these edges never imply confirmed membership."""
    candidates = sorted(nodes, key=lambda node: str(node["id"]))[:MAX_CANDIDATES_PER_VERSION]
    proposals: list[dict[str, object]] = []
    for index, first in enumerate(candidates):
        first_term = str(first["term"])
        if len(first_term) < 4:
            continue
        for second in candidates[index + 1 :]:
            second_term = str(second["term"])
            if len(second_term) < 4 or first_term.casefold() == second_term.casefold():
                continue
            score = float(fuzz.ratio(first_term, second_term))
            normalized_score = float(fuzz.ratio(_normalize_term(first_term), _normalize_term(second_term)))
            score = max(score, normalized_score)
            if score < FUZZY_PROPOSAL_THRESHOLD:
                continue
            proposals.append(
                {
                    "id": hashlib.sha256(f"{first['id']}\0{second['id']}".encode()).hexdigest()[:24],
                    "sourceId": first["id"],
                    "targetId": second["id"],
                    "score": round(score, 1),
                    "method": "rapidfuzz",
                    "scores": {"rapidfuzz": round(score, 1)},
                    "reason": f"RapidFuzz spelling/format similarity ({score:.1f}%)",
                    "status": "proposed",
                    "confirmed": False,
                }
            )
            if len(proposals) >= MAX_PROPOSALS_PER_VERSION:
                return proposals
    return proposals


def propose_contextual_variants(
    nodes: list[dict[str, object]],
    blocks: Iterable[CandidateBlock],
    embed_contexts: Callable[[list[str]], list[list[float]]],
    *,
    threshold: float = CONTEXTUAL_PROPOSAL_THRESHOLD,
    similarity_matrix: Callable[[list[list[float]]], list[list[float]]] | None = None,
) -> list[dict[str, object]]:
    """Suggest unconfirmed pairs from locally embedded, mention-masked contexts."""
    contexts_by_id = _contexts_by_candidate(nodes, blocks)
    if len(contexts_by_id) < 2:
        return []

    centroids = _embed_candidate_contexts(contexts_by_id, embed_contexts)
    candidate_ids = sorted(centroids)
    vectors = [centroids[candidate_id] for candidate_id in candidate_ids]
    matrix = similarity_matrix(vectors) if similarity_matrix else None
    return _rank_contextual_pairs(candidate_ids, vectors, matrix, threshold)


def _contexts_by_candidate(
    nodes: list[dict[str, object]],
    blocks: Iterable[CandidateBlock],
) -> dict[str, list[str]]:
    block_text = {block.location: block.text for block in blocks}
    return {
        str(node["id"]): contexts
        for node in sorted(nodes, key=lambda item: str(item["id"]))[:MAX_CANDIDATES_PER_VERSION]
        if (contexts := _candidate_contexts(node, block_text))
    }


def _embed_candidate_contexts(
    contexts_by_id: dict[str, list[str]],
    embed_contexts: Callable[[list[str]], list[list[float]]],
) -> dict[str, list[float]]:
    owners = [candidate_id for candidate_id, contexts in contexts_by_id.items() for _ in contexts]
    texts = [context for contexts in contexts_by_id.values() for context in contexts]
    embeddings = embed_contexts(texts)
    _validate_embeddings(embeddings, len(texts))
    dimensions = len(embeddings[0])
    sums: dict[str, list[float]] = {}
    counts: dict[str, int] = {}
    for candidate_id, vector in zip(owners, embeddings, strict=True):
        total = sums.setdefault(candidate_id, [0.0] * dimensions)
        for index, value in enumerate(vector):
            total[index] += value
        counts[candidate_id] = counts.get(candidate_id, 0) + 1
    return {
        candidate_id: _normalize_vector([value / counts[candidate_id] for value in total])
        for candidate_id, total in sums.items()
    }


def _validate_embeddings(embeddings: list[list[float]], expected_count: int) -> None:
    if len(embeddings) != expected_count:
        raise CandidateError("The local contextual model returned an invalid embedding count.")
    dimensions = len(embeddings[0]) if embeddings else 0
    if dimensions == 0 or any(len(vector) != dimensions for vector in embeddings):
        raise CandidateError("The local contextual model returned invalid embeddings.")
    if any(not math.isfinite(value) for vector in embeddings for value in vector):
        raise CandidateError("The local contextual model returned invalid embeddings.")


def _rank_contextual_pairs(
    candidate_ids: list[str],
    vectors: list[list[float]],
    matrix: list[list[float]] | None,
    threshold: float,
) -> list[dict[str, object]]:
    _validate_similarity_matrix(matrix, len(candidate_ids))
    best_pairs: list[tuple[float, str, str]] = []
    for index, first_id in enumerate(candidate_ids):
        for second_index in range(index + 1, len(candidate_ids)):
            second_id = candidate_ids[second_index]
            pair = (
                _pair_similarity(vectors, matrix, index, second_index),
                first_id,
                second_id,
            )
            _keep_best_pair(best_pairs, pair, threshold)
    return [_contextual_proposal(pair) for pair in sorted(best_pairs, reverse=True)]


def _validate_similarity_matrix(matrix: list[list[float]] | None, candidate_count: int) -> None:
    if matrix is not None and (
        len(matrix) != candidate_count
        or any(len(row) != candidate_count for row in matrix)
    ):
        raise CandidateError("The local contextual model returned an invalid similarity matrix.")


def _pair_similarity(
    vectors: list[list[float]],
    matrix: list[list[float]] | None,
    first_index: int,
    second_index: int,
) -> float:
    if matrix is not None:
        return matrix[first_index][second_index]
    return sum(
        left * right
        for left, right in zip(vectors[first_index], vectors[second_index], strict=True)
    )


def _keep_best_pair(
    best_pairs: list[tuple[float, str, str]],
    pair: tuple[float, str, str],
    threshold: float,
) -> None:
    if pair[0] < threshold:
        return
    if len(best_pairs) < MAX_PROPOSALS_PER_VERSION:
        heapq.heappush(best_pairs, pair)
    elif pair > best_pairs[0]:
        heapq.heapreplace(best_pairs, pair)


def _contextual_proposal(pair: tuple[float, str, str]) -> dict[str, object]:
    score, first_id, second_id = pair
    percentage = round(max(0.0, min(1.0, score)) * 100, 1)
    return {
        "id": hashlib.sha256(f"minilm\0{first_id}\0{second_id}".encode()).hexdigest()[:24],
        "sourceId": first_id,
        "targetId": second_id,
        "score": percentage,
        "method": "minilm",
        "scores": {"minilm": percentage},
        "reason": f"MiniLM contextual similarity ({percentage:.1f}% cosine; candidate mentions masked)",
        "status": "proposed",
        "confirmed": False,
    }


def merge_proposals(*proposal_sets: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    """Combine evidence for a pair without duplicating its review edge."""
    merged: dict[tuple[str, str], dict[str, object]] = {}
    for proposals in proposal_sets:
        for proposal in proposals:
            pair = tuple(sorted((str(proposal["sourceId"]), str(proposal["targetId"]))))
            current = merged.get(pair)
            if current is None:
                merged[pair] = dict(proposal)
                continue
            current_scores = dict(current.get("scores", {}))
            current_scores.update(dict(proposal.get("scores", {})))
            methods = set(current.get("methods", [current.get("method", "unknown")]))
            methods.add(str(proposal.get("method", "unknown")))
            current["scores"] = current_scores
            current["methods"] = sorted(methods)
            current["method"] = "combined"
            current["score"] = max(float(value) for value in current_scores.values())
            if proposal.get("reason") not in str(current.get("reason", "")):
                current["reason"] = f"{current.get('reason', '')}; {proposal.get('reason', '')}"
    def proposal_rank(proposal: dict[str, object]) -> tuple[int, float, str]:
        methods = proposal.get("methods", [proposal.get("method", "unknown")])
        priority = 0 if len(methods) > 1 else 1 if "minilm" in methods else 2
        return (priority, -float(proposal.get("score", 0.0)), str(proposal["id"]))

    return sorted(merged.values(), key=proposal_rank)[:MAX_PROPOSALS_PER_VERSION]


def _candidate_contexts(node: dict[str, object], block_text: dict[str, str]) -> list[str]:
    raw_occurrences = node.get("occurrences", [])
    if not isinstance(raw_occurrences, list) or not raw_occurrences:
        return []
    contexts = []
    for occurrence in _sample_occurrences(raw_occurrences):
        context = _context_for_occurrence(occurrence, block_text)
        if context is not None:
            contexts.append(context)
    return contexts


def _sample_occurrences(occurrences: list[object]) -> list[object]:
    if len(occurrences) <= MAX_CONTEXTS_PER_CANDIDATE:
        return occurrences
    indices = {
        round(index * (len(occurrences) - 1) / (MAX_CONTEXTS_PER_CANDIDATE - 1))
        for index in range(MAX_CONTEXTS_PER_CANDIDATE)
    }
    return [occurrences[index] for index in sorted(indices)]


def _context_for_occurrence(occurrence: object, block_text: dict[str, str]) -> str | None:
    if not isinstance(occurrence, dict):
        return None
    text = block_text.get(str(occurrence.get("location", "")))
    start = occurrence.get("start")
    end = occurrence.get("end")
    if text is None or not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(text):
        return None
    prefix = _trim_context_left(text[max(0, start - MAX_CONTEXT_WINDOW_CHARS) : start])
    suffix = _trim_context_right(text[end : min(len(text), end + MAX_CONTEXT_WINDOW_CHARS)])
    if sum(char.isalpha() for char in f"{prefix}{suffix}") < 4:
        return None
    context = " ".join(f"{prefix} [ENTITY] {suffix}".split())
    return context[: MAX_CONTEXT_WINDOW_CHARS * 2 + 8] if context else None


def _trim_context_left(prefix: str) -> str:
    cut_points = [
        prefix.rfind(boundary) + len(boundary)
        for boundary in ("\n", ". ", "! ", "? ")
        if prefix.rfind(boundary) >= 0
    ]
    return prefix[max(cut_points) :] if cut_points else prefix


def _trim_context_right(suffix: str) -> str:
    cut_points = [
        position
        for boundary in ("\n", ". ", "! ", "? ")
        if (position := suffix.find(boundary)) >= 0
    ]
    return suffix[: min(cut_points)] if cut_points else suffix


def _normalize_vector(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0 or not math.isfinite(norm):
        raise CandidateError("The local contextual model returned invalid embeddings.")
    return [value / norm for value in vector]


def decide_candidate(nodes: list[dict[str, object]], candidate_id: str, decision: str) -> dict[str, object]:
    if decision not in {"included", "excluded", "suggested"}:
        raise CandidateError("Candidate decision must be included, excluded, or suggested.")
    for node in nodes:
        if node.get("id") == candidate_id:
            node["decision"] = decision
            node["pinned"] = decision in {"included", "excluded"}
            return node
    raise CandidateError("The selected candidate was not found in this document version.")


def _add_occurrence(
    collected: dict[str, dict[str, object]],
    category_counts: dict[str, int],
    term: str,
    category: str,
    location: str,
    start: int,
    end: int,
    *,
    source: str = "pattern",
    ner_label: str | None = None,
    ner_score: float = 0.0,
    on_candidate: Callable[[dict[str, object]], None] | None = None,
) -> None:
    normalized = " ".join(term.split()).casefold()
    if not normalized:
        return
    current = collected.get(normalized)
    is_new = current is None
    previous_category = str(current["category"]) if current is not None else category
    had_source = source in current["sources"] if current is not None else False
    if current is None:
        if category_counts.get(category, 0) >= MAX_CANDIDATES_PER_CATEGORY and category != "MANUAL":
            return
        current = {
            "term": term,
            "category": category,
            "occurrences": [],
            "occurrenceCount": 0,
            "manual": category == "MANUAL",
            "sources": set(),
            "nerLabels": set(),
            "nerScore": 0.0,
        }
        collected[normalized] = current
        category_counts[category] = category_counts.get(category, 0) + 1
    elif _category_priority(category) < _category_priority(str(current["category"])):
        previous_category = str(current["category"])
        current["category"] = category
        category_counts[previous_category] -= 1
        category_counts[category] = category_counts.get(category, 0) + 1
    current["manual"] = bool(current["manual"]) or category == "MANUAL"
    sources = current["sources"]
    assert isinstance(sources, set)
    sources.add(source)
    ner_labels = current["nerLabels"]
    assert isinstance(ner_labels, set)
    if ner_label:
        ner_labels.add(ner_label)
        current["nerScore"] = max(float(current["nerScore"]), ner_score)
    occurrences = current["occurrences"]
    assert isinstance(occurrences, list)
    occurrence = {"location": location, "start": start, "end": end}
    if occurrence in occurrences:
        return
    current["occurrenceCount"] = int(current["occurrenceCount"]) + 1
    if len(occurrences) < MAX_OCCURRENCES_PER_CANDIDATE:
        occurrences.append(occurrence)
    if on_candidate is not None and (is_new or previous_category != str(current["category"]) or not had_source):
        on_candidate(
            {
                "term": str(current["term"]),
                "category": str(current["category"]),
                "source": (
                    "manual" if current["manual"] else "ner" if "ner" in sources else "pattern"
                ),
                "location": location,
                "start": start,
                "end": end,
                "nerLabels": sorted(ner_labels),
                "nerScore": current["nerScore"],
                "occurrenceCount": current["occurrenceCount"],
            }
        )


def _category_priority(category: str) -> int:
    return {
        "EMAIL": 0,
        "PHONE": 0,
        "DATE": 1,
        "IDENTIFIER": 1,
        "MANUAL": 0,
        "NER_ENTITY": 2,
        "CAPITALIZED_PHRASE": 3,
    }.get(category, 4)


def _priority_level(category: str, ner_labels: set[str], ner_score: float) -> int:
    if category != "NER_ENTITY":
        return CANDIDATE_PRIORITY_LEVELS.get(category, 8)
    label_priorities = [
        NER_LABEL_PRIORITY_LEVELS.get(label.upper(), 8)
        for label in ner_labels
    ]
    base_priority = min(label_priorities, default=8)
    confidence_adjustment = round((max(0.0, min(1.0, ner_score)) - 0.5) * 2)
    return max(2, min(10, base_priority - confidence_adjustment))


def _candidate_id(version_id: str, normalized_term: str) -> str:
    return hashlib.sha256(f"{version_id}\0{normalized_term}".encode()).hexdigest()[:24]


def _normalize_term(term: str) -> str:
    return _NORMALIZE.sub("", term.casefold())


def _capitalized_phrase_matches(text: str) -> Iterable[tuple[int, int]]:
    window = deque(maxlen=4)
    for current in _CAPITALIZED_TOKEN.finditer(text):
        window.append(current)
        for size in range(2, len(window) + 1):
            phrase = list(window)[-size:]
            first = phrase[0]
            if first.group(0).casefold().rstrip(".") in _COMMON_SENTENCE_STARTERS:
                continue
            if all(not text[left.end() : right.start()].strip(" \t") for left, right in pairwise(phrase)):
                yield first.start(), phrase[-1].end()


def _overlaps_date(text: str, start: int, end: int) -> bool:
    return any(pattern.search(text, start, end) for pattern in (_MONTH_DATE, _NUMERIC_DATE))
