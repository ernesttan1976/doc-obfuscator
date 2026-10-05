# Ollaya Yes/No Redaction Scoring Plan

**Status:** core backend/UI integration implemented with local Ollaya CLI and `von:1.1`; cancellation and quality/calibration evaluation remain pending.
**Product:** Blot — Local Document Obfuscation  
**Related documents:** [`IMPLEMENTATION_PLAN.md`](./IMPLEMENTATION_PLAN.md), [`PRD Obfuscation App.md`](./PRD%20Obfuscation%20App.md)

## Goal

Use Ollaya's local decision model to answer two semantic yes/no questions about each already-extracted candidate term. If either answer is Yes, use the higher Yes-confidence as the candidate's **Redaction Confidence**. Convert that confidence into the specified **Review Priority** from 1/10 to 10/10. Deterministic extraction data supplies context to Ollaya but is not an additional scoring signal.

Blot estimates whether the user is likely to want a term obfuscated **in this document and context**. It does not determine whether a term is formally classified, confidential, or safe to disclose. Confidence and priority are review aids, not guarantees that every sensitive detail has been found.

## Current Blot baseline

- Candidate discovery is local: deterministic patterns and capitalized phrases, optional local GLiNER entity suggestions, and manual phrase selection.
- RapidFuzz and optional local MiniLM produce unconfirmed similarity proposals. Similarity alone never creates a confirmed replacement group.
- Candidate decisions and the graph are encrypted and scoped to a document version. Manual Include/Exclude decisions are pinned.
- The UI's current 1–10 candidate levels are heuristic defaults (email/phone/manual: 1; dates/identifiers: 2; capitalized phrases: 5). They are not calibrated probabilities.
- Ollaya scoring is implemented as a new local decision layer; it does not replace extraction or change graph membership.

## Proposed pipeline

```text
Supported document text
        ↓
Existing deterministic / optional NER / manual candidate extraction
        ↓
Candidate and local context features (bounded and transient)
        ↓
Ollaya yes/no semantic questions
        ↓
Keep each answer and Yes probability
        ↓
Highest affirmative Redaction Confidence
        ↓
Confidence-based review priority + pinned Include/Exclude
        ↓
User review and correction
        ↓
Encrypted, document-scoped user decisions
```

Candidate extraction and similarity grouping remain separate from this scoring step. The score ranks whether to suggest obfuscation; it does not assert that two terms are interchangeable and does not authorize group-wide replacement.

## Ollaya signal questions

Ask narrow, candidate-specific questions using only the minimum term/context needed. Use Ollaya's typed choice interface with `Yes` and `No` choices; preserve the model's probability for each choice rather than treating its selected label as certainty. The exact prompt wording and model must be fixed/versioned before release.

| Signal | Yes means | Score use |
| --- | --- | --- |
| `is_identifier` | Does the term identify a person, organization, project, unit, location, system, codeword, or other named entity in this context? | Redaction-positive |
| `has_operational_significance` | Does the term name or distinguish an operational activity, capability, vulnerability, plan, or resource in this context? | Redaction-positive |

Do not ask Ollaya to label a term “confidential.” The semantic signals are exactly these two; frequency, capitalization, extraction labels, and nearby entities are supporting input facts, not additional signals in the score.

Represent each question using Ollaya's returned `p_i = P(Yes)` value. Treat it as a model-reported probability estimate, not as calibrated confidence until validated. A missing answer, service error, or malformed response is **unknown**, not an implicit No. Use the returned Yes probabilities in scoring.

## Intermediate input builder

Use a deterministic backend function named `build_ollaya_scoring_input(...)` to construct the bounded, privacy-conscious payload for Ollaya. It lives in `backend/app/ollaya_scoring.py` and is called from `ProjectService.analyze_document_candidates` after candidate and occurrence extraction. This function does not make the semantic decision or calculate confidence/priority; it packages evidence from Blot's existing extraction for the two Ollaya questions.

Proposed interface:

```python
def build_ollaya_scoring_input(
    candidate: Candidate,
    text_blocks: Sequence[TextBlock],
    candidates: Sequence[Candidate],
    *,
    max_context_chars: int = 512,
) -> OllayaScoringInput:
    ...
```

Responsibilities:

