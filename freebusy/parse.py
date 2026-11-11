"""Turn an email into a SchedulingRequest.

Everything in a mail is evidence, never instruction. This module reads intent,
proposed times with the zones the person stated, length, participants,
who-sends-the-invite signals, authority, handoff triggers and loop guards.
Quoted text is stripped first so a forwarded "@agent" or a quoted approval
cannot count as the owner's word.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from .ontology import Authority, Intent, InviteOwner, OwnerProfile
from .zones import ZoneGuess, detect_zone, parse_pick_and_zone, zone_hint_from_header

AGENT_HEADER = "X-Scheduling-Agent"


@dataclass
class Email:
    thread_id: str
    message_id: str
    sender: str
    to: list[str]
    cc: list[str]
    subject: str
    body: str
    date: str = ""                     # RFC 2822 Date header, used for a zone hint only
    headers: dict[str, str] = field(default_factory=dict)
    sender_name: str = ""

    def participants(self) -> set[str]:
        return {a.lower() for a in [self.sender, *self.to, *self.cc] if a}


@dataclass
class ProposedTime:
    text: str
    weekday: Optional[str] = None
    date: Optional[str] = None        # YYYY-MM-DD once resolved
    hour: Optional[int] = None
    minute: int = 0
    zone: Optional[ZoneGuess] = None  # only if stated in the same message
    start: Optional[str] = None       # ISO with offset, only when the zone is known


@dataclass
class SchedulingRequest:
    thread_id: str
    message_id: str
    sender: str
    sender_name: str
    intent: str
    proposed: list[ProposedTime] = field(default_factory=list)
    pick: Optional[str] = None
    length_min: Optional[int] = None
    participants: list[str] = field(default_factory=list)
    invite_owner: str = InviteOwner.UNKNOWN.value
    invite_signals: list[str] = field(default_factory=list)
    authority: str = Authority.UNKNOWN.value
    handoff_trigger: Optional[str] = None
    loop_guard: Optional[str] = None          # set: ignore this mail, record once
    zone: Optional[ZoneGuess] = None
    zone_hint: Optional[str] = None           # from the header offset; an assumption only
    opt_out: bool = False
    out_of_scope: list[str] = field(default_factory=list)


# ---- helpers -----------------------------------------------------------------

_QUOTE_RE = re.compile(r"^\s*(>|On .+ wrote:|-----Original Message-----|From: .+)", re.MULTILINE)


def strip_quotes(body: str) -> str:
    """Keep only what this sender wrote: drop quoted replies and forwarded blocks."""
    lines = []
    for line in (body or "").splitlines():
        if re.match(r"^\s*>", line) or re.match(r"^On .+ wrote:\s*$", line) or line.strip() in (
                "-----Original Message-----", "---------- Forwarded message ---------"):
            break
        lines.append(line)
    return "\n".join(lines)


def split_signature(body: str) -> tuple[str, str]:
    m = re.search(r"\n--\s*\n|\n(Best|Regards|Kind regards|Thanks|Cheers|Many thanks)[,!]?\s*\n", body)
    if not m:
        return body, ""
    return body[:m.start()], body[m.start():]


def loop_guard(email: Email) -> Optional[str]:
    h = {k.lower(): v for k, v in email.headers.items()}
    if h.get("auto-submitted", "no").lower() != "no":
        return "auto-submitted"
    if h.get("precedence", "").lower() in ("bulk", "list", "junk", "auto_reply"):
        return f"precedence {h['precedence'].lower()}"
    if h.get(AGENT_HEADER.lower()):
        return "another scheduling agent (or our own mail)"
    if re.match(r"^(mailer-daemon|postmaster|no-?reply)@", email.sender, re.IGNORECASE):
        return "bounce or no-reply sender"
    if re.search(r"^(automatic reply|out of office|undeliverable|delivery status notification)", email.subject or "", re.IGNORECASE):
        return "auto-reply or bounce subject"
    return None


def owner_wrote(email: Email, profile: OwnerProfile) -> bool:
    return email.sender.lower() in {a.lower() for a in profile.owner_addresses}


def authority_for(email: Email, profile: OwnerProfile, thread: list[Email]) -> str:
    if owner_wrote(email, profile):
        return Authority.OWNER.value
    if email.sender.lower() in profile.allowlist:
        return Authority.ALLOWLISTED.value
    owners = {a.lower() for a in profile.owner_addresses}
    for m in thread:
        if m.sender.lower() in owners and profile.agent_address.lower() in m.participants():
            return Authority.INTRODUCED.value     # the owner forwarded, cc'd or wrote with the agent on the thread
    return Authority.UNKNOWN.value


def handoff_trigger(email: Email, profile: OwnerProfile) -> Optional[str]:
    agent = profile.agent_address.lower()
    own = strip_quotes(email.body)
    if owner_wrote(email, profile):
        if agent in email.participants():
            return "owner forward or cc"
        if re.search(r"(^|\s)@agent\b", own, re.IGNORECASE):
            return "owner @agent"
        return None
    if email.sender.lower() in profile.allowlist and agent in email.participants():
        return "allowlisted sender"
    return None


def agent_on_recipients(email: Email, profile: OwnerProfile) -> bool:
    """The agent's address is in To or Cc (a forward to the agent puts it in To)."""
    agent = profile.agent_address.lower()
    return bool(agent) and agent in {a.lower() for a in [*email.to, *email.cc] if a}


