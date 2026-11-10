"""Demo data. No real calendars, no real people.

Owner: Dana Whitfield, Pacific, three calendars (work, personal, family)
read as free/busy, writes to work. Six counterparts, some with a stated zone and some without. Two
weeks of busy blocks from a fixed "now" so every run is the same. Seven mail
threads matching the demo scenes, plus two loop-guard threads.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from ..ontology import AuthorityTier, Group, OwnerProfile, Person, Slot, Window
from ..parse import AGENT_HEADER, Email
from .base import WrongCalendar

PT = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 11, 9, 9, 0, tzinfo=PT)      # Monday morning

OWNER = "dana@example.com"
AGENT = "dana+agent@example.com"
WORK, PERSONAL, FAMILY = "work@example.com", "dana.personal@example.com", "family@example.com"

PEOPLE = {
    "lukas": Person(id="lukas", name="Lukas Brenner", emails=["lukas.brenner@alpineventures.example"],
                    source="stated", stated_at=NOW.isoformat()),                       # Zurich, zone never stated at first
    "priya": Person(id="priya", name="Priya Natarajan", emails=["priya@harborbio.example"],
                    zone="America/New_York", zone_source="stated", stated_at=NOW.isoformat()),
    "theo": Person(id="theo", name="Theo Marsh", emails=["theo.marsh@harborbio.example"],
                   zone="America/New_York", zone_source="stated", stated_at=NOW.isoformat()),  # Priya's assistant
    "marisol": Person(id="marisol", name="Marisol Vega", emails=["marisol@cascadehealth.example"],
                      zone="America/Los_Angeles", zone_source="stated", stated_at=NOW.isoformat()),
    "owen": Person(id="owen", name="Owen Hale", emails=["owen@brightpathcro.example"], stated_at=NOW.isoformat()),
    "nora": Person(id="nora", name="Nora Lind", emails=["nora@fjordtx.example"], stated_at=NOW.isoformat()),  # zone never stated
}


def people() -> dict[str, Person]:
    """Fresh copies every time, so one run's stated zones never leak into the next."""
    return {k: copy.deepcopy(v) for k, v in PEOPLE.items()}


def profile() -> OwnerProfile:
    windows = {"mon": [Window("10:00", "15:00")], "wed": [Window("09:30", "16:00")],
               "thu": [Window("08:00", "11:30"), Window("13:00", "16:30")], "fri": [Window("10:00", "14:00")]}
    g = Group(id="default", name="Introduced", windows=windows, buffer_min=30, default_len_min=30,
              earliest_days_out=3, horizon_days=14, tier=AuthorityTier.OFFER)
    return OwnerProfile(name="Dana Whitfield", home_zone="America/Los_Angeles", agent_address=AGENT,
                        owner_addresses=[OWNER], write_calendar=WORK, read_calendars=[PERSONAL, FAMILY],
                        groups={"default": g}, allowlist={"marisol@cascadehealth.example": AuthorityTier.BOOK},
                        auto_book=True)


def _slot(day_offset: int, h1: int, m1: int, h2: int, m2: int) -> Slot:
    d = (NOW + timedelta(days=day_offset)).replace(hour=0, minute=0)
    return Slot(d.replace(hour=h1, minute=m1).isoformat(), d.replace(hour=h2, minute=m2).isoformat())


def busy_blocks() -> dict[str, list[Slot]]:
    """Two weeks of realistic busy time. Titles do not exist here on purpose."""
    work, personal, family = [], [], []
    for d in range(0, 14):
        wd = (NOW + timedelta(days=d)).weekday()
        if wd >= 5:
            family.append(_slot(d, 10, 0, 12, 0))                 # weekend mornings
            continue
        work.append(_slot(d, 12, 0, 13, 0))                        # lunch, always closed
        if wd == 1:
            work.append(_slot(d, 8, 0, 12, 0))                     # Tuesday office mornings
        if wd == 0:
            work.append(_slot(d, 9, 0, 9, 45))                     # Monday team call
        if wd == 4:
            personal.append(_slot(d, 15, 0, 16, 0))
    work.append(_slot(3, 9, 0, 10, 0))          # Thu Nov 12
    work.append(_slot(9, 11, 0, 12, 0))         # Wed Nov 18
    work.append(_slot(10, 13, 0, 14, 0))        # Thu Nov 19
    personal.append(_slot(2, 15, 0, 16, 30))    # Wed Nov 11 afternoon
    family.append(_slot(7, 11, 0, 12, 0))       # Mon Nov 16
    return {WORK: work, PERSONAL: personal, FAMILY: family}


