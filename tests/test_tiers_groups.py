"""Tiers renamed with Trial added, and groups (David, 2026-11-10)."""

from datetime import timedelta

import pytest

from freebusy.adapters import fixture as fx
from freebusy.engine import Scheduler
from freebusy.groups import groups_from_config, groups_to_config, parse_windows, set_setting
from freebusy.holds import HoldLedger
from freebusy.ontology import TIER_NAMES, AuthorityTier, OwnerProfile, Person, Slot, ThreadStatus, from_json, to_json
from freebusy.policy import Gate


def _world(tmp_path, profile=None):
    p = profile or fx.new_user_profile()
    led, cal, mail = HoldLedger(tmp_path / "h.json"), fx.FixtureCalendar(), fx.FixtureMail()
    gate = Gate(p, led, tmp_path / "a.jsonl")
    return Scheduler(p, cal, mail, led, gate, fx.people()), cal, mail, led, gate


def test_tier_names():
    assert [TIER_NAMES[i] for i in range(4)] == ["Trial", "Offer", "Book", "Manage"]
    assert AuthorityTier.TRIAL == 0 and AuthorityTier.OBSERVE is AuthorityTier.TRIAL


def test_group_tier_caps_introduced_senders():
    p = fx.new_user_profile()
    assert p.tier_for("introduced", group=p.groups["work"]) == AuthorityTier.TRIAL
    p.groups["work"].tier = AuthorityTier.BOOK
    assert p.tier_for("introduced", group=p.groups["work"]) == AuthorityTier.BOOK
    p.auto_book = False
    assert p.tier_for("introduced", group=p.groups["work"]) == AuthorityTier.OFFER


def test_trial_offers_on_day_one_and_the_booking_waits(tmp_path):
    s, cal, mail, led, gate = _world(tmp_path)
    a = s.handle(mail.thread("fresh-intro"), fx.NOW)
    assert a.kind == "reply" and a.slots and led.for_thread("t13")
    assert gate.facts("t13").tier == AuthorityTier.TRIAL
    pick = fx._mail("t13", 2, fx.NORA, [fx.AGENT], [fx.OWNER], "Re", "B, CET\n\nNora", "Tue, 10 Nov 2026 09:05:00 +0100", "Nora Lind")
    b = s.handle(mail.thread("fresh-intro") + [pick], fx.NOW + timedelta(days=1))
    assert b.kind == "notify" and s.pending["t13"].reason == "approve_first" and cal.events == []
    c = s.resume("t13", "approve", fx.NOW + timedelta(days=1))
    assert c.kind == "reply+notify" and len(cal.events) == 1


def test_trial_their_invite_also_waits(tmp_path):
    s, cal, mail, led, gate = _world(tmp_path)
    s.handle(mail.thread("their-assistant"), fx.NOW)
    pick = fx._mail("t3", 3, fx.THEO, [fx.AGENT], [fx.OWNER, fx.PRIYA], "Re", "A works, thanks. Invite coming from us.\n\nTheo",
                    "Tue, 10 Nov 2026 09:00:00 -0500", "Theo Marsh")
    b = s.handle(mail.thread("their-assistant") + [pick], fx.NOW + timedelta(days=1))
    assert b.kind == "notify" and s.pending["t3"].reason == "approve_first"
    c = s.resume("t3", "yes", fx.NOW + timedelta(days=1))
    assert c.kind == "reply" and s.state("t3").status == ThreadStatus.AWAITING_INVITE.value


def test_unknown_sender_never_gets_a_reply_in_trial(tmp_path):
    s, cal, mail, led, gate = _world(tmp_path)
    cold = fx._mail("t21", 1, fx.OWEN, [fx.AGENT], [], "Hello", "Hi, could we find some time next week?", "Mon, 09 Nov 2026 12:40:00 -0500")
    n = s.handle([cold], fx.NOW)
    assert n.kind == "notify" and mail.sent == [] and s.pending["t21"].reason == "unknown_sender"
    v = gate.pre_tool_call("schedule_send", {"thread_id": "t21", "to": [fx.OWEN], "body": "hi", "slots": []})
    assert v["action"] == "block" and "nobody introduced" in v["message"]


def test_trial_send_without_held_times_is_blocked(tmp_path):
    s, cal, mail, led, gate = _world(tmp_path)
    s.handle(mail.thread("fresh-intro"), fx.NOW)
    v = gate.pre_tool_call("schedule_send", {"thread_id": "t13", "to": [fx.NORA], "body": "Happy to help", "slots": []})
    assert v["action"] == "block" and "Trial" in v["message"]


def test_family_sees_full_availability_and_work_sees_nine_to_five(tmp_path):
    s, cal, mail, led, gate = _world(tmp_path)
    fam = s.handle(mail.thread("family-ask"), fx.NOW)
    work = s.handle(mail.thread("fresh-intro"), fx.NOW)
    assert s.profile.group_for(s.person_for(fx.AMARA)).name == "Family"
    assert s.profile.group_for(s.person_for(fx.NORA)).name == "Work"
    fam_days = {x.start_dt().weekday() for x in fam.slots}
    assert any(x.start_dt().hour < 9 or x.end_dt().hour >= 17 or x.start_dt().weekday() >= 5 for x in fam.slots), fam_days
    assert all(9 <= x.start_dt().hour and x.end_dt().hour <= 17 and x.start_dt().weekday() < 5 for x in work.slots)


def test_group_membership_by_address_and_domain():
    p = fx.profile()
    assert p.group_for(Person("x", "X", emails=["cousin@whitfield.example"])).id == "family"
    assert p.group_for(Person("y", "Y", emails=["someone@else.example"])).id == "default"


def test_groups_round_trip_through_config_and_json():
    g = groups_from_config({})
    assert set(g) == {"work", "family"} and g["family"].visibility == "full"
    assert groups_from_config(groups_to_config(g)) == g
    p = fx.profile()
    assert from_json(OwnerProfile, to_json(p)) == p


def test_group_setting_edits():
    g = groups_from_config({})
    assert "mon-thu 10:00-16:00" in set_setting(g, "Work", "windows", "mon-thu 10am-4pm")
    assert set_setting(g, "work", "tier", "book").endswith("2 Book")
    assert set_setting(g, "family", "travel", "on").endswith("on")
    assert "whitfield.example" in set_setting(g, "family", "domains", "add @whitfield.example")
    set_setting(g, "Board", "length", "45")
    assert g["board"].default_len_min == 45 and g["board"].windows == g["work"].windows or g["board"].windows
    with pytest.raises(ValueError):
        set_setting(g, "work", "colour", "blue")
    with pytest.raises(ValueError):
        parse_windows("whenever")
