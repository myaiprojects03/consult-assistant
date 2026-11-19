# Consult Assistant

An AI assistant for an IT consulting firm, built on Claude. It answers prospects and clients on the website
and by phone, books discovery calls, handles client support, and helps consultants find internal
knowledge. Staff run it from an admin dashboard. The sample firm is "Acme IT Consulting", and the
assistant is "Ava".

## What it does

- **Website chat:** answers only from the firm's approved content, qualifies leads, and books calls in a
  consultant's Google Calendar with confirmation and reminders. It never states prices or makes promises:
  an output guard checks every sentence.
- **Voice:** the same assistant on the phone (Twilio) and in the browser. It understands interruptions,
  transfers to a consultant in office hours, and can call people on a staff request within calling hours.
- **Client support:** existing clients verify by an emailed code, then check project and ticket status or raise
  Jira tickets. Urgent cases go straight to their account manager.
- **Team assistant:** answers consultants from internal documents with cited sources, and drafts emails
  and proposals. Each consultant sees only their own clients; the database query enforces this.
- **Admin dashboard:**
  - leads, conversations, call transcripts, bookings and handovers;
  - FAQs, services, policies and rules edited without code (checked, versioned, applied at once);
  - GDPR export and erasure, and a nightly retention clean-up;
  - full logs.
- **Integrations:** HubSpot, Google Calendar and Gmail, Slack, Jira, Twilio. They run through a durable job
  queue with retries, idempotency keys and failure alerts.

## How it fits together

```
 Website widget ─┐                       ┌─ Claude (or Groq while testing)
 Browser call  ──┼─► FastAPI backend ────┼─ Postgres + pgvector (data, knowledge, job queue)
 Phone (Twilio) ─┤   assistant, guard,   ├─ Redis (rate limits, verification codes)
 /team, /admin ──┘   tools, voice, jobs  └─ HubSpot · Google · Slack · Jira · Twilio (background jobs)
```

| Part | Built with |
|---|---|
| Backend | Python 3.12, FastAPI, asyncpg, Postgres 16 with pgvector, Redis |
| AI | Claude (`claude-haiku-5-5` for chat and voice, `claude-sonnet-5-5` for drafts), or any OpenAI-compatible API |
| Frontend | Next.js 15, React 19, Tailwind |
| Hosting | Vercel, Render, Neon, Upstash (free plans for a pilot) |

## Quick start

Needs Python 3.12+, Node.js 20+ and Docker.

```bash
cp .env.example .env                  # add GROQ_API_KEY (free) or ANTHROPIC_API_KEY
docker compose up -d
cd backend && python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt && uvicorn app.main:app --reload --port 8000
# second terminal
cd frontend && cp .env.example .env.local && npm install && npm run dev
```

Open http://localhost:3000 and click **Chat with us**. Sample clients, staff accounts and each integration:
[docs/SETUP.md](docs/SETUP.md).

## Documentation

| Document | For |
|---|---|
| [docs/SETUP.md](docs/SETUP.md) | Running it locally and connecting each service |
| [docs/DEPLOY.md](docs/DEPLOY.md) | Putting it online, the go-live checklist and running costs |
| [docs/HANDOVER.md](docs/HANDOVER.md) | The firm's staff: daily routine, editing content, privacy requests |
| [CLAUDE.md](CLAUDE.md) | Architecture rules for maintaining it with Claude Code |

## Tests

```bash
cd backend && pytest                  # 181 tests; set TEST_DATABASE_URL to include Postgres
ruff check . && mypy app evals scripts
python -m evals.run                   # 27 behaviour checks against the real model
cd ../frontend && npm run test:e2e    # browser tests (see the comment in playwright.config.ts)
```

GitHub Actions runs everything on each push:
- lint, types and tests;
- dependency audits;
- a production build;
- browser tests of the whole system, including automatic WCAG A and AA checks.

## Project structure

```
backend/app/
  assistant/     chat brain: instructions, tools, output guard, one turn
  voice/         call sessions (phone and browser), speech formatting, Twilio
  booking/       availability, bookings, consultant routing, emails
  clients/       clients, tickets, email verification, handover rules
  team/          internal knowledge assistant
  admin/ content/ privacy/   dashboard queries, content editing, GDPR tools
  jobs/          durable job queue, handlers, QStash
  integrations/  HubSpot, Google, Slack, Jira (with logged stand-ins)
  kb/ db/ llm/   knowledge base, storage and migrations, model providers
  api/           HTTP and WebSocket endpoints
backend/scripts/ setup helpers (Google sign-in, staff accounts, sample data, checks)
backend/tests/   unit and Postgres tests; e2e_server.py for browser tests
backend/evals/   behaviour checks against the real model
content/         the firm's content and the assistant's rules (editable in the dashboard)
frontend/        website, chat widget (embed.js), /voice, /team, /admin; e2e/ browser tests
integrations/    HubSpot app project and Slack app manifest
docs/            setup, deployment and staff guides
```

## Add the widget to any website

```html
<script src="https://<website address>/embed.js" async></script>
```

Then add that site's address to `ALLOWED_ORIGINS` on the API and to `WIDGET_PARENT_ORIGINS` on the frontend.