class FixtureCalendar:
    def __init__(self):
        self.write_calendar = WORK
        self.read_calendars = [PERSONAL, FAMILY]
        self._busy = busy_blocks()
        self.events: list[dict] = []
        self._n = 0

    def free_busy(self, calendar: str, start: datetime, end: datetime) -> list[Slot]:
        win = Slot(start.isoformat(), end.isoformat())
        blocks = list(self._busy.get(calendar, []))
        blocks += [Slot(e["start"], e["end"]) for e in self.events if e["calendar"] == calendar and not e.get("deleted")]
        return [b for b in blocks if b.overlaps(win)]

    def create_event(self, calendar: str, slot: Slot, title: str, attendees: list[str],
                     description: str = "", tentative: bool = False) -> str:
        if calendar != self.write_calendar:
            raise WrongCalendar(f"writes go to {self.write_calendar} only")
        self._n += 1
        eid = f"evt-{self._n}"
        self.events.append({"id": eid, "calendar": calendar, "start": slot.start, "end": slot.end, "title": title,
                            "attendees": list(attendees), "tentative": tentative})
        return eid

    def delete_event(self, calendar: str, event_id: str) -> None:
        if calendar != self.write_calendar:
            raise WrongCalendar(f"writes go to {self.write_calendar} only")
        for e in self.events:
            if e["id"] == event_id:
                e["deleted"] = True


def _mail(thread: str, n: int, sender: str, to: list[str], cc: list[str], subject: str, body: str,
          date: str, name: str = "", headers: Optional[dict] = None) -> Email:
    return Email(thread_id=thread, message_id=f"<{thread}-{n}@example>", sender=sender, to=to, cc=cc,
                 subject=subject, body=body, date=date, headers=headers or {}, sender_name=name)


LUKAS, PRIYA, THEO, MARISOL, OWEN, NORA = (PEOPLE[k].emails[0] for k in ("lukas", "priya", "theo", "marisol", "owen", "nora"))


