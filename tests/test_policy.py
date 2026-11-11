import json

from freebusy.adapters import fixture as fx
from freebusy.ontology import AuthorityTier, Slot
from freebusy.policy import redact


def _slot():
    return {"start": "2026-11-12T13:30:00-08:00", "end": "2026-11-12T14:00:00-08:00"}


def _ready(world, tier=AuthorityTier.BOOK, zone="Europe/Zurich"):
    g = world["gate"]
    f = g.facts("t1")
    f.participants = {fx.LUKAS, fx.OWNER, fx.AGENT}
    f.tier, f.person_zone, f.authority = tier, zone, "introduced"
    world["ledger"].add(Slot(**_slot()), "t1", "lukas", now=world["now"])
    return g


def test_send_blocked_for_unstated_zone(world):
    g = _ready(world, zone=None)
    v = g.pre_tool_call("schedule_send", {"thread_id": "t1", "to": [fx.LUKAS], "body": "Thu 1:30 pm PT / 4:30 pm ET", "slots": [_slot()]})
    assert v["action"] == "block" and "never stated" in v["message"]
    ok = g.pre_tool_call("schedule_send", {"thread_id": "t1", "to": [fx.LUKAS], "body": "Thu 1:30 pm PT", "slots": [_slot()]})
    assert ok is None


def test_send_blocked_for_stranger_recipient(world):
    g = _ready(world)
    v = g.pre_tool_call("schedule_send", {"thread_id": "t1", "to": [fx.LUKAS], "cc": ["someone.else@example.org"], "body": "hi", "slots": []})
    assert v["action"] == "block" and "not on the thread" in v["message"]


def test_send_blocked_without_held_slot(world):
    g = _ready(world)
    v = g.pre_tool_call("schedule_send", {"thread_id": "t1", "to": [fx.LUKAS], "body": "hi",
                                          "slots": [{"start": "2026-11-13T10:00:00-08:00", "end": "2026-11-13T10:30:00-08:00"}]})
    assert v["action"] == "block" and "not held" in v["message"]


def test_send_blocked_for_tier_zero(world):
    g = _ready(world, tier=AuthorityTier.OBSERVE)
    v = g.pre_tool_call("schedule_send", {"thread_id": "t1", "to": [fx.LUKAS], "body": "hi", "slots": []})
    assert v["action"] == "block" and "tier 1" in v["message"]


def test_write_blocked_on_other_calendar(world):
    g = _ready(world)
    v = g.pre_tool_call("schedule_book", {"thread_id": "t1", "calendar": fx.PERSONAL, "slot": _slot(), "attendees": [fx.LUKAS]})
    assert v["action"] == "block" and "only writable calendar" in v["message"]


def test_write_blocked_for_tier_one_until_owner_approves(world):
    g = _ready(world, tier=AuthorityTier.OFFER)
    args = {"thread_id": "t1", "calendar": fx.WORK, "slot": _slot(), "attendees": [fx.LUKAS]}
    assert "approve" in g.pre_tool_call("schedule_book", args)["message"]
    g.facts("t1").owner_approved = True
    assert g.pre_tool_call("schedule_book", args) is None


def test_write_blocked_for_sixty_minutes_and_extra_guest(world):
    g = _ready(world)
    long_slot = {"start": "2026-11-12T13:30:00-08:00", "end": "2026-11-12T14:30:00-08:00"}
    world["ledger"].add(Slot(**long_slot), "t1", "lukas", now=world["now"])
    v = g.pre_tool_call("schedule_book", {"thread_id": "t1", "calendar": fx.WORK, "slot": long_slot, "attendees": [fx.LUKAS]})
    assert "60-minute" in v["message"]
    v2 = g.pre_tool_call("schedule_book", {"thread_id": "t1", "calendar": fx.WORK, "slot": _slot(), "attendees": [fx.LUKAS, "x@y.example"]})
    assert "attendee not on the thread" in v2["message"]


def test_other_tools_pass_and_audit_redacts(world):
    g = world["gate"]
    assert g.pre_tool_call("read_file", {"path": "x"}) is None
    line = g.audit({"tool": "x", "authorization": "Bearer abc", "note": "token ya29.AAAAbbbbCCCC", "args": {"api_key": "k"}})
    assert line["authorization"] == "[redacted]" and "ya29" not in line["note"] and line["args"]["api_key"] == "[redacted]"
    text = world["gate"].audit_path.read_text()
    assert "Bearer abc" not in text and json.loads(text.splitlines()[-1])["tool"] == "x"


def test_other_plugins_tools_pass_unless_the_owner_opts_in(world):
    from freebusy.policy import Gate
    g = world["gate"]
    for tool in ("send_email", "gmail_send", "reply_email", "create_event", "calendar_write"):
        assert g.pre_tool_call(tool, {"to": ["anyone@example.org"], "body": "hi"}) is None     # no thread_id, not ours: untouched
    assert g.pre_tool_call("schedule_send", {"to": [fx.LUKAS], "body": "hi"})["action"] == "block"
    strict = Gate(g.profile, g.ledger, vet_other_tools=True)
    assert "needs a thread" in strict.pre_tool_call("send_email", {"to": ["anyone@example.org"], "body": "hi"})["message"]
    assert strict.pre_tool_call("create_event", {"calendar": "other"})["action"] == "block"
