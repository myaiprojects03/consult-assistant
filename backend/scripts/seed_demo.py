"""Add two sample clients with projects, so client support and the team assistant can be tried end to end.

    python -m scripts.seed_demo                          clients and projects
    python -m scripts.seed_demo --contact you@example.com
        also lets that email speak for Northwind Traders in the website chat (the verification code is sent
        there, so use an inbox you can read)

Safe to run again: clients and projects are updated, not duplicated. Restart the API afterwards so the
client-specific internal documents (content/internal/clients/) are loaded.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from app.clients.store import Client, ClientStore, PgClientStore, Project
from app.config import get_settings
from app.db.migrations import connect

DEMO: list[dict[str, Any]] = [
    {"slug": "northwind", "name": "Northwind Traders", "tier": "enterprise", "account_manager": "sam",
     "contacts": [("it.lead@northwind.example", "Jordan Lee", "IT lead")],
     "projects": [
         {"name": "Azure landing zone", "status": "in_progress", "phase": "Build (phase 2 of 3)",
          "next_milestone": "Production subscription handover", "days": 12, "owner": "sam",
          "client_update": "Networking and identity are in place; workloads move over in the next sprint."},
         {"name": "Managed security monitoring", "status": "in_progress", "phase": "Ongoing service",
          "next_milestone": "Quarterly security review", "days": 30, "owner": "sam",
          "client_update": "Monitoring is live for all offices; no open incidents."}]},
    {"slug": "brightwater", "name": "Brightwater Dental", "tier": "sme", "account_manager": "sam",
     "contacts": [("office@brightwater.example", "Alex Morgan", "Practice manager")],
     "projects": [
         {"name": "Microsoft 365 migration", "status": "in_progress", "phase": "Pilot",
          "next_milestone": "Move the remaining 14 mailboxes", "days": 6, "owner": "sam",
          "client_update": "Pilot users are on Microsoft 365; the rest of the practice moves next week."}]},
]


async def seed(clients: ClientStore, contact: str = "") -> dict[str, Client]:
    out: dict[str, Client] = {}
    today = datetime.now(UTC).date()
    for d in DEMO:
        c = await clients.upsert_client(d["slug"], d["name"], d["tier"], d["account_manager"])
        for email, name, role in d["contacts"]:
            await clients.add_contact(c.id, email, name, role)
        for p in d["projects"]:
            await clients.upsert_project(Project(
                id="", client_id=c.id, name=p["name"], status=p["status"], phase=p["phase"],
                next_milestone=p["next_milestone"], milestone_date=today + timedelta(days=p["days"]),
                client_update=p["client_update"], owner=p["owner"]))
        out[c.slug] = c
    if contact:
        await clients.add_contact(out["northwind"].id, contact, "", "Demo contact")
    return out


async def main(contact: str) -> int:
    s = get_settings()
    try:
        pool = await connect(s.database_url)
    except Exception as exc:
        print(f"Database is not reachable ({exc}). Start it with: docker compose up -d")
        return 1
    try:
        made = await seed(PgClientStore(pool), contact.strip().lower())
    finally:
        await pool.close()
    for c in made.values():
        print(f"OK  {c.name} ({c.tier}), id {c.slug}")
    if contact:
        print(f"OK  {contact} can now verify as Northwind Traders in the website chat")
    print("Restart the API so the client documents in content/internal/clients/ are loaded.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--contact", default="", help="an email you can read, to verify as Northwind in the chat")
    sys.exit(asyncio.run(main(ap.parse_args().contact)))
