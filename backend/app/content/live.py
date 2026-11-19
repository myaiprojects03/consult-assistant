"""Which content staff can edit, how an edit is checked, and the folder the assistant reads from.

The assistant's loaders read a folder (content/). To apply dashboard edits without changing them, the app
builds an "effective" folder: a copy of content/ with every edited file written over it. An edit is
checked by building that folder with the edit in it and running every loader, so a file that would break
the assistant is refused with the loader's own message, before anything changes.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.booking.emails import load_sms, load_templates
from app.booking.slots import BookingConfig
from app.booking.team import load_team
from app.content.store import ContentFile
from app.kb.content import load_entries, load_internal_entries, load_rules
from app.privacy.store import Retention
from app.voice.rules import VoiceRules

MAX_BYTES = 200_000


@dataclass(frozen=True)
class Editable:
    path: str
    title: str
    help: str


FILES = [
    Editable("faqs.yaml", "FAQs", "Questions visitors ask often, with the approved answer."),
    Editable("services.yaml", "Services", "What the firm offers. The assistant describes services only from here."),
    Editable("company.yaml", "Company facts", "Who the firm is, where it works, how to reach it."),
    Editable("policies.yaml", "Policies", "Privacy, terms and other policies the assistant may explain."),
    Editable("rules.yaml", "Assistant rules", "Booking hours, lead questions, handover rules, voice greetings and "
             "calling hours."),
    Editable("team.yaml", "Consultants and routing", "Who takes which calls and handovers, their calendars and "
             "phone numbers."),
    Editable("emails.yaml", "Email wording", "Booking confirmation, change, cancellation and reminder emails."),
    Editable("sms.yaml", "Text message wording", "Booking text messages (sent to phone numbers)."),
]
INTERNAL = re.compile(r"^internal/(?:clients/)?[a-z0-9][a-z0-9-]{1,60}\.md$")


class ContentError(ValueError):
    """The edit would break the assistant; the message says why."""


def editable(path: str) -> bool:
    return path in {f.path for f in FILES} or bool(INTERNAL.match(path))


def write_overrides(folder: Path, files: list[ContentFile]) -> None:
    for f in files:
        target = folder / f.path
        if f.deleted:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f.body, encoding="utf-8")


def materialize(defaults: Path, files: list[ContentFile]) -> Path:
    """A new folder: the defaults with the edited files written over them."""
    folder = Path(tempfile.mkdtemp(prefix="content-"))
    shutil.copytree(defaults, folder, dirs_exist_ok=True)
    write_overrides(folder, files)
    return folder


def check_folder(folder: Path) -> None:
    """Run every loader the assistant uses; raise ContentError with a readable reason."""
    steps = [
        ("rules.yaml", lambda: VoiceRules.from_rules(load_rules(folder).voice)),
        ("rules.yaml (booking)", lambda: BookingConfig.from_rules(load_rules(folder).booking)),
        ("rules.yaml (privacy)", lambda: Retention.from_rules(load_rules(folder).privacy)),
        ("FAQs, services, company facts or policies", lambda: load_entries(folder)),
        ("internal documents", lambda: load_internal_entries(folder)),
        ("team.yaml", lambda: load_team(folder).default),
        ("emails.yaml", lambda: load_templates(folder)),
        ("sms.yaml", lambda: load_sms(folder)),
    ]
    for what, run in steps:
        try:
            run()
        except Exception as exc:     # any loader error means the assistant could not start with this
            raise ContentError(f"{what}: {type(exc).__name__}: {exc}") from exc


def check_edit(current: Path, path: str, body: str) -> None:
    """Would `current` with this file changed still load? Raises ContentError if not."""
    if not editable(path):
        raise ContentError("This file cannot be edited here.")
    if len(body.encode()) > MAX_BYTES:
        raise ContentError(f"Too long (the limit is {MAX_BYTES // 1000} KB).")
    if path.endswith(".yaml"):
        try:
            yaml.safe_load(body)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
            raise ContentError(f"Not valid YAML{where}: {getattr(exc, 'problem', exc)}") from exc
    elif not body.lstrip().startswith("---"):
        raise ContentError("An internal document starts with a header: ---, title:, kind:, (client:), ---.")
    with tempfile.TemporaryDirectory(prefix="content-check-") as tmp:
        folder = Path(tmp)
        shutil.copytree(current, folder, dirs_exist_ok=True)
        write_overrides(folder, [ContentFile(path, body, 0)])
        check_folder(folder)
