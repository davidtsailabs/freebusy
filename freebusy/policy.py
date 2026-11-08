"""The policy gate (pre_tool_call) and the audit line.

Nothing sends or writes without passing here. A send or a calendar write is
blocked when it lacks a held slot, a known or stated zone for every zone it
shows, an authority match for the tier, when it targets a recipient who is
not on the thread, or a calendar other than the configured one. The reason
comes back as {"action": "block", "message": ...}. Every decision is one
JSONL line with secrets redacted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .holds import HoldLedger
from .ontology import AuthorityTier, OwnerProfile, Slot
from .zones import ABBREVIATIONS

SEND_TOOLS = {"schedule_send", "send_email", "email_send", "gmail_send", "mail_send", "reply_email"}
WRITE_TOOLS = {"schedule_book", "calendar_create_event", "create_event", "calendar_write", "book_event"}

_ZONE_WORDS = re.compile(r"(?<![A-Za-z])(" + "|".join(k for k in ABBREVIATIONS if len(k) <= 4) + r")(?![A-Za-z])", re.IGNORECASE)
_SECRET_KEYS = re.compile(r"(token|secret|password|passwd|authorization|api[_-]?key|credential|cookie|bearer)", re.IGNORECASE)
_SECRET_VALUES = re.compile(r"\b(ya29\.[A-Za-z0-9_\-]+|sk-[A-Za-z0-9]{8,}|Bearer\s+[A-Za-z0-9._\-]+|[A-Za-z0-9_\-]{40,})\b")


@dataclass
class ThreadFacts:
    """What the gate knows about one thread. Filled by the tools as the thread moves."""
    thread_id: str
    participants: set[str] = field(default_factory=set)
    person_id: str = ""
    person_zone: Optional[str] = None       # IANA, stated only
    authority: str = "unknown"
    tier: int = AuthorityTier.OBSERVE
    owner_approved: bool = False            # the owner said yes to this thread's booking
    offer_rounds: int = 0


def block(message: str) -> dict:
    return {"action": "block", "message": message}


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("[redacted]" if _SECRET_KEYS.search(str(k)) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return _SECRET_VALUES.sub("[redacted]", obj)
    return obj


class Gate:
    def __init__(self, profile: OwnerProfile, ledger: HoldLedger, audit_path: Optional[Path] = None):
        self.profile = profile
        self.ledger = ledger
        self.audit_path = Path(audit_path) if audit_path else None
        self.threads: dict[str, ThreadFacts] = {}
        self.last: list[dict] = []

    def facts(self, thread_id: str) -> ThreadFacts:
        return self.threads.setdefault(thread_id, ThreadFacts(thread_id=thread_id))

    # -- the hook ----------------------------------------------------------------
    def pre_tool_call(self, tool_name: str = "", args: Optional[dict] = None, task_id: str = "", **kwargs) -> Optional[dict]:
        args = args or {}
        try:
            if tool_name in SEND_TOOLS:
                verdict = self._check_send(args)
            elif tool_name in WRITE_TOOLS:
                verdict = self._check_write(args)
            else:
                return None
        except Exception as exc:  # a gate that cannot decide blocks
            verdict = block(f"policy check failed, so the action is blocked: {exc}")
        self.audit({"tool": tool_name, "thread": args.get("thread_id"), "verdict": "block" if verdict else "allow",
                    "reason": (verdict or {}).get("message", "")})
        return verdict

    def _check_send(self, a: dict) -> Optional[dict]:
        t = self.facts(str(a.get("thread_id", "")))
        if not a.get("thread_id"):
            return block("a send needs a thread; nothing goes out unattached")
        if t.tier < AuthorityTier.OFFER:
            return block(f"authority is '{t.authority}' (tier {int(t.tier)}); tier 1 or higher is needed to reply. Draft for the owner instead.")
        recipients = {r.lower() for r in [*a.get("to", []), *a.get("cc", [])] if r}
        strangers = recipients - t.participants - {x.lower() for x in self.profile.owner_addresses} - {self.profile.agent_address.lower()}
        if strangers:
            return block(f"recipient not on the thread: {', '.join(sorted(strangers))}")
        body = str(a.get("body", ""))
        zones = {ABBREVIATIONS[m.group(1).lower()][0] for m in _ZONE_WORDS.finditer(body)}
        allowed = {self.profile.home_zone, t.person_zone} if t.person_zone else {self.profile.home_zone}
        extra = zones - allowed
        if extra:
            return block(f"the reply shows a time in a zone the person never stated ({', '.join(sorted(extra))}); ask their zone instead")
        for s in a.get("slots", []):
            slot = Slot(s["start"], s["end"]) if isinstance(s, dict) else s
            if not self.ledger.find(t.thread_id, slot):
                return block(f"offered slot {slot.start} is not held for this thread; hold first, then send")
        return None

    def _check_write(self, a: dict) -> Optional[dict]:
        t = self.facts(str(a.get("thread_id", "")))
        cal = a.get("calendar", "")
        if cal != self.profile.write_calendar:
            return block(f"calendar write targets '{cal}'; the only writable calendar is '{self.profile.write_calendar}'")
        slot = a.get("slot")
        slot = Slot(slot["start"], slot["end"]) if isinstance(slot, dict) else slot
        if slot is None or not self.ledger.find(t.thread_id, slot):
            return block("no held slot for this booking; book only what was offered and held")
        if not t.person_zone and not a.get("zone_confirmed"):
            return block("the person's zone is not known; a booking confirms a time in a zone they stated")
        guests = {g.lower() for g in a.get("attendees", [])}
        if guests - t.participants:
            return block(f"attendee not on the thread: {', '.join(sorted(guests - t.participants))}")
        length = (slot.end_dt() - slot.start_dt()).total_seconds() / 60
        needs_owner = length > self.profile.max_len_without_owner_min
        if t.tier < AuthorityTier.BOOK and not t.owner_approved:
            return block(f"tier {int(t.tier)} may offer but not book; the owner approves with /schedule approve {t.thread_id}")
        if needs_owner and not t.owner_approved:
            return block(f"a {int(length)}-minute meeting needs the owner's approval before booking")
        return None

    # -- audit ----------------------------------------------------------------
    def audit(self, event: dict) -> dict:
        line = {"at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(), **redact(event)}
        self.last.append(line)
        if self.audit_path:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a") as f:
                f.write(json.dumps(line, sort_keys=True) + "\n")
        return line
