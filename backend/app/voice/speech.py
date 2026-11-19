"""Chat text -> text that sounds right when spoken by text-to-speech.

The model is told to answer in plain speech on calls, but replies still pass through here: Markdown,
links and lists are removed, and references and email addresses are spelled out so a caller can write
them down.
"""
from __future__ import annotations

import re

REFERENCE = re.compile(r"\b(DC|TK)[-‐-—−]([A-Z0-9]{6})\b")
EMAIL = re.compile(r"\b([\w.+-]+)@([\w-]+(?:\.[\w-]+)+)\b")
LINK = re.compile(r"\[([^\]]+)\]\((?:https?://)?[^)]+\)")
URL = re.compile(r"https?://\S+")
MARKS = re.compile(r"\*\*|__|(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)|`")
BULLET = re.compile(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+")
HEADING = re.compile(r"(?m)^\s*#+\s*")
ASIDE = re.compile(r"\s*[(\[][^()\[\]]{0,80}[)\]]")      # "(Waiting for email)": notes are not spoken


def spell(code: str) -> str:
    return " ".join(code)


def speakable(text: str) -> str:
    """Speakable version of one piece of reply text (whitespace at the ends is kept for streaming)."""
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    t = LINK.sub(r"\1", text)
    t = URL.sub("the link we'll send you", t)
    t = ASIDE.sub("", t)
    t = HEADING.sub("", t)
    t = BULLET.sub("", t)
    t = MARKS.sub("", t)
    t = REFERENCE.sub(lambda m: f"{spell(m.group(1))}, {spell(m.group(2))}", t)
    t = EMAIL.sub(lambda m: f"{m.group(1).replace('.', ' dot ')} at {m.group(2).replace('.', ' dot ')}", t)
    t = t.replace("e.g.", "for example").replace("i.e.", "that is").replace("&", " and ")
    t = re.sub(r"\s*[–—]\s*", ", ", t)
    # a line break after a list item or heading is a pause: "Cloud migration, Security"
    t = re.sub(r"(?<=[^\s.!?:;,])[ \t]*\n+[ \t]*", ", ", t)
    t = re.sub(r"[ \t]*\n+[ \t]*", " ", t)
    t = re.sub(r" {2,}", " ", t).strip()
    return f"{' ' if lead else ''}{t}{' ' if trail and t else ''}" if t else ("" if not (lead or trail) else " ")
