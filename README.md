# Blot — Local Document Obfuscation

This repository contains the OpenDesign React prototype and the approved implementation roadmap in [`IMPLEMENTATION_PLAN.md`](./IMPLEMENTATION_PLAN.md). The product target is a local-only browser app backed by FastAPI. Stages 2–4 add encrypted project state, immutable source intake, and local document previews for TXT, MD, CSV, XLSX, DOCX, and PPTX. Stage 5 provides deterministic pattern candidates, manual phrase candidates, encrypted per-version graph storage, unconfirmed RapidFuzz proposals, an optional pinned GLiNER NER model, and an optional pinned MiniLM contextual encoder. Both model artifact sets download only after explicit user confirmation.

## Run the current UI prototype

```bash
npm ci
npm run dev
```

Vite opens the preserved workspace at `http://localhost:5173/obfuscation-workspace.html`.

## Run the local API and project storage

For a one-terminal development setup, install dependencies once and then start both services together. On Apple-Silicon macOS:

```bash
uv sync --inexact --extra dev --extra minilm-macos
npm ci
npm run dev:app
```

On Windows, replace `minilm-macos` with `minilm-windows`. The development launcher also syncs the platform runtime before starting Python, preserving already-installed optional packages. The command starts the local API and Vite, then opens the app in your browser. Press Ctrl+C to stop the services it started. If the API is already running on port 8765, it reuses it and never kills unrelated processes. To run the services separately, start FastAPI with `uv run --extra minilm-macos python -m backend.app` on Apple-Silicon macOS or `uv run --extra minilm-windows python -m backend.app` on Windows; start Vite with `npm run dev` in another terminal. Vite proxies `/api` to the loopback-only API. It provides protected project/version/export/restoration/backup endpoints, explicit local-model status/download/cancel endpoints, native project/document/backup pickers, and an idle session lock.

To install both optional Python model runtimes, use `uv sync --inexact --extra models --extra minilm-macos` on Apple-Silicon macOS or `uv sync --inexact --extra models --extra minilm-windows` on Windows. This installs Python packages only; NER and MiniLM weights/tokenizer assets remain absent until explicitly downloaded from their controls in the saved-document review panel.

Candidate review also uses Ollaya's local CLI with the `von:1.1` model. Install Ollaya and explicitly acquire that model with `ollaya pull von:1.1` before launching Blot; Blot never downloads it. Each extracted term and its bounded local context is sent to the local Ollaya runtime for the identifier and operational-significance questions. Candidate text is supplied on the CLI's stdin and is not sent to a hosted endpoint. If Ollaya or the model is unavailable, analysis continues with existing heuristic levels and labels the semantic score as unavailable.

The backend writes each Ollaya request and raw response as a single local log line. This includes the candidate and bounded context supplied for scoring; those logs are not sent externally. Context is not stored in the encrypted project state.

Use **Add or open workspace** in the sidebar to create or open a project. Previously opened workspaces are remembered in this browser and appear under **Recent workspaces** for one-click reopening. Select **Import file** to choose DOCX, PPTX, TXT, MD, CSV, or XLSX sources; each file is copied read-only into `.blot/originals/` and recorded as an original version in `.blot/project.sqlite3`. Files over 100 MB are rejected. Project graph/map state uses AES-GCM encryption with its random data key in the OS credential store.

Opening a saved TXT or MD shows a local text preview with UTF-8/UTF-16 encoding and line-ending metadata. CSV previews retain the detected delimiter, quoting, rows, and columns; ambiguous or inconsistent dialects are rejected. XLSX previews read literal shared-string and inline-string cells across worksheets, preserve formulas and workbook parts, and flag detected unsupported text-bearing parts. DOCX/PPTX previews scan WordprocessingML and DrawingML paragraph text across package XML parts, including split runs, tables, headers/comments, slides, and notes; exact-text adapter replacements preserve run formatting and leave untouched package parts intact. Bounded coverage reports list examined XML parts, skipped non-XML parts, and detected unsupported parts; saved-document review surfaces these warnings and per-format coverage counts beside the preview, plus bounded PPTX slide/DOCX text-part navigation and a dense text mode. DOCX page layout is not inferred from raw package XML. Images/OCR, macros, embedded binary content, external relationship targets, document metadata, and text outside the supported paragraph XML elements are not processed. Candidate analysis always uses local patterns for email-like values, phone-like values, dates, identifiers, and capitalized phrases; once installed, the optional GLiNER model adds bounded entity suggestions with labels and confidence. MiniLM encodes bounded nearby context after masking each candidate mention, then offers cosine-scored contextual suggestions. Both Apache-2.0 artifact sets are pinned to immutable revisions and verified before local loading; model outputs and RapidFuzz edges remain suggestions until the user confirms a group. The saved-document UI supports model acquisition, review breadth, manual phrase selection, Include/Exclude with Undo, review-status counts, proposal review, and explicit group operations. Candidate data is stored in the encrypted project graph; contextual text and embeddings are not persisted. Obfuscation, export, and product restoration remain pending. Previews are bounded; placeholder-like text is counted without exposing it in diagnostics. Demo documents stay labeled as unsaved.

