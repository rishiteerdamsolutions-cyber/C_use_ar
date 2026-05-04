# Trainer UI — tabs, panels, and major elements

The canonical UI is **`assets/TRAINER.html`** (~5.6k lines: inline CSS + HTML + large `<script>`). Below is a structural map.

## Header (`<header>`)

- **Logo:** text `cusear™`
- **`#api-key-inp`** — client API key (password field); **Save** → `saveTrainerApiKey()`
- **`#server-status`** / **`#dot`** / **`#status-text`** — health polling `GET ${API}/health`
- **`#countdown`** — run countdown when applicable
- **`#start-btn`** — START/STOP; `handleStartStopButton()`
- **Export app** — opens `#export-desktop-modal` (Mac/Windows build for selected ar™)
- **Help & Permissions** — shows `#permissions-gate`

## Global overlays / banners

| Id | Role |
|----|------|
| `#export-desktop-modal` | Desktop export wizard |
| `#entitlement-banner` | Plan / entitlement messaging |
| `#trainer-run-activity-banner` | “Run in progress” notice |
| `#trainer-automation-notice-modal` | First-time automation warning |
| `#permissions-gate` | Accessibility / permissions onboarding |

## Tab bar (`.tabs` → `switchTab(...)`)

| Tab label | Panel id | Purpose |
|-----------|----------|---------|
| Step Builder | `#panel-build` | Workflow name, WhatsApp notify, templates, saved steps list, join workflow, add-step form |
| Website | `#panel-website` | `#step-builder-shell-website` (moved builder dock) + website form |
| AI Media Studio | `#panel-ai-media` | Topic/industry, generate images/videos queue |
| automation | `#panel-automation` | Scheduler, snake-grid append, per-workflow step hints |
| ar™ | `#panel-ar` | Bundles: pick workflows, notify modes, schedule, run now |
| Run | `#panel-run` | Pick workflow, fast/smart, dry run, log |
| Best AI™ | `#panel-best-ai` | Query, slots, synthesizer + `#step-builder-shell-best-ai` |
| Saved | `#panel-workflows` | Clone/rename/delete list |

## Step Builder (`#panel-build`) — key controls

- **`#wf-name`** — workflow name; drives load/save
- **`#build-whatsapp-number`** — per-workflow notify phone
- **`#steps-card`** / **`#step-list`** — rendered steps; edit/insert/delete
- **`#action-type`** — `<select>` of all `action_type` values (see `STEP_TYPES_REFERENCE.md`)
- Conditional rows: `#desc-row`, `#type-text-row`, `#type-focus-row`, `#ai-prompt-row`, `#ai-model-row`, `#url-row`, `#wait-seconds-row`, `#add-wait-after-row`, `#tab-count-row`, `#automation-run-range-row`, `#grid-nav-snake-row`, `#shell-command-row`, `#best-ai-step-slot-row`, `#hotkey-keys-row`, `#screenshot-section`, `#live-vision-at-run`
- **`#add-btn`** → `addStep()` — `POST /teach/step` or `POST …/step/{n}/update`
- **`#join-source-select`** / **`#join-workflow-btn`** — append another workflow

## Run panel (`#panel-run`)

- **`#run-wf-select`**, **`#run-steps-preview`**, **`#dry-build`** vs run panel’s **`#dry-run`**
- **`#run-log`**, **`#run-prog-wrap`**, mode buttons `setMode(…,'fast'|'smart')`
- **`POST ${API}/run`** with JSON body

## JavaScript modules (logical) inside single file

- API helpers: `mergeTrainerFetchOpts`, `trainerAuthHeaders`, `checkServer`
- Workflow: `loadWorkflowSteps`, `renderStepList`, `addStep`, `editStep`, `deleteStep`, `joinWorkflowSteps`
- Constants: `ACTION_DEFAULTS`, `TRAINER_REPEAT_ACTION_TYPES`, `TRAINER_ARROW_LABELS_UI`, `TRAINER_HOTKEY_MAX_KEYS`
- Automation: `saveAutomationSettings`, `reloadAutomationSettings`, snake grid append, campaign day UI (large block)
- ar™: `loadArWorkflowDropdown`, `bundleArFromSelected`, `runArNow`, schedule controls
- Best AI: bridge poll, `runBestAiSynthesize`, slot fields
- Website: `WEBSITE_INDUSTRIES`, `createWebsiteBuild`, form validation
- Desktop export: polling job status

## Docking behavior

`#step-builder-dock` is the primary shell; when switching to Website or Best AI tabs, scripts may move the dock into `#step-builder-shell-website` or `#step-builder-shell-best-ai` so the builder stays accessible.

## Session storage

- `trainer_last_workflow` (`TRAINER_WF_STORAGE_KEY`) — remember last loaded workflow name

For every `fetch` target, grep `assets/TRAINER.html` for `` `${API}/` ``.
