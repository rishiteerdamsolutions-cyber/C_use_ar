# Step types (`action_type`) — complete reference

All steps are JSON objects inside `workflow.steps[]`. The runner (`dashboard.py` → `run_workflow`) uses `step.get("action_type") or step.get("action", "click")`.

## Common step fields (any type)

| Field | Type | Meaning |
|-------|------|---------|
| `step` | int | 1-based order |
| `action_type` | string | Dispatcher key (this document) |
| `description` | string | Human label / click instruction / notes |
| `status` | string | e.g. `saved`, `analysed`, `live_vision_run`, `saved_no_vision` |
| `automation_run_min` | int or empty | Optional: only run on scheduler run # ≥ this |
| `automation_run_max` | int or empty | Optional: only run on scheduler run # ≤ this |
| `screenshot` | string | Filename under `screenshots/` for trained click image |

## `click`

**Purpose:** Move mouse and click at trained coordinates, or use **live vision** on a fresh screenshot at run time.

| Field | Meaning |
|-------|---------|
| `x`, `y` | Pixel coordinates (from vision on training image, or 0,0 for live-only) |
| `live_vision` | bool — if true, capture screen at run and resolve target from `description` |
| `description` | Element to find (vision prompt) |

**Local:** PyAutoGUI click; live path uses screen capture + vision API. **Teach:** multipart file field `screenshot` optional if `live_vision` + description.

## Window / app

| `action_type` | Runtime behavior (local) |
|----------------|---------------------------|
| `minimize` | macOS: Cmd+M; Windows: Win+Down |
| `maximize` | macOS: Ctrl+Cmd+F; Windows: Win+Up |
| `open_chrome` | `open -a Google Chrome` / `start chrome` / `google-chrome` |
| `close_chrome` | Quit Chrome (AppleScript / taskkill / pkill) |
| `open_cursor` | Launch Cursor app |

## Navigation / keys

| `action_type` | Fields | Behavior |
|----------------|--------|----------|
| `open_url` | `url` | Open URL in browser (prefers Chrome); supports token substitution |
| `open_tab` | `url` optional | Cmd/Ctrl+T; if URL, types URL + Enter |
| `open_whatsapp` | — | Opens fixed WhatsApp Web URL |
| `press_enter` | `description` | `enter` / `return` / `command+return` via `TRAINER_PRESS_ENTER_MODE` |
| `press_home` | `description` | Home key |
| `press_space` | `description` | Space (with optional Chrome activation for WhatsApp flows) |
| `press_tab` | `tab_count`, `direct_jump`, optional direct-jump trained `x`,`y` | Tab N times **or** single click at trained coords |
| `press_arrow_left` / `press_arrow_right` / `press_arrow_up` / `press_arrow_down` | `tab_count` (reused as repeat count), optional `repeat_scale_campaign_day`, `tab_press_increment` | Arrow key repeats; scaling uses `CURRENT_AUTOMATION_RUN` |
| `press_automation_grid_nav` | `grid_nav_cols`, `grid_nav_rows` | Snake pattern over a virtual grid keyed by automation run index |
| `hotkey` | `hotkey_keys` (array of strings) | `pyautogui.hotkey(*keys)` |
| `copy` / `paste` | `description` | Cmd/Ctrl+C or V |

## Timing / shell

| `action_type` | Fields | Behavior |
|----------------|--------|----------|
| `wait` | `wait_seconds`, optional note in `description` | Sleep up to 120s (interruptible) |
| `shell` | `shell_command` | Subprocess with allowlist / `TRAINER_ALLOW_SHELL`, `TRAINER_SHELL_UNRESTRICTED`, etc. |

## Typing / AI text / media

| `action_type` | Fields | Behavior |
|----------------|--------|----------|
| `type` | `type_text`, optional `focus_target` | Token-expand text; optional live-vision click before Cmd+A, Cmd+V style typing |
| `ai_type` | `ai_prompt`, optional `ai_model`, optional `focus_target` | OpenAI generates text then types |
| `ai_image` | `ai_prompt`, optional `ai_model` | Generates 1080×1350 infographic; copies path to clipboard; sets runtime vars |
| `type_project_name` | optional `focus_target` | Types workflow display name |
| `type_whatsapp_number` | optional `focus_target` | Types digits from workflow notify number |
| `type_completion_message` | optional `focus_target` | Rebuilds WhatsApp completion body for this run; clipboard + paste behavior controlled by env |
| `type_image_text_caption` / `type_video_text_caption` | optional `focus_target` | Types `{{CURRENT_CAPTION}}` after an `upload` bind |
| `upload` | `description` | **Marker:** binds next AI-media queue item to `CURRENT_*` runtime vars; user completes file picker manually |

## WhatsApp completion

| `action_type` | Behavior |
|----------------|----------|
| `completion_link` | Builds message + `https://web.whatsapp.com/send?phone=…&text=…` from run results; copies URL |
| `completion_message` | Same body text in memory / log; optional clipboard via `TRAINER_COMPLETION_MESSAGE_CLIPBOARD_ON_STEP` |
| `completion_clipboard_refresh` | Re-copies `WHATSAPP_COMPLETION_TEXT` to clipboard after another step overwrote it |

## Best AI™ bridge

| `action_type` | Behavior |
|----------------|----------|
| `best_ai_copy_query_bundle` | Clipboard ← topic + platform instructions |
| `best_ai_capture_slot_from_clipboard` | `best_ai_slot`: `chatgpt` \| `gemini` \| `claude` — reads clipboard into bridge JSON |
| `best_ai_run_synthesizer` | OpenAI merge/judge over bridge slots |

## Constants mirrored in UI (`TRAINER.html`)

- `TRAINER_REPEAT_ACTION_TYPES`: `press_tab`, `press_arrow_*` — share numeric `tab_count`.
- `TRAINER_ARROW_LABELS_UI`: human labels for arrow types.
- `_TRAINER_ARROW_PY_KEYS` in `dashboard.py`: maps to PyAutoGUI key names `left`/`right`/`up`/`down`.

## Dry run

When `dry_run=True`, `run_workflow` prints what would happen and appends synthetic results; no PyAutoGUI clicks (except shell still may be skipped — check code for shell in dry run: shell block uses `_trainer_run_shell_step` only when not dry — actually dry_run continues before pyautogui import path with `continue` early in loop).

## Skip by automation window

If `automation_run_min` / `automation_run_max` set and `CURRENT_AUTOMATION_RUN` is outside range, step is **skipped** (manual runs: min/max ignored for skip — see `_trainer_step_skipped_automation_run_range` in `dashboard.py`).
