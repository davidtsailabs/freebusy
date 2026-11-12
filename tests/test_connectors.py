"""0.3 connectors without a network: the guard, iCalendar, CalDAV on recorded replies, the local hold store,
the ledger mirror, IMAP message parsing, provider guesses, the keychain and the connect flow's refusals."""

import io
import json
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from freebusy import keychain
from freebusy.adapters import ics
from freebusy.adapters.base import WrongCalendar
from freebusy.adapters.caldav import CalDAVClient, CalDAVError, same_site
from freebusy.adapters.imap_smtp import ImapSmtpMail, thread_key, to_email, xoauth2_string
from freebusy.adapters.routed import LOCAL, CalendarMirror, LocalHoldStore, RoutedCalendar, ref
from freebusy.holds import HoldLedger
from freebusy.netguard import TEST_OVERRIDE, UnsafeTarget, check_host, check_link, check_url
from freebusy.ontology import Slot
from freebusy.providers import guess

PUBLIC = lambda host, port: ["17.42.251.41"]          # noqa: E731  a public address, so no DNS is needed
UTC = timezone.utc


# ---- the network guard ------------------------------------------------------------------

def test_https_only_and_public_addresses_only(monkeypatch):
    monkeypatch.delenv(TEST_OVERRIDE, raising=False)
    assert check_url("https://caldav.icloud.com/", PUBLIC)
    for bad in ("http://caldav.icloud.com/", "ftp://x.example/", "file:///etc/passwd", "https://user:pw@caldav.icloud.com/"):
        with pytest.raises(UnsafeTarget):
            check_url(bad, PUBLIC)
    for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.10", "172.16.0.1", "169.254.169.254", "::1", "fe80::1", "0.0.0.0",
               "::ffff:127.0.0.1", "100.64.0.1"):
        with pytest.raises(UnsafeTarget):
            check_url("https://sneaky.example/", lambda h, p, ip=ip: [ip])
    with pytest.raises(UnsafeTarget):          # one bad address among good ones is enough to refuse
        check_host("mixed.example", 993, lambda h, p: ["17.42.251.41", "10.1.1.1"])


def test_the_test_override_allows_http_and_loopback_only(monkeypatch):
    monkeypatch.setenv(TEST_OVERRIDE, "1")
    assert check_url("http://127.0.0.1:5232/", lambda h, p: ["127.0.0.1"])
    with pytest.raises(UnsafeTarget):
        check_url("http://router.local/", lambda h, p: ["192.168.1.1"])
    with pytest.raises(UnsafeTarget):
        check_url("https://metadata.example/", lambda h, p: ["169.254.169.254"])
    monkeypatch.setenv(TEST_OVERRIDE, "yes")       # only exactly "1" switches it on
    with pytest.raises(UnsafeTarget):
        check_url("http://127.0.0.1:5232/", lambda h, p: ["127.0.0.1"])


def test_meeting_links_are_https_host_names():
    assert check_link("https://meet.jit.si/dana-calls") == "https://meet.jit.si/dana-calls"
    for bad in ("http://zoom.us/j/1", "https://10.0.0.1/room", "javascript:alert(1)", "https://u:p@whereby.com/x", "https://localhost/x"):
        with pytest.raises(UnsafeTarget):
            check_link(bad)


def test_password_goes_to_the_same_site_only():
    assert same_site("caldav.icloud.com", "p42-caldav.icloud.com")
    assert not same_site("caldav.icloud.com", "icloud.com.evil.example")
    assert not same_site("mail.example.co.uk", "attacker.co.uk")
    assert not same_site("127.0.0.1", "10.0.0.1")
    assert same_site("cloud.example.org", "cloud.example.org")


# ---- iCalendar ---------------------------------------------------------------------------

EVENTS = """BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VTIMEZONE\r
TZID:Europe/Zurich\r
BEGIN:STANDARD\r
DTSTART:19701025T030000\r
END:STANDARD\r
END:VTIMEZONE\r
BEGIN:VEVENT\r
UID:a\r
SUMMARY:Board meeting about the secret merger\r
DESCRIPTION:Attendees and notes that must never leave the adapter\r
DTSTART;TZID=Europe/Zurich:20261117T180000\r
DTEND;TZID=Europe/Zurich:20261117T190000\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:b\r
DTSTART:20261118T170000Z\r
DURATION:PT45M\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:c\r
DTSTART:20261118T200000Z\r
DTEND:20261118T210000Z\r
TRANSP:TRANSPARENT\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:d\r
DTSTART:20261119T200000Z\r
DTEND:20261119T210000Z\r
STATUS:CANCELLED\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:e\r
DTSTART;VALUE=DATE:20261120\r
DTEND;VALUE=DATE:20261121\r
SUMMARY:Holiday\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:f\r
DTSTART:20261218T170000Z\r
DTEND:20261218T180000Z\r
END:VEVENT\r
END:VCALENDAR\r
"""


