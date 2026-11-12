"""Against real servers running on this machine: Radicale (CalDAV) and GreenMail (IMAP + SMTP). No real accounts.

Skipped unless ``FREEBUSY_LOCAL_SERVERS=1``. They need, on 127.0.0.1:

* Radicale on :5232, htpasswd user ``dana@dana.test`` / ``danapw``, with calendars ``work``, ``personal`` and
  ``shared-ro`` (read-only for that user through a rights file; one daily recurring event in it), e.g.
  ``python -m venv /tmp/rad && /tmp/rad/bin/pip install radicale && /tmp/rad/bin/python -m radicale --config <cfg>``.
* GreenMail standalone (IMAP :3143, SMTP :3025), users ``agent:agentpw@dana.test``, ``dana:danapw@dana.test``,
  ``lukas:lukaspw@other.test``, logins by email:
  ``java -Dgreenmail.setup.test.smtp -Dgreenmail.setup.test.imap -Dgreenmail.users.login=email
  -Dgreenmail.users=agent:agentpw@dana.test,dana:danapw@dana.test,lukas:lukaspw@other.test -jar greenmail-standalone.jar``

The tests set ``FREEBUSY_ALLOW_LOCAL_TEST_SERVERS=1`` themselves (http and loopback), and only inside these tests.
"""

import importlib.util
import os
import smtplib
import socket
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import pytest

from freebusy import keychain
from freebusy.adapters.caldav import CalDAVClient
from freebusy.adapters.imap_smtp import ImapSmtpMail
from freebusy.adapters.routed import CalendarMirror, RoutedCalendar, ref
from freebusy.connect import ConnectFlow, doctor
from freebusy.groups import groups_to_config
from freebusy.netguard import TEST_OVERRIDE
from freebusy.ontology import AuthorityTier, Slot, default_groups

ROOT = Path(__file__).resolve().parents[1]
DAV = "http://127.0.0.1:5232/"
OWNER, AGENT, LUKAS = "dana@dana.test", "agent@dana.test", "lukas@other.test"
PW = {f"caldav:{OWNER}": "danapw", f"mail:{AGENT}": "agentpw"}
MAIL = {"kind": "imap_smtp", "imap_host": "127.0.0.1", "imap_port": 3143, "smtp_host": "127.0.0.1", "smtp_port": 3025,
        "security": "plain", "username": AGENT}


