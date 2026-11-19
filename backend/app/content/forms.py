"""Form editing for the list files (FAQs, services, company facts, policies).

Staff edit entries (a title and a body) in a form; the server writes the YAML, so nobody has to get
indentation right. Each entry keeps its slug, which is its identity in the knowledge base; a new entry
gets one made from its title.
"""
from __future__ import annotations

import json
import re
from typing import Any

import yaml

LIST_FILES = {"faqs.yaml": "faq-", "services.yaml": "", "company.yaml": "", "policies.yaml": ""}
MAX_ENTRIES = 300
PLAIN = re.compile(r"^[A-Za-z0-9][^:#\n\"'{}\[\],&*!|>%@`]*$")


def is_list_file(path: str) -> bool:
    return path in LIST_FILES


def header_of(text: str) -> str:
    """The comment lines at the top of a file (kept when the form rewrites it)."""
    lines = []
    for line in text.splitlines():
        if line.startswith("#"):
            lines.append(line)
        elif line.strip():
            break
    return "\n".join(lines)


def entries_of(text: str) -> list[dict[str, str]]:
    data = yaml.safe_load(text) or []
    if not isinstance(data, list):
        raise ValueError("This file is not a list of entries.")
    out = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("Every entry needs a title and a body.")
        body = str(item.get("body", "")).strip()
        body = re.sub(r"(?<!\n)\n(?!\n)", " ", body)     # line wraps in the file are not breaks; paragraphs stay
        out.append({"slug": str(item.get("slug", "")), "title": str(item.get("title", "")).strip(),
                    "body": body, "audience": str(item.get("audience", "public"))})
    return out


def slugify(title: str, prefix: str, taken: set[str]) -> str:
    base = prefix + (re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50] or "entry")
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def _scalar(v: str) -> str:
    return v if PLAIN.match(v) and v == v.strip() else json.dumps(v, ensure_ascii=False)


def render(path: str, entries: list[dict[str, Any]], header: str = "") -> str:
    """Entries -> the file's YAML. Raises ValueError for an entry without a title or body."""
    if len(entries) > MAX_ENTRIES:
        raise ValueError(f"At most {MAX_ENTRIES} entries.")
    taken = {str(e.get("slug") or "") for e in entries if e.get("slug")}
    blocks = []
    for i, e in enumerate(entries, 1):
        title = " ".join(str(e.get("title", "")).split())
        body = str(e.get("body", "")).replace("\r\n", "\n").strip()
        if not title or not body:
            raise ValueError(f"Entry {i} needs both a title and text.")
        slug = str(e.get("slug") or "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,80}", slug):
            slug = slugify(title, LIST_FILES[path], taken)
            taken.add(slug)
        lines = [f"- slug: {slug}", f"  title: {_scalar(title)}"]
        if str(e.get("audience", "public")) not in ("", "public"):
            lines.append(f"  audience: {_scalar(str(e['audience']))}")
        lines.append("  body: |")
        lines += [f"    {line}".rstrip() for line in body.split("\n")]
        blocks.append("\n".join(lines))
    return (header + "\n" if header else "") + "\n\n".join(blocks) + "\n"