def test_events_reduce_to_start_and_end_only():
    a, b = datetime(2026, 11, 16, tzinfo=UTC), datetime(2026, 11, 23, tzinfo=UTC)
    busy = ics.busy_from_events(EVENTS, a, b, "America/Los_Angeles")
    assert busy == [Slot("2026-11-17T09:00:00-08:00", "2026-11-17T10:00:00-08:00"),
                    Slot("2026-11-18T09:00:00-08:00", "2026-11-18T09:45:00-08:00")]
    assert "merger" not in json.dumps([s.__dict__ for s in busy])


def test_freebusy_reply_periods():
    text = ("BEGIN:VCALENDAR\r\nBEGIN:VFREEBUSY\r\nDTSTART:20261116T000000Z\r\nDTEND:20261123T000000Z\r\n"
            "FREEBUSY;FBTYPE=BUSY:20261117T170000Z/20261117T180000Z,20261118T170000Z/PT30M\r\n"
            "FREEBUSY;FBTYPE=FREE:20261119T170000Z/20261119T180000Z\r\n"
            "FREEBUSY;FBTYPE=BUSY-TENTATIVE:20261120T170000Z/20261120T171500Z\r\nEND:VFREEBUSY\r\nEND:VCALENDAR\r\n")
    busy = ics.busy_from_freebusy(text, datetime(2026, 11, 16, tzinfo=UTC), datetime(2026, 11, 23, tzinfo=UTC), "UTC")
    assert [s.start[:16] for s in busy] == ["2026-11-17T17:00", "2026-11-18T17:00", "2026-11-20T17:00"]
    assert busy[1].end.startswith("2026-11-18T17:30")


def test_holds_are_tentative_and_transparent_and_text_is_escaped():
    slot = Slot("2026-11-18T10:00:00-08:00", "2026-11-18T10:30:00-08:00")
    hold = ics.calendar([ics.vevent("u1", slot, "Hold; for, Lukas", tentative=True, organizer="d@x.example", attendees=["l@y.example"])])
    assert "STATUS:TENTATIVE" in hold and "TRANSP:TRANSPARENT" in hold and "SUMMARY:Hold\\; for\\, Lukas" in hold
    assert "ATTENDEE" not in hold                     # a hold never invites anyone
    real = ics.calendar([ics.vevent("u2", slot, "Dana and Lukas", url="https://meet.jit.si/x", organizer="d@x.example",
                                    attendees=["l@y.example"], alarms_min=[10])])
    real = "\n".join(ics.unfold(real))
    assert "DTSTART:20261118T180000Z" in real and "TRANSP:OPAQUE" in real and "mailto:l@y.example" in real and "TRIGGER:-PT10M" in real
    assert ics.busy_from_events(real, datetime(2026, 11, 18, tzinfo=UTC), datetime(2026, 11, 19, tzinfo=UTC), "UTC")
    assert not ics.busy_from_events(hold, datetime(2026, 11, 18, tzinfo=UTC), datetime(2026, 11, 19, tzinfo=UTC), "UTC")


# ---- CalDAV on recorded replies ---------------------------------------------------------------

def _ms(*responses):
    body = "".join(responses)
    return f'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">{body}</d:multistatus>'


PRINCIPAL = _ms('<d:response><d:href>/</d:href><d:propstat><d:prop><d:current-user-principal><d:href>/123/principal/</d:href>'
                '</d:current-user-principal></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>')
HOME = _ms('<d:response><d:href>/123/principal/</d:href><d:propstat><d:prop><c:calendar-home-set>'
           '<d:href>https://p42-caldav.icloud.com:443/123/calendars/</d:href></c:calendar-home-set></d:prop>'
           '<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>')


def _cal(href, name, writable=True, comps=("VEVENT",)):
    privs = "<d:privilege><d:read/></d:privilege>" + ("<d:privilege><d:write/></d:privilege>" if writable else "")
    cs = "".join(f'<c:comp name="{c}"/>' for c in comps)
    return (f'<d:response><d:href>{href}</d:href><d:propstat><d:prop><d:resourcetype><d:collection/><c:calendar/></d:resourcetype>'
            f'<d:displayname>{name}</d:displayname><c:supported-calendar-component-set>{cs}</c:supported-calendar-component-set>'
            f'<d:current-user-privilege-set>{privs}</d:current-user-privilege-set></d:prop>'
            f'<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>')


