# Blot — Local Document Obfuscation

This repository contains the OpenDesign React prototype and the approved implementation roadmap in [`IMPLEMENTATION_PLAN.md`](./IMPLEMENTATION_PLAN.md). The product target is a local-only browser app backed by FastAPI; the current UI is still a demo and does not yet process Office files or persist project graphs.

## Run the current UI prototype

```bash
npm ci
npm run dev
```

Vite opens the preserved workspace at `http://localhost:5173/obfuscation-workspace.html`.

## Run the Stage 1 local API shell

In one terminal, create the Python environment and start FastAPI:

```bash
uv sync --extra dev
uv run python -m backend.app
```

In a second terminal, run `npm run dev`. Vite proxies `/api` to the loopback-only service at `127.0.0.1:8765`. The API provides `/api/health`, a launch-scoped `/api/bootstrap` token, a protected session endpoint, and protected project create/open endpoints. The current demo UI is not yet connected to project storage.

Use **Add or open project** in the sidebar to invoke the macOS/Windows native folder picker, then create or open the project. Project state uses an AES-GCM encrypted sidecar with the data key stored in the OS credential store. Project metadata tables are initialized in `.blot/project.sqlite3`. The sample documents remain clearly marked as unsaved demo content; persistent document import follows in Stage 3.

For production-like local serving, run `npm run build` and then `uv run python -m backend.app`. Visit `http://127.0.0.1:8765/obfuscation-workspace.html`.

## Stage 1 checks

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
