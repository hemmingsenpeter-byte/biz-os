"""Database-level guard: tasks become done only via complete_task, with evidence.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from alembic import op
from biz_kernel.evidence.models import TASK_DONE_GUARD_DDL

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(TASK_DONE_GUARD_DDL.replace("%", "%%"))


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS tasks_done_guard ON tasks")
    op.execute("DROP FUNCTION IF EXISTS biz_guard_task_done()")
