# freebusy

A Hermes Agent plugin that books meetings by email or chat, on any calendar and any mailbox, without revealing a calendar and without inventing a time zone.

## The one invariant

Every offer comes from free/busy only. Every slot is held before it is offered. A time is never shown in a zone the other person did not give.

Three modes inside that: **offer** (build slots from the rules), **check** (test the other side's times), **book** (re-check live, create the event, clear the holds). Each is one tool; a fourth, `schedule_resume`, carries out your instruction on a waiting thread. Nothing sends or writes without passing the policy hook.

The one exception to "free/busy only" is yours to switch on: travel buffers (below) also read the location and video-link fields of your events, on your machine, and reduce them to "in person, 40 minutes" or "virtual" before anything else sees them.

## Connectors

| Connector | Status | Works with |
|---|---|---|
| CalDAV calendars | shipped in 0.3 | iCloud, Fastmail, Nextcloud, Zoho, any standard CalDAV server, with an app password |
| IMAP + SMTP mail | shipped in 0.3 | any mailbox that takes an app password (iCloud, Fastmail, Zoho, Gmail, your own domain) |
| Chat channels | shipped in 0.3 | Hermes' own gateway: Telegram, WhatsApp, iMessage, SMS and the rest it serves |
| Local hold store | shipped in 0.3 | holds and bookings kept on your machine, written to a calendar feed file any calendar app can subscribe to |
| Microsoft Graph (calendar and mail) | coming in 0.4 | Microsoft 365 and Outlook.com, with your own app registration |
| Google Calendar and Gmail sign-in | coming in 0.4 | the Google adapter switched on; Gmail already works today over IMAP with an app password |
| Teams and Meet links per meeting | coming in 0.4 | until then, a fixed link of your choice (below) |

Mix them freely: a work calendar on one server, a family calendar on another, mail from a third account, chat from Hermes.

## Try it in one command

```
python -m freebusy.demo
```

Prints thirteen scenes on fixture data. No accounts, no network, nothing of yours is read.

1. A Zurich sponsor who gives no zone.
2. The reply "B, CET", and the booking.
3. The other side's assistant sends the invite.
4. A hold conflict between two threads.
5. A send showing Eastern time to someone who never said Eastern (blocked).
6. The notes the owner receives.
7. A fresh introduction: the owner cc's the assistant and it offers times at once.
8. A 60-minute ask waits for the owner, who replies "approve"; the 60-minute offer goes out in the same run.
9. Trial on day one: a new user with no setup. The assistant offers held times on an introduction, the pick waits for the owner, a stranger gets no reply, and the owner's "approve" books it.
10. Groups: a Family request sees evenings and weekends, a Work request sees weekdays 9 to 5.
11. A travel buffer pushes an offer from 8:00 am to 1:30 pm, around an in-person meeting 40 minutes away.
12. Reminders created at booking: two on the event, a text and a briefing for the owner, none for the other person.
13. The day-before briefing.

```
python -m pytest
```

## Connecting your calendars and mail

```
/freebusy connect you@example.com
```

One step per message, each answered with `/freebusy connect <answer>` (`/freebusy connect` alone repeats the current step):

1. **Which connectors.** It guesses the provider from your domain (iCloud, Fastmail, Zoho, Gmail, Outlook) and, for your own domain, checks `https://yourdomain/.well-known/caldav`. You pick `calendar`, `mail` or `calendar mail`. A guess is never final: `provider fastmail`, `server caldav https://cloud.example.com/remote.php/dav/` or `server mail imap.example.com:993 smtp.example.com:465` changes it.
2. **The app password.** Make one at your provider, then run in a terminal on this machine:
   ```
   hermes freebusy password caldav:you@example.com
   ```
   It asks without showing what you type and stores the password in the system keychain (the macOS Keychain, or the Secret Service on Linux). It is never typed in chat, never written to config and never logged. A password pasted into chat is refused and not repeated. Then `/freebusy connect next`. (Without the Hermes CLI, the same prompt is `python -m freebusy.keychain set caldav:you@example.com`, run from the plugin folder with the plugin's Python.)
3. **Which calendars to read.** A numbered list of every calendar the account can see, marked read-only where it is. `read 1 3` or `read all`. Only start and end times are read.
4. **Where holds go.** One writable calendar (`hold 2`), or `hold local` for the local hold store. If none of your calendars is writable, the local store is offered on its own.
5. **A meeting link.** `link https://meet.jit.si/your-room` (a Zoom personal room, Jitsi, Whereby or any fixed https link) or `link none`.
6. **The test.** It reads a week of busy times from each calendar, makes one hold on the hold calendar and removes it, and signs in to IMAP and SMTP without sending. Only when everything passes does it switch the plugin to connectors mode.

Run it again with another address to add a second account; the calendar list then covers both. `/freebusy doctor` repeats the checks at any time: servers, the network rules, which connections have a keychain password, where holds go, the meeting link, chat contacts, and the same read-hold-remove test.

### Read many, hold on one

Busy time is the union of every calendar you chose, across accounts. Holds and booked meetings go to exactly one place. The policy gate and the adapter both refuse any other calendar, and the adapter deletes only events the plugin created there.

A hold is a TENTATIVE, TRANSPARENT event: you see it in your calendar, and it does not count twice against your free time (the hold ledger already keeps a held slot from being offered again). It disappears when the offer is booked, released or expires. A booked meeting is a normal busy event with the other person invited.

With `hold local`, the events live in `local-calendar.json` in the plugin's data folder and are written to `feed/freebusy.ics` on every change. Subscribe to that file in your calendar app (Apple Calendar: File, New Calendar Subscription, with the file's path), or serve it however you like. Booked meetings in the local store count as busy.

### Free/busy on CalDAV

Where the server supports it, busy time comes from a `free-busy-query` report, so the server sends busy periods and nothing else. Where it does not, the adapter fetches the events with recurrences expanded by the server, keeps start, end, status and transparency, and drops everything else before returning. Cancelled, transparent and all-day events do not block.

### Mail

The mailbox is opened read-only and messages are fetched without marking them read. Only threads with the assistant's address on a message are parsed past their headers. Replies go out as replies in the same thread from the assistant's address, with the `X-Scheduling-Agent` header. Notes for you (Booked, Nothing fits, Needs you) are emailed to your own address and kept in `notes.jsonl`.

The plugin does not poll on its own. `/freebusy check` handles new scheduling mail now; to run it every few minutes, schedule that command with Hermes' own cron. Sign-in is an app password; the adapter already speaks XOAUTH2 for IMAP and SMTP, and getting the token for Gmail and Outlook is 0.4.

### Chat

Scheduling requests can arrive over any channel Hermes' gateway serves. List the people who may schedule this way, keyed by platform and user id:

```yaml
chat_contacts:
  telegram:123456789: {email: lukas@alpineventures.example, name: Lukas Brenner}
  whatsapp:+41790000000: Lukas Brenner
```

Listing someone is your introduction. Their messages go through the same engine, rules, tiers, holds and gate as mail, in a thread of their own (`chat:<platform>:<chat id>`), and the reply goes back on the same chat with one line first: "(Dana's scheduling assistant, an AI, is answering.)". Anything that waits for you gets no reply in the chat; you get the note. Messages from anyone else, you included, and small talk from a contact go to the agent exactly as before. A contact with no email is never mailed an invite.

It uses Hermes' `post_gateway_admission` hook, so it only ever sees senders Hermes has already admitted, after its own authorization and slash commands.

### Meeting links

Asked at connect. The link goes on every meeting the assistant books (in the event and in the "Booked" reply) and in your briefing. It must be https and a real host name. Per-meeting Teams and Meet links come with the Microsoft and Google connectors in 0.4.

## Two layers, kept distinct

| Universal rules (on for everyone) | Personal profile (set per owner) |
|---|---|
| Time zone clarity: never show a time in a zone the person did not give; ask once, plainly; convert once when told. | Groups, each with its own windows by weekday, buffer, default length, earliest day, horizon, tier and members. |
| Free/busy privacy: calendars are read as free or busy only (travel buffers are the one opt-in exception). | Who is allowed: introduced, allowlisted senders, the owner's own word. |
| Holds: a slot is held when offered and never offered twice; 48-hour expiry; at most three days per offer. | Invite ownership: who sends the invite by default; what to do when the other side has an assistant. |
| The Glance format: one lead line, lettered rows with bold days, one ask, a plain signature that says it is an AI assistant. | Reminders: the channel for texts, the times, the briefing. |
| Reminders go to the owner only. | Tone and signature; what the owner is told and when; escalation thresholds. |

The universal layer is fixed in code. The personal layer is `config_schema` in `plugin.yaml`, so Hermes Desktop shows it as a settings form; `/schedule setup` fills in the rules by chat and `/freebusy connect` the connectors.

## Setting it up

`/schedule setup` asks one question per message and saves each answer to config as soon as you give it:

1. Your name and home time zone.
2. How the assistant sends mail: a plus address (you+agent@yourdomain, the default), your same address with every message marked as the assistant's, or an alias.
3. The one calendar it may write to. It reads the others as free or busy only. In fixture mode it lists the fixture calendars; with connectors, `/freebusy connect` sets this.
4. Work hours (default weekdays 9:00 to 17:00).
5. Family hours (default every day 7:00 to 21:00).
6. The default meeting length (default 30 minutes).
7. Auto-book on or off.
8. Where reminder texts go: Telegram, SMS, iMessage or email, or off.
9. Travel buffers: none, Work, Family or both, and your base address if on.

Reply with `/schedule setup <answer>`. `/schedule setup` on its own asks the current question again, so you can stop and come back. `/schedule setup skip` keeps the default. `/schedule setup redo` starts over.

It never asks for a password or a key. Calendar and mail app passwords go into the system keychain from a terminal (`hermes freebusy password`); messaging credentials stay in Hermes. An answer that looks like a credential is refused, not saved and not repeated back.

## Tiers

| Tier | May | Still goes to the owner |
|---|---|---|
| 0 Trial | offer held times in a thread it was brought into, with the zone question | every booking, and any reply that is not an offer of held times |
| 1 Offer | reply with held slots, ask the zone, hold | every booking |
| 2 Book | book a fitting stated time, clear holds, tell the owner | the exceptions below |
| 3 Manage | reschedule and cancel | defined, not built |

Each group has a tier. A new install starts both groups in Trial, so the assistant shows its work on the first introduction while every booking waits for you. Finishing setup moves groups still in Trial to Book (auto-book on) or Offer (auto-book off). You can set any group's tier yourself with `/schedule group <name> tier <0-3>`.

Introduced senders (you forwarded, cc'd, wrote `@agent` in your own message, or approved an unknown sender) get Book with auto-book on and Offer with it off, never above their group's tier. Allowlisted addresses get the tier you set for them.

Whatever the tier, a sender nobody introduced never gets a reply without you. Always yours, at every tier: meetings of 60 minutes or more, times outside the windows or inside the notice period, senders nobody introduced, anything that is not scheduling, and two failed offer rounds.

## Groups

Each group has its own weekday windows (in your zone), buffer between meetings, default length, earliest day out, horizon, tier, visibility, travel-buffer switch, and the senders who belong to it, by address or by domain. Senders who match no group get the default group.

The defaults:

- **Work** (the default group): weekdays 9 AM to 5 PM, 15-minute buffer, from tomorrow, two weeks out, busy-only. When the other person has stated a zone, offers also stay inside their 9 to 5.
- **Family**: every day 7 AM to 9 PM, no buffer, full availability: every free minute inside those hours is offered, any day, without the other person's workday limit.

Edit them in Hermes Desktop (Settings, Plugins, freebusy) or by chat:

```
/schedule groups
/schedule group Work windows mon-thu 9:30-16:00; fri 9:00-12:00
/schedule group Family domains add whitfield.example
/schedule group Work travel on
/schedule group Board length 45        (a new name creates a group with Work's rules)
```

Settings: `windows`, `buffer`, `length`, `earliest`, `horizon`, `tier`, `visibility` (busy_only or full), `members`, `domains`, `travel`.

## Travel buffers

Off by default, and switched on per group. When on, the calendar adapter reads two fields of your events, locally: the location and the video link. Each event becomes one fact: in person with so many minutes of travel from your base address, or virtual. Titles, attendees and descriptions are never read for this, the location text never leaves the adapter, and nothing about where you are is said to anyone. When offers are built, in-person events get the travel time blocked before and after.

Travel minutes come from a distance table, not a maps service. The fixture has a small table (Emeryville 40 minutes, Mission Bay 20). In live use an in-person event with no table entry counts as 30 minutes each way, which you can change in settings.

## Reminders

On by default for every event the assistant books. All of them go to you, never to the other person.

- **On the event:** a pop-up 10 minutes before and an email one day before.
- **A text to you:** one hour before, on the channel you picked (Telegram, SMS, iMessage through BlueBubbles, or email).
- **A briefing the evening before** (5 PM in your zone): who, their organization and the zone they stated, what the thread was about and what they asked, how the time was agreed, who sent the invite, the link, and any open question they asked that the assistant did not answer. One short block, plain text, nothing from your other calendars.

The text and the briefing are run-once Hermes scheduled jobs, created through Hermes' own cron tool and delivered by its messaging, so the plugin holds no messaging credentials. In fixture mode they are written to `outbox.jsonl` instead. `/schedule reminders` lists them. When the other side sends the invite, you still get the text and the briefing; the event reminders are theirs to set.

Every reminder passes the policy hook, which refuses any target that is someone on the thread other than you, or an email address that is not yours.

## What the owner hears, and when

Three notes, plain text, in a thread addressed to the owner alone: **Booked** (with the line to block it on calendars the plugin cannot write to, and which reminders were set), **Nothing fits**, **Needs you**. Never a note to say slots were offered. A Nothing fits or Needs you note ends with what you can reply, for example "Reply approve, offer 30, or decline".

## Handoff: who has the thread

Decided 2026-11-10: **cc always means take it**, and **your word pauses it only when it is about the time or the booking**.

- **You bring the assistant in** by putting its address on To or Cc, forwarding the thread to it, or writing `@agent` in your own words. It takes the thread and acts in the same run, whether the thread is new or already going.
- **If someone already asked** (for times, with times of their own, or to confirm one), it answers that message now, as if it had just arrived, and treats the sender as introduced by you.
- **If nobody has asked yet** (a fresh introduction such as "Lukas, meet my assistant, cc'd, who will find us a time"), it writes to the other people on the thread straight away: held times in the Glance format, and the question about their time zone.
- **You write without the assistant, about the time or the booking** (a time, a day, a length, "move", "cancel", "not that day", "I'll handle it"): it pauses. It releases its holds and answers nobody on that thread until you cc it again or write `@agent`.
- **You write without the assistant and it is only social** ("thanks", "looking forward"): nothing changes. It keeps the thread and its holds.
- **`@agent stop`** (or "agent, stop") in your own words also hands the thread back. Quoted text never counts.

The rule lives in one function, `engine._owner_message`, so it can change in one place.

## The owner's controls

Some requests wait for you: a meeting of 60 minutes or more, a sender nobody introduced, a time they proposed outside your windows, a pick that needs your yes before booking (Trial, Offer, or auto-book off), and nothing fitting. The assistant keeps the waiting request and the reason, and the Needs you note tells you which.

When you answer, it finishes the job in the same run. There are three ways to answer, and they all do the same thing:

- **Reply to the note by email**, with the assistant on the recipients and your reply starting with the instruction.
- **In Hermes:** `/schedule approve <thread>`, or `/schedule <thread> <instruction>`. `/schedule waiting` lists what is waiting.
- **Tell the agent in chat.** It calls `schedule_resume`, and Hermes asks you to confirm before anything happens. Only your own yes counts; an automatic or model approval does not.

| You write | What happens |
|---|---|
| `approve`, `yes` or `go ahead` | Exactly what was waiting: offer the 60 minutes they asked for, book the time they picked or proposed, treat the unknown sender as introduced and offer times, or try one more round when nothing fit. |
| `offer 30` (any number of minutes) | Offer that length instead. |
| `decline` | A polite no to the other person, and the holds are released. |
| anything else | The assistant replies with this list. |

Your approval clears the one gate you answered. It does not lift the others: times still appear only in zones the person stated, and bookings still go only to the one calendar you chose. Every resume is written to the audit log with who approved it, the instruction and the result.

## The security boundary

- **Declared capabilities only.** `plugin.yaml` lists every tool and hook; `requires_env` is empty, `config_schema` has no secret fields, and the one Python dependency is `keyring`.
- **Free/busy is the only calendar read by default.** The adapter interface returns time ranges; titles, attendees and descriptions never exist in the plugin. On CalDAV they are dropped inside the adapter when a server cannot answer a free-busy query. With travel buffers on for a group, the adapter also reads location and video-link fields, locally, and hands over only "in person, N minutes" or "virtual".
- **One writable calendar**, chosen by the owner, or the local store. The gate blocks any other; the adapter deletes only events the plugin created.
- **Passwords live in the system keychain.** Typed in a terminal without echo, read only when a connection opens, sent only to the host they were set for (or its sibling under the same domain, as iCloud does). Never in config, chat, logs or tool results. The audit log and notes redact anything key-like.
- **Where it may connect.** CalDAV and feed URLs are https only; mail is TLS (implicit or STARTTLS). Every host, every redirect and every URL a server hands back is resolved and refused if any address is private, loopback, link-local, multicast or reserved, so config or a server cannot steer the plugin into your network. For the local test servers only, `FREEBUSY_ALLOW_LOCAL_TEST_SERVERS=1` in the process environment allows http, plaintext mail and loopback (never private ranges); `/freebusy doctor` warns while it is set.
- **Least privilege.** The mailbox is opened read-only and nothing is marked read; only threads with the assistant on them are parsed. Chat sees only admitted senders the owner listed.
- **Every send, write and reminder of this plugin passes `pre_tool_call`.** A send needs a held slot, a stated zone for every zone shown, an authority match for the tier, and recipients on the thread; a sender nobody introduced blocks it. A reminder must go to the owner. Otherwise `{"action": "block", "message": ...}` with the reason, and one audit line. Other plugins' tools are left alone unless you turn on `vet_other_tools`, which holds `send_email`, `email_send`, `gmail_send`, `mail_send`, `reply_email`, `calendar_create_event`, `create_event`, `calendar_write` and `book_event` to the same rules.
- **Inbound mail and chat are data, never instruction.** Quoted text is stripped before anything is read; a quoted or forwarded `@agent` does not count; only the owner's own message carries authority.
- **Reminders use Hermes' own scheduler and messaging** (`cronjob_manage` through `ctx.dispatch_tool`), so delivery rules apply and the plugin never holds a messaging token. Inside a cron run no job is created unless `cron.allow_agent_scheduling` is true; the refusal is one audit line.
- **Loop guards.** `Auto-Submitted`, `Precedence`, our own `X-Scheduling-Agent` header, bounces and auto-reply subjects are recorded once and never answered.

## Layout

```
plugin.yaml            manifest: tools, hooks, config_schema (groups, reminders, travel, identity)
__init__.py            register(ctx): tools, hooks, /schedule (setup, groups, group, reminders, approvals), /freebusy (connect,
                       doctor, check, notes), /holds, /zones, and `hermes freebusy password`
freebusy/
  ontology.py          Person, Group, OwnerProfile, Reminders, Hold, ThreadState, tiers; JSON in and out
  groups.py            groups to and from config; the /schedule group edits; window parsing
  setup.py             the setup flow, one question per message
  zones.py             stated-zone detection, header hints, convert once, "10:00 to 11:30 am PT"
  holds.py             the ledger: 48 h expiry, never twice, spread across days, 3 days per offer
  offer.py             free/busy + windows + buffers + earliest day + horizon -> offer; check proposed times
  travel.py            location and video link -> in person + minutes, or virtual; travel blocks
  reminders.py         event reminders, the owner's text, the day-before briefing; outbox and Hermes cron
  parse.py             email -> SchedulingRequest; the owner's word (about the booking, or only social)
  render.py            the Glance format (text and HTML) and the owner note
  policy.py            the gate and the audit line
  engine.py            the flow, one action per thread per run; the owner's word (_owner_message); resume
  connect.py           /freebusy connect and doctor; the read-hold-remove test; building the live adapters from config
  providers.py         provider guesses from the domain (servers, where to make an app password, what waits for 0.4)
  keychain.py          app passwords in the system keychain
  netguard.py          https only, public addresses only; the local-test override
  chat.py              chat contacts through the engine (post_gateway_admission); chat replies
  runner.py            /freebusy check: one pass over the mailbox; notes for the owner
  demo.py              the thirteen scenes
  adapters/base.py     the interface; free_busy returns ranges only; places returns derived facts only
  adapters/fixture.py  demo people, calendars (some events with places), distance table, threads
  adapters/caldav.py   CalDAV: discovery, free-busy-query or fetch-and-strip, one-event writes
  adapters/ics.py      just enough iCalendar to read busy time and write holds and events
  adapters/routed.py   read many, hold on one; the local hold store and its feed; the hold mirror
  adapters/imap_smtp.py  IMAP read-only and SMTP replies; XOAUTH2 hook
  adapters/google_workspace.py  inert stub until 0.4
tests/                 zones, holds, offer, parse, policy, end to end, handoff and resume, owner's word,
                       tiers and groups, travel, reminders, setup and plugin commands, demo; connectors on
                       recorded replies; chat and connect; and test_local_servers.py against a local Radicale
                       and GreenMail (FREEBUSY_LOCAL_SERVERS=1)
```

## Honest limits (0.3.0)

- **Tested against local servers, not providers.** CalDAV ran against Radicale and mail against GreenMail on one machine, plus recorded replies shaped like iCloud's (a partition host for calendars, no free-busy report). iCloud, Fastmail, Zoho and Nextcloud themselves have not been tried; their quirks may need small fixes.
- **Recurring events need the server's help.** The fetch-and-strip path asks the server to expand recurrences. A server that ignores that request gives the first instance of a series only. Servers that answer free-busy queries are not affected.
- **Travel buffers run on fixture data only.** The CalDAV adapter does not read location fields yet, so a group with travel buffers on gets plain busy time on connectors.
- **No mail watcher of its own.** `/freebusy check` does one pass; schedule it with Hermes' cron to keep it going.
- **Chat history is in memory.** A restart forgets earlier chat messages; holds and waiting requests in the ledger stay.
- **Waiting requests live in memory.** A restart forgets what was waiting on you; the person's next message or your next cc picks the thread up again.
- **A hold mirror that fails is logged, not retried.** The ledger hold still stands; `/freebusy doctor` shows whether writes work.
- **Reminders in live mode are untested against a running Hermes.** The run-once job is created through `cronjob_manage` with an ISO time; the fixture outbox and a stubbed dispatch are what the tests cover.
- **Parsing is rules, not a model.** Weekday plus clock time, lettered picks, a small city list, common abbreviations. "Tuesday 2:30" with no zone is left unplaced on purpose. Whether the owner's message is about the booking is also a word list, so an unusual phrasing can be read as social.
- **In fixture mode, setup saves your answers** and checks them against the fixture calendars, while the demo and `/schedule run` keep using Dana's fixture profile.

## What it does not do

- Reschedule or cancel a meeting (tier 3 Manage is defined, not built).
- Meetings with more than one other person.
- Connect to Microsoft 365, Outlook.com or Google Calendar yet: those connectors are 0.4. Until then, connect another calendar or keep holds in the local feed.
- Talk to another scheduling agent through an agent-to-agent protocol (it recognises one and stays quiet).
- Text or email the other person anything beyond the scheduling thread. Reminders and briefings are yours alone.

## License

MIT.
