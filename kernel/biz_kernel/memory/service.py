"""Cited memory: ingest sources, write drafts, curate, recall by scope, undo any write."""

from __future__ import annotations

import fnmatch
import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session

from biz_kernel.audit.log import record
from biz_kernel.memory.embedder import Embedder
from biz_kernel.memory.models import (
    MemoryCite,
    MemoryNote,
    MemorySource,
    MemoryWrite,
)

EXCERPT_MAX = 300
_FIELDS = ("scope", "type", "title", "body", "status")


class MemoryRuleError(Exception):
    """Raised when a memory rule would be broken (bad quote, no cite, unknown id)."""


@dataclass(frozen=True)
class Cite:
    source_id: uuid.UUID
    quote: str
    locator: str | None = None


@dataclass(frozen=True)
class CitedExcerpt:
    note_id: uuid.UUID
    title: str
    excerpt: str
    source_uri: str
    quote: str
    locator: str | None
    score: float


def _norm(s: str) -> str:
    return " ".join(s.split())


def ingest_source(session: Session, kind: str, uri: str, content: bytes | str) -> MemorySource:
    """Store a source verbatim; the same bytes are stored once (dedupe by sha256)."""
    raw = content if isinstance(content, bytes) else content.encode()
    sha = hashlib.sha256(raw).hexdigest()
    existing = session.scalar(select(MemorySource).where(MemorySource.sha256 == sha))
    if existing:
        return existing
    src = MemorySource(kind=kind, uri=uri, sha256=sha, content=raw.decode(errors="replace"))
    session.add(src)
    session.flush()
    return src


def _snapshot(note: MemoryNote) -> dict[str, Any]:
    snap: dict[str, Any] = {f: getattr(note, f) for f in _FIELDS}
    snap["embedding"] = None if note.embedding is None else [float(x) for x in note.embedding]
    return snap


def _log_write(
    session: Session,
    note: MemoryNote,
    op: str,
    before: dict[str, Any] | None,
    actor: str,
) -> MemoryWrite:
    w = MemoryWrite(note_id=note.id, op=op, before=before, after=_snapshot(note), actor=actor)
    session.add(w)
    session.flush()
    record(session, actor, "memory_write", str(note.id), op=op, write_id=str(w.id))
    return w


def _embed(embedder: Embedder, title: str, body: str) -> list[float]:
    return embedder.embed([f"{title}\n{body}"])[0]


def _get(session: Session, note_id: uuid.UUID) -> MemoryNote:
    note = session.get(MemoryNote, note_id)
    if note is None:
        raise MemoryRuleError(f"unknown note {note_id}")
    return note


def write_draft(
    session: Session,
    actor: str,
    scope: str,
    type: str,
    title: str,
    body: str,
    cites: list[Cite],
    embedder: Embedder,
) -> MemoryNote:
    """Create a draft note. Every quote must appear verbatim in its source."""
    if not cites:
        raise MemoryRuleError("a note needs at least one citation")
    checked: list[Cite] = []
    for c in cites:
        src = session.get(MemorySource, c.source_id)
        if src is None:
            raise MemoryRuleError(f"unknown source {c.source_id}")
        if not c.quote.strip() or _norm(c.quote) not in _norm(src.content):
            raise MemoryRuleError("quote does not appear in the source")
        checked.append(c)
    note = MemoryNote(
        scope=scope,
        type=type,
        title=title,
        body=body,
        status="draft",
        embedding=_embed(embedder, title, body),
        created_by=actor,
    )
    session.add(note)
    session.flush()
    for c in checked:
        session.add(
            MemoryCite(note_id=note.id, source_id=c.source_id, quote=c.quote, locator=c.locator)
        )
    session.flush()
    _log_write(session, note, "draft", None, actor)
    return note


def update_note(
    session: Session,
    note_id: uuid.UUID,
    actor: str,
    embedder: Embedder,
    *,
    title: str | None = None,
    body: str | None = None,
) -> MemoryNote:
    """Change title/body. A curated note goes back to draft: edits need re-approval."""
    note = _get(session, note_id)
    before = _snapshot(note)
    if title is not None:
        note.title = title
    if body is not None:
        note.body = body
    if note.status == "curated":
        note.status = "draft"
    note.embedding = _embed(embedder, note.title, note.body)
    session.flush()
    _log_write(session, note, "update", before, actor)
    return note


def retire_note(session: Session, note_id: uuid.UUID, actor: str) -> MemoryNote:
    note = _get(session, note_id)
    before = _snapshot(note)
    note.status = "retired"
    session.flush()
    _log_write(session, note, "retire", before, actor)
    return note


