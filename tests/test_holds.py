from datetime import datetime, timedelta, timezone

import pytest

from freebusy.holds import HoldLedger, spread_across_days
from freebusy.ontology import Slot

NOW = datetime(2026, 11, 9, 17, 0, tzinfo=timezone.utc)


def slot(day: int, h: int, length=30):
    s = datetime(2026, 11, day, h, 0, tzinfo=timezone.utc)
    return Slot(s.isoformat(), (s + timedelta(minutes=length)).isoformat())


def test_hold_expires_after_48_hours(tmp_path):
    led = HoldLedger(tmp_path / "h.json")
    led.add(slot(12, 18), "t1", "p1", now=NOW)
    assert led.expire(NOW + timedelta(hours=47)) == []
    gone = led.expire(NOW + timedelta(hours=48))
    assert len(gone) == 1 and led.open() == []


def test_never_offer_a_slot_held_for_another_thread(tmp_path):
    led = HoldLedger(tmp_path / "h.json")
    led.add(slot(12, 18), "t1", "p1", now=NOW)
    assert not led.is_free(slot(12, 18), "t2")
    assert led.is_free(slot(12, 18), "t1")         # the same thread may re-offer its own hold
    with pytest.raises(ValueError):
        led.add(slot(12, 18), "t2", "p2", now=NOW)
    overlapping = Slot(slot(12, 18).start, slot(12, 18, 90).end)
    assert led.filter_free([overlapping, slot(13, 18)], "t2") == [slot(13, 18)]


def test_booking_clears_every_hold_for_the_thread(tmp_path):
    led = HoldLedger(tmp_path / "h.json")
    led.hold_offer([slot(12, 18), slot(13, 18), slot(16, 18)], "t1", "p1", now=NOW)
    led.add(slot(18, 18), "t2", "p2", now=NOW)
    led.book("t1", slot(13, 18))
    assert [h.thread_id for h in led.open()] == ["t2"]


def test_offer_spans_at_most_three_days(tmp_path):
    led = HoldLedger(tmp_path / "h.json")
    with pytest.raises(ValueError):
        led.hold_offer([slot(12, 18), slot(13, 18), slot(16, 18), slot(17, 18)], "t1", "p1", now=NOW)


def test_spread_prefers_days_without_other_holds():
    slots = [slot(12, 18), slot(13, 18), slot(16, 18), slot(17, 18)]
    chosen = spread_across_days(slots, avoid_days={"2026-11-12"})
    assert {s.start[:10] for s in chosen} == {"2026-11-13", "2026-11-16", "2026-11-17"}


def test_ledger_persists(tmp_path):
    p = tmp_path / "h.json"
    HoldLedger(p).add(slot(12, 18), "t1", "p1", now=NOW)
    assert len(HoldLedger(p).open()) == 1


def test_hold_budget(tmp_path):
    led = HoldLedger(tmp_path / "h.json", max_open=2)
    led.add(slot(12, 18), "t1", "p1", now=NOW)
    led.add(slot(13, 18), "t1", "p1", now=NOW)
    with pytest.raises(ValueError):
        led.add(slot(16, 18), "t2", "p2", now=NOW)
