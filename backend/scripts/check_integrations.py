"""Check every configured integration with a harmless call, and say exactly what to fix.

    python -m scripts.check_integrations               read-only checks
    python -m scripts.check_integrations --send-test   also send a test email (to EMAIL_FROM_ADDRESS or --to)
                                                       and a test Slack message to the leads channel
Covers HubSpot, Google Calendar, Gmail, Slack, Jira, Twilio and QStash.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta

import httpx

from app.booking.team import load_team
from app.config import CONTENT_DIR, Settings, get_settings
from app.db.repo import MemoryRepo
from app.integrations.base import IntegrationError, call
from app.integrations.registry import build_integrations
from app.kb.content import load_rules

results: list[bool] = []


def report(ok: bool | None, name: str, detail: str) -> None:
    label = {True: "OK  ", False: "FAIL", None: "OFF "}[ok]
    print(f"{label}  {name}: {detail}")
    if ok is False:
        results.append(False)


async def check(s: Settings, send_test: bool, to: str) -> None:
    rules = load_rules(CONTENT_DIR)
    try:
        integ = build_integrations(s, MemoryRepo(), rules.firm_name)
    except IntegrationError as exc:
        report(False, "settings", str(exc))
        return
    async with httpx.AsyncClient(timeout=15) as http:
        # HubSpot
        if s.crm_provider == "hubspot":
            try:
                await call(http, "GET", "https://api.hubapi.com/crm/v3/objects/contacts", service="HubSpot",
                           params={"limit": 1}, headers={"Authorization": f"Bearer {s.hubspot_access_token}"})
                await call(http, "GET", "https://api.hubapi.com/crm/v3/objects/deals", service="HubSpot",
                           params={"limit": 1}, headers={"Authorization": f"Bearer {s.hubspot_access_token}"})
                report(True, "HubSpot", "token works; contacts and deals are readable")
            except IntegrationError as exc:
                report(False, "HubSpot", str(exc))
        else:
            report(None, "CRM", "CRM_PROVIDER=none (leads are kept in the database only)")

        # Google Calendar
        if s.calendar_provider == "google":
            team = load_team(CONTENT_DIR)
            now = datetime.now(UTC)
            try:
                ids = [c.calendar_id for c in team.consultants]
                busy = await integ.calendar.busy(ids, now, now + timedelta(days=1))
                report(True, "Google Calendar", f"can read {len(busy)} calendar(s): "
                       + ", ".join(f"{c.name} ({c.calendar_id})" for c in team.consultants))
            except IntegrationError as exc:
                report(False, "Google Calendar", str(exc))
        else:
            report(None, "Calendar", "CALENDAR_PROVIDER=local (bookings in the database only, no invitations)")

        # Gmail
        if s.email_provider == "gmail":
            if send_test:
                target = to or s.email_from_address
                if not target:
                    report(False, "Gmail", "give --to you@example.com or set EMAIL_FROM_ADDRESS")
                else:
                    try:
                        await integ.mailer.send(to_email=target, to_name="", subject=f"Test from {rules.firm_name}'s "
                                                f"assistant", text="If you can read this, email sending works.\n")
                        report(True, "Gmail", f"test email sent to {target}")
                    except IntegrationError as exc:
                        report(False, "Gmail", str(exc))
            else:
                report(True, "Gmail", "configured (run with --send-test to send a test email)")
        else:
            report(None, "Email", "EMAIL_PROVIDER=none (emails are written to the event log)")

        # Slack
        if s.slack_bot_token:
            try:
                r = await call(http, "POST", "https://slack.com/api/auth.test", service="Slack",
                               headers={"Authorization": f"Bearer {s.slack_bot_token}"})
                data = r.json()
                if not data.get("ok"):
                    raise IntegrationError(f"Slack: {data.get('error')}")
                detail = f"connected to workspace '{data.get('team')}' as {data.get('user')}"
                if send_test:
                    await integ.notifier.post(integ.channels["leads"], "Test message from the website assistant.")
                    detail += f"; test message posted to {integ.channels['leads']}"
                report(True, "Slack", detail)
            except IntegrationError as exc:
                report(False, "Slack", str(exc))
        else:
            report(None, "Slack", "SLACK_BOT_TOKEN not set (team messages are written to the event log)")

        # Jira
        if s.ticketing_provider == "jira":
            await check_jira(http, s)
        else:
            report(None, "Tickets", "TICKETING_PROVIDER=none (tickets are kept in the database only)")

        # Twilio (phone calls and SMS)
        if s.voice_provider == "twilio" or s.sms_provider == "twilio":
            await check_twilio(http, s, send_test, to)
        else:
            report(None, "Phone and SMS", "VOICE_PROVIDER=none, SMS_PROVIDER=none (browser voice calls still work "
                   "at /voice)")

        # QStash
        if s.job_runner == "qstash" or s.qstash_token:
            try:
                await call(http, "GET", f"{s.qstash_url.rstrip('/')}/v2/schedules", service="QStash",
                           headers={"Authorization": f"Bearer {s.qstash_token}"})
                report(True, "QStash", f"token works (JOB_RUNNER={s.job_runner})")
            except IntegrationError as exc:
                report(False, "QStash", str(exc))
        else:
            report(None, "QStash", f"not used (JOB_RUNNER={s.job_runner}: jobs run inside the server)")
    await integ.aclose()


async def check_jira(http: httpx.AsyncClient, s: Settings) -> None:
    base, auth = s.jira_base_url.rstrip("/"), (s.jira_email, s.jira_api_token)
    try:
        me = (await call(http, "GET", f"{base}/rest/api/3/myself", service="Jira", auth=auth)).json()
        r = await call(http, "GET", f"{base}/rest/api/3/project/{s.jira_project_key}", service="Jira", auth=auth,
                       ok=(200, 404))
        if r.status_code == 404:
            report(False, "Jira", f"signed in as {me.get('displayName')}, but project '{s.jira_project_key}' was not "
                   "found or this account cannot see it (check JIRA_PROJECT_KEY)")
            return
        types = [t.get("name") for t in r.json().get("issueTypes", [])]
        if s.jira_issue_type not in types:
            report(False, "Jira", f"project {s.jira_project_key} has no issue type '{s.jira_issue_type}'; it has: "
                   f"{', '.join(types)} (set JIRA_ISSUE_TYPE to one of them)")
            return
        report(True, "Jira", f"signed in as {me.get('displayName')}; project {s.jira_project_key} "
               f"({r.json().get('name')}) accepts '{s.jira_issue_type}' issues")
    except IntegrationError as exc:
        report(False, "Jira", str(exc))


async def check_twilio(http: httpx.AsyncClient, s: Settings, send_test: bool, to: str) -> None:
    auth = (s.twilio_account_sid, s.twilio_auth_token)
    base = f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}"
    try:
        acct = (await call(http, "GET", f"{base}.json", service="Twilio", auth=auth)).json()
        nums = (await call(http, "GET", f"{base}/IncomingPhoneNumbers.json", service="Twilio", auth=auth,
                           params={"PhoneNumber": s.twilio_phone_number})).json().get("incoming_phone_numbers", [])
    except IntegrationError as exc:
        report(False, "Twilio", str(exc))
        return
    if acct.get("type") == "Trial":
        report(False, "Twilio", "this is a trial account: Twilio blocks <ConversationRelay> (AI voice) and "
               "transfers to phone numbers on trials. Upgrade the account to take real calls (docs/SETUP.md).")
        return
    if not nums:
        report(False, "Twilio", f"signed in, but {s.twilio_phone_number or 'TWILIO_PHONE_NUMBER'} is not a number on "
               "this account (Phone Numbers > Manage > Active numbers)")
        return
    hook = nums[0].get("voice_url", "")
    want = s.public_api_url.rstrip("/") + "/api/voice/twilio/incoming"
    if s.voice_provider == "twilio" and hook != want:
        report(False, "Twilio", f"number found, but its 'A call comes in' webhook is '{hook or 'not set'}'; set it "
               f"to {want} (HTTP POST)")
        return
    detail = f"account '{acct.get('friendly_name')}', number {s.twilio_phone_number}"
    if s.voice_provider == "twilio":
        detail += f", calls go to {want}"
    if send_test and s.sms_provider == "twilio" and to.startswith("+"):
        try:
            await call(http, "POST", f"{base}/Messages.json", service="Twilio", auth=auth,
                       data={"To": to, "From": s.twilio_phone_number, "Body": "Test from the assistant: SMS works."})
            detail += f"; test SMS sent to {to}"
        except IntegrationError as exc:
            report(False, "Twilio SMS", str(exc))
            return
    report(True, "Twilio", detail)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send-test", action="store_true", help="send a test email and a test Slack message")
    ap.add_argument("--to", default="", help="where to send the test email")
    args = ap.parse_args()
    asyncio.run(check(get_settings(), args.send_test, args.to))
    print("\nAll configured integrations work." if not results else "\nFix the FAIL lines above (see docs/SETUP.md).")
    return 1 if results else 0


if __name__ == "__main__":
    sys.exit(main())
