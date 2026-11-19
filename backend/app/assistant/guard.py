"""Output guard: the assistant never states a price or makes a promise, whatever the model writes.

The prompt asks the model not to; this guard enforces it. Replies stream sentence by sentence, and each
sentence is checked before the visitor sees it. A sentence with a price (an amount with a currency) not
on the approved list, or a guarantee or promise, is replaced with a safe line.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

PRICE_LINE = ("I can't quote prices here. A consultant will give you an exact figure in a written proposal "
              "after a free discovery call.")
PROMISE_LINE = "Commitments like that are set in a signed proposal, not in this chat."
# The assistant cannot contact anyone later, so it must not offer to.
FOLLOWUP_LINE = "You can ask me here any time for the latest status."
FOLLOWUP_RE = re.compile(
    r"\b(?:i|we)(?:'ll|’ll|\s+will|\s+shall)\s+(?:\w+\s+)?(?:let\s+you\s+know|keep\s+you\s+(?:posted|updated|informed)"
    r"|notify\s+you|update\s+you|get\s+back\s+to\s+you|follow\s+up\s+with\s+you|reach\s+out\s+to\s+you)\b",
    re.IGNORECASE)

_CURRENCY = r"(?:[$€£¥₹]|\b(?:usd|eur|gbp|pkr|inr|aud|cad|chf)\b)"
_AMOUNT = r"\d[\d,]*(?:\.\d+)?\s?(?:k|m|bn|million|thousand)?\b"
PRICE_RE = re.compile(
    rf"{_CURRENCY}\s?{_AMOUNT}|\b{_AMOUNT}\s?(?:dollars|euros|pounds|rupees|usd|eur|gbp|pkr)\b"
    r"|\b\d[\d,]*(?:\.\d+)?\s?(?:per|an?|/)\s?(?:hour|hr|day)\b",
    re.IGNORECASE)
PROMISE_RE = re.compile(
    r"\b(?:we|i|acme)\s+(?:can\s+|will\s+)?(?:guarantee|promise)\b|\bguaranteed\b|\bguarantee(?:s)?\s+(?:that|you)\b"
    r"|\b100\s?%\s+(?:uptime|secure|safe|success|satisfaction)\b|\bwe will definitely\b",
    re.IGNORECASE)
NEGATION_RE = re.compile(r"\b(?:can't|cannot|can not|not|no|don't|won't|never|unable to)\b[^.!?]{0,25}$",
                         re.IGNORECASE)
BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+|\n")
# Tool results carry machine start values (2026-11-20T14:00:00+05:00) for the model to pass back. A visitor
# must never see one, so they are removed, with a wrapper like "(start ...)" or "start=..." around them.
_DASH = "-\u2010\u2011\u2012\u2013"
MACHINE_TIME_RE = re.compile(
    rf"\s*(?:\(\s*)?(?:start\s*[=:]?\s*)?\d{{4}}[{_DASH}]\d{{2}}[{_DASH}]\d{{2}}T\d{{2}}:\d{{2}}(?::\d{{2}}(?:\.\d+)?)?"
    rf"(?:Z|[+{_DASH}\u2212]\d{{2}}:?\d{{2}})?(?:\s*\))?",
    re.IGNORECASE)


def scrub(sentence: str) -> str:
    return MACHINE_TIME_RE.sub("", sentence)


def _norm(s: str) -> str:
    return re.sub(r"[\s,]", "", s.lower())


@dataclass
class Verdict:
    text: str
    reason: str | None = None   # "price" | "promise" | "followup" when the sentence was replaced
    found: str = ""


def check_sentence(sentence: str, allowed_prices: list[str]) -> Verdict:
    allowed = {_norm(a) for a in allowed_prices}
    for m in PRICE_RE.finditer(sentence):
        if _norm(m.group(0)) not in allowed:
            return Verdict(PRICE_LINE, "price", m.group(0))
    for m in PROMISE_RE.finditer(sentence):
        if not NEGATION_RE.search(sentence[: m.start()]):
            return Verdict(PROMISE_LINE, "promise", m.group(0))
    m2 = FOLLOWUP_RE.search(sentence)
    if m2 and not NEGATION_RE.search(sentence[: m2.start()]):
        return Verdict(FOLLOWUP_LINE, "followup", m2.group(0))
    return Verdict(sentence)


@dataclass
class StreamGuard:
    """Feed model text in any pieces; get back checked text, released one finished sentence at a time."""
    allowed_prices: list[str]
    buffer: str = ""
    blocked: list[Verdict] = field(default_factory=list)
    _said: set[str] = field(default_factory=set)

    def _release(self, sentence: str, sep: str) -> str:
        sentence = scrub(sentence)
        if not sentence.strip():
            return sentence + sep
        v = check_sentence(sentence, self.allowed_prices)
        if v.reason is None:
            return sentence + sep
        self.blocked.append(v)
        if v.text in self._said:   # say each safe line once per reply
            return ""
        self._said.add(v.text)
        lead = sentence[: len(sentence) - len(sentence.lstrip())]
        return lead + v.text + sep

    def feed(self, text: str) -> str:
        self.buffer += text
        out, start = [], 0
        for m in BOUNDARY_RE.finditer(self.buffer):
            out.append(self._release(self.buffer[start:m.start()], m.group(0)))
            start = m.end()
        self.buffer = self.buffer[start:]
        return "".join(out)

    def flush(self) -> str:
        rest, self.buffer = self.buffer, ""
        return self._release(rest, "") if rest else ""
