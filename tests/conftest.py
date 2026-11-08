import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from freebusy.adapters import fixture as fx  # noqa: E402
from freebusy.engine import Scheduler  # noqa: E402
from freebusy.holds import HoldLedger  # noqa: E402
from freebusy.policy import Gate  # noqa: E402


@pytest.fixture
def world(tmp_path):
    profile, cal, mail = fx.profile(), fx.FixtureCalendar(), fx.FixtureMail()
    ledger = HoldLedger(tmp_path / "holds.json")
    gate = Gate(profile, ledger, tmp_path / "audit.jsonl")
    sched = Scheduler(profile, cal, mail, ledger, gate, dict(fx.PEOPLE))
    return {"profile": profile, "cal": cal, "mail": mail, "ledger": ledger, "gate": gate, "sched": sched, "now": fx.NOW}
