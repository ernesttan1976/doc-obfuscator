from __future__ import annotations

import hashlib
import re
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise

from rapidfuzz import fuzz

from .document_adapters import ParsedDocument

MAX_CANDIDATES_PER_VERSION = 1_000
MAX_CANDIDATES_PER_CATEGORY = 1_000
MAX_OCCURRENCES_PER_CANDIDATE = 2_000
MAX_PROPOSALS_PER_VERSION = 1_000
FUZZY_PROPOSAL_THRESHOLD = 88.0

# V1 defaults are a review-breadth aid, not a sensitivity score.
CANDIDATE_LEVELS = {
    "EMAIL": 1,
    "PHONE": 1,
    "DATE": 2,
    "IDENTIFIER": 2,
    "CAPITALIZED_PHRASE": 5,
    "MANUAL": 1,
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
                "level": CANDIDATE_LEVELS[str(record["category"])],
                "source": "manual" if record["manual"] else "pattern",
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
                    "reason": f"RapidFuzz spelling/format similarity ({score:.1f}%)",
                    "status": "proposed",
                    "confirmed": False,
                }
            )
            if len(proposals) >= MAX_PROPOSALS_PER_VERSION:
                return proposals
    return proposals


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
) -> None:
    normalized = " ".join(term.split()).casefold()
    if not normalized:
        return
    current = collected.get(normalized)
    if current is None:
        if category_counts.get(category, 0) >= MAX_CANDIDATES_PER_CATEGORY and category != "MANUAL":
            return
        current = {
            "term": term,
            "category": category,
            "occurrences": [],
            "occurrenceCount": 0,
            "manual": category == "MANUAL",
        }
        collected[normalized] = current
        category_counts[category] = category_counts.get(category, 0) + 1
    elif _category_priority(category) < _category_priority(str(current["category"])):
        previous_category = str(current["category"])
        current["category"] = category
        category_counts[previous_category] -= 1
        category_counts[category] = category_counts.get(category, 0) + 1
    current["manual"] = bool(current["manual"]) or category == "MANUAL"
    occurrences = current["occurrences"]
    assert isinstance(occurrences, list)
    occurrence = {"location": location, "start": start, "end": end}
    if occurrence in occurrences:
        return
    current["occurrenceCount"] = int(current["occurrenceCount"]) + 1
    if len(occurrences) < MAX_OCCURRENCES_PER_CANDIDATE:
        occurrences.append(occurrence)


def _category_priority(category: str) -> int:
    return {"EMAIL": 0, "PHONE": 0, "DATE": 1, "IDENTIFIER": 1, "MANUAL": 0, "CAPITALIZED_PHRASE": 2}.get(category, 3)


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
            if all(text[left.end() : right.start()].isspace() for left, right in pairwise(phrase)):
                yield first.start(), phrase[-1].end()


def _overlaps_date(text: str, start: int, end: int) -> bool:
    return any(pattern.search(text, start, end) for pattern in (_MONTH_DATE, _NUMERIC_DATE))
