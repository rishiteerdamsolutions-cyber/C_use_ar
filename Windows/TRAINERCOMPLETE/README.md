# TRAINERCOMPLETE — Rebuild kit for cusear™ Trainer

This folder is a **self-contained specification + code snapshot** of the Trainer as shipped in this repository (root `TRAINER.html` + local `dashboard.py` server + cloud `agency_api` trainer). Hand the whole folder to another model or engineer to recreate the same **behavior, API contract, step taxonomy, and UI structure** (visual design can differ).

## Contents

| Path | Purpose |
|------|---------|
| `README.md` | This index |
| `REBUILD_SPEC.md` | End-to-end architecture and rebuild checklist |
| `STEP_TYPES_REFERENCE.md` | Every workflow step `action_type` and runtime behavior |
| `API_AND_DATA.md` | HTTP endpoints, multipart fields, storage (Mongo vs JSON files) |
| `UI_PANELS.md` | Tabs, panels, major DOM ids, user flows |
| `RUNTIME_AND_ENV.md` | Environment variables and runtime tokens |
| `WORKFLOW_JSON_EXAMPLE.json` | Example saved workflow document |
| `DASHBOARD_ANCHORS.md` | Line-level pointers into `dashboard.py` for local server + `run_workflow` |
| `RUNTIME_AND_ENV.md` | Tokens + important environment variables |
| `assets/TRAINER.html` | Full copy of production Trainer UI (single-file app) |
| `code/agency_api/trainer_service.py` | Cloud trainer: teach + Mongo workflows + dry run |
| `code/agency_api/routes/trainer.py` | FastAPI router mounting under `/api/trainer` |

## Two deployment modes (critical)

1. **Local Trainer** — Open `TRAINER.html` while `python3 dashboard.py` serves **port 7788**. The UI sets `API` to `http://localhost:7788` (no `/api/trainer` prefix). **Live mouse/keyboard**, file workflows under `workflows/`, screenshots, automation, ar™, campaigns, etc. are implemented in **`dashboard.py`** (large file; not duplicated here — see `REBUILD_SPEC.md` for anchors).

2. **Cloud Trainer** — Same HTML on a deployed site: `API` becomes `{origin}/api/trainer`. **`agency_api`** provides list/get/delete workflows, teach step (multipart), join, **run is validation-only (forced dry run)**. No real desktop automation in the cloud.

## Quick rebuild order

1. Read `REBUILD_SPEC.md` then `STEP_TYPES_REFERENCE.md`.
2. Implement API contract from `API_AND_DATA.md` (or wire FastAPI router from `code/`).
3. Port UI from `assets/TRAINER.html` or rebuild from `UI_PANELS.md`.
4. Implement `run_workflow` in a local server mirroring `dashboard.py` semantics for each `action_type`.
