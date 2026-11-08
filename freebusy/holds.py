"""The holds ledger, universal layer.

A slot is held the moment it is offered and never offered twice. Holds expire
after 48 hours, clear on booking, and open offers are spread across days so
two people are not competing for the same afternoon. At most three days per
offer. The ledger is one JSON file; the adapter may also mirror holds as
tentative events on the write calendar (free/busy then excludes them for free).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from .ontology import Hold, Slot, from_dict, now_iso, to_dict

MAX_DAYS_PER_OFFER = 3


class HoldLedger:
    def __init__(self, path: Optional[Path] = None, expiry_hours: int = 48, max_open: int = 12):
        self.path = Path(path) if path else None
        self.expiry_hours = expiry_hours
        self.max_open = max_open
        self.holds: list[Hold] = []
        if self.path and self.path.exists():
            self.holds = [from_dict(Hold, h) for h in json.loads(self.path.read_text()).get("holds", [])]

    # -- persistence ---------------------------------------------------------
    def save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"holds": to_dict(self.holds), "saved": now_iso()}, indent=1))
            tmp.replace(self.path)

    # -- queries -------------------------------------------------------------
    def expire(self, now: Optional[datetime] = None) -> list[Hold]:
        now = now or datetime.now(timezone.utc)
        gone = [h for h in self.holds if datetime.fromisoformat(h.expires) <= now]
        if gone:
            self.holds = [h for h in self.holds if h not in gone]
            self.save()
        return gone

    def open(self) -> list[Hold]:
        return list(self.holds)

    def for_thread(self, thread_id: str) -> list[Hold]:
        return [h for h in self.holds if h.thread_id == thread_id]

    def is_free(self, slot: Slot, thread_id: Optional[str] = None) -> bool:
        """True when no other thread holds a slot overlapping this one."""
        for h in self.holds:
            if h.thread_id != thread_id and h.slot.overlaps(slot):
                return False
        return True

    def busy_days(self, exclude_thread: Optional[str] = None) -> set[str]:
        """Dates (YYYY-MM-DD, in the slot's own offset) with holds from other threads."""
        return {h.slot.start[:10] for h in self.holds if h.thread_id != exclude_thread}

    def filter_free(self, slots: Iterable[Slot], thread_id: Optional[str] = None) -> list[Slot]:
        return [s for s in slots if self.is_free(s, thread_id)]

    # -- changes -------------------------------------------------------------
    def add(self, slot: Slot, thread_id: str, person_id: str, now: Optional[datetime] = None,
            event_id: Optional[str] = None) -> Hold:
        if not self.is_free(slot, thread_id):
            raise ValueError("slot is held for another thread")
        if len(self.holds) >= self.max_open:
            raise ValueError(f"hold budget reached ({self.max_open} open holds)")
        now = now or datetime.now(timezone.utc)
        for h in self.for_thread(thread_id):
            if h.slot.key() == slot.key():
                return h
        hold = Hold(slot=slot, thread_id=thread_id, person_id=person_id,
                    expires=(now + timedelta(hours=self.expiry_hours)).isoformat(),
                    created=now.isoformat(), event_id=event_id)
        self.holds.append(hold)
        self.save()
        return hold

    def hold_offer(self, slots: list[Slot], thread_id: str, person_id: str,
                   now: Optional[datetime] = None) -> list[Hold]:
        """Hold a whole offer. Fails as a unit if any slot is taken or the budget is out."""
        for s in slots:
            if not self.is_free(s, thread_id):
                raise ValueError("slot is held for another thread")
        days = {s.start[:10] for s in slots}
        if len(days) > MAX_DAYS_PER_OFFER:
            raise ValueError(f"an offer spans at most {MAX_DAYS_PER_OFFER} days")
        return [self.add(s, thread_id, person_id, now) for s in slots]

    def release(self, thread_id: str, keep: Optional[Slot] = None) -> list[Hold]:
        """Clear a thread's holds, optionally keeping the one that was booked."""
        gone = [h for h in self.holds if h.thread_id == thread_id and (keep is None or h.slot.key() != keep.key())]
        self.holds = [h for h in self.holds if h not in gone]
        self.save()
        return gone

    def book(self, thread_id: str, slot: Slot) -> list[Hold]:
        """On booking every hold for the thread goes, the booked one included; the event replaces it."""
        return self.release(thread_id)

    def find(self, thread_id: str, slot: Slot) -> Optional[Hold]:
        for h in self.for_thread(thread_id):
            if h.slot.key() == slot.key():
                return h
        return None


def spread_across_days(slots: list[Slot], avoid_days: set[str], max_days: int = MAX_DAYS_PER_OFFER) -> list[Slot]:
    """Pick slots over at most ``max_days`` distinct days, preferring days with no other open hold.

    All free time within a chosen day is kept; a fourth day is never added.
    """
    by_day: dict[str, list[Slot]] = {}
    for s in sorted(slots, key=lambda x: x.start):
        by_day.setdefault(s.start[:10], []).append(s)
    ordered = sorted(by_day, key=lambda d: (d in avoid_days, d))
    chosen = ordered[:max_days]
    return [s for d in sorted(chosen) for s in by_day[d]]
