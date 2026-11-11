"""Groups in and out of config, and the one-setting-at-a-time edits behind /schedule group.

Config shape (what ``config_schema`` stores and Hermes Desktop shows as JSON):

    {"Work": {"windows": {"mon": ["09:00-17:00"], ...}, "buffer_min": 15, "default_len_min": 30,
              "earliest_days_out": 1, "horizon_days": 14, "tier": 0, "visibility": "busy_only",
              "members": [], "domains": [], "travel_buffers": false}, "Family": {...}}

Windows can also be written in short form for a run of days: "mon-fri 09:00-17:00",
"every day 07:00-21:00", "sat,sun 10:00-14:00", several joined with ";".
"""

from __future__ import annotations

import re
from typing import Any

from .ontology import TIER_NAMES, WEEKDAYS, Group, Window, default_groups

SETTINGS = ("windows", "buffer", "length", "earliest", "horizon", "tier", "visibility", "members", "domains", "travel")
_FIELD = {"buffer": "buffer_min", "length": "default_len_min", "earliest": "earliest_days_out", "horizon": "horizon_days"}
_TIER_WORDS = {"trial": 0, "offer": 1, "book": 2, "manage": 3}


def _window_list(spec: Any) -> list[Window]:
    if isinstance(spec, str):
        spec = [x for x in re.split(r"[,\s]+", spec) if x]
    out = []
    for w in spec or []:
        if isinstance(w, dict):
            out.append(Window(w["start"], w["end"]))
        else:
            a, b = str(w).split("-")
            out.append(Window(_hhmm(a), _hhmm(b)))
    return out


def _hhmm(t: str) -> str:
    t = t.strip().lower()
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    if not m:
        raise ValueError(f"not a clock time: {t}")
    h, mm, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if h > 24 or mm > 59:
        raise ValueError(f"not a clock time: {t}")
    return f"{h:02d}:{mm:02d}"


def _days(spec: str) -> list[str]:
    spec = spec.strip().lower()
    if spec in ("every day", "everyday", "daily", "all"):
        return list(WEEKDAYS)
    if spec in ("weekdays", "workdays"):
        return list(WEEKDAYS[:5])
    if spec in ("weekends", "weekend"):
        return list(WEEKDAYS[5:])
    out: list[str] = []
    for part in spec.split(","):
        part = part.strip()[:3] if "-" not in part else part.strip()
        if "-" in part:
            a, b = (x.strip()[:3] for x in part.split("-"))
            i, j = WEEKDAYS.index(a), WEEKDAYS.index(b)
            out += list(WEEKDAYS[i:j + 1]) if i <= j else list(WEEKDAYS[i:]) + list(WEEKDAYS[:j + 1])
        elif part in WEEKDAYS:
            out.append(part)
        else:
            raise ValueError(f"not a day: {part}")
    return out


