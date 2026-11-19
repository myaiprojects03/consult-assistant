"""HubSpot CRM (free plan) through its REST API, with a static access token from a single-account app
(set up with the HubSpot CLI: integrations/hubspot-app/README.md).

One conversation becomes: a contact (matched by email, so a returning visitor updates the same record),
a deal once a discovery call is booked, and one note with the lead details and conversation summary
that is updated on every sync instead of piling up. Empty values never overwrite what is in HubSpot.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from app.integrations.base import CRM, CRMRecord, CRMResult, IntegrationError, call

API = "https://api.hubapi.com"
# HubSpot-defined association type ids
DEAL_TO_CONTACT = 3
NOTE_TO_CONTACT = 202
NOTE_TO_DEAL = 214
LEAD_STATUS = {"new": "NEW", "qualified": "OPEN", "not_a_fit": "UNQUALIFIED"}


def _assoc(object_id: str, type_id: int) -> dict[str, Any]:
    return {"to": {"id": object_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED",
                                                "associationTypeId": type_id}]}


def contact_properties(r: CRMRecord) -> dict[str, str]:
    first, _, last = r.name.strip().partition(" ")
    props = {"firstname": first, "lastname": last.strip(), "phone": r.phone, "company": r.company,
             "jobtitle": r.job_title,
             "hs_lead_status": "OPEN_DEAL" if r.booked else LEAD_STATUS.get(r.lead_status, "NEW")}
    return {k: v for k, v in props.items() if v}


class HubSpotCRM(CRM):
    name = "HubSpot"

    def __init__(self, token: str, pipeline: str = "default", deal_stage: str = "appointmentscheduled",
                 client: httpx.AsyncClient | None = None):
        if not token:
            raise IntegrationError("HubSpot: HUBSPOT_ACCESS_TOKEN is not set.", retryable=False)
        self.pipeline, self.deal_stage = pipeline, deal_stage
        self.client = client or httpx.AsyncClient(base_url=API, timeout=20,
                                                  headers={"Authorization": f"Bearer {token}"})

    async def _call(self, method: str, path: str, ok: tuple[int, ...] = (200, 201, 204), **kw: Any) -> httpx.Response:
        return await call(self.client, method, path, service="HubSpot", ok=ok, **kw)

    async def sync(self, record: CRMRecord) -> CRMResult:
        r = await self._call("POST", "/crm/v3/objects/contacts/batch/upsert", json={"inputs": [
            {"idProperty": "email", "id": record.email.lower(), "properties": contact_properties(record)}]})
        results = r.json().get("results") or []
        if not results:
            raise IntegrationError(f"HubSpot: contact upsert returned no result: {r.text[:200]}")
        contact_id = str(results[0]["id"])

        deal_id, new_deal = record.deal_id, False
        if record.booked:
            if deal_id:
                r = await self._call("PATCH", f"/crm/v3/objects/deals/{deal_id}", ok=(200, 404),
                                     json={"properties": {"dealname": record.deal_name}})
                if r.status_code == 404:     # deleted in HubSpot: create it again
                    deal_id = ""
            if not deal_id:
                r = await self._call("POST", "/crm/v3/objects/deals", json={
                    "properties": {"dealname": record.deal_name, "pipeline": self.pipeline,
                                   "dealstage": self.deal_stage},
                    "associations": [_assoc(contact_id, DEAL_TO_CONTACT)]})
                deal_id, new_deal = str(r.json()["id"]), True

        note_id = record.note_id
        if note_id:
            r = await self._call("PATCH", f"/crm/v3/objects/notes/{note_id}", ok=(200, 404),
                                 json={"properties": {"hs_note_body": record.note_html}})
            if r.status_code == 404:
                note_id = ""
            elif new_deal:
                await self._call("PUT", f"/crm/v4/objects/notes/{note_id}/associations/default/deals/{deal_id}")
        if not note_id:
            assoc = [_assoc(contact_id, NOTE_TO_CONTACT)] + ([_assoc(deal_id, NOTE_TO_DEAL)] if deal_id else [])
            r = await self._call("POST", "/crm/v3/objects/notes", json={
                "properties": {"hs_timestamp": datetime.now(UTC).isoformat(timespec="milliseconds")
                               .replace("+00:00", "Z"), "hs_note_body": record.note_html},
                "associations": assoc})
            note_id = str(r.json()["id"])
        return CRMResult(contact_id=contact_id, deal_id=deal_id, note_id=note_id)
