# Document candidate data flow

## Target flow at a glance

```text
Open/import original
  → parse supported editable text for preview
  → Stage 1: extract and deduplicate words from the whole document
  → Stage 2: query local Ollaya for identifier, operational-significance,
    and common-word signals
  → stream each Stage 2 result to the review UI as it arrives
  → save scored candidates and decisions in encrypted project state
  → bulk Include/Exclude or individually cherry-pick reviewed words
  → export preview uses included/auto-suggested candidates at that level
```

## Staged candidate review

1. **Stage 1 — extract unique words.** Read all supported editable text in the document and build one deduplicated set of words for the whole document. Remove punctuation; treat hyphens and underscores as separators so that the words on either side are considered independently. Keep the extracted terms even when they look malformed or appear jumbled together: those are noisy candidates for review, not a reason to silently discard them.
2. **Stage 2 — classify each word with Ollaya.** Query three typed yes/no signals for each unique word: `is_identifier`, `is_operationally_significant`, and `is_common_word`. Ollaya classifies candidates but does not make the user's Include/Exclude decision.
3. **Interpret the signals for review.** `is_common_word` by itself must not eliminate a word. If either `is_identifier` or `is_operationally_significant` is affirmative, treat that as meaningful even when `is_common_word` is also affirmative. Keep the signals visible so the user can make the final decision.
4. **Show results incrementally.** After Stage 1 finishes extracting the unique set, show the words as Ollaya queries them and update each row when its result arrives; do not wait for all queries to finish. The user can select words while results arrive, then bulk Include/Exclude the selection or cherry-pick individual decisions after scoring completes. Preserve explicit decisions when analysis is repeated.

## Current implementation flow

1. **Document is opened.** `App.jsx` calls `loadProjectDocumentPreview` for a project document. The local `/api/projects/document-preview` endpoint reads the selected saved version through the document adapter and returns supported editable text, preview sections, and coverage warnings. For an original version, the frontend then calls `loadProjectDocumentCandidates`.
2. **Stage 1 extracts words.** `ProjectService.analyze_document_candidates` loads the original and its private project state, builds supported text blocks, then calls `extract_word_candidates` in `candidate_engine.py`. Punctuation, hyphens, and underscores separate tokens; case-folded matches across all blocks become one candidate with occurrence locations. There is no fixed limit on the number of unique words extracted. User-added phrases remain pinned manual candidates.
3. **Stage 2 is streamed.** The service emits each unique word with `scoreStatus: queued`, then emits an updated event as that word's Ollaya query completes. The frontend appends/updates rows immediately and keeps the complete review list visible regardless of obfuscation level. Failed or unavailable queries still produce a reviewable row; words without an affirmative identifier or operational signal are not auto-suggested.
4. **Word context is built.** Before scoring each word, `build_ollaya_scoring_input` uses its first valid occurrence to extract at most 192 characters from a bounded sentence window. The payload contains the candidate word and, when available, that short context; it does not send the whole document to Ollaya.
5. **Ollaya classifies locally.** `LocalOllayaScorer` invokes the local Ollaya CLI for three yes/no judgments: `is_identifier`, `is_operationally_significant`, and `is_common_word`. Successful results are cached in a process-local, bounded LRU (up to 2,048 entries), keyed by model, questions, scoring method, and exact compact input. This is a score cache, not a cache of the whole candidate-analysis result. A common-word Yes never excludes a word by itself; an affirmative identifier or operational signal takes precedence over common-word status. Ollaya answers inform review and priority, not the user's Include/Exclude decision.
6. **Results and decisions are saved.** The service rebuilds the candidate nodes for that document version while preserving prior decisions and manual pins, then saves them with the private project graph using encrypted local state. The API completion event includes candidates, confirmed groups, and scan/scoring warnings. Candidate decisions made later are also persisted in that encrypted state. Ollaya's in-memory response cache is not persisted.
7. **The frontend presents words for review.** The full unique-word list and three signals remain visible at every obfuscation level. Users can select rows as results stream, apply Include/Exclude to all words or a selected subset, and make individual decisions. Per-word decisions are saved through `/api/projects/candidate-decision`; bulk decisions use one `/api/projects/candidate-decisions` request. Signal-threshold selection requires an affirmative identifier or operational signal and a `probabilityYes` at or above the chosen percentage; unavailable signals do not match. Confirmed candidate groups are a separate operation and are not the same as including candidates.
8. **Export consumes the decisions.** Export preview reads the saved candidate graph, selected level, and decisions. Excluded candidates are omitted; included and auto-suggested candidates are eligible only when in the selected priority range. The user reviews the resulting preview before creating a separate obfuscated version.

## Current rough edges

- Stage 1 deliberately preserves malformed or concatenated tokens for human review; it does not repair or split a jumbled word beyond punctuation, hyphen, and underscore boundaries.
- Ollaya queries each unique word sequentially, so documents with many distinct words can take time to finish Stage 2.
- The review list covers every extracted word, while export remains limited by the selected obfuscation level and each word's decision.

## Bulk-review behavior

The bulk panel's default scope is **all extracted words**; users can also build a selected subset while Ollaya results stream. Bulk Include/Exclude applies after scoring finishes. Signal thresholds select candidates based on a valid affirmative identifier/operational answer and its `probabilityYes`; missing/unavailable signals never match. The common-word signal is displayed but does not independently change a decision. Selection and confirmed-group membership remain separate concepts. Bulk decisions are saved in one request and preserve confirmed-group propagation and pinned exclusion rules.

Signal labels describe the three underlying Ollaya questions.
