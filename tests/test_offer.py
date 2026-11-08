from datetime import timedelta
from zoneinfo import ZoneInfo

from freebusy.adapters import fixture as fx
from freebusy.adapters.base import all_busy
from freebusy.holds import HoldLedger
from freebusy.offer import build_offer, check_proposed
from freebusy.ontology import Slot

PT = ZoneInfo("America/Los_Angeles")


def _busy(cal):
    return all_busy(cal, fx.NOW - timedelta(days=1), fx.NOW + timedelta(days=21))


def test_offer_respects_earliest_horizon_windows_and_three_days():
    p, cal = fx.profile(), fx.FixtureCalendar()
    r = build_offer(p, p.groups["default"], _busy(cal), HoldLedger(), "t1", fx.NOW)
    assert r.slots and r.length_min == 30 and not r.needs_owner
    days = {s.start[:10] for s in r.slots}
    assert len(days) <= 3
    for s in r.slots:
        st = s.start_dt().astimezone(PT)
        assert st.date() >= (fx.NOW + timedelta(days=3)).date()      # earliest day out is a day rule
        assert st >= fx.NOW + timedelta(hours=24)                      # and never inside the notice period
        assert st <= fx.NOW + timedelta(days=14)
        assert st.weekday() < 5 and st.weekday() != 1          # no weekends, no Tuesdays
        assert not (12 <= st.hour < 13)                        # never inside lunch
        for b in _busy(cal):                                   # buffers: 30 minutes clear of every busy block
            assert s.end_dt() + timedelta(minutes=30) <= b.start_dt() or s.start_dt() >= b.end_dt() + timedelta(minutes=30)


def test_sixty_minutes_is_flagged_to_owner_and_thirty_offered():
    p, cal = fx.profile(), fx.FixtureCalendar()
    r = build_offer(p, p.groups["default"], _busy(cal), HoldLedger(), "t1", fx.NOW, length_min=60)
    assert r.needs_owner and r.length_min == 30


def test_stated_zone_clips_to_their_workday():
    p, cal = fx.profile(), fx.FixtureCalendar()
    r = build_offer(p, p.groups["default"], _busy(cal), HoldLedger(), "t1", fx.NOW, person_zone="America/New_York")
    assert r.slots
    for s in r.slots:
        et = s.start_dt().astimezone(ZoneInfo("America/New_York"))
        assert 9 <= et.hour and s.end_dt().astimezone(ZoneInfo("America/New_York")).hour <= 17


def test_offer_skips_slots_held_for_another_thread():
    p, cal, led = fx.profile(), fx.FixtureCalendar(), HoldLedger()
    first = build_offer(p, p.groups["default"], _busy(cal), led, "t1", fx.NOW)
    led.hold_offer(first.slots, "t1", "lukas", fx.NOW)
    second = build_offer(p, p.groups["default"], _busy(cal), led, "t2", fx.NOW)
    held = {h.slot.key() for h in led.open()}
    assert second.slots and not any(s.key() in held for s in second.slots)
    assert not any(s.overlaps(h.slot) for s in second.slots for h in led.open())


def test_check_proposed_first_clean_match():
    p, cal = fx.profile(), fx.FixtureCalendar()
    busy = _busy(cal)
    inside = Slot(fx.NOW.replace(day=12, hour=14, minute=0).isoformat(), fx.NOW.replace(day=12, hour=14, minute=30).isoformat())
    lunch = Slot(fx.NOW.replace(day=12, hour=12, minute=0).isoformat(), fx.NOW.replace(day=12, hour=12, minute=30).isoformat())
    soon = Slot(fx.NOW.replace(hour=11).isoformat(), fx.NOW.replace(hour=11, minute=30).isoformat())
    checks = check_proposed(p, p.groups["default"], busy, HoldLedger(), "t1", [soon, lunch, inside], fx.NOW)
    assert [c.ok for c in checks] == [False, False, True]
    assert "notice" in checks[0].reason
