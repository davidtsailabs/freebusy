"""David's two decisions of 2026-11-10: "cc always means take it" and "when the gate is unblocked, finish the job"."""

import json
from datetime import timedelta

from freebusy.adapters import fixture as fx
from freebusy.engine import Scheduler
from freebusy.holds import HoldLedger
from freebusy.ontology import ThreadStatus
from freebusy.policy import Gate


def _owner(thread, n, body, to=None, cc=None):
    return fx._mail(thread, n, fx.OWNER, to if to is not None else [fx.AGENT], cc if cc is not None else [], "Re", body,
                    "Mon, 09 Nov 2026 10:00:00 -0800", "Dana Whitfield")


# ---- 1. cc always means take it ----------------------------------------------------

def test_fresh_introduction_offers_straight_away(world):
    s, mail, led = world["sched"], world["mail"], world["ledger"]
    a = s.handle(mail.thread("fresh-intro"), fx.NOW)
    assert a.kind == "reply" and a.slots
    assert a.text.startswith("Hi Nora,") and "Dana asked me to find a time with you." in a.text
    assert "A.  " in a.text and "not your zone? Add it after the letter" in a.text    # the zone ask
    assert len(led.for_thread("t13")) == len(a.slots)                                   # every slot held
    assert mail.sent[-1]["to"] == [fx.NORA] and mail.sent[-1]["cc"] == [fx.OWNER]
    assert s.state("t13").status == ThreadStatus.AWAITING_COUNTERPARTY.value


def test_owner_cc_on_an_unanswered_request_handles_it_now(world):
    s, led = world["sched"], world["ledger"]
    # Lukas wrote to Dana alone; Dana replies with the agent cc'd. The agent answers Lukas's ask in this run.
    lukas = fx._mail("t20", 1, fx.LUKAS, [fx.OWNER], [], "Call", "Hi Dana, when are you free next week for a call?\n\nLukas",
                     "Mon, 09 Nov 2026 17:42:00 +0100", "Lukas Brenner")
    dana = _owner("t20", 2, "Lukas, my assistant (cc) will find us a time.", to=[fx.LUKAS], cc=[fx.AGENT])
    a = s.handle([lukas, dana], fx.NOW)
    assert a.kind == "reply" and a.text.startswith("Hi Lukas,") and a.request.authority == "introduced"
    assert "asked me to find" not in a.text and led.for_thread("t20")


def test_owner_cc_on_an_active_thread_keeps_it(world):
    s, mail, led = world["sched"], world["mail"], world["ledger"]
    s.handle(mail.thread("zurich-no-zone"), fx.NOW)
    sent = len(mail.sent)
    owner = fx._mail("t1", 3, fx.OWNER, [fx.LUKAS], [fx.AGENT], "Re", "Lukas, looking forward to it.", "Tue, 10 Nov 2026 08:00:00 -0800", "Dana")
    a = s.handle(mail.thread("zurich-no-zone") + [owner], fx.NOW + timedelta(hours=1))
    assert a.kind == "silent" and "already answered" in a.reason and len(mail.sent) == sent   # no duplicate offer
    assert s.state("t1").status == ThreadStatus.AWAITING_COUNTERPARTY.value and led.for_thread("t1")


def test_agent_stop_hands_back_and_stays_silent_until_cc_again(world):
    s, mail, led = world["sched"], world["mail"], world["ledger"]
    s.handle(mail.thread("zurich-no-zone"), fx.NOW)
    thread = mail.thread("zurich-no-zone")
    stop = fx._mail("t1", 3, fx.OWNER, [fx.LUKAS], [fx.AGENT], "Re", "@agent stop. Lukas, I'll pick this up.\n\n> @agent find a time",
                    "Tue, 10 Nov 2026 08:00:00 -0800", "Dana")
    a = s.handle(thread + [stop], fx.NOW + timedelta(hours=1))
    assert a.kind == "silent" and s.state("t1").status == ThreadStatus.HANDED_BACK.value and led.for_thread("t1") == []
    lukas = fx._mail("t1", 4, fx.LUKAS, [fx.OWNER], [fx.AGENT], "Re", "Could you send me a few more times for next week?",
                     "Tue, 10 Nov 2026 18:00:00 +0100", "Lukas Brenner")
    sent = len(mail.sent)
    assert s.handle(thread + [stop, lukas], fx.NOW + timedelta(hours=2)).kind == "silent" and len(mail.sent) == sent
    back = fx._mail("t1", 5, fx.OWNER, [fx.LUKAS], [fx.AGENT], "Re", "Agent, please take this back.", "Tue, 10 Nov 2026 10:00:00 -0800", "Dana")
    c = s.handle(thread + [stop, lukas, back], fx.NOW + timedelta(hours=3))
    assert c.kind in ("reply", "reply+notify") and len(mail.sent) == sent + 1               # Lukas's waiting message, handled now
    assert s.state("t1").status != ThreadStatus.HANDED_BACK.value


