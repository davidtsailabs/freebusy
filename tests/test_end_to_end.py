"""Zurich mail -> offer with zone ask -> "B, CET" -> book -> holds cleared -> owner notified."""

import re
from datetime import timedelta

from freebusy.adapters import fixture as fx
from freebusy.ontology import ThreadStatus
from freebusy.render import words_before_signature


def test_zurich_to_booked(world):
    s, mail, led, cal = world["sched"], world["mail"], world["ledger"], world["cal"]

    a = s.handle(mail.thread("zurich-no-zone"), fx.NOW)
    assert a.kind == "reply"
    assert "Hi Lukas," in a.text and "A.  " in a.text and "B.  " in a.text
    assert "PT" in a.text and not re.search(r"\b(CET|CEST|ET|EST)\b", a.text.split("Times are")[0])
    assert "you look to be on Central European time" in a.text
    assert "Dana is cc'd here" in a.text and a.text.endswith("AI assistant to Dana Whitfield · handles scheduling only")
    assert "<table" in a.html and "font:600" in a.html
    assert words_before_signature(a.text.split("Times are")[0]) < 90     # the reply proper stays short; the footnote is extra
    held = sorted(led.for_thread("t1"), key=lambda h: h.slot.start)
    assert len(held) == len(a.slots) >= 3 and len({h.slot.start[:10] for h in held}) <= 3
    assert s.state("t1").status == ThreadStatus.AWAITING_COUNTERPARTY.value
    assert mail.sent[-1]["cc"] == [fx.OWNER] and mail.sent[-1]["headers"]["X-Scheduling-Agent"] == "1"

    b = s.handle(mail.thread("b-cet"), fx.NOW + timedelta(days=1))
    assert b.kind == "reply+notify"
    assert b.booked.start == held[1].slot.start                  # B is the second row
    assert "CET" in b.text and "Booked:" in b.text               # converted once, now that the zone is stated
    assert led.for_thread("t1") == [] and led.open() == []      # every hold for the thread cleared
    assert len(cal.events) == 1 and cal.events[0]["calendar"] == fx.WORK and cal.events[0]["attendees"] == [fx.LUKAS]
    assert s.state("t1").status == ThreadStatus.DONE.value
    assert b.owner_subject.startswith("Booked: block") and "BOOKED" in b.owner_note and "Lukas Brenner" in b.owner_note
    assert s.people["lukas"].zone == "Europe/Berlin" and s.people["lukas"].zone_source == "stated"


def test_their_assistant_keeps_the_hold_and_creates_no_event(world):
    s, mail, led, cal = world["sched"], world["mail"], world["ledger"], world["cal"]
    a = s.handle(mail.thread("their-assistant"), fx.NOW)
    assert a.kind == "reply" and "send the invite when ready" in a.text and "ET" in a.text
    pick = fx._mail("t3", 3, fx.THEO, [fx.AGENT], [fx.OWNER, fx.PRIYA], "Re", "A works.", "Tue, 10 Nov 2026 09:00:00 -0500", "Theo")
    b = s.handle(mail.thread("their-assistant") + [pick], fx.NOW + timedelta(days=1))
    assert b.kind == "reply" and "Send the invite to dana@example.com" in b.text
    assert s.state("t3").status == ThreadStatus.AWAITING_INVITE.value and cal.events == []
    assert led.for_thread("t3")                                  # the hold stays until their invite lands


def test_hold_conflict_spreads_and_refuses(world):
    s, mail, led = world["sched"], world["mail"], world["ledger"]
    s.handle(mail.thread("zurich-no-zone"), fx.NOW)
    d = s.handle(mail.thread("hold-conflict"), fx.NOW)
    assert d.kind == "reply"
    t1 = led.for_thread("t1")
    assert not any(x.overlaps(h.slot) for x in d.slots for h in t1)
    assert not ({h.slot.start[:10] for h in t1} & {x.start[:10] for x in d.slots})


def test_owner_notified_for_an_hour(world):
    s, mail = world["sched"], world["mail"]
    f = s.handle(mail.thread("owner-notification"), fx.NOW)
    assert f.kind == "notify" and f.owner_subject.startswith("Needs you:") and "60 minutes" in f.owner_note
    assert mail.sent == []                                       # nothing went to the person
    assert s.state("t6").status == ThreadStatus.AWAITING_OWNER.value


def test_owner_writing_without_the_agent_hands_back(world):
    # Changed 2026-11-10 ("cc always means take it"): writing with the agent still cc'd no longer hands back;
    # dropping the agent from the recipients does.
    s, mail, led = world["sched"], world["mail"], world["ledger"]
    s.handle(mail.thread("zurich-no-zone"), fx.NOW)
    owner = fx._mail("t1", 3, fx.OWNER, [fx.LUKAS], [], "Re", "Lukas, let's just do Friday.", "Tue, 10 Nov 2026 08:00:00 -0800", "Dana")
    a = s.handle(mail.thread("zurich-no-zone") + [owner], fx.NOW + timedelta(days=1))
    assert a.kind == "silent" and s.state("t1").status == ThreadStatus.HANDED_BACK.value
    assert led.for_thread("t1") == []                            # holds go back to the pool


def test_loop_guard_is_silent(world):
    s, mail = world["sched"], world["mail"]
    assert s.handle(mail.thread("auto-reply"), fx.NOW).kind == "silent"
    assert s.handle(mail.thread("own-mail"), fx.NOW).kind == "silent"
    assert mail.sent == []


def test_plugin_registers_with_a_bare_context(tmp_path, monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path

    monkeypatch.setenv("FREEBUSY_DATA_DIR", str(tmp_path))
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("freebusy_plugin", root / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["freebusy_plugin"] = mod
    spec.loader.exec_module(mod)

    class Ctx:
        def __init__(self):
            self.tools, self.hooks, self.commands = {}, {}, {}

        def register_tool(self, name, toolset, schema, handler, **kw):
            self.tools[name] = handler

        def register_hook(self, name, fn, **kw):
            self.hooks[name] = fn

        def register_command(self, name, handler, description=""):
            self.commands[name] = handler

        def get_config(self, key, default=None):
            return default

    ctx = Ctx()
    mod.register(ctx)
    assert set(ctx.tools) == {"schedule_parse", "schedule_offer", "schedule_check", "schedule_book", "schedule_holds", "schedule_resume"}
    assert set(ctx.hooks) == {"pre_tool_call", "post_tool_call", "pre_llm_call", "post_approval_response", "post_gateway_admission"}
    assert set(ctx.commands) == {"schedule", "freebusy", "holds", "zones"}
    assert ctx.hooks["post_gateway_admission"](platform="telegram", source={"user_id": "1"}, text="Tuesday?") is None   # fixture: agent as usual
    assert "freebusy facts" in ctx.hooks["pre_llm_call"](session_id="s", user_message="x")["context"]
    assert ctx.hooks["pre_tool_call"](tool_name="read_file", args={}) is None
    blocked = ctx.hooks["pre_tool_call"](tool_name="schedule_book", args={"thread_id": "t1", "calendar": "other", "slot": {"start": "a", "end": "b"}})
    assert blocked["action"] == "block"
    assert "Scene 1" in ctx.commands["schedule"]("demo")
    assert "CET" in ctx.commands["zones"]("B, CET")
    mod.register(object())      # a context with nothing on it is fine
