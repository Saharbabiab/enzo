# Enzo — Excellent Nearly Zero Oopsies

A self-hosted, Jarvis-style personal assistant powered by Claude. You talk to it by voice or text from a web app,
an Android app or Telegram; it answers in a dry, polite butler voice and does things for you: calendar, Gmail,
weather, news, reminders, Spotify, web reading, phone calls and messages, reading and tapping your phone's screen,
and handing coding jobs to Claude Code on your PC.

It is model-driven: Claude picks the tools and skills; the code supplies tools, memory, approvals and safety checks.
There is no phrase or regex routing.

## What is inside

```
backend/    Python FastAPI server (port 8765): the agent, tools, memory, voice, Telegram bot, scheduler
frontend/   React + Vite web app (PWA) and the Capacitor Android app around it
```

Main pieces of the backend:

| Area | Where |
|---|---|
| Agent pipeline (web + Telegram) | `agent/pipeline.py`, `agent/big_brain.py` (Claude) |
| Persona (who Enzo is, who you are) | `persona/ENZO.md`, `persona/USER.md` |
| Tools and built-in skills | `agent/tools.py`, `agent/skills/` |
| Playbooks (routines as Markdown) | `skills/<name>/SKILL.md` |
| Memory (facts with trust levels, chat search) | `agent/facts.py`, `agent/memory.py`, `agent/chat_search.py` |
| Approvals for risky actions + audit log | `agent/approvals.py`, `tools.RISK` |
| MCP servers as tools | `agent/mcp_client.py`, `data/mcp_servers.json` |
| Learning (lessons, nightly review, playbook proposals) | `agent/lessons.py`, `agent/nightly.py`, `agent/skill_review.py` |
| Login (password hash, per-device sessions) | `auth.py`, `set_password.py`, `sessions.py` |

## Brains

`MAIN_BRAIN=claude` (the default) answers every turn with Claude (`BIG_BRAIN_MODEL`, e.g. `claude-opus-5-5`,
`claude-sonnet-5-5` or the cheap and fast `claude-haiku-5-5`). Spend is capped per day by `CLAUDE_DAILY_USD`.

Optional fallbacks, used only when Claude is unavailable or over budget: a local Ollama model (`OLLAMA_HOST`),
Gemini (`agent/gemini_brain.py`) and any OpenAI-compatible endpoint (`FREE_LLM_BASE_URL`). Leave them empty to run
on Claude only.

## Quick start (PC)

Needs Python 3.11, Node 20 and MongoDB (local or in Docker).

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env            # fill in ANTHROPIC_API_KEY, MONGO_URI, DEFAULT_CITY ...
python set_password.py          # writes ENZO_PASSWORD_HASH into .env
python server.py                # http://localhost:8765

cd ../frontend
npm ci
npm run dev                     # http://localhost:5173 (proxies /api and /ws to the backend)
```

Edit `backend/persona/USER.md` with a few lines about yourself (where you live, how you talk to Enzo).
Optional: `python telegram_bot.py` for Telegram (set `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`),
`python google_setup.py` for Calendar and Gmail.

## Docker

From the repository root:

```bash
docker build -f backend/Dockerfile -t enzo .
```

or use `backend/docker-compose.yml` (its build context is the repository root). Put your `.env` next to the
compose file; data lives in `./data`. Expose it to your phone only over HTTPS (a reverse proxy such as Nginx Proxy
Manager, or Tailscale), never by opening a port to the backend directly.

## Android app

The app is a thin Capacitor shell that loads your Enzo server and adds what a browser cannot: speech, contacts,
calls, notifications, reminders, and an accessibility service for reading and tapping the screen. It can be set as
the phone's default digital assistant.

1. Set your server address in `frontend/capacitor.config.ts` (`server.url`).
2. `npm run build && npx cap sync android`, then build in Android Studio or with `android/gradlew assembleDebug`.
3. Push notifications need your own Firebase project: put its `google-services.json` in `frontend/android/app/`
   (it is gitignored).

## Safety

- Every tool has a risk level. Sending mail, writing files, running code, installing skills, calls and similar
  actions ask Yes / Always / No first; every decision is logged to an audit file.
- A turn that read outside text (web pages, mail, notifications) asks before any outbound action, even after
  "Always", and facts learned in it are stored as untrusted.
- Web tools refuse internal network addresses (checked after DNS and on every redirect).
- `run_python` only runs in the sandbox container (`backend/sandbox/`) and is off by default
  (`ENABLE_PYTHON_TOOL=false`); the Playwright browser is off by default too (`ENABLE_BROWSER=false`).
- Secrets live only in `backend/.env` (gitignored); logs are redacted.

## Tests

```bash
cd backend && python -m pytest -q tests     # no LLM or API key needed
cd frontend && npx tsc --noEmit -p tsconfig.app.json
```

To measure tool choice on the real agent loop with fake tools (costs API money):
`python -m agent.bench --loop --brain claude`.

## Notes

Some defaults are Israel-specific (Hebrew support, the bus and train planner in `agent/transit.py`, the
`Asia/Jerusalem` time zone in Docker). Change `DEFAULT_CITY`, `HOME_PLACE` / `HOME_LAT` / `HOME_LON` and `TZ` for your
place. A few internal names still say `sharp` (the Mongo database `sharp_db`, the chat role `"sharp"`, the APK file
names); they are the project's old name and work as they are.

## License

MIT, see `LICENSE`.
