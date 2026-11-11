"""Travel buffers: opt-in per group, off by default.

When a group has ``travel_buffers`` on, the calendar adapter reads two fields of the
owner's events, locally: the location and the conferencing link. Each event becomes
a derived fact and nothing more: in person with so many travel minutes from the
owner's base address, or virtual. Titles, attendees and descriptions are never read
into these facts, never reach the model and are never said to anyone. The location
text itself stays inside the adapter; only the minutes leave it.

In-person events then get their travel time blocked before and after when offers
are built. Travel minutes come from a distance table (fixture mode) or a default
(live mode, until a local table is set); no maps service is called.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from .ontology import Slot

DEFAULT_TRAVEL_MIN = 30
_VIRTUAL_HINTS = ("zoom.us", "meet.google", "teams.microsoft", "webex", "whereby", "video call", "virtual", "online", "phone")


@dataclass(frozen=True)
class PlaceFact:
    """One of the owner's events reduced to what travel needs. No title, no attendees, no location text."""
    slot: Slot
    kind: str               # in_person | virtual
    travel_min: int = 0


def derive(slot: Slot, location: Optional[str], conference: Optional[str], base: str,
           table: Optional[dict[str, int]] = None, default_min: int = DEFAULT_TRAVEL_MIN) -> Optional[PlaceFact]:
    """Turn an event's location and conferencing fields into a fact; None when neither says anything."""
    loc = (location or "").strip()
    if conference or (loc and any(h in loc.lower() for h in _VIRTUAL_HINTS)):
        return PlaceFact(slot, "virtual", 0)
    if not loc:
        return None
    if base and loc.lower() == base.lower():
        return PlaceFact(slot, "in_person", 0)
    minutes = (table or {}).get(loc)
    return PlaceFact(slot, "in_person", int(minutes if minutes is not None else default_min))


def travel_blocks(facts: list[PlaceFact]) -> list[Slot]:
    """Busy time for the trip there and back around each in-person event."""
    out = []
    for f in facts:
        if f.kind == "in_person" and f.travel_min > 0:
            pad = timedelta(minutes=f.travel_min)
            out.append(Slot((f.slot.start_dt() - pad).isoformat(), f.slot.start))
            out.append(Slot(f.slot.end, (f.slot.end_dt() + pad).isoformat()))
    return out
