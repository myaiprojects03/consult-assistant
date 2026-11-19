# Consult Assistant: notes for Claude Code

AI assistant for an IT consulting firm: website chat (Phase 1), integrations (2), client support and an
internal knowledge assistant (3), voice agent (4), admin dashboard and go-live (5).

## Layout
- `backend/` FastAPI, Python 3.12. Entry: `app/main.py`.
  - `app/llm/` provider-neutral model interface (`base.py`); Claude (`anthropic_llm.py`) and any
    OpenAI-compatible API (`openai_compat_llm.py`, used for Groq). Chosen by `LLM_PROVIDER`.
  - `app/assistant/` the chat brain: `prompts.py` (instructions), `tools.py` (what the model may do),
    `guard.py` (no prices or promises reach the visitor), `agent.py` (one turn: rules, model, tools, storage).
  - `app/kb/` knowledge base: content sync from `content/*.yaml`, local embeddings.
  - `app/db/` migrations (append only) and the repository (Postgres + in-memory twin for tests).
  - `app/api/chat.py` public chat API (streamed replies, retry-safe message ids).
  - `app/booking/` availability (`slots.py`, pure and tested), bookings (`store.py`, the database prevents
    overlaps), `service.py` (book, move, cancel), consultant routing (`team.py`), email wording (`emails.py`).
  - `app/jobs/` the outbox: `store.py` (durable queue), `runner.py` (retries, alerts), `handlers.py` (what each
    job does), `qstash.py` (serverless delivery). `app/api/jobs.py` is the signed QStash callback.
  - `app/integrations/` HubSpot, Google Calendar, Gmail, Slack behind interfaces in `base.py`; `local.py`
    stand-ins log instead when a service is off. Chosen in `registry.py` from settings.
  - `app/clients/` clients, projects, tickets (`store.py`), email-code verification (`verify.py`), ticket
    service and handover rules (`tickets.py`). Jira lives in `app/integrations/jira.py`.
  - `app/staff/` staff users, scrypt passwords, hashed session tokens, per-client access.
  - `app/team/agent.py` the internal assistant; `app/api/team.py` its private API (Bearer session).
  - `app/voice/` calls: `session.py` (one live call, ConversationRelay message format, used by phone and
    browser calls), `speech.py` (text -> speakable), `rules.py` (greetings, transfer and calling hours),
    `store.py` (calls table), `twilio.py` (signatures, call tokens, TwiML, REST). `app/api/voice.py` routes.
  - `app/admin/queries.py` dashboard figures and lists (read only); `app/api/admin.py` its API (admins).
  - `app/content/` dashboard edits: `store.py` (versions), `live.py` (content/ + edits, checked before
    saving), `forms.py` (list files edited as a form).
  - `app/privacy/store.py` GDPR: find, export, erase one person; nightly retention (`privacy.retention` job).
  - `scripts/` `google_auth` (one-time Google sign-in), `check_integrations`, `qstash_setup`, `staff`
    (accounts and access), `seed_demo` (sample clients).
- `content/internal/` internal documents (Markdown with a title/kind/client header) for the team assistant.
- `content/` the firm's editable content and the assistant's rules (`rules.yaml`, including booking hours
  and voice rules), consultants and routing (`team.yaml`), email wording (`emails.yaml`), SMS wording
  (`sms.yaml`). Sample data for "Acme IT Consulting".
- `integrations/` the HubSpot app project (CLI) and the Slack app manifest.
- `docs/` SETUP (local run, integrations), DEPLOY (hosting, go-live checklist), HANDOVER (staff guide).
- `frontend/` Next.js: the chat widget (`/widget`), `public/embed.js` (one-line install), demo site (`/`),
  team assistant (`/team`), browser voice calls (`/voice`, `src/lib/voice.ts`), admin dashboard (`/admin`).

## Rules for changes
- Facts the assistant states come only from `content/` via `search_knowledge`. Never put firm facts in prompts.
- Keep `guard.py` in front of every reply. New channels (voice, WhatsApp) must stream through it too.
- Every tool validates its input in `tools.py` and returns text; tools never raise.
- Public chat searches `audience = 'public'` only. Internal material must never be reachable from it.
- Client data is read through the client id bound to the conversation (after verification) or the staff
  user's access list, never from what the model or visitor typed. Client tools are offered only after
  verification (`tools_for`). Internal search filters by allowed clients in SQL.
- Verification answers must not reveal whether an email belongs to a client.
- Voice: never cancel a turn mid-way (a tool may be booking); on interruption stop speaking and tell the
  next turn what was heard. Twilio webhooks must pass `twilio_form` (signature); call WebSockets need the
  call token. Outbound calls: only from the `voice.outbound` job, claimed before dialling, never redialled.
- Schema changes: add a new entry to `MIGRATIONS`; never edit an applied one.
- Any write that can be retried (messages, bookings, tickets) needs an idempotency key.
- Outside services are called from background jobs, not in the chat turn. Enqueue with a stable key
  (`jobs.enqueue(kind, key, payload)`); handlers re-read current state and must be safe to run twice.
- Never claim a booking in text: only `book_discovery_call`'s result confirms one.
- Times: store UTC, show the visitor's zone (`describe()`), compute availability in the office zone.
- Personal data: a new table or column holding it must be covered by `app/privacy/store.py` (find, export,
  erase, retention). Log entries about a person carry their conversation id so erasure removes them;
  privacy records keep only `Person.fingerprint`, never the email or number.
- A change to what staff or visitors see needs its browser test in `frontend/e2e/` (run by CI).
- Secrets only in `.env` (never in code, tests or docs).

## Commands
```bash
docker compose up -d                                   # Postgres (pgvector) + Redis
cd backend && source .venv/bin/activate
uvicorn app.main:app --reload --port 8000              # API
pytest                                                 # tests (DB tests need TEST_DATABASE_URL)
ruff check . && mypy app evals scripts                 # style and types
python -m evals.run                                    # behaviour checks against the real model
python -m scripts.check_integrations [--send-test]     # HubSpot, Google, Slack, Jira, Twilio, QStash OK?
python -m scripts.seed_demo --contact you@example.com   # sample clients
python -m scripts.qstash_setup [--keep-awake]          # after deploying (docs/DEPLOY.md)
python -m scripts.staff add|grant|list ...              # team assistant accounts
cd frontend && npm run dev                             # widget + demo site on :3000 (/team, /voice, /admin)
E2E_DATABASE_URL=postgresql://consult:consult@localhost:5432/consult_e2e npm run test:e2e   # browser tests
```

Custom commands for this repo are in `.claude/commands/` (`/add-faq`, `/run-evals`, `/new-connector`).
