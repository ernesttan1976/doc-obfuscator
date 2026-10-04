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

**Gate:** a clean checkout can install and start the local app; an external network client cannot reach the file API; no document content is logged.

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

### Stage 6 — Review workflow in the preserved React design

- Wire project/file/version selection, import status, editable text preview, page/slide navigation, dense text mode, graph panel, candidate breadth slider, change counts, decision labels, and Undo.
- Add coverage/unresolved reporting and explicit “editable text only” scope text. Surface unsupported-part warnings beside preview and export approval.
- Invalidate approval when source/version, graph, profile, or slider changes.
- Add accessible controls and keyboard behavior without changing the supplied visual design unnecessarily.

**Gate:** acceptance flows can be completed without dev tools; narrow/wide slider behavior and manual overrides match golden tests; responsive layout and keyboard checks pass.

### Stage 7 — Obfuscation preview and export

- Allocate collision-resistant random placeholders per project/document version; replacement map never enters exported bytes.
- Preview the exact output, coverage, matches, and warnings. Require explicit approval; export a new immutable version only.
- Preserve CSV quoting/line endings, Office package structure, workbook formulas, and supported formatting. Never overwrite originals.

**Gate:** exported synthetic fixtures contain only expected placeholders; no source map/graph is present; unknown/unsupported coverage is reported; approval invalidates correctly.

### Stage 8 — Returned Office restoration

- Import returned DOCX/PPTX/XLSX into an explicitly selected project and source version.
- Restore only exact valid tokens from that encrypted local map; support moved/repeated tokens; leave modified/unknown/foreign-project tokens untouched.
- Provide before/after preview, unresolved-token report, and save as a new version. No plaintext restoration action exists.

**Gate:** moved/repeated intact tokens restore; altered/foreign tokens do not; unresolved report is accurate; all originals remain unchanged.

### Stage 9 — Cross-platform launcher, backup, and release hardening

- Provide macOS and Windows launch/setup scripts that bootstrap the pinned environment, start loopback service, open the browser, and stop cleanly.
- Make model download explicit, resumable, checksummed, and cancellable. Support use after download with no network access.
- Test OS credential-store behavior and passphrase backup portability on both OSes; document reset/recovery and deletion consequences.
- Run performance tests against synthetic files up to 100 MB; ensure long jobs expose progress/cancel rather than blocking the UI.

**Gate:** fresh setup and uninstall/reinstall test on macOS and Windows; offline workflow works after model acquisition; dependency/license/security checks pass.

### Stage 10 — Pilot and acceptance release

- Run acceptance suite exclusively with synthetic data; pilot with non-sensitive documents only after explicit informed review.
- Validate false-positive load, grouping explanations, slider defaults, warnings, restoration reports, and performance with users.
- Review the PRD, supported-format matrix, privacy copy, setup instructions, and known limitations against shipped behavior.

**Release gate:** FR01–FR10 plus approved XLSX additions have mapped passing acceptance tests; privacy invariants have automated checks; no known silent-loss/false-completion path remains.

## Cross-cutting verification strategy

- **Unit:** text encoding/dialect handling, candidate normalization, graph decisions, token generation/collision, encryption/key errors, placeholder exact-match semantics.
- **Format fixtures:** synthetic DOCX/PPTX/XLSX with tables, split runs, notes, comments/revisions where supported, formulas, styles, unknown parts, pre-existing token-like text, and malformed/hostile ZIP entries.
- **Integration:** project creation → import → review → include/exclude → preview → export → external-edit simulation → Office restore → unresolved report.
- **Privacy:** assert no original terms in logs or exported sidecars; exported output contains no map; service listens only on loopback; outbound traffic limited to explicit model download.
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
- [ ] Stages 5–10: not started.

**Current boundary:** Candidate extraction/graph suggestions begin in Stage 5; review workflow is Stage 6; obfuscation/export is Stage 7; returned Office restoration is Stage 8. DOCX/PPTX and XLSX adapters support exact mapped text replacement for adapter round-trip verification, but the product does not yet expose an export or restoration workflow. Unsupported-part warnings are visible in preview; explicit acknowledgement is gated with export in Stage 7. Idle relock and portable encrypted backup remain release-hardening work (Stage 9); the chosen unlock policy is OS reauthentication, not a UI-only lock.
