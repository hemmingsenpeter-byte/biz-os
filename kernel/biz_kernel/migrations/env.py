"""Alembic environment: target metadata is every kernel and installed-capability table."""

from __future__ import annotations

import importlib
import pkgutil

from alembic import context
from sqlalchemy import create_engine, text

from biz_kernel.db import Base
from biz_kernel.models_registry import import_all_models


def _import_capabilities() -> None:
    for mod in pkgutil.iter_modules():
        if mod.name.startswith("biz_cap_"):
            pkg = importlib.import_module(mod.name)
            try:
                importlib.import_module(f"{mod.name}.models")
            except ModuleNotFoundError as exc:
                if exc.name != f"{mod.name}.models":
                    raise
            del pkg


import_all_models()
_import_capabilities()
target_metadata = Base.metadata
config = context.config


def _include_object(obj, name, type_, reflected, compare_to) -> bool:  # type: ignore[no-untyped-def]
    # Pack tables (ext_*) belong to pack branches, never to autogenerate of the kernel.
    return not (type_ == "table" and name is not None and name.startswith("ext_"))


def run() -> None:
    url = config.attributes.get("url") or config.get_main_option("sqlalchemy.url")
    if context.is_offline_mode():
        context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(str(url), future=True)
    with engine.connect() as conn:
        context.configure(
            connection=conn,
            target_metadata=target_metadata,
            include_object=_include_object,
            version_table_pk=True,
        )
        with context.begin_transaction():
            if conn.dialect.name == "postgresql":
                conn.execute(text("SELECT pg_advisory_xact_lock(4776437063156798791)"))
            context.run_migrations()
    engine.dispose()


run()
