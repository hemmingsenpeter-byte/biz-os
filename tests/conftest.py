"""Shared fixtures: a real Postgres 16 + pgvector via pgserver; schema rebuilt per session."""

from __future__ import annotations

import tempfile
from collections.abc import Iterator

import pytest
from biz_kernel import db
from biz_kernel.models_registry import import_all_models
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    import pgserver

    srv = pgserver.get_server(tempfile.mkdtemp(prefix="bizpg-"), cleanup_mode="stop")
    srv.psql("CREATE DATABASE biz_test;")
    uri = srv.get_uri("biz_test").replace("postgresql://", "postgresql+psycopg://", 1)
    yield uri


@pytest.fixture(scope="session")
def engine(pg_url: str) -> Engine:
    eng = db.configure(pg_url)
    with eng.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    import_all_models()
    db.Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Each test runs inside a transaction that is rolled back afterwards."""
    conn = engine.connect()
    trans = conn.begin()
    sess = Session(bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
    try:
        yield sess
    finally:
        sess.close()
        trans.rollback()
        conn.close()
