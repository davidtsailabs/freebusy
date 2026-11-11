"""The ontology: people, groups, the owner profile, constraints, holds, thread state.

Every fact carries a source (stated, inferred, calendar) and a date. Nothing
here does any work; it is the shared vocabulary for the other modules, with
plain JSON (de)serialization so the whole state can live in one file.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum, IntEnum
from typing import Any, Optional

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Source(str, Enum):
    STATED = "stated"        # the person wrote it
    INFERRED = "inferred"    # guessed from evidence; must be shown as an assumption
    CALENDAR = "calendar"    # read from free/busy
    OWNER = "owner"          # set by the owner in the profile


class Intent(str, Enum):
    ASKS_FOR_TIMES = "asks_for_times"
    PROPOSES_TIMES = "proposes_times"
    CONFIRMS = "confirms"
    DECLINES = "declines"
    NOT_SCHEDULING = "not_scheduling"


class Authority(str, Enum):
    OWNER = "owner"                # the owner's own message
    INTRODUCED = "introduced"      # the owner forwarded, cc'd or wrote in the thread
    ALLOWLISTED = "allowlisted"    # a sender on the owner's list
    UNKNOWN = "unknown"


class AuthorityTier(IntEnum):
    """How far the agent may go on its own with an introduced or allowlisted sender.

    Unknown senders never get a reply without the owner, whatever the tier: that rule is
    the sender's authority (``Authority.UNKNOWN``), checked by the engine and the gate.
    """
    TRIAL = 0     # offers held slots in the thread under the rules, so a new user sees it work; the booking waits for the owner
    OFFER = 1     # replies with held slots, asks the zone, holds; the booking waits for the owner
    BOOK = 2      # also books a fitting stated time, clears holds, notifies the owner
    MANAGE = 3    # also reschedules and cancels (defined, not implemented)
    OBSERVE = 0   # the 0.1 name for tier 0, kept as an alias of TRIAL


TIER_NAMES = {0: "Trial", 1: "Offer", 2: "Book", 3: "Manage"}


class ThreadStatus(str, Enum):
    OFFERING = "offering"
    AWAITING_COUNTERPARTY = "awaiting_counterparty"
    AWAITING_INVITE = "awaiting_invite"
    AWAITING_OWNER = "awaiting_owner"
    DONE = "done"
    HANDED_BACK = "handed_back"


class InviteOwner(str, Enum):
    US = "us"
    THEM = "them"
    UNKNOWN = "unknown"


@dataclass
class Fact:
    """A value with provenance. ``source`` says who said it; ``at`` says when."""
    value: Any
    source: str = Source.STATED.value
    at: str = field(default_factory=now_iso)


@dataclass
class Person:
    id: str
    name: str
    emails: list[str] = field(default_factory=list)
    zone: Optional[str] = None            # IANA name, only if stated
    zone_source: Optional[str] = None     # stated | inferred
    source: str = Source.STATED.value
    stated_at: str = field(default_factory=now_iso)
    group_id: Optional[str] = None
    tier: int = AuthorityTier.TRIAL
    org: Optional[str] = None             # their organization, when known; used in the owner's briefing only


@dataclass
class Window:
    """A working window on one weekday, in the owner's home zone. "HH:MM" strings."""
    start: str
    end: str


@dataclass
class Group:
    """A class of people the owner treats alike (Work, Family, sponsors).

    ``members`` are addresses and ``domains`` are mail domains; a sender matching either
    belongs here. ``tier`` caps what an introduced sender in this group gets.
    ``visibility``: "busy_only" offers inside the windows and, when the person stated a zone,
    inside their 9 to 5 too; "full" offers every free minute inside the windows, any day.
    ``travel_buffers``: opt-in; blocks travel time around the owner's in-person events.
    """
    id: str
    name: str
    windows: dict[str, list[Window]] = field(default_factory=dict)  # weekday -> windows
    buffer_min: int = 30
    default_len_min: int = 30
    earliest_days_out: int = 3
    horizon_days: int = 14
    tier: int = AuthorityTier.BOOK
    visibility: str = "busy_only"
    members: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    travel_buffers: bool = False

    def matches(self, address: str) -> bool:
        a = (address or "").lower()
        return bool(a) and (a in {m.lower() for m in self.members}
                            or a.rsplit("@", 1)[-1] in {d.lower().lstrip("@") for d in self.domains})


