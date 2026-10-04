# Blot — Local Document Obfuscation

This repository contains the OpenDesign React prototype and the approved implementation roadmap in [`IMPLEMENTATION_PLAN.md`](./IMPLEMENTATION_PLAN.md). The product target is a local-only browser app backed by FastAPI. Stage 2 adds encrypted project state and immutable source intake; document parsing and processing remain later-stage work.

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

In a second terminal, run `npm run dev`. Vite proxies `/api` to the loopback-only service at `127.0.0.1:8765`. The API provides `/api/health`, a launch-scoped `/api/bootstrap` token, protected project create/open/import endpoints, and native folder/document pickers.

Use **Add or open project** in the sidebar to create or open a project. Select **Import file** to choose DOCX, PPTX, TXT, MD, CSV, or XLSX sources; each file is copied read-only into `.blot/originals/` and recorded as an original version in `.blot/project.sqlite3`. Files over 100 MB are rejected. Project graph/map state uses AES-GCM encryption with its random data key in the OS credential store. Imported originals are not parsed or modified yet; text preview and processing are Stage 3. Demo documents stay labeled as unsaved.

For production-like local serving, run `npm run build` and then `uv run python -m backend.app`. Visit `http://127.0.0.1:8765/obfuscation-workspace.html`.

## Local checks

```bash
uv run pytest
npm run build
npm audit --omit=dev
```

## Build and preview the UI prototype

```bash
npm run build
npm run preview
```

The planned end state adds a FastAPI local service, user-selected project folders, encrypted local graphs/maps, DOCX/PPTX/XLSX processing, and macOS/Windows launchers. See the implementation plan for stage gates and the exact accepted scope.