CALS = _ms('<d:response><d:href>/123/calendars/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>'
           '<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>',
           _cal("/123/calendars/home/", "Home"), _cal("/123/calendars/work/", "Work (shared)", writable=False),
           _cal("/123/calendars/tasks/", "Reminders", comps=("VTODO",)))
EVENT_REPORT = _ms('<d:response><d:href>/123/calendars/home/a.ics</d:href><d:propstat><d:prop><c:calendar-data>'
                   + EVENTS.replace("&", "&amp;").replace("<", "&lt;") +
                   '</c:calendar-data></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>')


class Recorded:
    """An opener that replays recorded server answers and keeps what was asked."""

    def __init__(self, script):
        self.script, self.calls = list(script), []

    def open(self, req, timeout=None):
        self.calls.append((req.get_method(), req.full_url, dict(req.header_items())))
        status, headers, body = self.script.pop(0)
        if status >= 300:
            raise urllib.error.HTTPError(req.full_url, status, "x", headers, io.BytesIO(body.encode()))

        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        r = R(body.encode())
        r.status, r.headers = status, headers
        return r


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.delenv(TEST_OVERRIDE, raising=False)
    monkeypatch.setattr("freebusy.netguard._resolve", PUBLIC)


def test_icloud_style_discovery_and_fallback_to_stripped_events(public_dns):
    rec = Recorded([(301, {"Location": "https://caldav.icloud.com/"}, ""), (207, {}, PRINCIPAL), (207, {}, HOME), (207, {}, CALS),
                    (403, {}, "<error/>"), (207, {}, EVENT_REPORT), (207, {}, EVENT_REPORT)])
    c = CalDAVClient("https://caldav.icloud.com/", "dana@icloud.com", lambda: "abcd-efgh-ijkl-mnop", opener=rec)
    cals = c.discover()
    assert [(x.name, x.writable) for x in cals] == [("Home", True), ("Work (shared)", False)]      # VTODO-only list dropped
    assert cals[0].url == "https://p42-caldav.icloud.com:443/123/calendars/home/"
    a, b = datetime(2026, 11, 16, tzinfo=UTC), datetime(2026, 11, 23, tzinfo=UTC)
    busy = c.busy(cals[0].url, a, b, "America/Los_Angeles")
    assert len(busy) == 2 and c.fb_supported[cals[0].url] is False
    c.busy(cals[0].url, a, b, "America/Los_Angeles")                          # remembered: straight to the events query
    methods = [m for m, _, _ in rec.calls]
    assert methods == ["PROPFIND", "PROPFIND", "PROPFIND", "PROPFIND", "REPORT", "REPORT", "REPORT"]
    assert all("Authorization" in h for _, _, h in rec.calls)                  # caldav.icloud.com and its p42 sibling only


def test_a_redirect_to_a_private_address_is_refused(monkeypatch):
    monkeypatch.delenv(TEST_OVERRIDE, raising=False)
    monkeypatch.setattr("freebusy.netguard._resolve", lambda h, p: ["10.0.0.7"] if h == "intranet.example" else ["17.42.251.41"])
    rec = Recorded([(301, {"Location": "https://intranet.example/admin"}, "")])
    c = CalDAVClient("https://dav.example.org/cal/", "dana", lambda: "pw", opener=rec)
    with pytest.raises(UnsafeTarget):
        c.request("PROPFIND", "https://dav.example.org/cal/", b"", "0")
    assert len(rec.calls) == 1


def test_the_password_is_not_sent_to_another_site(public_dns):
    rec = Recorded([(207, {}, PRINCIPAL)])
    c = CalDAVClient("https://dav.fastmail.com/", "dana", lambda: "pw", opener=rec)
    c.request("PROPFIND", "https://elsewhere.example/x", b"", "0")
    assert "Authorization" not in rec.calls[0][2]


def test_wrong_password_and_missing_password(public_dns):
    c = CalDAVClient("https://caldav.example.org/dav/", "dana", lambda: "pw", opener=Recorded([(401, {}, "")]))
    with pytest.raises(CalDAVError, match="401"):
        c.discover()
    c = CalDAVClient("https://caldav.example.org/dav/", "dana", lambda: None, opener=Recorded([]))
    with pytest.raises(CalDAVError, match="keychain"):
        c.discover()


