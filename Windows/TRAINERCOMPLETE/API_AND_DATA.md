# HTTP API and data contracts

Paths below are **relative to `API`** (see `REBUILD_SPEC.md`). On cloud, prefix is `/api/trainer`; on local `:7788`, routes are at server root.

## Health & mode

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Trainer liveness |
| GET | `/mode` | Consumer vs trainer mode flags |
| POST | `/permissions/trial` | Permissions probe (desktop) |

## Workflows CRUD

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/workflows` | List `{ name, total_steps }[]` |
| GET | `/workflow/{name}` | Full workflow JSON |
| DELETE | `/workflow/{name}` | Delete workflow |
| DELETE | `/workflow/{name}/step/{n}` | Delete step; renumber |
| POST | `/workflow/{name}/join` | JSON `{ source_workflow }` — append clone of source steps |
| POST | `/workflow/{name}/rename` | Rename workflow |
| POST | `/workflow/{name}/clone` | Clone to new name |
| POST | `/workflow/{name}/step/{n}/update` | Same multipart as teach, replaces step |

## Teach (add step)

| Method | Path | Body |
|--------|------|------|
| POST | `/teach/step` | `multipart/form-data` |

### Multipart fields (trainer → server)

| Field | When required | Meaning |
|-------|----------------|---------|
| `workflow_name` | always | Target workflow |
| `action_type` | always | Step type string |
| `description` | most non-text types | Click label / notes |
| `type_text` | `type` | Literal typed text |
| `ai_prompt` | `ai_type`, `ai_image` | Model instruction |
| `ai_model` | optional | OpenAI model override |
| `focus_target` | optional | Live-vision hint before typing |
| `url` | `open_url`, `open_tab` | URL string |
| `wait_seconds` | `wait` | Float 0–120 |
| `tab_count` | tab / arrow repeat types | Int 1–200 |
| `direct_jump` | `press_tab` | `1` / `0` |
| `direct_jump_screenshot` | with direct_jump | Image file |
| `repeat_scale_campaign_day` | arrow types | `1` enables scaling |
| `tab_press_increment` | arrow types | Int add per scheduled run |
| `shell_command` | `shell` | Command string |
| `grid_nav_cols`, `grid_nav_rows` | `press_automation_grid_nav` | Ints |
| `hotkey_keys_json` | `hotkey` | JSON array of key name strings |
| `best_ai_slot` | `best_ai_capture_slot_from_clipboard` | `chatgpt` \| `gemini` \| `claude` |
| `live_vision` | `click` | `1` if checkbox |
| `screenshot` | `click` (non-live) | PNG/JPG file |
| `insert_after` | optional | Insert new step after step # (stringified int) |
| `automation_run_min`, `automation_run_max` | optional | Scheduler window |
| `add_wait_after` | optional | If `1`, server may append auto wait step after save |

## Run

| Method | Path | JSON body |
|--------|------|-----------|
| POST | `/run` | `{ "workflow_name", "dry_run"?, "mode"?: "smart"\|"fast" }` |

**Cloud:** response includes only dry-run validation; `coerced_to_dry_run` may be true.

**Local:** executes `run_workflow` on `workflows/{name}.json`.

## Run control

| Method | Path |
|--------|------|
| POST | `/run/stop` |
| GET | `/run/status` |

## Automation (per workflow)

| Method | Path |
|--------|------|
| GET | `/workflow/{name}/automation` |
| POST | `/workflow/{name}/automation` |

## Trainer-wide automation summary

| Method | Path |
|--------|------|
| GET | `/trainer/automation-summary` |

## ar™ bundles

| Method | Path |
|--------|------|
| GET | `/bundles` |
| POST | `/bundle` |
| GET | `/bundle/{slug}` |
| POST | `/bundle/{slug}/run` |

## Best AI

| Method | Path |
|--------|------|
| GET/POST | `/best-ai/ui-bridge` |
| POST | `/best-ai/synthesize` |

## AI Media Studio

| Method | Path |
|--------|------|
| POST | `/ai-media/start` |
| GET | `/ai-media/status` |
| POST | `/ai-media/stop` |

## Website builder

| Method | Path |
|--------|------|
| POST | `/website/build/basic` |
| POST | `/website/build/admin` |

## Campaign / media

| Method | Path |
|--------|------|
| GET | `/media/list` |
| POST | `/media/upload` |
| GET | `/workflow/{wf}/campaign` |
| POST | `/campaign/create`, `/campaign/generate-batch`, `/campaign/generate-day`, `/campaign/review`, `/campaign/validate`, `/campaign/assign-uploaded` |

## Desktop export

| Method | Path |
|--------|------|
| GET | `/export-desktop-ar/capabilities` |
| POST | `/export-desktop-ar` |
| GET | `/export-desktop-ar/job?id=` |
| GET | `/export-desktop-ar/download?id=` |

## Storage

- **Local:** `workflows/<name>.json`, screenshots beside workflow images, `sessions/best_ai/ui_bridge.json`.
- **Cloud:** MongoDB collection `Collections.TRAINER` — documents `{ name, owner_id, data: <workflow json> }`.

## Headers

- `X-API-Key`: optional locally; required when `TRAINER_REQUIRE_API_KEY` in cloud.
- `Content-Type: multipart/form-data` for teach and media upload.
