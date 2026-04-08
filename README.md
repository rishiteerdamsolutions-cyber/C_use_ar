# Autonomous Web Agency Agent v1.0
### Karimnagar, Telangana — Enterprise-Grade Desktop Automation

> One command → complete website delivered to client  
> `"Build salon website for Ritu"` → Cursor → GitHub → MongoDB → Vercel → WhatsApp URL

---

## Architecture Overview

```
L1 Remote Config   Firebase RTDB → local JSON cache (auto-heal all users)
L2 Workflow Engine JSON recipes → state machine (PENDING → SUCCESS / RETRY / HUMAN)
L3 Vision Engine   Screenshot → Claude Vision → x,y coordinates (no hardcoding)
L4 Execution Engine pyautogui mouse/keyboard with FAILSAFE always on
L5 Fallback Manager 4-attempt cascade → AI recovery → human popup
L6 Session Recorder JSON proof-of-delivery + failure analytics
```

---

## Quick Start

### 1. Prerequisites
- Python 3.11+ (Windows or macOS)
- Internet connection
- Cursor AI installed
- GitHub account
- Vercel account
- MongoDB Atlas account

### 2. Install dependencies
```bash
pip install -r requirements.txt --break-system-packages
```

### 3. Store your credentials (one-time setup)
```bash
python main.py --setup
```
You will be prompted for:
- Gmail address
- GitHub Personal Access Token
- OpenAI API key
- Gemini API key
- Anthropic API key
- Vercel domain prefix
- Phone number

All credentials stored securely in your OS keychain. **Never written to disk in plaintext.**

### 4. Set Firebase URLs (optional but recommended)
```bash
export FIREBASE_CONFIG_URL="https://your-project.firebaseio.com/config.json"
export FIREBASE_UPDATE_URL="https://your-project.firebaseio.com/updates.json"
```

### 5. Health check
```bash
python main.py --check
```

### 6. Run the agent
```bash
python main.py
```

Then type your command:
```
Agency Agent ▶  Build salon website for Priya Beauty Parlour
```

---

## Project Structure

```
autonomous-web-agency/
│
├── main.py                    ← Entry point & orchestrator
├── VERSION                    ← Current version (1.0.0)
├── requirements.txt
├── pytest.ini
│
├── config/
│   └── remote_config.py       ← Firebase fetch + local cache fallback
│
├── vision/
│   └── vision_engine.py       ← Screenshot + Claude/GPT-4o element finder
│
├── execution/
│   ├── executor.py            ← Mouse, keyboard, clipboard, windows
│   └── fallback.py            ← 4-attempt cascade fallback manager
│
├── analytics/
│   └── session.py             ← Step logger + success rate + JSON proof
│
├── ai/
│   └── validator.py           ← GPT-4o ↔ Gemini prompt validation loop
│
├── security/
│   └── credentials.py         ← Keychain storage + AES-256 cache + log redaction
│
├── updater/
│   └── auto_update.py         ← Firebase version check + delta download + rollback
│
├── templates/
│   └── salon/
│       ├── template.json      ← Template metadata, sections, tech stack
│       └── prompt.txt         ← Complete Cursor AI prompt (Next.js + Tailwind)
│
├── tests/
│   ├── test_remote_config.py
│   ├── test_session.py
│   ├── test_auto_update.py
│   └── test_security.py
│
├── sessions/                  ← Session JSON files (auto-created)
├── screenshots/               ← Vision engine screenshots (auto-created)
└── logs/                      ← agent.log (auto-created)
```

---

## Safety Features

| Feature | Detail |
|---|---|
| FAILSAFE | Move mouse to top-left corner → immediate stop |
| Confidence gate | Agent refuses clicks below 70% confidence |
| Zero credential logging | CredentialRedactFilter on all loggers |
| Keychain storage | OS keychain (macOS Keychain / Windows Credential Manager) |
| AES-256 cache | Encrypted local fallback — never plaintext |
| Human approval | OTP steps pause and wait for user confirmation |
| Circuit breaker | 3 API failures → 60s pause → retry |
| Backup before update | Previous version kept 7 days for rollback |

---

## Running Tests

```bash
pytest tests/ -v
```

Expected output:
```
tests/test_remote_config.py ........   PASSED
tests/test_session.py ...........      PASSED
tests/test_auto_update.py .......      PASSED
tests/test_security.py .......        PASSED
```

---

## Business Model

| Tier | Price | Use Case |
|---|---|---|
| V1 | Free (beta) | 50 users, collect testimonials |
| V2 | ₹999/website | Pay per delivery, Razorpay |
| V3 | ₹2,999/month | Unlimited websites |
| V4 | ₹9,999/month | White label for agencies |

**Target margin:** ₹949/site | **API cost:** ~₹50/site | **Net/month:** ₹2.85L

---

## Adding New Templates

1. Create `templates/<category>/template.json` (copy salon template as base)
2. Create `templates/<category>/prompt.txt` with complete Cursor prompt
3. Add keywords to template.json `keywords` array
4. Agent auto-discovers templates on startup — no code changes needed

---

## Deployment Workflow (What the Agent Does)

```
User: "Build salon website"
  ↓
Template matched (keyword: salon)
  ↓
GPT-4o refines prompt → Gemini validates (max 3 iterations)
  ↓
Cursor: open new project → paste validated prompt
  ↓
Monitor Cursor every 10 min for completion keywords
  ↓
GitHub API: create repo → Cursor commits + pushes
  ↓
MongoDB Atlas: get connection string from ENV
  ↓
Vercel: create project → link repo
  ↓ [PAUSE: user enters OTP]
  ↓ [RESUME: agent continues]
Vercel: set MONGODB_URI + all ENV variables
  ↓
Copy live URL
  ↓
WhatsApp / Gmail → send to client
  ↓
Session saved to sessions/<id>.json (proof of delivery)
```

---

## Troubleshooting

**"Remote config unavailable"** → Set FIREBASE_CONFIG_URL or run once with internet to create cache

**"Low confidence error"** → UI changed on platform. Update button labels in Firebase config (no reinstall needed)

**Agent stuck at Cursor** → Agent monitors every 10 min. If Cursor shows error, agent auto-retries prompt

**OTP popup not appearing** → Ensure tkinter is available: `python -m tkinter`

**Test failures** → Run `python main.py --check` to identify missing packages/credentials

---

*Built with ♥ in Karimnagar, Telangana · Autonomous Web Agency Agent v1.0*