# ---- read many, hold on one ------------------------------------------------------------------

class FakeClient:
    def __init__(self, busy):
        self._busy, self.puts, self.deleted = busy, [], []

    def busy(self, url, a, b, zone):
        return list(self._busy.get(url, []))

    def put_event(self, url, uid, body):
        self.puts.append((url, uid, body))
        return url + uid + ".ics"

    def delete(self, href):
        self.deleted.append(href)


def test_read_many_hold_on_one_caldav(tmp_path):
    a = FakeClient({"https://x/a/": [Slot("2026-11-18T09:00:00-08:00", "2026-11-18T10:00:00-08:00")]})
    b = FakeClient({"https://y/b/": [Slot("2026-11-18T11:00:00-08:00", "2026-11-18T12:00:00-08:00")], "https://y/w/": []})
    cal = RoutedCalendar({"caldav:a": a, "caldav:b": b}, [ref("caldav:a", "https://x/a/"), ref("caldav:b", "https://y/b/")],
                         ref("caldav:b", "https://y/w/"), "America/Los_Angeles", meeting_link="https://meet.jit.si/dana",
                         organizer="dana@x.example")
    from freebusy.adapters.base import all_busy
    busy = all_busy(cal, datetime(2026, 11, 18, tzinfo=UTC), datetime(2026, 11, 19, tzinfo=UTC))
    assert len(busy) == 2
    slot = Slot("2026-11-19T10:00:00-08:00", "2026-11-19T10:30:00-08:00")
    eid = cal.create_event(cal.write_calendar, slot, "Dana and Lukas", ["lukas@z.example", "telegram.1@chat.invalid"])
    body = "\n".join(ics.unfold(b.puts[-1][2]))
    assert "https://meet.jit.si/dana" in body and "mailto:lukas@z.example" in body and "chat.invalid" not in body
    assert cal.join_link(eid) == "https://meet.jit.si/dana"
    with pytest.raises(WrongCalendar):
        cal.create_event(ref("caldav:a", "https://x/a/"), slot, "x", [])
    with pytest.raises(WrongCalendar):
        cal.delete_event(cal.write_calendar, "https://y/w/someone-elses-event.ics")      # only our own events
    cal.delete_event(cal.write_calendar, eid)
    assert b.deleted == [eid] and not a.puts


def test_local_hold_store_and_its_feed(tmp_path):
    store = LocalHoldStore(tmp_path)
    cal = RoutedCalendar({}, [], LOCAL, "America/Los_Angeles", store, meeting_link="https://whereby.com/dana")
    slot = Slot("2026-11-19T10:00:00-08:00", "2026-11-19T10:30:00-08:00")
    hold = cal.create_event(LOCAL, slot, "Hold", [], tentative=True)
    win = (datetime(2026, 11, 19, tzinfo=UTC), datetime(2026, 11, 20, 12, tzinfo=UTC))
    assert cal.free_busy(LOCAL, *win) == []                         # holds do not count as busy
    booked = cal.create_event(LOCAL, slot, "Dana and Lukas", ["l@x.example"], reminders=[{"method": "popup", "minutes": 10}])
    assert cal.free_busy(LOCAL, *win) == [slot]
    feed = (tmp_path / "feed" / "freebusy.ics").read_text()
    assert feed.count("BEGIN:VEVENT") == 2 and "https://whereby.com/dana" in feed and "TRIGGER:-PT10M" in feed
    cal.delete_event(LOCAL, hold)
    assert (tmp_path / "feed" / "freebusy.ics").read_text().count("BEGIN:VEVENT") == 1
    assert LocalHoldStore(tmp_path).events[0]["id"] == booked                # it survives a restart


def test_the_ledger_mirrors_holds_and_removes_them(tmp_path):
    store = LocalHoldStore(tmp_path)
    cal = RoutedCalendar({}, [], LOCAL, "UTC", store)
    errors = []
    led = HoldLedger(tmp_path / "holds.json", mirror=CalendarMirror(cal, on_error=lambda w, e: errors.append(w)))
    now = datetime(2026, 11, 9, 9, tzinfo=UTC)
    s1, s2 = Slot("2026-11-12T17:00:00+00:00", "2026-11-12T17:30:00+00:00"), Slot("2026-11-13T17:00:00+00:00", "2026-11-13T17:30:00+00:00")
    held = led.hold_offer([s1, s2], "t1", "lukas", now)
    assert all(h.event_id for h in held) and len(store.events) == 2 and all(e["tentative"] for e in store.events)
    led.book("t1", s1)
    assert store.events == [] and not errors
    led.hold_offer([s1], "t2", "nora", now)
    led.expire(now + timedelta(hours=49))
    assert store.events == []
    assert HoldLedger(tmp_path / "holds.json").holds == []


