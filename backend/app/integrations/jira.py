"""Jira Cloud (free plan) through REST API v3, signed in with an Atlassian account email and API token.

A ticket from the assistant becomes an issue in one project (JIRA_PROJECT_KEY), labelled with our
reference, the client and the priority. The reference label makes creation safe to retry: before
creating, we look for an issue that already carries it. Status is read live from Jira, so what a
client is told always matches what the consultant set. Comment text is never shown to clients
(consultants may write internal notes there); only the status, the assignee's name and the date.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from app.integrations.base import IntegrationError, TicketInfo, Ticketing, TicketState, call

STAGES = {"new": "open", "indeterminate": "in_progress", "done": "resolved"}
# Jira's default priority scheme. If a project has no Priority field, the issue is created without it.
JIRA_PRIORITY = {"low": "Low", "normal": "Medium", "high": "High", "urgent": "Highest"}


def adf(*paragraphs: str) -> dict[str, Any]:
    """Atlassian Document Format: one paragraph per non-empty line."""
    lines = [line for p in paragraphs for line in p.splitlines() if line.strip()]
    return {"type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": line[:4000]}]} for line in lines]}


def ref_label(reference: str) -> str:
    return "assistant-" + reference.lower()


class JiraTicketing(Ticketing):
    name = "Jira"

    def __init__(self, base_url: str, email: str, api_token: str, project_key: str, issue_type: str = "Task",
                 client: httpx.AsyncClient | None = None):
        missing = [n for n, v in (("JIRA_BASE_URL", base_url), ("JIRA_EMAIL", email), ("JIRA_API_TOKEN", api_token),
                                  ("JIRA_PROJECT_KEY", project_key)) if not v]
        if missing:
            raise IntegrationError(f"Jira: {', '.join(missing)} not set (see docs/SETUP.md).", retryable=False)
        self.base = base_url.rstrip("/")
        self.project, self.issue_type = project_key, issue_type
        self.client = client or httpx.AsyncClient(base_url=self.base, timeout=20, auth=(email, api_token),
                                                  headers={"Accept": "application/json"})

    async def _call(self, method: str, path: str, ok: tuple[int, ...] = (200, 201, 204), **kw: Any) -> httpx.Response:
        return await call(self.client, method, path, service="Jira", ok=ok, **kw)

    async def _find(self, label: str) -> str | None:
        r = await self._call("POST", "/rest/api/3/search/jql", json={
            "jql": f'project = "{self.project}" AND labels = "{label}"', "fields": ["key"], "maxResults": 1})
        issues = r.json().get("issues") or []
        return str(issues[0]["key"]) if issues else None

    async def create(self, t: TicketInfo) -> tuple[str, str]:
        label = ref_label(t.reference)
        key = await self._find(label)            # created by an earlier attempt whose answer was lost
        if not key:
            prefix = "[Urgent] " if t.priority == "urgent" else ""
            fields = {
                "project": {"key": self.project},
                "issuetype": {"name": self.issue_type},
                "summary": f"{prefix}{t.client_name}: {t.summary}"[:250],
                "labels": [label, f"client-{t.client_slug}", f"priority-{t.priority}", f"category-{t.category}"],
                "description": adf(f"Client: {t.client_name}", f"Contact: {t.contact_email}",
                                   f"Priority: {t.priority}", f"Category: {t.category}",
                                   f"Reference: {t.reference}", "", t.details or t.summary, "",
                                   "Raised through the website assistant."),
            }
            fields["priority"] = {"name": JIRA_PRIORITY.get(t.priority, "Medium")}
            r = await self._call("POST", "/rest/api/3/issue", json={"fields": fields}, ok=(201, 400))
            if r.status_code == 400 and "priority" in r.text.lower():
                fields.pop("priority")            # this project does not use Jira's Priority field
                r = await self._call("POST", "/rest/api/3/issue", json={"fields": fields})
            elif r.status_code == 400:
                raise IntegrationError(f"Jira: HTTP 400: {r.text[:300]}", retryable=False)
            key = str(r.json()["key"])
        return key, f"{self.base}/browse/{key}"

    async def comment(self, key: str, text: str, marker: str) -> None:
        r = await self._call("GET", f"/rest/api/3/issue/{key}/comment", params={"orderBy": "-created",
                                                                               "maxResults": 20})
        if marker in json.dumps(r.json().get("comments", [])):
            return                                # an earlier attempt already added it
        await self._call("POST", f"/rest/api/3/issue/{key}/comment", json={"body": adf(text, f"({marker})")})

    async def state(self, key: str) -> TicketState | None:
        r = await self._call("GET", f"/rest/api/3/issue/{key}", ok=(200, 404),
                             params={"fields": "status,assignee,updated"})
        if r.status_code == 404:
            return None
        f = r.json().get("fields", {})
        status = f.get("status") or {}
        category = (status.get("statusCategory") or {}).get("key", "new")
        return TicketState(status=status.get("name", ""), stage=STAGES.get(category, "open"),
                           assignee=(f.get("assignee") or {}).get("displayName", ""), updated=f.get("updated", ""))