def test_agent_comma_stop_also_hands_back(world):
    s, mail = world["sched"], world["mail"]
    s.handle(mail.thread("fresh-intro"), fx.NOW)
    stop = _owner("t13", 2, "Agent, stop. I have this.", to=[fx.NORA], cc=[fx.AGENT])
    assert s.handle(mail.thread("fresh-intro") + [stop], fx.NOW).kind == "silent"
    assert s.state("t13").status == ThreadStatus.HANDED_BACK.value


# ---- 2. when the gate is unblocked, finish the job ------------------------------------

def test_priya_hour_approved_by_email_offers_sixty_in_the_same_run(world):
    s, mail, led, gate = world["sched"], world["mail"], world["ledger"], world["gate"]
    f = s.handle(mail.thread("owner-notification"), fx.NOW)
    assert f.kind == "notify" and s.pending["t6"].reason == "long_meeting"
    a = s.handle(mail.thread("owner-notification") + [_owner("t6", 3, "approve")], fx.NOW + timedelta(minutes=10))
    assert a.kind == "reply" and "60 minutes" in a.text and a.slots
    assert all((x.end_dt() - x.start_dt()) >= timedelta(minutes=60) for x in a.slots)
    assert mail.sent[-1]["to"] == [fx.PRIYA] and "t6" not in s.pending
    line = [json.loads(x) for x in gate.audit_path.read_text().splitlines() if '"resume"' in x][-1]
    assert line["approved_by"] == f"owner by email ({fx.OWNER})" and line["resume"] == "approve" and line["result"] == "reply"
    # Priya picks A; the 60-minute booking passes because the owner approved that length.
    pick = fx._mail("t6", 4, fx.PRIYA, [fx.AGENT], [fx.OWNER], "Re", "A works.", "Mon, 09 Nov 2026 14:00:00 -0500", "Priya")
    b = s.handle(mail.thread("owner-notification") + [_owner("t6", 3, "approve"), pick], fx.NOW + timedelta(hours=1))
    assert b.kind == "reply+notify" and (b.booked.end_dt() - b.booked.start_dt()) == timedelta(minutes=60)


def test_offer_30_instead(world):
    s, mail = world["sched"], world["mail"]
    s.handle(mail.thread("owner-notification"), fx.NOW)
    a = s.resume("t6", "offer 30", fx.NOW, approved_by="owner via /schedule")
    assert a.kind == "reply" and "30 minutes" in a.text


def test_decline_sends_a_polite_no_and_releases(world):
    s, mail, led = world["sched"], world["mail"], world["ledger"]
    s.handle(mail.thread("owner-notification"), fx.NOW)
    a = s.resume("t6", "decline", fx.NOW)
    assert a.kind == "reply" and "cannot make this one work" in a.text and led.for_thread("t6") == []
    assert s.state("t6").status == ThreadStatus.DONE.value and mail.sent[-1]["to"] == [fx.PRIYA]


def test_unknown_instruction_lists_the_accepted_ones(world):
    s, mail = world["sched"], world["mail"]
    s.handle(mail.thread("owner-notification"), fx.NOW)
    a = s.resume("t6", "hmm, maybe later", fx.NOW)
    assert a.kind == "answer" and "approve, yes or go ahead" in a.text and "offer <minutes>" in a.text and "decline" in a.text
    assert "t6" in s.pending and mail.sent == []                 # still waiting
    assert s.resume("t99", "approve", fx.NOW).text == "Nothing on t99 is waiting for you."


def test_unknown_sender_approved_becomes_introduced_and_offered(world):
    s, mail = world["sched"], world["mail"]
    cold = fx._mail("t21", 1, fx.OWEN, [fx.AGENT], [], "Hello", "Hi, could we find some time next week to talk?\n\nOwen",
                    "Mon, 09 Nov 2026 12:40:00 -0500", "Owen Hale")
    n = s.handle([cold], fx.NOW)
    assert n.kind == "notify" and s.pending["t21"].reason == "unknown_sender" and mail.sent == []
    a = s.resume("t21", "go ahead", fx.NOW)
    assert a.kind == "reply" and a.slots and mail.sent[-1]["to"] == [fx.OWEN]
    assert s.state("t21").introduced
    # The approval does not bypass the zone rule: Owen never stated a zone, so no second zone appears.
    assert "ET" not in a.text.split("Times are")[0]


