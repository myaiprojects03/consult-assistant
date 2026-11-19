# Deploying

The assistant runs on free plans for a demo or pilot. The go-live checklist at the end says what to
upgrade before real customers use it. Plan limits were checked in November 2026; check them again before
you rely on them.

| Part | Where | Free plan | What it holds |
|---|---|---|---|
| Website, widget, `/team`, `/voice`, `/admin` | Vercel | Hobby (personal, non-commercial use only) | nothing (pages only) |
| API (chat, calls, jobs) | Render, Frankfurt | Free: 512 MB, sleeps after 15 idle minutes, 750 hours a month | nothing (all state is in the database) |
| Database | Neon, Frankfurt | 1 GB per project, 100 compute hours a month, sleeps after 5 idle minutes | everything |
| Rate limits, verification codes | Upstash Redis, Frankfurt | 500,000 commands a month | short-lived keys only |
| Background tasks when the API sleeps | Upstash QStash | 1,000 messages a day | task ids only |

Nothing here needs a payment card. Keep every key out of the repository: each one goes only into the
host's environment settings.

## 1. Before you start
- The project is on GitHub. Render and Vercel deploy from it, and the GitHub checks (`.github/workflows/ci.yml`)
  must pass before Render deploys a change.
- Your local `.env` works (`python -m scripts.check_integrations` is all OK). You will copy values from it.

## 2. Database: Neon (5 minutes)
1. Sign up at neon.com. Create a project named `consult-assistant`, Postgres 16 or newer, region
   **AWS Europe (Frankfurt)**.
2. On the project dashboard, click **Connect** and copy the connection string. Either the pooled or the
   direct one works: the API switches to the direct host itself, because the migration lock needs it.
3. You don't need to create any tables. The API creates them on its first start, including the `vector`
   and `btree_gist` extensions.

## 3. Redis and QStash: Upstash (5 minutes)
1. Sign up at upstash.com. **Redis > Create database**: name `consult-assistant`, region
   **eu-central-1 (Frankfurt)**, free plan. Under **Connect**, copy the URL that starts with
   `rediss://` (it includes the password).
2. **QStash**: use the EU region (the default; the API uses `https://qstash.upstash.io`) and copy `QSTASH_TOKEN`, `QSTASH_CURRENT_SIGNING_KEY` and
   `QSTASH_NEXT_SIGNING_KEY`.

## 4. API: Render (10 minutes)
1. Sign up at render.com with GitHub. **New > Blueprint**, then pick the repository. Render reads
   `render.yaml` and creates the `consult-assistant-api` web service.