def owner_at_agent(own_text: str) -> bool:
    return bool(re.search(r"(^|\s)@agent\b", own_text or "", re.IGNORECASE))


_STOP_RE = re.compile(r"(^|\s)(@agent\s*[,:]?\s*stop|agent\s*,\s*stop)\b", re.IGNORECASE)


def owner_says_stop(own_text: str) -> bool:
    """"@agent stop" or "agent, stop" in the owner's own (un-quoted) text."""
    return bool(_STOP_RE.search(own_text or ""))


_ABOUT_BOOKING = [
    r"\b\d{1,2}(:\d{2})?\s*(am|pm|a\.m\.|p\.m\.)", r"\b\d{1,2}:\d{2}\b",                      # a time
    r"\b(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(day|nesday|rsday|urday|sday)?\b",        # a day
    r"\b(today|tonight|tomorrow|next week|this week|weekend|this (morning|afternoon|evening))\b",
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b", r"\b\d{1,2}/\d{1,2}\b",
    r"\b\d{1,3}\s*(min|mins|minutes?)\b|\b(an|one|half an|two)\s+hours?\b",                    # a length
    r"\b(move|moving|moved|resched\w*|postpone\w*|push (it|this|that)|cancel\w*|call it off|instead)\b",
    r"\bnot (that|this) (day|time|week)\b", r"\b(doesn'?t|does not|won'?t|will not) work\b", r"\bcan'?t make\b",
    r"\bi('| wi)ll (handle|take|sort|set|book|schedule|send)\b", r"\bi('ve| have) got (it|this)\b",
    r"\bleave (it|this) (to|with) me\b",
    r"\b(book|booked|booking|invite|slot|slots|availability)\b",
    r"\b(earlier|later|shorter|longer) (time|slot|day|in the day|that day|one)\b",
]


def owner_about_booking(own_text: str) -> Optional[str]:
    """The words in the owner's own text that are about the time or the booking; None when it is only social.

    About the booking: a time, a day or a date, a length, "move", "cancel", "not that day",
    "I'll handle it", "book", "invite". Only social: "thanks", "looking forward", "great to meet".
    Pass text with quotes already stripped; the signature block is ignored.
    """
    body, _ = split_signature(own_text or "")
    for p in _ABOUT_BOOKING:
        m = re.search(p, body, re.IGNORECASE)
        if m:
            return m.group(0)
    return None


_INSTRUCTIONS = (("approve", re.compile(r"^(?:approved?|yes|go ahead)\b", re.IGNORECASE)),
                 ("offer", re.compile(r"^offer\s+(\d{1,3})\b", re.IGNORECASE)),
                 ("decline", re.compile(r"^decline\b", re.IGNORECASE)))


def parse_instruction(text: str) -> Optional[tuple[str, Optional[int]]]:
    """The owner's instruction on a waiting thread, read from the start of the text.

    "approve" / "yes" / "go ahead" -> ("approve", None); "offer 30" -> ("offer", 30);
    "decline" -> ("decline", None). A leading "@agent" is allowed. Anything else -> None.
    """
    t = re.sub(r"^\s*@agent\b[\s,:]*", "", (text or "").strip(), flags=re.IGNORECASE).strip()
    for kind, rx in _INSTRUCTIONS:
        m = rx.match(t)
        if m:
            return kind, (int(m.group(1)) if kind == "offer" else None)
    return None


