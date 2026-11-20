# Deploying

The assistant runs on free plans for a demo or pilot. The go-live checklist at the end says what to
upgrade before real customers use it. Plan limits were checked in November 2026; check them again before
you rely on them.

| Part | Where | Free plan | What it holds |
|---|---|---|---|
| Website, widget, `/team`, `/voice`, `/admin` | Vercel project `consult-assistant` | Hobby (personal, non-commercial use only) | nothing (pages only) |
| API (chat, calls, jobs) | Vercel project `consult-assistant-api`, Frankfurt | Hobby: FastAPI as one function, 2 GB memory, 5 minutes per request or call | nothing (all state is in the database) |
| Database | Neon, Frankfurt | 1 GB per project, 100 compute hours a month, sleeps after 5 idle minutes | everything |
| Rate limits, verification codes | Upstash Redis, Frankfurt | Free: 256 MB, 500,000 commands a month | short-lived keys only |
| Background tasks | Upstash QStash | 1,000 messages a day | task ids only |

Nothing here needs a payment card. Keep every key out of the repository: each one goes only into the
host's environment settings. (Render, Koyeb and Hugging Face Docker Spaces need a card or a paid plan for
the API; `render.yaml` and `backend/Dockerfile` remain for a host with a card, see the end of this guide.)

## 1. Before you start
- The project is on GitHub, and the GitHub checks (`.github/workflows/ci.yml`) pass. Vercel deploys from it.
- Your local `.env` works (`python -m scripts.check_integrations` is all OK). You will copy values from it.

## 2. Database: Neon (5 minutes)
1. Sign up at neon.com. Create a project named `consult-assistant`, Postgres 16 or newer, region
   **AWS Europe (Frankfurt)**.
2. On the project dashboard, click **Connect** and copy the connection string. Either the pooled or the
   direct one works: the API switches to the direct host itself, because the migration lock needs it.
3. You don't need to create any tables. The API creates them on its first start, including the `vector`
   and `btree_gist` extensions.

## 3. Redis and QStash (5 minutes)
1. **Redis:** sign up at upstash.com and create a Redis database in **eu-central-1 (Frankfurt)**. Under
   **Connect**, copy the URL that starts with `rediss://` (two "s": Upstash accepts only encrypted connections).
   The free plan allows one database; it can be shared with another project, because every key this app writes
   has its own prefix. Redis Cloud's free plan (redis.io/try-free) works too.
2. **QStash:** in the same Upstash account, open **QStash**. Use the EU region (the default; the API uses
   `https://qstash.upstash.io`) and copy `QSTASH_TOKEN`, `QSTASH_CURRENT_SIGNING_KEY` and
   `QSTASH_NEXT_SIGNING_KEY`.

## 4. API: Vercel (10 minutes)
1. Sign up at vercel.com with GitHub. **Add New > Project**, import the repository, name the project
   `consult-assistant-api`, and set **Root Directory** to `backend`. The framework preset should show
   **FastAPI** (choose it if it does not). Leave "Include files outside the root directory in the Build Step"
   on: the build copies `content/` and the embedding model into the function (`scripts/vercel_build.py`).
