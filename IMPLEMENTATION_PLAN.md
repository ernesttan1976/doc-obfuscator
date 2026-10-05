# Obfuscation App — End-to-End Implementation Plan

**Status:** approved planning baseline; implementation is staged below.  
**Primary PRD:** [`PRD Obfuscation App.md`](./PRD%20Obfuscation%20App.md)  
**Design reference:** [`obfuscation-workspace.html`](./obfuscation-workspace.html)

## Product outcome

Deliver a local-only browser application for a single user to import documents into a chosen project folder, review and adjust sensitive-term suggestions, export a new obfuscated copy for manual use with an external agent, and restore exact intact placeholders in an agent-returned Office document. The original is never overwritten. The app does not connect to an LLM or upload document content.

This is disclosure reduction, not a claim of anonymity or complete file sanitization. Coverage reports and warnings are part of the product, not optional diagnostics.

## Decisions from the planning interview

| Area | Decision |
| --- | --- |
| Runtime | Local browser application; React UI served by a localhost FastAPI/Python service. |
| Launch platforms | macOS and Windows. |
| Launch experience | One-click local launcher that starts the service and opens the browser. |
| Workspace | User chooses a project folder; source files remain unchanged and outputs are new versions. |
| Formats | DOCX, PPTX, TXT, MD, CSV, and XLSX. XLSX scope is literal text cell values; preserve formulas and workbook structure. |
| XLSX restoration | In scope: exact placeholder restoration in returned workbooks. |
| Text encodings | UTF-8 with/without BOM and detectable UTF-16; reject malformed or ambiguous encodings before export. |
| Office coverage | Aim to process editable text, including extended Office parts such as notes and other editable surfaces. The product must identify unhandled text-bearing parts and warn before the user elects to export. It must never describe that warning as proof that all sensitive content was found. |
| Term discovery | Broad local English-language candidate discovery (entities and sensitive patterns), plus manual user selections. |
| Similarity | RapidFuzz plus a locally run MiniLM encoder. Similarities are suggestions; the user confirms group membership before group-wide replacement. Model download is explicit and user initiated. |
| Decision scope | Current document/version by default; a confirmed group can be explicitly applied to selected project files later. |
| Slider | Fixed default 1–10 tiers for v1; manual Include/Exclude is pinned and undoable. |
| Privacy | No LLM integrations or document telemetry. Bind only to loopback. Encrypt graph and mapping with a data key protected by the OS credential store (macOS Keychain / Windows Credential Manager). |
| Idle lock | After 15 minutes idle, require OS reauthentication (macOS authorization / Windows Hello) before reopening project state. |
| Backup | Portable encrypted backup using a user-provided backup passphrase, restorable on either launch OS. |
| Scale | 100 MB per source file; target under 3 seconds to first useful preview for ordinary documents, with longer processing shown as a cancellable job. |
| Verification data | Synthetic documents only; do not use sensitive material for acceptance tests. |

### Approved changes from the PRD

1. XLSX is added, including literal cell text masking and restoration. Formulas and workbook behavior are preserved; formulas themselves are not rewritten.
2. Office coverage is broadened beyond the original narrow body-text description. The implementation must publish a per-format coverage inventory and report unsupported/unknown text-bearing parts. Because the user chose warn-and-continue, export may proceed only after explicit acknowledgement; the report must remain attached to that output/version.
3. Candidate seeding includes broad local English entity and pattern extraction, in addition to manual selection. Similarity never silently creates an accepted replacement group.
4. The browser app is explicitly targeted at macOS and Windows and must include a one-click launcher.

These deltas should be merged into the PRD once the technical coverage inventory and threat model are validated in Stage 1; until then, this plan is the decision record.

## Product boundaries and safety invariants

