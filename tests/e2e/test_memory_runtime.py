"""M1: the runtime seeds the composition's memory profile and roles recall it through the tool."""

from pathlib import Path

import pytest
from biz_kernel import db
from biz_kernel.memory.models import MemoryNote
from biz_kernel.memory.service import curate
from biz_kernel.models_registry import import_all_models
from biz_kernel.runtime import build_runtime
from biz_port_fakes import (
    FakeAccounting,
    FakeCalendar,
    FakeMail,
    FakeModel,
    FakeSign,
    FakeSpeech,
    FakeStorage,
    FakeTelephony,
)
from sqlalchemy import create_engine

ROOT = Path(__file__).parents[2]


@pytest.mark.parametrize(("app", "role"), [("demo", "demo_clerk"), ("hive", "receptionist")])
def test_seed_and_recall(fresh_db: str, app: str, role: str) -> None:
    import biz_cap_demo_desk  # noqa: F401

    eng = create_engine(fresh_db)
    import_all_models()
    db.Base.metadata.create_all(eng)
    db.configure(fresh_db)
    fakes = {
        "mail": FakeMail(),
        "telephony": FakeTelephony(),
        "calendar": FakeCalendar(),
        "model_local": FakeModel(),
        "model_cloud": FakeModel(),
        "storage": FakeStorage(),
        "sign": FakeSign(),
        "speech": FakeSpeech(),
        "accounting": FakeAccounting(),
    }
    rt = build_runtime(ROOT / "apps" / app, ROOT, ports=fakes)
    assert rt.seed_memory() > 0
    assert rt.seed_memory() == 0  # idempotent
    with db.session_scope() as s:
        for note in s.query(MemoryNote).all():
            curate(s, note.id, "owner")
        res = rt.registry.call(
            s,
            rt.policy,
            actor=role,
            name="memory.recall",
            args={"query": "how do we sound when we write"},
        )
        assert res.ran and res.output
        assert all(hit["quote"] and hit["source_uri"] for hit in res.output)
        assert all(len(hit["excerpt"]) <= 300 for hit in res.output)
    eng.dispose()
