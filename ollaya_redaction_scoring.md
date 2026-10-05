# Ollaya Yes/No Redaction Scoring Plan

**Status:** proposed design; implementation and model validation not started.  
**Product:** Blot — Local Document Obfuscation  
**Related documents:** [`IMPLEMENTATION_PLAN.md`](./IMPLEMENTATION_PLAN.md), [`PRD Obfuscation App.md`](./PRD%20Obfuscation%20App.md)

## Goal

Use Ollaya's local decision model to answer a compact set of semantic yes/no questions about each already-extracted candidate term. Combine those answers with deterministic and user-learned evidence into an explainable **Redaction Likelihood** from 0.00 to 1.00. The existing 1–10 slider then applies a threshold to that score.

Blot estimates whether the user is likely to want a term obfuscated **in this document and context**. It does not determine whether a term is formally classified, confidential, or safe to disclose. The score is a review aid, not a guarantee that every sensitive detail has been found.

## Current Blot baseline

- Candidate discovery is local: deterministic patterns and capitalized phrases, optional local GLiNER entity suggestions, and manual phrase selection.
- RapidFuzz and optional local MiniLM produce unconfirmed similarity proposals. Similarity alone never creates a confirmed replacement group.
- Candidate decisions and the graph are encrypted and scoped to a document version. Manual Include/Exclude decisions are pinned.
- The UI's current 1–10 candidate levels are heuristic defaults (email/phone/manual: 1; dates/identifiers: 2; capitalized phrases: 5). They are not calibrated probabilities.
- The app currently has no Ollaya integration. Adding Ollaya is a new local decision layer; it must not replace extraction or silently change graph membership.

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
Combine Ollaya probabilities with deterministic and user-learned signals
        ↓
Redaction Likelihood 0.00–1.00 + reasons
        ↓
Sensitivity slider threshold + pinned Include/Exclude
        ↓
User review and correction
        ↓
Encrypted, scoped feedback for future scoring
```

Candidate extraction and similarity grouping remain separate from this scoring step. The score ranks whether to suggest obfuscation; it does not assert that two terms are interchangeable and does not authorize group-wide replacement.

## Ollaya signal questions

Ask narrow, candidate-specific questions using only the minimum term/context needed. Use Ollaya's typed choice interface with `Yes` and `No` choices; preserve the model's probability for each choice rather than treating its selected label as certainty. The exact prompt wording and model must be fixed/versioned before release.

| Signal | Yes means | Score use |
| --- | --- | --- |
| `is_identifier` | The term identifies a person, organization, project, unit, location, system, codeword, or other named entity in this context. | Redaction-positive |
| `has_operational_significance` | The term names or distinguishes an operational activity, capability, vulnerability, plan, or resource in this context. | Redaction-positive |
| `is_document_specific_or_uncommon` | The term appears to be a local/internal label or unusually specific term rather than a generic phrase. | Redaction-positive |
| `is_linked_to_other_sensitive_details` | Nearby extracted entities or details make the term more revealing in combination than alone. | Redaction-positive |
| `matches_user_sensitive_pattern` | The term/context resembles patterns the user has previously included for obfuscation in this project/profile. | Redaction-positive; only when such local feedback exists |
| `is_publicly_common` | The term is widely known and ordinary in the relevant context. | Redaction-negative |
| `masking_preserves_task_meaning` | Replacing the term with a placeholder is likely to preserve enough structure for an external LLM task. | Separate usability signal; not evidence of sensitivity |

The feature extractor can supply facts such as frequency, capitalization, entity type, occurrence count, nearby dates/locations, and graph similarity. These are supplied as compact structured facts, not as permission for Ollaya to inspect the whole document. Do not ask Ollaya to label a term “confidential.”

Represent each question using Ollaya's returned `p_i = P(Yes)` value. Treat it as a model-reported probability estimate, not as calibrated confidence until validated. A missing answer, service error, low-confidence answer, or unsupported context is **unknown**, not an implicit No. Store the selected label and returned probabilities for diagnostics only if useful; use probability values in scoring.

## Initial score proposal

Start with a simple, documented weighted model rather than asking Ollaya for one opaque overall percentage. Use positive signals whose weights total 1.0, then subtract a bounded penalty for public/common evidence:

```text
positive = 0.25 * p_is_identifier
         + 0.20 * p_has_operational_significance
         + 0.15 * p_is_document_specific_or_uncommon
         + 0.15 * p_is_linked_to_other_sensitive_details
         + 0.25 * p_matches_user_sensitive_pattern

