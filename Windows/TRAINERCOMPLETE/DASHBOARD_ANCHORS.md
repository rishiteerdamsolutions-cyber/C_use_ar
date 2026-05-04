# `dashboard.py` — where to read implementation

Line numbers are approximate to the snapshot in this repo; use search if they drift.

## Core execution

| Symbol | ~Line | Role |
|--------|-------|------|
| `run_workflow` | 5212 | Main step loop: dry run branches, `shell`, Best AI, `upload`, `ai_image`, PyAutoGUI actions, typing block, clicks |
| `_trainer_order_steps_for_run` | 5087 | Sort steps by `step` field |
| `_trainer_step_skipped_automation_run_range` | 307 | Skip step when outside scheduler run window |
| `_trainer_repeat_press_count` | 351 | Arrow/tab repeat count with campaign scaling |
| `_trainer_grid_snake_nav_press_plan` | 218 | Snake path for `press_automation_grid_nav` |
| `_trainer_run_shell_step` | 622 | Shell allowlist execution |

## HTTP routing (`Handler` / `do_POST` / `do_GET`)

Search for path string literals. Examples from grep:

| Path fragment | ~Line | Notes |
|---------------|-------|------|
| `/teach/step` | 7068 | Multipart teach / add step |
| `/workflow/.../step/.../update` | 7492 | Edit step |
| `/workflow/` GET/DELETE | 6951+ | Load / delete workflow |
| `/workflow/.../automation` | 6927, 8311 | GET/POST schedule |
| `/workflow/.../join` | 8447 | Append source workflow |
| `/workflow/.../rename` | 8501 | Rename |
| `/workflow/.../clone` | 8557 | Clone |
| `/workflow/.../campaign` | 6908 | Campaign JSON |

Also search for: `/run`, `/run/stop`, `/run/status`, `/bundles`, `/bundle`, `/ai-media/`, `/best-ai/`, `/website/build/`, `/export-desktop-ar`, `/campaign/`, `/media/`.

## Trainer constants

| Symbol | ~Line |
|--------|-------|
| `_TRAINER_TAB_COUNT_ACTIONS` | 98 |
| `_TRAINER_ARROW_PY_KEYS` | 107 |
| `_TRAINER_HOTKEY_MAX_KEYS` | 129 |
| `_trainer_parse_hotkey_keys_json` | 145 |

## Vision / clicks

Search: `analyse_screenshot_for_click`, `_trainer_use_live_vision_click`, `_capture_screen_png`, `_analyse_click_with_retries`.
