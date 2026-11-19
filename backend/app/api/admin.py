"""Admin dashboard API. Admins only (staff accounts with role admin, same sign-in as /team).

GET  /api/admin/overview?days=30          figures and a daily series for the period
GET  /api/admin/conversations             ?channel=web|voice &status=handover|booked|qualified|not_a_fit|new &q= &page=
GET  /api/admin/conversations/{id}        transcript, lead, bookings, calls, tickets, events (viewing is logged)
GET  /api/admin/leads | bookings | handovers | calls | events | jobs
GET  /api/admin/content                   editable files: title, help, edited or default, version
GET  /api/admin/content/{path}            current text, the default, history (?version=n for an old one)
PUT  /api/admin/content/{path}            {text, base_version, note}: checked, saved, applied at once
PUT  /api/admin/content-form/{path}       {entries: [{slug, title, body}], base_version, note}: the same, for
                                          FAQs, services, company facts and policies edited as a form
POST /api/admin/content/{path}/reset      {base_version}: back to the default file
DELETE /api/admin/content/{path}?base_version=n   remove an internal document

GET  /api/admin/privacy                   retention periods and the log of privacy requests and clean-ups
POST /api/admin/privacy/find              {email, phone}: what is held about a person (POST: keeps them out of
POST /api/admin/privacy/export            {email, phone}: a copy of it all                     the access logs)
POST /api/admin/privacy/erase             {email, phone, confirm}: delete it (confirm = the email or number again)
POST /api/admin/privacy/retention         run the nightly clean-up now
POST /api/admin/bookings/{id}/cancel      cancel a confirmed booking (the person gets the cancellation email)

Figures and lists need Postgres; content editing works with either storage.
"""
from __future__ import annotations

import re
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.admin.queries import AdminQueries
from app.api.team import current_user
from app.config import CONTENT_DIR
from app.content.forms import entries_of, header_of, is_list_file, render
from app.content.live import FILES, INTERNAL, ContentError, check_edit, editable
from app.content.store import ContentStore, EditConflict
from app.db.repo import PostgresRepo, Repo
from app.privacy.store import Person, PgPrivacy, PrivacyError, Retention
from app.staff.store import StaffUser

router = APIRouter(prefix="/api/admin", tags=["admin"])


async def admin_user(user: StaffUser = Depends(current_user)) -> StaffUser:
    if not user.is_admin:
        raise HTTPException(403, "The dashboard is for admins.")
    return user


def queries(request: Request) -> AdminQueries:
    repo = request.app.state.repo
    if not isinstance(repo, PostgresRepo):
        raise HTTPException(503, "The dashboard figures need the database (start it with docker compose up -d).")
    return AdminQueries(repo.pool)


def get_repo(request: Request) -> Repo:
    return request.app.state.repo


def content_store(request: Request) -> ContentStore:
    return request.app.state.content


# ---------------------------------------------------------------- figures and lists
@router.get("/overview")
async def overview(days: int = 30, _: StaffUser = Depends(admin_user),
                   q: AdminQueries = Depends(queries)) -> dict[str, Any]:
    return await q.overview(days)


@router.get("/conversations")
async def conversations(channel: str = "", status: str = "", q: str = "", page: int = 0,
                        _: StaffUser = Depends(admin_user), db: AdminQueries = Depends(queries)) -> dict[str, Any]:
    return {"items": await db.conversations(channel=channel, status=status, q=q, page=max(0, page))}


@router.get("/conversations/{conversation_id}")
async def conversation(conversation_id: str, request: Request, user: StaffUser = Depends(admin_user),
                       db: AdminQueries = Depends(queries), repo: Repo = Depends(get_repo)) -> dict[str, Any]:
    found = await db.conversation(conversation_id)
    if found is None:
        raise HTTPException(404, "Conversation not found.")
    await repo.log_event(None, "admin_viewed_conversation", {"user": user.id, "conversation": conversation_id})
    found["bookings"] = _consultant_names(request, found["bookings"])
    return found


