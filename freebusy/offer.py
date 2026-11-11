"""Build offers from free/busy blocks and the profile's rules.

Inputs are busy blocks only (never titles), the group's windows and buffers,
the earliest-day and horizon rules, and the holds ledger. Output is a list of
free ranges over at most three days, each one a row in the reply. The default
length is 30 minutes; 60 minutes or more is flagged to the owner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from .holds import HoldLedger, spread_across_days
from .ontology import WEEKDAYS, Group, OwnerProfile, Slot
from .zones import to_zone

MAX_ROWS = 6              # letters A to F
PERSON_DAY = (time(9, 0), time(17, 0))   # the other side's workday when their zone is stated


@dataclass
class OfferResult:
    slots: list[Slot] = field(default_factory=list)
    needs_owner: list[str] = field(default_factory=list)   # reasons the owner must weigh in
    notes: list[str] = field(default_factory=list)         # internal, never sent
    length_min: int = 30


def _hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _subtract(ranges: list[tuple[datetime, datetime]], block: tuple[datetime, datetime]) -> list[tuple[datetime, datetime]]:
    out = []
    bs, be = block
    for s, e in ranges:
        if be <= s or bs >= e:
            out.append((s, e))
            continue
        if bs > s:
            out.append((s, bs))
        if be < e:
            out.append((be, e))
    return out


def free_ranges_for_day(day: datetime, group: Group, busy: list[Slot], buffer_min: int, zone: str,
                        person_zone: Optional[str] = None) -> list[tuple[datetime, datetime]]:
    """Free ranges inside the group's windows on one day, with buffers around every busy block."""
    tz = ZoneInfo(zone)
    weekday = WEEKDAYS[day.weekday()]
    ranges = [(datetime.combine(day.date(), _hhmm(w.start), tz), datetime.combine(day.date(), _hhmm(w.end), tz))
              for w in group.windows.get(weekday, [])]
    pad = timedelta(minutes=buffer_min)
    for b in busy:
        ranges = _subtract(ranges, (b.start_dt() - pad, b.end_dt() + pad))
    if person_zone:
        # Keep only the overlap with their 9 to 5; the two zones may put that on another calendar day.
        clipped = []
        for s, e in ranges:
            ps = to_zone(s, person_zone)
            lo = datetime.combine(ps.date(), PERSON_DAY[0], ps.tzinfo)
            hi = datetime.combine(ps.date(), PERSON_DAY[1], ps.tzinfo)
            s2, e2 = max(s, lo), min(e, hi)
            if s2 < e2:
                clipped.append((s2, e2))
        ranges = clipped
    return ranges


def build_offer(profile: OwnerProfile, group: Group, busy: list[Slot], ledger: HoldLedger, thread_id: str,
                now: datetime, length_min: Optional[int] = None, person_zone: Optional[str] = None,
                horizon_days: Optional[int] = None, earliest_days_out: Optional[int] = None,
                owner_approved_length: bool = False) -> OfferResult:
    """``owner_approved_length``: the owner's own word set this length, so 60 minutes or more is offered as asked."""
    result = OfferResult()
    length = length_min or group.default_len_min
    result.length_min = length
    if length > profile.max_len_without_owner_min and not owner_approved_length:
        result.needs_owner.append(f"{length}-minute meeting requested; the owner approves anything of 60 minutes or more")
        length = group.default_len_min
        result.notes.append(f"offering {length}-minute slots meanwhile")
        result.length_min = length
    horizon = horizon_days or group.horizon_days
    earliest = earliest_days_out if earliest_days_out is not None else group.earliest_days_out
    tz = ZoneInfo(profile.home_zone)
    now_local = now.astimezone(tz)
    first = (now_local + timedelta(days=earliest)).replace(hour=0, minute=0, second=0, microsecond=0)
    last = now_local + timedelta(days=horizon)
    notice = now + timedelta(hours=profile.notice_hours)
    candidates: list[Slot] = []
    day = first
    while day <= last:
        clip = person_zone if group.visibility != "full" else None     # "full": every free minute in the windows
        for s, e in free_ranges_for_day(day, group, busy, group.buffer_min, profile.home_zone, clip):
            if s < notice or (e - s) < timedelta(minutes=length):
                continue
            candidates.append(Slot(s.isoformat(), e.isoformat()))
        day += timedelta(days=1)
    candidates = ledger.filter_free(candidates, thread_id)
    if not candidates:
        result.notes.append("nothing free inside the rules and the horizon")
        return result
    chosen = spread_across_days(candidates, ledger.busy_days(exclude_thread=thread_id), profile.max_hold_days_per_offer)
    result.slots = chosen[:MAX_ROWS]
    return result


@dataclass
class Check:
    slot: Slot
    ok: bool
    reason: str = ""


def check_proposed(profile: OwnerProfile, group: Group, busy: list[Slot], ledger: HoldLedger, thread_id: str,
                   proposed: list[Slot], now: datetime) -> list[Check]:
    """Test the other side's times against the same rules. The caller takes the first clean match."""
    out: list[Check] = []
    notice = now + timedelta(hours=profile.notice_hours)
    for p in proposed:
        s, e = p.start_dt(), p.end_dt()
        if s < notice:
            out.append(Check(p, False, "inside the notice period"))
            continue
        day = s.astimezone(ZoneInfo(profile.home_zone))
        inside = any(fs <= s and e <= fe for fs, fe in
                     free_ranges_for_day(day, group, busy, group.buffer_min, profile.home_zone))
        if not inside:
            out.append(Check(p, False, "outside the owner's windows or spoken for"))
            continue
        if not ledger.is_free(p, thread_id):
            out.append(Check(p, False, "held for another conversation"))
            continue
        if (e - s) > timedelta(minutes=profile.max_len_without_owner_min):
            out.append(Check(p, False, "60 minutes or more needs the owner"))
            continue
        out.append(Check(p, True, "fits"))
    return out


def still_free(slot: Slot, busy: list[Slot]) -> bool:
    """The live re-check before booking: free on every calendar, no buffer applied to the chosen slot itself."""
    return not any(slot.overlaps(b) for b in busy)
