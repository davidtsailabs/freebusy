"""Chat channels through the same engine; /freebusy connect and doctor; the plugin in connectors mode. No network."""

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from freebusy import keychain
from freebusy.adapters import fixture as fx
from freebusy.adapters.caldav import CalendarInfo
from freebusy.chat import ChatDesk, ChatOutbox, RoutingMail, handle_chat
from freebusy.connect import ConnectFlow
from freebusy.engine import Scheduler
from freebusy.holds import HoldLedger
from freebusy.ontology import Slot
from freebusy.policy import Gate

ROOT = Path(__file__).resolve().parents[1]
NORA = {"email": "nora@fjordtx.example", "name": "Nora Lind"}


@pytest.fixture
def chat(tmp_path):
    profile, cal = fx.profile(), fx.FixtureCalendar()
    ledger = HoldLedger(tmp_path / "holds.json")
    gate = Gate(profile, ledger, tmp_path / "audit.jsonl")
    out = ChatOutbox()
    sched = Scheduler(profile, cal, RoutingMail(None, out), ledger, gate, {})
    desk = ChatDesk({"telegram:42": NORA, "whatsapp:+15550001": "Ana Ruiz"}, profile.agent_address)
    notes = []
    run = lambda text, uid="42", platform="telegram", n=[0]: (n.__setitem__(0, n[0] + 1) or  # noqa: E731
                                                               handle_chat(sched, desk, out, platform, {"user_id": uid, "chat_id": uid},
                                                                           text, str(n[0]), fx.NOW, lambda s, t: notes.append((s, t))))
    return {"run": run, "sched": sched, "cal": cal, "ledger": ledger, "notes": notes, "gate": gate}


def test_a_chat_contact_gets_held_times_with_the_assistant_line(chat):
    res = chat["run"]("Hi! Could we find 30 minutes next week? I'm on Eastern time.")
    assert res["action"] == "handled"
    reply = res["reply"]
    assert reply.startswith("(Dana's scheduling assistant, an AI, is answering.)")
    assert "A." in reply and "B." in reply and "ET" in reply
    assert "handles scheduling only" not in reply and "cc'd here" not in reply           # mail-only lines are dropped
    holds = chat["ledger"].for_thread("chat:telegram:42")
    assert holds and all(h.person_id == "nora@fjordtx.example" for h in holds)          # held before offered


def test_the_pick_books_through_the_same_gate_and_the_owner_hears(chat):
    chat["run"]("Could we find 30 minutes next week? I'm on Eastern time.")
    res = chat["run"]("B works")
    assert res["action"] == "handled" and "Booked:" in res["reply"]
    ev = chat["cal"].events[-1]
    assert ev["attendees"] == ["nora@fjordtx.example"] and not ev["tentative"]
    assert chat["notes"] and chat["notes"][-1][0].startswith("Booked")
    assert any(a.get("tool") == "schedule_book" and a.get("verdict") == "allow" for a in chat["gate"].last)


def test_everyone_else_and_small_talk_go_to_the_agent(chat):
    assert chat["run"]("Could we meet Tuesday?", uid="999") is None                    # not a listed contact (the owner included)
    assert chat["run"]("Thanks for the photos from Sunday!") is None                    # a contact, but not scheduling
    assert chat["run"]("", uid="42") is None


def test_a_long_meeting_waits_for_the_owner_and_says_nothing_in_the_chat(chat):
    res = chat["run"]("Could we get an hour next week? I'm on Eastern time.")
    assert res == {"action": "handled"}
    assert chat["notes"] and "Needs you" in chat["notes"][-1][0]
    assert "chat:telegram:42" in chat["sched"].pending


def test_a_contact_without_email_is_never_invited_by_mail(chat):
    res = chat["run"]("Could we find 30 minutes next week? I'm on Eastern time.", uid="+15550001", platform="whatsapp")
    assert res["action"] == "handled" and "A." in res["reply"]
    assert chat["sched"].people["whatsapp.15550001@chat.invalid"].name == "Ana Ruiz"


# ---- /freebusy connect without a network -------------------------------------------------------

