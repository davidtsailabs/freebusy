"""IMAP + SMTP: any mailbox with an app password. Standard library only.

Reading: the mailbox is opened read-only and messages are fetched with
``BODY.PEEK[]``, so nothing is marked read. Only threads with the assistant's
address on a message (To, Cc or From) are returned; the rest of the inbox is
never parsed past its headers. A UID watermark (per UIDVALIDITY) keeps a thread
from being handled twice.

Sending: one message per thread, as a reply (In-Reply-To and References), from
the assistant's address, with our ``X-Scheduling-Agent`` header so neither side
answers its own mail.

Sign-in: ``auth: password`` (an app password from the keychain) is what 0.3
ships. ``auth: xoauth2`` is wired as a hook: pass ``token`` (a callable that
returns an OAuth access token) and both IMAP and SMTP authenticate with
XOAUTH2. Getting that token (Gmail, Outlook) is 0.4; nothing here stores one.

Connections are TLS: implicit TLS on 993/465, STARTTLS otherwise. Plaintext
exists only for local test servers under ``FREEBUSY_ALLOW_LOCAL_TEST_SERVERS=1``.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import html as _html
import imaplib
import json
import re
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, Optional

from ..netguard import UnsafeTarget, check_host, local_test_override
from ..parse import AGENT_HEADER, Email

TIMEOUT = 20
MAX_FETCH = 200
KEEP_HEADERS = ("Auto-Submitted", "Precedence", AGENT_HEADER, "X-Autoreply", "X-Autorespond", "List-Id")


class MailError(RuntimeError):
    pass


def xoauth2_string(user: str, token: str) -> str:
    return f"user={user}\x01auth=Bearer {token}\x01\x01"


def _addrs(values) -> list[str]:
    return [a.lower() for _, a in email.utils.getaddresses([str(v) for v in values if v]) if a]


def _text(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    body = part.get_content()
    if part.get_content_type() == "text/html":
        body = re.sub(r"(?is)<(script|style).*?</\1>", " ", body)
        body = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", body)
        body = _html.unescape(re.sub(r"<[^>]+>", "", body))
    return body.replace("\r\n", "\n")


def thread_key(msg) -> str:
    refs = re.findall(r"<[^>]+>", str(msg.get("References", "")))
    root = refs[0] if refs else (re.findall(r"<[^>]+>", str(msg.get("In-Reply-To", ""))) or [str(msg.get("Message-ID", ""))])[0]
    return "mail:" + re.sub(r"[^A-Za-z0-9._@+=-]", "", root.strip("<> ")) if root else "mail:unknown"


def to_email(raw: bytes) -> tuple[Email, str, list[str]]:
    """One message as the engine's Email, plus its own Message-ID and References chain."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    name, sender = email.utils.parseaddr(str(msg.get("From", "")))
    mid = str(msg.get("Message-ID", "")).strip()
    refs = re.findall(r"<[^>]+>", str(msg.get("References", ""))) or re.findall(r"<[^>]+>", str(msg.get("In-Reply-To", "")))
    headers = {h: str(msg[h]) for h in KEEP_HEADERS if msg[h] is not None}
    e = Email(thread_id=thread_key(msg), message_id=mid, sender=sender.lower(), to=_addrs(msg.get_all("To", [])),
              cc=_addrs(msg.get_all("Cc", [])), subject=str(msg.get("Subject", "")), body=_text(msg),
              date=str(msg.get("Date", "")), headers=headers, sender_name=name)
    return e, mid, refs