@router.get("/leads")
async def leads(status: str = "", page: int = 0, _: StaffUser = Depends(admin_user),
                db: AdminQueries = Depends(queries)) -> dict[str, Any]:
    return {"items": await db.leads(status=status, page=max(0, page))}


def _consultant_names(request: Request, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bookings name the consultant by id (content/team.yaml); show the name."""
    names = {c.id: c.name for c in request.app.state.team.consultants}
    return [{**r, "consultant": names.get(str(r.get("consultant_id")), r.get("consultant_id"))} for r in rows]


@router.get("/bookings")
async def bookings(request: Request, upcoming: bool = True, page: int = 0, _: StaffUser = Depends(admin_user),
                   db: AdminQueries = Depends(queries)) -> dict[str, Any]:
    return {"items": _consultant_names(request, await db.bookings(upcoming=upcoming, page=max(0, page)))}


@router.get("/handovers")
async def handovers(page: int = 0, _: StaffUser = Depends(admin_user),
                    db: AdminQueries = Depends(queries)) -> dict[str, Any]:
    return {"items": await db.handovers(max(0, page))}


@router.get("/calls")
async def calls(page: int = 0, _: StaffUser = Depends(admin_user),
                db: AdminQueries = Depends(queries)) -> dict[str, Any]:
    return {"items": await db.calls(max(0, page))}


@router.get("/events")
async def events(kind: str = "", page: int = 0, _: StaffUser = Depends(admin_user),
                 db: AdminQueries = Depends(queries)) -> dict[str, Any]:
    return {"items": await db.events(kind=kind[:60], page=max(0, page))}


@router.get("/jobs")
async def jobs(status: str = "failed", _: StaffUser = Depends(admin_user),
               db: AdminQueries = Depends(queries)) -> dict[str, Any]:
    return {"items": await db.jobs(status)}


# ---------------------------------------------------------------- content editing
class ContentIn(BaseModel):
    text: str = Field(..., max_length=200_000)
    base_version: int = Field(0, ge=0)
    note: str = Field("", max_length=200)


class ResetIn(BaseModel):
    base_version: int = Field(..., ge=0)


def _default(path: str) -> str:
    f = CONTENT_DIR / path
    return f.read_text(encoding="utf-8") if f.is_file() else ""


def _doc_title(request: Request, path: str) -> str:
    """An internal document's title from its header; the file name if it has none."""
    try:
        text = (request.app.state.content_dir / path).read_text(encoding="utf-8")
        meta = yaml.safe_load(text.split("---", 2)[1]) if text.startswith("---") else None
        if isinstance(meta, dict) and str(meta.get("title") or "").strip():
            return str(meta["title"]).strip()
    except (OSError, IndexError, yaml.YAMLError):
        pass
    return path.removeprefix("internal/")


def _check_path(path: str) -> None:
    if not editable(path):
        raise HTTPException(404, "Not an editable file.")


@router.get("/content")
async def content_list(request: Request, _: StaffUser = Depends(admin_user),
                       store: ContentStore = Depends(content_store)) -> dict[str, Any]:
    edited = {f.path: f for f in await store.all()}
    items: list[dict[str, Any]] = []
    for f in FILES:
        e = edited.get(f.path)
        items.append({"path": f.path, "title": f.title, "help": f.help, "group": "assistant",
                      "edited": e is not None, "version": e.version if e else 0,
                      "updated_at": e.updated_at if e else None})
    defaults = sorted(str(p.relative_to(CONTENT_DIR)) for p in (CONTENT_DIR / "internal").rglob("*.md"))
    for path in sorted(set(defaults) | {p for p in edited if p.startswith("internal/")}):
        e = edited.get(path)
        if e and e.deleted:
            continue
        items.append({"path": path, "title": _doc_title(request, path), "group": "internal",
                      "help": "Internal document for the team assistant.", "edited": e is not None,
                      "version": e.version if e else 0, "updated_at": e.updated_at if e else None})
    return {"items": items, "internal_pattern": INTERNAL.pattern}


@router.get("/content/{path:path}")
async def content_get(path: str, version: int = 0, _: StaffUser = Depends(admin_user),
                      store: ContentStore = Depends(content_store)) -> dict[str, Any]:
    _check_path(path)
    current = await store.get(path)
    if version:
        old = await store.version_body(path, version)
        if old is None:
            raise HTTPException(404, "No such version.")
        return {"path": path, "text": old, "version": version}
    default = _default(path)
    text = current.body if current and not current.deleted else ("" if current else default)
    out: dict[str, Any] = {"path": path, "text": text, "default": default,
                           "version": current.version if current else 0, "edited": current is not None,
                           "history": [h.__dict__ for h in await store.history(path)]}
    if is_list_file(path):
        try:
            out["entries"] = entries_of(text)
        except (ValueError, yaml.YAMLError):
            out["entries"] = None        # the text is not a clean list right now: edit it as text
    return out


async def _apply(request: Request) -> None:
    from app.main import apply_content  # the app module wires everything that reads content
    await apply_content(request.app.state)


@router.put("/content/{path:path}")
async def content_save(path: str, body: ContentIn, request: Request, user: StaffUser = Depends(admin_user),
                       store: ContentStore = Depends(content_store), repo: Repo = Depends(get_repo)
                       ) -> dict[str, Any]:
    _check_path(path)
    text = body.text.replace("\r\n", "\n")
    try:
        check_edit(request.app.state.content_dir, path, text)
        saved = await store.save(path, text, base_version=body.base_version, user_id=user.id,
                                 note=" ".join(body.note.split()))
    except ContentError as exc:
        raise HTTPException(422, str(exc)) from exc
    except EditConflict as exc:
        raise HTTPException(409, "Someone saved this file after you opened it. Reload to see their version.") from exc
    await _apply(request)
    await repo.log_event(None, "admin_content_saved", {"user": user.id, "path": path, "version": saved.version})
    return {"path": path, "version": saved.version, "applied": True}


class FormIn(BaseModel):
    entries: list[dict[str, Any]] = Field(..., max_length=300)
    base_version: int = Field(0, ge=0)
    note: str = Field("", max_length=200)


@router.put("/content-form/{path:path}")
async def content_save_form(path: str, body: FormIn, request: Request, user: StaffUser = Depends(admin_user),
                            store: ContentStore = Depends(content_store), repo: Repo = Depends(get_repo)
                            ) -> dict[str, Any]:
    if not is_list_file(path):
        raise HTTPException(404, "This file is edited as text.")
    current = await store.get(path)
    header = header_of(current.body if current else _default(path))
    try:
        text = render(path, body.entries, header)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return await content_save(path, ContentIn(text=text, base_version=body.base_version, note=body.note),
                              request, user, store, repo)


@router.post("/content/{path:path}/reset")
async def content_reset(path: str, body: ResetIn, request: Request, user: StaffUser = Depends(admin_user),
                        store: ContentStore = Depends(content_store), repo: Repo = Depends(get_repo)
                        ) -> dict[str, Any]:
    _check_path(path)
    try:
        await store.reset(path, base_version=body.base_version, user_id=user.id)
    except EditConflict as exc:
        raise HTTPException(409, "Someone saved this file after you opened it. Reload to see their version.") from exc
    await _apply(request)
    await repo.log_event(None, "admin_content_reset", {"user": user.id, "path": path})
    return {"path": path, "version": 0, "applied": True}


@router.delete("/content/{path:path}")
async def content_delete(path: str, base_version: int, request: Request, user: StaffUser = Depends(admin_user),
                         store: ContentStore = Depends(content_store), repo: Repo = Depends(get_repo)
                         ) -> dict[str, Any]:
    if not INTERNAL.match(path):
        raise HTTPException(400, "Only internal documents can be removed; other files can be reset.")
    try:
        await store.save(path, "", base_version=base_version, user_id=user.id, note="removed", deleted=True)
    except EditConflict as exc:
        raise HTTPException(409, "Someone saved this file after you opened it. Reload to see their version.") from exc
    await _apply(request)
    await repo.log_event(None, "admin_content_removed", {"user": user.id, "path": path})
    return {"path": path, "removed": True}


# ---------------------------------------------------------------- privacy (GDPR)
def privacy(request: Request) -> PgPrivacy:
    repo = request.app.state.repo
    if not isinstance(repo, PostgresRepo):
        raise HTTPException(503, "Privacy tools need the database (start it with docker compose up -d).")
    return PgPrivacy(repo.pool)


class PersonIn(BaseModel):
    email: str = Field("", max_length=254)
    phone: str = Field("", max_length=40)
    confirm: str = Field("", max_length=254)

    def person(self) -> Person:
        try:
            return Person.parse(self.email, self.phone)
        except PrivacyError as exc:
            raise HTTPException(422, str(exc)) from exc


@router.get("/privacy")
async def privacy_overview(request: Request, _: StaffUser = Depends(admin_user),
                           db: PgPrivacy = Depends(privacy)) -> dict[str, Any]:
    return {"retention": Retention.from_rules(request.app.state.rules.privacy).describe(),
            "history": await db.history()}


@router.post("/privacy/find")
async def privacy_find(body: PersonIn, _: StaffUser = Depends(admin_user),
                       db: PgPrivacy = Depends(privacy)) -> dict[str, Any]:
    return await db.find(body.person())


@router.post("/privacy/export")
async def privacy_export(body: PersonIn, user: StaffUser = Depends(admin_user),
                         db: PgPrivacy = Depends(privacy), repo: Repo = Depends(get_repo)) -> dict[str, Any]:
    p = body.person()
    out = await db.export(p)
    await repo.log_event(None, "privacy_exported", {"user": user.id, "person": p.fingerprint,
                                                    "conversations": len(out["conversations"])})
    return out


def _confirmed(p: Person, confirm: str) -> bool:
    typed = confirm.strip().lower()
    digits = re.sub(r"\D", "", typed)
    return bool(typed) and ((p.email != "" and typed == p.email)
                            or (p.phone != "" and len(digits) >= len(p.phone) and digits.endswith(p.phone)))


@router.post("/privacy/erase")
async def privacy_erase(body: PersonIn, user: StaffUser = Depends(admin_user),
                        db: PgPrivacy = Depends(privacy)) -> dict[str, Any]:
    p = body.person()
    if not _confirmed(p, body.confirm):
        raise HTTPException(422, "To confirm, type the email address or phone number again.")
    try:
        return await db.erase(p, user.id)
    except PrivacyError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/privacy/retention")
async def privacy_run_retention(request: Request, user: StaffUser = Depends(admin_user),
                                db: PgPrivacy = Depends(privacy)) -> dict[str, Any]:
    return {"deleted": await db.purge(Retention.from_rules(request.app.state.rules.privacy), user_id=user.id)}


@router.post("/bookings/{booking_id}/cancel")
async def booking_cancel(booking_id: str, request: Request, user: StaffUser = Depends(admin_user),
                         repo: Repo = Depends(get_repo)) -> dict[str, Any]:
    service = request.app.state.booking
    try:
        booking = await service.store.get(booking_id)
    except ValueError:
        booking = None
    if booking is None:
        raise HTTPException(404, "Booking not found.")
    if booking.status != "confirmed":
        raise HTTPException(409, "This booking is already cancelled.")
    cancelled = await service.cancel(booking)
    await repo.log_event(booking.conversation_id, "admin_booking_cancelled",
                         {"user": user.id, "reference": booking.reference})
    return {"reference": cancelled.reference, "status": cancelled.status}
