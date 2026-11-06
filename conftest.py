"""Shared fixtures: a real Postgres 16 + pgvector via pgserver; schema rebuilt per session."""

from __future__ import annotations

import socket
import tempfile
import uuid
from collections.abc import Iterator

import pytest
from biz_kernel import db
from biz_kernel.models_registry import import_all_models
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session


@pytest.fixture(autouse=True, scope="session")
def _no_live_network():  # type: ignore[no-untyped-def]
    """Tests never touch the network: only Unix sockets (local Postgres) may connect."""
    real = socket.socket.connect

    def guarded(self, address):  # type: ignore[no-untyped-def]
        if self.family != socket.AF_UNIX:
            raise RuntimeError(f"live network blocked in tests: {address!r}")
        return real(self, address)

    socket.socket.connect = guarded  # type: ignore[method-assign]
    yield
    socket.socket.connect = real  # type: ignore[method-assign]


@pytest.fixture(scope="session")
def pg_server():  # type: ignore[no-untyped-def]
    import pgserver

    return pgserver.get_server(tempfile.mkdtemp(prefix="bizpg-"), cleanup_mode="stop")


def sa_url(srv, database: str) -> str:  # type: ignore[no-untyped-def]
    return srv.get_uri(database).replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture(scope="session")
def pg_url(pg_server) -> Iterator[str]:  # type: ignore[no-untyped-def]
    pg_server.psql("CREATE DATABASE biz_test;")
    yield sa_url(pg_server, "biz_test")


@pytest.fixture
def fresh_db(pg_server) -> str:  # type: ignore[no-untyped-def]
    """A brand-new committed database (for DBOS/subprocess tests). Returns its SQLAlchemy URL."""
    name = f"biz_{uuid.uuid4().hex[:10]}"
    pg_server.psql(f"CREATE DATABASE {name};")
    url = sa_url(pg_server, name)
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    eng.dispose()
    return url


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
