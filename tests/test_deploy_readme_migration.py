"""Local review proofs: run the README 'Migrating stored object paths' SQL against the schema."""

import re
from pathlib import Path

import pytest
from biz_kernel.memory import ingest_source
from biz_kernel.models_registry import import_all_models
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

import_all_models()

README = (Path(__file__).resolve().parents[1] / "deploy" / "README.md").read_text()
OLD, NEW = "file:///srv/biz/objects", "file:///var/lib/biz-os/objects"


def _list_query() -> str:
    m = re.search(r'-Atc "\n(.*?)"\s*\n', README, re.S)
    assert m
    return m.group(1)


def _rewrite_statements() -> list[str]:
    m = re.search(r"<<'SQL'\n(.*?)\n\s*SQL\n", README, re.S)
    assert m
    body = m.group(1).replace(":'old'", f"'{OLD}'").replace(":'new'", f"'{NEW}'")
    return [s.strip() for s in body.split(";") if s.strip() not in {"", "BEGIN", "COMMIT"}]


def test_step6_check_can_pass_with_profile_memory(session) -> None:
    # load_profile ingests app profile docs as file:<name> (kernel/biz_kernel/memory/profile.py).
    ingest_source(session, "document", "file:hours.md", "We open at nine.")
    session.flush()
    # README: drop the cap_voice_capture_notes line if that table does not exist.
    query = "\n".join(ln for ln in _list_query().splitlines() if "cap_voice" not in ln)
    rows = {r[0] for r in session.execute(text(query))}
    assert rows <= {NEW}, f"step 3/6 query lists non-object URIs: {rows}"


def test_rewrite_runs_when_memory_cites_an_old_object(session) -> None:
    ingest_source(session, "document", f"{OLD}/ab/{'0' * 64}.pdf", "Quote.")
    session.flush()
    voice = session.execute(text("SELECT to_regclass('cap_voice_capture_notes')")).scalar()
    for stmt in _rewrite_statements():
        if "cap_voice" in stmt and voice is None:
            continue  # README: drop the cap_voice_capture_notes line if that table is absent
        try:
            with session.begin_nested():
                session.execute(text(stmt))
        except DBAPIError as exc:
            pytest.fail(f"README rewrite fails: {stmt.splitlines()[0]} -> {exc.orig}")
