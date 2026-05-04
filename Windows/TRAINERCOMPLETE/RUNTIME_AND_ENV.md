# Runtime variables and environment variables

## Runtime tokens (expanded in `dashboard.py` → `_resolve_runtime_tokens`)

Commonly referenced in step text / AI prompts / URLs:

| Token | Meaning |
|-------|---------|
| `{{WORKFLOW_NAME}}` | Workflow key / name |
| `{{PROJECT_FOLDER_NAME}}` | Alias / project folder context |
| `{{LAST_TYPED_TEXT}}` | Previous type output |
| `{{CURRENT_TOPIC}}` / `{{TOPIC_SLOT}}` | Topic for AI / social |
| `{{CURRENT_CAPTION}}` | After `upload` binds AI media item |
| `{{CURRENT_CAPTION_PATH}}` | Path to caption file if exported |
| `{{CURRENT_IMAGE_PATH}}` / `{{CURRENT_VIDEO_PATH}}` / `{{CURRENT_MEDIA_PATH}}` | Bound media paths |
| `{{WHATSAPP_COMPLETION_URL}}` | From `completion_link` |

Scheduler / campaign injects may include:

- `CURRENT_AUTOMATION_RUN` — 1-based scheduler run counter
- `CURRENT_CAMPAIGN_DAY` — campaign day index (when using campaign features)
- `RUN_SOURCE` — `manual_run` vs automation labels

## Environment variables (non-exhaustive — see `dashboard.py` and hints in `TRAINER.html`)

### Vision / clicks

- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` — vision for teach + live clicks
- `TRAINER_LIVE_VISION_CLICKS` — force live vision on every click
- `TRAINER_LIVE_VISION_DELAY` — delay before capture
- `TRAINER_ACTIVATE_APP` — app name to foreground before clicks
- `TRAINER_AI_TYPE_MODEL` — default for `ai_type`

### Typing / WhatsApp

- `TRAINER_ACTIVATE_APP_BEFORE_TYPE`
- `TRAINER_TYPE_FOCUS_DELAY`, `TRAINER_TYPE_FOCUS_CLICK_DELAY`
- `TRAINER_PRESS_ENTER_MODE` — `enter` \| `return` \| `cmd_enter` (Darwin)
- `TRAINER_WHATSAPP_NOTIFY_NUMBER` — fallback notify digits
- `TRAINER_COMPLETION_MESSAGE_CLIPBOARD_ON_STEP`
- `TRAINER_COMPLETION_MESSAGE_PASTE_ONLY`
- `TRAINER_WHATSAPP_FOCUS_COMPOSE`, `TRAINER_WHATSAPP_FOCUS_BEFORE_PASTE`, `TRAINER_WHATSAPP_COMPOSE_FOCUS_WAIT`
- `TRAINER_ACTIVATE_CHROME_BEFORE_WHATSAPP_STEPS`
- `TRAINER_OPEN_URL_REUSE_CHROME_WINDOW` / per-step `reuse_chrome_window` in JSON

### Shell

- `TRAINER_ALLOW_SHELL=1` — enable shell steps
- `TRAINER_SHELL_ALLOWLIST`, `TRAINER_SHELL_UNRESTRICTED`

### Consumer / mode

- `AGENCY_USER_MODE` — `consumer` vs `trainer` (permissions gate)
- `CUSEAR_DEFAULT_AR_SLUG` — locks consumer mode for shipped desktop bundles

### Best AI

- `BEST_AI_SYNTH_MODEL` — default `gpt-4o-mini`

### Misc trainer tuning

- `TRAINER_TAB_INTERVAL` — delay between repeated arrow / grid-nav key presses (see `run_workflow` grid branch)

Search `os.environ.get` in `dashboard.py` for the authoritative full set.