2. Render asks for the values marked `sync: false`:

   | Setting | Value |
   |---|---|
   | `PUBLIC_API_URL` | `https://consult-assistant-api.onrender.com` (Render shows the address; if the name is taken, it adds letters) |
   | `ALLOWED_ORIGINS` | the Vercel address from step 5, e.g. `https://consult-assistant.vercel.app` (fill it in after step 5 if you don't know it yet) |
   | `DATABASE_URL` | Neon connection string |
   | `REDIS_URL` | Upstash `rediss://...` URL |
   | `QSTASH_TOKEN`, `QSTASH_CURRENT_SIGNING_KEY`, `QSTASH_NEXT_SIGNING_KEY` | from Upstash |
   | `LLM_PROVIDER` | `groq` (while testing) or `anthropic` |
   | `GROQ_API_KEY` or `ANTHROPIC_API_KEY` | the key for that provider; leave the other empty |

3. Integrations: on the service, open **Environment > Add from .env**. Paste the lines for HubSpot, Google,
   Slack, Jira and Twilio from your local `.env`. An integration you leave out stays off and logs what it
   would have done.
4. The first build takes about 5 minutes, because it puts the embedding model into the image. Then open
   `https://<PUBLIC_API_URL>/health`. It should show `{"status":"ok","database":true}`.
5. QStash schedules (once, from your computer, with the production values):
   ```bash
   cd backend && source .venv/bin/activate
   QSTASH_TOKEN='<token>' PUBLIC_API_URL='https://<api address>' python -m scripts.qstash_setup --keep-awake
   ```
   This creates an hourly safety-net run for background tasks, which also runs the nightly privacy
   clean-up. It also creates a call to `/health` every 10 minutes, so the free API does not fall asleep: a
   sleeping API takes about a minute to answer the first chat message, and phone calls would fail. One
   always-awake service uses about 744 of the 750 free hours a month.

## 5. Website: Vercel (5 minutes)
1. Sign up at vercel.com with GitHub. **Add New > Project**, import the repository, and set
   **Root Directory** to `frontend`. Vercel detects Next.js.
2. Environment variables:
   - `NEXT_PUBLIC_API_URL` = the Render address (no trailing slash)
   - `WIDGET_PARENT_ORIGINS` = the firm's own website, if the widget goes on it (e.g. `https://www.acme.example`)
3. Deploy. Put the Vercel address into `ALLOWED_ORIGINS` on Render. If the widget goes on the firm's site,
   add that address too, separated by a comma. Render redeploys by itself.

## 6. Accounts and phone line
- **Staff accounts in the production database:** run from your computer with the Neon connection string:
  ```bash
  DATABASE_URL='<neon connection string>' python -m scripts.staff add you@firm.example "Your Name" --role admin
  DATABASE_URL='<neon connection string>' python -m scripts.seed_demo --contact you@example.com   # demo clients, optional
  ```
- **Twilio (phone calls):** set the number's voice webhook to `https://<api address>/api/voice/twilio/incoming`
  ([SETUP.md](SETUP.md), Twilio). `PUBLIC_API_URL` must be the Render address.
- **Google:** the refresh token from `scripts.google_auth` works on the server as it is. Publish the Google app
  ([SETUP.md](SETUP.md), Google): in "Testing" mode, Google expires the token after 7 days.

## 7. Check it end to end
1. `https://<website>/` opens the demo site. Click **Chat with us**, ask a question and book a call. You
   should get the confirmation email and the calendar invitation, and see the lead in Slack and HubSpot.
2. `https://<website>/admin`: sign in. The conversation and the booking should be there.
3. `https://<website>/voice`: make a browser call.
4. Upstash > QStash > Logs: the `/health` calls and the hourly sweep return 200.

To put the widget on the firm's site, add this one line:
`<script src="https://<website>/embed.js" async></script>`.

---

# Go-live checklist

Work through this before real customers use the assistant.

## Hosting: move off the limits that hurt real use
- [ ] **Render:** change the instance to **Starter** ($7 a month, 512 MB, always on). Render says free services are
  not for production, and a free service can restart at any time. Then set `JOB_RUNNER=local` (the server
  runs tasks on time itself) and remove the keep-awake call: `python -m scripts.qstash_setup --no-keep-awake`.
- [ ] **Vercel:** move to **Pro** ($20 per developer a month). Vercel's terms allow Hobby only for personal,
  non-commercial use, and a firm's website chat is commercial use.
- [ ] **Neon:** the free plan is enough for a small firm (1 GB holds years of chats). Move to a paid plan if you
  need a longer restore history or the compute hours run out.
- [ ] Use your own domains: `chat.firm.example` on Vercel and `api.firm.example` on Render. Then update
  `PUBLIC_API_URL`, `ALLOWED_ORIGINS`, `NEXT_PUBLIC_API_URL` and the Twilio webhook.

## Switch the model to Claude
1. In the Anthropic Console, create an API key on the firm's account (it needs credits), and set a monthly
   spend limit.
2. On your computer, run the behaviour checks against Claude:
   `LLM_PROVIDER=anthropic ANTHROPIC_API_KEY='<key>' python -m evals.run`. All checks should pass.
3. On Render, set `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`, then remove `GROQ_API_KEY`. Chat and
   calls use `claude-haiku-5-5` (fast and low-cost). The team assistant's drafts use `claude-sonnet-5-5`.
   Both can be changed with `ANTHROPIC_CHAT_MODEL` and `ANTHROPIC_DRAFT_MODEL`.

## Security
- [ ] Every staff member has their own account (`scripts.staff`). No shared logins. Admin only for the people
  who need the dashboard.
- [ ] Two-factor sign-in on every service account: GitHub, Render, Vercel, Neon, Upstash, Anthropic, Twilio,
  Google, HubSpot, Slack, Jira.
- [ ] `APP_ENV=production` (set by `render.yaml`). It turns off the API docs page, hides error details and
  health details, and refuses to start without the database and Redis.
- [ ] `ALLOWED_ORIGINS` lists only the firm's sites. `WIDGET_PARENT_ORIGINS` lists only the site that shows the widget.
- [ ] Keys made for testing are rotated, and none were ever committed (`git log -p | grep -i key` finds nothing).

## Privacy (GDPR)
- [ ] Replace the sample privacy notice (`frontend/src/app/privacy/page.tsx`) with the firm's reviewed policy,
  and set the retention periods in the dashboard (Content and rules > Assistant rules, `privacy:`) to match it.
- [ ] Data processing agreements in place with each processor that receives personal data: Anthropic,
  Render, Vercel, Neon, Upstash, Twilio, Google, HubSpot, Slack, Atlassian.
- [ ] Hosting is in the EU (Frankfurt) for the API, the database and Redis. Anthropic and Groq process the chat
  text outside the EU; record that in the privacy notice and the records of processing.
- [ ] Someone owns privacy requests: dashboard **Privacy** tab (find, download, erase), then the listed
  HubSpot, Calendar, Jira and Slack records by hand.

## Monitoring
- [ ] Slack alerts: a background task that fails after its retries (email, calendar, CRM, Jira, SMS, calls)
  posts to `SLACK_CHANNEL_ALERTS`. Make sure someone watches that channel.
- [ ] An uptime check on `https://<api address>/health` from a monitoring service, alerting by email. It
  returns 503 when the database is unreachable.
- [ ] Weekly: dashboard Overview (handovers, answer speed, token use, failed tasks) and Log.
- [ ] Render > Logs for errors after each deploy. Render deploys only after the GitHub checks pass.

## Monthly running costs (estimate for a small firm)
Assumptions: 1,000 website conversations of about 6 turns each, 300 team-assistant drafts, and 200 minutes
of phone calls. The dashboard Overview shows the real token use per turn. Recompute after the first
month.

| Item | Cost a month |
|---|---|
| Render Starter (API) | $7 |
| Vercel Pro (one developer seat) | $20 |
| Neon, Upstash | $0 on the free plans at this volume |
| Claude Haiku 5.5 for chat and calls ($0.10 / $0.50 per million input / output tokens; about 6,000 in and 250 out per turn) | about $5 |
| Claude Sonnet 5.5 for team drafts ($2 / $10 per million tokens; about 8,000 in and 1,500 out per draft) | about $10 |
| Twilio ConversationRelay ($0.07 a minute) | about $14, plus call minutes and the phone number at Twilio's rates for the country |
| **Total** | **about $55 to $65** |

Prices are from the providers' pricing pages in November 2026. They change, so check them before you quote
them.
