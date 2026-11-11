"""The flow: parse, decide, offer or check or book, render, notify.

One action per thread per run. When the owner clears a gate, ``resume`` finishes
the waiting action in that same run. The engine never sends or writes on its own;
it hands the gate the same arguments the tools would, so a block here is the
same block Hermes would show. Adapters do the reading and the sending.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from .adapters.base import CalendarAdapter, MailAdapter, all_busy, all_places
from .holds import HoldLedger
from .offer import build_offer, check_proposed, still_free
from .ontology import (TIER_NAMES, Authority, AuthorityTier, Intent, InviteOwner, OwnerProfile, Person, Slot, ThreadState,
                       ThreadStatus, now_iso)
from .parse import (Email, SchedulingRequest, agent_on_recipients, loop_guard, owner_about_booking, owner_at_agent,
                    owner_says_stop, parse_email, parse_instruction, split_signature, strip_quotes)
from .policy import Gate
from .reminders import Job, Outbox, briefing_text, briefing_time, deliver_target, event_reminders, first_ask, open_questions, reminder_text
from .travel import travel_blocks
from .render import OwnerNote, Reply, Row, note_subject, render_html, render_owner_note, render_text
from .zones import format_day, format_slot_for, label_for, zone_line


@dataclass
class Action:
    kind: str                       # reply | reply+notify | notify | silent | blocked | answer (to the owner)
    thread_id: str
    reason: str = ""
    text: str = ""
    html: str = ""
    owner_note: Optional[str] = None
    owner_subject: str = ""
    slots: list[Slot] = field(default_factory=list)
    booked: Optional[Slot] = None
    request: Optional[SchedulingRequest] = None
    reminders: list = field(default_factory=list)   # reminder Jobs scheduled for the owner


# Why a request waits for the owner, and what "approve" does for each.
WAIT_REASONS = {
    "long_meeting": "offer the length they asked for",
    "unknown_sender": "treat the sender as introduced and offer times",
    "outside_windows": "book the time they proposed",
    "approve_first": "book the time they picked",
    "nothing_fits": "try one more round of offers",
}


@dataclass
class Pending:
    """The request waiting on the owner for one thread, and why."""
    reason: str                     # a key of WAIT_REASONS
    detail: str
    request: SchedulingRequest
    thread: list[Email]
    slot: Optional[Slot] = None     # the time waiting to be booked (approve_first, outside_windows, long_meeting)
    at: str = field(default_factory=now_iso)


def accepted_instructions(thread_id: str, p: Optional[Pending] = None) -> str:
    what = f" ({p.detail})" if p else ""
    approve = WAIT_REASONS.get(p.reason, "approve the waiting action") if p else "approve the waiting action"
    return (f"On {thread_id}{what} I accept:\n"
            f"- approve, yes or go ahead: {approve}\n"
            f"- offer <minutes>, for example offer 30: offer that length instead\n"
            f"- decline: send a polite no and release the holds")


class Scheduler:
    def __init__(self, profile: OwnerProfile, calendar: CalendarAdapter, mail: MailAdapter, ledger: HoldLedger,
                 gate: Optional[Gate] = None, people: Optional[dict[str, Person]] = None, outbox=None):
        self.profile, self.cal, self.mail, self.ledger = profile, calendar, mail, ledger
        self.gate = gate or Gate(profile, ledger)
        self.people = people or {}
        self.threads: dict[str, ThreadState] = {}
        self.pending: dict[str, Pending] = {}
        self.outbox = outbox if outbox is not None else Outbox()     # fixture: recorded; live: Hermes scheduled jobs

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

    def busy_for(self, group, now: datetime, days: int = 21) -> list[Slot]:
        """Busy time for building offers: free/busy, plus travel there and back around in-person events when the
        group opted in. Only derived facts are used (in person + minutes, or virtual); nothing else is read."""
        busy = self.busy(now, days)
        if not getattr(group, "travel_buffers", False):
            return busy
        facts = all_places(self.cal, now - timedelta(days=1), now + timedelta(days=days), self.profile.base_address,
                           self.profile.default_travel_min)
        return sorted(busy + travel_blocks(facts), key=lambda s: s.start)

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
            return self._owner_message(req, latest, thread, now)
        if st.status == ThreadStatus.HANDED_BACK.value:
            return Action("silent", req.thread_id, "handed back to the owner; silent until the owner cc's the agent or writes @agent",
                          request=req)
        return self._act(req, thread, now)

    # -- the owner's word --------------------------------------------------------
    def _owner_message(self, req: SchedulingRequest, latest: Email, thread: list[Email], now: datetime) -> Action:
        """The owner wrote in the thread. Two rules, both David's of 2026-11-10.

        "cc always means take it":
        - An instruction on a waiting thread (the owner's own text begins with approve / yes /
          go ahead / offer N / decline, with the agent on the recipients or @agent) resumes it.
        - "@agent stop" or "agent, stop" in the owner's own text hands the thread back.
        - The agent on To or Cc (a forward counts), or @agent in the owner's own text: the agent
          takes the thread and acts in this same run. If the latest message from someone else is
          a scheduling request it is handled now, as introduced; if there is no request yet, the
          agent offers held slots to the other participants with the zone ask.

        "The owner's word pauses it only when it is about the time or the booking":
        - The owner writes without the agent on the recipients, and their own words are about the
          time or the booking (a time, a day, a length, "move", "cancel", "not that day",
          "I'll handle it"): the agent pauses. Holds are released and it stays silent until the
          owner cc's it again or writes @agent.
        - The owner writes without the agent and the message is only social ("thanks",
          "looking forward"): nothing changes. The agent keeps the thread and its holds.

        Only the owner's own text counts; quoted text is stripped first. Kept in one function on
        purpose, so what the owner's word means mid-thread can be changed in one place.
        """
        st = self.state(req.thread_id)
        own = strip_quotes(latest.body)
        addressed = agent_on_recipients(latest, self.profile) or owner_at_agent(own)
        if addressed and req.thread_id in self.pending and parse_instruction(own):
            return self.resume(req.thread_id, own, now, approved_by=f"owner by email ({latest.sender})")
        if owner_says_stop(own):
            return self._hand_back(req, "the owner wrote @agent stop")
        if not addressed:
            if not (self.ledger.for_thread(req.thread_id) or st.history):
                return Action("silent", req.thread_id, "the owner wrote without the agent; nothing to take", request=req)
            if st.status == ThreadStatus.HANDED_BACK.value:
                return Action("silent", req.thread_id, "already handed back to the owner", request=req)
            about = owner_about_booking(own)
            if about:
                return self._hand_back(req, f"paused: the owner wrote about the time or the booking (\"{about}\")")
            self.gate.audit({"thread": req.thread_id, "owner_word": "social", "kept": True})
            return Action("silent", req.thread_id, "the owner's message is only social; the agent keeps the thread", request=req)

        # The agent takes the thread.
        if st.status == ThreadStatus.HANDED_BACK.value or not st.history:
            st.move(ThreadStatus.OFFERING.value, "the owner cc'd the agent: taking the thread")
        owners = {a.lower() for a in self.profile.owner_addresses}
        agent = self.profile.agent_address.lower()
        others = [m for m in thread[:-1] if m.sender.lower() not in owners | {agent} and not loop_guard(m)]
        if others and others[-1].message_id not in st.handled:
            msg = others[-1]
            earlier = [m for m in thread if m is not msg]
            theirs = parse_email(msg, self.profile, earlier, now, offered_letters=len(self.ledger.for_thread(req.thread_id)))
            if theirs.intent in (Intent.ASKS_FOR_TIMES.value, Intent.PROPOSES_TIMES.value, Intent.CONFIRMS.value):
                theirs.authority = Authority.INTRODUCED.value
                return self._act(theirs, thread, now)
        if req.thread_id in self.pending:
            return Action("silent", req.thread_id, f"taken; waiting on the owner: {self.pending[req.thread_id].detail}", request=req)
        if self.ledger.for_thread(req.thread_id) or st.status in (ThreadStatus.DONE.value, ThreadStatus.AWAITING_INVITE.value):
            return Action("silent", req.thread_id, "taken; the last request is already answered", request=req)
        return self._offer_unasked(req, latest, thread, now)

    def _offer_unasked(self, req: SchedulingRequest, latest: Email, thread: list[Email], now: datetime) -> Action:
        """A fresh introduction: nobody has asked yet, so offer held slots to the other participants."""
        owners = {a.lower() for a in self.profile.owner_addresses}
        agent = self.profile.agent_address.lower()
        to = [a for a in dict.fromkeys(x.lower() for x in [*latest.to, *latest.cc]) if a not in owners | {agent}]
        if not to:
            to = sorted({a for m in thread for a in m.participants()} - owners - {agent})
        if not to:
            return Action("silent", req.thread_id, "taken; nobody else on the thread to offer to", request=req)
        person = self.person_for(to[0])
        names = " and ".join(self.person_for(a).name.split()[0] for a in to)
        ask = SchedulingRequest(thread_id=req.thread_id, message_id=req.message_id, sender=to[0], sender_name=person.name,
                                intent=Intent.ASKS_FOR_TIMES.value, length_min=req.length_min,
                                participants=sorted({a for m in thread for a in m.participants()}),
                                authority=Authority.INTRODUCED.value, handoff_trigger="owner forward or cc")
        if req.length_min:
            self.gate.facts(req.thread_id).approved_len_min = req.length_min    # the owner's own word sets the length
        return self._act(ask, thread, now, to=to, greet=names, intro=True)

    def _hand_back(self, req: SchedulingRequest, why: str) -> Action:
        released = self.ledger.release(req.thread_id)
        self.pending.pop(req.thread_id, None)
        self.state(req.thread_id).move(ThreadStatus.HANDED_BACK.value, why)
        return Action("silent", req.thread_id, f"handed back: {why}; {len(released)} holds released", request=req)

    # -- acting on a request ---------------------------------------------------
    def _act(self, req: SchedulingRequest, thread: list[Email], now: datetime, to: Optional[list[str]] = None,
             greet: str = "", intro: bool = False) -> Action:
        st = self.state(req.thread_id)
        if req.message_id and req.message_id not in st.handled:
            st.handled.append(req.message_id)
        self.pending.pop(req.thread_id, None)          # a newer request replaces whatever was waiting
        if st.introduced and req.authority == Authority.UNKNOWN.value:
            req.authority = Authority.INTRODUCED.value
        person = self.person_for(req.sender)
        if req.zone and req.zone.source == "stated":
            person.zone, person.zone_source = req.zone.zone, "stated"
        facts = self.gate.facts(req.thread_id)
        facts.participants = set(req.participants) | {a for m in thread for a in m.participants()}
        facts.person_id, facts.person_zone = person.id, person.zone
        facts.authority = req.authority
        facts.tier = self.profile.tier_for(req.authority, req.sender, self.profile.group_for(person))
        st.person_id = person.id
        if req.invite_owner != InviteOwner.UNKNOWN.value:
            st.invite_owner = req.invite_owner

        if req.opt_out:
            st.move(ThreadStatus.HANDED_BACK.value, "the person asked for the owner")
            return self._notify(req, "Needs you", f"{person.name} would rather sort it out with you", ask="Take the thread")
        if req.out_of_scope:
            return self._notify(req, "Needs you", f"{person.name} asked about {', '.join(req.out_of_scope)}", ask="Answer them")
        if req.authority == Authority.UNKNOWN.value:      # whatever the tier, nobody introduced them: the owner decides
            st.move(ThreadStatus.AWAITING_OWNER.value, "unknown sender")
            self._wait(req, thread, "unknown_sender", f"{person.name} wrote and nobody introduced them")
            return self._notify(req, "Needs you", f"{person.name} wrote to the assistant and nobody introduced them",
                                ask="Reply approve or decline")
        if req.intent == Intent.CONFIRMS.value:
            return self._book(req, person, st, thread, now)
        if req.intent == Intent.DECLINES.value:
            self.ledger.release(req.thread_id)
            st.move(ThreadStatus.DONE.value, "declined")
            return Action("silent", req.thread_id, "declined; holds released", request=req)
        if req.intent in (Intent.ASKS_FOR_TIMES.value, Intent.PROPOSES_TIMES.value):
            return self._offer(req, person, st, thread, now, to=to, greet=greet, intro=intro)
        return Action("silent", req.thread_id, "not a scheduling message", request=req)

    def _wait(self, req: SchedulingRequest, thread: list[Email], reason: str, detail: str, slot: Optional[Slot] = None) -> Pending:
        p = Pending(reason=reason, detail=detail, request=req, thread=list(thread), slot=slot)
        self.pending[req.thread_id] = p
        self.gate.audit({"thread": req.thread_id, "waiting": reason, "detail": detail})
        return p

    # -- resume: the owner cleared a gate ------------------------------------------
    def resume(self, thread_id: str, instruction: str, now: datetime, approved_by: str = "owner") -> Action:
        """Apply the owner's instruction to the request waiting on ``thread_id`` and finish the job in this run.

        "approve" / "yes" / "go ahead" approves the exact pending action; "offer N" offers N minutes
        instead; "decline" sends a polite no and releases holds; anything else is answered with the
        accepted instructions. Everything resume does still goes through the gate.
        """
        p = self.pending.get(thread_id)
        parsed = parse_instruction(instruction)
        if p is None:
            act = Action("answer", thread_id, "nothing is waiting", text=f"Nothing on {thread_id} is waiting for you.")
        elif parsed is None:
            act = Action("answer", thread_id, "instruction not recognised", text=accepted_instructions(thread_id, p))
        else:
            kind, minutes = parsed
            self.pending.pop(thread_id)
            req, thread, st, facts = p.request, p.thread, self.state(thread_id), self.gate.facts(thread_id)
            st.move(ThreadStatus.OFFERING.value, f"the owner said '{instruction.strip()[:40]}' ({approved_by})")
            if kind == "decline":
                act = self._decline(req, thread, now)
            elif kind == "offer":
                self.ledger.release(thread_id)
                if p.reason == "unknown_sender":
                    st.introduced, req.authority = True, Authority.INTRODUCED.value
                facts.approved_len_min = max(facts.approved_len_min, minutes)
                st.offer_rounds = 0
                req.length_min, req.intent, req.proposed, req.pick = minutes, Intent.ASKS_FOR_TIMES.value, [], None
                act = self._act(req, thread, now)
            else:
                if p.reason == "long_meeting":
                    facts.approved_len_min = max(facts.approved_len_min, req.length_min or 0)
                elif p.reason == "unknown_sender":
                    st.introduced, req.authority = True, Authority.INTRODUCED.value
                elif p.reason == "approve_first":
                    facts.owner_approved = True
                elif p.reason == "outside_windows" and p.slot is not None:
                    self.ledger.release(thread_id)
                    try:
                        self.ledger.add(p.slot, thread_id, facts.person_id or req.sender, now)
                    except ValueError as exc:
                        act = self._notify(req, "Needs you", f"Their time is no longer free: {exc}", ask="Reply offer 30 or decline")
                        return self._audit_resume(thread_id, instruction, approved_by, p, act)
                    req.intent, req.pick = Intent.CONFIRMS.value, "A"
                elif p.reason == "nothing_fits":
                    st.offer_rounds = 0
                act = self._act(req, thread, now)
        return self._audit_resume(thread_id, instruction, approved_by, p, act)

    def _audit_resume(self, thread_id: str, instruction: str, approved_by: str, p: Optional["Pending"], act: Action) -> Action:
        self.gate.audit({"thread": thread_id, "resume": instruction.strip()[:80], "approved_by": approved_by,
                         "pending": p.reason if p else None, "result": act.kind, "detail": act.reason})
        return act

    def _decline(self, req: SchedulingRequest, thread: list[Email], now: datetime) -> Action:
        released = self.ledger.release(req.thread_id)
        st, facts = self.state(req.thread_id), self.gate.facts(req.thread_id)
        facts.participants |= {a for m in thread for a in m.participants()}
        facts.tier = max(facts.tier, AuthorityTier.OFFER)     # the owner's own instruction authorises this one reply
        facts.authority = "owner_instruction"
        person = self.person_for(req.sender)
        first = self.profile.name.split()[0]
        reply = Reply(name=person.name.split()[0], owner_name=self.profile.name,
                      lead=f"Thank you for reaching out. {first} cannot make this one work, so I will close it here.")
        act = self._send(req, reply, [])
        st.move(ThreadStatus.DONE.value, f"declined on the owner's word; {len(released)} holds released")
        act.reason = f"{act.reason}; declined, {len(released)} holds released"
        return act

    # -- offer ----------------------------------------------------------------
    def _offer(self, req: SchedulingRequest, person: Person, st: ThreadState, thread: list[Email], now: datetime,
               to: Optional[list[str]] = None, greet: str = "", intro: bool = False) -> Action:
        group = self.profile.group_for(person)
        busy = self.busy_for(group, now)
        facts = self.gate.facts(req.thread_id)
        extra = ""
        their_time: Optional[Slot] = None
        if req.intent == Intent.PROPOSES_TIMES.value:
            resolved = [Slot(p.start, (datetime.fromisoformat(p.start) + timedelta(minutes=req.length_min or group.default_len_min)).isoformat())
                        for p in req.proposed if p.start]
            if resolved:
                checks = check_proposed(self.profile, group, busy, self.ledger, req.thread_id, resolved, now)
                for c in checks:
                    if c.ok:
                        return self._confirm_slot(req, person, st, c.slot, thread, now)
                for c in checks:     # free on every calendar and held by nobody, but outside the windows or the notice
                    if still_free(c.slot, busy) and self.ledger.is_free(c.slot, req.thread_id):
                        their_time = c.slot
                        break
                extra = (f"{req.proposed[0].text.capitalize()} is spoken for on {self.profile.name.split()[0]}'s side. "
                         f"These are open for {req.length_min or group.default_len_min} minutes:")
            else:
                extra = (f"I cannot place \"{req.proposed[0].text}\" without your time zone. Open on "
                         f"{self.profile.name.split()[0]}'s side for {req.length_min or group.default_len_min} minutes:")
        if st.offer_rounds >= self.profile.max_offer_rounds:
            st.move(ThreadStatus.AWAITING_OWNER.value, "offer rounds used up")
            self._wait(req, thread, "nothing_fits", f"two offer rounds with {person.name} found no time")
            return self._notify(req, "Nothing fits", f"Two offer rounds with {person.name} found no time",
                                ask="Reply approve to retry, or decline")
        approved = bool(req.length_min) and 0 < (req.length_min or 0) <= facts.approved_len_min
        result = build_offer(self.profile, group, busy, self.ledger, req.thread_id, now, length_min=req.length_min,
                             person_zone=person.zone, owner_approved_length=approved)
        if result.needs_owner:
            st.move(ThreadStatus.AWAITING_OWNER.value, "; ".join(result.needs_owner))
            self._wait(req, thread, "long_meeting", f"{person.name} asked for {req.length_min} minutes")
            return self._notify(req, "Needs you", f"{person.name} asked for {req.length_min} minutes",
                                ask="Reply approve, offer 30, or decline", who=person.name)
        if not result.slots:
            st.move(ThreadStatus.AWAITING_OWNER.value, "nothing free inside the rules")
            self._wait(req, thread, "nothing_fits", f"no free time inside the rules for {person.name}")
            return self._notify(req, "Nothing fits", f"No free time inside the rules for {person.name}",
                                ask="Open a window, or reply decline")
        self.ledger.hold_offer(result.slots, req.thread_id, person.id, now)
        st.offer_rounds += 1
        st.length_min = result.length_min
        st.move(ThreadStatus.AWAITING_COUNTERPARTY.value, f"offered {len(result.slots)} slots")
        rows = [Row(format_day(s.start_dt(), self.profile.home_zone),
                    format_slot_for(s.start_dt(), s.end_dt(), self.profile.home_zone, person.zone)) for s in result.slots]
        first = self.profile.name.split()[0]
        ask = ("Reply with a letter and I will send the invite, or send yours to " + self.profile.owner_addresses[0] + "."
               if st.invite_owner == InviteOwner.UNKNOWN.value else
               "Reply with a letter and I will send the invite." if st.invite_owner == InviteOwner.US.value else
               "Reply with a letter and send the invite when ready.")
        lead = extra or f"{first} can do {result.length_min} minutes on these days:"
        if intro:
            lead = f"{first} asked me to find a time with you. {lead}"
        reply = Reply(name=greet or person.name.split()[0], lead=lead, rows=rows, ask=ask,
                      zone_line=zone_line(self.profile.home_zone, person.zone, req.zone_hint), owner_name=self.profile.name)
        act = self._send(req, reply, result.slots, to=to)
        if their_time is not None and act.kind == "reply":
            # Their own time is free but outside the rules: the counter-offer went out, and the owner may still say yes to theirs.
            self._wait(req, thread, "outside_windows", f"{person.name} proposed {req.proposed[0].text}, outside the windows",
                       slot=their_time)
        return act

    def _confirm_slot(self, req, person, st, slot: Slot, thread, now) -> Action:
        """Their proposed time fits: hold it, then book under the same gate as a pick."""
        self.ledger.hold_offer([slot], req.thread_id, person.id, now)
        req.pick = "A"
        st.history.append([now_iso(), st.status, f"their proposed time fit: {req.proposed[0].text if req.proposed else slot.start}"])
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
        length = min(req.length_min or st.length_min or group.default_len_min, int((chosen.end_dt() - chosen.start_dt()).total_seconds() // 60))
        booked = Slot(chosen.start, (chosen.start_dt() + timedelta(minutes=length)).isoformat())
        if not still_free(booked, self.busy(now)):
            self.ledger.release(req.thread_id)
            req.intent = Intent.ASKS_FOR_TIMES.value
            return self._offer(req, person, st, thread, now)
        facts = self.gate.facts(req.thread_id)
        facts.person_zone = person.zone
        if st.invite_owner == InviteOwner.THEM.value and facts.tier < AuthorityTier.BOOK and not facts.owner_approved:
            # Agreeing a time for their invite is the booking, so below Book it waits for the owner like any other.
            when = f"{format_day(booked.start_dt(), self.profile.home_zone)} · {format_slot_for(booked.start_dt(), booked.end_dt(), self.profile.home_zone, person.zone)}"
            st.move(ThreadStatus.AWAITING_OWNER.value, f"{TIER_NAMES[int(facts.tier)]}: the booking waits for the owner")
            self._wait(req, thread, "approve_first", f"{person.name} picked {when}", slot=booked)
            return self._notify(req, "Needs you", f"{person.name} picked {when}", ask="Reply approve to accept their invite",
                                who=person.name)
        if st.invite_owner == InviteOwner.THEM.value:
            st.move(ThreadStatus.AWAITING_INVITE.value, f"they send the invite for {booked.start}")
            reply = Reply(name=person.name.split()[0], lead=f"{format_day(booked.start_dt(), self.profile.home_zone)}, "
                          f"{format_slot_for(booked.start_dt(), booked.end_dt(), self.profile.home_zone, person.zone)} works. "
                          f"Send the invite to {self.profile.owner_addresses[0]} and I will accept it.", owner_name=self.profile.name)
            act = self._send(req, reply, [])
            if act.kind == "reply":
                act.reminders = self._remind(req, person, st, booked, "", thread, now, ours=False)
            return act
        self.ledger.add(booked, req.thread_id, person.id, now) if not self.ledger.find(req.thread_id, booked) else None
        verdict = self.gate.pre_tool_call("schedule_book", {"thread_id": req.thread_id, "calendar": self.profile.write_calendar,
                                                            "slot": {"start": booked.start, "end": booked.end}, "attendees": [req.sender]})
        if verdict:
            st.move(ThreadStatus.AWAITING_OWNER.value, verdict["message"])
            if verdict.get("code") in ("approve_first", "long_meeting"):
                when = f"{format_day(booked.start_dt(), self.profile.home_zone)} · {format_slot_for(booked.start_dt(), booked.end_dt(), self.profile.home_zone, person.zone)}"
                self._wait(req, thread, verdict["code"], f"{person.name} picked {when}", slot=booked)
                act = self._notify(req, "Needs you", f"{person.name} picked {when}", ask="Reply approve to book it", who=person.name)
                act.reason = verdict["message"]
                return act
            return Action("blocked", req.thread_id, verdict["message"], request=req)
        event_id = self.cal.create_event(self.profile.write_calendar, booked, f"{self.profile.name.split()[0]} and {person.name}", [req.sender],
                                         reminders=event_reminders(self.profile.reminders))
        agreed = self._agreed(req, st, person)
        self.ledger.book(req.thread_id, booked)
        self.pending.pop(req.thread_id, None)
        st.move(ThreadStatus.DONE.value, f"booked {booked.start} as {event_id}")
        when = f"{format_day(booked.start_dt(), self.profile.home_zone)} · {format_slot_for(booked.start_dt(), booked.end_dt(), self.profile.home_zone, person.zone)}"
        reply = Reply(name=person.name.split()[0], lead=f"Booked: {when}. The invite is on its way.", owner_name=self.profile.name)
        act = self._send(req, reply, [])
        act.reminders = self._remind(req, person, st, booked, event_id, thread, now, ours=True, agreed=agreed)
        note = OwnerNote(state="Booked", headline=f"{person.name}, {when}", who=person.name, when=when, ask="Nothing",
                         stops=[s[2] for s in st.history[-4:]], thread_ref=req.thread_id, block_elsewhere=True,
                         reminders=self._reminder_line(act.reminders))
        act.owner_note, act.owner_subject, act.booked = render_owner_note(note), note_subject(note), booked
        act.kind = "reply+notify"
        return act

    # -- reminders: the owner's alone ------------------------------------------------
    def _agreed(self, req: SchedulingRequest, st: ThreadState, person: Person) -> str:
        first = person.name.split()[0]
        fit = next((h[2] for h in reversed(st.history) if h[2].startswith("their proposed time fit")), None)
        if fit:
            how = f"{first} proposed \"{fit.split(': ', 1)[-1]}\" and it fit your rules"
        elif req.pick:
            rounds = max(st.offer_rounds, 1)
            z = f" with {req.zone.label}" if req.zone and req.zone.source == "stated" else ""
            how = f"{first} picked {req.pick}{z} from {rounds} round{'s' if rounds > 1 else ''} of held times"
        else:
            how = f"{first} confirmed by email"
        facts = self.gate.facts(req.thread_id)
        if facts.owner_approved:
            how += "; you approved it"
        elif facts.approved_len_min > self.profile.max_len_without_owner_min:
            how += f"; you approved {facts.approved_len_min} minutes"
        return how

    def _reminder_line(self, jobs: list) -> str:
        r = self.profile.reminders
        if not r.on:
            return "Reminders: off"
        parts = [f"pop-up {r.popup_min} min" if r.popup_min else "", f"email {r.email_min // 60 // 24} day" if r.email_min else ""]
        parts += [f"{j.kind} {'by ' + r.channel if j.kind == 'text' else 'the evening before'}" for j in jobs]
        return "Reminders: " + ", ".join(p for p in parts if p)

    def _remind(self, req: SchedulingRequest, person: Person, st: ThreadState, booked: Slot, event_id: str, thread: list[Email],
                now: datetime, ours: bool, agreed: str = "") -> list:
        """Schedule the text and the day-before briefing for the owner. Each passes the gate, which refuses any target
        that is not the owner's own."""
        r = self.profile.reminders
        if not r.on:
            return []
        zone = self.profile.home_zone
        link = getattr(self.cal, "join_link", lambda _e: None)(event_id) if event_id else None
        where = f"Join: {link}" if link else ("Join: the link is in their invite" if not ours else "Join: no link on the event")
        deliver = deliver_target(r)
        owners = {a.lower() for a in self.profile.owner_addresses}
        agent = self.profile.agent_address.lower()
        theirs = [split_signature(strip_quotes(m.body))[0] for m in thread if m.sender.lower() not in owners | {agent}]
        jobs: list[Job] = []
        if r.text_before_min:
            at = booked.start_dt() - timedelta(minutes=r.text_before_min)
            if at > now:
                jobs.append(Job("text", at.isoformat(), deliver, reminder_text(person, booked, zone, r.text_before_min, where),
                                req.thread_id, event_id, name=f"freebusy reminder {req.thread_id}"))
        if r.briefing:
            invite = (f"sent by the assistant from {self.profile.write_calendar}" if ours else f"{person.name.split()[0]}'s side sends it")
            text = briefing_text(person, person.org or "", booked, zone, thread[0].subject if thread else "", "",
                                 first_ask(theirs), agreed or self._agreed(req, st, person), invite, where,
                                 open_questions(theirs), req.thread_id)
            at = briefing_time(booked, zone, r.briefing_at, now)
            jobs.append(Job("briefing", at.isoformat(), deliver, text, req.thread_id, event_id, name=f"freebusy briefing {req.thread_id}"))
        done = []
        for j in jobs:
            verdict = self.gate.pre_tool_call("schedule_remind", {"thread_id": j.thread_id, "deliver": j.deliver, "kind": j.kind})
            if verdict:
                continue
            j.job_id = self.outbox.schedule(j)
            done.append(j)
        return done

    # -- send and notify -------------------------------------------------------
    def _send(self, req: SchedulingRequest, reply: Reply, slots: list[Slot], to: Optional[list[str]] = None) -> Action:
        text, html = render_text(reply), render_html(reply)
        to = list(to or [req.sender])
        args = {"thread_id": req.thread_id, "to": to, "cc": list(self.profile.owner_addresses), "body": text,
                "slots": [{"start": s.start, "end": s.end} for s in slots]}
        verdict = self.gate.pre_tool_call("schedule_send", args)
        if verdict:
            return Action("blocked", req.thread_id, verdict["message"], text=text, request=req)
        self.mail.send(req.thread_id, to, list(self.profile.owner_addresses), "Re: scheduling", text, html)
        return Action("reply", req.thread_id, "sent", text=text, html=html, slots=slots, request=req)

    def _notify(self, req: SchedulingRequest, state: str, headline: str, ask: str = "Nothing", who: str = "") -> Action:
        st = self.state(req.thread_id)
        note = OwnerNote(state=state, headline=headline, who=who or req.sender_name, ask=ask,
                         stops=[h[2] for h in st.history[-4:]] or [headline], thread_ref=req.thread_id)
        return Action("notify", req.thread_id, headline, owner_note=render_owner_note(note), owner_subject=note_subject(note), request=req)
