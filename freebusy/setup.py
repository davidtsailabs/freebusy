"""/schedule setup: one question per message, each answer saved to config at once.

The flow can be left and resumed (``/schedule setup`` asks the current question again),
skipped forward (``/schedule setup skip`` keeps the default), and redone
(``/schedule setup redo``). It never asks for a secret: mail and calendar credentials
are set by the owner in Hermes' own env and auth, and an answer that looks like a
password or a key is refused without being saved or repeated.

Until setup is finished every group is in Trial: the assistant offers held times and
every booking waits for the owner. Finishing setup moves groups still in Trial to Book
(auto-book on) or Offer (auto-book off).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .groups import describe_windows, find_group, groups_from_config, groups_to_config, parse_windows
from .ontology import TIER_NAMES, AuthorityTier
from .reminders import CHANNELS
from .zones import label_for, zone_from_iana_or_abbr

_SECRETISH = re.compile(r"(\bpassword\b|\bpasswd\b|\bapi[ _-]?key\b|\bsecret\b|\btoken\b|\bbearer\b|\boauth\b|\bsk-[A-Za-z0-9]{6,}|"
                        r"ya29\.|[A-Za-z0-9_\-]{32,})", re.IGNORECASE)
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


@dataclass
class Step:
    key: str
    ask: Callable[["SetupFlow"], str]
    save: Callable[["SetupFlow", str], str]       # applies the answer, returns a short confirmation; raises ValueError
    default: str = ""


def _onoff(text: str) -> bool:
    t = text.strip().lower()
    if t in ("on", "yes", "y", "true", "book", "book it"):
        return True
    if t in ("off", "no", "n", "false", "ask", "ask me", "ask me first"):
        return False
    raise ValueError("Reply on or off.")


class SetupFlow:
    def __init__(self, state_path: Path, get: Callable[[str, Any], Any], set_: Callable[[str, Any], None],
                 calendars: Optional[list[str]] = None, fixture: bool = False):
        self.path = Path(state_path)
        self.get, self.set = get, set_
        self.calendars = list(calendars or [])
        self.fixture = fixture
        self.state = {"step": 0, "done": False, "answered": []}
        if self.path.exists():
            try:
                self.state.update(json.loads(self.path.read_text()))
            except Exception:
                pass
        self.steps = [
            Step("name_zone", SetupFlow._ask_name, SetupFlow._save_name),
            Step("identity", SetupFlow._ask_identity, SetupFlow._save_identity),
            Step("calendar", SetupFlow._ask_calendar, SetupFlow._save_calendar, "1"),
            Step("work_windows", SetupFlow._ask_work, SetupFlow._save_work, "default"),
            Step("family_windows", SetupFlow._ask_family, SetupFlow._save_family, "default"),
            Step("length", SetupFlow._ask_length, SetupFlow._save_length, "30"),
            Step("auto_book", SetupFlow._ask_auto, SetupFlow._save_auto, "on"),
            Step("reminders", SetupFlow._ask_reminders, SetupFlow._save_reminders, "email"),
            Step("travel", SetupFlow._ask_travel, SetupFlow._save_travel, "none"),
        ]

    # -- driving --------------------------------------------------------------------
    @property
    def done(self) -> bool:
        return bool(self.state.get("done"))

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, indent=1))

    def _question(self) -> str:
        i = self.state["step"]
        step = self.steps[i]
        dflt = "" if not step.default else (", or /schedule setup skip for the default" +
                                            ("." if step.default == "default" else f" ({step.default})."))
        tail = f"\n\n(Question {i + 1} of {len(self.steps)}. Reply /schedule setup <answer>{dflt or '.'})"
        return step.ask(self) + tail

    def current(self) -> str:
        """Where the owner left off: the current question, or the summary when finished."""
        if self.done:
            return self.summary() + "\n\nSetup is finished. /schedule setup redo starts again."
        return self._question()

    def redo(self) -> str:
        self.state = {"step": 0, "done": False, "answered": []}
        self._persist()
        return "Starting setup again. Your earlier answers stay in place until you answer differently.\n\n" + self._question()

    def answer(self, text: str) -> str:
        if self.done:
            return self.current()
        text = (text or "").strip()
        step = self.steps[self.state["step"]]
        if text.lower() == "skip":
            if not step.default:
                return "This one has no default.\n\n" + self._question()
            text = step.default
        if not text:
            return self._question()
        if _SECRETISH.search(text):
            return ("That looks like a password or a key, so I did not save it. I never take credentials in chat: "
                    "set them in Hermes (hermes auth, or the plugin's settings in Hermes Desktop).\n\n" + self._question())
        try:
            said = step.save(self, text)
        except (ValueError, KeyError, IndexError) as exc:
            return f"{exc}\n\n" + self._question()
        if step.key not in self.state["answered"]:
            self.state["answered"].append(step.key)
        self.state["step"] += 1
        if self.state["step"] >= len(self.steps):
            self.state["step"] = len(self.steps) - 1
            said += "\n\n" + self._finish()
            self._persist()
            return said
        self._persist()
        return said + "\n\n" + self._question()

    def _finish(self) -> str:
        groups = groups_from_config(self.get("groups", {}))
        auto = bool(self.get("auto_book", True))
        tier = AuthorityTier.BOOK if auto else AuthorityTier.OFFER
        moved = [g.name for g in groups.values() if g.tier == AuthorityTier.TRIAL]
        for g in groups.values():
            if g.tier == AuthorityTier.TRIAL:
                g.tier = tier
        self.set("groups", groups_to_config(groups))
        self.state["done"] = True
        moved_txt = (f"{' and '.join(moved)} moved from Trial to {TIER_NAMES[int(tier)]}." if moved else "Group tiers unchanged.")
        return "Setup is finished. " + moved_txt + "\n\n" + self.summary()

    def summary(self) -> str:
        groups = groups_from_config(self.get("groups", {}))
        zone = self.get("home_zone", "")
        lines = [f"Name: {self.get('owner_name', '') or 'not set'}; home zone {zone} ({label_for(zone) if zone else '-'})",
                 f"Mail: {self.get('identity_mode', 'plus')}; you {', '.join(self.get('owner_addresses', []) or []) or 'not set'}; "
                 f"assistant {self.get('agent_address', '') or 'not set'}",
                 f"Writes to: {self.get('write_calendar', '') or 'not set'}; reads as free/busy: "
                 f"{', '.join(self.get('read_calendars', []) or []) or 'none'}"]
        for g in groups.values():
            lines.append(f"{g.name}: {describe_windows(g.windows)}, {g.default_len_min} min, tier {int(g.tier)} {TIER_NAMES[int(g.tier)]}, "
                         f"travel buffers {'on' if g.travel_buffers else 'off'}")
        lines.append(f"Auto-book: {'on' if self.get('auto_book', True) else 'off'}")
        on = self.get("reminders_on", True)
        lines.append("Reminders: " + (f"on; texts by {self.get('reminder_channel', 'email')}"
                                      f"{' to ' + self.get('reminder_target', '') if self.get('reminder_target', '') else ''}" if on else "off"))
        return "\n".join(lines)

    # -- the questions ------------------------------------------------------------------
    def _ask_name(self) -> str:
        return ("Let's set up your scheduling assistant, one question at a time.\n\n"
                "Your name and home time zone? For example: Dana Whitfield, America/Los_Angeles (Pacific or a city works too).")

    def _save_name(self, text: str) -> str:
        if "," not in text:
            raise ValueError("Give your name, a comma, then your zone. For example: Dana Whitfield, Pacific")
        name, zone_text = (x.strip() for x in text.rsplit(",", 1))
        z = zone_from_iana_or_abbr(zone_text)
        if not name or not z:
            raise ValueError(f"I do not know the zone \"{zone_text}\". Try an IANA name like Europe/Zurich, or Pacific, Eastern, CET.")
        self.set("owner_name", name)
        self.set("home_zone", z[0])
        return f"Saved: {name}, {z[0]}."

    def _ask_identity(self) -> str:
        return ("How should the assistant send mail? Reply with one of these, then your own address:\n"
                "- plus (default): it writes as you+agent@yourdomain\n"
                "- same: it writes from your own address and marks every message as the assistant's\n"
                "- alias: it has its own address; put the alias after yours\n"
                "For example: plus dana@example.com")

    def _save_identity(self, text: str) -> str:
        parts = text.split()
        mode = parts[0].lower() if parts and parts[0].lower() in ("plus", "same", "alias") else "plus"
        emails = _EMAIL.findall(text)
        if not emails:
            raise ValueError("I need your address. For example: plus dana@example.com")
        owner = emails[0].lower()
        if mode == "plus":
            local, domain = owner.split("@")
            agent = f"{local}+agent@{domain}"
        elif mode == "same":
            agent = owner
        else:
            if len(emails) < 2:
                raise ValueError("For alias, give your address and then the alias. For example: alias dana@example.com assistant@example.com")
            agent = emails[1].lower()
        self.set("identity_mode", mode)
        self.set("owner_addresses", [owner])
        self.set("agent_address", agent)
        note = " Every message carries the assistant's header and signature, and @agent in your own words is how you bring it in." \
            if mode == "same" else ""
        return f"Saved: you are {owner}; the assistant writes as {agent} ({mode}).{note}"

    def _ask_calendar(self) -> str:
        if self.calendars:
            rows = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(self.calendars))
            src = "These are the fixture calendars." if self.fixture else "These are the calendars I can see."
            return (f"Which one calendar may I write to? I read every other one as free or busy only. {src}\n{rows}\n"
                    "Reply with a number or a calendar id.")
        return ("Which one calendar may I write to? Reply with its id (for example you@example.com). "
                "I read the others you list later as free or busy only.")

    def _save_calendar(self, text: str) -> str:
        t = text.strip()
        if self.calendars:
            if t.isdigit():
                cal = self.calendars[int(t) - 1]
            elif t in self.calendars:
                cal = t
            else:
                raise ValueError(f"Pick one of the {len(self.calendars)} calendars by number.")
            others = [c for c in self.calendars if c != cal]
        else:
            cal, others = t, list(self.get("read_calendars", []) or [])
        self.set("write_calendar", cal)
        self.set("read_calendars", others)
        return f"Saved: I write only to {cal}" + (f"; I read {', '.join(others)} as free or busy." if others else ".")

    def _windows(self, name: str, text: str, default: str) -> str:
        groups = groups_from_config(self.get("groups", {}))
        g = find_group(groups, name)
        if g is None:
            raise KeyError(f"no {name} group")
        g.windows = parse_windows(default if text.strip().lower() == "default" else text)
        self.set("groups", groups_to_config(groups))
        return f"Saved: {g.name} is {describe_windows(g.windows)}."

    def _ask_work(self) -> str:
        return ("Work hours, in your zone? Default: mon-fri 9:00-17:00, busy-only. "
                "Reply default, or for example: mon-thu 9:30-16:00; fri 9:00-12:00")

    def _save_work(self, text: str) -> str:
        return self._windows("work", text, "mon-fri 09:00-17:00")

    def _ask_family(self) -> str:
        return ("Family hours? Family sees all your free time inside these hours. Default: every day 7:00-21:00. "
                "Reply default, or for example: every day 8:00-20:00")

    def _save_family(self, text: str) -> str:
        return self._windows("family", text, "every day 07:00-21:00")

    def _ask_length(self) -> str:
        return "Default meeting length, in minutes? Default 30. Anything of 60 minutes or more still waits for you."

    def _save_length(self, text: str) -> str:
        m = re.search(r"\d{1,3}", text)
        if not m or not 5 <= int(m.group(0)) <= 240:
            raise ValueError("Give a number of minutes between 5 and 240.")
        groups = groups_from_config(self.get("groups", {}))
        for g in groups.values():
            g.default_len_min = int(m.group(0))
        self.set("groups", groups_to_config(groups))
        return f"Saved: {m.group(0)} minutes by default."

    def _ask_auto(self) -> str:
        return ("When someone picks one of the held times, should I book it (on) or ask you first (off)? "
                "Until setup is finished I am in Trial: I offer times and every booking waits for you.")

    def _save_auto(self, text: str) -> str:
        on = _onoff(text)
        self.set("auto_book", on)
        return "Saved: auto-book on." if on else "Saved: auto-book off; I ask you before every booking."

    def _ask_reminders(self) -> str:
        return ("Reminders are on for every meeting I book: a pop-up 10 minutes before and an email a day before on the event, "
                "a text to you an hour before, and a briefing the evening before. They go to you only, never to the other person. "
                "Where should texts go: telegram, sms, imessage or email? Add your own chat id, number or address after it if you like, "
                "or reply off. (The channel's own sign-in is set in Hermes, not here.)")

    def _save_reminders(self, text: str) -> str:
        parts = text.split(None, 1)
        choice = parts[0].lower().rstrip(",")
        if choice == "off":
            self.set("reminders_on", False)
            return "Saved: reminders off."
        if choice not in CHANNELS:
            raise ValueError("Reply telegram, sms, imessage, email, or off.")
        target = parts[1].strip() if len(parts) > 1 else ""
        if choice == "email" and target and target.lower() not in [a.lower() for a in self.get("owner_addresses", []) or []]:
            raise ValueError("Email reminders go to one of your own addresses only.")
        self.set("reminders_on", True)
        self.set("reminder_channel", choice)
        self.set("reminder_target", target)
        return f"Saved: texts by {choice}{' to ' + target if target else ''}."

    def _ask_travel(self) -> str:
        return ("Travel buffers are off. If you turn them on for a group, I read only the location and video-link fields of your "
                "events, on this machine, and block travel time before and after in-person meetings when I offer times. "
                "Titles, attendees and descriptions are never read, and nothing about the place is said to anyone. "
                "Turn on for: none, work, family or both? If on, add your base address after a comma.")

    def _save_travel(self, text: str) -> str:
        choice, _, base = text.partition(",")
        choice = choice.strip().lower()
        if choice not in ("none", "off", "work", "family", "both"):
            raise ValueError("Reply none, work, family or both.")
        on = {"work", "family"} if choice == "both" else ({choice} if choice in ("work", "family") else set())
        if on and not base.strip():
            raise ValueError("Add your base address after a comma, for example: work, 500 Howard St, San Francisco")
        groups = groups_from_config(self.get("groups", {}))
        for g in groups.values():
            g.travel_buffers = g.id in on
        self.set("groups", groups_to_config(groups))
        if on:
            self.set("base_address", base.strip())
            return f"Saved: travel buffers on for {', '.join(sorted(on))}, from {base.strip()}."
        return "Saved: travel buffers off."