def curate(session: Session, note_id: uuid.UUID, by: str) -> MemoryNote:
    """Promote draft to curated (owner approval or curation SOP; `by` is recorded)."""
    note = _get(session, note_id)
    n = session.scalar(
        select(func.count()).select_from(MemoryCite).where(MemoryCite.note_id == note.id)
    )
    if not n:
        raise MemoryRuleError("a note without a citation cannot be curated")
    if note.status != "draft":
        raise MemoryRuleError(f"only drafts can be curated (note is {note.status})")
    before = _snapshot(note)
    note.status = "curated"
    session.flush()
    _log_write(session, note, "curate", before, by)
    return note


def undo(session: Session, write_id: uuid.UUID, actor: str) -> MemoryNote:
    """Restore the note to the state before `write_id`; the undo is itself a reversible write.

    Undoing a note's creation retires it (notes are never deleted).
    """
    w = session.get(MemoryWrite, write_id)
    if w is None:
        raise MemoryRuleError(f"unknown write {write_id}")
    note = _get(session, w.note_id)
    current = _snapshot(note)
    target = w.before
    if target is None:
        note.status = "retired"
    else:
        for f in _FIELDS:
            setattr(note, f, target[f])
        note.embedding = target.get("embedding")
    session.flush()
    _log_write(session, note, "undo", current, actor)
    return note


# --- recall -----------------------------------------------------------------------------


def _like(pattern: str) -> str:
    esc = pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return esc.replace("*", "%")


def scope_matches(scope: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(scope, p) for p in patterns)


def _scope_clause(patterns: list[str]) -> Any:
    return or_(*[MemoryNote.scope.like(_like(p), escape="\\") for p in patterns])


def _tsquery(query: str) -> str:
    toks = re.findall(r"[a-z0-9]+", query.lower())
    return " | ".join(dict.fromkeys(toks))


def recall(
    session: Session,
    query: str,
    scopes: list[str],
    role_sources: list[str],
    embedder: Embedder,
    k: int = 5,
    include_drafts: bool = False,
) -> list[CitedExcerpt]:
    """Hybrid (cosine + full-text, reciprocal-rank fused) recall of short cited excerpts.

    Notes must match BOTH a requested scope and a declared role source.
    """
    if not scopes or not role_sources:
        return []
    statuses = ["curated", "draft"] if include_drafts else ["curated"]
    allowed = and_(
        MemoryNote.status.in_(statuses), _scope_clause(scopes), _scope_clause(role_sources)
    )
    pool = max(k * 4, 20)
    qvec = embedder.embed([query])[0]
    vec_ids = list(
        session.scalars(
            select(MemoryNote.id)
            .where(allowed, MemoryNote.embedding.is_not(None))
            .order_by(MemoryNote.embedding.cosine_distance(qvec))
            .limit(pool)
        )
    )
    fts_ids: list[uuid.UUID] = []
    tsq = _tsquery(query)
    if tsq:
        q = func.to_tsquery(text("'simple'::regconfig"), tsq)
        fts_ids = list(
            session.scalars(
                select(MemoryNote.id)
                .where(allowed, MemoryNote.search.op("@@")(q))
                .order_by(func.ts_rank(MemoryNote.search, q).desc())
                .limit(pool)
            )
        )
    scores: dict[uuid.UUID, float] = {}
    for ids in (vec_ids, fts_ids):
        for rank, nid in enumerate(ids):
            scores[nid] = scores.get(nid, 0.0) + 1.0 / (60 + rank + 1)
    top = sorted(scores, key=lambda n: scores[n], reverse=True)[:k]
    out: list[CitedExcerpt] = []
    for nid in top:
        note = session.get(MemoryNote, nid)
        assert note is not None
        cite = session.execute(
            select(MemoryCite, MemorySource)
            .join(MemorySource, MemorySource.id == MemoryCite.source_id)
            .where(MemoryCite.note_id == nid)
            .order_by(MemoryCite.id)
            .limit(1)
        ).first()
        if cite is None:
            continue  # defensive: uncited notes are never recalled
        c, s = cite
        out.append(
            CitedExcerpt(
                note_id=nid,
                title=note.title,
                excerpt=_excerpt(note.body, query),
                source_uri=s.uri,
                quote=c.quote[:EXCERPT_MAX],
                locator=c.locator,
                score=scores[nid],
            )
        )
    return out


def _excerpt(body: str, query: str) -> str:
    text_ = _norm(body)
    if len(text_) <= EXCERPT_MAX:
        return text_
    toks = set(re.findall(r"[a-z0-9]+", query.lower()))
    sentences = re.split(r"(?<=[.!?])\s+", text_)
    best = max(sentences, key=lambda s: len(toks & set(re.findall(r"[a-z0-9]+", s.lower()))))
    return best[: EXCERPT_MAX - 1] + "…" if len(best) > EXCERPT_MAX else best
