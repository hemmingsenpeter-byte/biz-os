"""Seed memory from a directory of markdown files (the `memory_profile` of a composition)."""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from biz_kernel.memory.embedder import Embedder
from biz_kernel.memory.models import MemoryCite, MemoryNote
from biz_kernel.memory.service import Cite, ingest_source, write_draft

_SECTION = re.compile(r"^## +(.+?)\s*$", re.M)


def load_profile(
    session: Session,
    directory: str | Path,
    embedder: Embedder,
    actor: str = "seed",
    scope: str = "business",
) -> list[MemoryNote]:
    """Ingest each *.md as a source; one draft note per `## ` section, citing its first line.

    Idempotent: a section already noted from the same source is skipped.
    """
    created: list[MemoryNote] = []
    for path in sorted(Path(directory).glob("*.md")):
        content = path.read_text()
        src = ingest_source(session, "document", f"file:{path.name}", content)
        marks = list(_SECTION.finditer(content))
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(content)
            body = content[m.end() : end].strip()
            if not body:
                continue
            title = m.group(1)
            dup = session.scalar(
                select(MemoryNote.id)
                .join(MemoryCite, MemoryCite.note_id == MemoryNote.id)
                .where(
                    MemoryCite.source_id == src.id,
                    MemoryNote.title == title,
                    MemoryNote.scope == scope,
                )
                .limit(1)
            )
            if dup:
                continue
            quote = next(line.strip() for line in body.splitlines() if line.strip())
            note = write_draft(
                session,
                actor,
                scope,
                "profile",
                title,
                body,
                [Cite(src.id, quote, f"{path.name}#{title}")],
                embedder,
            )
            created.append(note)
    return created
