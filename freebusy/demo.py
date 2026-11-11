"""Print the demo scenes on fixture data. ``python -m freebusy.demo``."""

from __future__ import annotations

from datetime import timedelta

from .adapters import fixture as fx
from .engine import Scheduler
from .holds import HoldLedger
from .groups import describe_windows
from .ontology import TIER_NAMES
from .policy import Gate


def _fresh(profile=None):
    p, cal, mail, led = profile or fx.profile(), fx.FixtureCalendar(), fx.FixtureMail(), HoldLedger()
    gate = Gate(p, led)
    return Scheduler(p, cal, mail, led, gate, fx.people()), cal, mail, led, gate


def _rows(text: str) -> list[str]:
    return [l for l in text.splitlines() if len(l) > 2 and l[1] == "." and l[0] in "ABCDEF"]


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

    say("\nScene 7. A fresh introduction: the owner cc's the agent, the agent offers at once")
    s7, _, mail7, led7, _ = _fresh()
    intro = mail7.thread("fresh-intro")[0]
    say(f"  Dana to Nora, cc the agent: \"{intro.body.splitlines()[0]}\"")
    g = s7.handle(mail7.thread("fresh-intro"), fx.NOW)
    say(f"  action: {g.kind} to {', '.join(mail7.sent[-1]['to'])} (Dana cc'd); {len(led7.for_thread('t13'))} slots held for t13")
    say("  " + g.text.replace("\n", "\n  "))

    say("\nScene 8. The owner clears a gate and the job finishes in the same run")
    s8, _, mail8, led8, gate8 = _fresh()
    h = s8.handle(mail8.thread("owner-notification"), fx.NOW)
    say(f"  Priya asks for an hour: {h.kind}, \"{h.owner_subject}\"; waiting: {s8.pending['t6'].reason}")
    reply = fx._mail("t6", 3, fx.OWNER, [fx.AGENT], [], "Re: Needs you", "approve\n\nOn Mon, the assistant wrote:\n> NEEDS YOU",
                     "Mon, 09 Nov 2026 09:40:00 -0800", "Dana Whitfield")
    say("  Dana replies to the note: \"approve\"")
    i = s8.handle(mail8.thread("owner-notification") + [reply], fx.NOW)
    resumed = [x for x in gate8.last if "resume" in x][-1]
    say(f"  action: {i.kind} to {mail8.sent[-1]['to'][0]}; {len(i.slots)} slots of 60 minutes or more held for t6")
    say(f"  audit: approved by {resumed['approved_by']}, instruction \"{resumed['resume']}\", result {resumed['result']}")
    say("  " + i.text.replace("\n", "\n  "))

    say("\nScene 9. Trial on day one: a new user, no setup yet")
    s9, cal9, mail9, led9, gate9 = _fresh(fx.new_user_profile())
    say(f"  groups: {', '.join(f'{g.name} tier {int(g.tier)} {TIER_NAMES[int(g.tier)]}' for g in s9.profile.groups.values())}")
    j = s9.handle(mail9.thread("fresh-intro"), fx.NOW)
    say(f"  Dana cc's the assistant on an intro to Nora: {j.kind}; {len(led9.for_thread('t13'))} slots held; "
        f"tier {int(gate9.facts('t13').tier)} {TIER_NAMES[int(gate9.facts('t13').tier)]}")
    say("  " + "\n  ".join(_rows(j.text)[:3]) + "\n  ...")
    pick = fx._mail("t13", 2, fx.NORA, [fx.AGENT], [fx.OWNER], "Re", "B works. I'm in Copenhagen, so CET.\n\nNora",
                    "Tue, 10 Nov 2026 09:05:00 +0100", "Nora Lind")
    k = s9.handle(mail9.thread("fresh-intro") + [pick], fx.NOW + timedelta(days=1))
    say(f"  Nora picks B: {k.kind}, \"{k.owner_subject}\"; events created: {len(cal9.events)} (the booking waits for Dana)")
    cold = fx._mail("t21", 1, fx.OWEN, [fx.AGENT], [], "Hello", "Hi, could we find some time next week?", "Mon, 09 Nov 2026 12:40:00 -0500",
                    "Owen Hale")
    m = s9.handle([cold], fx.NOW)
    say(f"  a stranger writes to the assistant: {m.kind}; replies sent to him: "
        f"{len([x for x in mail9.sent if fx.OWEN in x['to']])} (Trial still never answers an unknown sender)")
    k2 = s9.resume("t13", "approve", fx.NOW + timedelta(days=1))
    say(f"  Dana replies approve: {k2.kind}; events created: {len(cal9.events)}")

    say("\nScene 10. Groups: Family sees full availability, Work sees 9 to 5")
    s10, _, mail10, _, _ = _fresh(fx.new_user_profile())
    fam = s10.handle(mail10.thread("family-ask"), fx.NOW)
    work = s10.handle(mail10.thread("fresh-intro"), fx.NOW)
    say(f"  Amara (whitfield.example) is in {s10.profile.group_for(s10.person_for(fx.AMARA)).name}: "
        f"{describe_windows(s10.profile.groups['family'].windows)}, full")
    say("  " + "\n  ".join(_rows(fam.text)[:4]))
    say(f"  Nora (no group match) is in {s10.profile.group_for(s10.person_for(fx.NORA)).name}: "
        f"{describe_windows(s10.profile.groups['work'].windows)}, busy-only")
    say("  " + "\n  ".join(_rows(work.text)[:4]))

    say("\nScene 11. A travel buffer pushes an offer later")
    _, cal11, _, _, _ = _fresh()
    facts = cal11.places(fx.WORK, fx.NOW, fx.NOW + timedelta(days=4), fx.BASE)
    thu = next(f for f in facts if f.slot.start.startswith("2026-11-12"))
    say(f"  what the adapter hands over for Thu Nov 12, 9:00 am: {thu.kind}, {thu.travel_min} minutes each way "
        f"(no title, no attendees, no address)")
    for on in (False, True):
        p11 = fx.profile()
        p11.groups["default"].travel_buffers = on
        s11, _, mail11, _, _ = _fresh(p11)
        o = s11.handle(mail11.thread("blocked-est-send"), fx.NOW)
        thu_rows = [r for r in _rows(o.text) if "Thu Nov 12" in r]
        say(f"  travel buffers {'on' if on else 'off'}: Owen's first Thursday row is \"{thu_rows[0][4:] if thu_rows else 'none'}\"")

    say("\nScene 12. Reminders created at booking")
    s12, cal12, mail12, _, _ = _fresh()
    s12.handle(mail12.thread("zurich-no-zone"), fx.NOW)
    b12 = s12.handle(mail12.thread("b-cet"), fx.NOW + timedelta(days=1))
    ev = cal12.events[-1]
    say(f"  on the event {ev['id']}: " + ", ".join(f"{'pop-up' if r['method'] == 'popup' else r['method']} "
                                                f"{str(r['minutes'] // 1440) + ' day' if r['minutes'] >= 1440 else str(r['minutes']) + ' minutes'} before"
                                                for r in ev["reminders"]))
    for job in b12.reminders:
        say(f"  {job.kind} to Dana at {job.at} via {job.deliver} (fixture: written to the outbox)")
    say(f"  text: {next(x.text for x in b12.reminders if x.kind == 'text')}")
    say(f"  sent to Lukas: {len([x for x in mail12.sent if fx.LUKAS in x['to']])} messages (the offer and the confirmation); "
        f"reminders to Lukas: 0")
    v = s12.gate.pre_tool_call("schedule_remind", {"thread_id": "t1", "deliver": f"email:{fx.LUKAS}", "kind": "text"})
    say(f"  a reminder aimed at Lukas: {v['action']}: {v['message']}")

    say("\nScene 13. The day-before briefing")
    s13, _, mail13, _, _ = _fresh()
    s13.handle(mail13.thread("owner-notification"), fx.NOW)
    yes = fx._mail("t6", 3, fx.OWNER, [fx.AGENT], [], "Re: Needs you", "approve", "Mon, 09 Nov 2026 09:40:00 -0800", "Dana Whitfield")
    s13.handle(mail13.thread("owner-notification") + [yes], fx.NOW)
    pick13 = fx._mail("t6", 4, fx.PRIYA, [fx.AGENT], [fx.OWNER], "Re: Deep dive",
                      "B works. Should I bring the dosing data, or keep it high level?\n\nPriya", "Mon, 09 Nov 2026 14:30:00 -0500",
                      "Priya Natarajan")
    b13 = s13.handle(mail13.thread("owner-notification") + [yes, pick13], fx.NOW + timedelta(hours=6))
    brief = next(x for x in b13.reminders if x.kind == "briefing")
    say(f"  to Dana only, at {brief.at}:")
    say("  " + brief.text.replace("\n", "\n  "))

    text = "\n".join(out)
    if not quiet:
        print(text)
    return text


if __name__ == "__main__":
    run_scenes()
