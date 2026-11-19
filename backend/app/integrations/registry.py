"""Builds the integrations chosen in settings. Anything not configured falls back to the logged stand-in."""
from __future__ import annotations

import httpx

from app.config import Settings
from app.db.repo import Repo
from app.integrations.base import (
    CRM,
    Calendar,
    IntegrationError,
    Integrations,
    Mailer,
    Notifier,
    Telephony,
    TextMessages,
    Ticketing,
)
from app.integrations.google import GmailMailer, GoogleAuth, GoogleCalendar
from app.integrations.hubspot import HubSpotCRM
from app.integrations.jira import JiraTicketing
from app.integrations.local import LocalCalendar, LocalCRM, LocalMailer, LocalNotifier, LocalSMS, LocalTicketing
from app.integrations.slack import SlackNotifier
from app.voice.twilio import TwilioClient, TwilioSMS, TwilioTelephony


def build_integrations(s: Settings, repo: Repo, firm_name: str) -> Integrations:
    clients: list[httpx.AsyncClient] = []
    google: GoogleAuth | None = None
    if s.calendar_provider == "google" or s.email_provider == "gmail":
        gclient = httpx.AsyncClient(timeout=20)
        clients.append(gclient)
        google = GoogleAuth(s.google_client_id, s.google_client_secret, s.google_refresh_token, gclient)

    crm: CRM
    if s.crm_provider == "hubspot":
        hubspot = HubSpotCRM(s.hubspot_access_token, s.hubspot_pipeline, s.hubspot_deal_stage)
        clients.append(hubspot.client)
        crm = hubspot
    elif s.crm_provider == "none":
        crm = LocalCRM(repo)
    else:
        raise IntegrationError(f"Unknown CRM_PROVIDER '{s.crm_provider}' (none, hubspot).", retryable=False)

    calendar: Calendar
    if s.calendar_provider == "google":
        assert google is not None
        calendar = GoogleCalendar(google)
    elif s.calendar_provider == "local":
        calendar = LocalCalendar(repo)
    else:
        raise IntegrationError(f"Unknown CALENDAR_PROVIDER '{s.calendar_provider}' (local, google).",
                               retryable=False)

    mailer: Mailer
    if s.email_provider == "gmail":
        assert google is not None
        mailer = GmailMailer(google, s.email_from_name or firm_name, s.email_from_address)
    elif s.email_provider == "none":
        mailer = LocalMailer(repo)
    else:
        raise IntegrationError(f"Unknown EMAIL_PROVIDER '{s.email_provider}' (none, gmail).", retryable=False)

    notifier: Notifier
    if s.slack_bot_token:
        slack = SlackNotifier(s.slack_bot_token)
        clients.append(slack.client)
        notifier = slack
    else:
        notifier = LocalNotifier(repo)

    ticketing: Ticketing
    if s.ticketing_provider == "jira":
        jira = JiraTicketing(s.jira_base_url, s.jira_email, s.jira_api_token, s.jira_project_key, s.jira_issue_type)
        clients.append(jira.client)
        ticketing = jira
    elif s.ticketing_provider == "none":
        ticketing = LocalTicketing(repo)
    else:
        raise IntegrationError(f"Unknown TICKETING_PROVIDER '{s.ticketing_provider}' (none, jira).", retryable=False)

    twilio: TwilioClient | None = None
    if s.voice_provider == "twilio" or s.sms_provider == "twilio":
        twilio = TwilioClient(s.twilio_account_sid, s.twilio_auth_token, s.twilio_phone_number)
        clients.append(twilio.client)
    for setting, value in (("VOICE_PROVIDER", s.voice_provider), ("SMS_PROVIDER", s.sms_provider)):
        if value not in ("none", "twilio"):
            raise IntegrationError(f"Unknown {setting} '{value}' (none, twilio).", retryable=False)
    sms: TextMessages = TwilioSMS(twilio) if twilio and s.sms_provider == "twilio" else LocalSMS(repo)
    telephony: Telephony | None = TwilioTelephony(twilio) if twilio and s.voice_provider == "twilio" else None

    channels = {"leads": s.slack_channel_leads, "handovers": s.slack_channel_handovers,
                "support": s.slack_channel_support or s.slack_channel_handovers,
                "alerts": s.slack_channel_alerts or s.slack_channel_handovers}
    return Integrations(crm, calendar, mailer, notifier, ticketing, sms, channels, clients, telephony)