# ---- mail ------------------------------------------------------------------------------------

RAW = (b"From: Lukas Brenner <Lukas.Brenner@alpine.example>\r\nTo: Dana+agent@example.com\r\nCc: dana@example.com\r\n"
       b"Subject: Re: Intro call\r\nMessage-ID: <m2@alpine.example>\r\nIn-Reply-To: <m1@example.com>\r\n"
       b"References: <m0@example.com> <m1@example.com>\r\nDate: Mon, 09 Nov 2026 17:42:00 +0100\r\nAuto-Submitted: no\r\n"
       b"X-Secret-Header: never kept\r\nMIME-Version: 1.0\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
       b"<p>Can we do <b>Tuesday 2:30</b>?</p><script>x()</script><p>Lukas</p>")


def test_a_message_becomes_an_email_with_only_the_headers_the_engine_needs():
    e, mid, refs = to_email(RAW)
    assert e.sender == "lukas.brenner@alpine.example" and e.to == ["dana+agent@example.com"] and e.cc == ["dana@example.com"]
    assert e.thread_id == "mail:m0@example.com" and mid == "<m2@alpine.example>" and refs[-1] == "<m1@example.com>"
    assert "Tuesday 2:30" in e.body and "<b>" not in e.body and "x()" not in e.body
    assert e.headers == {"Auto-Submitted": "no"} and e.date.endswith("+0100")


def test_plaintext_mail_is_refused_outside_tests(monkeypatch):
    monkeypatch.delenv(TEST_OVERRIDE, raising=False)
    monkeypatch.setattr("freebusy.netguard._resolve", lambda h, p: ["127.0.0.1"])
    m = ImapSmtpMail({"imap_host": "localhost", "imap_port": 3143, "smtp_host": "localhost", "smtp_port": 3025, "security": "plain",
                      "username": "a"}, lambda: "pw", "a@x.example", [])
    with pytest.raises(UnsafeTarget):
        m.imap()
    monkeypatch.setattr("freebusy.netguard._resolve", PUBLIC)
    with pytest.raises(UnsafeTarget):
        m.smtp()


def test_xoauth2_hook_shape_and_refusal_without_a_token(monkeypatch):
    assert xoauth2_string("a@x.example", "tok") == "user=a@x.example\x01auth=Bearer tok\x01\x01"
    monkeypatch.setattr("freebusy.netguard._resolve", lambda h, p: ["127.0.0.1"])
    monkeypatch.setenv(TEST_OVERRIDE, "1")
    m = ImapSmtpMail({"imap_host": "localhost", "imap_port": 1, "smtp_host": "localhost", "smtp_port": 1, "auth": "xoauth2",
                      "security": "plain", "username": "a"}, lambda: None, "a@x.example", [])
    with pytest.raises(Exception):
        m.imap()                  # nothing listens on port 1; and with no token XOAUTH2 would refuse before signing in


# ---- providers and the keychain ------------------------------------------------------------------

def test_provider_guesses():
    assert guess("dana@icloud.com").caldav == "https://caldav.icloud.com/"
    assert guess("dana@fastmail.com").smtp == ("smtp.fastmail.com", 465)
    assert guess("dana@gmail.com").caldav is None and "0.4" in guess("dana@gmail.com").note
    assert guess("dana@outlook.com").imap is None and "0.4" in guess("dana@outlook.com").note
    other = guess("dana@whitfield.example")
    assert other.key == "other" and other.imap == ("imap.whitfield.example", 993)


def test_keychain_prompt_never_echoes(capsys):
    keychain.use_backend(keychain.MemoryBackend())
    try:
        answers = iter(["s3cret-app-pw", "s3cret-app-pw"])
        out = keychain.prompt_and_store("caldav:dana@icloud.com", getpass=lambda _: next(answers))
        assert "s3cret" not in out and keychain.get("caldav:dana@icloud.com") == "s3cret-app-pw"
        answers = iter(["one", "two"])
        assert "differ" in keychain.prompt_and_store("mail:x", getpass=lambda _: next(answers)) and not keychain.has("mail:x")
    finally:
        keychain.use_backend(None)
