"""/freebusy connect, /freebusy doctor, and building the live adapters from config.

``/freebusy connect you@example.com`` guesses the provider from the domain and
walks the owner through it, one message at a time:

1. which connectors to use (calendar, mail, or both), with servers the owner can override;
2. the app password, typed in a terminal on this machine (``hermes freebusy password <id>``),
   stored in the system keychain; never in chat, never in config;
3. which calendars to read busy times from (any number, across accounts);
4. where holds go: one writable calendar, or the local hold store and its feed file;
5. a fixed meeting link for video meetings, or none;
6. a test: read a week of busy times, make one hold and remove it, sign in to mail.

Config keeps servers, user names and calendar URLs only. Nothing here logs or
returns a password.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urljoin

from . import keychain
from .adapters.caldav import CalDAVClient, safe_discover
from .adapters.imap_smtp import ImapSmtpMail
from .adapters.routed import LOCAL, LocalHoldStore, RoutedCalendar, ref, split_ref
from .netguard import TEST_OVERRIDE, UnsafeTarget, check_host, check_link, check_url, local_test_override
from .ontology import Slot
from .providers import PROVIDERS, guess

_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_HOSTPORT = re.compile(r"^([A-Za-z0-9.\-]+):(\d{2,5})$")
# App passwords as providers print them (Apple: abcd-efgh-ijkl-mnop; Google: abcd efgh ijkl mnop), or anything key-like.
_APP_PASSWORD = re.compile(r"\b[a-z]{4}([- ])[a-z]{4}\1[a-z]{4}\1[a-z]{4}\b|\bpassword\b|[A-Za-z0-9_\-]{24,}", re.IGNORECASE)


def probe_caldav(url: str) -> bool:
    """An unauthenticated PROPFIND to see whether a CalDAV server answers there. No credentials are sent."""
    try:
        check_url(url)
        req = urllib.request.Request(url, method="PROPFIND", headers={"Depth": "0", "User-Agent": "freebusy/0.3"})
        urllib.request.urlopen(req, timeout=8)
        return True
    except urllib.error.HTTPError as err:
        return err.code in (207, 301, 302, 307, 308, 401)
    except Exception:
        return False


# ---- building the adapters --------------------------------------------------------

def _clients(conns: dict, password: Callable[[str], Optional[str]]) -> dict[str, CalDAVClient]:
    return {cid: CalDAVClient(c["url"], c.get("username") or c.get("email", ""), (lambda cid=cid: password(cid)))
            for cid, c in conns.items() if c.get("kind") == "caldav"}


def build_connectors(get: Callable[[str, Any], Any], data_dir: Path, password: Callable[[str], Optional[str]] = keychain.get):
    """(calendar adapter or None, mail adapter or None, local store or None) from config. Nothing connects here."""
    conns = dict(get("connections", {}) or {})
    write = str(get("write_calendar", "") or "")
    reads = [r for r in (get("read_calendars", []) or []) if r]
    cal, store = None, None
    if write or reads:
        write = write or LOCAL                      # nothing writable chosen: holds go to the local store
        store = LocalHoldStore(Path(data_dir)) if write == LOCAL or LOCAL in reads else None
        owners = list(get("owner_addresses", []) or [])
        cal = RoutedCalendar(_clients(conns, password), reads, write, str(get("home_zone", "America/Los_Angeles")), store,
                             meeting_link=str(get("meeting_link", "") or ""), organizer=owners[0] if owners else "")
    mail = None
    mid = str(get("mail_connection", "") or "")
    if mid and mid in conns:
        name = str(get("owner_name", "") or "")
        mail = ImapSmtpMail(conns[mid], lambda: password(mid), str(get("agent_address", "") or conns[mid].get("username", "")),
                            list(get("owner_addresses", []) or []), Path(data_dir) / "imap-state.json",
                            agent_name=f"Assistant to {name}" if name else "")
    return cal, mail, store


# ---- the test, shared by connect and doctor ------------------------------------------

def run_test(cal, mail, now: Optional[datetime] = None) -> tuple[bool, list[str]]:
    """Read a week of busy times, make one hold and remove it, sign in to mail. Counts only, never event times."""
    now = now or datetime.now(timezone.utc)
    ok, lines = True, []
    if cal is not None:
        for c in dict.fromkeys([*cal.read_calendars, cal.write_calendar]):
            label = "local hold store" if c == LOCAL else split_ref(c)[1]
            try:
                n = len(cal.free_busy(c, now, now + timedelta(days=7)))
                lines.append(f"ok   read {label}: {n} busy block{'s' if n != 1 else ''} in the next 7 days")
            except Exception as exc:
                ok = False
                lines.append(f"FAIL read {label}: {exc}")
        start = (now + timedelta(days=7)).replace(minute=0, second=0, microsecond=0)
        slot = Slot(start.isoformat(), (start + timedelta(minutes=15)).isoformat())
        target = "the local hold store" if cal.write_calendar == LOCAL else split_ref(cal.write_calendar)[1]
        try:
            eid = cal.create_event(cal.write_calendar, slot, "freebusy test hold (removed at once)", [], tentative=True)
            cal.delete_event(cal.write_calendar, eid)
            lines.append(f"ok   made one hold on {target} and removed it")
        except Exception as exc:
            ok = False
            lines.append(f"FAIL hold on {target}: {exc}")
    if mail is not None:
        try:
            lines.append("ok   " + mail.check())
        except Exception as exc:
            ok = False
            lines.append(f"FAIL mail: {exc}")
    if cal is None and mail is None:
        ok = False
        lines.append("nothing is connected yet: /freebusy connect you@example.com")
    return ok, lines


def doctor(get: Callable[[str, Any], Any], data_dir: Path, password: Callable[[str], Optional[str]] = keychain.get,
           has_password: Callable[[str], bool] = keychain.has, now: Optional[datetime] = None) -> str:
    lines = [f"mode: {get('mode', 'fixture')}"]
    if get("mode", "fixture") != "connectors":
        lines.append("  connectors are not switched on; /freebusy connect sets mode to connectors when its test passes")
    if local_test_override():
        lines.append(f"WARN {TEST_OVERRIDE}=1 is set: http and loopback servers are allowed. Unset it outside tests.")
    conns = dict(get("connections", {}) or {})
    for cid, c in conns.items():
        where = c.get("url") or f"{c.get('imap_host')}:{c.get('imap_port')} / {c.get('smtp_host')}:{c.get('smtp_port')}"
        try:
            if c.get("kind") == "caldav":
                check_url(c["url"])
            else:
                check_host(c["imap_host"], int(c.get("imap_port", 993)))
                check_host(c["smtp_host"], int(c.get("smtp_port", 465)))
            net = "address allowed"
        except (UnsafeTarget, KeyError) as exc:
            net = f"BLOCKED ({exc})"
        pw = "app password in keychain" if has_password(cid) else "NO app password (hermes freebusy password " + cid + ")"
        lines.append(f"{cid}: {where}; {net}; {pw}")
    if not conns:
        lines.append("no connections")
    write = get("write_calendar", "")
    lines.append(f"holds go to: {'the local hold store, feed ' + str(Path(data_dir) / 'feed' / 'freebusy.ics') if write == LOCAL else write or 'not set'}")
    lines.append(f"reads busy times from: {len(get('read_calendars', []) or [])} calendar(s)")
    link = get("meeting_link", "")
    if link:
        try:
            check_link(link)
            lines.append(f"meeting link: {link}")
        except UnsafeTarget as exc:
            lines.append(f"meeting link REFUSED: {exc}")
    else:
        lines.append("meeting link: none")
    contacts = dict(get("chat_contacts", {}) or {})
    lines.append(f"chat contacts: {len(contacts)}" + (" (" + ", ".join(sorted(contacts)) + ")" if contacts else ""))
    try:
        cal, mail, _ = build_connectors(get, data_dir, password)
        ok, test = run_test(cal, mail, now)
        lines += ["test:"] + ["  " + t for t in test]
        lines.append("all good" if ok else "something needs fixing (see FAIL above)")
    except Exception as exc:
        lines.append(f"FAIL could not build the connectors: {exc}")
    return "\n".join(lines)


# ---- the guided flow ------------------------------------------------------------------

class ConnectFlow:
    def __init__(self, state_path: Path, get: Callable[[str, Any], Any], set_: Callable[[str, Any], None], data_dir: Path,
                 password: Callable[[str], Optional[str]] = keychain.get, has_password: Callable[[str], bool] = keychain.has,
                 probe: Callable[[str], bool] = probe_caldav, now: Optional[Callable[[], datetime]] = None):
        self.path = Path(state_path)
        self.get, self.set, self.data_dir = get, set_, Path(data_dir)
        self.password, self.has_password, self.probe = password, has_password, probe
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.state: dict = json.loads(self.path.read_text()) if self.path.exists() else {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, indent=1))

    def _conns(self) -> dict:
        return dict(self.get("connections", {}) or {})

    # -- driving ----------------------------------------------------------------------
    def current(self) -> str:
        step = self.state.get("step")
        if not step:
            return "Start with your address: /freebusy connect you@example.com"
        return getattr(self, f"_ask_{step}")()

    def start(self, address: str) -> str:
        address = address.strip().lower()
        p = guess(address)
        caldav = p.caldav
        if p.key == "other" and caldav and not self.probe(urljoin(caldav, "/.well-known/caldav")):
            caldav = None
        self.state = {"step": "choose", "email": address, "provider": p.key, "caldav": caldav,
                      "imap": list(p.imap) if p.imap else None, "smtp": list(p.smtp) if p.smtp else None}
        self._save()
        return self._ask_choose()

    def answer(self, text: str) -> str:
        text = (text or "").strip()
        if _EMAIL.match(text):
            return self.start(text)
        if text.lower() in ("cancel", "stop"):
            self.state = {}
            self._save()
            return "Stopped. Nothing you already saved was removed. /freebusy connect you@example.com starts again."
        step = self.state.get("step")
        if not step:
            return self.current()
        if not text:
            return self.current()
        if _APP_PASSWORD.search(text) and not text.lower().startswith(("server ", "link ")):
            return ("That looks like a password, so I did not save it or repeat it. Passwords never go in chat: "
                    "run the terminal command below instead, and if it was a real one, consider making a new one.\n\n"
                    + (self._ask_password() if step == "password" else self.current()))
        try:
            return getattr(self, f"_on_{step}")(text)
        except (ValueError, UnsafeTarget, KeyError, IndexError) as exc:
            return f"{exc}\n\n{self.current()}"

    # -- 1. connectors --------------------------------------------------------------------
    def _ask_choose(self) -> str:
        s = self.state
        p = PROVIDERS.get(s["provider"]) or guess(s["email"])
        name = p.name if s["provider"] != "other" else f"your own domain ({p.name})"
        rows = [f"Looks like {name}. What should I connect for {s['email']}?"]
        rows.append(f"- calendar: CalDAV at {s['caldav']}" if s.get("caldav") else "- calendar: no CalDAV server found for this address")
        rows.append(f"- mail: IMAP {s['imap'][0]}:{s['imap'][1]}, SMTP {s['smtp'][0]}:{s['smtp'][1]}" if s.get("imap") else
                    "- mail: no IMAP/SMTP servers for this address in this version")
        if p.note:
            rows.append(p.note)
        rows.append("Reply /freebusy connect with: calendar, mail, or calendar mail. To change servers first: "
                    "server caldav https://..., or server mail imap.example.com:993 smtp.example.com:465. "
                    "Another provider: provider icloud (fastmail, zoho, nextcloud).")
        return "\n".join(rows)

    def _on_choose(self, text: str) -> str:
        s, words = self.state, text.split()
        if words[0].lower() == "provider":
            p = PROVIDERS.get(words[1].lower()) if len(words) > 1 else None
            if p is None:
                raise ValueError(f"I know {', '.join(PROVIDERS)}.")
            s.update(provider=p.key, caldav=p.caldav, imap=list(p.imap) if p.imap else None, smtp=list(p.smtp) if p.smtp else None)
            self._save()
            return self._ask_choose()
        if words[0].lower() == "server":
            if len(words) == 3 and words[1].lower() == "caldav":
                s["caldav"] = check_url(words[2])
            elif len(words) == 4 and words[1].lower() == "mail":
                hp = [_HOSTPORT.match(w) for w in words[2:]]
                if not all(hp):
                    raise ValueError("Give host:port twice, for example: server mail imap.example.com:993 smtp.example.com:465")
                s["imap"], s["smtp"] = [hp[0].group(1), int(hp[0].group(2))], [hp[1].group(1), int(hp[1].group(2))]
                for h, port in (s["imap"], s["smtp"]):
                    check_host(h, port)
            else:
                raise ValueError("Use: server caldav https://... or server mail imap-host:port smtp-host:port")
            self._save()
            return self._ask_choose()
        use = {w.lower().strip(",") for w in words} & {"calendar", "mail"}
        if "both" in text.lower():
            use = {"calendar", "mail"}
        if not use:
            raise ValueError("Reply calendar, mail, or calendar mail.")
        if "calendar" in use and not s.get("caldav"):
            raise ValueError("There is no calendar server for this address yet; give one with server caldav https://...")
        if "mail" in use and not s.get("imap"):
            raise ValueError("There are no mail servers for this address yet; give them with server mail host:port host:port")
        conns, ids = self._conns(), []
        if "calendar" in use:
            check_url(s["caldav"])
            cid = f"caldav:{s['email']}"
            conns[cid] = {"kind": "caldav", "url": s["caldav"], "username": s["email"], "provider": s["provider"]}
            ids.append(cid)
        if "mail" in use:
            cid = f"mail:{s['email']}"
            conns[cid] = {"kind": "imap_smtp", "imap_host": s["imap"][0], "imap_port": s["imap"][1], "smtp_host": s["smtp"][0],
                          "smtp_port": s["smtp"][1], "username": s["email"], "provider": s["provider"]}
            if local_test_override() and s["imap"][1] not in (993,) and s["smtp"][1] not in (465, 587):
                conns[cid]["security"] = "plain"         # local test servers only; refused without the override
            ids.append(cid)
            self.set("mail_connection", cid)
        self.set("connections", conns)
        if not self.get("owner_addresses", []):
            self.set("owner_addresses", [s["email"]])
        if not self.get("agent_address", ""):
            local, domain = s["email"].split("@")
            self.set("agent_address", f"{local}+agent@{domain}")
        s.update(step="password", ids=ids, use=sorted(use))
        self._save()
        return f"Saved the servers for {', '.join(ids)} (no password in config).\n\n" + self._ask_password()

    # -- 2. password ------------------------------------------------------------------------
    def _ask_password(self) -> str:
        s = self.state
        p = PROVIDERS.get(s["provider"]) or guess(s["email"])
        missing = [i for i in s["ids"] if not self.has_password(i)]
        if not missing:
            return "The app password is already in the keychain. Reply /freebusy connect next to go on."
        cmds = "\n".join(f"  hermes freebusy password {i}" for i in missing)
        return (f"Make an app password ({p.app_passwords or 'in your provider security settings'}). Do not paste it here. "
                f"In a terminal on this machine run:\n{cmds}\n"
                "It asks for the password without showing it and keeps it in the system keychain. "
                "Then reply /freebusy connect next.")

    def _on_password(self, text: str) -> str:
        if text.lower() not in ("next", "done", "ok"):
            return ("That is not something I take in chat. If it was a password, it was not saved anywhere; "
                    "change it at your provider to be safe.\n\n" + self._ask_password())
        missing = [i for i in self.state["ids"] if not self.has_password(i)]
        if missing:
            return f"I do not see a password for {', '.join(missing)} yet.\n\n" + self._ask_password()
        if "calendar" in self.state["use"]:
            self.state["step"] = "calendars"
            self._save()
            return self._ask_calendars()
        self.state["step"] = "link" if self.get("write_calendar", "") else "hold"
        self._save()
        return self.current()

    # -- 3. which calendars to read ----------------------------------------------------------------
    def _found(self) -> list[dict]:
        found, errors = [], []
        conns = self._conns()
        for cid, client in _clients(conns, self.password).items():
            cals, err = safe_discover(client)
            if err:
                errors.append(f"{cid}: {err}")
            found += [{"ref": ref(cid, c.url), "name": c.name, "writable": c.writable, "conn": cid} for c in cals]
        self.state["found"], self.state["errors"] = found, errors
        self._save()
        return found

    def _ask_calendars(self) -> str:
        found = self._found()
        if not found:
            return ("I could not list any calendars. " + "; ".join(self.state.get("errors") or []) +
                    "\nCheck the server and the app password (/freebusy doctor), then reply /freebusy connect next.")
        rows = "\n".join(f"{i + 1}. {c['name']} ({c['conn'].split(':', 1)[1]}){'' if c['writable'] else ', read-only'}"
                         for i, c in enumerate(found))
        return ("Which calendars should I read busy times from? I see only start and end times, never titles or details.\n"
                f"{rows}\nReply /freebusy connect read 1 2 (or read all).")

    def _on_calendars(self, text: str) -> str:
        found = self.state.get("found") or self._found()
        if text.lower() == "next":
            return self._ask_calendars()
        words = text.lower().split()
        if not words or words[0] != "read":
            raise ValueError("Reply read and the numbers, for example read 1 3, or read all.")
        picks = found if words[1:] == ["all"] else [found[int(w) - 1] for w in words[1:] if w.isdigit()]
        if not picks:
            raise ValueError("Pick at least one calendar by number.")
        self.set("read_calendars", [c["ref"] for c in picks])
        self.state["step"] = "hold"
        self._save()
        return f"Saved: reading {len(picks)} calendar(s).\n\n" + self._ask_hold()

    # -- 4. where holds go ----------------------------------------------------------------------
    def _ask_hold(self) -> str:
        found = self.state.get("found") or []
        writable = [(i, c) for i, c in enumerate(found) if c["writable"]]
        feed = self.data_dir / "feed" / "freebusy.ics"
        local = (f"local: keep holds and bookings on this machine and write them to {feed}, "
                 "a calendar feed file you can subscribe to in any calendar app")
        if not writable:
            return (f"None of these calendars is writable, so holds can go to the local store.\n- {local}\n"
                    "Reply /freebusy connect hold local.")
        rows = "\n".join(f"{i + 1}. {c['name']}" for i, c in writable)
        return (f"Where should holds and booked meetings go? One calendar only; I never write to the others.\n{rows}\n"
                f"- {local}\nReply /freebusy connect hold 2 (a number above) or hold local.")

    def _on_hold(self, text: str) -> str:
        words = text.lower().split()
        if not words or words[0] != "hold" or len(words) != 2:
            raise ValueError("Reply hold and a number, or hold local.")
        if words[1] == "local":
            target = LOCAL
            said = "Saved: holds go to the local store and its feed file."
        else:
            c = (self.state.get("found") or [])[int(words[1]) - 1]
            if not c["writable"]:
                raise ValueError(f"{c['name']} is read-only. Pick a writable one, or hold local.")
            target = c["ref"]
            said = f"Saved: holds go to {c['name']}; every other calendar is read as busy times only."
        self.set("write_calendar", target)
        self.set("read_calendars", [r for r in (self.get("read_calendars", []) or []) if r != target])
        self.state["step"] = "link"
        self._save()
        return said + "\n\n" + self._ask_link()

    # -- 5. meeting link ----------------------------------------------------------------------------
    def _ask_link(self) -> str:
        return ("Do you have a fixed video link for meetings? It goes on every meeting I book. Free options: a Zoom personal room, "
                "a Jitsi room (https://meet.jit.si/your-name), Whereby (https://whereby.com/your-room) or similar. "
                "Teams and Meet links per meeting come in 0.4.\nReply /freebusy connect link https://... or link none.")

    def _on_link(self, text: str) -> str:
        words = text.split()
        if not words or words[0].lower() != "link" or len(words) != 2:
            raise ValueError("Reply link and the URL, or link none.")
        if words[1].lower() == "none":
            self.set("meeting_link", "")
            said = "Saved: no meeting link."
        else:
            self.set("meeting_link", check_link(words[1]))
            said = f"Saved: {words[1]} goes on booked meetings."
        self.state["step"] = "test"
        self._save()
        return said + "\n\n" + self._test()

    # -- 6. test ---------------------------------------------------------------------------------
    def _ask_test(self) -> str:
        return "Reply /freebusy connect test to run the test again."

    def _on_test(self, text: str) -> str:
        return self._test()

    def _test(self) -> str:
        cal, mail, _ = build_connectors(self.get, self.data_dir, self.password)
        ok, lines = run_test(cal, mail, self.now())
        out = "Test:\n" + "\n".join(lines)
        if ok:
            self.set("mode", "connectors")
            self.state["step"] = "done"
            self._save()
            return out + ("\n\nConnected. The assistant now reads and holds through these connectors. "
                          "Connect another account with /freebusy connect other@example.com; check any time with /freebusy doctor.")
        return out + "\n\nNot switched on yet. Fix what failed, then reply /freebusy connect test."

    def _ask_done(self) -> str:
        return "Connected. /freebusy doctor checks everything; /freebusy connect other@example.com adds another account."

    def _on_done(self, text: str) -> str:
        return self._ask_done()
