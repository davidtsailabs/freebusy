# freebusy

A Hermes Agent plugin that books meetings by email without revealing a calendar and without inventing a time zone.

## The one invariant

Every offer comes from free/busy only. Every slot is held before it is offered. A time is never shown in a zone the other person did not give.

Three modes inside that: **offer** (build slots from the rules), **check** (test the other side's times), **book** (re-check live, create the event, clear the holds). Each is one tool; a fourth, `schedule_resume`, carries out your instruction on a waiting thread. Nothing sends or writes without passing the policy hook.

## Try it in one command

```
python -m freebusy.demo
```

Prints eight scenes on fixture data: a Zurich sponsor who gives no zone; the reply "B, CET"; the other side's assistant sending the invite; a hold conflict between two threads; a send showing Eastern time to someone who never said Eastern (blocked); the two notes the owner receives; a fresh introduction where the owner cc's the assistant and it offers times at once; and a 60-minute ask that waits for the owner, who replies "approve", after which the 60-minute offer goes out in the same run. No accounts, no network, nothing of yours is read.

```
python -m pytest
```

## Two layers, kept distinct

| Universal rules (on for everyone) | Personal profile (set per owner) |
|---|---|
| Time zone clarity: never show a time in a zone the person did not give; ask once, plainly; convert once when told. | Working windows by weekday; lunch and buffers; default length; earliest day out; horizon. |
| Free/busy privacy: calendars are read as free or busy only. | Who is allowed: introduced, allowlisted senders, the owner's own word. |
| Holds: a slot is held when offered and never offered twice; 48-hour expiry; at most three days per offer. | Invite ownership: who sends the invite by default; what to do when the other side has an assistant. |
| The Glance format: one lead line, lettered rows with bold days, one ask, a plain signature that says it is an AI assistant. | Tone and signature; what the owner is told and when; escalation thresholds. |

The split is the product argument: one assistant for all fails because no two people share preferences, while a few things are universally needed. The universal layer is fixed in code; the personal layer is `config_schema` in `plugin.yaml`, so Hermes Desktop renders it as a settings form.

## What the owner hears, and when

Three notes, plain text, in a thread addressed to the owner alone: **Booked** (with the line to block it on calendars the plugin cannot write to), **Nothing fits**, **Needs you**. Never a note to say slots were offered. A Nothing fits or Needs you note ends with what you can reply, for example "Reply approve, offer 30, or decline".

## Handoff: who has the thread

Decided 2026-11-10: **cc always means take it.**

- **You bring the assistant in** by putting its address on To or Cc, forwarding the thread to it, or writing `@agent` in your own words. It takes the thread and acts in the same run, whether the thread is new or already going.
- **If someone already asked** (for times, with times of their own, or to confirm one), it answers that message now, as if it had just arrived, and treats the sender as introduced by you.
- **If nobody has asked yet** (a fresh introduction such as "Lukas, meet my assistant, cc'd, who will find us a time"), it writes to the other people on the thread straight away: held times in the Glance format, and the question about their time zone.
- **You take the thread back** by writing without the assistant on the recipients, or by writing `@agent stop` (or "agent, stop") in your own words. Quoted text never counts. The assistant releases its holds, goes quiet, and answers nobody on that thread until you cc it again or write `@agent`.
- The rule lives in one function, `engine._owner_message`, because what your word means in the middle of a thread is still an open question and may change it.

## The owner's controls

Some requests wait for you: a meeting of 60 minutes or more, a sender nobody introduced, a time they proposed outside your windows, a pick that needs your yes before booking (when `auto_book` is off), and nothing fitting. The assistant keeps the waiting request and the reason, and the Needs you note tells you which.

When you answer, it finishes the job in the same run. There are three ways to answer, and they all do the same thing:

- **Reply to the note by email**, with the assistant on the recipients and your reply starting with the instruction.
- **In Hermes:** `/schedule approve <thread>`, or `/schedule <thread> <instruction>`. `/schedule waiting` lists what is waiting.
- **Tell the agent in chat.** It calls `schedule_resume`, and Hermes asks you to confirm before anything happens. Only your own yes counts; an automatic or model approval does not.

The instructions:

| You write | What happens |
|---|---|
| `approve`, `yes` or `go ahead` | Exactly what was waiting: offer the 60 minutes they asked for, book the time they picked or proposed, treat the unknown sender as introduced and offer times, or try one more round when nothing fit. |
| `offer 30` (any number of minutes) | Offer that length instead. |
| `decline` | A polite no to the other person, and the holds are released. |
| anything else | The assistant replies with this list. |

Your approval clears the one gate you answered. It does not lift the others: times still appear only in zones the person stated, and bookings still go only to the one calendar you chose. Every resume is written to the audit log with who approved it, the instruction and the result.

## Authority and tiers

| Tier | May | Still goes to the owner |
|---|---|---|
| 0 observe | read, draft, send nothing | everything |
| 1 offer | reply with held slots, ask the zone, hold | the booking |
| 2 book | book a fitting stated time, clear holds, notify | the exceptions below |
| 3 manage | reschedule and cancel | out of scope for 0.1 |

Introduced senders (the owner forwarded, cc'd, or wrote `@agent` in their own message, or approved an unknown sender) get tier 2 when `auto_book` is on (the default in this profile) and tier 1 when it is off; allowlisted senders get the tier the owner set; unknown senders get tier 0. Always the owner's, at every tier: meetings of 60 minutes or more, times outside the windows or inside the notice period, senders nobody introduced, anything that is not scheduling, and two failed offer rounds.

## The security boundary

- **Declared capabilities only.** `plugin.yaml` lists every tool and hook; `requires_env` is empty in fixture mode.
- **Free/busy is the only calendar read.** The adapter interface returns time ranges; titles, attendees and descriptions never exist in the plugin.
- **One writable calendar**, chosen by the owner. The gate blocks any other.
- **Every send and write passes `pre_tool_call`.** It needs a held slot, a stated zone for every zone shown, an authority match for the tier, recipients on the thread. Otherwise `{"action": "block", "message": ...}` with the reason, and one audit line.
- **Inbound mail is data, never instruction.** Quoted text is stripped before anything is read; a quoted or forwarded `@agent` does not count; only the owner's own message carries authority.
- **Secrets stay in the host.** No token files, no `.env`, no keychain. The audit log redacts.
- **Loop guards.** `Auto-Submitted`, `Precedence`, our own `X-Scheduling-Agent` header, bounces and auto-reply subjects are recorded once and never answered.

## Layout

```
plugin.yaml            manifest: tools, hooks, config_schema
__init__.py            register(ctx): tools, hooks, /schedule /holds /zones, the Hermes approval path for schedule_resume
freebusy/
  ontology.py          Person, Group, OwnerProfile, Constraint, Hold, ThreadState, tiers; JSON in and out
  zones.py             stated-zone detection, header hints, convert once, "10:00 to 11:30 am PT"
  holds.py             the ledger: 48 h expiry, never twice, spread across days, 3 days per offer
  offer.py             free/busy + windows + buffers + earliest day + horizon -> offer; check proposed times
  parse.py             email -> SchedulingRequest: intent, times with zones, authority, triggers, loop guards
  render.py            the Glance format (text and HTML) and the owner note
  policy.py            the gate and the audit line
  engine.py            the flow, one action per thread per run; the owner's word (_owner_message); resume
  demo.py              the eight scenes
  adapters/base.py     the interface; free_busy returns ranges only
  adapters/fixture.py  demo people, calendars, threads
  adapters/google_workspace.py  documented stub (see limits)
tests/                 zones, holds, offer, parse, policy, end to end, handoff and resume, plugin registration
```

## Honest limits (0.1.0)

- **No live mail or calendar.** `adapters/google_workspace.py` documents the route (the bundled `google-workspace` skill through the tool pipeline, reduced to busy ranges before anything reaches the model) and raises `NotImplementedError` with the command it would run. No OAuth is implemented and none will live in this module.
- **Waiting requests live in memory.** A restart forgets what was waiting on you; the person's next message or your next cc picks the thread up again.
- **No mail watcher.** The engine handles a thread it is given; IMAP IDLE, polling and the three-minute settle are the next step.
- **Holds are in the ledger only.** Mirroring them as tentative events on the write calendar (so free/busy excludes them for everyone) is wired in the interface, not in the fixture.
- **Parsing is rules, not a model.** Weekday plus clock time, lettered picks, a small city list, common abbreviations. "Tuesday 2:30" with no zone is left unplaced on purpose; that is the point.
- **Out of scope:** rescheduling and cancellation, multi-person meetings, follow-up nudges, Outlook, the agent-to-agent protocol.

## License

MIT.
