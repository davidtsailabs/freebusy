"""CalDAV (RFC 4791): iCloud, Fastmail, Nextcloud, Zoho and any standard server, with an app password.

Reading: a ``free-busy-query`` REPORT where the server supports it (the server
sends back busy periods only). On the hold calendar the events query comes first,
so the plugin's own holds (OPAQUE, marked ``X-FREEBUSY-HOLD``) can be left out of
its own busy time while they still block the slot for everyone else. Where it does not, a ``calendar-query`` REPORT
with ``<C:expand>`` fetches the events and ``ics.busy_from_events`` reduces them
to start and end inside this module, before anything is returned.

Writing: one event per PUT, to the one calendar the owner chose for holds.

Every URL, including each redirect and every href a server returns, passes
``netguard.check_url`` first. The password is read from the keychain when a
request is made and sent only to the host it was set for.
"""

from __future__ import annotations

import base64
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import urljoin, urlsplit

from ..netguard import UnsafeTarget, check_url
from ..ontology import Slot
from . import ics

DAV, CAL, CS = "DAV:", "urn:ietf:params:xml:ns:caldav", "http://calendarserver.org/ns/"
NS = {"d": DAV, "c": CAL}
MAX_BYTES = 5_000_000
TIMEOUT = 20


class CalDAVError(RuntimeError):
    pass


@dataclass
class CalendarInfo:
    url: str
    name: str
    writable: bool
    events: bool = True          # supports VEVENT


_SHARED_SLD = {"co", "com", "org", "net", "ac", "gov", "edu", "ne", "or"}


def same_site(configured: str, other: str) -> bool:
    """The configured host, or a sibling under its parent domain (iCloud serves calendars from pNN-caldav.icloud.com
    after you sign in at caldav.icloud.com). A parent that looks like a public suffix (co.uk, com.au) never counts."""
    a, b = (configured or "").lower().rstrip("."), (other or "").lower().rstrip(".")
    if not a or not b:
        return False
    if a == b:
        return True
    labels = a.split(".")
    if len(labels) < 3 or ":" in a or all(x.isdigit() for x in labels):
        return False                                   # IP addresses match exactly or not at all
    parent = ".".join(labels[1:])
    plabels = parent.split(".")
    if len(plabels) < 2 or (len(plabels) == 2 and (plabels[0] in _SHARED_SLD or len(plabels[0]) <= 2)):
        return False
    return b.endswith("." + parent)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None              # redirects are followed by hand, each one checked


def _xml(body: str) -> bytes:
    return ('<?xml version="1.0" encoding="utf-8"?>\n' + body).encode("utf-8")


PROP_PRINCIPAL = _xml('<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>')
PROP_HOME = _xml('<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><c:calendar-home-set/></d:prop></d:propfind>')
PROP_CALS = _xml('<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><d:resourcetype/><d:displayname/>'
                 '<c:supported-calendar-component-set/><d:current-user-privilege-set/></d:prop></d:propfind>')


def freebusy_query(start: datetime, end: datetime) -> bytes:
    return _xml(f'<c:free-busy-query xmlns:c="urn:ietf:params:xml:ns:caldav"><c:time-range start="{ics.utc_stamp(start)}" '
                f'end="{ics.utc_stamp(end)}"/></c:free-busy-query>')


def events_query(start: datetime, end: datetime) -> bytes:
    s, e = ics.utc_stamp(start), ics.utc_stamp(end)
    # Only the properties that decide busy time are asked for, where the server honours <c:prop> filtering.
    return _xml('<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><c:calendar-data>'
                f'<c:expand start="{s}" end="{e}"/><c:comp name="VCALENDAR"><c:comp name="VEVENT">'
                '<c:prop name="DTSTART"/><c:prop name="DTEND"/><c:prop name="DURATION"/><c:prop name="TRANSP"/>'
                '<c:prop name="STATUS"/><c:prop name="RRULE"/><c:prop name="RECURRENCE-ID"/><c:prop name="UID"/>'
                '<c:prop name="X-FREEBUSY-HOLD"/></c:comp></c:comp>'
                '</c:calendar-data></d:prop><c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
                f'<c:time-range start="{s}" end="{e}"/></c:comp-filter></c:comp-filter></c:filter></c:calendar-query>')


