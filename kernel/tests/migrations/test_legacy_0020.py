"""The two historical 0020 schemas upgrade to the same union without rewriting data."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from alembic.script import ScriptDirectory
from biz_kernel import migrations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError


@pytest.mark.parametrize("origin", ["0019", "control", "tax", "combined"])
def test_reconciles_every_legacy_shape_and_preserves_records(fresh_db: str, origin: str) -> None:
    migrations.upgrade(fresh_db, "0020" if origin in {"control", "combined"} else "0019")
    engine = create_engine(fresh_db)
    sentinel = uuid.uuid4()
    if origin in {"tax", "combined"}:
        with engine.begin() as connection:
            for table in ("cap_invoicing_invoices", "cap_payables_bills"):
                connection.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN tax NUMERIC(16,2) NOT NULL DEFAULT 0")
                )
            connection.execute(text("UPDATE alembic_version SET version_num = '0020'"))
            connection.execute(
                text(
                    "INSERT INTO cap_invoicing_invoices "
                    "(id,job_id,party_id,reference,currency,lines,total,tax,"
                    "status,paid,reminders_sent,created_at) "
                    "VALUES (:id,:job,:party,'historic','NZD','[]',100,15,'posted',40,1,now())"
                ),
                {"id": sentinel, "job": uuid.uuid4(), "party": uuid.uuid4()},
            )
            connection.execute(
                text(
                    "INSERT INTO cap_payables_bills "
                    "(id,supplier_party_id,number,lines,total,tax,currency,status,"
                    "problems,created_at) "
                    "VALUES (:id,:party,'historic','[]',200,30,'NZD','posted','[]',now())"
                ),
                {"id": sentinel, "party": uuid.uuid4()},
            )
    if origin in {"control", "combined"}:
        with engine.begin() as connection:
            connection.execute(
                text(
                    'INSERT INTO control_pauses(id,at,paused,"by",reason) '
                    "VALUES (:id,now(),true,'owner','historic pause')"
                ),
                {"id": sentinel},
            )
            connection.execute(
                text(
                    "INSERT INTO approval_corrections "
                    '(id,at,op_id,actor,tool,from_version,to_version,"before","after","by",reason) '
                    "VALUES (:id,now(),:op,'r','t',1,2,'{}','{}','owner','historic correction')"
                ),
                {"id": sentinel, "op": uuid.uuid4()},
            )
    migrations.upgrade(fresh_db)
    migrations.upgrade(fresh_db)  # repeated startup must be a no-op
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0021"
        if origin in {"tax", "combined"}:
            assert connection.execute(
                text(
                    "SELECT total,tax,paid,status,reminders_sent "
                    "FROM cap_invoicing_invoices WHERE id=:id"
                ),
                {"id": sentinel},
            ).one() == (Decimal(100), Decimal(15), Decimal(40), "posted", 1)
            assert connection.execute(
                text("SELECT total,tax,status FROM cap_payables_bills WHERE id=:id"),
                {"id": sentinel},
            ).one() == (Decimal(200), Decimal(30), "posted")
        if origin in {"control", "combined"}:
            assert (
                connection.scalar(
                    text("SELECT reason FROM control_pauses WHERE id=:id"), {"id": sentinel}
                )
                == "historic pause"
            )
            assert (
                connection.scalar(
                    text("SELECT reason FROM approval_corrections WHERE id=:id"), {"id": sentinel}
                )
                == "historic correction"
            )
    for table in ("cap_invoicing_invoices", "cap_payables_bills"):
        tax = next(
            column for column in inspect(engine).get_columns(table) if column["name"] == "tax"
        )
        assert not tax["nullable"]
        assert getattr(tax["type"], "precision", None) == 16
        assert getattr(tax["type"], "scale", None) == 2
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as connection:
        connection.execute(
            text(
                'INSERT INTO control_pauses(id,at,paused,"by",reason) '
                "VALUES (gen_random_uuid(),now(),true,'owner','new')"
            )
        )
        connection.execute(text("UPDATE control_pauses SET reason='tampered'"))
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO approval_corrections "
                '(id,at,op_id,actor,tool,from_version,to_version,"before","after","by",reason) '
                "VALUES (gen_random_uuid(),now(),gen_random_uuid(),"
                "'r','t',1,2,'{}','{}','owner','new')"
            )
        )
        connection.execute(text("DELETE FROM approval_corrections"))
    engine.dispose()


def test_reconciliation_refuses_incompatible_tax_and_rolls_back(fresh_db: str) -> None:
    migrations.upgrade(fresh_db, "0020")
    engine = create_engine(fresh_db)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE cap_payables_bills ADD COLUMN tax TEXT"))
    with pytest.raises(RuntimeError, match="incompatible legacy tax"):
        migrations.upgrade(fresh_db)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0020"
    assert not any(
        c["name"] == "tax" for c in inspect(engine).get_columns("cap_invoicing_invoices")
    )
    engine.dispose()


def test_reconciliation_has_one_head_and_guards_tax_rollback(fresh_db: str) -> None:
    scripts = ScriptDirectory.from_config(migrations.make_config(fresh_db))
    assert scripts.get_heads() == ["0021"]
    migrations.upgrade(fresh_db)
    with pytest.raises(RuntimeError, match="drops tax data"):
        migrations.downgrade(fresh_db, "0020")
    with pytest.raises(RuntimeError, match="drops tax data"):
        migrations.downgrade(fresh_db, "0020", allow_data_loss="false")  # type: ignore[arg-type]


@pytest.mark.parametrize("variant", ["conditional", "different_function"])
def test_reconciliation_refuses_weakened_append_only_guards(fresh_db: str, variant: str) -> None:
    migrations.upgrade(fresh_db, "0020")
    engine = create_engine(fresh_db)
    with engine.begin() as connection:
        connection.execute(text("DROP TRIGGER control_pauses_append_only ON control_pauses"))
        function, condition = "biz_reject_mutation()", "WHEN (false)"
        if variant == "different_function":
            connection.execute(text("CREATE SCHEMA unrelated"))
            connection.execute(
                text(
                    "CREATE FUNCTION unrelated.biz_reject_mutation() RETURNS trigger "
                    "LANGUAGE plpgsql AS $$ BEGIN RETURN OLD; END $$"
                )
            )
            function, condition = "unrelated.biz_reject_mutation()", ""
        connection.execute(
            text(
                "CREATE TRIGGER control_pauses_append_only BEFORE UPDATE OR DELETE "
                f"ON control_pauses FOR EACH ROW {condition} EXECUTE FUNCTION {function}"
            )
        )
    with pytest.raises(RuntimeError, match="incompatible append-only trigger"):
        migrations.upgrade(fresh_db)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0020"
    engine.dispose()
