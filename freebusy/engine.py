"""The flow: parse, decide, offer or check or book, render, notify.

One action per thread per run. The engine never sends or writes on its own;
it hands the gate the same arguments the tools would, so a block here is the
same block Hermes would show. Adapters do the reading and the sending.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from .adapters.base import CalendarAdapter, MailAdapter, all_busy
from .holds import HoldLedger
from .offer import build_offer, check_proposed, still_free
from .ontology import Authority, AuthorityTier, Intent, InviteOwner, OwnerProfile, Person, Slot, ThreadState, ThreadStatus
from .parse import Email, SchedulingRequest, parse_email
from .policy import Gate
from .render import OwnerNote, Reply, Row, note_subject, render_html, render_owner_note, render_text
from .zones import format_day, format_slot_for, label_for, zone_line


@dataclass
class Action:
    kind: str                       # reply | notify | silent | blocked | draft
    thread_id: str
    reason: str = ""
    text: str = ""
    html: str = ""
    owner_note: Optional[str] = None
    owner_subject: str = ""
    slots: list[Slot] = field(default_factory=list)
    booked: Optional[Slot] = None
    request: Optional[SchedulingRequest] = None


class Scheduler:
    def __init__(self, profile: OwnerProfile, calendar: CalendarAdapter, mail: MailAdapter, ledger: HoldLedger,
                 gate: Optional[Gate] = None, people: Optional[dict[str, Person]] = None):
        self.profile, self.cal, self.mail, self.ledger = profile, calendar, mail, ledger
        self.gate = gate or Gate(profile, ledger)
        self.people = people or {}
        self.threads: dict[str, ThreadState] = {}

    # -- lookups -------------------------------------------------------------
    def state(self, thread_id: str) -> ThreadState:
        return self.threads.setdefault(thread_id, ThreadState(thread_id=thread_id))

    def person_for(self, email: str) -> Person:
        for p in self.people.values():
            if email.lower() in {e.lower() for e in p.emails}:
                return p
        p = Person(id=email.lower(), name=email.split("@")[0], emails=[email])
        self.people[p.id] = p
        return p

    def busy(self, now: datetime, days: int = 21) -> list[Slot]:
        return all_busy(self.cal, now - timedelta(days=1), now + timedelta(days=days))

    # -- the one entry point ---------------------------------------------------
    def handle(self, thread: list[Email], now: datetime) -> Action:
        latest = thread[-1]
        st = self.state(latest.thread_id)
        offered = len(self.ledger.for_thread(latest.thread_id))
        req = parse_email(latest, self.profile, thread[:-1], now, offered_letters=offered)
        if req.loop_guard:
            self.gate.audit({"thread": req.thread_id, "ignored": req.loop_guard})
            return Action("silent", req.thread_id, f"ignored: {req.loop_guard}", request=req)
        if req.authority == Authority.OWNER.value:
            active = bool(self.ledger.for_thread(req.thread_id)) or st.history
            if req.handoff_trigger == "owner @agent":
                st.move(ThreadStatus.OFFERING.value, "owner @agent: taking the thread")
                return Action("silent", req.thread_id, "owner instruction noted; acting on the next message", request=req)
            if active:
                st.move(ThreadStatus.HANDED_BACK.value, "the owner wrote; the owner's word wins")
                return Action("silent", req.thread_id, "owner wrote in the thread; going quiet", request=req)
            return Action("silent", req.thread_id, "owner introduced the agent; waiting for the other side", request=req)

        person = self.person_for(req.sender)
        if req.zone and req.zone.source == "stated":
            person.zone, person.zone_source = req.zone.zone, "stated"
        facts = self.gate.facts(req.thread_id)
        facts.participants = set(req.participants) | {m.sender.lower() for m in thread}
        facts.person_id, facts.person_zone = person.id, person.zone
        facts.authority = req.authority
        facts.tier = self.profile.tier_for(req.authority, req.sender)
        st.person_id = person.id
        if req.invite_owner != InviteOwner.UNKNOWN.value:
            st.invite_owner = req.invite_owner

        if req.opt_out:
            st.move(ThreadStatus.HANDED_BACK.value, "the person asked for the owner")
            return self._notify(req, "Needs you", f"{person.name} would rather sort it out with you", ask="Take the thread")
        if req.out_of_scope:
            return self._notify(req, "Needs you", f"{person.name} asked about {', '.join(req.out_of_scope)}", ask="Answer them")
        if facts.tier < AuthorityTier.OFFER:
            st.move(ThreadStatus.AWAITING_OWNER.value, "unknown sender")
            return self._notify(req, "Needs you", f"{person.name} wrote to the assistant and nobody introduced them",
                                ask="Introduce or ignore")
        if req.intent == Intent.CONFIRMS.value:
            return self._book(req, person, st, thread, now)
        if req.intent == Intent.DECLINES.value:
            self.ledger.release(req.thread_id)
            st.move(ThreadStatus.DONE.value, "declined")
            return Action("silent", req.thread_id, "declined; holds released", request=req)
        if req.intent in (Intent.ASKS_FOR_TIMES.value, Intent.PROPOSES_TIMES.value):
            return self._offer(req, person, st, thread, now)
        return Action("silent", req.thread_id, "not a scheduling message", request=req)

    # -- offer ----------------------------------------------------------------
    def _offer(self, req: SchedulingRequest, person: Person, st: ThreadState, thread: list[Email], now: datetime) -> Action:
        group = self.profile.group_for(person)
        busy = self.busy(now)
        extra = ""
        if req.intent == Intent.PROPOSES_TIMES.value:
            resolved = [Slot(p.start, (datetime.fromisoformat(p.start) + timedelta(minutes=req.length_min or group.default_len_min)).isoformat())
                        for p in req.proposed if p.start]
            if resolved:
                checks = check_proposed(self.profile, group, busy, self.ledger, req.thread_id, resolved, now)
                for c in checks:
                    if c.ok:
                        return self._confirm_slot(req, person, st, c.slot, thread, now)
                extra = (f"{req.proposed[0].text.capitalize()} is spoken for on {self.profile.name.split()[0]}'s side. "
                         f"These are open for {req.length_min or group.default_len_min} minutes:")
            else:
                extra = (f"I cannot place \"{req.proposed[0].text}\" without your time zone. Open on "
                         f"{self.profile.name.split()[0]}'s side for {req.length_min or group.default_len_min} minutes:")
        if st.offer_rounds >= self.profile.max_offer_rounds:
            return self._notify(req, "Nothing fits", f"Two offer rounds with {person.name} found no time", ask="Take it from here")
        result = build_offer(self.profile, group, busy, self.ledger, req.thread_id, now, length_min=req.length_min, person_zone=person.zone)
        if result.needs_owner:
            st.move(ThreadStatus.AWAITING_OWNER.value, "; ".join(result.needs_owner))
            return self._notify(req, "Needs you", f"{person.name} asked for {req.length_min} minutes",
                                ask=f"Approve {req.length_min} minutes or offer 30", who=person.name)
        if not result.slots:
            return self._notify(req, "Nothing fits", f"No free time inside the rules for {person.name}", ask="Open a window or decline")
        self.ledger.hold_offer(result.slots, req.thread_id, person.id, now)
        st.offer_rounds += 1
        st.move(ThreadStatus.AWAITING_COUNTERPARTY.value, f"offered {len(result.slots)} slots")
        rows = [Row(format_day(s.start_dt(), self.profile.home_zone),
                    format_slot_for(s.start_dt(), s.end_dt(), self.profile.home_zone, person.zone)) for s in result.slots]
        first = self.profile.name.split()[0]
        ask = ("Reply with a letter and I will send the invite, or send yours to " + self.profile.owner_addresses[0] + "."
               if st.invite_owner == InviteOwner.UNKNOWN.value else
               "Reply with a letter and I will send the invite." if st.invite_owner == InviteOwner.US.value else
               "Reply with a letter and send the invite when ready.")
        lead = extra or f"{first} can do {result.length_min} minutes on these days:"
        reply = Reply(name=person.name.split()[0], lead=lead, rows=rows, ask=ask,
                      zone_line=zone_line(self.profile.home_zone, person.zone, req.zone_hint), owner_name=self.profile.name)
        return self._send(req, reply, result.slots)

    def _confirm_slot(self, req, person, st, slot: Slot, thread, now) -> Action:
        """Their proposed time fits: hold it, then book under the same gate as a pick."""
        self.ledger.hold_offer([slot], req.thread_id, person.id, now)
        req.pick = "A"
        return self._book(req, person, st, thread, now)

    # -- book ------------------------------------------------------------------
    def _book(self, req: SchedulingRequest, person: Person, st: ThreadState, thread: list[Email], now: datetime) -> Action:
        holds = sorted(self.ledger.for_thread(req.thread_id), key=lambda h: h.slot.start)
        if not holds:
            return Action("silent", req.thread_id, "a confirmation with nothing held; nothing to book", request=req)
        if not req.pick:
            if len(holds) == 1:
                req.pick = "A"
            else:
                reply = Reply(name=person.name.split()[0], lead="Which one did you mean? Reply with the letter.", owner_name=self.profile.name,
                              rows=[Row(format_day(h.slot.start_dt(), self.profile.home_zone),
                                        format_slot_for(h.slot.start_dt(), h.slot.end_dt(), self.profile.home_zone, person.zone)) for h in holds])
                return self._send(req, reply, [h.slot for h in holds])
        idx = ord(req.pick) - ord("A")
        if idx >= len(holds):
            return Action("silent", req.thread_id, "pick does not match an offered letter", request=req)
        chosen = holds[idx].slot
        group = self.profile.group_for(person)
        length = min(req.length_min or group.default_len_min, int((chosen.end_dt() - chosen.start_dt()).total_seconds() // 60))
        booked = Slot(chosen.start, (chosen.start_dt() + timedelta(minutes=length)).isoformat())
        if not still_free(booked, self.busy(now)):
            self.ledger.release(req.thread_id)
            req.intent = Intent.ASKS_FOR_TIMES.value
            return self._offer(req, person, st, thread, now)
        facts = self.gate.facts(req.thread_id)
        facts.person_zone = person.zone
        if st.invite_owner == InviteOwner.THEM.value:
            st.move(ThreadStatus.AWAITING_INVITE.value, f"they send the invite for {booked.start}")
            reply = Reply(name=person.name.split()[0], lead=f"{format_day(booked.start_dt(), self.profile.home_zone)}, "
                          f"{format_slot_for(booked.start_dt(), booked.end_dt(), self.profile.home_zone, person.zone)} works. "
                          f"Send the invite to {self.profile.owner_addresses[0]} and I will accept it.", owner_name=self.profile.name)
            return self._send(req, reply, [])
        self.ledger.add(booked, req.thread_id, person.id, now) if not self.ledger.find(req.thread_id, booked) else None
        verdict = self.gate.pre_tool_call("schedule_book", {"thread_id": req.thread_id, "calendar": self.profile.write_calendar,
                                                            "slot": {"start": booked.start, "end": booked.end}, "attendees": [req.sender]})
        if verdict:
            st.move(ThreadStatus.AWAITING_OWNER.value, verdict["message"])
            return Action("blocked", req.thread_id, verdict["message"], request=req)
        event_id = self.cal.create_event(self.profile.write_calendar, booked, f"{self.profile.name.split()[0]} and {person.name}", [req.sender])
        self.ledger.book(req.thread_id, booked)
        st.move(ThreadStatus.DONE.value, f"booked {booked.start} as {event_id}")
        when = f"{format_day(booked.start_dt(), self.profile.home_zone)} · {format_slot_for(booked.start_dt(), booked.end_dt(), self.profile.home_zone, person.zone)}"
        reply = Reply(name=person.name.split()[0], lead=f"Booked: {when}. The invite is on its way.", owner_name=self.profile.name)
        act = self._send(req, reply, [])
        note = OwnerNote(state="Booked", headline=f"{person.name}, {when}", who=person.name, when=when, ask="Nothing",
                         stops=[s[2] for s in st.history[-4:]], thread_ref=req.thread_id, block_elsewhere=True)
        act.owner_note, act.owner_subject, act.booked = render_owner_note(note), note_subject(note), booked
        act.kind = "reply+notify"
        return act

    # -- send and notify -------------------------------------------------------
    def _send(self, req: SchedulingRequest, reply: Reply, slots: list[Slot]) -> Action:
        text, html = render_text(reply), render_html(reply)
        args = {"thread_id": req.thread_id, "to": [req.sender], "cc": list(self.profile.owner_addresses), "body": text,
                "slots": [{"start": s.start, "end": s.end} for s in slots]}
        verdict = self.gate.pre_tool_call("schedule_send", args)
        if verdict:
            return Action("blocked", req.thread_id, verdict["message"], text=text, request=req)
        self.mail.send(req.thread_id, [req.sender], list(self.profile.owner_addresses), "Re: scheduling", text, html)
        return Action("reply", req.thread_id, "sent", text=text, html=html, slots=slots, request=req)

    def _notify(self, req: SchedulingRequest, state: str, headline: str, ask: str = "Nothing", who: str = "") -> Action:
        st = self.state(req.thread_id)
        note = OwnerNote(state=state, headline=headline, who=who or req.sender_name, ask=ask,
                         stops=[h[2] for h in st.history[-4:]] or [headline], thread_ref=req.thread_id)
        return Action("notify", req.thread_id, headline, owner_note=render_owner_note(note), owner_subject=note_subject(note), request=req)
