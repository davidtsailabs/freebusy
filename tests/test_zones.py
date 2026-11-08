from datetime import datetime
from zoneinfo import ZoneInfo

from freebusy.zones import (detect_zone, format_day, format_range, format_slot_for, parse_pick_and_zone,
                            zone_hint_from_header, zone_line)

PT = ZoneInfo("America/Los_Angeles")


def test_zurich_mail_with_no_zone_yields_nothing():
    assert detect_zone("Thanks Dana. Can we do Tuesday 2:30? Happy to move if that does not suit.") is None


def test_header_offset_is_only_a_hint():
    assert zone_hint_from_header("Mon, 09 Nov 2026 17:42:00 +0100") == "Central European time"
    assert zone_hint_from_header("Mon, 09 Nov 2026 12:02:00 -0500") == "Eastern or Central time"
    assert zone_hint_from_header("garbage") is None


def test_b_cet_reply_is_pick_plus_stated_zone():
    pick, zone = parse_pick_and_zone("B, CET")
    assert pick == "B"
    assert zone.zone == "Europe/Berlin" and zone.label == "CET" and zone.source == "stated"


def test_pick_variants():
    assert parse_pick_and_zone("Option A works")[0] == "A"
    assert parse_pick_and_zone("the second one please")[0] == "B"
    assert parse_pick_and_zone("A, I'm in Chicago")[1].zone == "America/Chicago"


def test_my_time_without_zone_is_not_a_zone():
    assert detect_zone("Tuesday at 2:30 my time") is None


def test_city_in_body_is_stated_but_signature_city_is_inferred():
    assert detect_zone("I'm in Seattle this month.").source == "stated"
    g = detect_zone("See you soon.", signature="Lukas Brenner\nZurich")
    assert g.source == "inferred" and g.confidence < 0.6


def test_utc_offset_in_text():
    g = detect_zone("I am on UTC+5:30 these days")
    assert g.source == "stated" and g.zone == "UTC+05:30"


def test_format_range_styles():
    s, e = datetime(2026, 11, 12, 10, 0, tzinfo=PT), datetime(2026, 11, 12, 11, 30, tzinfo=PT)
    assert format_range(s, e, "America/Los_Angeles") == "10:00 to 11:30 am PT"
    assert format_range(s, datetime(2026, 11, 12, 13, 0, tzinfo=PT), "America/Los_Angeles") == "10:00 am to 1:00 pm PT"
    assert format_day(s, "America/Los_Angeles") == "Thu Nov 12"


def test_second_zone_only_when_stated():
    s, e = datetime(2026, 11, 12, 10, 0, tzinfo=PT), datetime(2026, 11, 12, 10, 30, tzinfo=PT)
    assert "/" not in format_slot_for(s, e, "America/Los_Angeles", None)
    assert format_slot_for(s, e, "America/Los_Angeles", "Europe/Zurich").endswith("7:00 to 7:30 pm CET")


def test_zone_line_states_hint_as_assumption():
    line = zone_line("America/Los_Angeles", None, "Central European time")
    assert "you look to be on Central European time" in line and "Pacific" in line
    assert zone_line("America/Los_Angeles", "Europe/Zurich", None) == "Times in Pacific and CET."