1. Locate the candidate's occurrences in supported text blocks and gather a small, sentence-clipped context window for each occurrence (deduplicate repeated contexts and cap the total payload).
2. Include the candidate term and compact deterministic facts Blot already knows: occurrence count, candidate source (pattern/NER/manual), any available NER label, capitalization/shape, and types of nearby extracted candidates. Do not compute or inject extra sensitivity labels.
3. Omit unsupported/empty fields rather than turning them into negative answers. Never include the full document, unrelated text blocks, embeddings, user history, or the encrypted graph/map.
4. Return a typed payload with a schema version so the prompt and inference behavior can be tested and versioned. Context is transient and must not be logged or persisted.

Example payload:

```json
{
  "schemaVersion": "blot_ollaya_input_v1",
  "candidate": "Project Falcon",
  "occurrenceCount": 2,
  "extractionSources": ["capitalized_phrase", "ner"],
  "entityLabels": ["project"],
  "surfaceFacts": {"capitalized": true, "tokenCount": 2},
  "nearbyEntityTypes": ["date", "location"],
  "contextSnippets": ["... briefing for Project Falcon begins ..."]
}
```

The Ollaya adapter consumes this payload and asks the two questions from the table. Keep the input builder, Ollaya adapter, and final score function separate so each can be unit-tested independently.

## Confidence and priority

For each Ollaya question, retain the selected Yes/No answer and `probabilityYes`. A candidate receives a Redaction Confidence only if at least one question is answered Yes. When both are Yes, take the higher Yes probability; when only one is Yes, use that question's probability. If both are No, do not suggest redaction and leave Review Priority unset. If a question is unavailable, evaluate the available answer; if no affirmative answer is available, leave priority unset and use the existing non-Ollaya fallback behavior.

Implement the rule as two pure backend functions:

```python
def calculate_redaction_confidence(signals: Sequence[SignalResult]) -> float | None:
    affirmative = [s.probability_yes for s in signals if s.answer == "Yes"]
    return max(affirmative) if affirmative else None

def confidence_to_priority(confidence: float | None) -> int | None:
    ...  # exact bins below; validate confidence is in [0, 1]
```

Map confidence to priority exactly as follows. Bounds are lower-inclusive and upper-exclusive, except the final range includes 1.00. A priority of **10/10 is the highest review priority** and **1/10 the lowest**, following the requested mapping even though the numeric priority decreases as confidence increases.

| Redaction Confidence | Review Priority |
| --- | ---: |
| 0.50 ≤ confidence < 0.55 | 10/10 |
| 0.55 ≤ confidence < 0.60 | 9/10 |
| 0.60 ≤ confidence < 0.65 | 8/10 |
| 0.65 ≤ confidence < 0.70 | 7/10 |
| 0.70 ≤ confidence < 0.75 | 6/10 |
| 0.75 ≤ confidence < 0.80 | 5/10 |
| 0.80 ≤ confidence < 0.85 | 4/10 |
| 0.85 ≤ confidence < 0.90 | 3/10 |
| 0.90 ≤ confidence < 0.95 | 2/10 |
| 0.95 ≤ confidence ≤ 1.00 | 1/10 |

Confidence below 0.50 does not enter a priority band; under a two-choice Yes/No response, it should normally correspond to a No answer. Validate answer/probability consistency and treat malformed or contradictory responses as unavailable rather than assigning a priority. The Review Priority is an ordinal bucket, not a calibrated probability. The underlying Redaction Confidence remains Ollaya's model-reported Yes probability and must not be described as calibrated until validated.

Use Review Priority to order unpinned candidates from 10 (review first) down to 1. The existing 1–10 sensitivity slider should not re-run Ollaya or modify confidence/priority. Its interaction with priority—such as filtering by a minimum priority—must be specified separately; do not retain the earlier likelihood-threshold anchors. Manual decisions stay authoritative:

```text
Excluded/pinned → do not suggest redaction
Included/pinned → suggest redaction regardless of Ollaya answer/priority
Otherwise      → suggest only if at least one Ollaya answer is Yes;
                  order by Review Priority (10 first)
```

Scores, priorities, and suggestions must never confirm similarity edges or expand a replacement group. Explicit Include/Exclude decisions remain pinned across re-analysis and slider changes.

## User decisions and feedback

- A user's Include/Exclude action is authoritative for that candidate in its current document/version and overrides the score and slider.
- Persist decisions in the existing encrypted project graph. They remain scoped to a document version unless a separate explicit cross-document feature is designed later.
- Use explicit user decisions for evaluation/calibration only; do not add learned user-pattern signals to the score in this plan.
- Do not treat no action as rejection or approval. Similarity groups still require explicit confirmation.
- Manual Include/Exclude decisions remain pinned across re-analysis and slider changes.

