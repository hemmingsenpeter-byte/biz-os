"""Fit-out extension table, one-to-one with a job record."""

from sqlalchemy import Column, Date, ForeignKey, Integer, MetaData, Numeric, String, Table, Uuid


def define(metadata: MetaData) -> None:
    Table(
        "ext_fitout_job",
        metadata,
        Column("job_id", Uuid, ForeignKey("jobs.id"), primary_key=True),
        Column("floor_area_m2", Numeric(10, 2), nullable=True),
        Column("headcount", Integer, nullable=True),
        Column("lease_end", Date, nullable=True),
        Column("base_build_standard", String(200), nullable=False, default=""),
    )
