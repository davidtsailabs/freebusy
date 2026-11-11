"""Reminders around every event the agent books. On by default; all of them go to the owner.

a. On the event itself: a pop-up 10 minutes before and an email 1 day before (configurable).
b. A text to the owner before the meeting, default 1 hour before, on the channel the owner
   picked at setup (Telegram, SMS, iMessage or email). In Hermes this is a one-shot scheduled
   job created through ``cronjob_manage`` (an ISO time is a run-once schedule); in fixture mode
   the job is written to the outbox instead.
c. A day-before briefing to the owner, assembled from what the agent knows about the thread:
   who, their organization and stated zone, what was asked and why, how the time was agreed,
   who sent the invite, the link or place, and open questions. Nothing from the owner's
   private calendar.
d. Never a reminder to the other person. The gate checks every reminder's target.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from .ontology import Person, Reminders, Slot
from .zones import format_day, format_slot_for, label_for

# The owner's choice at setup -> the Hermes delivery platform.
CHANNELS = {"telegram": "telegram", "sms": "sms", "imessage": "bluebubbles", "email": "email"}
CRON_TOOL = "cronjob_manage"


def event_reminders(r: Reminders) -> list[dict]:
    """The reminders set on the calendar event itself."""
    if not r.on:
        return []
    out = []
    if r.popup_min:
        out.append({"method": "popup", "minutes": int(r.popup_min)})
    if r.email_min:
        out.append({"method": "email", "minutes": int(r.email_min)})
    return out


def deliver_target(r: Reminders) -> str:
    platform = CHANNELS.get((r.channel or "email").lower(), "email")
    return f"{platform}:{r.target}" if r.target else platform


@dataclass
class Job:
    """One message to the owner at one time."""
    kind: str            # text | briefing
    at: str              # ISO 8601 with offset
    deliver: str         # Hermes delivery target, the owner's own
    text: str
    thread_id: str
    event_id: str = ""
    name: str = ""
    job_id: str = ""     # the outbox entry or the Hermes scheduled job


# ---- the texts ------------------------------------------------------------------

def reminder_text(person: Person, slot: Slot, zone: str, minutes: int, where: str) -> str:
    lead = f"In {minutes // 60} hour{'s' if minutes >= 120 else ''}" if minutes % 60 == 0 else f"In {minutes} minutes"
    return f"{lead}: {person.name}, {format_slot_for(slot.start_dt(), slot.end_dt(), zone, None)}. {where}"


_Q_SPLIT = re.compile(r"(?<=[.?!])\s+")
_SCHEDULING_Q = re.compile(r"\b(when|time|times|free|available|availability|slot|works?|week|day|call|meet|hour|minutes?|zone|"
                           r"\w+day|tomorrow|\d{1,2}(:\d{2})?\s*(am|pm)?)\b", re.IGNORECASE)


def open_questions(their_texts: list[str], limit: int = 2) -> list[str]:
    """Questions the other side asked that are not about scheduling (the agent answered those)."""
    out = []
    for t in their_texts:
        for sentence in _Q_SPLIT.split(t.replace("\n", " ")):
            s = sentence.strip()
            if s.endswith("?") and not _SCHEDULING_Q.search(s) and s not in out:
                out.append(s)
    return out[:limit]


def first_ask(their_texts: list[str]) -> str:
    for t in their_texts:
        for sentence in _Q_SPLIT.split(t.replace("\n", " ")):
            s = sentence.strip()
            if len(s) > 12 and not re.match(r"^(hi|hello|hey|dear|thanks|thank you)\b", s, re.IGNORECASE):
                return s if len(s) <= 120 else s[:117] + "..."
    return ""


def briefing_text(person: Person, org: str, slot: Slot, zone: str, subject: str, why: str, asked: str, agreed: str,
                  invite: str, where: str, questions: list[str], thread_id: str) -> str:
    """The day-before briefing: one short plain block in the Glance style, for the owner only."""
    when = f"{format_day(slot.start_dt(), zone)} · {format_slot_for(slot.start_dt(), slot.end_dt(), zone, None)}"
    said = f"said {label_for(person.zone)}" if person.zone and person.zone_source == "stated" else "zone not stated"
    lines = [f"TOMORROW: {person.name}, {when}",
             f"Who: {person.name}{', ' + org if org else ''} ({said})",
             f"About: {subject}" + (f". {why}" if why else ""),
             ]
    if asked:
        lines.append(f"They asked: \"{asked}\"")
    lines += [f"Agreed: {agreed}", f"Invite: {invite}", where,
              "Open: " + (" / ".join(f"\"{q}\"" for q in questions) if questions else "nothing open in the thread"),
              f"Thread: {thread_id}"]
    return "\n".join(lines)


def briefing_time(slot: Slot, zone: str, at: str, now: datetime) -> datetime:
    """The evening before at ``at`` in the owner's zone; now, when that has already passed."""
    tz = ZoneInfo(zone)
    day_before = slot.start_dt().astimezone(tz).date() - timedelta(days=1)
    h, m = (int(x) for x in (at or "17:00").split(":"))
    when = datetime.combine(day_before, time(h, m), tz)
    return when if when > now else now