## Review and export a saved project document

DOCX pages are rendered locally with LibreOffice and PDF.js. Candidate mentions are highlighted on the rendered page; click to include, double-click to exclude, or right-click for options. The selectable text layer also supports manual phrase selection. The DOCX UI uses rendered pages rather than exposing raw OOXML text-part sections as a separate view. Install LibreOffice to enable DOCX page rendering. Pagination comes from the document renderer rather than guessed page breaks in DOCX XML.

The existing obfuscation-level slider filters by the candidate's deterministic level. Ollaya separately reports Redaction confidence and a Review Priority from 1–10; scored candidates are ordered by Review Priority (10 first). A complete pair of No answers is not automatically suggested, while explicit Include/Exclude decisions remain authoritative. If local scoring is unavailable, the app falls back to its existing heuristic behavior and identifies the missing Ollaya result.

1. Open or create a local project and import the document. Select the **Original** version in the review toolbar.
2. Review the coverage report, adjust the minimum sensitivity, include or exclude candidates, confirm or edit similar-term groups, and use Undo as needed. The visible match count follows the selected level and saved decisions.
3. Select **Preview & export obfuscated copy**. Inspect the generated placeholders, occurrence counts, bounded output preview, and coverage warnings. Existing placeholder-like strings and unsupported Office/XLSX content require explicit acknowledgement.
4. Select **Approve and save new version** to write a distinct obfuscated version under `.blot/outputs/` and download it for manual use. The original remains unchanged. Random placeholders are collision-checked; the term map is written only to encrypted project state, never to the output package.
5. Use the version selector to inspect the obfuscated copy or download it again. Approval previews expire after 15 minutes and are rejected if the source bytes or version-scoped review graph changed.

## Restore a returned Office file

1. Select the associated **Obfuscated** DOCX, PPTX, or XLSX version.
2. Choose the returned file and review the restored output preview and unresolved-token report.
3. Save a separate **Restored** version. Only exact case-sensitive tokens mapped to that project document/version are restored. Moved and repeated intact tokens work; changed, unknown, and foreign-project tokens remain untouched and are reported.

The returned file and original are not overwritten. Restoration reports are stored in the encrypted project state and remain available when reopening the restored version.

## Encrypted backup and idle lock

Use **Encrypted backup** to create a portable `.blotbackup` using a passphrase of at least 12 characters, or restore one into an empty project folder. The passphrase is not stored; losing it makes that backup unrecoverable. The archive includes project versions and private mappings and is protected with scrypt-derived AES-GCM encryption. Successful restore creates a fresh project key in the destination computer's OS credential store.

The local project session locks after 15 minutes without user activity. Reopening calls the OS credential-store provider before project state is made available again. Keychain/Credential Manager consent behavior depends on OS account configuration and must be verified on the target machine.

## One-click setup and launch

- **macOS:** run `setup-macos.command` once, then `launch-macos.command` to start Blot in a terminal and open the browser. Closing/stopping the launcher stops the service.
- **Windows:** double-click `setup-windows.bat` once, then `launch-windows.bat`. The launcher opens the browser and stops the local Python service when the launcher exits. The `.ps1` scripts are also available for PowerShell.

Both launchers bind the service to loopback. macOS setup installs the Apple-Silicon MLX runtime; Windows setup installs the PyTorch runtime. MiniLM inference is explicitly CPU-only on both platforms. Model weights remain separate, opt-in downloads. Cross-platform clean-install and OS credential-prompt acceptance is still required before release.

Only adapter-supported editable text is covered. Coverage warnings are not proof that all sensitive content was found, and the app does not sanitize images/OCR, metadata, macros, embedded binaries, or unknown package surfaces.

For production-like local serving on Apple-Silicon macOS, run `uv sync --inexact --extra minilm-macos`, then `npm run build` and `uv run --extra minilm-macos python -m backend.app`. On Windows, use `uv sync --inexact --extra minilm-windows` and `uv run --extra minilm-windows python -m backend.app`. Visit `http://127.0.0.1:8765/obfuscation-workspace.html`.

## Local checks

```bash
# Apple-Silicon macOS; on Windows use --extra minilm-windows instead
uv run --extra minilm-macos pytest
uv run --extra minilm-macos ruff check backend
npm test
npm run build
npm audit --omit=dev
```

## Build and preview the UI prototype

```bash
npm run build
npm run preview
```

Stages 6–9 now include connected review/export/restoration workflows, encrypted backup/restore, idle locking, resumable model artifact downloads, and macOS/Windows launch/setup scripts. Stage 10 has a synthetic-data end-to-end acceptance foundation. Real OS credential prompts and installation, model inference/resource limits, browser keyboard/responsive acceptance, human pilot, and 100 MB performance gates remain before release; see `IMPLEMENTATION_PLAN.md`.

## Command to Start the App
```
uv sync --extra dev --extra minilm-macos
npm ci
npm run dev:app
```