- Process locally; no service may send document text or term/context embeddings to a remote endpoint. The only planned network operation is an explicit, integrity-checked model download.
- Bind the API to `127.0.0.1`/`::1`, use a per-launch secret for browser requests, restrict origins, and reject non-local clients.
- Keep originals immutable. Every obfuscated and restored result is a separate version.
- Keep graph and placeholder mapping out of exported documents and logs; encrypt them at rest.
- Use cryptographically random opaque placeholders, scan for collisions, and retain the mapping only in the local encrypted project graph.
- Restore exact known tokens only in an explicitly associated returned DOCX/PPTX/XLSX. Never fuzzy-match, infer, or restore TXT/MD/CSV/chat text.
- Report altered, unknown, foreign-project, and unresolved tokens. “Complete” means no recognized unresolved tokens remain; it does not certify factual correctness.
- Exclude OCR/image text, macros/executable content, general metadata sanitization, and any component not covered by a tested adapter. Surface any detected unsupported part and the user's acknowledgement.
- Diagnostics must not contain raw source terms or document excerpts.

## Target architecture

```text
React + Vite (development) / static built UI (release)
                  │ localhost + launch-scoped token
                  ▼
FastAPI local service ── workspace/project/version service
       │                  SQLite metadata + encrypted graph/map sidecars
       ├── text adapters: UTF-8/UTF-16, TXT/MD/CSV
       ├── OOXML adapters: DOCX/PPTX/XLSX, preserving package parts
       ├── candidate pipeline: local English entity/pattern extraction
       ├── graph/similarity: RapidFuzz + local MiniLM
       └── security: OS credential store, encrypted portable backup
```

Development keeps the existing high-fidelity React design. Release serving must use the same-origin FastAPI process; do not expose a general-purpose unauthenticated file API. Use a typed API contract, explicit job states for long-running extraction/model work, cancellation, and structured errors that omit source text.

## Stages and gates

### Stage 0 — Import source and plan (complete)

- Copy the OpenDesign source, React/Vite prototype, artifact metadata, and source history into this folder. Exclude generated `node_modules` and `dist`; retain the byte-identical existing PRD rather than create a duplicate.
- Keep the existing OpenDesign HTML as the visual regression/design source.
- Record user decisions and accepted scope changes in this plan.

**Gate:** project assets are local; existing PRD retained; neighboring workspace untouched.

### Stage 1 — Repository baseline, runtime shell, and security design

- Establish `frontend/` and `backend/` ownership without discarding the current UI; choose and pin React/Vite and FastAPI/Python versions.
- Serve the production UI from FastAPI; create a one-command development mode and smoke-test both.
- Define localhost-only binding, per-process browser token, origin/CSRF policy, file-picker boundary, archive limits, logging redaction, and API error contract.
- Create the threat model and Office coverage inventory before promising “everything editable.” Model and NER artifacts must be pinned, checksummed, licensed, and stored locally after explicit download.
- Add executable local checks for frontend build and Python tests using synthetic fixtures; automate them in the release hardening stage.

**Gate:** a clean checkout can install and start the local app; an external network client cannot reach the file API; document content is not logged except in the explicitly requested, local one-line Ollaya request/response records.

### Stage 2 — Project folders, versions, and encrypted local state

- Create/open projects in a user-selected directory; copy imports into immutable originals and maintain a manifest/version history.
- Add SQLite metadata for projects, files, processing jobs, exports, and restore reports. Keep sensitive graph/map material in separately encrypted records/sidecars.
- Use an OS credential store to protect the random per-project data key; make a missing key an explicit, non-destructive error.
- Implement idle relock with OS reauthentication and encrypted backup/restore with a portable passphrase in Stage 9. Never silently overwrite a backup or original.

**Gate:** create/reopen a synthetic project; verify graph/map ciphertext at rest; missing key produces a recoverable, non-destructive error. Cross-OS backup restore is gated in Stage 9.

### Stage 3 — Text and spreadsheet adapters

- TXT/MD: preserve line endings, BOM/encoding, and ordinary text structure; support UTF-8 and detectable UTF-16 only.
- CSV: retain delimiters, quoting, rows, columns, and supported encoding; reject ambiguous dialects rather than silently rewrite structure.
- XLSX: traverse literal text values across worksheets, retain formulas/styles/relationships, skip formula rewriting, and produce a coverage report. Adapter tests prove exact-token replacement round-trips; the project restoration workflow remains Stage 8.
- Validate archive safety, malformed packages, large inputs, and count pre-existing placeholder-like text so Stage 7 can require review before export.
- Provide protected, bounded local previews for TXT/MD/CSV/XLSX. Count existing placeholder-like strings without logging or returning their values; report unsupported workbook parts rather than implying complete coverage.

