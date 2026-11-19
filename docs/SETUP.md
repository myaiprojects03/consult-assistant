# Setup

How to run the project on your computer, then how to connect each outside service. Every service is
optional: anything not connected is replaced by a stand-in that writes what it would have done to the event
log. To put the project online, see [DEPLOY.md](DEPLOY.md).

Keys and tokens go only in `.env` (and in the hosting settings), never in code, documents or chats. After
changing `.env`, restart the backend.

## 1. Run it on your computer

Needs Python 3.12+, Node.js 20+ and Docker Desktop.

```bash
cp .env.example .env              # add GROQ_API_KEY (free, console.groq.com) or ANTHROPIC_API_KEY
docker compose up -d              # Postgres with pgvector, and Redis

cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
```

In a second terminal:

```bash
cd frontend
cp .env.example .env.local
npm install
npm run dev
```

| Address | What |
|---|---|
| http://localhost:3000 | Demo website with the chat widget (**Chat with us**) |
| http://localhost:3000/voice | Browser voice call (Chrome or Edge; headphones recommended) |
| http://localhost:3000/team | Team assistant (staff sign-in) |
| http://localhost:3000/admin | Admin dashboard (admin sign-in) |
| http://localhost:8000/health | Model, storage, integrations and background task status |

Sample clients and staff accounts (in `backend/`, with the virtual environment active):

```bash
python -m scripts.seed_demo --contact you@example.com    # Northwind (enterprise), Brightwater (small business)
python -m scripts.staff add you@example.com "Your Name" --role admin
python -m scripts.staff add sam@example.com "Sam Patel" --role consultant --consultant sam
python -m scripts.staff grant sam@example.com brightwater
```

`you@example.com` should be an inbox you can read: it can then verify as Northwind in the chat ("I'm an
existing client"). Passwords are typed at a hidden prompt (12 characters or more). Restart the backend after
`seed_demo` so the client documents are loaded.

## 2. Slack (5 minutes)

1. At https://api.slack.com/apps, click **Create New App > From a manifest**. Pick the workspace, then paste
   `integrations/slack-app-manifest.yaml`. Click **Create**, then **Install**.
2. Copy the **Bot token** (`xoxb-...`, under **OAuth & Permissions**).
3. `.env`:
   ```
   SLACK_BOT_TOKEN=xoxb-...
   SLACK_CHANNEL_LEADS=#leads
   SLACK_CHANNEL_HANDOVERS=#handovers
   ```
   For a private channel, invite the app (`/invite @Consult Assistant`).

Posts: leads and bookings in #leads, handovers with a summary in #handovers, and alerts for failed tasks.

## 3. Google Calendar and Gmail (15 minutes)

The assistant uses one Google account: calls go into its calendar, and emails are sent from its Gmail.

1. **Consent screen:** at https://console.cloud.google.com, go to **APIs & Services > OAuth consent screen**
   (Google Auth Platform) and click **Get started**. Use the app name `Consult Assistant` and audience
   **External**. Under **Audience > Test users**, add the Gmail address the assistant will use.
2. **Client:** go to **Clients > Create client**, type **Desktop app**. Copy the ID and secret:
   ```
   CALENDAR_PROVIDER=google
   EMAIL_PROVIDER=gmail
   GOOGLE_CLIENT_ID=...
   GOOGLE_CLIENT_SECRET=...
   EMAIL_FROM_ADDRESS=<that Gmail address>
   ```
3. **Sign in once:** run `python -m scripts.google_auth`. In the browser, sign in. On "Google hasn't
   verified this app", click **Continue**, tick all permissions, then **Continue**. The token is saved to
   `.env` and never shown.
4. **Time zone:** in Google Calendar, go to **Settings > General > Time zone** and set the office time zone.

While the app is in "Testing", Google ends the sign-in after 7 days: run `google_auth` again. To remove the
limit, publish the app once the site is live (it needs the site's `/privacy` page). Test bookings with
another email address: Google sends no invitation to the calendar's own address.

## 4. HubSpot (10 minutes)

```bash
npm install -g @hubspot/cli@latest
hs account auth                          # browser: keep the permissions, "Generate and send to CLI"
cd integrations/hubspot-app && hs project upload
```

1. In HubSpot, go to **Development > Projects > consult-assistant-hubspot > Consult Assistant**. On the
   **Distribution** tab, click **Install now**, then **Connect app**.
2. Copy the **Access token** (`pat-...`) shown on the same tab:
   ```
   CRM_PROVIDER=hubspot
   HUBSPOT_ACCESS_TOKEN=pat-...
   ```

What it creates:
- a contact per visitor email;
- a deal once a call is booked;
- one note per conversation (updated, never duplicated).

If `hs project upload` rejects the app file, run `hs project create`. Choose App, then static auth, and copy
the `requiredScopes` from `app-hsmeta.json`.

## 5. Jira (10 minutes, free up to 10 users)

1. Sign up at https://www.atlassian.com/software/jira/free. Create a **Kanban**, team-managed project with
   the key `SUP`.
2. Create an API token at https://id.atlassian.com/manage-profile/security/api-tokens.
3. `.env`:
   ```
   TICKETING_PROVIDER=jira
   JIRA_BASE_URL=https://<site>.atlassian.net
   JIRA_EMAIL=<the Atlassian account email>
   JIRA_API_TOKEN=...
   JIRA_PROJECT_KEY=SUP
   ```
   Optional: `SLACK_CHANNEL_SUPPORT=#support` for new tickets (otherwise they go to #handovers).

Clients see each issue's status and assignee, never its comments.

## 6. Phone calls and text messages: Twilio (needs a paid account)

Browser calls (`/voice`) need nothing. Phone calls need an upgraded Twilio account, because the trial blocks
ConversationRelay and transfers. Costs: about $0.07 a minute plus call charges and the number.

1. Upgrade the account. Buy a number with Voice and SMS. Accept the AI features addendum: search
   "ConversationRelay" in the Console.
2. Give the API a public https address. On your computer, use a free tunnel:
   `brew install cloudflared && cloudflared tunnel --url http://localhost:8000`.
3. `.env`:
   ```
   VOICE_PROVIDER=twilio
   SMS_PROVIDER=twilio
   TWILIO_ACCOUNT_SID=AC...
   TWILIO_AUTH_TOKEN=...
   TWILIO_PHONE_NUMBER=+44...
   PUBLIC_API_URL=https://<tunnel or API address>
   ```
4. Set the number's voice webhook ("A call comes in") to `https://<PUBLIC_API_URL>/api/voice/twilio/incoming`,
   HTTP POST.
5. For transfers, give each consultant a `phone:` in `content/team.yaml`.

When calls happen:
- **Transfers:** only within `voice.transfer_hours`; outside them, the caller is offered a call back.
- **Outbound calls:** from the dashboard (**Calls > Ask Ava to call someone**), only within
  `voice.calling_hours`, and never dialled twice.
- **Text messages:** bookings made with a phone number get an SMS confirmation and reminder.

## 7. Check everything

```bash
python -m scripts.check_integrations                                   # OK, OFF or FAIL with the reason
python -m scripts.check_integrations --send-test --to you@example.com   # also a test email and Slack post
```

Then book a call in the chat with your own email. Within seconds you should see:
- the calendar event and invitation;
- the confirmation email;
- the post in #leads;
- the contact, deal and note in HubSpot.
