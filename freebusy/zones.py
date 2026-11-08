"""Time zone rules, universal layer.

The rule: never emit a time in a zone the person did not state. A zone is
"stated" only when it comes from words the person wrote (an abbreviation, a
city, a UTC offset in the text). Mail header offsets and domains produce a
hint at most, and a hint is shown as an assumption to confirm, never as fact.
Convert once, when told.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

# Abbreviation -> (IANA zone, short label shown to people)
ABBREVIATIONS = {
    "pt": ("America/Los_Angeles", "PT"), "pst": ("America/Los_Angeles", "PT"), "pdt": ("America/Los_Angeles", "PT"),
    "pacific": ("America/Los_Angeles", "PT"),
    "mt": ("America/Denver", "MT"), "mst": ("America/Denver", "MT"), "mdt": ("America/Denver", "MT"),
    "mountain": ("America/Denver", "MT"),
    "ct": ("America/Chicago", "CT"), "cst": ("America/Chicago", "CT"), "cdt": ("America/Chicago", "CT"),
    "central": ("America/Chicago", "CT"),
    "et": ("America/New_York", "ET"), "est": ("America/New_York", "ET"), "edt": ("America/New_York", "ET"),
    "eastern": ("America/New_York", "ET"),
    "gmt": ("Europe/London", "UK"), "bst": ("Europe/London", "UK"), "uk time": ("Europe/London", "UK"),
    "utc": ("UTC", "UTC"),
    "cet": ("Europe/Berlin", "CET"), "cest": ("Europe/Berlin", "CET"),
    "central european": ("Europe/Berlin", "CET"),
    "eet": ("Europe/Athens", "EET"),
    "ist": ("Asia/Kolkata", "IST"),
    "jst": ("Asia/Tokyo", "JST"),
    "hkt": ("Asia/Hong_Kong", "HKT"),
    "sgt": ("Asia/Singapore", "SGT"),
    "aest": ("Australia/Sydney", "AET"), "aedt": ("Australia/Sydney", "AET"),
}

# City or region -> IANA zone. Small on purpose; unknown cities are not guessed.
CITIES = {
    "san francisco": "America/Los_Angeles", "los angeles": "America/Los_Angeles", "seattle": "America/Los_Angeles",
    "bay area": "America/Los_Angeles", "san diego": "America/Los_Angeles", "portland": "America/Los_Angeles",
    "denver": "America/Denver", "salt lake": "America/Denver", "phoenix": "America/Phoenix",
    "chicago": "America/Chicago", "austin": "America/Chicago", "dallas": "America/Chicago", "houston": "America/Chicago",
    "minneapolis": "America/Chicago",
    "new york": "America/New_York", "boston": "America/New_York", "washington": "America/New_York",
    "philadelphia": "America/New_York", "atlanta": "America/New_York", "miami": "America/New_York",
    "toronto": "America/Toronto", "montreal": "America/Toronto",
    "london": "Europe/London", "dublin": "Europe/Dublin",
    "paris": "Europe/Paris", "berlin": "Europe/Berlin", "munich": "Europe/Berlin", "zurich": "Europe/Zurich",
    "basel": "Europe/Zurich", "geneva": "Europe/Zurich", "amsterdam": "Europe/Amsterdam", "brussels": "Europe/Brussels",
    "madrid": "Europe/Madrid", "milan": "Europe/Rome", "rome": "Europe/Rome", "copenhagen": "Europe/Copenhagen",
    "stockholm": "Europe/Stockholm", "oslo": "Europe/Oslo", "vienna": "Europe/Vienna",
    "tel aviv": "Asia/Jerusalem", "dubai": "Asia/Dubai", "mumbai": "Asia/Kolkata", "bangalore": "Asia/Kolkata",
    "singapore": "Asia/Singapore", "hong kong": "Asia/Hong_Kong", "tokyo": "Asia/Tokyo", "seoul": "Asia/Seoul",
    "shanghai": "Asia/Shanghai", "beijing": "Asia/Shanghai", "sydney": "Australia/Sydney", "melbourne": "Australia/Melbourne",
}

LABELS = {
    "America/Los_Angeles": "PT", "America/Denver": "MT", "America/Phoenix": "MT", "America/Chicago": "CT",
    "America/New_York": "ET", "America/Toronto": "ET", "Europe/London": "UK", "Europe/Dublin": "Ireland",
    "Europe/Berlin": "CET", "Europe/Paris": "CET", "Europe/Zurich": "CET", "Europe/Amsterdam": "CET",
    "Europe/Brussels": "CET", "Europe/Madrid": "CET", "Europe/Rome": "CET", "Europe/Copenhagen": "CET",
    "Europe/Stockholm": "CET", "Europe/Oslo": "CET", "Europe/Vienna": "CET", "Europe/Athens": "EET",
    "Asia/Kolkata": "IST", "Asia/Tokyo": "JST", "Asia/Hong_Kong": "HKT", "Asia/Singapore": "SGT",
    "Australia/Sydney": "AET", "Australia/Melbourne": "AET", "UTC": "UTC",
}

# Representative zones per UTC offset, used only to word a hint.
_OFFSET_HINTS = {
    -8: "Pacific time", -7: "Mountain or Pacific time", -6: "Central time", -5: "Eastern or Central time",
    -4: "Eastern time", 0: "UK time", 1: "Central European time", 2: "Central or Eastern European time",
    3: "Eastern European time", 5.5: "India time", 8: "Singapore or Hong Kong time", 9: "Japan time",
    10: "Eastern Australia time", 11: "Eastern Australia time",
}


@dataclass
class ZoneGuess:
    zone: str            # IANA
    label: str           # what we show: PT, ET, CET
    confidence: float    # 0..1
    source: str          # stated | inferred
    evidence: str        # the words that produced it


def label_for(zone: str) -> str:
    if zone in LABELS:
        return LABELS[zone]
    return zone.split("/")[-1].replace("_", " ")


_ABBR_RE = re.compile(
    r"(?<![A-Za-z])(" + "|".join(sorted((re.escape(k) for k in ABBREVIATIONS), key=len, reverse=True)) + r")(?![A-Za-z])",
    re.IGNORECASE,
)
_OFFSET_RE = re.compile(r"\b(?:UTC|GMT)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?\b", re.IGNORECASE)
_CITY_RE = re.compile(
    r"\b(?:in|from|based in|here in|located in|i'm in|i am in|we're in|we are in)\s+(" +
    "|".join(sorted((re.escape(k) for k in CITIES), key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
_BARE_CITY_RE = re.compile(
    r"\b(" + "|".join(sorted((re.escape(k) for k in CITIES), key=len, reverse=True)) + r")\b", re.IGNORECASE,
)


def detect_zone(text: str, signature: str = "") -> Optional[ZoneGuess]:
    """Find a zone the person stated in their own words.

    Order: an abbreviation anywhere ("2:30 CET", "my time (ET)") > a UTC offset
    in the text > a city the person places themselves in ("I'm in Zurich").
    A city that appears only in the signature block is inferred, not stated,
    and comes back with low confidence so the caller shows it as an assumption.
    "my time" or "our time" with no zone is not a zone.
    """
    body = text or ""
    m = _OFFSET_RE.search(body)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        hours = int(m.group(2)) + (int(m.group(3) or 0) / 60)
        name = f"UTC{m.group(1)}{int(hours):02d}:{int(round((hours % 1) * 60)):02d}"
        return ZoneGuess(name, name, 0.9, "stated", m.group(0))
    m = _ABBR_RE.search(body)
    if m:
        zone, label = ABBREVIATIONS[m.group(1).lower()]
        return ZoneGuess(zone, label, 0.95, "stated", m.group(0))
    m = _CITY_RE.search(body)
    if m:
        zone = CITIES[m.group(1).lower()]
        return ZoneGuess(zone, label_for(zone), 0.8, "stated", m.group(0))
    m = _BARE_CITY_RE.search(signature or "")
    if m:
        zone = CITIES[m.group(1).lower()]
        return ZoneGuess(zone, label_for(zone), 0.5, "inferred", f"signature: {m.group(0)}")
    return None


def zone_hint_from_header(date_header: str) -> Optional[str]:
    """Turn a Date header offset into a hint to be worded as an assumption.

    This never returns a zone, only words like "Central European time", because
    an offset is evidence about a mail server, not a statement by a person.
    """
    m = re.search(r"([+-])(\d{2})(\d{2})\s*$", (date_header or "").strip())
    if not m:
        return None
    sign = 1 if m.group(1) == "+" else -1
    offset = sign * (int(m.group(2)) + int(m.group(3)) / 60)
    return _OFFSET_HINTS.get(offset)


def zone_from_iana_or_abbr(value: str) -> Optional[tuple[str, str]]:
    """Accept "CET", "Eastern" or "Europe/Zurich"; return (iana, label)."""
    if not value:
        return None
    v = value.strip()
    if v.lower() in ABBREVIATIONS:
        return ABBREVIATIONS[v.lower()]
    if v.lower() in CITIES:
        z = CITIES[v.lower()]
        return z, label_for(z)
    try:
        ZoneInfo(v)
        return v, label_for(v)
    except Exception:
        return None


def to_zone(dt: datetime, zone: str) -> datetime:
    if zone.upper().startswith("UTC") and len(zone) > 3:
        sign = 1 if zone[3] == "+" else -1
        hh, mm = zone[4:].split(":")
        return dt.astimezone(timezone(timedelta(hours=sign * int(hh), minutes=sign * int(mm))))
    return dt.astimezone(ZoneInfo(zone))


def _clock(dt: datetime, with_suffix: bool) -> str:
    h = dt.hour % 12 or 12
    s = f"{h}:{dt.minute:02d}"
    return f"{s} {'am' if dt.hour < 12 else 'pm'}" if with_suffix else s


def format_range(start: datetime, end: datetime, zone: str) -> str:
    """"10:00 to 11:30 am PT". One am/pm when both sides share it."""
    s, e = to_zone(start, zone), to_zone(end, zone)
    same = (s.hour < 12) == (e.hour < 12)
    return f"{_clock(s, not same)} to {_clock(e, True)} {label_for(zone)}"


def format_day(dt: datetime, zone: str) -> str:
    d = to_zone(dt, zone)
    return d.strftime("%a %b ") + str(d.day)


def format_slot_for(start: datetime, end: datetime, owner_zone: str, person_zone: Optional[str]) -> str:
    """Owner's zone always; the person's zone after a slash only when stated.

    Passing a zone that was never stated is the caller's error; this function is
    the last line, so it also strips any second zone when ``person_zone`` is None.
    """
    text = format_range(start, end, owner_zone)
    if person_zone and person_zone != owner_zone:
        text += " / " + format_range(start, end, person_zone)
    return text


def zone_line(owner_zone: str, person_zone: Optional[str], hint: Optional[str]) -> str:
    """The footnote under an offer."""
    owner_name = {"PT": "Pacific", "ET": "Eastern", "CT": "Central", "MT": "Mountain"}.get(label_for(owner_zone), label_for(owner_zone))
    if person_zone and person_zone != owner_zone:
        return f"Times in {owner_name} and {label_for(person_zone)}."
    if hint:
        return (f"Times are {owner_name}; you look to be on {hint}, so reply with your zone "
                f"after the letter (for example \"B, Eastern\") if so.")
    return f"Times are {owner_name}; not your zone? Add it after the letter (for example \"B, Eastern\")."


def parse_pick_and_zone(text: str) -> tuple[Optional[str], Optional[ZoneGuess]]:
    """Read a reply like "B, CET", "Option A", "A, I'm in Chicago" or "the first one"."""
    t = (text or "").strip()
    pick = None
    m = re.match(r"^\s*(?:option\s+)?([A-Fa-f])\b", t, re.IGNORECASE)
    if m:
        pick = m.group(1).upper()
    else:
        words = {"first": "A", "second": "B", "third": "C", "fourth": "D", "fifth": "E", "sixth": "F"}
        m = re.search(r"\bthe\s+(first|second|third|fourth|fifth|sixth)\b", t, re.IGNORECASE)
        if m:
            pick = words[m.group(1).lower()]
    return pick, detect_zone(t)
