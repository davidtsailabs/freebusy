"""The Glance format: how every reply to a person looks, and the owner note.

One greeting, one lead line, lettered rows (A, B, C) with a bold day and a
time, one ask, a footnote (zone line, presence line) and a plain signature
that says it is an AI assistant. Plain text and simple HTML. The owner is
told in plain text: Booked, Nothing fits, or Needs you.
"""

from __future__ import annotations

import html as _html
from dataclasses import dataclass, field
from typing import Optional

LETTERS = "ABCDEF"
SYS = "-apple-system,'SF Pro Text','Helvetica Neue',Helvetica,Arial,sans-serif"


@dataclass
class Row:
    day: str     # "Tue Nov 10"
    time: str    # "10:00 to 11:30 am PT"


@dataclass
class Reply:
    name: str
    lead: str
    rows: list[Row] = field(default_factory=list)
    ask: str = ""
    extra: str = ""
    zone_line: str = ""
    owner_name: str = ""
    owner_cc: bool = True


def signature(owner_name: str) -> str:
    return f"AI assistant to {owner_name} · handles scheduling only"


def presence_line(owner_name: str, cc: bool) -> str:
    if not cc:
        return ""
    first = owner_name.split()[0] if owner_name else "the owner"
    return f"{first} is cc'd here; say so if you would rather sort it out with {first} directly."


def render_text(r: Reply) -> str:
    blocks = [f"Hi {r.name or 'there'},"]
    if r.lead:
        blocks.append(r.lead)
    if r.extra:
        blocks.append(r.extra)
    if r.rows:
        blocks.append("\n".join(f"{LETTERS[i]}.  {row.day}  {row.time}" for i, row in enumerate(r.rows[:len(LETTERS)])))
    if r.ask:
        blocks.append(r.ask)
    foot = " ".join(x for x in (r.zone_line, presence_line(r.owner_name, r.owner_cc)) if x)
    if foot:
        blocks.append(foot)
    blocks.append(signature(r.owner_name))
    return "\n\n".join(blocks)


def render_html(r: Reply) -> str:
    e = lambda x: _html.escape(str(x))
    rows = "".join(
        f'<tr><td style="padding:7px 10px 7px 0;vertical-align:middle;width:26px"><span style="display:inline-block;'
        f'width:24px;height:24px;border-radius:12px;background:#111;color:#fff;text-align:center;font:700 12px/24px {SYS}">'
        f'{LETTERS[i]}</span></td>'
        f'<td style="padding:7px 12px 7px 0;font:600 16px/1.4 {SYS};color:#111;white-space:nowrap">{e(row.day)}</td>'
        f'<td style="padding:7px 0;font:16px/1.4 {SYS};color:#111">{e(row.time)}</td></tr>'
        for i, row in enumerate(r.rows[:len(LETTERS)]))
    foot = " ".join(x for x in (r.zone_line, presence_line(r.owner_name, r.owner_cc)) if x)
    parts = [f'<div style="font:16px/1.5 {SYS};color:#111">', f'<p style="margin:0 0 12px">Hi {e(r.name or "there")},</p>']
    if r.lead:
        parts.append(f'<p style="margin:0 0 10px">{e(r.lead)}</p>')
    if r.extra:
        parts.append(f'<p style="margin:0 0 10px">{e(r.extra)}</p>')
    if rows:
        parts.append(f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:2px 0 12px">{rows}</table>')
    if r.ask:
        parts.append(f'<p style="margin:0 0 6px">{e(r.ask)}</p>')
    if foot:
        parts.append(f'<p style="margin:14px 0 0;font-size:13px;line-height:1.5;color:#6b7280">{e(foot)}</p>')
    parts.append(f'<p style="margin:10px 0 0;font-size:12px;line-height:1.5;color:#9ca3af">{e(signature(r.owner_name))}</p></div>')
    return "\n".join(parts)


def words_before_signature(text: str) -> int:
    return len(text.split("AI assistant to")[0].split())


# ---- the owner note -----------------------------------------------------------

STATES = ("Booked", "Nothing fits", "Needs you")


@dataclass
class OwnerNote:
    state: str                      # Booked | Nothing fits | Needs you
    headline: str                   # one line, 12 words at most
    who: str = ""                   # name, role, company
    when: str = ""                  # "Tue Nov 10 · 10:00 am PT"
    ask: str = "Nothing"            # 3 to 6 words, or "Nothing"
    options: list[str] = field(default_factory=list)
    stops: list[str] = field(default_factory=list)    # 3 to 5 short lines, newest last
    thread_ref: str = ""
    block_elsewhere: bool = False   # add the line to block it on calendars the agent cannot write to


def note_subject(n: OwnerNote) -> str:
    if n.state == "Booked" and n.when:
        return f"Booked: block {n.when} on your other calendars" if n.block_elsewhere else f"Booked: {n.when}"
    return f"{n.state}: {n.headline}"


def render_owner_note(n: OwnerNote) -> str:
    if n.state not in STATES:
        raise ValueError(f"state must be one of {STATES}")
    t = [n.state.upper(), n.headline, ""]
    if n.who:
        t.append(f"Who: {n.who}")
    if n.when:
        t.append(f"When: {n.when}")
    t.append(f"Your move: {n.ask}")
    if n.options:
        t += [""] + [f"{LETTERS[i]}. {o}" for i, o in enumerate(n.options[:6])]
    if n.stops:
        t += ["", "What happened:"] + [f"- {s}" for s in n.stops[-5:]]
    if n.block_elsewhere and n.state == "Booked":
        t += ["", "Block this time on any calendar the assistant cannot write to."]
    if n.thread_ref:
        t += ["", f"Thread: {n.thread_ref}"]
    return "\n".join(t)
