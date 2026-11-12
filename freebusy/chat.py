"""Chat channels: scheduling requests over Hermes' own gateway (Telegram, WhatsApp, iMessage, SMS, ...).

The hook is ``post_gateway_admission``. Hermes calls it once per inbound
message that already passed its own authorization, pairing and slash-command
handling, and lets a plugin answer it instead of the agent. So:

* A sender Hermes does not admit never reaches this code.
* Of the admitted senders, only the owner's chat contacts (``chat_contacts`` in
  config, keyed ``platform:user_id``) are handled here. Listing someone is the
  owner's introduction. Everyone else, the owner included, goes to the agent as before.
* A contact's message becomes an Email in the thread ``chat:<platform>:<chat id>``
  and goes through the same engine, rules, tiers, holds and policy gate as mail.
* The reply goes back on the same chat through Hermes' normal route (the hook
  returns it), with one line up front saying an assistant is answering.
* Anything that waits for the owner (a long meeting, nothing fits, not
  scheduling) gets no reply in the chat; the owner gets the note instead.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Callable, Optional

from .ontology import Intent, Person
from .parse import Email
from .render import presence_line, signature

PSEUDO_DOMAIN = "chat.invalid"            # a contact with no email; never mailed (the calendar adapter drops it from invites)
HISTORY = 20


def assistant_line(owner_name: str) -> str:
    first = owner_name.split()[0] if owner_name else "the owner"
    return f"({first}'s scheduling assistant, an AI, is answering.)"


def chat_text(text: str, owner_name: str) -> str:
    """The email rendering, made for a chat: the one-line note first, no 'cc'd here' line, no mail signature."""
    blocks = [b for b in text.split("\n\n") if b.strip() and b.strip() != signature(owner_name)]
    cc = presence_line(owner_name, True)
    blocks = [b.replace(cc, "").strip() for b in blocks]
    return "\n\n".join([assistant_line(owner_name), *[b for b in blocks if b]])


class ChatOutbox:
    """Where the engine's send lands for a chat thread: kept, then returned as the hook's reply."""

    def __init__(self):
        self.replies: dict[str, str] = {}

    def send(self, thread_id: str, to, cc, subject: str, text: str, html=None) -> str:
        self.replies[thread_id] = text
        return f"chat-{thread_id}-{len(self.replies)}"


class RoutingMail:
    """The engine's mail adapter in connector mode: chat threads to the chat outbox, the rest to mail (if any)."""

    def __init__(self, mail=None, chat: Optional[ChatOutbox] = None):
        self.mail, self.chat = mail, chat or ChatOutbox()

    def list_threads(self):
        return self.mail.list_threads() if self.mail else []

    def send(self, thread_id: str, to, cc, subject: str, text: str, html=None) -> str:
        if thread_id.startswith("chat:"):
            return self.chat.send(thread_id, to, cc, subject, text, html)
        if not self.mail:
            raise RuntimeError("no mail connector; connect one with /freebusy connect")
        return self.mail.send(thread_id, to, cc, subject, text, html)


def _norm(contact) -> dict:
    if isinstance(contact, str):
        return {"email": contact} if "@" in contact else {"name": contact}
    return dict(contact or {})


class ChatDesk:
    def __init__(self, contacts: dict, agent_address: str):
        self.contacts = {str(k).lower(): _norm(v) for k, v in (contacts or {}).items()}
        self.agent = agent_address
        self.history: dict[str, list[Email]] = {}

    def contact(self, platform: str, user_id: str) -> Optional[dict]:
        return self.contacts.get(f"{platform}:{user_id}".lower())

    def address(self, platform: str, user_id: str, c: dict) -> str:
        email = str(c.get("email") or "").strip().lower()
        safe = re.sub(r"[^a-z0-9]+", ".", f"{platform}.{user_id}".lower()).strip(".")
        return email or f"{safe}@{PSEUDO_DOMAIN}"


def handle_chat(sched, desk: ChatDesk, outbox: ChatOutbox, platform: str, source: dict, text: str, message_id: str,
                now: datetime, notify: Callable[[str, str], None]) -> Optional[dict]:
    """The post_gateway_admission answer: None (the agent handles it as usual) or {"action": "handled", "reply"?}."""
    user_id = str(source.get("user_id") or "")
    c = desk.contact(platform, user_id)
    if not c or not text.strip():
        return None
    addr = desk.address(platform, user_id, c)
    chat_id = str(source.get("chat_id") or user_id)
    tid = f"chat:{platform}:{chat_id}"
    name = str(c.get("name") or source.get("user_name") or addr.split("@")[0])
    person = sched.people.get(addr) or Person(id=addr, name=name, emails=[addr], group_id=c.get("group"))
    sched.people[addr] = person
    st = sched.state(tid)
    st.introduced = True                           # the owner listed this contact: that is the introduction
    mail = Email(thread_id=tid, message_id=f"<{platform}-{message_id or len(desk.history.get(tid, []))}@chat>", sender=addr,
                 to=[sched.profile.agent_address], cc=[], subject="Chat", body=text, sender_name=name)
    thread = [*desk.history.get(tid, []), mail]
    desk.history[tid] = thread[-HISTORY:]
    outbox.replies.pop(tid, None)
    act = sched.handle(thread, now)
    if act.owner_note:
        notify(act.owner_subject or "freebusy", act.owner_note)
    reply = outbox.replies.pop(tid, None)
    if reply and act.kind in ("reply", "reply+notify"):
        out = chat_text(reply, sched.profile.name)
        desk.history[tid].append(Email(thread_id=tid, message_id=f"<agent-{len(thread)}@chat>", sender=sched.profile.agent_address,
                                       to=[addr], cc=[], subject="Chat", body=reply, headers={"X-Scheduling-Agent": "1"}))
        return {"action": "handled", "reply": out}
    req = act.request
    if act.kind == "silent" and (req is None or req.intent == Intent.NOT_SCHEDULING.value):
        return None                                  # not about scheduling: the agent answers as it normally would
    return {"action": "handled"}                     # waiting on the owner, or blocked: no reply in the chat
