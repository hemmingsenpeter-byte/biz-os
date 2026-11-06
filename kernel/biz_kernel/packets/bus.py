"""Packets: the only way parts pass information. Immutable, typed, versioned rows."""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import jsonschema
from sqlalchemy import ForeignKey, String, Text, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column

from biz_kernel.audit.log import APPEND_ONLY_TABLES, record
from biz_kernel.db import Base, utcnow, uuid7


class Packet(Base):
    __tablename__ = "packets"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    type: Mapped[str] = mapped_column(String(80), index=True)
    schema_version: Mapped[int]
    job_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    from_part: Mapped[str] = mapped_column(String(80))
    from_sop: Mapped[str | None] = mapped_column(String(120))
    to_parts: Mapped[list[str]] = mapped_column(ARRAY(String(80)), default=list)
    summary: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    open_questions: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("packets.id"))
    approved_by: Mapped[str | None] = mapped_column(String(120))
    goal_key: Mapped[str | None] = mapped_column(String(120))
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class PacketAck(Base):
    __tablename__ = "packet_acks"
    packet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("packets.id"), primary_key=True)
    part: Mapped[str] = mapped_column(String(80), primary_key=True)
    status: Mapped[str] = mapped_column(String(20))  # accepted | returned
    note: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(default=utcnow)


APPEND_ONLY_TABLES.append("packets")


class SchemaError(ValueError):
    pass


class SchemaRegistry:
    """Packet JSON Schemas keyed by (type, version). Loaded from packets/<type>/v<n>.json."""

    def __init__(self) -> None:
        self._schemas: dict[tuple[str, int], dict[str, Any]] = {}

    def register(self, type_: str, version: int, schema: dict[str, Any]) -> None:
        jsonschema.Draft202012Validator.check_schema(schema)
        self._schemas[(type_, version)] = schema

    def load_dir(self, root: Path) -> int:
        count = 0
        for path in sorted(root.glob("*/v*.json")):
            self.register(path.parent.name, int(path.stem[1:]), json.loads(path.read_text()))
            count += 1
        return count

    def get(self, type_: str, version: int) -> dict[str, Any]:
        try:
            return self._schemas[(type_, version)]
        except KeyError as exc:
            raise SchemaError(f"no schema for {type_}/v{version}") from exc

    def latest(self, type_: str) -> int:
        versions = [v for (t, v) in self._schemas if t == type_]
        if not versions:
            raise SchemaError(f"no schema for {type_}")
        return max(versions)

    def known(self) -> list[tuple[str, int]]:
        return sorted(self._schemas)

    def validate(self, type_: str, version: int, payload: dict[str, Any]) -> None:
        try:
            jsonschema.validate(payload, self.get(type_, version))
        except jsonschema.ValidationError as exc:
            raise SchemaError(f"{type_}/v{version}: {exc.message}") from exc


Subscriber = Callable[[Session, Packet], None]


class Bus:
    """Validates and writes packets, then notifies subscribers (the SOP runner hooks in here)."""

    def __init__(self, schemas: SchemaRegistry) -> None:
        self.schemas = schemas
        self._subs: dict[str, list[tuple[str, Subscriber]]] = defaultdict(list)

    def subscribe(self, type_: str, part: str, handler: Subscriber) -> None:
        self._subs[type_].append((part, handler))

    def subscribers(self, type_: str) -> list[str]:
        return [part for part, _ in self._subs.get(type_, [])]

    def publish(
        self,
        session: Session,
        *,
        type: str,
        payload: dict[str, Any],
        from_part: str,
        version: int | None = None,
        job_id: uuid.UUID | None = None,
        from_sop: str | None = None,
        summary: str = "",
        evidence: dict[str, Any] | None = None,
        open_questions: list[Any] | None = None,
        supersedes_id: uuid.UUID | None = None,
        approved_by: str | None = None,
        goal_key: str | None = None,
        idempotency_key: str | None = None,
    ) -> Packet:
        if idempotency_key:
            existing = session.scalar(
                select(Packet).where(Packet.idempotency_key == idempotency_key)
            )
            if existing is not None:
                return existing
        version = version or self.schemas.latest(type)
        self.schemas.validate(type, version, payload)
        packet = Packet(
            type=type,
            schema_version=version,
            job_id=job_id,
            from_part=from_part,
            from_sop=from_sop,
            to_parts=self.subscribers(type),
            summary=summary,
            payload=payload,
            evidence=evidence or {},
            open_questions=open_questions or [],
            supersedes_id=supersedes_id,
            approved_by=approved_by,
            goal_key=goal_key,
            idempotency_key=idempotency_key,
        )
        session.add(packet)
        session.flush()
        record(session, from_part, "packet", f"{type}/v{version}", packet_id=packet.id)
        for _, handler in self._subs.get(type, []):
            handler(session, packet)
        return packet

    def ack(
        self,
        session: Session,
        packet_id: uuid.UUID,
        part: str,
        status: str,
        note: str | None = None,
    ) -> PacketAck:
        if status not in ("accepted", "returned"):
            raise ValueError(status)
        ack = PacketAck(packet_id=packet_id, part=part, status=status, note=note)
        session.merge(ack)
        session.flush()
        return ack


def timeline(session: Session, job_id: uuid.UUID) -> list[Packet]:
    return list(session.scalars(select(Packet).where(Packet.job_id == job_id).order_by(Packet.id)))
