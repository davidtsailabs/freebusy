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
    OBSERVE = 0   # read, draft for the owner, send nothing
    OFFER = 1     # reply with held slots, ask the zone, hold
    BOOK = 2      # book on a fitting stated time, clear holds, notify
    MANAGE = 3    # also reschedule and cancel (out of scope for 0.1)


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
    tier: int = AuthorityTier.OBSERVE


@dataclass
class Window:
    """A working window on one weekday, in the owner's home zone. "HH:MM" strings."""
    start: str
    end: str


@dataclass
class Group:
    """A class of people the owner treats alike (clients, sponsors, friends)."""
    id: str
    name: str
    windows: dict[str, list[Window]] = field(default_factory=dict)  # weekday -> windows
    buffer_min: int = 30
    default_len_min: int = 30
    earliest_days_out: int = 3
    horizon_days: int = 14
    tier: int = AuthorityTier.OFFER
    visibility: str = "busy_only"


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

    def group_for(self, person: Optional[Person]) -> Group:
        gid = (person.group_id if person and person.group_id else None) or self.default_group
        if gid in self.groups:
            return self.groups[gid]
        return default_group()

    def tier_for(self, authority: str, sender: str = "") -> int:
        if authority == Authority.OWNER.value:
            return AuthorityTier.MANAGE
        if sender and sender.lower() in self.allowlist:
            return int(self.allowlist[sender.lower()])
        if authority == Authority.INTRODUCED.value:
            return AuthorityTier.BOOK if self.auto_book else AuthorityTier.OFFER
        if authority == Authority.ALLOWLISTED.value:
            return AuthorityTier.OFFER
        return AuthorityTier.OBSERVE


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

    def move(self, status: str, note: str = "") -> None:
        self.status = status
        self.updated = now_iso()
        self.history.append([self.updated, status, note])


def default_group() -> Group:
    """Sensible defaults: weekdays 10:00 to 16:00 in the owner's home zone."""
    return Group(
        id="default", name="Default",
        windows={d: [Window("10:00", "16:00")] for d in WEEKDAYS[:5]},
    )


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
    OwnerProfile: {"groups": ("dict", Group)},
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
