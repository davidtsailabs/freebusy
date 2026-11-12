"""Just enough iCalendar (RFC 5545) for busy times, holds and booked events. Standard library only.

Reading keeps start, end and whether the event blocks time. Summaries,
descriptions, attendees and locations are never returned from here: the
reducers below drop every other property before anything leaves the adapter.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..ontology import Slot

_DUR = re.compile(r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")
PRODID = "-//freebusy//Hermes scheduling plugin 0.3//EN"
HOLD_MARK = "X-FREEBUSY-HOLD"            # on every hold this plugin writes; with the UID prefix, how it knows its own
HOLD_UID_PREFIX = "freebusy-hold-"


def is_own_hold(props: dict) -> bool:
    return HOLD_MARK in props or props.get("UID", ({}, ""))[1].strip().startswith(HOLD_UID_PREFIX)


def unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def _split_prop(line: str) -> tuple[str, dict[str, str], str]:
    # NAME;PARAM=a;PARAM2="b:c":value  (a colon inside a quoted parameter is not the separator)
    inq, cut = False, -1
    for i, ch in enumerate(line):
        if ch == '"':
            inq = not inq
        elif ch == ":" and not inq:
            cut = i
            break
    head, value = (line, "") if cut < 0 else (line[:cut], line[cut + 1:])
    parts = head.split(";")
    params = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def components(text: str, kind: str) -> list[list[tuple[str, dict[str, str], str]]]:
    """The properties of each ``kind`` component (VEVENT, VFREEBUSY), nested components left out."""
    out, cur, depth = [], None, 0
    for line in unfold(text):
        name, params, value = _split_prop(line)
        if name == "BEGIN":
            if value.upper() == kind and cur is None:
                cur, depth = [], 0
            elif cur is not None:
                depth += 1
            continue
        if name == "END":
            if cur is not None and depth == 0 and value.upper() == kind:
                out.append(cur)
                cur = None
            elif cur is not None:
                depth -= 1
            continue
        if cur is not None and depth == 0:
            cur.append((name, params, value))
    return out


def _zone(tzid: Optional[str], default: str):
    if not tzid:
        return ZoneInfo(default)
    tzid = tzid.strip().lstrip("/")
    try:
        return ZoneInfo(tzid)
    except (ZoneInfoNotFoundError, ValueError):
        # Some servers prefix ids (e.g. "/mozilla.org/20050126_1/Europe/Zurich"); try the last two parts.
        tail = "/".join(tzid.split("/")[-2:])
        try:
            return ZoneInfo(tail)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo(default)


def parse_dt(value: str, params: dict[str, str], default_zone: str) -> Optional[datetime]:
    """An aware datetime, or None for a date-only value (all-day)."""
    v = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", v):
        return None
    if v.endswith("Z"):
        return datetime.strptime(v[:-1], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    return datetime.strptime(v, "%Y%m%dT%H%M%S").replace(tzinfo=_zone(params.get("TZID"), default_zone))


def parse_date(value: str) -> Optional[date]:
    v = value.strip()
    return datetime.strptime(v[:8], "%Y%m%d").date() if re.fullmatch(r"\d{8}", v) else None


def parse_duration(value: str) -> timedelta:
    m = _DUR.match(value.strip())
    if not m:
        raise ValueError(f"bad duration {value!r}")
    sign, w, d, h, mi, s = m.groups()
    td = timedelta(weeks=int(w or 0), days=int(d or 0), hours=int(h or 0), minutes=int(mi or 0), seconds=int(s or 0))
    return -td if sign == "-" else td


def utc_stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _slot(a: datetime, b: datetime, zone: str) -> Slot:
    z = ZoneInfo(zone)
    return Slot(a.astimezone(z).isoformat(), b.astimezone(z).isoformat())


def _overlaps(a: datetime, b: datetime, start: datetime, end: datetime) -> bool:
    return a < end and start < b


def busy_from_events(text: str, start: datetime, end: datetime, zone: str, skip_own_holds: bool = True) -> list[Slot]:
    """Each blocking VEVENT reduced to start and end. Cancelled, transparent and all-day events are dropped, and so are
    this plugin's own holds (marked, see ``vevent``): they block the slot for everyone else, not for freebusy itself.

    Recurring events are expected already expanded by the server (calendar-query with <C:expand>); a
    master that still carries an RRULE counts for its own first instance only.
    """
    out = []
    for props in components(text, "VEVENT"):
        get = {n: (p, v) for n, p, v in props}
        if get.get("STATUS", ({}, ""))[1].strip().upper() == "CANCELLED":
            continue
        if skip_own_holds and is_own_hold(get):
            continue
        if get.get("TRANSP", ({}, "OPAQUE"))[1].strip().upper() == "TRANSPARENT":
            continue
        if "DTSTART" not in get:
            continue
        a = parse_dt(get["DTSTART"][1], get["DTSTART"][0], zone)
        if a is None:
            continue                                   # all-day: not a busy block, as in the Google reducer
        if "DTEND" in get:
            b = parse_dt(get["DTEND"][1], get["DTEND"][0], zone)
        elif "DURATION" in get:
            b = a + parse_duration(get["DURATION"][1])
        else:
            b = a
        if b is None or b <= a or not _overlaps(a, b, start, end):
            continue
        out.append(_slot(a, b, zone))
    return sorted(out, key=lambda s: s.start)


def busy_from_freebusy(text: str, start: datetime, end: datetime, zone: str) -> list[Slot]:
    """FREEBUSY periods of a VFREEBUSY reply. FBTYPE=FREE is dropped; BUSY, BUSY-UNAVAILABLE and BUSY-TENTATIVE count."""
    out = []
    for props in components(text, "VFREEBUSY"):
        for name, params, value in props:
            if name != "FREEBUSY" or params.get("FBTYPE", "BUSY").upper() == "FREE":
                continue
            for period in value.split(","):
                a_txt, _, b_txt = period.strip().partition("/")
                if not b_txt:
                    continue
                a = parse_dt(a_txt, {}, zone)
                b = (a + parse_duration(b_txt)) if b_txt.upper().startswith(("P", "+P", "-P")) else parse_dt(b_txt, {}, zone)
                if a and b and b > a and _overlaps(a, b, start, end):
                    out.append(_slot(a, b, zone))
    return sorted(out, key=lambda s: s.start)


def is_freebusy_reply(text: str) -> bool:
    return "BEGIN:VFREEBUSY" in text.upper()


def escape(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n")


def fold(line: str) -> str:
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode("utf-8")) > 74:
            out.append(cur)
            cur = " " + ch
        else:
            cur += ch
    out.append(cur)
    return "\r\n".join(out)


def vevent(uid: str, slot: Slot, summary: str, description: str = "", url: str = "", tentative: bool = False,
           organizer: str = "", attendees: Iterable[str] = (), alarms_min: Iterable[int] = (), stamp: Optional[datetime] = None) -> list[str]:
    """One VEVENT. A hold is TENTATIVE and OPAQUE: it blocks the slot for every other reader (a colleague's free/busy
    lookup, another booking tool), so a held slot is really held. It carries ``X-FREEBUSY-HOLD`` and a ``freebusy-hold-``
    UID so freebusy's own reads leave it out; the ledger already keeps a held slot from being offered twice."""
    stamp = stamp or datetime.now(timezone.utc)
    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{utc_stamp(stamp)}",
             f"DTSTART:{utc_stamp(slot.start_dt())}", f"DTEND:{utc_stamp(slot.end_dt())}",
             f"SUMMARY:{escape(summary)}",
             f"STATUS:{'TENTATIVE' if tentative else 'CONFIRMED'}",
             "TRANSP:OPAQUE"]
    if tentative:
        lines.append(f"{HOLD_MARK}:1")
    if description:
        lines.append(f"DESCRIPTION:{escape(description)}")
    if url:
        lines += [f"URL:{url}", f"LOCATION:{escape(url)}"]
    if organizer and not tentative:
        lines.append(f"ORGANIZER:mailto:{organizer}")
        for a in attendees:
            lines.append(f"ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{a}")
    for m in alarms_min:
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{escape(summary)}", f"TRIGGER:-PT{int(m)}M", "END:VALARM"]
    lines.append("END:VEVENT")
    return lines


def calendar(events: list[list[str]], name: str = "") -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN"]
    if name:
        lines.append(f"X-WR-CALNAME:{escape(name)}")
    for ev in events:
        lines += ev
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


def subtract(busy: list[Slot], remove: list[Slot]) -> list[Slot]:
    """Busy periods minus the given intervals (for a free-busy reply, which cannot say which period is our own hold)."""
    out = []
    for b in busy:
        pieces = [(b.start_dt(), b.end_dt())]
        for r in remove:
            ra, rb = r.start_dt(), r.end_dt()
            nxt = []
            for a, e in pieces:
                if rb <= a or ra >= e:
                    nxt.append((a, e))
                    continue
                if a < ra:
                    nxt.append((a, ra))
                if rb < e:
                    nxt.append((rb, e))
            pieces = nxt
        tz = b.start_dt().tzinfo
        out += [Slot(a.astimezone(tz).isoformat(), e.astimezone(tz).isoformat()) for a, e in pieces]
    return out
