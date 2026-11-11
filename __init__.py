"""freebusy plugin: registration with Hermes.

Wires six tools, four hooks and three slash commands (/schedule with setup, groups,
group, reminders and the owner's controls; /holds; /zones). Every hook accepts
**kwargs and never raises. Anything the host context lacks is skipped
quietly. In fixture mode nothing here reaches a network or a credential.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from freebusy import __version__  # noqa: E402
from freebusy.adapters import fixture as fx  # noqa: E402
from freebusy.engine import Scheduler  # noqa: E402
from freebusy.holds import HoldLedger  # noqa: E402
from freebusy.offer import build_offer, check_proposed  # noqa: E402
from freebusy.groups import describe, groups_from_config, groups_to_config, set_setting  # noqa: E402
from freebusy.ontology import OwnerProfile, Reminders, Slot, from_dict, to_dict  # noqa: E402
from freebusy.reminders import HermesCron, Outbox  # noqa: E402
from freebusy.setup import SetupFlow  # noqa: E402
from freebusy.parse import Email, parse_email, parse_instruction  # noqa: E402
from freebusy.policy import Gate  # noqa: E402
from freebusy.zones import detect_zone, label_for  # noqa: E402
from freebusy import schemas  # noqa: E402

logger = logging.getLogger(__name__)

_RT: dict = {}   # runtime: profile, ledger, gate, scheduler, data_dir

RESUME_RULE = "freebusy-resume"          # rule_key prefix for the Hermes approval of a schedule_resume call
_HUMAN_YES = {"once", "session", "always"}


def _data_dir(ctx) -> Path:
    """FREEBUSY_DATA_DIR wins (tests and the demo set it), then the host's plugin data dir, then ~/.freebusy."""
    if os.environ.get("FREEBUSY_DATA_DIR"):
        base = Path(os.environ["FREEBUSY_DATA_DIR"])
        base.mkdir(parents=True, exist_ok=True)
        return base
    try:
        from plugins.plugin_storage import plugin_data_dir  # type: ignore
        return Path(plugin_data_dir("freebusy"))
    except Exception:
        base = Path.home() / ".freebusy"
        base.mkdir(parents=True, exist_ok=True)
        return base


_OVERLAY: dict = {}   # answers saved this session; persisted to settings.json only when the host has no set_config


def _cfg(ctx, key: str, default):
    if key in _OVERLAY:
        return _OVERLAY[key]
    try:
        return ctx.get_config(key, default=default)
    except Exception:
        return default


def _set_cfg(ctx, key: str, value) -> None:
    """Save one setting: through Hermes (the same writer as the Desktop settings form) when the host offers it,
    else to the plugin's own settings.json. Never a secret: config_schema declares none and setup refuses them."""
    _OVERLAY[key] = value
    if hasattr(ctx, "set_config"):
        try:
            ctx.set_config(key, value)
            return
        except Exception as exc:
            logger.debug("freebusy set_config %s: %s", key, exc)
    path = _data_dir(ctx) / "settings.json"
    saved = json.loads(path.read_text()) if path.exists() else {}
    saved[key] = value
    path.write_text(json.dumps(saved, indent=1, sort_keys=True))


def _load_overlay(ctx) -> None:
    _OVERLAY.clear()
    if hasattr(ctx, "set_config"):
        return
    path = _data_dir(ctx) / "settings.json"
    if path.exists():
        try:
            _OVERLAY.update(json.loads(path.read_text()))
        except Exception:
            pass


def _reminders_from_config(ctx) -> Reminders:
    return Reminders(on=bool(_cfg(ctx, "reminders_on", True)), popup_min=int(_cfg(ctx, "reminder_popup_min", 10)),
                     email_min=int(_cfg(ctx, "reminder_email_min", 1440)), text_before_min=int(_cfg(ctx, "reminder_text_before_min", 60)),
                     channel=str(_cfg(ctx, "reminder_channel", "email")), target=str(_cfg(ctx, "reminder_target", "")),
                     briefing=bool(_cfg(ctx, "briefing_on", True)), briefing_at=str(_cfg(ctx, "briefing_at", "17:00")))


