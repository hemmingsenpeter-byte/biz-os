"""cap_quoting_* tables: briefs, surveys, scopes, price book, estimates, proposals, variations.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)
MONEY = sa.Numeric(16, 2)
UNIT = sa.Numeric(16, 4)
JSONB = postgresql.JSONB()


def _pk(table: str) -> sa.PrimaryKeyConstraint:
    return sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}"))


def _ix(table: str, col: str, unique: bool = False) -> None:
    op.create_index(op.f(f"ix_{table}_{col}"), table, [col], unique=unique)


def upgrade() -> None:
    t = "cap_quoting_briefs"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("requirements", JSONB, nullable=False),
        sa.Column("constraints", JSONB, nullable=False),
        sa.Column("budget_hint", sa.Text(), nullable=True),
        sa.Column("created_at", TZ, nullable=False),
        _pk(t),
    )
    _ix(t, "job_id")

    t = "cap_quoting_surveys"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("checklist_answers", JSONB, nullable=False),
        sa.Column("measurements", JSONB, nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", TZ, nullable=False),
        _pk(t),
    )
    _ix(t, "job_id")

    t = "cap_quoting_scopes"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("areas", JSONB, nullable=False),
        sa.Column("exclusions", JSONB, nullable=False),
        sa.Column("assumptions", JSONB, nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("superseded_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", TZ, nullable=False),
        _pk(t),
        sa.UniqueConstraint("job_id", "version", name="uq_quoting_scope_version"),
    )
    _ix(t, "job_id")

    op.create_table(
        "cap_quoting_price_items",
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column("category", sa.String(80), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("unit", sa.String(30), nullable=False),
        sa.Column("unit_cost", UNIT, nullable=False),
        sa.Column("unit_price", UNIT, nullable=False),
        sa.Column("source", sa.String(200), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", TZ, nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_cap_quoting_price_items")),
    )
    _ix("cap_quoting_price_items", "category")

    t = "cap_quoting_price_history"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(80), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("unit", sa.String(30), nullable=False),
        sa.Column("unit_cost", UNIT, nullable=False),
        sa.Column("unit_price", UNIT, nullable=False),
        sa.Column("source", sa.String(200), nullable=False),
        sa.Column("valid_from", TZ, nullable=False),
        sa.Column("superseded_at", TZ, nullable=False),
        _pk(t),
    )
    _ix(t, "code")

    t = "cap_quoting_estimates"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("contingency_pct", sa.Numeric(7, 2), nullable=False),
        sa.Column("discount_pct", sa.Numeric(7, 2), nullable=False),
        sa.Column("total_cost", MONEY, nullable=False),
        sa.Column("total_price", MONEY, nullable=False),
        sa.Column("margin_pct", sa.Numeric(7, 2), nullable=False),
        sa.Column("margin_ok", sa.Boolean(), nullable=False),
        sa.Column("warnings", JSONB, nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("superseded_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", TZ, nullable=False),
        sa.ForeignKeyConstraint(
            ["scope_id"],
            ["cap_quoting_scopes.id"],
            name=op.f("fk_cap_quoting_estimates_scope_id_cap_quoting_scopes"),
        ),
        _pk(t),
        sa.UniqueConstraint("job_id", "version", name="uq_quoting_estimate_version"),
    )
    _ix(t, "job_id")

    t = "cap_quoting_estimate_lines"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("estimate_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(80), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("quantity", UNIT, nullable=False),
        sa.Column("unit", sa.String(30), nullable=False),
        sa.Column("unit_cost", UNIT, nullable=False),
        sa.Column("unit_price", UNIT, nullable=False),
        sa.Column("line_cost", MONEY, nullable=False),
        sa.Column("line_price", MONEY, nullable=False),
        sa.Column("unpriced", sa.Boolean(), nullable=False),
        sa.Column("cost_basis", JSONB, nullable=False),
        sa.ForeignKeyConstraint(
            ["estimate_id"],
            ["cap_quoting_estimates.id"],
            name=op.f("fk_cap_quoting_estimate_lines_estimate_id_cap_quoting_estimates"),
        ),
        _pk(t),
    )
    _ix(t, "estimate_id")

    t = "cap_quoting_proposals"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("estimate_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("valid_until", TZ, nullable=False),
        sa.Column("data", JSONB, nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("superseded_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", TZ, nullable=False),
        sa.ForeignKeyConstraint(
            ["estimate_id"],
            ["cap_quoting_estimates.id"],
            name=op.f("fk_cap_quoting_proposals_estimate_id_cap_quoting_estimates"),
        ),
        _pk(t),
        sa.UniqueConstraint("job_id", "version", name="uq_quoting_proposal_version"),
    )
    _ix(t, "job_id")

    t = "cap_quoting_variations"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("lines", JSONB, nullable=False),
        sa.Column("discount_pct", sa.Numeric(7, 2), nullable=False),
        sa.Column("total_cost", MONEY, nullable=False),
        sa.Column("total_price", MONEY, nullable=False),
        sa.Column("margin_pct", sa.Numeric(7, 2), nullable=False),
        sa.Column("margin_ok", sa.Boolean(), nullable=False),
        sa.Column("warnings", JSONB, nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        _pk(t),
    )
    _ix(t, "job_id")


def downgrade() -> None:
    for t in (
        "cap_quoting_variations",
        "cap_quoting_proposals",
        "cap_quoting_estimate_lines",
        "cap_quoting_estimates",
        "cap_quoting_price_history",
        "cap_quoting_price_items",
        "cap_quoting_scopes",
        "cap_quoting_surveys",
        "cap_quoting_briefs",
    ):
        op.drop_table(t)