_LEN_RE = re.compile(r"\b(\d{1,3})\s*(?:-|\s)?\s*(?:min|mins|minutes?)\b|\b(an|one|1|half an|two|2)\s+hours?\b", re.IGNORECASE)


def parse_length(text: str) -> Optional[int]:
    m = _LEN_RE.search(text)
    if not m:
        return None
    if m.group(1):
        return int(m.group(1))
    word = m.group(2).lower()
    return {"an": 60, "one": 60, "1": 60, "half an": 30, "two": 120, "2": 120}[word]


_DAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2, "thursday": 3,
         "thu": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}
_TIME_RE = re.compile(
    r"\b(?:(?:next\s+)?(monday|mon|tuesday|tues|tue|wednesday|wed|thursday|thurs|thu|friday|fri|saturday|sat|sunday|sun)"
    r"(?:\s+(?:the\s+)?\d{1,2}(?:st|nd|rd|th)?)?)?\s*(?:,|at|@)?\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?"
    r"(?:\s*(?:my time|our time|your time))?\b", re.IGNORECASE)


def _resolve_date(weekday_word: str, now: datetime, zone: str) -> str:
    today = now.astimezone(ZoneInfo(zone)).date()
    target = _DAYS[weekday_word.lower()]
    delta = (target - today.weekday()) % 7 or 7
    return (today + timedelta(days=delta)).isoformat()


def parse_proposed_times(text: str, now: datetime, owner_zone: str, length_min: int = 30) -> list[ProposedTime]:
    out: list[ProposedTime] = []
    zone = detect_zone(text)
    for m in _TIME_RE.finditer(text):
        day, hour, minute, ampm = m.group(1), int(m.group(2)), int(m.group(3) or 0), (m.group(4) or "").replace(".", "").lower()
        if not day and not ampm and not m.group(3):
            continue                       # a bare number is not a time
        if hour > 23 or minute > 59:
            continue
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        if not ampm and not m.group(3) and hour < 7:
            hour += 12                     # "Tuesday at 2" means the afternoon
        if not ampm and m.group(3) and 1 <= hour <= 6:
            hour += 12                     # "2:30" with no am/pm means the afternoon
        pt = ProposedTime(text=m.group(0).strip(), weekday=day, hour=hour, minute=minute, zone=zone if zone and zone.source == "stated" else None)
        if day:
            pt.date = _resolve_date(day, now, owner_zone)
        if pt.date and pt.zone:
            from .zones import to_zone  # local import keeps module load light
            tz = pt.zone.zone
            try:
                start = datetime.fromisoformat(f"{pt.date}T{hour:02d}:{minute:02d}:00").replace(tzinfo=ZoneInfo(tz))
            except Exception:
                start = None
            if start:
                pt.start = start.isoformat()
        out.append(pt)
    return out


_INVITE_THEM = [r"\bi('| wi)ll send (the|an|over the|over an) invite", r"\bon behalf of\b", r"\bassistant to\b",
                r"\bexecutive assistant\b", r"\bcalendly\.com|\bscheduling link\b|\bbook a time here\b",
                r"\bwe('| wi)ll send (the|a) (calendar )?invite", r"\bsend(ing)? (you )?(the|a|an) invite from (my|our) (side|end)"]
_INVITE_US = [r"\bsend me (the|an|a) invite", r"\bplease send (the|an|a) (calendar )?invite", r"\byou (can )?send the invite",
              r"\bsend over (an|the) invite\b"]

_ASKS = [r"\bwhen (are|would) you (be )?(free|available)", r"\bavailability\b", r"\bfind (a|some) time", r"\bset up (a|the) (call|meeting|chat)",
         r"\bschedule (a|the|some time)", r"\bcould we (meet|talk|connect)", r"\bwhat (times?|days?) work", r"\bsuggest (a few|some) times",
         r"\bgrab (some )?time\b", r"\bget (something|time) (on the|in the) (calendar|books)",
         r"\b(get|find|grab|do|have|fit in)\s+(an hour|\d+\s*(min|mins|minutes)|half an hour|some time|a (quick |short )?(call|chat|meeting))\b"]