def _profile_from_config(ctx) -> OwnerProfile:
    groups = groups_from_config(_cfg(ctx, "groups", {}))
    default = str(_cfg(ctx, "default_group", "Work")).strip().lower()
    return OwnerProfile(name=_cfg(ctx, "owner_name", ""), home_zone=_cfg(ctx, "home_zone", "America/Los_Angeles"),
                        agent_address=_cfg(ctx, "agent_address", ""), owner_addresses=list(_cfg(ctx, "owner_addresses", [])),
                        write_calendar=_cfg(ctx, "write_calendar", ""), read_calendars=list(_cfg(ctx, "read_calendars", [])),
                        groups=groups, default_group=default if default in groups else next(iter(groups)),
                        allowlist={k.lower(): int(v) for k, v in dict(_cfg(ctx, "allowlist", {})).items()},
                        auto_book=bool(_cfg(ctx, "auto_book", True)),
                        max_len_without_owner_min=int(_cfg(ctx, "max_len_without_owner_min", 59)),
                        notice_hours=int(_cfg(ctx, "notice_hours", 24)), hold_expiry_hours=int(_cfg(ctx, "hold_expiry_hours", 48)),
                        max_open_holds=int(_cfg(ctx, "max_open_holds", 12)), base_address=str(_cfg(ctx, "base_address", "")),
                        default_travel_min=int(_cfg(ctx, "default_travel_min", 30)), reminders=_reminders_from_config(ctx))


def _build_runtime(ctx) -> dict:
    data = _data_dir(ctx)
    mode = _cfg(ctx, "mode", "fixture")
    dispatch = getattr(ctx, "dispatch_tool", None)
    if mode == "fixture" or not _cfg(ctx, "owner_name", ""):
        profile, cal, mail, people = fx.profile(), fx.FixtureCalendar(), fx.FixtureMail(), fx.people()
        outbox = Outbox(data / "outbox.jsonl")             # fixture: reminders are recorded, never sent
    else:
        profile = _profile_from_config(ctx)
        from freebusy.adapters.google_workspace import GoogleWorkspaceAdapter
        cal = GoogleWorkspaceAdapter(profile.write_calendar, profile.read_calendars, dispatch)
        mail, people = fx.FixtureMail(), {}   # mail transport is the next step; see README
        outbox = HermesCron(dispatch) if dispatch else Outbox(data / "outbox.jsonl")
    ledger = HoldLedger(data / "holds.json", expiry_hours=profile.hold_expiry_hours, max_open=profile.max_open_holds)
    gate = Gate(profile, ledger, data / "audit.jsonl")
    sched = Scheduler(profile, cal, mail, ledger, gate, people, outbox=outbox)
    return {"profile": profile, "ledger": ledger, "gate": gate, "scheduler": sched, "mail": mail, "cal": cal, "data": data, "mode": mode,
            "ctx": ctx, "outbox": outbox}


def _rt() -> dict:
    return _RT


def _now() -> datetime:
    return fx.NOW if _RT.get("mode", "fixture") == "fixture" else datetime.now(timezone.utc)


def _email_from_args(a: dict) -> Email:
    return Email(thread_id=a.get("thread_id", ""), message_id=a.get("message_id", ""), sender=a.get("sender", ""),
                 to=list(a.get("to", [])), cc=list(a.get("cc", [])), subject=a.get("subject", ""), body=a.get("body", ""),
                 date=a.get("date", ""), headers=dict(a.get("headers", {})), sender_name=a.get("sender_name", ""))


# ---- tool handlers (always return a JSON string) ------------------------------