@dataclass
class MeetingType:
    id: str
    name: str
    length_min: int = 30
    medium: str = "video"   # video | call | in_person
    needs_owner: bool = False


@dataclass
class Constraint:
    kind: str                       # zone | length | window | earliest | medium | person
    value: Any
    source: str = Source.STATED.value
    at: str = field(default_factory=now_iso)


@dataclass
class Slot:
    """A candidate meeting time. ISO 8601 with offset, in the owner's home zone."""
    start: str
    end: str

    def start_dt(self) -> datetime:
        return datetime.fromisoformat(self.start)

    def end_dt(self) -> datetime:
        return datetime.fromisoformat(self.end)

    def overlaps(self, other: "Slot") -> bool:
        return self.start_dt() < other.end_dt() and other.start_dt() < self.end_dt()

    def key(self) -> str:
        return f"{self.start}/{self.end}"


@dataclass
class Hold:
    slot: Slot
    thread_id: str
    person_id: str
    expires: str
    created: str = field(default_factory=now_iso)
    event_id: Optional[str] = None


@dataclass
class OwnerProfile:
    name: str
    home_zone: str = "America/Los_Angeles"
    agent_address: str = ""
    owner_addresses: list[str] = field(default_factory=list)
    write_calendar: str = ""
    read_calendars: list[str] = field(default_factory=list)   # free/busy only
    groups: dict[str, Group] = field(default_factory=dict)
    default_group: str = "default"
    allowlist: dict[str, int] = field(default_factory=dict)   # email -> tier
    auto_book: bool = True          # David's pick; a profile setting, off for approve-first
    max_len_without_owner_min: int = 59   # 60 minutes or more goes to the owner
    notice_hours: int = 24
    max_hold_days_per_offer: int = 3
    max_open_holds: int = 12
    hold_expiry_hours: int = 48
    max_offer_rounds: int = 2
    base_address: str = ""          # where the owner starts from, for travel buffers; never sent anywhere
    default_travel_min: int = 30    # travel each way to an in-person event with no entry in a distance table
    reminders: "Reminders" = field(default_factory=lambda: Reminders())

    def group_for(self, person: Optional[Person]) -> Group:
        """The person's own group, else the group whose members or domains match their address, else the default."""
        if person and person.group_id and person.group_id in self.groups:
            return self.groups[person.group_id]
        for g in self.groups.values():
            if person and any(g.matches(e) for e in person.emails):
                return g
        if self.default_group in self.groups:
            return self.groups[self.default_group]
        return default_group()

    def tier_for(self, authority: str, sender: str = "", group: Optional[Group] = None) -> int:
        """Owner: Manage. An allowlisted address: the tier set for it. Introduced: Book with auto-book on,
        Offer with it off, never above the group's tier (a group in Trial keeps introduced senders in Trial).
        Unknown: tier 0, and the unknown-sender rule still stops any reply without the owner."""
        if authority == Authority.OWNER.value:
            return AuthorityTier.MANAGE
        if sender and sender.lower() in self.allowlist:
            return int(self.allowlist[sender.lower()])
        if authority == Authority.INTRODUCED.value:
            base = AuthorityTier.BOOK if self.auto_book else AuthorityTier.OFFER
            return min(base, int(group.tier)) if group is not None else base
        if authority == Authority.ALLOWLISTED.value:
            return AuthorityTier.OFFER
        return AuthorityTier.TRIAL


