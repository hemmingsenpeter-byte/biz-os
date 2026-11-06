"""Quoting tools. All drafts or reads: nothing here sends, pays or signs."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

from biz_kernel.db import utcnow
from biz_kernel.policy.types import SideEffect
from biz_kernel.tools.registry import Tool, ToolRegistry
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from biz_cap_quoting import config, service
from biz_cap_quoting.models import (
    Brief,
    Estimate,
    EstimateLine,
    PriceHistory,
    PriceItem,
    Proposal,
    Scope,
    Survey,
    Variation,
)

CAP = "quoting"
DEFAULT_VALID_DAYS = 30


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required or [],
        "properties": props,
    }


_STR: dict[str, Any] = {"type": "string"}
_NUM: dict[str, Any] = {"type": ["number", "string"]}
_STRS: dict[str, Any] = {"type": "array", "items": _STR}
_LINE = _obj(
    {
        "code": _STR,
        "description": _STR,
        "quantity": _NUM,
        "unit": _STR,
        "unit_cost": _NUM,
        "unit_price": _NUM,
        "unpriced": {"type": "boolean"},
    },
    ["quantity"],
)
_ROW = _obj(
    {
        "code": _STR,
        "category": _STR,
        "description": _STR,
        "unit": _STR,
        "unit_cost": _NUM,
        "unit_price": _NUM,
    },
    ["code", "category", "description", "unit", "unit_cost", "unit_price"],
)


def _uid(value: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError as e:
        raise ValueError(f"{what} is not a valid id: {value!r}") from e


def _next_version(session: Session, model: Any, job_id: uuid.UUID) -> int:
    top = session.scalar(select(func.max(model.version)).where(model.job_id == job_id))
    return int(top or 0) + 1


def _supersede(session: Session, model: Any, job_id: uuid.UUID, new_id: uuid.UUID) -> None:
    for old in session.scalars(
        select(model).where(model.job_id == job_id, model.status == "current", model.id != new_id)
    ):
        old.status = "superseded"
        old.superseded_by = new_id


def _totals_out(t: service.Totals, currency: str) -> dict[str, Any]:
    return {
        "total_cost": str(t.total_cost),
        "total_price": str(t.total_price),
        "total_cost_minor": service.minor(t.total_cost),
        "total_price_minor": service.minor(t.total_price),
        "currency": currency,
        "margin_pct": float(t.margin_pct),
        "margin_ok": t.margin_ok,
        "warnings": t.warnings,
    }


def register(registry: ToolRegistry, ports: dict[str, Any]) -> None:
    def brief_write(
        session: Session,
        job_id: str,
        summary: str,
        requirements: list[str] | None = None,
        constraints: list[str] | None = None,
        budget_hint: str | None = None,
    ) -> dict[str, Any]:
        row = Brief(
            job_id=_uid(job_id, "job_id"),
            summary=summary,
            requirements=requirements or [],
            constraints=constraints or [],
            budget_hint=budget_hint,
        )
        session.add(row)
        session.flush()
        return {"brief_id": str(row.id)}

    def survey_record(
        session: Session,
        job_id: str,
        checklist_answers: dict[str, Any] | None = None,
        measurements: list[dict[str, Any]] | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        clean = [
            {"area": m["area"], "quantity": str(service.dec(m["quantity"])), "unit": m["unit"]}
            for m in measurements or []
        ]
        row = Survey(
            job_id=_uid(job_id, "job_id"),
            checklist_answers=checklist_answers or {},
            measurements=clean,
            notes=notes,
        )
        session.add(row)
        session.flush()
        return {"survey_id": str(row.id)}

    def scope_write(
        session: Session,
        job_id: str,
        areas: list[dict[str, Any]],
        exclusions: list[str] | None = None,
        assumptions: list[str] | None = None,
    ) -> dict[str, Any]:
        jid = _uid(job_id, "job_id")
        norm = [
            {
                "name": a["name"],
                "items": [
                    {**it, "quantity": str(service.dec(it["quantity"]))}
                    for it in a.get("items", [])
                ],
            }
            for a in areas
        ]
        row = Scope(
            job_id=jid,
            version=_next_version(session, Scope, jid),
            areas=norm,
            exclusions=exclusions or [],
            assumptions=assumptions or [],
        )
        session.add(row)
        session.flush()
        _supersede(session, Scope, jid, row.id)
        session.flush()
        return {"scope_id": str(row.id), "version": row.version}

    def price_book_read(
        session: Session, categories: list[str] | None = None, query: str | None = None
    ) -> dict[str, Any]:
        stmt = select(PriceItem).order_by(PriceItem.category, PriceItem.code)
        if categories:
            stmt = stmt.where(PriceItem.category.in_(categories))
        if query:
            like = f"%{query.strip()}%"
            stmt = stmt.where(or_(PriceItem.description.ilike(like), PriceItem.code.ilike(like)))
        return {"items": [service.item_dict(i) for i in session.scalars(stmt)]}

    def price_book_import(
        session: Session, rows: list[dict[str, Any]], source: str
    ) -> dict[str, Any]:
        """Idempotent by code: unchanged rows are left alone; changed rows keep a history row."""
        latest = {str(r["code"]): r for r in rows}  # last row for a code wins
        imported = updated = unchanged = 0
        for code, r in latest.items():
            fields = {
                "category": str(r["category"]),
                "description": str(r["description"]),
                "unit": str(r["unit"]),
                "unit_cost": service.dec(r["unit_cost"]),
                "unit_price": service.dec(r["unit_price"]),
            }
            cur = session.get(PriceItem, code)
            if cur is None:
                session.add(PriceItem(code=code, source=source, **fields))
                imported += 1
            elif all(getattr(cur, k) == v for k, v in fields.items()):
                unchanged += 1
            else:
                session.add(
                    PriceHistory(
                        code=code,
                        revision=cur.revision,
                        category=cur.category,
                        description=cur.description,
                        unit=cur.unit,
                        unit_cost=cur.unit_cost,
                        unit_price=cur.unit_price,
                        source=cur.source,
                        valid_from=cur.updated_at,
                    )
                )
                for k, v in fields.items():
                    setattr(cur, k, v)
                cur.source, cur.revision, cur.updated_at = source, cur.revision + 1, utcnow()
                updated += 1
        session.flush()
        return {"imported": imported, "updated": updated, "unchanged": unchanged}

    def estimate_write(
        session: Session,
        job_id: str,
        scope_id: str,
        lines: list[dict[str, Any]],
        contingency_pct: Any = 0,
        discount_pct: Any = 0,
    ) -> dict[str, Any]:
        jid = _uid(job_id, "job_id")
        scope = session.get(Scope, _uid(scope_id, "scope_id"))
        if scope is None or scope.job_id != jid:
            raise LookupError(f"scope {scope_id} not found for job {job_id}")
        pol = config.policy()
        priced, warnings = service.price_lines(session, lines)
        cont, disc = service.dec(contingency_pct), service.dec(discount_pct)
        t = service.totals(priced, pol, cont, disc)
        t.warnings = warnings + t.warnings
        if scope.status != "current":
            t.warnings.append(f"scope_superseded: scope version {scope.version} is not current")
        est = Estimate(
            job_id=jid,
            scope_id=scope.id,
            version=_next_version(session, Estimate, jid),
            currency=pol.currency,
            contingency_pct=cont,
            discount_pct=disc,
            total_cost=t.total_cost,
            total_price=t.total_price,
            margin_pct=t.margin_pct,
            margin_ok=t.margin_ok,
            warnings=t.warnings,
        )
        session.add(est)
        session.flush()
        for ln in priced:
            session.add(
                EstimateLine(
                    estimate_id=est.id,
                    position=ln.position,
                    code=ln.code,
                    description=ln.description,
                    quantity=ln.quantity,
                    unit=ln.unit,
                    unit_cost=ln.unit_cost,
                    unit_price=ln.unit_price,
                    line_cost=ln.line_cost,
                    line_price=ln.line_price,
                    unpriced=ln.unpriced,
                    cost_basis=ln.cost_basis,
                )
            )
        _supersede(session, Estimate, jid, est.id)
        session.flush()
        return {
            "estimate_id": str(est.id),
            "version": est.version,
            "unpriced_count": sum(1 for ln in priced if ln.unpriced),
            **_totals_out(t, pol.currency),
        }

    def _estimate(session: Session, estimate_id: str) -> Estimate:
        est = session.get(Estimate, _uid(estimate_id, "estimate_id"))
        if est is None:
            raise LookupError(f"estimate {estimate_id} not found")
        return est

    def _recheck(session: Session, est: Estimate) -> service.Totals:
        """Recompute an estimate from its stored lines against the policy as it is now.

        Stored totals are not trusted: they must match what the stored lines give, every
        priced line's cost must match its price-book basis, and the scope must be current."""
        pol = config.policy()
        rows = session.scalars(
            select(EstimateLine)
            .where(EstimateLine.estimate_id == est.id)
            .order_by(EstimateLine.position)
        ).all()
        problems: list[str] = []
        lines: list[service.PricedLine] = []
        for ln in rows:
            line_cost = service.money(ln.quantity * ln.unit_cost)
            line_price = service.money(ln.quantity * ln.unit_price)
            if (line_cost, line_price) != (ln.line_cost, ln.line_price):
                problems.append(f"line_mismatch: line {ln.position} totals do not recompute")
            if ln.quantity < 0 or ln.unit_cost < 0 or ln.unit_price < 0:
                problems.append(f"negative_value: line {ln.position}")
            book_cost = (ln.cost_basis or {}).get("book_unit_cost")
            if not ln.unpriced and (book_cost is None or service.dec(book_cost) != ln.unit_cost):
                problems.append(f"cost_basis_mismatch: line {ln.position} cost is not the book's")
            lines.append(
                service.PricedLine(
                    position=ln.position,
                    code=ln.code,
                    description=ln.description,
                    quantity=ln.quantity,
                    unit=ln.unit,
                    unit_cost=ln.unit_cost,
                    unit_price=ln.unit_price,
                    line_cost=line_cost,
                    line_price=line_price,
                    unpriced=ln.unpriced,
                    cost_basis=ln.cost_basis,
                )
            )
            if ln.unpriced:
                problems.append(f"unpriced_line: line {ln.position} ({ln.description})")
        try:
            t = service.totals(lines, pol, est.contingency_pct, est.discount_pct)
        except ValueError as e:
            t = service.Totals(est.total_cost, est.total_price, est.margin_pct, False, [])
            problems.append(f"negative_value: {e}")
        if (t.total_cost, t.total_price) != (est.total_cost, est.total_price):
            problems.append("totals_mismatch: stored totals differ from the stored lines")
        t.warnings += problems
        if any(not p.startswith("unpriced_line") for p in problems):
            t.margin_ok = False
        if est.status != "current":
            t.warnings.append("estimate_superseded: a newer version exists")
        scope = session.get(Scope, est.scope_id)
        if scope is None or scope.status != "current":
            t.warnings.append("scope_superseded: the estimate's scope is not current")
        return t

    def estimate_check(session: Session, estimate_id: str) -> dict[str, Any]:
        est = _estimate(session, estimate_id)
        t = _recheck(session, est)
        return {
            "estimate_id": str(est.id),
            "version": est.version,
            "margin_pct": float(t.margin_pct),
            "margin_ok": t.margin_ok,
            "warnings": t.warnings,
        }

    def proposal_build(
        session: Session, job_id: str, estimate_id: str, valid_days: int = DEFAULT_VALID_DAYS
    ) -> dict[str, Any]:
        jid = _uid(job_id, "job_id")
        est = _estimate(session, estimate_id)
        if est.job_id != jid:
            raise LookupError(f"estimate {estimate_id} does not belong to job {job_id}")
        if est.status != "current":
            raise ValueError(f"estimate {estimate_id} is not current (estimate_not_current)")
        scope = session.get(Scope, est.scope_id)
        if scope is None or scope.status != "current":
            raise ValueError(
                f"estimate {estimate_id} prices a superseded scope (scope_not_current)"
            )
        check = _recheck(session, est)
        if not check.margin_ok:
            raise ValueError(
                f"estimate {estimate_id} fails the margin policy (margin_not_ok): "
                + "; ".join(check.warnings)
            )
        lines = session.scalars(
            select(EstimateLine)
            .where(EstimateLine.estimate_id == est.id)
            .order_by(EstimateLine.position)
        ).all()
        valid_until = utcnow() + timedelta(days=int(valid_days))
        ver = _next_version(session, Proposal, jid)
        data: dict[str, Any] = {
            "job_id": str(jid),
            "version": ver,
            "estimate_id": str(est.id),
            "estimate_version": est.version,
            "currency": est.currency,
            "valid_days": int(valid_days),
            "valid_until": valid_until.date().isoformat(),
            "total_price": str(est.total_price),
            "total_price_minor": service.minor(est.total_price),
            "scope": {
                "areas": [a["name"] for a in scope.areas],
                "exclusions": scope.exclusions,
                "assumptions": scope.assumptions,
            },
            # Client-facing lines carry price only; costs and margin stay internal.
            "lines": [
                {
                    "description": ln.description,
                    "quantity": str(ln.quantity.normalize()),
                    "unit": ln.unit,
                    "unit_price": str(ln.unit_price),
                    "line_price": str(ln.line_price),
                }
                for ln in lines
            ],
            "contingency_pct": str(est.contingency_pct),
            "discount_pct": str(est.discount_pct),
        }
        row = Proposal(
            job_id=jid, estimate_id=est.id, version=ver, valid_until=valid_until, data=data
        )
        session.add(row)
        session.flush()
        _supersede(session, Proposal, jid, row.id)
        session.flush()
        data["proposal_id"] = str(row.id)
        row.data = dict(data)
        return {"proposal_id": str(row.id), "version": ver, "data": data}

    def variation_price(
        session: Session,
        job_id: str,
        description: str,
        lines: list[dict[str, Any]],
        discount_pct: Any = 0,
    ) -> dict[str, Any]:
        jid = _uid(job_id, "job_id")
        pol = config.policy()
        priced, warnings = service.price_lines(session, lines, allow_credit=True)
        disc = service.dec(discount_pct)
        t = service.totals(priced, pol, Decimal(0), disc)
        t.warnings = warnings + t.warnings
        row = Variation(
            job_id=jid,
            description=description,
            currency=pol.currency,
            lines=[
                {
                    "code": ln.code,
                    "description": ln.description,
                    "quantity": str(ln.quantity),
                    "unit": ln.unit,
                    "unit_cost": str(ln.unit_cost),
                    "unit_price": str(ln.unit_price),
                    "line_cost": str(ln.line_cost),
                    "line_price": str(ln.line_price),
                    "unpriced": ln.unpriced,
                    "cost_basis": ln.cost_basis,
                }
                for ln in priced
            ],
            discount_pct=disc,
            total_cost=t.total_cost,
            total_price=t.total_price,
            margin_pct=t.margin_pct,
            margin_ok=t.margin_ok,
            warnings=t.warnings,
        )
        session.add(row)
        session.flush()
        return {"variation_id": str(row.id), **_totals_out(t, pol.currency)}

    for t in [
        Tool(
            f"{CAP}.brief_write",
            CAP,
            SideEffect.DRAFT,
            _obj(
                {
                    "job_id": _STR,
                    "summary": _STR,
                    "requirements": _STRS,
                    "constraints": _STRS,
                    "budget_hint": _STR,
                },
                ["job_id", "summary"],
            ),
            brief_write,
            "Store a client brief",
        ),
        Tool(
            f"{CAP}.survey_record",
            CAP,
            SideEffect.DRAFT,
            _obj(
                {
                    "job_id": _STR,
                    "checklist_answers": {"type": "object"},
                    "measurements": {
                        "type": "array",
                        "items": _obj(
                            {"area": _STR, "quantity": _NUM, "unit": _STR},
                            ["area", "quantity", "unit"],
                        ),
                    },
                    "notes": _STR,
                },
                ["job_id"],
            ),
            survey_record,
            "Store survey answers and measurements",
        ),
        Tool(
            f"{CAP}.scope_write",
            CAP,
            SideEffect.DRAFT,
            _obj(
                {
                    "job_id": _STR,
                    "areas": {
                        "type": "array",
                        "items": _obj(
                            {
                                "name": _STR,
                                "items": {
                                    "type": "array",
                                    "items": _obj(
                                        {
                                            "category": _STR,
                                            "description": _STR,
                                            "quantity": _NUM,
                                            "unit": _STR,
                                        },
                                        ["description", "quantity"],
                                    ),
                                },
                            },
                            ["name"],
                        ),
                    },
                    "exclusions": _STRS,
                    "assumptions": _STRS,
                },
                ["job_id", "areas"],
            ),
            scope_write,
            "Write a new scope version",
        ),
        Tool(
            f"{CAP}.price_book_read",
            CAP,
            SideEffect.READ,
            _obj({"categories": _STRS, "query": _STR}),
            price_book_read,
            "Read price book items",
        ),
        Tool(
            f"{CAP}.price_book_import",
            CAP,
            SideEffect.DRAFT,
            _obj(
                {"rows": {"type": "array", "items": _ROW}, "source": _STR},
                ["rows", "source"],
            ),
            price_book_import,
            "Import price book rows, idempotent by code",
        ),
        Tool(
            f"{CAP}.estimate_write",
            CAP,
            SideEffect.DRAFT,
            _obj(
                {
                    "job_id": _STR,
                    "scope_id": _STR,
                    "lines": {"type": "array", "items": _LINE},
                    "contingency_pct": _NUM,
                    "discount_pct": _NUM,
                },
                ["job_id", "scope_id", "lines"],
            ),
            estimate_write,
            "Price a scope into a new estimate version under the margin policy",
        ),
        Tool(
            f"{CAP}.proposal_build",
            CAP,
            SideEffect.DRAFT,
            _obj(
                {"job_id": _STR, "estimate_id": _STR, "valid_days": {"type": "integer"}},
                ["job_id", "estimate_id"],
            ),
            proposal_build,
            "Build a proposal version from an estimate",
        ),
        Tool(
            f"{CAP}.variation_price",
            CAP,
            SideEffect.DRAFT,
            _obj(
                {
                    "job_id": _STR,
                    "description": _STR,
                    "lines": {"type": "array", "items": _LINE},
                    "discount_pct": _NUM,
                },
                ["job_id", "description", "lines"],
            ),
            variation_price,
            "Price a variation against the margin policy",
        ),
        Tool(
            f"{CAP}.estimate_check",
            CAP,
            SideEffect.READ,
            _obj({"estimate_id": _STR}, ["estimate_id"]),
            estimate_check,
            "Re-check an estimate against the current margin policy",
        ),
    ]:
        registry.register(t)