def threads() -> dict[str, list[Email]]:
    """The scenes. Keys are the scene names used by the demo and the tests."""
    t: dict[str, list[Email]] = {}

    # 1. Zurich, no zone given. The owner introduced the agent; Lukas proposes a bare time.
    t["zurich-no-zone"] = [
        _mail("t1", 1, OWNER, [LUKAS], [AGENT], "Intro call", "Lukas, great to meet. My assistant (cc) will find us a time.\n\nDana",
              "Mon, 09 Nov 2026 08:10:00 -0800", "Dana Whitfield"),
        _mail("t1", 2, LUKAS, [AGENT], [OWNER], "Re: Intro call",
              "Hello,\n\nThanks Dana. Can we do Tuesday 2:30? Happy to move if that does not suit.\n\nBest\nLukas",
              "Mon, 09 Nov 2026 17:42:00 +0100", "Lukas Brenner"),
    ]
    # 2. The "B, CET" reply. Same thread, after the offer.
    t["b-cet"] = t["zurich-no-zone"] + [
        _mail("t1", 3, LUKAS, [AGENT], [OWNER], "Re: Intro call", "B, CET\n\nLukas", "Tue, 10 Nov 2026 09:05:00 +0100", "Lukas Brenner"),
    ]
    # 3. The other side's assistant sends the invite. Zone stated (ET).
    t["their-assistant"] = [
        _mail("t3", 1, OWNER, [PRIYA], [AGENT], "Harbor Bio x Dana", "Priya, copying my assistant to set up 30 minutes.\n\nDana",
              "Mon, 09 Nov 2026 08:30:00 -0800", "Dana Whitfield"),
        _mail("t3", 2, THEO, [AGENT], [OWNER, PRIYA], "Re: Harbor Bio x Dana",
              "Hi,\n\nI'm Theo, assistant to Priya. Could you suggest some times next week? We are on ET. I'll send the invite from our side once we agree.\n\nThanks\nTheo",
              "Mon, 09 Nov 2026 12:02:00 -0500", "Theo Marsh"),
    ]
    # 4. A hold conflict: Marisol (allowlisted, Seattle) asks while Lukas's holds are open.
    t["hold-conflict"] = [
        _mail("t4", 1, MARISOL, [AGENT], [], "Catch up", "Hi, I'm in Seattle this month. When are you free next week for a quick call?\n\nMarisol",
              "Mon, 09 Nov 2026 10:15:00 -0800", "Marisol Vega"),
    ]
    # 5. A blocked send: Owen never stated a zone; a reply showing ET must be stopped.
    t["blocked-est-send"] = [
        _mail("t5", 1, OWNER, [OWEN], [AGENT], "Vendor intro", "Owen, my assistant (cc) can find a time.\n\nDana",
              "Mon, 09 Nov 2026 09:00:00 -0800", "Dana Whitfield"),
        _mail("t5", 2, OWEN, [AGENT], [OWNER], "Re: Vendor intro", "Sure. Could we grab some time next week?\n\nOwen",
              "Mon, 09 Nov 2026 12:40:00 -0500", "Owen Hale"),
    ]
    # 6. Owner notification: a 60-minute request from an introduced sender goes to the owner first.
    t["owner-notification"] = [
        _mail("t6", 1, OWNER, [PRIYA], [AGENT], "Deep dive", "Priya, my assistant will coordinate.\n\nDana",
              "Mon, 09 Nov 2026 09:20:00 -0800", "Dana Whitfield"),
        _mail("t6", 2, PRIYA, [AGENT], [OWNER], "Re: Deep dive", "Could we get an hour next week? I'm on Eastern time.\n\nPriya",
              "Mon, 09 Nov 2026 13:00:00 -0500", "Priya Natarajan"),
    ]
    # 7. A fresh introduction: the owner cc's the agent and nobody has asked yet. The agent offers straight away.
    t["fresh-intro"] = [
        _mail("t13", 1, OWNER, [NORA], [AGENT], "Intro: Nora and Dana",
              "Nora, meet my assistant, cc'd, who will find us a time.\n\nDana", "Mon, 09 Nov 2026 08:45:00 -0800", "Dana Whitfield"),
    ]
    # Loop guards, for the tests: an auto-reply and our own mail.
    t["auto-reply"] = [
        _mail("t7", 1, LUKAS, [AGENT], [], "Automatic reply: Intro call", "I am out of the office.", "Mon, 09 Nov 2026 17:43:00 +0100",
              headers={"Auto-Submitted": "auto-replied"}),
    ]
    t["own-mail"] = [
        _mail("t8", 1, AGENT, [LUKAS], [OWNER], "Re: Intro call", "Hi Lukas, ...", "Mon, 09 Nov 2026 09:00:00 -0800",
              headers={AGENT_HEADER: "1"}),
    ]
    return t


class FixtureMail:
    def __init__(self):
        self._threads = threads()
        self.sent: list[dict] = []
        self._n = 0

    def list_threads(self) -> list[list[Email]]:
        return [v for k, v in self._threads.items() if k not in ("b-cet",)]

    def thread(self, scene: str) -> list[Email]:
        return list(self._threads[scene])

    def send(self, thread_id: str, to: list[str], cc: list[str], subject: str, text: str, html: Optional[str] = None) -> str:
        self._n += 1
        mid = f"<sent-{self._n}@example>"
        self.sent.append({"id": mid, "thread_id": thread_id, "to": to, "cc": cc, "subject": subject, "text": text, "html": html,
                          "headers": {AGENT_HEADER: "1"}})
        return mid
