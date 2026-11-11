"""Travel buffers: opt-in per group, off by default; only derived facts leave the adapter."""

from dataclasses import fields
from datetime import timedelta

from freebusy.adapters import fixture as fx
from freebusy.adapters.google_workspace import reduce_to_places
from freebusy.engine import Scheduler
from freebusy.holds import HoldLedger
from freebusy.ontology import Slot
from freebusy.travel import PlaceFact, derive, travel_blocks


def _offer(travel: bool):
    p = fx.profile()
    p.groups["default"].travel_buffers = travel
    s = Scheduler(p, fx.FixtureCalendar(), fx.FixtureMail(), HoldLedger(), None, fx.people())
    return s.handle(s.mail.thread("blocked-est-send"), fx.NOW)


def test_off_by_default():
    assert fx.profile().groups["default"].travel_buffers is False
    assert all(not g.travel_buffers for g in fx.new_user_profile().groups.values())


def test_in_person_event_pushes_the_offer_later():
    off, on = _offer(False), _offer(True)
    thu_off = [x for x in off.slots if x.start.startswith("2026-11-12")]
    thu_on = [x for x in on.slots if x.start.startswith("2026-11-12")]
    assert thu_off[0].start_dt().hour == 8              # 8:00 am, before the 9:00 in-person meeting in Emeryville
    assert thu_on[0].start_dt().hour == 13               # 40 minutes each way plus the buffer leave only the afternoon
    assert "Emeryville" not in on.text and "Harbor" not in on.text


def test_facts_carry_no_title_attendees_or_location_text():
    assert {f.name for f in fields(PlaceFact)} == {"slot", "kind", "travel_min"}
    cal = fx.FixtureCalendar()
    facts = cal.places(fx.WORK, fx.NOW, fx.NOW + timedelta(days=14), fx.BASE)
    kinds = {(f.slot.start[:10], f.kind, f.travel_min) for f in facts}
    assert ("2026-11-12", "in_person", 40) in kinds and ("2026-11-09", "virtual", 0) in kinds


def test_derive_and_blocks():
    s = Slot("2026-11-12T09:00:00-08:00", "2026-11-12T10:00:00-08:00")
    assert derive(s, None, "https://zoom.us/j/1", "home").kind == "virtual"
    assert derive(s, "Somewhere new", None, "home").travel_min == 30            # no table entry: the default
    assert derive(s, None, None, "home") is None
    blocks = travel_blocks([derive(s, "A", None, "home", {"A": 15})])
    assert blocks[0].start.endswith("08:45:00-08:00") and blocks[1].end.endswith("10:15:00-08:00")


def test_google_reduction_reads_only_location_and_conferencing():
    events = [{"start": {"dateTime": "2026-11-12T09:00:00-08:00"}, "end": {"dateTime": "2026-11-12T10:00:00-08:00"},
               "summary": "Board dinner with Acme", "attendees": [{"email": "ceo@acme.example"}], "description": "secret",
               "location": "Acme HQ"},
              {"start": {"dateTime": "2026-11-12T11:00:00-08:00"}, "end": {"dateTime": "2026-11-12T11:30:00-08:00"},
               "summary": "1:1", "hangoutLink": "https://meet.google.com/x"}]
    facts = reduce_to_places(events, "home", 25)
    assert [(f.kind, f.travel_min) for f in facts] == [("in_person", 25), ("virtual", 0)]
    assert "Acme" not in repr(facts) and "secret" not in repr(facts)
