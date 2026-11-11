from freebusy.demo import run_scenes


def test_all_thirteen_scenes_run():
    text = run_scenes(quiet=True)
    for n in range(1, 14):
        assert f"Scene {n}." in text
    assert "events created: 0 (the booking waits for Dana)" in text
    assert "travel buffers on: Owen's first Thursday row is \"Thu Nov 12  1:30" in text
    assert "TOMORROW: Priya Natarajan" in text and "Open: \"Should I bring the dosing data" in text