class FakeDAV:
    def __init__(self):
        self.events = {}

    def discover(self):
        return [CalendarInfo("https://p42-caldav.icloud.com/123/calendars/home/", "Home", True),
                CalendarInfo("https://p42-caldav.icloud.com/123/calendars/work/", "Work (shared)", False)]

    def busy(self, url, a, b, zone, own_holds=None):
        return [Slot("2026-11-18T09:00:00-08:00", "2026-11-18T10:00:00-08:00")]

    def put_event(self, url, uid, body):
        self.events[url + uid + ".ics"] = body
        return url + uid + ".ics"

    def delete(self, href):
        self.events.pop(href)


@pytest.fixture
def flow(tmp_path, monkeypatch):
    dav = FakeDAV()
    monkeypatch.setattr("freebusy.connect._clients", lambda conns, pw: {c: dav for c, v in conns.items() if v["kind"] == "caldav"})
    monkeypatch.setattr("freebusy.connect.check_url", lambda u: u)
    cfg, store = {}, keychain.MemoryBackend()
    keychain.use_backend(store)
    f = ConnectFlow(tmp_path / "connect.json", lambda k, d: cfg.get(k, d), cfg.__setitem__, tmp_path,
                    probe=lambda u: False, now=lambda: datetime(2026, 11, 12, 17, tzinfo=timezone.utc))
    yield f, cfg, dav, tmp_path
    keychain.use_backend(None)


def test_connect_walks_through_and_switches_on_only_after_the_test(flow):
    f, cfg, dav, tmp = flow
    q = f.start("Dana@iCloud.com")
    assert "Looks like iCloud" in q and "caldav.icloud.com" in q and "imap.mail.me.com:993" in q
    q = f.answer("calendar")
    assert "hermes freebusy password caldav:dana@icloud.com" in q and "Do not paste it here" in q
    assert cfg["connections"]["caldav:dana@icloud.com"] == {"kind": "caldav", "url": "https://caldav.icloud.com/",
                                                            "username": "dana@icloud.com", "provider": "icloud"}
    out = f.answer("abcd-efgh-ijkl-mnop")                       # an app password typed in chat
    assert "did not save it" in out and "abcd" not in out and "abcd" not in json.dumps(cfg)
    assert "I do not see a password" in f.answer("next")
    keychain.store("caldav:dana@icloud.com", "abcd-efgh-ijkl-mnop")
    q = f.answer("next")
    assert "1. Home (dana@icloud.com)" in q and "2. Work (shared) (dana@icloud.com), read-only" in q
    q = f.answer("read all")
    assert "Where should holds" in q and "1. Home" in q and "Work (shared)" not in q.split("Where should holds")[1].split("local:")[0]
    assert "read-only" in f.answer("hold 2")
    q = f.answer("hold 1")
    assert cfg["write_calendar"] == "caldav:dana@icloud.com|https://p42-caldav.icloud.com/123/calendars/home/"
    assert cfg["read_calendars"] == ["caldav:dana@icloud.com|https://p42-caldav.icloud.com/123/calendars/work/"]
    assert "fixed video link" in q
    assert "https URL" in f.answer("link http://zoom.us/j/1") and "mode" not in cfg
    out = f.answer("link https://meet.jit.si/dana-calls")
    assert "ok   read" in out and "made one hold on" in out and "Connected." in out
    assert cfg["mode"] == "connectors" and cfg["meeting_link"] == "https://meet.jit.si/dana-calls"
    assert dav.events == {}                                     # the test hold was removed
    assert "abcd" not in json.dumps(cfg) and "abcd" not in (tmp / "connect.json").read_text()
    assert cfg["owner_addresses"] == ["dana@icloud.com"] and cfg["agent_address"] == "dana+agent@icloud.com"


def test_no_writable_calendar_means_the_local_store(flow, monkeypatch):
    f, cfg, dav, tmp = flow
    monkeypatch.setattr(FakeDAV, "discover", lambda self: [CalendarInfo("https://x.example/ro/", "Team", False)])
    f.start("dana@fastmail.com")
    f.answer("calendar")
    keychain.store("caldav:dana@fastmail.com", "pw")
    f.answer("next")
    q = f.answer("read 1")
    assert "None of these calendars is writable" in q and "hold local" in q
    f.answer("hold local")
    out = f.answer("link none")
    assert "made one hold on the local hold store" in out and cfg["write_calendar"] == "local"
    assert (tmp / "feed" / "freebusy.ics").exists()