2. Environment variables. Paste them all at once into the first **Key** box:

   | Setting | Value |
   |---|---|
   | `APP_ENV` | `production` |
   | `JOB_RUNNER` | `qstash` (there is no always-on server, so QStash calls the API when a task is due) |
   | `EMBEDDING_PROVIDER` | `local` |
   | `CLIENT_IP_HEADER` | `x-real-ip` (set by Vercel; visitors cannot fake it) |
   | `PUBLIC_API_URL` | `https://consult-assistant-api.vercel.app` (the project's address) |
   | `ALLOWED_ORIGINS` | the website's address from step 5, e.g. `https://consult-assistant.vercel.app` |
   | `DATABASE_URL` | Neon connection string |
   | `REDIS_URL` | Upstash Redis URL (`rediss://...`) |
   | `QSTASH_TOKEN`, `QSTASH_CURRENT_SIGNING_KEY`, `QSTASH_NEXT_SIGNING_KEY` | from Upstash |
   | `LLM_PROVIDER` | `groq` (while testing) or `anthropic` |
   | `GROQ_API_KEY` or `ANTHROPIC_API_KEY` | the key for that provider |
   | Integrations | the HubSpot, Google, Slack, Jira and Twilio lines from your local `.env`; one you leave out stays off and logs what it would have done |

3. **Deploy.** The build takes about 3 to 5 minutes. Then open `https://<api address>/health`. It should show
   `{"status":"ok","database":true}`. `vercel.json` runs the API in Frankfurt (`fra1`), next to the database.
4. QStash schedules (once, from your computer, with the production values):
   ```bash
   cd backend && source .venv/bin/activate
   QSTASH_TOKEN='<token>' PUBLIC_API_URL='https://<api address>' python -m scripts.qstash_setup --keep-awake
   ```
   This creates an hourly safety-net run for background tasks, which also runs the nightly privacy
   clean-up, and a call to `/health` every 10 minutes that keeps an instance warm, so a chat rarely waits for
   a cold start (a few seconds).

Changing an environment variable on Vercel takes effect on the next deployment: **Deployments > ... >
Redeploy**.

## 5. Website: Vercel (5 minutes)
1. **Add New > Project** again, import the same repository, name it `consult-assistant`, and set **Root
   Directory** to `frontend`. Vercel detects Next.js.
2. Environment variables:
   - `NEXT_PUBLIC_API_URL` = the API address from step 4 (no trailing slash)
   - `WIDGET_PARENT_ORIGINS` = the firm's own website, if the widget goes on it (e.g. `https://www.acme.example`)
3. Deploy. If the website's address differs from what you put in `ALLOWED_ORIGINS` in step 4, correct it
   there and redeploy the API. If the widget goes on the firm's site, add that address too, separated by a
   comma.

## 6. Accounts and phone line
- **Staff accounts in the production database:** run from your computer with the Neon connection string:
  ```bash
  DATABASE_URL='<neon connection string>' python -m scripts.staff add you@firm.example "Your Name" --role admin
  DATABASE_URL='<neon connection string>' python -m scripts.seed_demo --contact you@example.com   # demo clients, optional
  ```
- **Twilio (phone calls):** set the number's voice webhook to `https://<api address>/api/voice/twilio/incoming`
  ([SETUP.md](SETUP.md), Twilio). `PUBLIC_API_URL` must be the API address.
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
- [ ] **Vercel:** move to **Pro** ($20 per developer a month, covering both projects). Vercel's terms allow
  Hobby only for personal, non-commercial use, and a firm's website chat is commercial use. Pro also lets a
  phone call run past 5 minutes: set `maxDuration` in `backend/vercel.json` to `800` (about 13 minutes).
  WebSockets on Vercel are in public beta; test a long call before relying on it.
- [ ] **Neon:** the free plan is enough for a small firm (1 GB holds years of chats). Move to a paid plan if you
  need a longer restore history or the compute hours run out.
- [ ] Use your own domains: `chat.firm.example` and `api.firm.example`, both on Vercel. Then update
  `PUBLIC_API_URL`, `ALLOWED_ORIGINS`, `NEXT_PUBLIC_API_URL` and the Twilio webhook.

## Switch the model to Claude
1. In the Anthropic Console, create an API key on the firm's account (it needs credits), and set a monthly
   spend limit.
2. On your computer, run the behaviour checks against Claude:
   `LLM_PROVIDER=anthropic ANTHROPIC_API_KEY='<key>' python -m evals.run`. All checks should pass.
3. On the API project in Vercel, set `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`, remove `GROQ_API_KEY`,
   and redeploy. Chat and calls use `claude-haiku-5-5` (fast and low-cost). The team assistant's drafts use `claude-sonnet-5-5`.
   Both can be changed with `ANTHROPIC_CHAT_MODEL` and `ANTHROPIC_DRAFT_MODEL`.

## Security
- [ ] Every staff member has their own account (`scripts.staff`). No shared logins. Admin only for the people
  who need the dashboard.
- [ ] Two-factor sign-in on every service account: GitHub, Vercel, Neon, Upstash, Anthropic, Twilio,
  Google, HubSpot, Slack, Jira.
- [ ] `APP_ENV=production`. It turns off the API docs page, hides error details and
  health details, and refuses to start without the database and Redis.
- [ ] `ALLOWED_ORIGINS` lists only the firm's sites. `WIDGET_PARENT_ORIGINS` lists only the site that shows the widget.
- [ ] Keys made for testing are rotated, and none were ever committed (`git log -p | grep -i key` finds nothing).

## Privacy (GDPR)
- [ ] On the firm's own website, use only the widget (`embed.js`); the showcase site in `frontend/src/app/page.tsx`
  says Acme is a fictional firm.
- [ ] Replace the starting privacy notice (`frontend/src/app/privacy/page.tsx`) with the firm's reviewed policy,
  and set the retention periods in the dashboard (Content and rules > Assistant rules, `privacy:`) to match it.
- [ ] Data processing agreements in place with each processor that receives personal data: Anthropic,
  Vercel, Neon, Redis, Upstash, Twilio, Google, HubSpot, Slack, Atlassian.
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
- [ ] Vercel > the API project > Logs for errors after each deploy.

## Monthly running costs (estimate for a small firm)
Assumptions: 1,000 website conversations of about 6 turns each, 300 team-assistant drafts, and 200 minutes
of phone calls. The dashboard Overview shows the real token use per turn. Recompute after the first
month.

| Item | Cost a month |
|---|---|
| Vercel Pro (one developer seat, website and API) | $20 |
| Neon, Upstash (Redis and QStash) | $0 on the free plans at this volume |
| Claude Haiku 5.5 for chat and calls ($0.10 / $0.50 per million input / output tokens; about 6,000 in and 250 out per turn) | about $5 |
| Claude Sonnet 5.5 for team drafts ($2 / $10 per million tokens; about 8,000 in and 1,500 out per draft) | about $10 |
| Twilio ConversationRelay ($0.07 a minute) | about $14, plus call minutes and the phone number at Twilio's rates for the country |
| **Total** | **about $50 to $60** |

Prices are from the providers' pricing pages in November 2026. They change, so check them before you quote
them.

## Another host (needs a card)
The API also runs as a container: `backend/Dockerfile` (build from the repository root). `render.yaml` is a
Render Blueprint for it, with Redis on Render Key Value; Render asks for a card before it creates a Blueprint
or a web service. On an always-on server, set `JOB_RUNNER=local` (the server runs tasks on time itself) and
drop the keep-awake call: `python -m scripts.qstash_setup --no-keep-awake`.
