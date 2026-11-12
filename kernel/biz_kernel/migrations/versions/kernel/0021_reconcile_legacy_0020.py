"""Reconcile either historical 0020 layout without changing its stored revision.

Canonical 0020 is controls/corrections. The other branch used the same ID for tax.
0021 inspects and preserves either layout, then supplies the missing half.
Downgrade targets canonical control-only 0020 and explicitly requires tax-drop consent.

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

import importlib.util
from decimal import Decimal, InvalidOperation
from pathlib import Path

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None
CONTROL_TABLES = {"control_pauses", "approval_corrections"}
TAX_TABLES = ("cap_invoicing_invoices", "cap_payables_bills")


def _controls() -> None:
    bind = op.get_bind()
    present = CONTROL_TABLES & set(sa.inspect(bind).get_table_names())
    if present and present != CONTROL_TABLES:
        raise RuntimeError("partial legacy control schema; inspect before upgrading")
    if not present:
        # The preceding migration is frozen; reuse its exact DDL for tax-only 0020.
        path = Path(__file__).with_name("0020_pause_and_corrections.py")
        spec = importlib.util.spec_from_file_location("_legacy_controls_0020", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.upgrade()
    common = {
        "id": (sa.Uuid, False),
        "at": (sa.DateTime, False),
        "by": (sa.String, False),
        "reason": (sa.Text, False),
    }
    required = {
        "control_pauses": {**common, "paused": (sa.Boolean, False)},
        "approval_corrections": {
            **common,
            "op_id": (sa.Uuid, False),
            "actor": (sa.String, False),
            "tool": (sa.String, False),
            "decision_type": (sa.String, True),
            "from_version": (sa.Integer, False),
            "to_version": (sa.Integer, False),
            "before": (postgresql.JSONB, False),
            "after": (postgresql.JSONB, False),
            "severity": (sa.String, True),
        },
    }
    for table, expected in required.items():
        inspector = sa.inspect(bind)
        actual = {column["name"]: column for column in inspector.get_columns(table)}
        for name, (kind, nullable) in expected.items():
            column = actual.get(name)
            if (
                column is None
                or not isinstance(column["type"], kind)
                or column["nullable"] != nullable
            ):
                raise RuntimeError(f"incompatible legacy column {table}.{name}")
            if name == "at" and not getattr(column["type"], "timezone", False):
                raise RuntimeError(f"legacy {table}.at must retain its time zone")
        if inspector.get_pk_constraint(table)["constrained_columns"] != ["id"]:
            raise RuntimeError(f"incompatible legacy primary key on {table}")
        trigger = bind.execute(
            sa.text(
                "SELECT t.tgenabled, t.tgtype, "
                "t.tgfoid = to_regprocedure('biz_reject_mutation()'), "
                "t.tgqual IS NULL, t.tgnargs, t.tgattr::text, t.tgisinternal "
                "FROM pg_trigger t "
                "WHERE t.tgrelid = to_regclass(:table) AND t.tgname = :name"
            ),
            {"table": table, "name": f"{table}_append_only"},
        ).first()
        if trigger is None:
            op.execute(
                sa.text(
                    f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE "
                    f"ON {table} FOR EACH ROW EXECUTE FUNCTION biz_reject_mutation()"
                )
            )
        elif trigger[0] not in ("O", "A") or tuple(trigger[1:]) != (27, True, True, 0, "", False):
            raise RuntimeError(f"incompatible append-only trigger on {table}")
    indexes = {
        index["name"]: index for index in sa.inspect(bind).get_indexes("approval_corrections")
    }
    index = indexes.get("ix_approval_corrections_op_id")
    if index is None:
        op.create_index("ix_approval_corrections_op_id", "approval_corrections", ["op_id"])
    elif index["column_names"] != ["op_id"] or index["unique"]:
        raise RuntimeError("incompatible correction lookup index")


def upgrade() -> None:
    _controls()
    bind = op.get_bind()
    for table in TAX_TABLES:
        if not sa.inspect(bind).has_table(table):
            raise RuntimeError(f"missing legacy accounting table {table}")
        columns = {column["name"]: column for column in sa.inspect(bind).get_columns(table)}
        tax = columns.get("tax")
        if tax is None:
            op.add_column(
                table, sa.Column("tax", sa.Numeric(16, 2), nullable=False, server_default="0")
            )
            continue
        kind = tax["type"]
        default = str(tax["default"] or "").split("::", 1)[0].strip("()' ")
        try:
            zero_default = Decimal(default).is_finite() and Decimal(default) == 0
        except InvalidOperation:
            zero_default = False
        if (
            not isinstance(kind, sa.Numeric)
            or kind.precision != 16
            or kind.scale != 2
            or tax["nullable"]
            or not zero_default
        ):
            raise RuntimeError(f"incompatible legacy tax column {table}.tax; no values changed")


def downgrade() -> None:
    if context.config.attributes.get("allow_data_loss") is not True:
        raise RuntimeError(
            "0021 downgrade drops tax data; restore a backup or explicitly allow_data_loss"
        )
    for table in TAX_TABLES:
        op.drop_column(table, "tax")
