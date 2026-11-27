"""Owner-only private tuning cohorts and promptly enqueued local comparisons."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from biz_kernel.api.routes_training import ReviewIn
from biz_kernel.auth.deps import DbSession, OwnerUser
from biz_kernel.training import cohorts, service
from biz_kernel.training.models import ComparisonJob, TrainingCohort


class CohortIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    version_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


class CohortReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    at: datetime
    cohort_id: uuid.UUID
    cohort_sha256: str
    by: str
    reason: str
    payload_sha256: str


class CohortOut(BaseModel):
    id: uuid.UUID
    at: datetime
    name: str
    task: str
    schema_sha256: str
    members: list[dict[str, Any]]
    created_by: str
    payload_sha256: str
    status: Literal["proposed", "accepted_for_testing"]
    review: CohortReviewOut | None
    private: Literal[True] = True
    split: Literal["tuning"] = "tuning"
    readiness_established: Literal[False] = False


class ComparisonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version_id: uuid.UUID
    cohort_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=120)
    model: dict[str, Any]


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    at: datetime
    job_id: uuid.UUID
    kind: str
    event_key: str
    payload: dict[str, Any]
    payload_sha256: str


class ComparisonOut(BaseModel):
    id: uuid.UUID
    at: datetime
    version_id: uuid.UUID
    cohort_id: uuid.UUID
    created_by: str
    idempotency_key: str
    payload_sha256: str
    contract: dict[str, Any]
    status: Literal["queued", "running", "completed", "failed", "unknown"]
    events: list[EventOut]
    summary: dict[str, Any] | None
    readiness_established: Literal[False] = False


def cohort_out(session: Session, row: TrainingCohort) -> CohortOut:
    cohorts.validate_cohort(session, row)
    review = cohorts.cohort_review_for(session, row)
    return CohortOut(
        **{
            key: getattr(row, key)
            for key in (
                "id",
                "at",
                "name",
                "task",
                "schema_sha256",
                "members",
                "created_by",
                "payload_sha256",
            )
        },
        status="accepted_for_testing" if review else "proposed",
        review=CohortReviewOut.model_validate(review) if review else None,
    )


def comparison_out(session: Session, row: ComparisonJob) -> ComparisonOut:
    row = cohorts.get_comparison(session, row.id)
    status, events, summary = cohorts.comparison_status(session, row)
    return ComparisonOut(
        **{
            key: getattr(row, key)
            for key in (
                "id",
                "at",
                "version_id",
                "cohort_id",
                "created_by",
                "idempotency_key",
                "payload_sha256",
                "contract",
            )
        },
        status=status,
        events=[EventOut.model_validate(event) for event in events],
        summary=summary,
    )


def _error(exc: service.TrainingError) -> HTTPException:
    code = (
        404
        if isinstance(exc, service.TrainingNotFound)
        else (409 if isinstance(exc, service.TrainingConflict) else 422)
    )
    return HTTPException(code, str(exc))


def router() -> APIRouter:
    def private_response(response: Response) -> None:
        response.headers["Cache-Control"] = "no-store"

    r = APIRouter(prefix="/training", tags=["training"], dependencies=[Depends(private_response)])

    @r.get("/cohorts", response_model=list[CohortOut])
    def listing(
        _: OwnerUser, session: DbSession, limit: int = Query(100, ge=1, le=250)
    ) -> list[CohortOut]:
        try:
            rows = session.scalars(
                select(TrainingCohort).order_by(TrainingCohort.at.desc()).limit(limit)
            )
            return [cohort_out(session, row) for row in rows]
        except service.TrainingError as exc:
            raise _error(exc) from exc

    @r.post("/cohorts", response_model=CohortOut)
    def create(body: CohortIn, owner: OwnerUser, session: DbSession) -> CohortOut:
        try:
            return cohort_out(
                session, cohorts.create_cohort(session, by=str(owner.id), **body.model_dump())
            )
        except service.TrainingError as exc:
            raise _error(exc) from exc

    @r.get("/cohorts/{cohort_id}", response_model=CohortOut)
    def one(cohort_id: uuid.UUID, _: OwnerUser, session: DbSession) -> CohortOut:
        try:
            return cohort_out(session, cohorts.get_cohort(session, cohort_id))
        except service.TrainingError as exc:
            raise _error(exc) from exc

    @r.post("/cohorts/{cohort_id}/attest", response_model=CohortOut)
    def attest(
        cohort_id: uuid.UUID, body: ReviewIn, owner: OwnerUser, session: DbSession
    ) -> CohortOut:
        try:
            cohorts.attest_cohort(session, cohort_id, by=str(owner.id), reason=body.reason)
            return cohort_out(session, cohorts.get_cohort(session, cohort_id))
        except service.TrainingError as exc:
            raise _error(exc) from exc

    @r.get("/comparisons", response_model=list[ComparisonOut])
    def jobs(
        _: OwnerUser, session: DbSession, limit: int = Query(100, ge=1, le=250)
    ) -> list[ComparisonOut]:
        try:
            rows = session.scalars(
                select(ComparisonJob).order_by(ComparisonJob.at.desc()).limit(limit)
            )
            return [comparison_out(session, row) for row in rows]
        except service.TrainingError as exc:
            raise _error(exc) from exc

    @r.post("/comparisons", response_model=ComparisonOut)
    def enqueue(body: ComparisonIn, owner: OwnerUser, session: DbSession) -> ComparisonOut:
        try:
            return comparison_out(
                session, cohorts.enqueue(session, by=str(owner.id), **body.model_dump())
            )
        except service.TrainingError as exc:
            raise _error(exc) from exc

    @r.get("/comparisons/{job_id}", response_model=ComparisonOut)
    def job(job_id: uuid.UUID, _: OwnerUser, session: DbSession) -> ComparisonOut:
        try:
            return comparison_out(session, cohorts.get_comparison(session, job_id))
        except service.TrainingError as exc:
            raise _error(exc) from exc

    return r
