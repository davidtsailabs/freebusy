"""Print the six demo scenes on fixture data. ``python -m freebusy.demo``."""

from __future__ import annotations

from datetime import timedelta

from .adapters import fixture as fx
from .engine import Scheduler
from .holds import HoldLedger
from .policy import Gate


def _fresh():
    p, cal, mail, led = fx.profile(), fx.FixtureCalendar(), fx.FixtureMail(), HoldLedger()
    gate = Gate(p, led)
    return Scheduler(p, cal, mail, led, gate, fx.people()), cal, mail, led, gate


def run_scenes(quiet: bool = False) -> str:
    out: list[str] = []
    say = out.append
    s, cal, mail, led, gate = _fresh()

    say("Scene 1. Zurich, no zone given")
    a = s.handle(mail.thread("zurich-no-zone"), fx.NOW)
    say(f"  parsed: {a.request.intent}, proposed \"{a.request.proposed[0].text}\", zone stated: none, header hint: {a.request.zone_hint}")
    say(f"  action: {a.kind}; {len(a.slots)} slots held for t1")
    say("  " + a.text.replace("\n", "\n  "))

    say("\nScene 2. The reply: \"B, CET\"")
    b = s.handle(mail.thread("b-cet"), fx.NOW + timedelta(days=1))
    say(f"  parsed: pick {b.request.pick}, zone {b.request.zone.label} ({b.request.zone.source})")
    say(f"  action: {b.kind}; booked {b.booked.start} as {cal.events[-1]['id']}; open holds now {len(led.open())}")
    say("  " + b.text.replace("\n", "\n  "))

    say("\nScene 3. Their assistant sends the invite")
    c = s.handle(mail.thread("their-assistant"), fx.NOW)
    say(f"  parsed: invite owner {c.request.invite_owner} ({c.request.invite_signals[0]}), zone {c.request.zone.label}")
    say(f"  action: {c.kind}; ask line: \"{[l for l in c.text.splitlines() if l.startswith('Reply')][0]}\"")
    pick = fx._mail("t3", 3, fx.THEO, [fx.AGENT], [fx.OWNER, fx.PRIYA], "Re: Harbor Bio x Dana", "A works, thanks. Invite coming from us.\n\nTheo",
                    "Tue, 10 Nov 2026 09:00:00 -0500", "Theo Marsh")
    c2 = s.handle(mail.thread("their-assistant") + [pick], fx.NOW + timedelta(days=1))
    say(f"  then: {c2.kind}; thread state {s.state('t3').status}; events created: {len([e for e in cal.events if 't3' in str(e)])} (none, they send)")

    say("\nScene 4. A hold conflict")
    before = {h.slot.start[:10] for h in led.for_thread('t3')}
    d = s.handle(mail.thread("hold-conflict"), fx.NOW)
    days = {x.start[:10] for x in d.slots}
    say(f"  t3 holds days {sorted(before)}; Marisol's offer uses {sorted(days)}; overlap: {sorted(before & days) or 'none'}")
    try:
        led.hold_offer([led.for_thread('t3')[0].slot], "t4", "marisol")
        say("  ledger accepted a double hold (bug)")
    except ValueError as exc:
        say(f"  ledger refuses to hold t3's slot for t4: {exc}")

    say("\nScene 5. A blocked send")
    e = s.handle(mail.thread("blocked-est-send"), fx.NOW)
    say(f"  Owen never stated a zone; the honest offer went out: {e.kind}")
    verdict = gate.pre_tool_call("schedule_send", {"thread_id": "t5", "to": [fx.OWEN], "cc": [fx.OWNER],
                                                   "body": "Hi Owen, how about Thu Nov 12 at 1:30 pm PT / 4:30 pm ET?", "slots": []})
    say(f"  a draft showing ET: {verdict['action']}: {verdict['message']}")
    verdict2 = gate.pre_tool_call("schedule_book", {"thread_id": "t5", "calendar": fx.PERSONAL,
                                                    "slot": {"start": e.slots[0].start, "end": e.slots[0].end}, "attendees": [fx.OWEN]})
    say(f"  a write to the personal calendar: {verdict2['action']}: {verdict2['message']}")

    say("\nScene 6. The owner hears from it")
    f = s.handle(mail.thread("owner-notification"), fx.NOW)
    say(f"  Priya asked for an hour: {f.kind}, subject \"{f.owner_subject}\"")
    say("  " + f.owner_note.replace("\n", "\n  "))
    say("\n  And the Booked note from scene 2:")
    say(f"  subject \"{b.owner_subject}\"")
    say("  " + b.owner_note.replace("\n", "\n  "))

    text = "\n".join(out)
    if not quiet:
        print(text)
    return text


if __name__ == "__main__":
    run_scenes()