## Local integration and privacy

1. Add an Ollaya adapter behind a small backend interface such as `score_candidate(features)`. Keep Ollaya transport/model-specific code out of candidate extraction, graph persistence, and UI logic.
2. Before committing to an endpoint, verify Ollaya's supported local invocation/API, model identifiers, response schema, installation/runtime requirements, model licensing, and resource use. Agent-host MCP tools are not themselves a product runtime dependency; Blot must invoke a locally available Ollaya runtime through a supported local interface.
3. Keep inference local and offline after any explicit model acquisition. Do not send document text, terms, contexts, embeddings, feedback, or graph data to a hosted Ollaya endpoint. Bind any service communication to loopback and apply the existing local-app request protections.
4. Pass the bounded candidate payload to the local Ollaya adapter. If several snippets are included, ask Ollaya to judge the candidate across those snippets and return one probability per question; do not add any other score signals.
5. Log one backend call record per candidate with only the model ID, outcome, duration, and valid-signal count. Never log terms, prompts, raw contexts, or raw model responses. Persist only what is required for reproducibility (confidence, priority, scoring version, signal answers/probabilities, and decision evidence) inside the encrypted graph. Context text is transient and is not persisted.
6. If the Ollaya runtime/model is absent, unavailable, or fails, continue with deterministic baseline scores/tiers and display a non-blocking status. Never block import, preview, or export on inference availability. Do not represent fallback scores as Ollaya scores.
7. Model downloads, if required, must be explicit, pinned, integrity checked, cancellable, and disclosed, following the existing optional GLiNER/MiniLM acquisition pattern. Do not add an automatic network dependency.

## Candidate result contract

Extend each candidate result with explicit, versioned fields along these lines:

```json
{
  "redactionConfidence": 0.93,
  "reviewPriority": 2,
  "scoringMethod": "ollaya_yes_no_v1",
  "scoringModel": "<verified-local-model-id>",
  "signals": {
    "isIdentifier": {"answer": "Yes", "probabilityYes": 0.93},
    "hasOperationalSignificance": {"answer": "Yes", "probabilityYes": 0.79}
  },
  "reasons": ["Named project identifier", "Operational context"],
  "scoreStatus": "complete"
}
```

The local model ID is pinned to `von:1.1`. The adapter validates probability ranges, missing keys, timeouts, malformed responses, and model/version metadata. It does not allow an Ollaya response to mutate Include/Exclude decisions, candidate identity, occurrences, group membership, or replacement mappings.

## UI changes

- Label the model result **Redaction confidence** and the derived bucket **Review Priority**; avoid “confidentiality probability.”
- Show the confidence, priority, a short “Why suggested” explanation, and the two Ollaya answers/probabilities. Distinguish semantic judgments from deterministic input facts and explicit user decisions.
- Order unpinned candidates by descending Review Priority (10 first). Keep the existing slider's filtering semantics separate until its interaction with priority is specified.
- Keep Include/Exclude and Undo available at all levels. Make pinned state visually distinct from Ollaya suggestions.
- Show a clear local-model unavailable/fallback indicator without implying inference completed.
- Scoring currently runs automatically with candidate analysis; running candidate analysis again re-runs scoring. Cancellation for an in-flight scoring pass remains to be implemented.

## Implementation sequence

### Phase 0 — Validate Ollaya runtime

- Identify the supported local API/CLI/library for this app and test typed Yes/No decisions with probability output.
- Confirm candidate-size limits, request timeouts, model distribution/license, offline behavior, and CPU/memory cost.
- Compare available decision models on a small synthetic prompt set; select and pin a version only after quality and operational checks.
- Gate further work if the runtime cannot guarantee local-only inference or typed probability output.

### Phase 1 — Scoring specification and fixtures

- Finalize question wording, input schema, max-of-affirmative-confidence rule, exact priority-bin boundaries, missing-signal behavior, and fallback rules.
- Create a synthetic dataset with different contexts for the same terms, identifier examples, operational references, and examples that are neither.
- Define expected signal behavior and review targets; avoid sensitive production documents.

### Phase 2 — Backend adapter and scoring

- Add the Ollaya client/adapter and a pure scoring function with explicit scoring-version metadata.
- Integrate after candidate extraction and before candidate results are returned/persisted; reuse bounded local contexts, with no external requests.
- Add timeout, cancellation, unavailable-model, invalid-output, partial-question, and deterministic-fallback paths.
- Keep old tier fields temporarily for migration/UI compatibility; do not reinterpret the existing heuristic 1–10 values as probabilities.

