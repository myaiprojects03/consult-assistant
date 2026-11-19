"""The consultants: whose calendar a call goes into and who is told about a lead or a handover.
Loaded from content/team.yaml, so staff change routing without touching code."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Consultant:
    id: str
    name: str
    calendar_id: str = "primary"
    email: str = ""
    slack_user_id: str = ""                                   # optional: @mention in Slack
    phone: str = ""                                           # E.164: live call transfers go here
    areas: list[str] = field(default_factory=list)           # words matched against the visitor's need
    handovers: list[str] = field(default_factory=list)       # handover reasons this person takes
    default: bool = False

    @property
    def mention(self) -> str:
        return f"<@{self.slack_user_id}>" if self.slack_user_id else self.name


@dataclass
class Team:
    consultants: list[Consultant]

    def __post_init__(self) -> None:
        if not self.consultants:
            raise ValueError("content/team.yaml needs at least one consultant.")
        ids = [c.id for c in self.consultants]
        if len(set(ids)) != len(ids):
            raise ValueError("content/team.yaml: consultant ids must be unique.")

    @property
    def default(self) -> Consultant:
        return next((c for c in self.consultants if c.default), self.consultants[0])

    def get(self, consultant_id: str) -> Consultant:
        return next((c for c in self.consultants if c.id == consultant_id), self.default)

    def for_need(self, need: str) -> Consultant:
        text = need.lower()
        best, score = self.default, 0
        for c in self.consultants:
            hits = sum(1 for a in c.areas if a.lower() in text)
            if hits > score:
                best, score = c, hits
        return best

    def for_handover(self, reason: str) -> Consultant:
        return next((c for c in self.consultants if reason in c.handovers), self.default)


def load_team(content_dir: Path) -> Team:
    path = content_dir / "team.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    people = (data or {}).get("consultants") or [{"id": "team", "name": "our team", "default": True}]
    return Team([Consultant(**p) for p in people])
