"""Owner-only correction proposals and review for offline testing."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from biz_kernel.api.routes_approvals import CorrectionOut
from biz_kernel.approvals.models import Correction
from biz_kernel.auth.deps import DbSession, OwnerUser
from biz_kernel.training import service
from biz_kernel.training.models import DraftSnapshot, TrainingCandidate, TrainingVersion


class SnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    at: datetime
    run_id: str
    step_id: str
    actor: str
    task: str
    prompt_path: str
    system: str
    output_schema: dict[str, Any]
    untrusted_text: str
    output: dict[str, Any]
    tier: str
    backend: str
    configured_model: str | None
    model_revision: str | None
    system_sha256: str
    schema_sha256: str
    input_sha256: str
    output_sha256: str
    payload_sha256: str


class ReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    at: datetime
    candidate_id: uuid.UUID
    decision: Literal["accepted_for_testing", "rejected"]
    by: str
    reason: str


class CandidateOut(BaseModel):
    id: uuid.UUID
    at: datetime
    correction_id: uuid.UUID
    snapshot_id: uuid.UUID | None
    rule: str
    expected_output: dict[str, Any] | None
    created_by: str
    payload_sha256: str
    status: Literal["proposed", "accepted_for_testing", "rejected"]
    eligible_for_testing: bool
    incomplete_reasons: list[str]
    snapshot: SnapshotOut | None
    review: ReviewOut | None
    regression_case: dict[str, Any] | None
    readiness_established: Literal[False] = False


class CorrectionContextOut(BaseModel):
    correction: CorrectionOut
    snapshots: list[SnapshotOut]
    candidate_id: uuid.UUID | None


class CandidateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule: str = Field(min_length=1, max_length=8000)
    snapshot_id: uuid.UUID | None = None
    expected_output: dict[str, Any] | None = None


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="", max_length=2000)


class VersionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parent_id: uuid.UUID | None
    rule: str = Field(min_length=1, max_length=8000)
    system: str = Field(min_length=1, max_length=200000)
    expected_output: dict[str, Any] | None = None
    snapshot_id: uuid.UUID | None = None
    checks: list[dict[str, Any]] | None = None


class VersionReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    at: datetime
    version_id: uuid.UUID
    version_sha256: str
    decision: Literal["accepted_for_testing", "rejected"]
    by: str
    reason: str
    payload_sha256: str


class VersionOut(BaseModel):
    id: uuid.UUID
    at: datetime
    candidate_id: uuid.UUID
    number: int
    parent_id: uuid.UUID | None
    snapshot_id: uuid.UUID | None
    rule: str
    system: str
    expected_output: dict[str, Any] | None
    checks: list[dict[str, Any]]
    created_by: str
    payload_sha256: str
    task: str | None
    output_schema: dict[str, Any] | None = Field(alias="schema")
    baseline_system: str | None
    status: Literal["proposed", "accepted_for_testing", "rejected"]
    eligible_for_testing: bool
    incomplete_reasons: list[str]
    review: VersionReviewOut | None
    readiness_established: Literal[False] = False


def version_out(session: Session, version: TrainingVersion) -> VersionOut:
    snapshot = service.validate_version(session, version)
    review = service.version_review_for(session, version)
    reasons = service.version_incomplete_reasons(version, snapshot)
    return VersionOut(
        **{
            name: getattr(version, name)
            for name in (
                "id",
                "at",
                "candidate_id",
                "number",
                "parent_id",
                "snapshot_id",
                "rule",
                "system",
                "expected_output",
                "checks",
                "created_by",
                "payload_sha256",
            )
        },
        task=snapshot.task if snapshot else None,
        schema=snapshot.output_schema if snapshot else None,
        baseline_system=snapshot.system if snapshot else None,
        status=review.decision if review else "proposed",
        eligible_for_testing=not reasons,
        incomplete_reasons=reasons,
        review=VersionReviewOut.model_validate(review) if review else None,
    )


def candidate_out(session: Session, candidate: TrainingCandidate) -> CandidateOut:
    service.validate_candidate(session, candidate)
    review = service.review_for(session, candidate.id)
    snapshot = session.get(DraftSnapshot, candidate.snapshot_id) if candidate.snapshot_id else None
    reasons = service.incomplete_reasons(candidate)
    return CandidateOut(
        **{
            name: getattr(candidate, name)
            for name in (
                "id",
                "at",
                "correction_id",
                "snapshot_id",
                "rule",
                "expected_output",
                "created_by",
                "payload_sha256",
            )
        },
        status=review.decision if review else "proposed",
        eligible_for_testing=not reasons,
        incomplete_reasons=reasons,
        snapshot=SnapshotOut.model_validate(snapshot) if snapshot else None,
        review=ReviewOut.model_validate(review) if review else None,
        regression_case=service.regression_case(session, candidate),
    )


def correction_out(session: Session, source: Correction) -> CorrectionContextOut:
    candidate = service.candidate_for_correction(session, source.id)
    return CorrectionContextOut(
        correction=CorrectionOut.model_validate(source),
        snapshots=[
            SnapshotOut.model_validate(row) for row in service.eligible_snapshots(session, source)
        ],
        candidate_id=candidate.id if candidate else None,
    )


def _error(exc: service.TrainingError) -> HTTPException:
    status = (
        404
        if isinstance(exc, service.TrainingNotFound)
        else (409 if isinstance(exc, service.TrainingConflict) else 422)
    )
    return HTTPException(status, str(exc))


def router() -> APIRouter:
    def private_response(response: Response) -> None:
        response.headers["Cache-Control"] = "no-store"

    r = APIRouter(prefix="/training", tags=["training"], dependencies=[Depends(private_response)])

    @r.get("/corrections", response_model=list[CorrectionContextOut])
    def corrections(
        _: OwnerUser,
        session: DbSession,
        limit: Annotated[int, Query(ge=1, le=250)] = 100,
    ) -> list[CorrectionContextOut]:
        sources = session.scalars(
            select(Correction).order_by(Correction.at.desc(), Correction.id.desc()).limit(limit)
        )
        try:
            return [correction_out(session, source) for source in sources]
        except service.TrainingError as exc:
            raise _error(exc) from exc

    @r.get("/corrections/{correction_id}", response_model=CorrectionContextOut)
    def correction_detail(
        correction_id: uuid.UUID, _: OwnerUser, session: DbSession
    ) -> CorrectionContextOut:
        try:
            return correction_out(session, service.correction(session, correction_id))
        except service.TrainingError as exc:
            raise _error(exc) from exc

    @r.post("/corrections/{correction_id}/candidates", response_model=CandidateOut)
    def propose(
        correction_id: uuid.UUID, body: CandidateIn, owner: OwnerUser, session: DbSession
    ) -> CandidateOut:
        try:
            candidate = service.create_candidate(
                session,
                correction_id,
                rule=body.rule,
                by=str(owner.id),
                snapshot_id=body.snapshot_id,
                expected_output=body.expected_output,
            )
            return candidate_out(session, candidate)
        except service.TrainingError as exc:
            raise _error(exc) from exc

    @r.get("/candidates", response_model=list[CandidateOut])
    def candidates(
        _: OwnerUser,
        session: DbSession,
        limit: Annotated[int, Query(ge=1, le=250)] = 100,
    ) -> list[CandidateOut]:
        rows = session.scalars(
            select(TrainingCandidate)
            .order_by(TrainingCandidate.at.desc(), TrainingCandidate.id.desc())
            .limit(limit)
        )
        try:
            return [candidate_out(session, candidate) for candidate in rows]
        except service.TrainingError as exc:
            raise _error(exc) from exc

    @r.get("/candidates/{candidate_id}", response_model=CandidateOut)
    def candidate_detail(candidate_id: uuid.UUID, _: OwnerUser, session: DbSession) -> CandidateOut:
        try:
            return candidate_out(session, service.get_candidate(session, candidate_id))
        except service.TrainingError as exc:
            raise _error(exc) from exc

    def decide(
        candidate_id: uuid.UUID,
        body: ReviewIn,
        owner: Any,
        session: Session,
        decision: service.Decision,
    ) -> CandidateOut:
        try:
            service.decide(
                session, candidate_id, decision=decision, by=str(owner.id), reason=body.reason
            )
            return candidate_out(session, service.get_candidate(session, candidate_id))
        except service.TrainingError as exc:
            raise _error(exc) from exc

    @r.post("/candidates/{candidate_id}/review", response_model=CandidateOut)
    def review(
        candidate_id: uuid.UUID, body: ReviewIn, owner: OwnerUser, session: DbSession
    ) -> CandidateOut:
        return decide(candidate_id, body, owner, session, "accepted_for_testing")

    @r.post("/candidates/{candidate_id}/reject", response_model=CandidateOut)
    def reject(
        candidate_id: uuid.UUID, body: ReviewIn, owner: OwnerUser, session: DbSession
    ) -> CandidateOut:
        return decide(candidate_id, body, owner, session, "rejected")

    @r.get("/candidates/{candidate_id}/versions", response_model=list[VersionOut])
    def versions(candidate_id: uuid.UUID, _: OwnerUser, session: DbSession) -> list[VersionOut]:
        try:
            service.validate_candidate(session, service.get_candidate(session, candidate_id))
            rows = session.scalars(
                select(TrainingVersion)
                .where(TrainingVersion.candidate_id == candidate_id)
                .order_by(TrainingVersion.number.desc())
            )
            return [version_out(session, row) for row in rows]
        except service.TrainingError as exc:
            raise _error(exc) from exc

    @r.post("/candidates/{candidate_id}/versions", response_model=VersionOut)
    def new_version(
        candidate_id: uuid.UUID, body: VersionIn, owner: OwnerUser, session: DbSession
    ) -> VersionOut:
        try:
            row = service.create_version(
                session, candidate_id, by=str(owner.id), **body.model_dump()
            )
            return version_out(session, row)
        except service.TrainingError as exc:
            raise _error(exc) from exc

    @r.get("/versions/{version_id}", response_model=VersionOut)
    def one_version(version_id: uuid.UUID, _: OwnerUser, session: DbSession) -> VersionOut:
        try:
            return version_out(session, service.get_version(session, version_id))
        except service.TrainingError as exc:
            raise _error(exc) from exc

    def version_decision(
        version_id: uuid.UUID,
        body: ReviewIn,
        owner: Any,
        session: Session,
        decision: service.Decision,
    ) -> VersionOut:
        try:
            service.decide_version(
                session, version_id, decision=decision, by=str(owner.id), reason=body.reason
            )
            return version_out(session, service.get_version(session, version_id))
        except service.TrainingError as exc:
            raise _error(exc) from exc

    @r.post("/versions/{version_id}/review", response_model=VersionOut)
    def review_version(
        version_id: uuid.UUID, body: ReviewIn, owner: OwnerUser, session: DbSession
    ) -> VersionOut:
        return version_decision(version_id, body, owner, session, "accepted_for_testing")

    @r.post("/versions/{version_id}/reject", response_model=VersionOut)
    def reject_version(
        version_id: uuid.UUID, body: ReviewIn, owner: OwnerUser, session: DbSession
    ) -> VersionOut:
        return version_decision(version_id, body, owner, session, "rejected")

    return r
