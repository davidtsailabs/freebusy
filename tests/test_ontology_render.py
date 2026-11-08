from freebusy.adapters import fixture as fx
from freebusy.ontology import Hold, OwnerProfile, Slot, ThreadState, from_json, to_json
from freebusy.render import OwnerNote, Reply, Row, note_subject, render_owner_note, render_text


def test_profile_round_trips_through_json():
    p = fx.profile()
    q = from_json(OwnerProfile, to_json(p))
    assert q == p and q.groups["default"].windows["thu"][0].start == "08:00"


def test_hold_and_thread_state_round_trip():
    h = Hold(Slot("2026-11-12T13:30:00-08:00", "2026-11-12T14:00:00-08:00"), "t1", "lukas", "2026-11-14T13:30:00-08:00")
    assert from_json(Hold, to_json(h)) == h
    st = ThreadState("t1")
    st.move("offering", "3 slots")
    assert from_json(ThreadState, to_json(st)).history[0][1] == "offering"


def test_tiers_follow_the_profile():
    p = fx.profile()
    assert p.tier_for("introduced") == 2
    p.auto_book = False
    assert p.tier_for("introduced") == 1
    assert p.tier_for("unknown") == 0 and p.tier_for("owner") == 3
    assert p.tier_for("allowlisted", "marisol@cascadehealth.example") == 2


def test_glance_format():
    r = Reply(name="Lukas", lead="Dana can do 30 minutes on these days:", rows=[Row("Thu Nov 12", "1:30 to 4:00 pm PT")],
              ask="Reply with a letter.", zone_line="Times are Pacific.", owner_name="Dana Whitfield")
    t = render_text(r)
    assert t.splitlines()[0] == "Hi Lukas," and "A.  Thu Nov 12  1:30 to 4:00 pm PT" in t
    assert t.count("\n\n\n") == 0 and t.endswith("AI assistant to Dana Whitfield · handles scheduling only")


def test_owner_note_states():
    n = OwnerNote(state="Nothing fits", headline="Two rounds with Lukas found no time", who="Lukas Brenner", ask="Take it from here",
                  stops=["offered 3", "offered 3 more"], thread_ref="t1")
    text = render_owner_note(n)
    assert text.startswith("NOTHING FITS") and "Your move: Take it from here" in text and note_subject(n).startswith("Nothing fits:")
    booked = OwnerNote(state="Booked", headline="x", when="Thu Nov 12 · 1:30 pm PT", block_elsewhere=True)
    assert note_subject(booked) == "Booked: block Thu Nov 12 · 1:30 pm PT on your other calendars"