_TIME_WORDS = re.compile(r"\b(times?|slots?|availability|schedule|meet|call|chat|hour|minutes|next week|this week)\b", re.IGNORECASE)
_CONFIRMS = [r"\b(that|this|it) works\b", r"\bworks for me\b", r"\bconfirm(ed)?\b", r"\blet'?s do\b", r"\bsee you (then|there)\b",
             r"\bperfect\b", r"\bsounds good\b", r"\bbook it\b", r"\bgo with\b"]
_DECLINES = [r"\bno longer\b", r"\bcan'?t make\b", r"\bcancel\b", r"\bnot (going to|gonna) work\b", r"\bpass on\b", r"\bhold off\b",
             r"\bnever mind\b", r"\bdoesn'?t work\b"]
_OPT_OUT = [r"\brather (sort|deal|talk).*(directly|with (him|her|them))", r"\bstop\b", r"\bremove me\b", r"\b(a |the )?human\b",
            r"\bunsubscribe\b", r"\bspeak (to|with) (him|her|them) directly"]
_OUT_OF_SCOPE = {"fees or pricing": r"\b(fee|fees|pricing|price|rate|invoice|budget|quote)\b",
                 "documents": r"\b(nda|contract|proposal|deck|document|attached)\b",
                 "commitments": r"\b(commit|agree to|sign|approve the)\b"}


def _any(patterns, text) -> Optional[str]:
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return m.group(0)
    return None


def parse_email(email: Email, profile: OwnerProfile, thread: list[Email], now: datetime,
                offered_letters: int = 0) -> SchedulingRequest:
    """The one entry point. ``thread`` is every earlier message, oldest first."""
    req = SchedulingRequest(thread_id=email.thread_id, message_id=email.message_id, sender=email.sender,
                            sender_name=email.sender_name or email.sender.split("@")[0], intent=Intent.NOT_SCHEDULING.value)
    req.participants = sorted(email.participants())
    req.loop_guard = loop_guard(email)
    if req.loop_guard:
        return req
    req.authority = authority_for(email, profile, thread)
    req.handoff_trigger = handoff_trigger(email, profile)
    own = strip_quotes(email.body)
    main, signature = split_signature(own)
    req.zone = detect_zone(main, signature)
    if req.zone and req.zone.source != "stated":
        req.zone_hint, req.zone = f"{req.zone.label} ({req.zone.evidence})", None
    if not req.zone and not req.zone_hint:
        req.zone_hint = zone_hint_from_header(email.date)
    req.length_min = parse_length(main)
    sig_them = _any(_INVITE_THEM, own)
    sig_us = _any(_INVITE_US, own)
    if sig_them:
        req.invite_owner, req.invite_signals = InviteOwner.THEM.value, [sig_them]
    elif sig_us:
        req.invite_owner, req.invite_signals = InviteOwner.US.value, [sig_us]
    req.opt_out = bool(_any(_OPT_OUT, main)) and not owner_wrote(email, profile)
    req.out_of_scope = [k for k, p in _OUT_OF_SCOPE.items() if re.search(p, main, re.IGNORECASE)]

    pick, pick_zone = parse_pick_and_zone(main)
    if offered_letters and pick and ord(pick) - ord("A") < offered_letters:
        req.pick = pick
        req.intent = Intent.CONFIRMS.value
        if pick_zone and pick_zone.source == "stated":
            req.zone = pick_zone
        return req
    proposed = parse_proposed_times(main, now, profile.home_zone, req.length_min or 30)
    if _any(_DECLINES, main):
        req.intent = Intent.DECLINES.value
    elif proposed and offered_letters and _any(_CONFIRMS, main):
        req.intent, req.proposed = Intent.CONFIRMS.value, proposed
    elif proposed:
        req.intent, req.proposed = Intent.PROPOSES_TIMES.value, proposed
    elif _any(_CONFIRMS, main) and offered_letters:
        req.intent = Intent.CONFIRMS.value
    elif _any(_ASKS, main):
        req.intent = Intent.ASKS_FOR_TIMES.value
    elif (req.handoff_trigger or req.authority in (Authority.INTRODUCED.value, Authority.ALLOWLISTED.value)) and _TIME_WORDS.search(main):
        req.intent = Intent.ASKS_FOR_TIMES.value     # an introduced person writing about time is asking for times
    return req