def test_outlook_and_gmail_say_what_comes_in_0_4(flow):
    f, cfg, _, _ = flow
    q = f.start("dana@outlook.com")
    assert "Microsoft Graph" in q and "0.4" in q and "no CalDAV server" in q
    assert "no calendar server" in f.answer("calendar")
    q = f.start("dana@gmail.com")
    assert "Google Calendar and Google sign-in come in 0.4" in q and "imap.gmail.com" in q


# ---- the plugin in connectors mode -------------------------------------------------------------

class Ctx:
    def __init__(self, config):
        self.tools, self.hooks, self.commands, self.cli, self.config = {}, {}, {}, {}, dict(config)

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = handler

    def register_hook(self, name, fn, **kw):
        self.hooks[name] = fn

    def register_command(self, name, handler, description="", **kw):
        self.commands[name] = handler

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli[name] = (setup_fn, handler_fn)

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def set_config(self, key, value):
        self.config[key] = value


def _plugin(tmp_path, monkeypatch, ctx):
    monkeypatch.setenv("FREEBUSY_DATA_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("freebusy_plugin_conn", ROOT / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["freebusy_plugin_conn"] = mod
    spec.loader.exec_module(mod)
    mod.register(ctx)
    return mod


LIVE = {"mode": "connectors", "owner_name": "Dana Whitfield", "home_zone": "America/Los_Angeles",
        "owner_addresses": ["dana@example.com"], "agent_address": "dana+agent@example.com", "write_calendar": "local",
        "read_calendars": [], "meeting_link": "https://meet.jit.si/dana", "chat_contacts": {"telegram:42": NORA}}


def test_connectors_mode_runs_on_the_local_store_and_chat(tmp_path, monkeypatch):
    ctx = Ctx(LIVE)
    mod = _plugin(tmp_path, monkeypatch, ctx)
    rt = mod._RT
    assert rt["mode"] == "connectors" and rt["profile"].write_calendar == "local" and "freebusy" in ctx.cli
    doc = ctx.commands["freebusy"]("doctor")
    assert "made one hold on the local hold store and removed it" in doc and "all good" in doc and "chat contacts: 1" in doc
    res = ctx.hooks["post_gateway_admission"](session_key="s", platform="telegram", source={"user_id": "42", "chat_id": "42"},
                                              message_id="m1", text="Could we find 30 minutes next week? I'm on Eastern time.")
    assert res["action"] == "handled" and res["reply"].startswith("(Dana's scheduling assistant")
    feed = (tmp_path / "feed" / "freebusy.ics").read_text()
    assert feed.count("STATUS:TENTATIVE") == len(rt["ledger"].open()) > 0                  # every hold mirrored to the feed
    assert ctx.hooks["post_gateway_admission"](platform="telegram", source={"user_id": "7"}, text="hello") is None
    assert "no mail connector" in ctx.commands["freebusy"]("check")


def test_cli_password_command_stores_in_the_keychain(tmp_path, monkeypatch, capsys):
    ctx = Ctx({})
    mod = _plugin(tmp_path, monkeypatch, ctx)
    keychain.use_backend(keychain.MemoryBackend())
    try:
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "pw-123")
        _, handler = ctx.cli["freebusy"]
        assert handler(type("A", (), {"freebusy_cmd": "password", "connection": "mail:dana@icloud.com"})()) == 0
        assert keychain.get("mail:dana@icloud.com") == "pw-123" and "pw-123" not in capsys.readouterr().out
    finally:
        keychain.use_backend(None)


def test_manifest_declares_0_3(tmp_path):
    import yaml
    m = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    assert m["version"] == "0.3.0" and m["config_schema"]["mode"]["choices"] == ["fixture", "connectors", "google_workspace"]
    assert "post_gateway_admission" in m["provides_hooks"] and m["python_dependencies"] == ["keyring>=25.7,<26"]
    for k in ("connections", "mail_connection", "meeting_link", "chat_contacts"):
        assert k in m["config_schema"] and m["config_schema"][k].get("type") != "secret"
    from freebusy import __version__
    assert __version__ == "0.3.0"