### Phase 3 — Encrypted graph, thresholding, and review UI

- Extend encrypted candidate records with confidence, priority, score version, signal answers/probabilities, and model status; avoid persisting context text.
- Order candidate review by priority and ensure pinned user decisions override automatic suggestions in both frontend and backend/export preview calculations.
- Add reasons/signal inspection and model-availability status to candidate review.
- Keep similarity proposals unconfirmed and unrelated to scores.

### Phase 4 — Evaluation and calibration

- Use explicit Include/Exclude decisions as reviewed evaluation labels; do not feed them back as a third score signal.
- Use synthetic data and opt-in, local review telemetry only if separately designed; never upload user examples.
- Assess probability calibration, false-positive burden, and missed-candidate behavior before changing the priority bins or defaults.

## Verification and acceptance criteria

### Unit and contract tests

- Each returned answer and Yes probability is validated; unavailable answers are not treated as No.
- When one or both questions answer Yes, confidence equals the maximum `probabilityYes` among the affirmative answers; when both answer No, no Ollaya priority is assigned.
- Every confidence boundary maps to the specified priority, including exact boundaries and 1.00.
- Scores remain bounded and deterministic for fixed Ollaya probabilities and a scoring version.
- Changing the slider does not call Ollaya or mutate confidence/priority; its filtering behavior is tested once specified.
- Pinned Include and Exclude override automatic suggestions; groups stay unconfirmed unless the user confirms them.
- Malformed output, timeouts, model absence, and cancellation result in a clearly identified fallback without failing document processing.

### Integration/privacy tests

- Exercise local Ollaya through the verified supported interface with synthetic candidate/context fixtures.
- Assert there is no non-loopback Ollaya request and no external transmission of candidate text or context.
- Assert logs contain neither terms nor context; encrypted graph state contains no plaintext term/context when inspected at rest.
- Assert scoring results remain version-scoped and never alter occurrences, groups, or placeholder maps.
- Verify behavior with Ollaya installed and unavailable, with optional NER/MiniLM present or absent.

### Product acceptance

- The same term can score differently in two synthetic contexts, demonstrating context-dependent decisions.
- The review UI explains major score contributors and clearly distinguishes inferred signals from explicit user choices.
- A user can include/exclude a suggestion; those decisions persist across slider changes and repeated analysis.
- The app never presents Redaction Confidence or Review Priority as official classification or a guarantee of complete discovery.
- Quality, latency, memory, and calibration targets are agreed from measurements before release claims/defaults are finalized.

## Implementation decisions and remaining evaluation

1. **Resolved:** use the supported local `ollaya run` CLI. Blot checks `ollaya list` for the configured model before running inference, passes candidate JSON on stdin, and does not download Ollaya models.
2. **Resolved for implementation:** use `von:1.1`, which returns typed Yes/No choices and per-choice probabilities. Runtime/resource targets and probability quality still need evaluation.
3. **Resolved:** score every extracted candidate automatically before results are returned and persisted.
4. **Resolved:** the existing obfuscation-level slider keeps its current filtering behavior; Ollaya Review Priority is shown and used for review ordering, not as a slider input.
5. Calibration, false-positive burden, and acceptance thresholds remain open for evaluation.
6. **Resolved:** preserve the existing heuristic level for filtering and fallback; expose Ollaya confidence/priority as separate fields.

The integration lives in `backend/app/ollaya_scoring.py`; `ProjectService.analyze_document_candidates` invokes it for every candidate. Ollaya input is transient and bounded, while only signal answers/probabilities and derived score metadata are stored in the encrypted, version-scoped graph. Missing CLI/model, timeout, or malformed output uses the existing heuristic behavior and displays an unavailable-scoring notice. An explicit in-flight cancel control and quality/calibration acceptance remain follow-up work.

## Decision summary

Blot should use Ollaya as a local semantic signal evaluator, not as an entity extractor or an opaque confidentiality classifier. `build_ollaya_scoring_input(...)` packages bounded candidate context and existing extraction facts; Ollaya returns answers/probabilities for `is_identifier` and `has_operational_significance`; `calculate_redaction_confidence(...)` selects the highest confidence among affirmative answers; `confidence_to_priority(...)` maps it to the specified 1–10 review bucket; and explicit user decisions remain authoritative. Locality, encrypted graph storage, user review, and non-blocking deterministic fallback preserve Blot's existing privacy and workflow invariants.