# ---- where jobs go ------------------------------------------------------------------

class Outbox:
    """Fixture mode: every job is recorded here (and to a JSONL file when given) instead of being scheduled."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else None
        self.jobs: list[Job] = []

    def schedule(self, job: Job) -> str:
        self.jobs.append(job)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps(asdict(job), sort_keys=True) + "\n")
        return f"outbox-{len(self.jobs)}"


class SchedulingRefused(RuntimeError):
    """Raised instead of creating a job when Hermes' own rule says an agent may not schedule here."""


def in_cron_run() -> bool:
    """True inside a Hermes cron run (the session marker Hermes sets for cron-spawned agents)."""
    try:
        from gateway.session_context import get_session_env
        raw = get_session_env("HERMES_CRON_SESSION", "")
    except Exception:
        raw = os.environ.get("HERMES_CRON_SESSION", "")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def agent_scheduling_allowed() -> bool:
    """Hermes' ``cron.allow_agent_scheduling``. Unreadable config counts as no."""
    try:
        from hermes_cli.config import load_config
        return bool(((load_config() or {}).get("cron") or {}).get("allow_agent_scheduling"))
    except Exception:
        return False


class HermesCron:
    """Live mode: one run-once scheduled job per message, through Hermes' own cron tool and delivery.

    ``ctx.dispatch_tool`` does not apply the toolset filter that keeps a cron-run agent from scheduling,
    so the same rule is applied here: inside a cron run, no job is created unless the owner has set
    ``cron.allow_agent_scheduling: true``.
    """

    def __init__(self, dispatch: Callable[[str, dict], str], in_cron: Callable[[], bool] = in_cron_run,
                 allowed: Callable[[], bool] = agent_scheduling_allowed):
        self._dispatch = dispatch
        self._in_cron, self._allowed = in_cron, allowed
        self.created: list[str] = []

    def schedule(self, job: Job) -> str:
        if self._in_cron() and not self._allowed():
            raise SchedulingRefused("inside a cron run and cron.allow_agent_scheduling is off; no reminder job was created")
        prompt = ("Deliver this note to the owner exactly as written. Do not add to it, do not call tools, "
                  "and do not contact anyone else.\n\n" + job.text)
        raw = self._dispatch(CRON_TOOL, {"action": "create", "name": job.name or f"freebusy {job.kind} {job.thread_id}",
                                         "schedule": job.at, "repeat": 1, "prompt": prompt, "deliver": job.deliver})
        self.created.append(str(raw))
        try:
            return str(json.loads(raw).get("job_id") or json.loads(raw).get("id") or raw)
        except Exception:
            return str(raw)
