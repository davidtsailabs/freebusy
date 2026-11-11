"""Google Workspace adapter: an inert stub for 0.2.0. Every method raises ``NotImplementedError``.

Nothing in this module dispatches a tool, runs a command or reads a credential.
Choosing ``mode: google_workspace`` builds the owner's profile from config and
then stops here: no calendar is read and none is written.

What is kept, because it is pure and tested: ``reduce_to_busy`` and
``reduce_to_places``, which strip an event list to start and end (or to a
travel fact) before anything could reach the model.

The route, for when it is wired:

* Hermes has no built-in calendar tools. Reads and writes go through the
  bundled ``google-workspace`` skill's real entry point
  (``scripts/google_api.py`` / ``scripts/gws_bridge.py``), so the plugin never
  sees a token.
* The script is resolved by the skill's installed absolute path, never a path
  relative to the terminal's working directory.
* Every argument is passed through ``shlex.quote``; calendar ids are config
  values and are treated as untrusted text.
* If the user has turned the terminal toolset off, the adapter refuses instead
  of dispatching around that choice.
* Writes go to one calendar, chosen by the owner at setup. The adapter refuses
  any other calendar id first.
* OAuth is not implemented here and never will be in this module: no token
  files, no ``.env`` reads, no keychain.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from ..ontology import Slot
from .base import WrongCalendar

NOT_WIRED = "the Google Workspace route is not wired in this version; use mode: fixture"


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
    """Inert in 0.2.0: holds the calendar ids, refuses a wrong write calendar, and raises on every call."""

    def __init__(self, write_calendar: str, read_calendars: list[str]):
        self.write_calendar = write_calendar
        self.read_calendars = list(read_calendars)

    def free_busy(self, calendar: str, start: datetime, end: datetime) -> list[Slot]:
        raise NotImplementedError(NOT_WIRED)

    def places(self, calendar: str, start: datetime, end: datetime, base: str, default_min: int = 30) -> list:
        raise NotImplementedError(NOT_WIRED)

    def create_event(self, calendar: str, slot: Slot, title: str, attendees: list[str],
                     description: str = "", tentative: bool = False, reminders: Optional[list[dict]] = None) -> str:
        if calendar != self.write_calendar:
            raise WrongCalendar(f"writes go to {self.write_calendar} only")
        raise NotImplementedError(NOT_WIRED)

    def delete_event(self, calendar: str, event_id: str) -> None:
        if calendar != self.write_calendar:
            raise WrongCalendar(f"writes go to {self.write_calendar} only")
        raise NotImplementedError(NOT_WIRED)
