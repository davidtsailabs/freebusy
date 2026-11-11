"""/schedule setup, /schedule groups and /schedule group through the plugin, and the manifest's config_schema."""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from freebusy.groups import groups_from_config  # noqa: E402
from freebusy.ontology import default_groups  # noqa: E402


class Ctx:
    """A host with set_config, like Hermes."""

    def __init__(self):
        self.tools, self.hooks, self.commands, self.config, self.dispatched = {}, {}, {}, {}, []

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = handler

    def register_hook(self, name, fn, **kw):
        self.hooks[name] = fn

    def register_command(self, name, handler, description="", **kw):
        self.commands[name] = handler

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def set_config(self, key, value):
        self.config[key] = value


def _plugin(tmp_path, monkeypatch, ctx=None):
    monkeypatch.setenv("FREEBUSY_DATA_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("freebusy_plugin_setup", ROOT / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["freebusy_plugin_setup"] = mod
    spec.loader.exec_module(mod)
    ctx = ctx or Ctx()
    mod.register(ctx)
    return mod, ctx


ANSWERS = ["Dana Whitfield, Pacific", "plus dana@example.com", "1", "default", "skip", "30", "on", "telegram 5551234",
           "work, 500 Howard St, San Francisco"]


def test_setup_one_question_per_message_saved_as_it_goes(tmp_path, monkeypatch):
    mod, ctx = _plugin(tmp_path, monkeypatch)
    q1 = mod._cmd_schedule("setup")
    assert "name and home time zone" in q1 and "Question 1 of 9" in q1
    out = mod._cmd_schedule("setup " + ANSWERS[0])
    assert ctx.config["owner_name"] == "Dana Whitfield" and ctx.config["home_zone"] == "America/Los_Angeles"
    assert "Question 2 of 9" in out and out.count("?") <= 2       # the next question only
    mod._cmd_schedule("setup " + ANSWERS[1])
    q3 = mod._cmd_schedule("setup")                                # resume: asks question 3 again
    assert "Question 3 of 9" in q3 and "work@example.com" in q3 and "fixture calendars" in q3
    for a in ANSWERS[2:]:
        out = mod._cmd_schedule("setup " + a)
    assert "Setup is finished" in out and "Work and Family moved from Trial to Book" in out
    c = ctx.config
    assert c["agent_address"] == "dana+agent@example.com" and c["owner_addresses"] == ["dana@example.com"]
    assert c["write_calendar"] == "work@example.com" and c["read_calendars"] == ["dana.personal@example.com", "family@example.com"]
    assert c["reminder_channel"] == "telegram" and c["reminder_target"] == "5551234" and c["auto_book"] is True
    g = groups_from_config(c["groups"])
    assert g["work"].travel_buffers and not g["family"].travel_buffers and c["base_address"].startswith("500 Howard")
    assert g["work"].tier == 2 and g["family"].tier == 2


def test_setup_refuses_secrets_and_can_be_redone(tmp_path, monkeypatch):
    mod, ctx = _plugin(tmp_path, monkeypatch)
    mod._cmd_schedule("setup " + ANSWERS[0])
    out = mod._cmd_schedule("setup my app password is abcd efgh ijkl")
    assert "did not save" in out and "Question 2 of 9" in out and "abcd" not in out
    assert "abcd" not in json.dumps(ctx.config) and "abcd" not in (tmp_path / "audit.jsonl").read_text()
    out = mod._cmd_schedule("setup redo")
    assert "Question 1 of 9" in out and ctx.config["owner_name"] == "Dana Whitfield"


def test_setup_resumes_across_restarts_without_set_config(tmp_path, monkeypatch):
    class Bare:
        def get_config(self, key, default=None):
            return default

    mod, _ = _plugin(tmp_path, monkeypatch, Bare())
    mod._cmd_schedule("setup " + ANSWERS[0])
    mod2, _ = _plugin(tmp_path, monkeypatch, Bare())            # a restart: settings.json and setup.json persist
    assert "Question 2 of 9" in mod2._cmd_schedule("setup")
    assert json.loads((tmp_path / "settings.json").read_text())["owner_name"] == "Dana Whitfield"


def test_setup_rejects_bad_answers_and_keeps_the_question(tmp_path, monkeypatch):
    mod, ctx = _plugin(tmp_path, monkeypatch)
    out = mod._cmd_schedule("setup Dana Whitfield, Narnia")
    assert "do not know the zone" in out and "Question 1 of 9" in out and "owner_name" not in ctx.config


def test_groups_list_and_edit(tmp_path, monkeypatch):
    mod, ctx = _plugin(tmp_path, monkeypatch)
    listing = mod._cmd_schedule("groups")
    assert "Work (default): mon-fri 09:00-17:00" in listing and "Family: every day 07:00-21:00; full" in listing
    assert "tier 0 Trial" in listing
    out = mod._cmd_schedule("group Work windows mon-thu 9:30-16:00; fri 9:00-12:00")
    assert out == "Work: windows is now mon-thu 09:30-16:00; fri 09:00-12:00"
    assert mod._cmd_schedule("group family domains add whitfield.example").endswith("whitfield.example")
    assert mod._cmd_schedule("group work tier 2").endswith("2 Book")
    assert mod._cmd_schedule("group work colour blue").startswith("not saved")
    g = groups_from_config(ctx.config["groups"])
    assert g["work"].windows["fri"][0].end == "12:00" and g["family"].domains == ["whitfield.example"] and g["work"].tier == 2


def test_manifest_groups_default_matches_the_code():
    import yaml  # the Hermes venv has PyYAML
    schema = yaml.safe_load((ROOT / "plugin.yaml").read_text())["config_schema"]
    assert groups_from_config(schema["groups"]["default"]) == default_groups()
    for key in ("reminders_on", "reminder_channel", "reminder_text_before_min", "briefing_on", "base_address", "identity_mode"):
        assert key in schema
    assert schema["reminder_channel"]["choices"] == ["telegram", "sms", "imessage", "email"]
    assert not any(v.get("type") == "secret" for v in schema.values())


def test_reminders_command_shows_the_outbox(tmp_path, monkeypatch):
    mod, _ = _plugin(tmp_path, monkeypatch)
    assert mod._cmd_schedule("reminders") == "no reminders yet"
    mod._cmd_schedule("run zurich-no-zone")
    mod._RT["scheduler"].handle(mod._RT["mail"].thread("b-cet"), mod._now().replace(day=10))
    out = mod._cmd_schedule("reminders")
    assert "text at 2026-11-12T" in out and "briefing at 2026-11-11T17:00" in out and "TOMORROW: Lukas Brenner" in out
    assert (tmp_path / "outbox.jsonl").exists()


def test_live_mode_builds_the_profile_from_config_and_uses_hermes_cron(tmp_path, monkeypatch):
    ctx = Ctx()
    ctx.config.update({"mode": "google_workspace", "owner_name": "Dana Whitfield", "owner_addresses": ["dana@example.com"],
                       "agent_address": "dana+agent@example.com", "write_calendar": "work@example.com",
                       "reminder_channel": "imessage"})
    ctx.dispatch_tool = lambda name, args: ctx.dispatched.append((name, args)) or "{}"
    mod, _ = _plugin(tmp_path, monkeypatch, ctx)
    p = mod._RT["profile"]
    assert set(p.groups) == {"work", "family"} and p.default_group == "work" and p.reminders.channel == "imessage"
    assert type(mod._RT["outbox"]).__name__ == "HermesCron"
