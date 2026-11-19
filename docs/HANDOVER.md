# Ava: guide for the team

Ava is the firm's AI assistant. This guide covers what Ava does, what the team does each day and each
week, and what to do when something goes wrong. The short training video walks through the dashboard;
this guide is the reference to come back to.

The technical side (setup, hosting, keys) is in `README.md`, `docs/SETUP.md` and `docs/DEPLOY.md`, for whoever looks
after the system.

## What Ava does

| Where | What Ava does | What Ava never does |
|---|---|---|
| **Website chat** | Answers questions from the approved content, asks a few questions about the need, and books a free discovery call in a consultant's calendar. For existing clients (after an email code check): ticket status, project updates, new support tickets. | Quote prices, give discounts or promise dates. Show a client's information before the email check. |
| **Phone and browser calls** | The same as the chat, by voice. Transfers the caller to a consultant during office hours (Monday to Friday, 09:00 to 18:00); outside them, offers a call back. Can call someone on your request (Monday to Friday, 10:00 to 17:00). | Call anyone twice for the same request, or outside calling hours. |
| **Team assistant** (`/team`) | Answers staff questions from internal documents (methods, procedures, past work) and each client's notes, and drafts emails and proposals, with the sources it used. | Show a client's notes to someone without access to that client. |
| **Dashboard** (`/admin`) | Shows everything above. Admins change what Ava says, handle privacy requests and check the log. | Open for consultants: it is for admins only. |

When Ava cannot help, or a conversation needs a person, it **hands over**. The conversation goes to the
Handovers list and to the `#handovers` Slack channel with a summary. Reasons:

- security incident
- pricing negotiation
- legal question or complaint
- large (enterprise) account
- urgent ticket
- the person asked for a human
- Ava cannot help

## Every day (about 5 minutes)

1. **Slack `#handovers`**: reply to each handover the same working day. The summary says who it is and what
   they need. The full conversation is in the dashboard (**Handovers**, then click the row).
2. **Slack `#leads`**: new leads and booked discovery calls. The consultant also gets the calendar invite.
3. **Dashboard > Log**: the top box lists **failed background tasks** (an email, calendar invite, CRM update,
   Jira ticket or SMS that did not go through after several tries). Each failure is also posted to Slack.
   Do that task by hand (for example, send the email yourself), or ask whoever looks after the system.

## Every week (about 15 minutes)

1. **Dashboard > Overview**, last 7 days:
   - conversations;
   - how many gave contact details, were qualified and booked a call;
   - handovers and their reasons;
   - answer speed.
2. **Read five conversations** (**Conversations**). Look for questions Ava could not answer well. Each one is
   a missing or unclear FAQ: add or fix it (next section).
3. **Leads**: anyone qualified who did not book. A consultant can follow up, or ask Ava to call them
   (**Calls > Ask Ava to call someone**).

## Changing what Ava says

**Dashboard > Content and rules.** Every change is checked before it is saved, and Ava uses it straight
away, in the chat and on calls.

| To change | Open | How |
|---|---|---|
| A question and its answer | **FAQs** | Edit the answer, or **Add a question**. Write the question the way a visitor would ask it. |
| What a service includes | **Services** | Same form: name and description. |
| Facts about the firm (offices, hours, certifications) | **Company facts** | Same form. |
| Policies (data, support hours, contracts) | **Policies** | Same form. |
| Booking hours, call length, greetings, calling hours, how long data is kept | **Assistant rules** | Text. Change only the values after the colons; the comments explain each one. |
| Email and text message wording | **Email wording**, **Text message wording** | Text. Keep the `{placeholders}`. |
| Who takes which calls and handovers | **Consultants and routing** | Text. |
| Internal documents for the team assistant | the **Internal documents** list | Edit, or type a name under the list and click **Add**. |

- **Write facts, not instructions.** An FAQ changes what Ava *says*. How Ava *behaves* is fixed for safety.
  For example, Ava always checks an existing client's email before showing anything, and it never gives
  prices. An FAQ cannot change that.
- **Never put prices in the content.** Ava is built not to state them. A consultant gives prices on the
  discovery call.
- Fill in **What changed** when you save. Every save is a version.
- **History** shows every earlier version: **Open** one, then **Save** it to make it current again.
- **Back to original** returns a file to the version first delivered.
- If two people edit the same file, the second save is refused with a message. Reload, then make the change
  again.

## Asking Ava to call someone

**Calls > Ask Ava to call someone**: number, name, and what the call is about. Ava says the reason in its
greeting. Outside calling hours, the call waits for the next allowed time. The call, its transcript and
its outcome appear in **Calls**. Calls need the firm's Twilio account.

## Privacy requests (GDPR)

When someone asks what we hold about them, or asks us to delete it:

1. **Dashboard > Privacy.** Enter the email address or phone number they used, then click **Find**.
2. **A copy** (access request): click **Download a copy**, and send the file to them by a secure route.
3. **Deletion**:
   - If they have an upcoming discovery call, click **Cancel booking** first. They get the cancellation email.
     Wait a minute, then click **Find** again.
   - Type their email or number again in the red box, then click **Erase for good**. This cannot be undone.
   - The result lists the copies in other systems (HubSpot, Google Calendar, Jira, Slack). Delete those by
     hand there.
   - Support tickets of a client company are kept, without the person's email. A client contact entry is
     kept, because it is how they sign in. Ask whoever looks after the system to remove it if the person has left.
4. Reply to the person within one month (the GDPR deadline).

Every night, data older than the periods set in **Assistant rules** (`privacy:`) is deleted. Defaults:
chats and calls after 1 year, booking records after 2 years. The **Privacy log** shows every request and
clean-up, with who did it. It keeps a one-way fingerprint of the person, never their email or number.

## Team assistant (`/team`)

- Sign in with your work email. Your access decides which clients' notes you can see. Ask an admin to change it.
- Ask in plain words: "What did we propose to Northwind last time?", or "Draft a follow-up email to
  Brightwater about the ERP issue".
- Check the sources under each answer before you use a draft. Ava writes `[date to confirm]` where it does
  not know something. Fill those in.
- It answers only from the documents. If something is missing, add it as an internal document.

## Accounts

Staff accounts are created by whoever looks after the system (`python -m scripts.staff`, see `README.md`):

- **Consultant**: the team assistant, with access to the clients they work with.
- **Admin**: also the dashboard. Give it only to people who need it.

When someone leaves, their account is disabled the same day (`scripts.staff disable`). Their sign-ins end
at once.

## When something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| The chat says "I'm having trouble answering right now" | The AI service had a short limit or outage | Nothing, if it passes within minutes. If it lasts, tell whoever looks after the system. |
| A failed task in **Log** or in Slack | An email, invite, CRM, Jira or SMS update did not go through | Do it by hand; tell the technical contact if it repeats. |
| Ava gave a wrong or vague answer | The content is missing or unclear | Fix the FAQ, service or policy (see above). |
| A visitor says they did not get the booking email | Email failed, or it is in their spam | Check **Log**; the booking itself is in **Bookings** and the consultant's calendar. |
| Someone cannot sign in | Wrong password, or too many tries (locked for 15 minutes) | Wait 15 minutes, or ask for a new password (`scripts.staff password`). |
| The dashboard figures do not load | The database is unreachable | Tell whoever looks after the system (DEPLOY.md, monitoring). |
| A caller could not be transferred | Outside transfer hours, or the consultant did not answer | Ava offered a call back; the call is in **Calls** with its outcome. |