@dataclass
class ThreadState:
    thread_id: str
    status: str = ThreadStatus.OFFERING.value
    person_id: Optional[str] = None
    invite_owner: str = InviteOwner.UNKNOWN.value
    offer_rounds: int = 0
    constraints: list[Constraint] = field(default_factory=list)
    history: list[list[str]] = field(default_factory=list)   # [at, status, note]
    updated: str = field(default_factory=now_iso)
    handled: list[str] = field(default_factory=list)          # message ids already acted on
    length_min: Optional[int] = None                         # the length on offer, once offered
    introduced: bool = False                                 # the owner vouched for this thread's sender after the fact

    def move(self, status: str, note: str = "") -> None:
        self.status = status
        self.updated = now_iso()
        self.history.append([self.updated, status, note])


def default_group() -> Group:
    """The fallback when a profile has no groups: weekdays 10:00 to 16:00 in the owner's home zone."""
    return Group(
        id="default", name="Default",
        windows={d: [Window("10:00", "16:00")] for d in WEEKDAYS[:5]},
    )


def default_groups() -> dict[str, Group]:
    """A new owner's two groups, both in Trial until setup is finished.

    Work: weekdays 9 AM to 5 PM in the owner's zone, busy-only. Family: full availability,
    every day 7 AM to 9 PM. Travel buffers off for both.
    """
    work = Group(id="work", name="Work", windows={d: [Window("09:00", "17:00")] for d in WEEKDAYS[:5]},
                 buffer_min=15, default_len_min=30, earliest_days_out=1, horizon_days=14, tier=AuthorityTier.TRIAL,
                 visibility="busy_only")
    family = Group(id="family", name="Family", windows={d: [Window("07:00", "21:00")] for d in WEEKDAYS},
                   buffer_min=0, default_len_min=30, earliest_days_out=0, horizon_days=14, tier=AuthorityTier.TRIAL,
                   visibility="full")
    return {"work": work, "family": family}


@dataclass
class Reminders:
    """What happens around every event the agent books. On by default.

    The other person never gets a reminder from the agent: every text and briefing goes to the owner.
    """
    on: bool = True
    popup_min: int = 10              # a pop-up on the event itself
    email_min: int = 1440            # an email from the calendar, one day before
    text_before_min: int = 60        # a text to the owner before the meeting
    channel: str = "email"           # telegram | sms | imessage | email
    target: str = ""                 # the owner's own chat, number or address; blank = that channel's home
    briefing: bool = True            # a day-before briefing to the owner
    briefing_at: str = "17:00"       # the evening before, in the owner's zone


# ---- JSON (de)serialization -------------------------------------------------

def to_dict(obj: Any) -> Any:
    if is_dataclass(obj):
        return {k: to_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_dict(v) for v in obj]
    return obj


def to_json(obj: Any, **kw) -> str:
    return json.dumps(to_dict(obj), indent=kw.pop("indent", 1), sort_keys=True, **kw)


_NESTED = {
    Hold: {"slot": Slot},
    Group: {"windows": ("dict_of_lists", Window)},
    OwnerProfile: {"groups": ("dict", Group), "reminders": Reminders},
    ThreadState: {"constraints": ("list", Constraint)},
}


def from_dict(cls, data: dict):
    """Rebuild a dataclass from a dict, including the nested types above."""
    kwargs = {}
    names = {f.name for f in fields(cls)}
    spec = _NESTED.get(cls, {})
    for k, v in data.items():
        if k not in names:
            continue
        rule = spec.get(k)
        if rule is None:
            kwargs[k] = v
        elif isinstance(rule, type):
            kwargs[k] = from_dict(rule, v) if v is not None else None
        else:
            shape, sub = rule
            if shape == "list":
                kwargs[k] = [from_dict(sub, x) for x in v]
            elif shape == "dict":
                kwargs[k] = {kk: from_dict(sub, vv) for kk, vv in v.items()}
            elif shape == "dict_of_lists":
                kwargs[k] = {kk: [from_dict(sub, x) for x in vv] for kk, vv in v.items()}
    return cls(**kwargs)


def from_json(cls, text: str):
    return from_dict(cls, json.loads(text))
