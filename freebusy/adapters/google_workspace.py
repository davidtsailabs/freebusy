"""Google Workspace adapter: a documented stub for 0.1.0.

How this is meant to work in Hermes, and why nothing here holds a credential:

* Hermes has no built-in calendar tools. The accepted route is the bundled
  ``google-workspace`` skill, whose script runs through the normal tool
  pipeline (``ctx.dispatch_tool("terminal", {...})`` or the skill's own tool),
  so approvals, redaction and budgets apply and the plugin never sees a token.
* The script is asked for events on one calendar between two instants. This
  adapter reduces the answer to busy ranges before anything reaches the model:
  ``reduce_to_busy`` keeps start and end and drops every other field.
* Writes go to one calendar, chosen by the owner at setup, through the same
  script. The adapter refuses any other calendar id before dispatching.
* OAuth is not implemented here and never will be in this module: no token
  files, no ``.env`` reads, no keychain. If a future version needs its own
  client it declares an ``oauth:`` block in ``plugin.yaml`` and calls
  ``credentialed_provider_request``, so Hermes attaches the token.

Until the dispatch is wired, every call raises ``NotImplementedError`` with
the command it would run, so a reviewer can see the exact surface.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Optional

from ..ontology import Slot
from .base import WrongCalendar

SKILL = "google-workspace"
SCRIPT = "scripts/gws.py"      # the bundled skill's entry point, as documented in its SKILL.md


def reduce_to_busy(events: list[dict[str, Any]]) -> list[Slot]:
    """Keep only start and end. Titles, attendees, descriptions and ids are dropped here."""
    out = []
    for ev in events:
        start = (ev.get("start") or {}).get("dateTime") or ev.get("start")
        end = (ev.get("end") or {}).get("dateTime") or ev.get("end")
        if isinstance(start, str) and isinstance(end, str) and "T" in start:
            if (ev.get("transparency") or "opaque") == "transparent":
                continue      # "free" events do not block
            out.append(Slot(start, end))
    return sorted(out, key=lambda s: s.start)


def reduce_to_places(events: list[dict[str, Any]], base: str, default_min: int = 30,
                     table: Optional[dict[str, int]] = None) -> list:
    """For travel buffers only: keep start, end, location and the conferencing link, derive a fact, drop the rest.

    Titles, attendees and descriptions are never read here. The location text stays in this function;
    what leaves is "in person, N minutes" or "virtual".
    """
    from ..travel import derive
    out = []
    for ev in events:
        busy = reduce_to_busy([ev])         # the same start/end rule as free/busy; free events do not count
        if not busy:
            continue
        entry = ((ev.get("conferenceData") or {}).get("entryPoints") or [{}])[0]
        fact = derive(busy[0], ev.get("location"), ev.get("hangoutLink") or entry.get("uri"), base, table, default_min)
        if fact:
            out.append(fact)
    return out


class GoogleWorkspaceAdapter:
    def __init__(self, write_calendar: str, read_calendars: list[str],
                 dispatch: Optional[Callable[[str, dict], str]] = None):
        self.write_calendar = write_calendar
        self.read_calendars = list(read_calendars)
        self._dispatch = dispatch     # ctx.dispatch_tool, injected by the plugin; None in fixture mode

    def _command(self, *parts: str) -> str:
        return " ".join(["python3", f"skills/{SKILL}/{SCRIPT}", *parts])

    def free_busy(self, calendar: str, start: datetime, end: datetime) -> list[Slot]:
        cmd = self._command("calendar", "events", "--calendar", calendar, "--from", start.isoformat(),
                            "--to", end.isoformat(), "--json")
        if self._dispatch is None:
            raise NotImplementedError(f"would dispatch through the tool pipeline: {cmd}")
        raw = self._dispatch("terminal", {"command": cmd})
        import json
        return reduce_to_busy(json.loads(raw) if isinstance(raw, str) else raw)

    def places(self, calendar: str, start: datetime, end: datetime, base: str, default_min: int = 30) -> list:
        cmd = self._command("calendar", "events", "--calendar", calendar, "--from", start.isoformat(),
                            "--to", end.isoformat(), "--fields", "start,end,location,hangoutLink,conferenceData,transparency", "--json")
        if self._dispatch is None:
            raise NotImplementedError(f"would dispatch through the tool pipeline: {cmd}")
        raw = self._dispatch("terminal", {"command": cmd})
        import json
        return reduce_to_places(json.loads(raw) if isinstance(raw, str) else raw, base, default_min)

    def create_event(self, calendar: str, slot: Slot, title: str, attendees: list[str],
                     description: str = "", tentative: bool = False, reminders: Optional[list[dict]] = None) -> str:
        if calendar != self.write_calendar:
            raise WrongCalendar(f"writes go to {self.write_calendar} only")
        guest_flags = [flag for a in attendees for flag in ("--attendee", a)]
        reminder_flags = [flag for r in (reminders or []) for flag in ("--reminder", f"{r['method']}:{r['minutes']}")]
        cmd = self._command("calendar", "create", "--calendar", calendar, "--start", slot.start, "--end", slot.end,
                            "--title", title, *guest_flags, *reminder_flags, "--conference")
        raise NotImplementedError(f"would dispatch through the tool pipeline: {cmd}")

    def delete_event(self, calendar: str, event_id: str) -> None:
        if calendar != self.write_calendar:
            raise WrongCalendar(f"writes go to {self.write_calendar} only")
        raise NotImplementedError(self._command("calendar", "delete", "--calendar", calendar, "--id", event_id))
