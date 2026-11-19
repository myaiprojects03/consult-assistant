"""Slack through the Web API (chat.postMessage) with a bot token from the app in
integrations/slack-app-manifest.yaml. The chat:write.public scope lets it post to public channels
without being invited first."""
from __future__ import annotations

import httpx

from app.integrations.base import IntegrationError, Notifier, call

API = "https://slack.com/api"
# Errors a retry cannot fix: report them at once.
PERMANENT = {"invalid_auth", "not_authed", "account_inactive", "token_revoked", "channel_not_found",
             "not_in_channel", "is_archived", "missing_scope", "msg_too_long", "no_text", "invalid_blocks"}


def escape(text: str) -> str:
    """Visitor text shown in Slack: no mentions (<!channel>, <@U…>) or links can be smuggled in."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SlackNotifier(Notifier):
    name = "Slack"

    def __init__(self, token: str, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(timeout=15, headers={"Authorization": f"Bearer {token}"})

    async def post(self, channel: str, text: str) -> None:
        r = await call(self.client, "POST", f"{API}/chat.postMessage", service="Slack",
                       json={"channel": channel, "text": text[:3900], "unfurl_links": False, "unfurl_media": False})
        data = r.json()
        if not data.get("ok"):
            err = data.get("error", "unknown_error")
            hint = " (invite the app to the channel or check the channel name)" if "channel" in err else ""
            raise IntegrationError(f"Slack: {err}{hint}", retryable=err not in PERMANENT)
