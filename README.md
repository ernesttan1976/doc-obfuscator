# Blot — Local Document Obfuscation

This repository contains the OpenDesign React prototype and the approved implementation roadmap in [`IMPLEMENTATION_PLAN.md`](./IMPLEMENTATION_PLAN.md). The product target is a local-only browser app backed by FastAPI. Stages 2–4 add encrypted project state, immutable source intake, and local document previews for TXT, MD, CSV, XLSX, DOCX, and PPTX. Stage 5 provides deterministic pattern candidates, manual phrase candidates, encrypted per-version graph storage, unconfirmed RapidFuzz proposals, and an optional pinned GLiNER NER model whose weights download only after explicit user confirmation.

## Run the current UI prototype

```bash
npm ci
npm run dev
```

Vite opens the preserved workspace at `http://localhost:5173/obfuscation-workspace.html`.

## Run the local API and project storage

In one terminal, create the Python environment and start FastAPI:

```bash
uv sync --extra dev
uv run python -m backend.app
```

To install the optional local NER runtime, run `uv sync --extra models`. This installs Python packages only; model weights and tokenizer assets remain absent until explicitly downloaded from the saved-document review panel.

In a second terminal, run `npm run dev`. Vite proxies `/api` to the loopback-only service at `127.0.0.1:8765`. The API provides `/api/health`, a launch-scoped `/api/bootstrap` token, protected project create/open/import/preview/candidate-analysis/candidate-decision/candidate-group endpoints, explicit local-model status/download/cancel endpoints, and native folder/document pickers.

Use **Add or open project** in the sidebar to create or open a project. Select **Import file** to choose DOCX, PPTX, TXT, MD, CSV, or XLSX sources; each file is copied read-only into `.blot/originals/` and recorded as an original version in `.blot/project.sqlite3`. Files over 100 MB are rejected. Project graph/map state uses AES-GCM encryption with its random data key in the OS credential store.

Opening a saved TXT or MD shows a local text preview with UTF-8/UTF-16 encoding and line-ending metadata. CSV previews retain the detected delimiter, quoting, rows, and columns; ambiguous or inconsistent dialects are rejected. XLSX previews read literal shared-string and inline-string cells across worksheets, preserve formulas and workbook parts, and flag detected unsupported text-bearing parts. DOCX/PPTX previews scan WordprocessingML and DrawingML paragraph text across package XML parts, including split runs, tables, headers/comments, slides, and notes; exact-text adapter replacements preserve run formatting and leave untouched package parts intact. Bounded coverage reports list examined XML parts, skipped non-XML parts, and detected unsupported parts; saved-document review surfaces these warnings and per-format coverage counts beside the preview, plus bounded PPTX slide/DOCX text-part navigation and a dense text mode. DOCX page layout is not inferred from raw package XML. Images/OCR, macros, embedded binary content, external relationship targets, document metadata, and text outside the supported paragraph XML elements are not processed. Candidate analysis always uses local patterns for email-like values, phone-like values, dates, identifiers, and capitalized phrases; once installed, the optional GLiNER model adds bounded entity suggestions with labels and confidence. Its Apache-2.0 artifacts are pinned to a fixed revision and verified before local loading. The saved-document UI supports review breadth, manual phrase selection, Include/Exclude with Undo, review-status counts, proposal review, and explicit group operations. Candidate data is stored in the encrypted project graph; RapidFuzz edges are suggestions until the user confirms a group. MiniLM contextual similarity, obfuscation, export, and product restoration remain pending. Previews are bounded; placeholder-like text is counted without exposing it in diagnostics. Demo documents stay labeled as unsaved.

For production-like local serving, run `npm run build` and then `uv run python -m backend.app`. Visit `http://127.0.0.1:8765/obfuscation-workspace.html`.

## Local checks

```bash
uv run pytest
uv run ruff check backend
npm test
npm run build
npm audit --omit=dev
```

## Build and preview the UI prototype

```bash
npm run build
npm run preview
```

Later stages add candidate discovery and review, obfuscation/export, Office restoration, idle relock, encrypted backup, and release launchers. See the implementation plan for stage gates and the exact accepted scope.
