"""Fit-out extension table for site access facts. `site_id` is the site's record id."""

from sqlalchemy import Column, MetaData, String, Table, Text, Uuid


def define(metadata: MetaData) -> None:
    Table(
        "ext_fitout_site",
        metadata,
        Column("site_id", Uuid, primary_key=True),
        Column("building_manager", String(200), nullable=False, default=""),
        Column("lift_rules", Text, nullable=False, default=""),
        Column("dock_rules", Text, nullable=False, default=""),
    )