def schedule_parse(args: dict, **kwargs) -> str:
    try:
        rt = _rt()
        email = _email_from_args(args)
        thread = [_email_from_args(m) for m in args.get("thread", [])]
        offered = len(rt["ledger"].for_thread(email.thread_id))
        req = parse_email(email, rt["profile"], thread, _now(), offered_letters=offered)
        return json.dumps(to_dict(req), default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def schedule_offer(args: dict, **kwargs) -> str:
    try:
        rt = _rt()
        sched: Scheduler = rt["scheduler"]
        person = sched.person_for(args.get("sender", ""))
        if args.get("person_zone"):
            return json.dumps({"error": "a zone is set from the person's own words through schedule_parse, not passed in"})
        group = rt["profile"].group_for(person)
        result = build_offer(rt["profile"], group, sched.busy(_now()), rt["ledger"], args.get("thread_id", ""), _now(),
                             length_min=args.get("length_min"), person_zone=person.zone)
        return json.dumps({"slots": to_dict(result.slots), "needs_owner": result.needs_owner, "length_min": result.length_min,
                           "note": "slots are not held until schedule_holds hold is called"})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def schedule_check(args: dict, **kwargs) -> str:
    try:
        rt = _rt()
        sched: Scheduler = rt["scheduler"]
        person = sched.person_for(args.get("sender", ""))
        group = rt["profile"].group_for(person)
        proposed = [Slot(s["start"], s["end"]) for s in args.get("slots", [])]
        checks = check_proposed(rt["profile"], group, sched.busy(_now()), rt["ledger"], args.get("thread_id", ""), proposed, _now())
        return json.dumps([{"slot": to_dict(c.slot), "ok": c.ok, "reason": c.reason} for c in checks])
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def schedule_book(args: dict, **kwargs) -> str:
    """Books only through the gate; the pre_tool_call hook has already run by the time this is called."""
    try:
        rt = _rt()
        slot = Slot(args["slot"]["start"], args["slot"]["end"])
        if not rt["ledger"].find(args.get("thread_id", ""), slot):
            return json.dumps({"error": "no held slot for this thread; offer and hold first"})
        verdict = rt["gate"].pre_tool_call("schedule_book", args)
        if verdict:
            return json.dumps({"blocked": verdict["message"]})
        event_id = rt["cal"].create_event(args.get("calendar", ""), slot, args.get("title", "Meeting"), list(args.get("attendees", [])))
        rt["ledger"].book(args.get("thread_id", ""), slot)
        return json.dumps({"event_id": event_id, "holds_cleared": True})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def schedule_holds(args: dict, **kwargs) -> str:
    try:
        rt = _rt()
        led: HoldLedger = rt["ledger"]
        action = args.get("action", "list")
        if action == "list":
            led.expire(_now())
            return json.dumps({"holds": to_dict(led.open())})
        if action == "hold":
            slots = [Slot(s["start"], s["end"]) for s in args.get("slots", [])]
            held = led.hold_offer(slots, args["thread_id"], args.get("person_id", args.get("sender", "")), _now())
            return json.dumps({"held": to_dict(held)})
        if action == "release":
            return json.dumps({"released": to_dict(led.release(args["thread_id"]))})
        return json.dumps({"error": f"unknown action {action}"})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def _resume_rule_key(thread_id: str, instruction: str) -> str:
    """One approval per waiting request and instruction, so an "always" answer never covers a later gate."""
    p = _rt()["scheduler"].pending.get(thread_id)
    kind = parse_instruction(instruction or "")
    what = f"{kind[0]}{kind[1] or ''}" if kind else "-"
    return f"{RESUME_RULE}:{thread_id}:{p.request.message_id if p else '-'}:{what}"


def schedule_resume(args: dict, **kwargs) -> str:
    """Runs only after the human approved it in Hermes: pre_tool_call returns {"action": "approve"} for this tool,
    and post_approval_response records the person's yes. Without that record nothing resumes."""
    try:
        rt = _rt()
        tid, instruction = args.get("thread_id", ""), args.get("instruction", "")
        seen = rt.setdefault("approvals", {}).pop(_resume_rule_key(tid, instruction), None)
        if not seen:
            return json.dumps({"blocked": "the owner's approval was not observed; ask the owner to use /schedule "
                                          f"{tid} <instruction> or to reply to the note by email"})
        act = rt["scheduler"].resume(tid, instruction, _now(),
                                     approved_by=f"owner via Hermes approval ({seen.get('surface', '?')}, {seen.get('choice')})")
        return json.dumps({"kind": act.kind, "reason": act.reason, "text": act.text or act.owner_note or ""})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# ---- hooks ----------------------------------------------------------------------

def _pre_tool_call(tool_name: str = "", args: dict | None = None, task_id: str = "", **kwargs):
    try:
        if tool_name == "schedule_resume":
            return _resume_directive(args or {})
        return _rt()["gate"].pre_tool_call(tool_name=tool_name, args=args or {}, task_id=task_id)
    except Exception as exc:
        return {"action": "block", "message": f"freebusy policy could not decide, so the call is blocked: {exc}"}


def _resume_directive(args: dict) -> dict:
    """schedule_resume always goes to the human: block when nothing waits, else ask Hermes for approval."""
    rt = _rt()
    tid, instruction = args.get("thread_id", ""), args.get("instruction", "")
    p = rt["scheduler"].pending.get(tid)
    if p is None:
        verdict = {"action": "block", "message": f"nothing on {tid} is waiting for the owner"}
    elif not parse_instruction(instruction):
        from freebusy.engine import accepted_instructions
        verdict = {"action": "block", "message": accepted_instructions(tid, p)}
    else:
        verdict = {"action": "approve", "rule_key": _resume_rule_key(tid, instruction),
                   "message": f"freebusy: {p.detail} on {tid}. Owner instruction: '{instruction.strip()[:60]}'"}
    rt["gate"].audit({"tool": "schedule_resume", "thread": tid, "verdict": verdict["action"], "reason": verdict["message"]})
    return verdict


def _post_approval_response(pattern_key: str = "", choice: str = "", surface: str = "", decided_by: str = "", **kwargs):
    """Record a person's yes to a schedule_resume approval. Smart (model) approvals do not count."""
    try:
        key = str(pattern_key or "")
        if not key.startswith(f"plugin_rule:{RESUME_RULE}:"):
            return None
        rule = key[len("plugin_rule:"):]
        human_yes = choice in _HUMAN_YES and not decided_by
        if human_yes:
            _rt().setdefault("approvals", {})[rule] = {"choice": choice, "surface": surface}
        _rt()["gate"].audit({"approval": rule, "choice": choice, "surface": surface, "decided_by": decided_by or "human",
                             "counted": human_yes})
    except Exception as exc:  # observers never raise
        logger.debug("freebusy post_approval_response: %s", exc)
    return None


def _post_tool_call(tool_name: str = "", args: dict | None = None, result: str = "", task_id: str = "", duration_ms: int = 0, **kwargs):
    try:
        if tool_name.startswith("schedule_") or tool_name in ("send_email", "calendar_create_event"):
            _rt()["gate"].audit({"tool": tool_name, "thread": (args or {}).get("thread_id"), "ms": duration_ms,
                                 "ok": not str(result).startswith('{"error')})
    except Exception as exc:  # observers never raise
        logger.debug("freebusy post_tool_call: %s", exc)


def _pre_llm_call(session_id: str = "", user_message: str = "", **kwargs):
    try:
        rt = _rt()
        led: HoldLedger = rt["ledger"]
        led.expire(_now())
        holds = led.open()
        lines = ["freebusy facts for this turn:",
                 f"- owner zone {label_for(rt['profile'].home_zone)}; never show a time in a zone the person did not state",
                 f"- open holds: {len(holds)} (a held slot is never offered to another thread)"]
        for h in holds[:12]:
            lines.append(f"  - {h.thread_id}: {h.slot.start} to {h.slot.end}, expires {h.expires}")
        zones = [(p.name, label_for(p.zone)) for p in rt["scheduler"].people.values() if p.zone and p.zone_source == "stated"]
        if zones:
            lines.append("- stated zones: " + ", ".join(f"{n} {z}" for n, z in zones))
        for tid, p in rt["scheduler"].pending.items():
            lines.append(f"- waiting on the owner: {tid} ({p.reason}: {p.detail}); the owner clears it with "
                         f"schedule_resume, /schedule {tid} <instruction>, or an email reply")
        return {"context": "\n".join(lines)}
    except Exception as exc:
        logger.debug("freebusy pre_llm_call: %s", exc)
        return None


# ---- slash commands -----------------------------------------------------------

def _show(act) -> str:
    return f"{act.kind}: {act.reason}\n\n{act.text or act.owner_note or ''}".rstrip()


def _cmd_schedule(raw_args: str = "", **kwargs) -> str:
    rt = _rt()
    parts = (raw_args or "").split()
    if not parts or parts[0] == "help":
        return ("/schedule setup                  set up, one question per message (setup <answer>, skip, redo)\n"
                "/schedule groups                 list groups and their rules\n"
                "/schedule group <name> <setting> <value>\n"
                "                                 settings: windows, buffer, length, earliest, horizon, tier, visibility,\n"
                "                                 members, domains, travel\n"
                "/schedule reminders              reminders scheduled for you (fixture: the outbox)\n"
                "/schedule demo                   run the fixture scenes\n"
                "/schedule profile                show the owner profile\n"
                "/schedule waiting                threads waiting on you, and why\n"
                "/schedule approve <thread>       approve what is waiting and finish it now\n"
                "/schedule <thread> <instruction> approve | yes | go ahead | offer <minutes> | decline\n"
                "/schedule run <scene>            handle one fixture thread")
    if parts[0] == "setup":
        return _cmd_setup((raw_args or "").strip()[len("setup"):].strip())
    if parts[0] == "groups":
        ctx = rt.get("ctx")
        note = "" if rt["mode"] != "fixture" else "\n(Fixture mode: these are your saved groups; the demo runs on Dana's fixture groups.)"
        return describe(groups_from_config(_cfg(ctx, "groups", {})), str(_cfg(ctx, "default_group", "Work")).lower()) + note
    if parts[0] == "group":
        return _cmd_group(parts[1:])
    if parts[0] == "reminders":
        jobs = getattr(rt.get("outbox"), "jobs", None)
        if jobs is None:
            return "reminders are Hermes scheduled jobs; see /cron list (names start with 'freebusy')"
        return "\n\n".join(f"{j.kind} at {j.at} via {j.deliver} ({j.thread_id})\n{j.text}" for j in jobs) or "no reminders yet"
    if parts[0] == "profile":
        return json.dumps(to_dict(rt["profile"]), indent=1)
    if parts[0] == "waiting":
        pend = rt["scheduler"].pending
        return "\n".join(f"{t}  {p.reason}: {p.detail}" for t, p in pend.items()) or "nothing is waiting on you"
    if parts[0] == "approve" and len(parts) > 1:
        if parts[1] in rt["scheduler"].pending:
            return _show(rt["scheduler"].resume(parts[1], "approve", _now(), approved_by="owner via /schedule approve"))
        rt["gate"].facts(parts[1]).owner_approved = True       # nothing waits yet: approve the thread's booking ahead of time
        rt["gate"].audit({"thread": parts[1], "owner_approved": True, "approved_by": "owner via /schedule approve"})
        return f"approved: nothing was waiting on {parts[1]}; its booking may go ahead when they pick"
    if parts[0] == "demo":
        from freebusy.demo import run_scenes
        return run_scenes(quiet=True)
    if parts[0] == "run" and len(parts) > 1 and rt["mode"] == "fixture":
        return _show(rt["scheduler"].handle(rt["mail"].thread(parts[1]), _now()))
    if len(parts) > 1 and parts[0] not in ("demo", "run", "approve", "setup", "group", "groups", "reminders"):
        return _show(rt["scheduler"].resume(parts[0], " ".join(parts[1:]), _now(), approved_by="owner via /schedule"))
    return "unknown subcommand; try /schedule help"


def _setup_flow() -> SetupFlow:
    rt = _rt()
    ctx = rt.get("ctx")
    if rt.get("mode", "fixture") == "fixture":
        calendars, fixture = [fx.WORK, fx.PERSONAL, fx.FAMILY], True
    else:
        calendars = [c for c in [_cfg(ctx, "write_calendar", ""), *(_cfg(ctx, "read_calendars", []) or [])] if c]
        fixture = False
    return SetupFlow(Path(rt.get("data") or _data_dir(ctx)) / "setup.json", lambda k, d: _cfg(ctx, k, d),
                     lambda k, v: _set_cfg(ctx, k, v), calendars, fixture)


def _cmd_setup(rest: str) -> str:
    """/schedule setup resumes where you left off; /schedule setup <answer> answers; skip keeps the default; redo restarts."""
    try:
        flow = _setup_flow()
        if not rest:
            return flow.current()
        if rest.lower() == "redo":
            return flow.redo()
        out = flow.answer(rest)
        _rt()["gate"].audit({"setup": flow.steps[min(flow.state["step"], len(flow.steps) - 1)].key, "done": flow.done})
        return out
    except Exception as exc:
        return f"setup could not continue: {exc}"


def _cmd_group(args: list[str]) -> str:
    """/schedule group <name> <setting> <value>: one edit, saved to config (and to the live profile outside fixture mode)."""
    rt = _rt()
    ctx = rt.get("ctx")
    if len(args) < 3:
        return "usage: /schedule group <name> <setting> <value>, for example /schedule group Work windows mon-fri 9:00-17:00"
    groups = groups_from_config(_cfg(ctx, "groups", {}))
    try:
        said = set_setting(groups, args[0], args[1], " ".join(args[2:]))
    except (ValueError, IndexError) as exc:
        return f"not saved: {exc}"
    _set_cfg(ctx, "groups", groups_to_config(groups))
    if rt.get("mode") != "fixture":
        rt["profile"].groups = groups
    rt["gate"].audit({"group_setting": said})
    return said


def _cmd_holds(raw_args: str = "", **kwargs) -> str:
    led: HoldLedger = _rt()["ledger"]
    led.expire(_now())
    if not led.open():
        return "no open holds"
    return "\n".join(f"{h.thread_id}  {h.slot.start} to {h.slot.end}  expires {h.expires}" for h in led.open())


def _cmd_zones(raw_args: str = "", **kwargs) -> str:
    if raw_args.strip():
        g = detect_zone(raw_args)
        return (f"{g.label} ({g.zone}), {g.source}, confidence {g.confidence:.2f}, from \"{g.evidence}\"" if g
                else "no zone stated in that text; a header offset would be a hint only")
    people = _rt()["scheduler"].people.values()
    rows = [f"{p.name}: {label_for(p.zone) if p.zone else 'not stated'}{' (' + p.zone_source + ')' if p.zone else ''}" for p in people]
    return "\n".join(rows) or "no people yet"


# ---- registration ---------------------------------------------------------------

def register(ctx) -> None:
    global _RT
    _load_overlay(ctx)
    try:
        _RT = _build_runtime(ctx)
    except Exception as exc:
        logger.warning("freebusy: runtime failed to build, falling back to fixture mode: %s", exc)
        _RT = {"profile": fx.profile(), "ledger": HoldLedger(), "mode": "fixture"}
        _RT["gate"] = Gate(_RT["profile"], _RT["ledger"])
        _RT["mail"], _RT["cal"] = fx.FixtureMail(), fx.FixtureCalendar()
        _RT["scheduler"] = Scheduler(_RT["profile"], _RT["cal"], _RT["mail"], _RT["ledger"], _RT["gate"], fx.people())
        _RT["ctx"], _RT["outbox"] = ctx, _RT["scheduler"].outbox

    tools = (("schedule_parse", schemas.SCHEDULE_PARSE, schedule_parse), ("schedule_offer", schemas.SCHEDULE_OFFER, schedule_offer),
             ("schedule_check", schemas.SCHEDULE_CHECK, schedule_check), ("schedule_book", schemas.SCHEDULE_BOOK, schedule_book),
             ("schedule_holds", schemas.SCHEDULE_HOLDS, schedule_holds),
             ("schedule_resume", schemas.SCHEDULE_RESUME, schedule_resume))
    if hasattr(ctx, "register_tool"):
        for name, schema, handler in tools:
            try:
                ctx.register_tool(name=name, toolset="freebusy", schema=schema, handler=handler)
            except Exception as exc:
                logger.warning("freebusy: could not register %s: %s", name, exc)
    if hasattr(ctx, "register_hook"):
        for hook, fn in (("pre_tool_call", _pre_tool_call), ("post_tool_call", _post_tool_call), ("pre_llm_call", _pre_llm_call),
                         ("post_approval_response", _post_approval_response)):
            try:
                ctx.register_hook(hook, fn)
            except Exception as exc:
                logger.warning("freebusy: could not register hook %s: %s", hook, exc)
    if hasattr(ctx, "register_command"):
        for name, fn, desc in (("schedule", _cmd_schedule, "Scheduling assistant: setup, groups, group, reminders, waiting, approve, <thread> <instruction>, demo"),
                               ("holds", _cmd_holds, "Open slot holds"), ("zones", _cmd_zones, "Stated time zones, or test a phrase")):
            try:
                ctx.register_command(name, handler=fn, description=desc)
            except Exception as exc:
                logger.warning("freebusy: could not register /%s: %s", name, exc)
    logger.info("freebusy %s registered in %s mode", __version__, _RT.get("mode"))
