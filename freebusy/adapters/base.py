"""The adapter interface.

Calendars are read as free or busy only: ``free_busy`` returns time ranges and
nothing else, so titles, attendees and descriptions never reach the model.
``create_event`` writes to the configured calendar only and refuses any other.
Mail adapters list the threads that reached the agent and send a finished
message; they never mine the owner's inbox.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Protocol

from ..ontology import Slot
from ..parse import Email


class WrongCalendar(PermissionError):
    pass


class CalendarAdapter(Protocol):
    write_calendar: str
    read_calendars: list[str]

    def free_busy(self, calendar: str, start: datetime, end: datetime) -> list[Slot]:
        """Busy ranges on one calendar between start and end. Ranges only."""

    def create_event(self, calendar: str, slot: Slot, title: str, attendees: list[str],
                     description: str = "", tentative: bool = False) -> str:
        """Create one event on the configured calendar; return its id. Raise WrongCalendar otherwise."""

    def delete_event(self, calendar: str, event_id: str) -> None:
        """Delete an event the plugin itself created (holds), on the configured calendar only."""


class MailAdapter(Protocol):
    def list_threads(self) -> list[list[Email]]:
        """Threads that reached the agent (a trigger matched), oldest message first."""

    def send(self, thread_id: str, to: list[str], cc: list[str], subject: str, text: str,
             html: Optional[str] = None) -> str:
        """Send one message in a thread; return its message id."""


def all_busy(cal: CalendarAdapter, start: datetime, end: datetime) -> list[Slot]:
    """Union of busy ranges across every configured read calendar and the write calendar."""
    out: list[Slot] = []
    for c in dict.fromkeys([*cal.read_calendars, cal.write_calendar]):
        out.extend(cal.free_busy(c, start, end))
    return sorted(out, key=lambda s: s.start)