**Gate:** golden fixtures prove encoding, line-ending, CSV dialect, and workbook structure invariants (except intended cell/text replacement); formulas/styles/relationships and originals remain unchanged; malformed/ambiguous input and unsupported workbook parts are reported before later export.

### Stage 4 — DOCX/PPTX editable-text adapters and coverage report (complete)

- Implement package-preserving Office text traversal/replacement and restoration, including supported body/slide text, tables, text boxes, notes and other editable text-bearing package parts.
- Maintain formatting and surrounding structure where possible; test terms split across runs, moved/repeated placeholders, and agent-edited structure.
- Inventory every examined/skipped part. Surface unsupported parts with a visible warning and explicit export acknowledgement. Unknown tokens remain untouched and are reported.
- Do not claim support for OCR/image text, macros, arbitrary embedded objects, or metadata removal unless a dedicated tested adapter is later accepted.

**Gate:** synthetic DOCX/PPTX fixtures round-trip with formatting/structure assertions; supported coverage report is stable; warnings are test-covered. Export-time warning acknowledgement is gated with Stage 7.

**Implemented scope:** DOCX/PPTX archives are validated and scanned locally. The adapters traverse WordprocessingML `w:t`/`w:delText` and DrawingML `a:t` paragraph text across package XML parts, including body/slide text, tables, headers, comments, and notes. Exact replacements span split runs, retain the leading run's formatting, apply to repeated/moved tokens, and leave untouched package parts byte-identical. Bounded coverage reports list examined XML parts, skipped non-XML parts, detected text-bearing parts, and unsupported/unhandled parts; metadata, images/OCR, macros, embedded binary content, external relationship targets, and non-paragraph/unknown XML text are not processed. The adapters are available for preview and round-trip verification; export acknowledgement and product restoration workflows remain Stages 7–8.

### Stage 5 — Candidate extraction, graph, and local similarity

- Seed English candidates with a pinned local NER model and deterministic local patterns (identifiers, dates, email/phone-like values and configurable phrase candidates); permit manual phrase selection.
- Generate spelling/format variants with RapidFuzz and contextual proposals with MiniLM; show score and reason for every proposed edge.
- Keep proposed graph edges distinct from confirmed groups. Add/remove/split/merge membership; prevent a manual Exclude from being overridden by auto suggestions.
- Scope decisions and occurrences to a document version. Use fixed, documented level-to-candidate defaults for the 1–10 slider.

**Gate:** synthetic tests prove no unconfirmed edge causes group replacement; manual Include/Exclude stay pinned across slider changes; offline inference produces stable proposals.

**Implemented foundation:** supported text blocks are scanned locally for email-like values, phone-like values, dates, identifiers, and capitalized phrase candidates. Candidate occurrences retain adapter locations and offsets; candidates are scoped to an immutable document version, assigned stable IDs, and persisted only in the AES-GCM encrypted project graph. Fixed review-level defaults are email/phone/manual = 1, dates/identifiers = 2, and capitalized phrases = 5 on the 1–10 scale. Manual phrases must occur in supported text and start Included/pinned. RapidFuzz spelling/format edges carry a score and explanation, remain `proposed`/unconfirmed, and never create group membership. Candidate Include/Exclude decisions survive repeated analysis. Explicit add/remove/split/merge group operations are available through the protected API and persist as confirmed memberships. The saved-document UI now scans candidates, lets users select/manual-add phrases, review and pin decisions, inspect proposals, and manage explicit groups; full Stage 6 remains in progress.

**Selected NER artifact:** `knowledgator/gliner-multitask-large-v0.5`, immutable revision `7a95e168036db9ec6f914c0cc6b218edbd87f310`, Apache-2.0. The explicitly downloaded FP16 safetensors file is 880,500,126 bytes; pinned tokenizer/configuration assets bring the download to about 0.9 GB. The checkpoint is loaded in FP32 for CPU compatibility, so inference may require several GB of memory. Artifact hashes are recorded in `backend/app/model_manager.py`; the downloader verifies them before atomic installation and the offline loader verifies them again before first inference. GLiNER `0.2.29` is an optional Python extra (`uv sync --extra models`); dependencies install separately from weights. No model assets are present by default.

