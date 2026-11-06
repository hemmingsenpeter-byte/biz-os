"""Demo extension table, one-to-one with a job record."""

from sqlalchemy import Column, ForeignKey, MetaData, String, Table, Uuid


def define(metadata: MetaData) -> None:
    Table(
        "ext_demo_job",
        metadata,
        Column("job_id", Uuid, ForeignKey("jobs.id"), primary_key=True),
        Column("trade_note", String(200), nullable=False, default=""),
    )