class ImapSmtpMail:
    def __init__(self, cfg: dict, password: Callable[[], Optional[str]], agent_address: str, owner_addresses: list[str],
                 state_path: Optional[Path] = None, agent_name: str = "", token: Optional[Callable[[], str]] = None):
        self.cfg = dict(cfg)
        self.username = cfg.get("username") or cfg.get("email", "")
        self.folder = cfg.get("folder", "INBOX")
        self.lookback_days = int(cfg.get("lookback_days", 14))
        self.auth = cfg.get("auth", "password")
        self._password, self._token = password, token
        self.agent = agent_address.lower()
        self.owners = [a.lower() for a in owner_addresses]
        self.agent_name = agent_name
        self.state_path = Path(state_path) if state_path else None
        self.state = json.loads(self.state_path.read_text()) if self.state_path and self.state_path.exists() else {}
        self._replies: dict[str, dict] = {}        # thread id -> subject, last message id, references
        self.sent: list[dict] = []

    # -- connecting -----------------------------------------------------------------
    def _security(self, port: int, implicit: int) -> str:
        sec = self.cfg.get("security") or ("ssl" if port == implicit else "starttls")
        if sec == "plain" and not local_test_override():
            raise UnsafeTarget("plaintext mail is allowed only for local test servers")
        return sec

    def _secret(self) -> str:
        pw = self._password()
        if not pw:
            raise MailError(f"no app password in the keychain for {self.username}; run: hermes freebusy password <connection>")
        return pw

    def imap(self) -> imaplib.IMAP4:
        host, port = self.cfg["imap_host"], int(self.cfg.get("imap_port", 993))
        check_host(host, port)
        sec = self._security(port, 993)
        ctx = ssl.create_default_context()
        try:
            if sec == "ssl":
                conn = imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=TIMEOUT)
            else:
                conn = imaplib.IMAP4(host, port, timeout=TIMEOUT)
                if sec == "starttls":
                    conn.starttls(ssl_context=ctx)
            if self.auth == "xoauth2":
                if not self._token:
                    raise MailError("XOAUTH2 sign-in is not available until 0.4; use an app password")
                tok = self._token()
                conn.authenticate("XOAUTH2", lambda _: xoauth2_string(self.username, tok).encode())
            else:
                conn.login(self.username, self._secret())
            return conn
        except imaplib.IMAP4.error as exc:
            raise MailError(f"IMAP sign-in failed for {self.username}: {exc}") from exc
        except OSError as exc:
            raise MailError(f"could not reach {host}:{port}: {exc}") from exc

    def smtp(self) -> smtplib.SMTP:
        host, port = self.cfg["smtp_host"], int(self.cfg.get("smtp_port", 465))
        check_host(host, port)
        sec = self._security(port, 465)
        ctx = ssl.create_default_context()
        try:
            if sec == "ssl":
                conn = smtplib.SMTP_SSL(host, port, context=ctx, timeout=TIMEOUT)
            else:
                conn = smtplib.SMTP(host, port, timeout=TIMEOUT)
                if sec == "starttls":
                    conn.starttls(context=ctx)
            if self.auth == "xoauth2":
                if not self._token:
                    raise MailError("XOAUTH2 sign-in is not available until 0.4; use an app password")
                import base64
                conn.ehlo()
                code, resp = conn.docmd("AUTH", "XOAUTH2 " + base64.b64encode(xoauth2_string(self.username, self._token()).encode()).decode())
                if code != 235:
                    raise MailError(f"SMTP XOAUTH2 refused ({code})")
            else:
                conn.login(self.username, self._secret())
            return conn
        except smtplib.SMTPException as exc:
            raise MailError(f"SMTP sign-in failed for {self.username}: {exc}") from exc
        except OSError as exc:
            raise MailError(f"could not reach {host}:{port}: {exc}") from exc

    # -- reading --------------------------------------------------------------------
    def _save_state(self) -> None:
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self.state))

    def list_threads(self, new_only: bool = True) -> list[list[Email]]:
        """Threads that reached the agent and have a message newer than the watermark, oldest message first."""
        conn = self.imap()
        try:
            typ, _ = conn.select(self.folder, readonly=True)
            if typ != "OK":
                raise MailError(f"cannot open folder {self.folder}")
            validity = (conn.response("UIDVALIDITY")[1] or [b"0"])[0]
            validity = validity.decode() if isinstance(validity, bytes) else str(validity)
            if self.state.get("uidvalidity") != validity:
                self.state = {"uidvalidity": validity, "last_uid": 0}
            since = (datetime.now(timezone.utc) - timedelta(days=self.lookback_days)).strftime("%d-%b-%Y")
            typ, data = conn.uid("SEARCH", None, "SINCE", since)
            uids = [int(u) for u in (data[0] or b"").split()][-MAX_FETCH:]
            msgs: list[tuple[int, Email, str, list[str]]] = []
            for uid in uids:
                typ, part = conn.uid("FETCH", str(uid), "(BODY.PEEK[HEADER.FIELDS (FROM TO CC)])")
                head = b"".join(p[1] for p in part if isinstance(p, tuple))
                hm = email.message_from_bytes(head, policy=email.policy.default)
                people = set(_addrs([hm.get("From", "")] + hm.get_all("To", []) + hm.get_all("Cc", [])))
                if self.agent not in people:
                    continue                          # not for the assistant: never read past the headers
                typ, part = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
                raw = b"".join(p[1] for p in part if isinstance(p, tuple))
                e, mid, refs = to_email(raw)
                msgs.append((uid, e, mid, refs))
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        last = int(self.state.get("last_uid", 0))
        threads: dict[str, list[tuple[int, Email, str, list[str]]]] = {}
        for item in msgs:
            threads.setdefault(item[1].thread_id, []).append(item)
        out = []
        for tid, items in threads.items():
            items.sort(key=lambda x: x[0])
            if new_only and items[-1][0] <= last:
                continue
            uid, latest, mid, refs = items[-1]
            self._replies[tid] = {"subject": latest.subject, "in_reply_to": mid, "references": [*refs, mid] if mid else refs}
            out.append([i[1] for i in items])
        self._pending_uid = max([last, *[m[0] for m in msgs]])
        return out

    def commit(self) -> None:
        """Move the watermark past what list_threads returned; call after the threads were handled."""
        self.state["last_uid"] = getattr(self, "_pending_uid", self.state.get("last_uid", 0))
        self._save_state()

    # -- sending ----------------------------------------------------------------------
    def _message(self, to: list[str], cc: list[str], subject: str, text: str, html: Optional[str], reply: Optional[dict]) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = email.utils.formataddr((self.agent_name, self.agent)) if self.agent_name else self.agent
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subject
        msg["Date"] = email.utils.formatdate(localtime=False)
        msg["Message-ID"] = email.utils.make_msgid(domain=self.agent.split("@")[-1] or None)
        msg[AGENT_HEADER] = "1"
        if reply and reply.get("in_reply_to"):
            msg["In-Reply-To"] = reply["in_reply_to"]
            msg["References"] = " ".join(reply.get("references") or [reply["in_reply_to"]])
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        return msg

    def send(self, thread_id: str, to: list[str], cc: list[str], subject: str, text: str, html: Optional[str] = None) -> str:
        reply = self._replies.get(thread_id)
        if reply and reply.get("subject"):
            s = reply["subject"]
            subject = s if s.lower().startswith("re:") else f"Re: {s}"
        msg = self._message(to, cc, subject, text, html, reply)
        conn = self.smtp()
        try:
            conn.send_message(msg, from_addr=self.agent, to_addrs=list(dict.fromkeys([*to, *cc])))
        finally:
            try:
                conn.quit()
            except Exception:
                pass
        self.sent.append({"thread_id": thread_id, "to": to, "cc": cc, "id": msg["Message-ID"]})
        return str(msg["Message-ID"])

    def send_owner_note(self, subject: str, text: str) -> str:
        """A note to the owner alone (Booked, Nothing fits, Needs you)."""
        if not self.owners:
            raise MailError("no owner address configured")
        msg = self._message([self.owners[0]], [], subject, text, None, None)
        conn = self.smtp()
        try:
            conn.send_message(msg, from_addr=self.agent, to_addrs=[self.owners[0]])
        finally:
            try:
                conn.quit()
            except Exception:
                pass
        return str(msg["Message-ID"])

    def check(self) -> str:
        """Sign in to both servers without sending anything. Returns a one-line result."""
        conn = self.imap()
        try:
            typ, _ = conn.select(self.folder, readonly=True)
            if typ != "OK":
                raise MailError(f"cannot open folder {self.folder}")
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        s = self.smtp()
        try:
            s.noop()
        finally:
            try:
                s.quit()
            except Exception:
                pass
        return f"IMAP {self.cfg['imap_host']} and SMTP {self.cfg['smtp_host']} accepted the sign-in"
