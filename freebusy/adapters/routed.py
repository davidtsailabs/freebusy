"""Read many, hold on one.

``RoutedCalendar`` is the CalendarAdapter for connector mode. It reads busy
times from every calendar the owner picked, across any number of CalDAV
accounts, and writes to exactly one target: a writable calendar the owner
picked, or the local hold store when none of theirs is writable (or they
prefer it).

Calendar references are ``<connection id>|<calendar url>``; ``local`` is the
local hold store. The local store keeps its events in one JSON file and writes
them out as an iCalendar feed file the owner can subscribe to in any calendar
app. Holds are TENTATIVE and OPAQUE, so they block the slot for anyone else who
reads the calendar or the feed; freebusy's own reads leave them out (by marker and
UID, or by subtracting the ledger's held intervals). Booked events are CONFIRMED.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..ontology import Slot
from . import ics
from .base import WrongCalendar
from .caldav import CalDAVClient

LOCAL = "local"


def ref(connection_id: str, url: str) -> str:
    return f"{connection_id}|{url}"


def split_ref(r: str) -> tuple[str, str]:
    conn, _, url = r.partition("|")
    return conn, url


class LocalHoldStore:
    """Holds and booked events kept on this machine, exported as ``feed/freebusy.ics``."""

    def __init__(self, data_dir: Path, name: str = "freebusy holds"):
        self.path = Path(data_dir) / "local-calendar.json"
        self.feed = Path(data_dir) / "feed" / "freebusy.ics"
        self.name = name
        self.events: list[dict] = json.loads(self.path.read_text()).get("events", []) if self.path.exists() else []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"events": self.events}, indent=1))
        self.feed.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.feed.with_suffix(".tmp")
        tmp.write_text(ics.calendar([ics.vevent(e["id"], Slot(e["start"], e["end"]), e["summary"], e.get("description", ""),
                                                e.get("url", ""), e.get("tentative", False),
                                                alarms_min=e.get("alarms", []))
                                     for e in self.events], self.name))
        os.replace(tmp, self.feed)

    def add(self, slot: Slot, summary: str, description: str = "", url: str = "", tentative: bool = False,
            alarms: Optional[list[int]] = None) -> str:
        eid = f"{ics.HOLD_UID_PREFIX if tentative else 'freebusy-'}{uuid.uuid4().hex}"
        self.events.append({"id": eid, "start": slot.start, "end": slot.end, "summary": summary, "description": description,
                            "url": url, "tentative": tentative, "alarms": list(alarms or [])})
        self._save()
        return eid

    def remove(self, event_id: str) -> None:
        self.events = [e for e in self.events if e["id"] != event_id]
        self._save()

    def busy(self, start: datetime, end: datetime) -> list[Slot]:
        win = Slot(start.isoformat(), end.isoformat())
        # Freebusy's own read: its holds are left out here; the feed shows them as busy to everyone else.
        return [Slot(e["start"], e["end"]) for e in self.events if not e.get("tentative") and Slot(e["start"], e["end"]).overlaps(win)]


class RoutedCalendar:
    def __init__(self, clients: dict[str, CalDAVClient], read_calendars: list[str], write_calendar: str, zone: str,
                 store: Optional[LocalHoldStore] = None, meeting_link: str = "", organizer: str = ""):
        self.clients = clients
        self.write_calendar = write_calendar
        self.read_calendars = [r for r in read_calendars if r != write_calendar]
        if write_calendar == LOCAL and store is None:
            raise ValueError("holds go to the local store, but no store was given")
        self.zone, self.store = zone, store
        self.meeting_link, self.organizer = meeting_link, organizer
        self.created: set[str] = set()
        self.own_holds = lambda: []     # held intervals from the ledger; set by the plugin (see __init__._build_runtime)

    def _client(self, r: str) -> tuple[CalDAVClient, str]:
        conn, url = split_ref(r)
        if conn not in self.clients or not url:
            raise KeyError(f"no connection for calendar {r}")
        return self.clients[conn], url

    def free_busy(self, calendar: str, start: datetime, end: datetime) -> list[Slot]:
        if calendar == LOCAL:
            return self.store.busy(start, end) if self.store else []
        client, url = self._client(calendar)
        if calendar == self.write_calendar:
            return client.busy(url, start, end, self.zone, own_holds=list(self.own_holds()))
        return client.busy(url, start, end, self.zone)

    def create_event(self, calendar: str, slot: Slot, title: str, attendees: list[str],
                     description: str = "", tentative: bool = False, reminders: Optional[list[dict]] = None) -> str:
        if calendar != self.write_calendar:
            raise WrongCalendar(f"writes go to {self.write_calendar} only")
        link = "" if tentative else self.meeting_link
        desc = "\n".join(x for x in (description, f"Join: {link}" if link else "") if x)
        alarms = [int(r["minutes"]) for r in (reminders or []) if r.get("minutes")]
        attendees = [a for a in attendees if not a.lower().endswith("@chat.invalid")]   # chat contacts without email: no invite
        if calendar == LOCAL:
            eid = self.store.add(slot, title, desc, link, tentative, alarms)
        else:
            client, url = self._client(calendar)
            uid = f"{ics.HOLD_UID_PREFIX if tentative else 'freebusy-'}{uuid.uuid4().hex}"
            body = ics.calendar([ics.vevent(uid, slot, title, desc, link, tentative, organizer=self.organizer,
                                            attendees=attendees, alarms_min=alarms)])
            eid = client.put_event(url, uid, body)
        self.created.add(eid)
        return eid

    def delete_event(self, calendar: str, event_id: str) -> None:
        if calendar != self.write_calendar:
            raise WrongCalendar(f"writes go to {self.write_calendar} only")
        if calendar == LOCAL:
            self.store.remove(event_id)
            return
        client, url = self._client(calendar)
        if not (event_id.startswith(url) and event_id.rsplit("/", 1)[-1].startswith("freebusy-")):
            raise WrongCalendar("only events this plugin created on the hold calendar can be deleted")
        client.delete(event_id)

    def join_link(self, event_id: str) -> Optional[str]:
        return self.meeting_link or None if event_id else None


class CalendarMirror:
    """Mirrors ledger holds as tentative events on the hold target, so the owner sees them in their calendar."""

    def __init__(self, cal, on_error=None):
        self.cal = cal
        self.on_error = on_error or (lambda what, exc: None)

    def on_add(self, hold) -> Optional[str]:
        try:
            return self.cal.create_event(self.cal.write_calendar, hold.slot, "Hold: offered, not yet booked", [],
                                         description=f"Held by freebusy for a scheduling thread. Expires {hold.expires}.",
                                         tentative=True)
        except Exception as exc:
            self.on_error("hold mirror add", exc)
            return None

    def on_remove(self, hold) -> None:
        if not hold.event_id:
            return
        try:
            self.cal.delete_event(self.cal.write_calendar, hold.event_id)
        except Exception as exc:
            self.on_error("hold mirror remove", exc)
