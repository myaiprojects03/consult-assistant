"""Staff accounts for the team assistant (/team). Passwords are typed at a hidden prompt, never as arguments.

    python -m scripts.staff add sam@acme.example "Sam Patel" --role consultant --consultant sam
    python -m scripts.staff add you@acme.example "Your Name" --role admin
    python -m scripts.staff grant sam@acme.example northwind      a consultant may see that client
    python -m scripts.staff revoke sam@acme.example northwind
    python -m scripts.staff password sam@acme.example             new password, signs out everywhere
    python -m scripts.staff disable sam@acme.example              (enable to undo)
    python -m scripts.staff list

Admins see every client. Consultants see only the clients granted to them, plus general documents.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

import asyncpg

from app.clients.store import PgClientStore
from app.config import get_settings
from app.db.migrations import connect
from app.staff.store import MIN_PASSWORD, PgStaffStore, StaffUser, hash_password


def ask_password() -> str:
    while True:
        pw = getpass.getpass(f"Password (at least {MIN_PASSWORD} characters): ")
        if len(pw) < MIN_PASSWORD:
            print("Too short.")
            continue
        if getpass.getpass("Again: ") != pw:
            print("They do not match.")
            continue
        return pw


async def find_user(staff: PgStaffStore, email: str) -> StaffUser:
    found = await staff.user_with_hash(email)
    if not found:
        raise SystemExit(f"No staff user {email}. Add one with: python -m scripts.staff add ...")
    return found[0]


async def run(a: argparse.Namespace, pool: asyncpg.Pool) -> None:
    staff, clients = PgStaffStore(pool), PgClientStore(pool)
    if a.cmd == "list":
        rows = await pool.fetch(
            "SELECT u.email, u.name, u.role, u.active, coalesce(string_agg(c.slug, ', ' ORDER BY c.slug), '') AS "
            "clients FROM staff_users u LEFT JOIN staff_client_access a ON a.user_id = u.id "
            "LEFT JOIN clients c ON c.id = a.client_id GROUP BY u.id ORDER BY u.email")
        for r in rows:
            scope = "all clients" if r["role"] == "admin" else (r["clients"] or "no clients yet")
            print(f"{r['email']:<32} {r['name']:<20} {r['role']:<10} {'active' if r['active'] else 'DISABLED':<8} "
                  f"{scope}")
        if not rows:
            print("No staff users yet.")
        return
    email = a.email.strip().lower()
    if a.cmd == "add":
        if await staff.user_with_hash(email):
            raise SystemExit(f"{email} already exists (use 'password' to change the password).")
        user = await staff.create_user(email, a.name, a.role, hash_password(ask_password()), a.consultant)
        print(f"OK  added {user.email} ({user.role})")
        return
    user = await find_user(staff, email)
    if a.cmd in ("grant", "revoke"):
        client = next((c for c in await clients.list_clients(None) if c.slug == a.client), None)
        if client is None:
            raise SystemExit(f"No client '{a.client}'. Clients: "
                             + ", ".join(c.slug for c in await clients.list_clients(None)))
        await (staff.grant if a.cmd == "grant" else staff.revoke)(user.id, client.id)
        print(f"OK  {user.email} {'can now see' if a.cmd == 'grant' else 'can no longer see'} {client.name}")
    elif a.cmd == "password":
        await staff.set_password(user.id, hash_password(ask_password()))
        print(f"OK  password changed; {user.email} is signed out everywhere")
    elif a.cmd in ("disable", "enable"):
        await staff.set_active(user.id, a.cmd == "enable")
        print(f"OK  {user.email} {a.cmd}d")


def main() -> int:
    ap = argparse.ArgumentParser(description="Staff accounts for the team assistant")
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add")
    add.add_argument("email")
    add.add_argument("name")
    add.add_argument("--role", choices=["consultant", "admin"], default="consultant")
    add.add_argument("--consultant", default="", help="their id in content/team.yaml, e.g. sam")
    for cmd in ("grant", "revoke"):
        p = sub.add_parser(cmd)
        p.add_argument("email")
        p.add_argument("client", help="client id, e.g. northwind")
    for cmd in ("password", "disable", "enable"):
        sub.add_parser(cmd).add_argument("email")
    sub.add_parser("list")
    a = ap.parse_args()

    async def go() -> int:
        try:
            pool = await connect(get_settings().database_url)
        except Exception as exc:
            print(f"Database is not reachable ({exc}). Start it with: docker compose up -d")
            return 1
        try:
            await run(a, pool)
        finally:
            await pool.close()
        return 0
    return asyncio.run(go())


if __name__ == "__main__":
    sys.exit(main())
