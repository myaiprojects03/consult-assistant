---
description: Add a connector for an outside system (CRM, tickets, calendar, email, chat)
argument-hint: <system, e.g. Zoho CRM>
---
Add a connector for: $ARGUMENTS

1. Find the interface for that kind of system in `backend/app/integrations/base.py` (CRM, Calendar, Mailer,
   Notifier) and implement it in a new file next to `hubspot.py`, `google.py` and `slack.py`. Use the `call()`
   helper so errors are sorted into retryable (network, 429, 5xx) and permanent (bad key, bad input).
2. Read credentials from settings (`app/config.py`), document them in `.env.example` and `docs/SETUP.md`, and select
   the connector by name in `app/integrations/registry.py`.
3. Connectors are only called from background jobs (`app/jobs/handlers.py`), never while the visitor waits
   (the calendar's free/busy check is the one exception). Jobs run at least once, so every create must be
   safe to repeat: match on a natural key (email) or use a fixed id (see `event_id_for`).
4. Errors must name the cause and the setting to check; never log secrets or personal data.
5. Write wire-format tests with `httpx.MockTransport` in `tests/test_integrations.py` (no real network calls),
   add a check to `scripts/check_integrations.py`, then run `pytest`, `ruff check .` and `mypy app evals`.