def test_approve_first_booking_books_the_held_pick(tmp_path):
    p = fx.profile()
    p.auto_book = False
    led = HoldLedger(tmp_path / "h.json")
    gate = Gate(p, led, tmp_path / "a.jsonl")
    mail, cal = fx.FixtureMail(), fx.FixtureCalendar()
    s = Scheduler(p, cal, mail, led, gate, fx.people())
    s.handle(mail.thread("zurich-no-zone"), fx.NOW)
    b = s.handle(mail.thread("b-cet"), fx.NOW + timedelta(days=1))
    assert b.kind == "notify" and s.pending["t1"].reason == "approve_first" and cal.events == []
    a = s.resume("t1", "yes", fx.NOW + timedelta(days=1))
    assert a.kind == "reply+notify" and len(cal.events) == 1 and cal.events[0]["calendar"] == fx.WORK
    assert led.for_thread("t1") == [] and s.state("t1").status == ThreadStatus.DONE.value


def test_approval_does_not_bypass_the_one_calendar_rule(world):
    s, mail, gate = world["sched"], world["mail"], world["ledger"]
    s.handle(mail.thread("owner-notification"), fx.NOW)
    a = s.resume("t6", "approve", fx.NOW)
    g = world["gate"]
    v = g.pre_tool_call("schedule_book", {"thread_id": "t6", "calendar": fx.PERSONAL,
                                          "slot": {"start": a.slots[0].start, "end": a.slots[0].end}, "attendees": [fx.PRIYA]})
    assert v["action"] == "block" and "only writable calendar" in v["message"]


def test_proposed_time_outside_windows_can_be_approved(world):
    s, mail, cal = world["sched"], world["mail"], world["cal"]
    intro = mail.thread("owner-notification")[0]
    ask = fx._mail("t6", 2, fx.PRIYA, [fx.AGENT], [fx.OWNER], "Re", "Could we do Tuesday at 4pm ET for 30 minutes?",
                   "Mon, 09 Nov 2026 13:00:00 -0500", "Priya")
    a = s.handle([intro, ask], fx.NOW)
    assert a.kind == "reply" and s.pending["t6"].reason == "outside_windows"   # counter-offer sent, her time kept for the owner
    b = s.resume("t6", "approve", fx.NOW)
    assert b.kind == "reply+notify" and b.booked.start_dt() == fx.NOW.replace(day=10, hour=13) and len(cal.events) == 1


# ---- the three sources wired to resume, through the plugin ---------------------------

def _plugin(tmp_path, monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path

    monkeypatch.setenv("FREEBUSY_DATA_DIR", str(tmp_path))
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("freebusy_plugin_resume", root / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["freebusy_plugin_resume"] = mod
    spec.loader.exec_module(mod)
    mod.register(object())
    return mod


def test_slash_commands_resume(tmp_path, monkeypatch):
    mod = _plugin(tmp_path, monkeypatch)
    assert mod._cmd_schedule("run owner-notification").startswith("notify:")
    assert "t6  long_meeting" in mod._cmd_schedule("waiting")
    assert "approve, yes or go ahead" in mod._cmd_schedule("t6 maybe")
    out = mod._cmd_schedule("approve t6")
    assert out.startswith("reply:") and "60 minutes" in out
    assert mod._cmd_schedule("waiting") == "nothing is waiting on you"
    audit = (tmp_path / "audit.jsonl").read_text()
    assert '"approved_by": "owner via /schedule approve"' in audit


def test_hermes_approval_path(tmp_path, monkeypatch):
    mod = _plugin(tmp_path, monkeypatch)
    mod._cmd_schedule("run owner-notification")
    args = {"thread_id": "t6", "instruction": "offer 30"}
    d = mod._pre_tool_call(tool_name="schedule_resume", args=args)
    assert d["action"] == "approve" and d["rule_key"].startswith("freebusy-resume:t6:")
    # Without the person's yes the handler does nothing.
    assert "not observed" in json.loads(mod.schedule_resume(args))["blocked"]
    # A model (smart) approval does not count; a person's does.
    mod._post_approval_response(pattern_key=f"plugin_rule:{d['rule_key']}", choice="smart_approve", decided_by="aux_llm")
    assert "not observed" in json.loads(mod.schedule_resume(args))["blocked"]
    mod._post_approval_response(pattern_key=f"plugin_rule:{d['rule_key']}", choice="once", surface="cli")
    r = json.loads(mod.schedule_resume(args))
    assert r["kind"] == "reply" and "30 minutes" in r["text"]
    assert mod._pre_tool_call(tool_name="schedule_resume", args=args)["action"] == "block"    # nothing waits any more
    assert '"approved_by": "owner via Hermes approval (cli, once)"' in (tmp_path / "audit.jsonl").read_text()