class CalDAVClient:
    """One account on one server. ``password`` is a callable so the secret is fetched per request and never stored here."""

    def __init__(self, base_url: str, username: str, password: Callable[[], Optional[str]], opener=None):
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.username = username
        self._password = password
        self._host = urlsplit(self.base_url).hostname
        self._opener = opener or urllib.request.build_opener(_NoRedirect)
        self.fb_supported: dict[str, bool] = {}       # calendar url -> free-busy-query works

    # -- transport ----------------------------------------------------------------
    def _auth(self, url: str) -> dict:
        if not same_site(self._host, urlsplit(url).hostname):
            return {}                                  # never send the password to another site
        pw = self._password()
        if not pw:
            raise CalDAVError(f"no app password in the keychain for {self.username}; run: hermes freebusy password <connection>")
        token = base64.b64encode(f"{self.username}:{pw}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def request(self, method: str, url: str, body: Optional[bytes] = None, depth: Optional[str] = None,
                ctype: str = "application/xml; charset=utf-8", headers: Optional[dict] = None) -> tuple[int, dict, str]:
        for _ in range(5):
            check_url(url)
            h = {"User-Agent": "freebusy/0.3", **(headers or {}), **self._auth(url)}
            if body is not None:
                h["Content-Type"] = ctype
            if depth is not None:
                h["Depth"] = depth
            req = urllib.request.Request(url, data=body, method=method, headers=h)
            try:
                with self._opener.open(req, timeout=TIMEOUT) as resp:
                    return resp.status, dict(resp.headers), resp.read(MAX_BYTES).decode("utf-8", "replace")
            except urllib.error.HTTPError as err:
                if err.code in (301, 302, 303, 307, 308) and err.headers.get("Location"):
                    url = urljoin(url, err.headers["Location"])
                    continue
                return err.code, dict(err.headers or {}), (err.read(MAX_BYTES) or b"").decode("utf-8", "replace")
            except urllib.error.URLError as err:
                raise CalDAVError(f"could not reach {urlsplit(url).hostname}: {err.reason}") from err
        raise CalDAVError("too many redirects")

    def _multistatus(self, method: str, url: str, body: bytes, depth: str) -> list[tuple[str, ET.Element]]:
        status, _, text = self.request(method, url, body, depth)
        if status == 401:
            raise CalDAVError("the server refused the user name or app password (401)")
        if status != 207:
            raise CalDAVError(f"{method} {urlsplit(url).path} answered {status}")
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise CalDAVError(f"unreadable reply to {method}: {exc}") from exc
        out = []
        for resp in root.findall("d:response", NS):
            href = (resp.findtext("d:href", "", NS) or "").strip()
            out.append((urljoin(url, href), resp))
        return out

    @staticmethod
    def _ok_prop(resp: ET.Element) -> ET.Element:
        merged = ET.Element("prop")
        for ps in resp.findall("d:propstat", NS):
            if " 200 " in (ps.findtext("d:status", "", NS) or "") + " ":
                for p in ps.findall("d:prop", NS):
                    merged.extend(list(p))
        return merged

    # -- discovery --------------------------------------------------------------------
    def _href_prop(self, url: str, body: bytes, tag: str) -> Optional[str]:
        for href, resp in self._multistatus("PROPFIND", url, body, "0"):
            el = self._ok_prop(resp).find(tag, NS)
            if el is not None:
                h = el.findtext("d:href", "", NS).strip()
                if h:
                    return urljoin(url, h)
        return None

    def discover(self) -> list[CalendarInfo]:
        """Calendars this account can see: principal, then home set, then each calendar with its write right."""
        start = self.base_url
        if urlsplit(start).path in ("", "/"):
            start = urljoin(start, "/.well-known/caldav")
        try:
            principal = self._href_prop(start, PROP_PRINCIPAL, "d:current-user-principal")
        except CalDAVError:
            if start == self.base_url:
                raise
            principal = self._href_prop(self.base_url, PROP_PRINCIPAL, "d:current-user-principal")
        home = self._href_prop(principal or start, PROP_HOME, "c:calendar-home-set") or principal or start
        cals = []
        for href, resp in self._multistatus("PROPFIND", home, PROP_CALS, "1"):
            prop = self._ok_prop(resp)
            rt = prop.find("d:resourcetype", NS)
            if rt is None or rt.find("c:calendar", NS) is None:
                continue
            comps = prop.find("c:supported-calendar-component-set", NS)
            names = {c.get("name", "").upper() for c in comps.findall("c:comp", NS)} if comps is not None else {"VEVENT"}
            privs = prop.find("d:current-user-privilege-set", NS)
            if privs is None:
                writable = True                        # the server does not say; the setup test proves it either way
            else:
                held = {child.tag for p in privs.findall("d:privilege", NS) for child in p}
                writable = bool(held & {f"{{{DAV}}}write", f"{{{DAV}}}write-content", f"{{{DAV}}}all"})
            name = (prop.findtext("d:displayname", "", NS) or "").strip() or urlsplit(href).path.rstrip("/").rsplit("/", 1)[-1]
            cals.append(CalendarInfo(url=href, name=name, writable=writable, events="VEVENT" in names))
        return [c for c in cals if c.events]

    # -- busy -------------------------------------------------------------------------
    def busy(self, cal_url: str, start: datetime, end: datetime, zone: str, own_holds: Optional[list[Slot]] = None) -> list[Slot]:
        """Busy ranges for freebusy itself. ``own_holds`` is given for the hold calendar: there the events are fetched
        so our own holds can be left out by marker and UID; if the server cannot answer that, a free-busy reply is used
        with the ledger's held intervals subtracted (a free-busy reply cannot say which period is ours)."""
        start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        if own_holds is not None:
            try:
                return self._events_busy(cal_url, start, end, zone)
            except CalDAVError as exc:
                if "401" in str(exc):
                    raise
            status, _, text = self.request("REPORT", cal_url, freebusy_query(start, end), "1")
            if status == 200 and ics.is_freebusy_reply(text):
                return ics.subtract(ics.busy_from_freebusy(text, start, end, zone), own_holds)
            raise CalDAVError(f"the hold calendar answered neither an events query nor a free-busy query ({status})")
        if self.fb_supported.get(cal_url, True):
            status, _, text = self.request("REPORT", cal_url, freebusy_query(start, end), "1")
            if status == 200 and ics.is_freebusy_reply(text):
                self.fb_supported[cal_url] = True
                return ics.busy_from_freebusy(text, start, end, zone)
            if status == 401:
                raise CalDAVError("the server refused the user name or app password (401)")
            self.fb_supported[cal_url] = False          # not supported here: fetch and strip instead
        return self._events_busy(cal_url, start, end, zone)

    def _events_busy(self, cal_url: str, start: datetime, end: datetime, zone: str) -> list[Slot]:
        out: list[Slot] = []
        for _, resp in self._multistatus("REPORT", cal_url, events_query(start, end), "1"):
            data = self._ok_prop(resp).findtext("c:calendar-data", "", NS)
            if data:
                out.extend(ics.busy_from_events(data, start, end, zone))
        return sorted(out, key=lambda s: s.start)

    # -- write ------------------------------------------------------------------------
    def put_event(self, cal_url: str, uid: str, body: str) -> str:
        url = urljoin(cal_url if cal_url.endswith("/") else cal_url + "/", f"{uid}.ics")
        status, _, text = self.request("PUT", url, body.encode("utf-8"), ctype="text/calendar; charset=utf-8",
                                       headers={"If-None-Match": "*"})
        if status not in (200, 201, 204):
            raise CalDAVError(f"the server did not save the event ({status})")
        return url

    def delete(self, href: str) -> None:
        status, _, _ = self.request("DELETE", href)
        if status not in (200, 204, 404):
            raise CalDAVError(f"the server did not delete the event ({status})")


def safe_discover(client: CalDAVClient) -> tuple[list[CalendarInfo], str]:
    try:
        return client.discover(), ""
    except (CalDAVError, UnsafeTarget) as exc:
        return [], str(exc)