**Selected contextual encoder:** `sentence-transformers/all-MiniLM-L6-v2`, immutable revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`, Apache-2.0. The explicitly downloaded safetensors checkpoint and pinned tokenizer/configuration/pooling files total 91,567,395 bytes. Local inference uses attention-mask mean pooling and normalized embeddings with CPU-only `transformers`/PyTorch; each candidate mention is masked before its bounded sentence context is embedded. Candidate context strings and vectors are transient and never persisted. Artifact hashes are recorded in `backend/app/similarity_manager.py`, verified before atomic installation and again before offline loading. The existing optional `models` extra supplies the runtime; MiniLM weights are downloaded separately and only after confirmation.

**Still pending:** validate both models' quality and CPU/memory performance on synthetic fixtures after a user explicitly downloads the artifacts; and pass the full Stage 5 acceptance gate. Sensitivity-threshold filtering and visibility of manual Include/Exclude decisions are covered by focused UI logic tests. Heuristic phrase discovery and RapidFuzz proposals remain available when either optional model is absent or unavailable.

### Stage 6 — Review workflow in the preserved React design

- Wire project/file/version selection, import status, editable text preview, page/slide navigation, dense text mode, graph panel, sensitivity slider, change counts, decision labels, and Undo.
- Add coverage/unresolved reporting and explicit “editable text only” scope text. Surface unsupported-part warnings beside preview and export approval.
- Invalidate approval when source/version, graph, profile, or slider changes.
- Add accessible controls and keyboard behavior without changing the supplied visual design unnecessarily.

**Gate:** acceptance flows can be completed without dev tools; narrow/wide slider behavior and manual overrides match golden tests; responsive layout and keyboard checks pass.

**Implemented:** saved project documents and their original/obfuscated versions are selectable in the review surface; empty projects have an import prompt and demo files no longer appear as project files. The saved-document preview exposes adapter/encoding, editable-text scope, unsupported-part warnings, bounded DOCX/PPTX text sections with keyboard navigation, dense text mode, sensitivity scoring and filtering, Include/Exclude decisions with group propagation that preserves pinned exclusions, review counts, manual phrase selection, proposal/group operations, and Undo. Slider, graph edits, file/version changes close any pending export preview. The design source remains intact; added responsive controls and an accessible preview/export dialog. Browser-based visual and keyboard acceptance remains to be performed.

### Stage 7 — Obfuscation preview and export

- Allocate collision-resistant random placeholders per project/document version; replacement map never enters exported bytes.
- Preview the exact output, coverage, matches, and warnings. Require explicit approval; export a new immutable version only.
- Preserve CSV quoting/line endings, Office package structure, workbook formulas, and supported formatting. Never overwrite originals.

**Gate:** exported synthetic fixtures contain only expected placeholders; no source map/graph is present; unknown/unsupported coverage is reported; approval invalidates correctly.

**Implemented:** protected APIs produce a bounded output preview and random 128-bit placeholders after collision checks against source tokens and the encrypted project map. Case-insensitive replacements preserve supported text encodings, CSV quoting, Office package structure, and XLSX formulas through the existing adapters. A preview plan is short-lived and binds source bytes/version, slider level, candidates, confirmed groups, and proposal edges; export rejects changed or expired plans. Office/XLSX coverage and pre-existing placeholder warnings require explicit acknowledgement. Approval saves a distinct obfuscated version under `.blot/outputs/`, writes term mappings only to the AES-GCM-encrypted state, and enables protected download/version preview. Synthetic tests cover warning acknowledgement, immutable originals, private maps, output downloads/version listing, exact supported replacements, and stale-graph rejection. Browser-based end-to-end acceptance remains to be performed.

### Stage 8 — Returned Office restoration

- Import returned DOCX/PPTX/XLSX into an explicitly selected project and source version.
- Restore only exact valid tokens from that encrypted local map; support moved/repeated tokens; leave modified/unknown/foreign-project tokens untouched.
- Provide before/after preview, unresolved-token report, and save as a new version. No plaintext restoration action exists.

**Gate:** moved/repeated intact tokens restore; altered/foreign tokens do not; unresolved report is accurate; all originals remain unchanged.

**Implemented:** the user associates a returned DOCX/PPTX/XLSX with an obfuscated version in the active project. The preview restores only exact, case-sensitive tokens whose encrypted mapping belongs to that document and source version; moved and repeated tokens are supported across adapter text blocks. Unknown/foreign and altered token-like strings remain unchanged and are counted in the report. Approval saves a new `restored` version, preserves the returned file, and stores the report in encrypted project state. Restored copies can be previewed and downloaded. Synthetic API acceptance covers export → agent-edit simulation → preview → restore → unresolved reporting → immutable original checks.

### Stage 9 — Cross-platform launcher, backup, and release hardening

- Provide macOS and Windows launch/setup scripts that bootstrap the pinned environment, start loopback service, open the browser, and stop cleanly.
- Make model download explicit, resumable, checksummed, and cancellable. Support use after download with no network access.
- Test OS credential-store behavior and passphrase backup portability on both OSes; document reset/recovery and deletion consequences.
- Run performance tests against synthetic files up to 100 MB; ensure long jobs expose progress/cancel rather than blocking the UI.

**Gate:** fresh setup and uninstall/reinstall test on macOS and Windows; offline workflow works after model acquisition; dependency/license/security checks pass.

**Implemented:** macOS setup/launch commands and Windows PowerShell setup/launch scripts bootstrap pinned dependencies, build the UI, start the loopback service, open the browser, and stop the service on normal exit. Idle activity locks the API after 15 minutes, clears pending export/restore plans, hides loaded project documents in the UI, and requires reopening through the OS credential-store provider. Portable project backups include versions and encrypted project state, use a user passphrase with scrypt + AES-GCM, and restore to an empty folder with a fresh OS-protected project key. Pinned model artifacts now resume from verified HTTP byte ranges after cancellation/interruption while retaining checksum validation.

**Acceptance still pending:** interactive OS credential-store prompts and clean install/uninstall behavior need validation on real macOS and Windows accounts. Offline model inference, model quality/resource limits, 100 MB performance, and release dependency/license review remain part of the platform release gate.

### Stage 10 — Pilot and acceptance release

- Run acceptance suite exclusively with synthetic data; pilot with non-sensitive documents only after explicit informed review.
- Validate false-positive load, grouping explanations, slider defaults, warnings, restoration reports, and performance with users.
- Review the PRD, supported-format matrix, privacy copy, setup instructions, and known limitations against shipped behavior.

**Release gate:** FR01–FR10 plus approved XLSX additions have mapped passing acceptance tests; privacy invariants have automated checks; no known silent-loss/false-completion path remains.

**Implemented foundation:** a synthetic-data integration test now exercises project create/import, candidate review, obfuscation export, returned Office edit simulation, exact restoration, unknown/altered token reporting, encrypted portable backup, and backup restore with mapping portability. Automated tests also cover API idle-lock enforcement and resumable downloads. Human pilot, false-positive review, browser keyboard/responsive review, cross-platform installation, and performance measurements have not yet been completed.

## Cross-cutting verification strategy

- **Unit:** text encoding/dialect handling, candidate normalization, graph decisions, token generation/collision, encryption/key errors, placeholder exact-match semantics.
- **Format fixtures:** synthetic DOCX/PPTX/XLSX with tables, split runs, notes, comments/revisions where supported, formulas, styles, unknown parts, pre-existing token-like text, and malformed/hostile ZIP entries.
- **Integration:** project creation → import → review → include/exclude → preview → export → external-edit simulation → Office restore → unresolved report.
- **Privacy:** assert no original terms in unrelated diagnostics or exported sidecars; Ollaya request/response logs are an explicit local exception; exported output contains no map; service listens only on loopback; outbound traffic limited to explicit model download.
- **Cross-platform:** run launcher, key-store, filesystem, backup/restore, and path/permission tests on macOS and Windows.
- **Performance:** import and first preview under 3 seconds for typical synthetic documents; background progress for larger inputs; 100 MB ceiling and memory limits tested.

## Risks to manage

1. “Everything editable” spans many vendor-specific OOXML parts. Maintain a tested support matrix and warn-and-continue exactly as chosen; never represent warnings as complete coverage.
2. Broad candidate discovery increases false positives. Candidate suggestions and similarity edges remain user-reviewed; the slider is a review aid, not a sensitivity score.
3. MiniLM/NER downloads add size, licensing, integrity, offline, and update concerns. Pin model revisions and hashes; download only by explicit action.
4. Cross-platform OS key stores differ. Test real macOS and Windows accounts and define key-loss recovery before storing irreplaceable mappings.
5. OOXML round-trip libraries may discard parts. Prefer tested package-preserving transformations; fail visibly on preservation anomalies and compare package structures in tests.
6. The supplied React app is currently a browser-only prototype. Its session-only behavior and sample DOCX/PPTX rows are not production processing; each feature must be connected to the local API before it is labeled complete.

## Current progress

- [x] Imported the OpenDesign source and retained the existing PRD.
- [x] Captured platform, format, model, privacy, storage, and workflow decisions.
- [x] Stage 1: baseline local React + FastAPI runtime and test/security scaffolding.
- [x] Stage 2: native project/document pickers, create/open/import UI and API, SQLite document/version records, read-only source copies, OS-protected data key, AES-GCM-encrypted graph/map sidecar, and missing-key errors.
- [x] Stage 3: strict UTF-8/UTF-16 TXT/MD parsing; structure-preserving CSV scanning/replacement; safe XLSX literal-cell preview/replacement with formula/package preservation and coverage warnings; protected bounded preview API/UI; placeholder-like text count.
- [x] Stage 4: safe DOCX/PPTX package parsing and exact paragraph-text replacement across split runs; bounded local text previews; detected coverage inventory for unsupported parts; synthetic format-preservation and round-trip tests.
- [~] Stage 5 foundation: deterministic local pattern candidates, manual phrase candidates, encrypted version-scoped graph storage, candidate Include/Exclude pinning, unconfirmed RapidFuzz proposals, local MiniLM contextual proposals with masked mentions, and explicit confirmed group operations. Sensitivity-threshold filtering and pinned-decision visibility have focused unit coverage. Fixed-revision Apache-2.0 GLiNER and MiniLM artifacts have confirmed-only, integrity-checked download paths and an optional local runtime; artifact acquisition and real inference/quality/performance validation remain pending before the full acceptance gate.
- [~] Stage 6 review workflow: saved document/version selection, import/parse status, bounded editable-text preview, candidate sensitivity scores and threshold filter, match/review counts, Include/Exclude/group decisions, Undo, coverage reporting, DOCX/PPTX section navigation, dense mode, accessible controls, and an empty-project import state. Keyboard interaction is implemented; responsive browser/keyboard acceptance is pending.
- [~] Stage 7 obfuscation preview/export: cryptographically random collision-checked placeholders, encrypted-only mappings, exact output preview, explicit coverage acknowledgement, immutable output versions, protected repeat download, and approval invalidation for source/graph/level changes. Backend synthetic integration tests pass; browser-based flow validation remains pending.
- [~] Stage 8 returned Office restoration: exact version-scoped restoration, unresolved reporting, and immutable restored versions are implemented and covered by synthetic integration tests.
- [~] Stage 9 release hardening: encrypted portable backup/restore, 15-minute idle locking, resumable integrity-checked model downloads, and macOS/Windows setup/launch scripts are implemented; actual OS prompt/platform acceptance remains pending.
- [~] Stage 10 pilot/release acceptance: synthetic end-to-end acceptance coverage is implemented; user pilot, browser accessibility/responsiveness, Windows/macOS installation, model inference/resource, and 100 MB performance gates remain pending.

**Current boundary:** Stages 6–9 now have connected saved-project UI/API workflows, synthetic tests, and launch/bootstrap assets. Stage 10 has a synthetic end-to-end acceptance foundation, not a completed pilot or release sign-off. Actual OS credential prompts and setup/teardown remain unverified on Windows and require hands-on macOS acceptance; the idle gate delegates the unlock check to the OS credential-store provider. Pinned NER/MiniLM artifacts remain opt-in and unacquired by default; real inference quality/resource limits, offline acceptance, browser accessibility/responsiveness, and 100 MB performance are outstanding. Unsupported-part warnings remain attached to previews/versions and require acknowledgement before export.