_SHORT = re.compile(r"^\s*(every day|everyday|daily|weekdays|weekends?|workdays|[a-z,\- ]+?)\s+"
                    r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*-\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*$", re.IGNORECASE)


def parse_windows(text: str) -> dict[str, list[Window]]:
    """"mon-fri 09:00-17:00; sat 10:00-12:00" -> {"mon": [Window], ...}. Days not named have no window."""
    out: dict[str, list[Window]] = {}
    for chunk in [c for c in re.split(r"[;\n]", text or "") if c.strip()]:
        m = _SHORT.match(chunk)
        if not m:
            raise ValueError(f"windows look like \"mon-fri 09:00-17:00\" or \"every day 7am-9pm\", not \"{chunk.strip()}\"")
        a, b = (x.strip() for x in m.group(2).split("-"))
        for d in _days(m.group(1)):
            out.setdefault(d, []).append(Window(_hhmm(a), _hhmm(b)))
    if not out:
        raise ValueError("no windows given")
    return out


def describe_windows(windows: dict[str, list[Window]]) -> str:
    """Group runs of days with the same windows: "mon-fri 09:00-17:00"."""
    keyed = [(d, ",".join(f"{w.start}-{w.end}" for w in windows.get(d, []))) for d in WEEKDAYS]
    parts, i = [], 0
    while i < len(keyed):
        j = i
        while j + 1 < len(keyed) and keyed[j + 1][1] == keyed[i][1]:
            j += 1
        if keyed[i][1]:
            days = "every day" if (i, j) == (0, 6) else keyed[i][0] if i == j else f"{keyed[i][0]}-{keyed[j][0]}"
            parts.append(f"{days} {keyed[i][1]}")
        i = j + 1
    return "; ".join(parts) or "none"


def group_from_config(name: str, cfg: dict) -> Group:
    gid = name.strip().lower()
    base = default_groups().get(gid) or Group(id=gid, name=name)
    windows = cfg.get("windows")
    if isinstance(windows, str):
        base.windows = parse_windows(windows)
    elif isinstance(windows, dict):
        base.windows = {d.lower()[:3]: _window_list(v) for d, v in windows.items() if _window_list(v)}
    for key in ("buffer_min", "default_len_min", "earliest_days_out", "horizon_days"):
        if key in cfg:
            setattr(base, key, int(cfg[key]))
    if "tier" in cfg:
        base.tier = parse_tier(cfg["tier"])
    if cfg.get("visibility") in ("busy_only", "full"):
        base.visibility = cfg["visibility"]
    base.members = [str(x).lower() for x in cfg.get("members", base.members)]
    base.domains = [str(x).lower().lstrip("@") for x in cfg.get("domains", base.domains)]
    base.travel_buffers = bool(cfg.get("travel_buffers", base.travel_buffers))
    base.id, base.name = gid, name
    return base


def groups_from_config(cfg: dict | None) -> dict[str, Group]:
    """An empty config means the two defaults, Work and Family."""
    if not cfg:
        return default_groups()
    return {g.id: g for g in (group_from_config(n, c or {}) for n, c in cfg.items())}


def group_to_config(g: Group) -> dict:
    return {"windows": {d: [f"{w.start}-{w.end}" for w in ws] for d, ws in g.windows.items() if ws},
            "buffer_min": g.buffer_min, "default_len_min": g.default_len_min, "earliest_days_out": g.earliest_days_out,
            "horizon_days": g.horizon_days, "tier": int(g.tier), "visibility": g.visibility, "members": list(g.members),
            "domains": list(g.domains), "travel_buffers": g.travel_buffers}


def groups_to_config(groups: dict[str, Group]) -> dict:
    return {g.name: group_to_config(g) for g in groups.values()}


def parse_tier(value: Any) -> int:
    v = str(value).strip().lower()
    if v in _TIER_WORDS:
        return _TIER_WORDS[v]
    n = int(v)
    if n not in TIER_NAMES:
        raise ValueError("tier is 0 trial, 1 offer, 2 book or 3 manage")
    return n


def _onoff(value: str) -> bool:
    v = value.strip().lower()
    if v in ("on", "yes", "true", "1"):
        return True
    if v in ("off", "no", "false", "0"):
        return False
    raise ValueError("say on or off")


def find_group(groups: dict[str, Group], name: str) -> Group | None:
    return groups.get(name.strip().lower()) or next((g for g in groups.values() if g.name.lower() == name.strip().lower()), None)


def set_setting(groups: dict[str, Group], name: str, setting: str, value: str) -> str:
    """Apply one /schedule group <name> <setting> <value> edit in place; return a plain confirmation.

    A name that does not exist yet creates a new group with Work's rules. Raises ValueError with a usable message.
    """
    g = find_group(groups, name)
    if g is None:
        g = group_from_config(name, group_to_config(default_groups()["work"]))
        groups[g.id] = g
    s = setting.strip().lower()
    if s not in SETTINGS:
        raise ValueError(f"settings are: {', '.join(SETTINGS)}")
    if s == "windows":
        g.windows = parse_windows(value)
    elif s in _FIELD:
        n = int(re.sub(r"[^\d]", "", value) or "x")
        setattr(g, _FIELD[s], n)
    elif s == "tier":
        g.tier = parse_tier(value)
    elif s == "visibility":
        v = value.strip().lower().replace("-", "_")
        if v not in ("busy_only", "full"):
            raise ValueError("visibility is busy_only or full")
        g.visibility = v
    elif s in ("members", "domains"):
        items = [x.strip().lower().lstrip("@") if s == "domains" else x.strip().lower() for x in re.split(r"[,\s]+", value) if x.strip()]
        if items and items[0] in ("+", "add"):
            setattr(g, s, sorted(set(getattr(g, s)) | set(items[1:])))
        elif items and items[0] in ("-", "remove"):
            setattr(g, s, [x for x in getattr(g, s) if x not in items[1:]])
        else:
            setattr(g, s, [] if items == ["none"] else items)
    elif s == "travel":
        g.travel_buffers = _onoff(value)
    return f"{g.name}: {s} is now {describe_setting(g, s)}"


def describe_setting(g: Group, s: str) -> str:
    return {"windows": lambda: describe_windows(g.windows), "buffer": lambda: f"{g.buffer_min} minutes",
            "length": lambda: f"{g.default_len_min} minutes", "earliest": lambda: f"{g.earliest_days_out} days out",
            "horizon": lambda: f"{g.horizon_days} days", "tier": lambda: f"{int(g.tier)} {TIER_NAMES[int(g.tier)]}",
            "visibility": lambda: g.visibility, "members": lambda: ", ".join(g.members) or "none",
            "domains": lambda: ", ".join(g.domains) or "none", "travel": lambda: "on" if g.travel_buffers else "off"}[s]()


def describe(groups: dict[str, Group], default: str = "") -> str:
    lines = []
    for g in groups.values():
        mark = " (default)" if g.id == default else ""
        lines.append(f"{g.name}{mark}: {describe_windows(g.windows)}; {g.visibility}; buffer {g.buffer_min} min; "
                     f"length {g.default_len_min} min; earliest {g.earliest_days_out} days out; horizon {g.horizon_days} days; "
                     f"tier {int(g.tier)} {TIER_NAMES[int(g.tier)]}; travel buffers {'on' if g.travel_buffers else 'off'}; "
                     f"members {', '.join(g.members) or 'none'}; domains {', '.join(g.domains) or 'none'}")
    return "\n".join(lines) or "no groups"
