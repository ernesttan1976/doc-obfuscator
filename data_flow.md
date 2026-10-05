# Document candidate data flow

## At a glance

```text
Open/import original
  → parse supported editable text for preview
  → discover deterministic + optional local NER candidates
  → stream discovery rows to the review UI (status: scanning)
  → build a short context for each candidate
  → ask local Ollaya for typed yes/no signals (or use unavailable fallback)
  → save scored candidates and decisions in encrypted project state
  → return the completed candidate list to the UI
  → filter by obfuscation level, review, Include/Exclude
  → export preview uses included/auto-suggested candidates at that level
```

## Detailed flow

1. **Document is opened.** `App.jsx` calls `loadProjectDocumentPreview` for a project document. The local `/api/projects/document-preview` endpoint reads the selected saved version through the document adapter and returns supported editable text, preview sections, and coverage warnings. For an original version, the frontend then calls `loadProjectDocumentCandidates`.
2. **Candidates are discovered.** The frontend posts to `/api/projects/document-candidates/stream`. `ProjectService.analyze_document_candidates` loads the original and its private project state, builds text blocks, then calls `analyze_candidates` in `candidate_engine.py`. Deterministic patterns find email addresses, phone numbers, dates, identifiers, and capitalized phrases. If an optional local NER model is available, its entities are merged in; user-added phrases are treated as pinned manual candidates. A normalized term and document-version ID determine the stable candidate ID. Discovery is capped at 1,000 candidates per version.
3. **Discovery is streamed.** As terms are found, `analyze_candidates` emits candidate events. The frontend appends/updates them and displays `scoreStatus: scanning`. These are provisional rows; final candidate data replaces them at the completion event.
4. **Candidate context is built.** Before scoring, `build_ollaya_scoring_input` uses the first valid occurrence to extract at most 192 characters from a bounded sentence window. The payload contains the candidate text and, when available, that short context; it does not send the whole document to Ollaya.
5. **Ollaya scores locally.** `LocalOllayaScorer` invokes the local Ollaya CLI and requests three yes/no judgments: named-entity identifier, organizational term, and operational significance. Successful results are cached in a process-local, bounded LRU (up to 2,048 entries), keyed by model, questions, scoring method, and exact compact input. This is a score cache, not a cache of the whole candidate-analysis result. Missing/failed Ollaya scoring is advisory: the candidate remains available with an unavailable score and heuristic priority fallback. Ollaya answers influence suggestion confidence/priority, not the user's decision.
6. **Results and decisions are saved.** The service rebuilds the candidate nodes for that document version while preserving prior decisions and manual pins, then saves them with the private project graph using encrypted local state. The API completion event includes candidates, confirmed groups, and scan/scoring warnings. Candidate decisions made later are also persisted in that encrypted state. Ollaya's in-memory response cache is not persisted.
7. **The frontend presents candidates.** `getVisibleCandidates` shows priority levels 2 through the selected obfuscation level, sorted by Ollaya review priority when available. Candidates at other levels remain in project state but are not active for that export level. Per-candidate Include/Exclude decisions are saved through `/api/projects/candidate-decision`. Bulk actions apply to all candidates shown at the current level, or to a manually/threshold-selected set, through one `/api/projects/candidate-decisions` request. Threshold matching requires Ollaya's affirmative answer and a `probabilityYes` at or above the chosen percentage; unavailable signals do not match. Confirmed candidate groups are a separate operation and are not the same as including candidates.
8. **Export consumes the decisions.** Export preview reads the saved candidate graph, selected level, and decisions. Excluded candidates are omitted; included and auto-suggested candidates are eligible only when in the selected priority range. The user reviews the resulting preview before creating a separate obfuscated version.

## Current rough edges

- The candidate list still combines discovery metadata, Ollaya signals, priority, occurrence locations, bulk Include/Exclude selection, and separate manual-group selection in each card.
- A single Include/Exclude action still reloads analysis after saving; bulk actions avoid repeated per-candidate requests by saving the selected IDs in one operation.
- Counts describe all candidates, while the visible list is filtered by obfuscation level. It is easy to mistake the full-list totals for the active export set.
- The bulk panel labels its scope as shown candidates at the active level; candidates outside that range remain untouched unless the level is changed.

## Bulk-review behavior

The bulk panel makes its active scope explicit: **shown candidates at the current obfuscation level**. Bulk Include/Exclude applies to that scope. Signal thresholds select candidates based on a valid affirmative Ollaya answer and its `probabilityYes`; missing/unavailable signals never match. Selection and confirmed-group membership remain separate concepts. Bulk decisions are saved in one request and preserve the existing confirmed-group propagation and pinned exclusion rules.

Signal labels describe the underlying Ollaya questions. Organizational-term scoring is separate from named-entity/identifier and operational-significance scoring.
