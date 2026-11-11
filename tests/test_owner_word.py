"""David's rule of 2026-11-10: the owner's word pauses the agent only when it is about the time or the booking."""

from datetime import timedelta

import pytest

from freebusy.adapters import fixture as fx
from freebusy.ontology import ThreadStatus
from freebusy.parse import owner_about_booking


@pytest.mark.parametrize("text", ["Lukas, let's just do Friday.", "Can we make it 3pm?", "Let's move this.", "Please cancel.",
                                  "Not that day, sorry.", "I'll handle it from here.", "45 minutes is plenty.",
                                  "Tomorrow works better", "I'll send the invite myself.", "Nov 18 is better"])
def test_about_the_booking(text):
    assert owner_about_booking(text)


@pytest.mark.parametrize("text", ["Thanks!", "Looking forward to it.", "Great to meet you, Lukas.", "Good morning Lukas, thanks for this.",
                                  "Thanks so much\n\nDana"])
def test_only_social(text):
    assert owner_about_booking(text) is None


def _owner(n, body, to, cc):
    return fx._mail("t1", n, fx.OWNER, to, cc, "Re", body, "Tue, 10 Nov 2026 08:00:00 -0800", "Dana")


def test_social_message_without_the_agent_does_not_pause(world):
    s, mail, led = world["sched"], world["mail"], world["ledger"]
    s.handle(mail.thread("zurich-no-zone"), fx.NOW)
    held = len(led.for_thread("t1"))
    a = s.handle(mail.thread("zurich-no-zone") + [_owner(3, "Thanks Lukas, looking forward to it.", [fx.LUKAS], [])],
                 fx.NOW + timedelta(hours=1))
    assert a.kind == "silent" and "only social" in a.reason
    assert s.state("t1").status == ThreadStatus.AWAITING_COUNTERPARTY.value and len(led.for_thread("t1")) == held
    # Lukas then picks, and the agent books as before.
    b = s.handle(mail.thread("b-cet"), fx.NOW + timedelta(days=1))
    assert b.kind == "reply+notify" and b.booked


def test_time_message_without_the_agent_pauses(world):
    s, mail, led = world["sched"], world["mail"], world["ledger"]
    s.handle(mail.thread("zurich-no-zone"), fx.NOW)
    a = s.handle(mail.thread("zurich-no-zone") + [_owner(3, "Lukas, not that day. I'll handle it.", [fx.LUKAS], [])],
                 fx.NOW + timedelta(hours=1))
    assert a.kind == "silent" and "paused" in a.reason
    assert s.state("t1").status == ThreadStatus.HANDED_BACK.value and led.for_thread("t1") == []


def test_quoted_time_does_not_pause(world):
    s, mail, led = world["sched"], world["mail"], world["ledger"]
    s.handle(mail.thread("zurich-no-zone"), fx.NOW)
    body = "Thank you!\n\nOn Mon, Lukas wrote:\n> Can we do Tuesday 2:30?"
    a = s.handle(mail.thread("zurich-no-zone") + [_owner(3, body, [fx.LUKAS], [])], fx.NOW + timedelta(hours=1))
    assert "only social" in a.reason and led.for_thread("t1")


def test_cc_still_means_take_it_even_with_time_words(world):
    s, mail, led = world["sched"], world["mail"], world["ledger"]
    s.handle(mail.thread("zurich-no-zone"), fx.NOW)
    a = s.handle(mail.thread("zurich-no-zone") + [_owner(3, "Lukas, Thursday would be ideal.", [fx.LUKAS], [fx.AGENT])],
                 fx.NOW + timedelta(hours=1))
    assert s.state("t1").status != ThreadStatus.HANDED_BACK.value and led.for_thread("t1")
    assert a.kind == "silent" and "taken" in a.reason