def _up(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(os.environ.get("FREEBUSY_LOCAL_SERVERS") != "1" or not all(_up(p) for p in (5232, 3143, 3025)),
                                reason="local Radicale and GreenMail not running (set FREEBUSY_LOCAL_SERVERS=1)")


@pytest.fixture(autouse=True)
def local(monkeypatch):
    monkeypatch.setenv(TEST_OVERRIDE, "1")
    mem = keychain.MemoryBackend()
    keychain.use_backend(mem)
    yield mem
    keychain.use_backend(None)


def _client():
    return CalDAVClient(DAV, OWNER, lambda: "danapw")


def _cal(cals, name):
    return next(c for c in cals if c.url.rstrip("/").endswith("/" + name))


def _ics_files(cal_url):
    status, _, text = _client().request("PROPFIND", cal_url, None, "1")
    return [h.text for h in ET.fromstring(text).iter("{DAV:}href") if h.text.endswith(".ics")]


def test_radicale_discovery_read_only_and_both_read_paths():
    c = _client()
    cals = c.discover()
    names = {x.url.rstrip("/").rsplit("/", 1)[-1]: x.writable for x in cals}
    assert names == {"work": True, "personal": True, "shared-ro": False}
    ro = _cal(cals, "shared-ro").url
    a, b = datetime(2026, 11, 16, tzinfo=timezone.utc), datetime(2026, 11, 23, tzinfo=timezone.utc)
    via_fb = c.busy(ro, a, b, "America/Los_Angeles")
    assert c.fb_supported[ro] is True and len(via_fb) == 6            # a daily recurring event, expanded by the server
    c.fb_supported[ro] = False
    via_events = c.busy(ro, a, b, "America/Los_Angeles")
    assert via_events == via_fb and all(s.start.endswith("-08:00") for s in via_events)
    assert "Secret" not in repr(via_events)


def test_read_many_hold_on_one_against_radicale(tmp_path):
    c = _client()
    cals = c.discover()
    cid = f"caldav:{OWNER}"
    work, personal, ro = (ref(cid, _cal(cals, n).url) for n in ("work", "personal", "shared-ro"))
    routed = RoutedCalendar({cid: c}, [personal, ro], work, "America/Los_Angeles", meeting_link="https://meet.jit.si/dana")
    slot = Slot("2026-11-24T10:00:00-08:00", "2026-11-24T10:30:00-08:00")
    hold = routed.create_event(work, slot, "Hold", [], tentative=True)
    win = (datetime(2026, 11, 24, tzinfo=timezone.utc), datetime(2026, 11, 25, tzinfo=timezone.utc))
    assert routed.free_busy(work, *win) == []                        # transparent: seen by the owner, not busy
    booked = routed.create_event(work, slot, "Dana and Lukas", [LUKAS])
    assert routed.free_busy(work, *win) == [slot]
    routed.delete_event(work, hold)
    routed.delete_event(work, booked)
    assert routed.free_busy(work, *win) == []
    with pytest.raises(PermissionError):
        routed.create_event(ro, slot, "x", [])                       # the read-only one is never a write target


def test_connect_two_accounts_then_doctor(tmp_path, local):
    cfg = {"owner_addresses": [OWNER], "agent_address": AGENT, "home_zone": "America/Los_Angeles"}
    f = ConnectFlow(tmp_path / "connect.json", lambda k, d: cfg.get(k, d), cfg.__setitem__, tmp_path, probe=lambda u: False)
    assert "dana.test" in f.start(OWNER)
    assert "127.0.0.1:5232" in f.answer(f"server caldav {DAV}")
    assert "hermes freebusy password caldav:dana@dana.test" in f.answer("calendar")
    keychain.store(f"caldav:{OWNER}", "wrong")
    q = f.answer("next")
    assert "could not list any calendars" in q and "401" in q
    keychain.store(f"caldav:{OWNER}", "danapw")
    q = f.answer("next")
    assert "read-only" in q and q.count(". ") >= 3
    order = [line.split(". ", 1)[1].split(" ")[0] for line in q.splitlines() if line[:1].isdigit()]
    q = f.answer("read all")
    w = order.index("work") + 1
    f.answer(f"hold {w}")
    out = f.answer("link https://meet.jit.si/dana-calls")
    assert "Connected." in out and "made one hold on" in out, out
    assert cfg["mode"] == "connectors" and len(cfg["read_calendars"]) == 2
    # the mailbox, a second account
    f.start(AGENT)
    f.answer("server mail 127.0.0.1:3143 127.0.0.1:3025")
    f.answer("mail")
    keychain.store(f"mail:{AGENT}", "agentpw")
    out = f.answer("next")
    out = f.answer("link https://meet.jit.si/dana-calls") if "video link" in out else out
    assert "IMAP 127.0.0.1 and SMTP 127.0.0.1 accepted the sign-in" in out, out
    assert cfg["mail_connection"] == f"mail:{AGENT}" and len(cfg["connections"]) == 2
    report = doctor(lambda k, d: cfg.get(k, d), tmp_path)
    assert "all good" in report and "WARN FREEBUSY_ALLOW_LOCAL_TEST_SERVERS=1" in report and "danapw" not in report


# ---- the whole loop over mail, against both servers ----------------------------------------------------

class Ctx:
    def __init__(self, config):
        self.hooks, self.commands, self.config = {}, {}, dict(config)

    def register_tool(self, name, toolset, schema, handler, **kw):
        pass

    def register_hook(self, name, fn, **kw):
        self.hooks[name] = fn

    def register_command(self, name, handler, description="", **kw):
        self.commands[name] = handler

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def set_config(self, key, value):
        self.config[key] = value


def _send(sender, to, cc, subject, body, mid, refs=()):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = sender, ", ".join(to), subject, mid
    if cc:
        m["Cc"] = ", ".join(cc)
    if refs:
        m["In-Reply-To"], m["References"] = refs[-1], " ".join(refs)
    m.set_content(body)
    with smtplib.SMTP("127.0.0.1", 3025) as s:
        s.send_message(m)


def _inbox(user, pw):
    box = ImapSmtpMail(dict(MAIL, username=user), lambda: pw, user, [])
    return [m for t in box.list_threads(new_only=False) for m in t]


def test_offer_hold_and_book_over_imap_smtp_and_caldav(tmp_path, monkeypatch, local):
    for k, v in PW.items():
        keychain.store(k, v)
    cals = _client().discover()
    cid, mid = f"caldav:{OWNER}", f"mail:{AGENT}"
    groups = default_groups()
    for g in groups.values():
        g.tier = AuthorityTier.BOOK
    work_url = _cal(cals, "work").url
    before = set(_ics_files(work_url))
    config = {"mode": "connectors", "owner_name": "Dana Whitfield", "home_zone": "America/Los_Angeles", "owner_addresses": [OWNER],
              "agent_address": AGENT, "groups": groups_to_config(groups), "meeting_link": "https://meet.jit.si/dana-calls",
              "connections": {cid: {"kind": "caldav", "url": DAV, "username": OWNER}, mid: MAIL}, "mail_connection": mid,
              "write_calendar": ref(cid, work_url), "read_calendars": [ref(cid, _cal(cals, n).url) for n in ("personal", "shared-ro")],
              "reminders_on": False}
    monkeypatch.setenv("FREEBUSY_DATA_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("freebusy_plugin_local", ROOT / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["freebusy_plugin_local"] = mod
    spec.loader.exec_module(mod)
    ctx = Ctx(config)
    mod.register(ctx)
    assert mod._RT["mode"] == "connectors"

    tag = uuid.uuid4().hex[:8]
    intro = f"<intro-{tag}@dana.test>"
    _send(OWNER, [LUKAS], [AGENT], f"Intro {tag}", "Lukas, meet my assistant, cc'd, who will find us a time.\n\nDana", intro)
    time.sleep(0.5)
    out = ctx.commands["freebusy"]("check")
    assert "reply" in out, out
    offer = [m for m in _inbox(LUKAS, "lukaspw") if m.subject.endswith(f"Intro {tag}")][-1]
    assert offer.sender == AGENT and "A." in offer.body and "AI assistant to Dana Whitfield" in offer.body
    assert offer.headers.get("X-Scheduling-Agent") == "1"
    holds = mod._RT["ledger"].open()
    assert holds and all(h.event_id for h in holds)
    assert len(set(_ics_files(work_url)) - before) == len(holds)     # every hold is a tentative event on the work calendar

    _send(LUKAS, [AGENT], [OWNER], f"Re: Intro {tag}", "B, ET\n\nLukas", f"<pick-{tag}@other.test>", refs=(intro,))
    time.sleep(0.5)
    out = ctx.commands["freebusy"]("check")
    assert "reply+notify" in out, out
    booked = [m for m in _inbox(LUKAS, "lukaspw") if m.subject.endswith(f"Intro {tag}") and "Booked" in m.body]
    assert booked and "Join: https://meet.jit.si/dana-calls" in booked[-1].body
    new = set(_ics_files(work_url)) - before
    assert len(new) == 1 and mod._RT["ledger"].open() == []          # holds removed, one booked event left
    a = datetime.now(timezone.utc)
    busy = _client().busy(work_url, a, a + timedelta(days=21), "America/Los_Angeles")
    assert len(busy) >= 1
    notes = [m for m in _inbox(OWNER, "danapw") if m.sender == AGENT and m.subject.startswith("Booked")]
    assert notes, "the owner's Booked note was emailed to the owner"
    for href in new:                                                   # tidy the shared test server
        mod._RT["cal"].delete_event(config["write_calendar"], urljoin_(work_url, href))
    assert "no new scheduling mail" in ctx.commands["freebusy"]("check")


def urljoin_(base, href):
    from urllib.parse import urljoin
    return urljoin(base, href)
