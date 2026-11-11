"""Reminders at booking: on the event, a text to the owner, a day-before briefing; never to the other person."""

import json

import pytest
from datetime import timedelta

from freebusy.adapters import fixture as fx
from freebusy.reminders import HermesCron, Job, Outbox, deliver_target, event_reminders, open_questions
from freebusy.ontology import Reminders


def _book_lukas(world):
    s, mail = world["sched"], world["mail"]
    s.handle(mail.thread("zurich-no-zone"), fx.NOW)
    return s.handle(mail.thread("b-cet"), fx.NOW + timedelta(days=1))


def test_event_carries_popup_and_email_reminders(world):
    b = _book_lukas(world)
    ev = world["cal"].events[-1]
    assert ev["reminders"] == [{"method": "popup", "minutes": 10}, {"method": "email", "minutes": 1440}]
    assert "Reminders: pop-up 10 min, email 1 day" in b.owner_note


def test_text_and_briefing_go_to_the_owner_only(world):
    b = _book_lukas(world)
    s = world["sched"]
    kinds = {j.kind: j for j in b.reminders}
    assert set(kinds) == {"text", "briefing"} and [j.kind for j in s.outbox.jobs] == ["text", "briefing"]
    start = b.booked.start_dt()
    assert kinds["text"].at == (start - timedelta(hours=1)).isoformat()
    assert kinds["briefing"].at.startswith((start - timedelta(days=1)).date().isoformat()) and "T17:00" in kinds["briefing"].at
    assert all(j.deliver == "telegram" for j in b.reminders)   # the demo owner chose Telegram at setup
    # Nothing went to Lukas but the booking confirmation.
    to_lukas = [m for m in world["mail"].sent if fx.LUKAS in m["to"]]
    assert len(to_lukas) == 2 and "Booked:" in to_lukas[-1]["text"]


def test_briefing_content(world):
    b = _book_lukas(world)
    brief = next(j.text for j in b.reminders if j.kind == "briefing")
    assert brief.startswith("TOMORROW: Lukas Brenner, Thu Nov 12")
    assert "Alpine Ventures (said CET)" in brief and "About: Intro call" in brief
    assert "picked B with CET" in brief and "sent by the assistant" in brief and "https://meet.example.com/" in brief
    assert "Open: nothing open in the thread" in brief and len(brief.splitlines()) <= 10


def test_open_questions_skip_scheduling():
    q = open_questions(["Can we do Tuesday 2:30? Should I bring the dosing data?", "When are you free?"])
    assert q == ["Should I bring the dosing data?"]


def test_their_invite_gets_text_and_briefing_but_no_event(world):
    s, mail, cal = world["sched"], world["mail"], world["cal"]
    s.handle(mail.thread("their-assistant"), fx.NOW)
    pick = fx._mail("t3", 3, fx.THEO, [fx.AGENT], [fx.OWNER, fx.PRIYA], "Re", "A works, thanks. Invite coming from us.\n\nTheo",
                    "Tue, 10 Nov 2026 09:00:00 -0500", "Theo Marsh")
    c = s.handle(mail.thread("their-assistant") + [pick], fx.NOW + timedelta(days=1))
    assert cal.events == [] and {j.kind for j in c.reminders} == {"text", "briefing"}
    brief = next(j.text for j in c.reminders if j.kind == "briefing")
    assert "Theo's side sends it" in brief and "link is in their invite" in brief


def test_reminders_off(world):
    world["profile"].reminders.on = False
    b = _book_lukas(world)
    assert b.reminders == [] and world["cal"].events[-1]["reminders"] == [] and "Reminders: off" in b.owner_note


def test_gate_refuses_a_reminder_to_the_other_person(world):
    _book_lukas(world)
    g = world["gate"]
    assert g.pre_tool_call("schedule_remind", {"thread_id": "t1", "deliver": f"email:{fx.LUKAS}"})["action"] == "block"
    assert g.pre_tool_call("schedule_remind", {"thread_id": "t1", "deliver": "email:someone@else.example"})["action"] == "block"
    assert g.pre_tool_call("schedule_remind", {"thread_id": "t1", "deliver": "telegram", "to": [fx.LUKAS]})["action"] == "block"
    assert g.pre_tool_call("schedule_remind", {"thread_id": "t1", "deliver": f"email:{fx.OWNER}"}) is None
    assert g.pre_tool_call("schedule_remind", {"thread_id": "t1", "deliver": "telegram"}) is None


def test_a_mis_set_target_is_never_scheduled(world):
    world["profile"].reminders.target = fx.LUKAS        # the owner typed the wrong address at setup
    b = _book_lukas(world)
    assert b.reminders == [] and world["sched"].outbox.jobs == []


def test_channels_and_event_reminders():
    assert deliver_target(Reminders(channel="imessage")) == "bluebubbles"
    assert deliver_target(Reminders(channel="telegram", target="12345")) == "telegram:12345"
    assert event_reminders(Reminders(popup_min=5, email_min=0)) == [{"method": "popup", "minutes": 5}]


def test_outbox_writes_jsonl(tmp_path):
    box = Outbox(tmp_path / "outbox.jsonl")
    box.schedule(Job("text", "2026-11-12T09:30:00-08:00", "sms", "In 1 hour", "t1"))
    assert json.loads((tmp_path / "outbox.jsonl").read_text())["deliver"] == "sms"


def test_hermes_cron_makes_one_run_once_job():
    calls = []
    cron = HermesCron(lambda name, args: calls.append((name, args)) or json.dumps({"job_id": "j1"}), in_cron=lambda: False)
    assert cron.schedule(Job("text", "2026-11-12T09:30:00-08:00", "telegram", "In 1 hour: Lukas", "t1")) == "j1"
    name, args = calls[0]
    assert name == "cronjob_manage" and args["action"] == "create" and args["schedule"] == "2026-11-12T09:30:00-08:00"
    assert args["deliver"] == "telegram" and args["repeat"] == 1 and "In 1 hour: Lukas" in args["prompt"]


def test_hermes_cron_refuses_inside_a_cron_run_unless_the_owner_allows_it(monkeypatch):
    from freebusy.reminders import SchedulingRefused, agent_scheduling_allowed, in_cron_run
    calls = []
    dispatch = lambda name, args: calls.append((name, args)) or json.dumps({"job_id": "j1"})
    job = Job("text", "2026-11-12T09:30:00-08:00", "telegram", "In 1 hour: Lukas", "t1")
    with pytest.raises(SchedulingRefused):
        HermesCron(dispatch, in_cron=lambda: True, allowed=lambda: False).schedule(job)
    assert calls == []
    assert HermesCron(dispatch, in_cron=lambda: True, allowed=lambda: True).schedule(job) == "j1"
    assert HermesCron(dispatch, in_cron=lambda: False, allowed=lambda: False).schedule(job) == "j1"
    # the defaults: the marker Hermes sets for cron-spawned agents, and config that cannot be read counts as no
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    assert in_cron_run() and not agent_scheduling_allowed()
    monkeypatch.delenv("HERMES_CRON_SESSION")
    assert not in_cron_run()