negative = 0.15 * p_is_publicly_common
redaction_likelihood = clamp(positive - negative, 0, 1)
```

Omit unavailable signals and normalize the active positive weights to sum to 1.0 before applying the public-common deduction. If there is no user feedback, remove that feature and re-normalize the remaining positive weights; do not treat absence of feedback as a negative user signal. If all positive signals are unavailable, report the likelihood as unavailable and use the deterministic fallback rather than inventing a score. Keep `masking_preserves_task_meaning` separate from Redaction Likelihood and present it as supporting review context.

These weights are **starting hypotheses**, not calibrated truth. Keep scoring versioned and tune it only against synthetic/reviewed examples and explicit user corrections. Show a score as an estimate, not as a formal probability claim until reliability calibration has been measured. UI copy should initially say “Redaction likelihood” and “Estimated from signals,” with rounded display (for example, 0.86).

## Slider and decisions

Use slider level as a threshold selector; moving it must not rerun or alter the score:

| Blot level | Initial threshold: suggest redaction when score is at least |
| ---: | ---: |
| 1 | 0.95 |
| 3 | 0.80 |
| 5 | 0.60 |
| 7 | 0.40 |
| 10 | 0.10 |

Interpolate thresholds linearly between anchors and document the generated 1–10 table in code/tests. Treat this mapping as an initial profile for user validation. The effective suggestion rule is:

```text
Excluded/pinned → do not suggest redaction
Included/pinned → suggest redaction regardless of score or slider threshold
Otherwise      → suggest redaction when redaction_likelihood >= threshold(level)
```

Explicitly included terms remain included even when the slider moves; explicitly excluded terms remain excluded. Scores, threshold changes, and suggestions must never confirm similarity edges or expand a replacement group.

## User feedback and graph learning

- A user's Include/Exclude action is the authoritative label for that candidate in its current document/version.
- Record feedback in the existing encrypted project graph with appropriate scope: document-version decisions by default; cross-document learning only through an explicit project/profile choice.
- Learn reusable patterns from user feedback (such as entity type, feature combinations, or confirmed related terms), not only literal strings. Keep exact-term memory scoped and user-visible; never silently generalize a decision to an unconfirmed similarity group.
- Do not learn from automatic suggestions that the user has not reviewed. Do not treat no action as rejection or approval.
- Keep feedback-derived features separately inspectable so a user can understand why a later candidate received a higher score and can remove/reset learned preferences.
- Manual exclusions must take precedence over learned positive signals for the same candidate and remain pinned as they do today.

## Local integration and privacy

1. Add an Ollaya adapter behind a small backend interface such as `score_candidate(features)`. Keep Ollaya transport/model-specific code out of candidate extraction, graph persistence, and UI logic.
2. Before committing to an endpoint, verify Ollaya's supported local invocation/API, model identifiers, response schema, installation/runtime requirements, model licensing, and resource use. Agent-host MCP tools are not themselves a product runtime dependency; Blot must invoke a locally available Ollaya runtime through a supported local interface.
3. Keep inference local and offline after any explicit model acquisition. Do not send document text, terms, contexts, embeddings, feedback, or graph data to a hosted Ollaya endpoint. Bind any service communication to loopback and apply the existing local-app request protections.
4. Pass a candidate term plus a short, sentence-clipped, mention-aware context window or compact feature JSON. Bound and mask context consistently with the existing local model pipeline; process multiple bounded contexts only when needed and combine their evidence deterministically.
5. Do not log terms, prompts, raw contexts, or raw model responses. Persist only what is required for reproducibility (score/version, signal probabilities, and decision evidence) inside the encrypted graph. Context text is transient and is not persisted.
6. If the Ollaya runtime/model is absent, unavailable, or fails, continue with deterministic baseline scores/tiers and display a non-blocking status. Never block import, preview, or export on inference availability. Do not represent fallback scores as Ollaya scores.
7. Model downloads, if required, must be explicit, pinned, integrity checked, cancellable, and disclosed, following the existing optional GLiNER/MiniLM acquisition pattern. Do not add an automatic network dependency.

## Candidate result contract

Extend each candidate result with explicit, versioned fields along these lines:

```json
{
  "redactionLikelihood": 0.86,
  "scoringMethod": "ollaya_yes_no_v1",
  "scoringModel": "<verified-local-model-id>",
  "signals": {
    "isIdentifier": {"probabilityYes": 0.93},
    "hasOperationalSignificance": {"probabilityYes": 0.88},
    "isDocumentSpecificOrUncommon": {"probabilityYes": 0.81},
    "isLinkedToOtherSensitiveDetails": {"probabilityYes": 0.74},
    "matchesUserSensitivePattern": {"probabilityYes": null},
    "isPubliclyCommon": {"probabilityYes": 0.12},
    "maskingPreservesTaskMeaning": {"probabilityYes": 0.91}
  },
  "reasons": ["Named project identifier", "Operational context"],
  "scoreStatus": "complete"
}
```

The model ID is a placeholder until the local Ollaya runtime is verified. Define strict validation for probability ranges, missing keys, timeouts, malformed responses, and model/version metadata. Do not allow an Ollaya response to mutate Include/Exclude decisions, candidate identity, occurrences, group membership, or replacement mappings.

## UI changes

- Rename candidate score explanations to **Redaction likelihood**; avoid “confidentiality probability.”
- Show the score, a short “Why suggested” explanation, and expandable signal answers/probabilities. Identify Ollaya-derived vs deterministic vs user-learned evidence.
- Retain the 1–10 control, relabel its behavior as the minimum likelihood threshold / review breadth, and make the level-to-threshold mapping discoverable.
- Keep Include/Exclude and Undo available at all levels. Make pinned state visually distinct from automatically thresholded suggestions.
- Show a clear local-model unavailable/fallback indicator without implying inference completed.
- Offer an option to run/re-run semantic scoring for the current document when appropriate; scoring should be cancellable and should not impact document extraction or export approval beyond the existing graph/level invalidation rules.

## Implementation sequence

### Phase 0 — Validate Ollaya runtime

- Identify the supported local API/CLI/library for this app and test typed Yes/No decisions with probability output.
- Confirm candidate-size limits, request timeouts, model distribution/license, offline behavior, and CPU/memory cost.
- Compare available decision models on a small synthetic prompt set; select and pin a version only after quality and operational checks.
- Gate further work if the runtime cannot guarantee local-only inference or typed probability output.

### Phase 1 — Scoring specification and fixtures

- Finalize question wording, feature definitions, score aggregation, missing-signal behavior, confidence/fallback rules, and the complete slider threshold table.
- Create a synthetic dataset with different contexts for the same terms, common/public terms, internal-style names, operational references, conjunction risks, and user-feedback examples.
- Define expected signal behavior and review targets; avoid sensitive production documents.

### Phase 2 — Backend adapter and scoring

- Add the Ollaya client/adapter and a pure scoring function with explicit scoring-version metadata.
- Integrate after candidate extraction and before candidate results are returned/persisted; reuse bounded local contexts, with no external requests.
- Add timeout, cancellation, unavailable-model, invalid-output, partial-question, and deterministic-fallback paths.
- Keep old tier fields temporarily for migration/UI compatibility; do not reinterpret the existing heuristic 1–10 values as probabilities.

### Phase 3 — Encrypted graph, thresholding, and review UI

- Extend encrypted candidate records with likelihood, score version, explainable signals, and model status; avoid persisting context text.
- Add the likelihood threshold mapping and ensure pinned user decisions override it in both frontend and backend/export preview calculations.
- Add reasons/signal inspection and model-availability status to candidate review.
- Keep similarity proposals unconfirmed and unrelated to scores.

### Phase 4 — User-learning feedback and calibration

- Capture explicit Include/Exclude feedback with its agreed scope; derive only explainable user-pattern features.
- Provide controls to inspect/reset learned patterns. Confirm exclusions cannot be overridden by score or inference.
- Use synthetic data and opt-in, local review telemetry only if separately designed; never upload user examples.
- Tune weights/threshold anchors and assess calibration, false-positive burden, and missed-candidate behavior before changing defaults.

## Verification and acceptance criteria

### Unit and contract tests

- Each yes/no probability is validated; Yes raises and No lowers only its defined feature contribution.
- Missing/unknown signals are excluded and weights are re-normalized; unavailable user history does not count as No.
- Public-common evidence reduces likelihood; task-meaning preservation is reported separately and does not raise sensitivity score.
- Scores remain bounded and deterministic for a fixed model response, feature set, and scoring version.
- Slider threshold is monotonic; all 10 levels have stable values; changing the slider does not call Ollaya or mutate scores.
- Pinned Include and Exclude override every threshold; groups stay unconfirmed unless the user confirms them.
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
- The app never presents Redaction Likelihood as official classification or a guarantee of complete discovery.
- Quality, latency, memory, and calibration targets are agreed from measurements before release claims/defaults are finalized.

## Open decisions before implementation

1. Which supported Ollaya local interface and model/version are available to the packaged Blot app?
2. Which model meets the latency and CPU/memory target for bounded Yes/No questions?
3. Should Ollaya scoring run automatically after candidate extraction or only when the user enables it / requests it?
4. What project/profile scope is allowed for user-learned patterns, and how are those patterns inspected and reset?
5. What calibration and false-positive acceptance targets should determine score weights and threshold anchors?
6. Should the first release migrate all current heuristic tiers to likelihood scoring at once, or expose Ollaya scoring as an opt-in profile until validated?

## Decision summary

Blot should use Ollaya as a local semantic signal evaluator, not as an entity extractor or an opaque confidentiality classifier. Deterministic extraction supplies candidate facts; several typed Yes/No judgements yield per-signal probabilities; an explainable, versioned scorer derives Redaction Likelihood; the slider chooses a threshold; and explicit user decisions remain authoritative. Locality, encrypted graph storage, user review, and non-blocking deterministic fallback preserve Blot's existing privacy and workflow invariants.
