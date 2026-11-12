"""One pass over the mailbox, and the owner's notes.

``check_mail`` lists the threads that reached the assistant since the last
pass, hands each to the engine, and delivers any owner note. It runs when the
owner asks (``/freebusy check``) or when they schedule that command with
Hermes' own cron; the plugin starts no background job of its own.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .policy import redact


class OwnerNotes:
    """Notes for the owner: emailed to the owner's own address when a mail connector exists, always kept in notes.jsonl."""

    def __init__(self, path: Path, mail=None):
        self.path, self.mail = Path(path), mail

    def __call__(self, subject: str, text: str) -> str:
        sent = ""
        if self.mail is not None:
            try:
                sent = self.mail.send_owner_note(subject, text)
            except Exception as exc:
                sent = f"not emailed: {exc}"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(redact({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "subject": subject,
                                       "text": text, "emailed": sent})) + "\n")
        return sent

    def recent(self, n: int = 5) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines()[-n:]]


def check_mail(sched, mail, notes: OwnerNotes, now: Optional[datetime] = None) -> list[str]:
    if mail is None:
        return ["no mail connector; /freebusy connect you@example.com adds one"]
    now = now or datetime.now(timezone.utc)
    out = []
    threads = mail.list_threads()
    for thread in threads:
        tid = thread[-1].thread_id
        try:
            act = sched.handle(thread, now)
        except Exception as exc:          # one bad thread never stops the pass; the owner hears about it
            notes("freebusy: a thread could not be handled", f"{tid}: {exc}")
            out.append(f"{tid}: error ({exc})")
            continue
        if act.owner_note:
            notes(act.owner_subject or "freebusy", act.owner_note)
        out.append(f"{tid}: {act.kind}, {act.reason}")
    mail.commit()
    return out or ["no new scheduling mail"]
