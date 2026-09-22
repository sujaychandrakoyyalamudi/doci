import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import func, select, update

from doci.auth import Actor
from doci.db import Database
from doci.documents import EDITABLE
from doci.models import Action, AuditEvent, Case, Decision, Document, Run, now, uid
from doci.schemas import ApprovalInput, CreateCase


def proposal_hash(analysis: dict) -> str:
    return hashlib.sha256(
        json.dumps(analysis, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def serialize(row, exclude=()) -> dict:
    result = {}
    for col in row.__table__.columns:
        if col.name not in exclude:
            value = getattr(row, col.name)
            result[col.name] = value.isoformat() if hasattr(value, "isoformat") else value
    return result


class Repository:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def case(session, case_id: str, tenant: str) -> Case:
        case = session.scalar(select(Case).where(Case.id == case_id, Case.tenant_id == tenant))
        if case is None:
            raise HTTPException(404, "Case not found")
        return case

    def create_case(self, actor: Actor, data: CreateCase) -> dict:
        with self.db.session.begin() as session:
            case = Case(
                id=uid(),
                tenant_id=actor.tenant_id,
                reference=f"DOC-{uid()[:8].upper()}",
                **data.model_dump(),
                created_by=actor.id,
            )
            session.add(case)
            session.flush()
            session.add(
                AuditEvent(
                    tenant_id=actor.tenant_id,
                    case_id=case.id,
                    actor=actor.id,
                    event="case.created",
                    detail=case.title,
                )
            )
        return serialize(case)

    def list_cases(self, tenant: str) -> list[dict]:
        with self.db.session() as session:
            counts = dict(
                session.execute(
                    select(Document.case_id, func.count())
                    .where(Document.tenant_id == tenant)
                    .group_by(Document.case_id)
                ).all()
            )
            return [
                {**serialize(case), "document_count": counts.get(case.id, 0)}
                for case in session.scalars(
                    select(Case)
                    .where(Case.tenant_id == tenant)
                    .order_by(Case.created_at.desc())
                    .limit(500)
                )
            ]

    def case_detail(self, case_id: str, tenant: str) -> dict:
        with self.db.session() as session:
            case = self.case(session, case_id, tenant)
            run = session.get(Run, case.current_run_id) if case.current_run_id else None
            decision = (
                session.scalar(select(Decision).where(Decision.run_id == run.id)) if run else None
            )
            action = session.scalar(select(Action).where(Action.run_id == run.id)) if run else None
            run_data = serialize(run) if run else None
            if run_data:
                run_data["proposal_hash"] = proposal_hash(run.analysis)
            detail = {
                **serialize(case),
                "run": run_data,
                "decision": serialize(decision) if decision else None,
                "action": serialize(action) if action else None,
            }
        # Release the first connection before fetching related collections; small
        # Cloud SQL pools must not deadlock on nested sessions under concurrent reads.
        return {
            **detail,
            "documents": self.documents(tenant, case_id),
            "events": self.events(tenant, case_id),
        }

    def documents(self, tenant: str, case_id: str | None = None) -> list[dict]:
        with self.db.session() as session:
            query = select(Document).where(Document.tenant_id == tenant)
            if case_id:
                query = query.where(Document.case_id == case_id)
            return [
                serialize(doc, ("storage_key", "tenant_id"))
                for doc in session.scalars(query.order_by(Document.created_at.desc()).limit(1000))
            ]

    def events(self, tenant: str, case_id: str | None = None) -> list[dict]:
        with self.db.session() as session:
            query = select(AuditEvent).where(AuditEvent.tenant_id == tenant)
            if case_id:
                query = query.where(AuditEvent.case_id == case_id)
            return [
                serialize(e)
                for e in session.scalars(query.order_by(AuditEvent.created_at.desc()).limit(100))
            ]

    def create_run(self, actor: Actor, case_id: str, mode: str) -> Run:
        with self.db.session.begin() as session:
            case = self.case(session, case_id, actor.tenant_id)
            if case.status not in EDITABLE:
                raise HTTPException(409, "This case already has an active review or is resolved")
            if case.current_run_id:
                old_decision = session.scalar(
                    select(Decision).where(Decision.run_id == case.current_run_id)
                )
                if old_decision and old_decision.decision == "approve":
                    old_run = session.get(Run, case.current_run_id)
                    if old_run.status == "failed":
                        raise HTTPException(
                            409, "Retry the approved run before starting another review"
                        )
            run = Run(
                id=uid(),
                case_id=case_id,
                tenant_id=actor.tenant_id,
                requested_by=actor.id,
                mode=mode,
            )
            result = session.execute(
                update(Case)
                .where(Case.id == case_id, Case.version == case.version, Case.status.in_(EDITABLE))
                .values(
                    status="queued",
                    current_run_id=run.id,
                    version=case.version + 1,
                    updated_at=now(),
                )
            )
            if result.rowcount != 1:
                raise HTTPException(409, "The case changed; refresh and try again")
            session.add(run)
            session.flush()
            session.add(
                AuditEvent(
                    tenant_id=actor.tenant_id,
                    case_id=case_id,
                    run_id=run.id,
                    actor=actor.id,
                    event="review.queued",
                    detail="Document review requested",
                )
            )
        return run

    def decide(self, actor: Actor, run_id: str, data: ApprovalInput) -> Decision:
        with self.db.session.begin() as session:
            run = session.scalar(
                select(Run)
                .where(Run.id == run_id, Run.tenant_id == actor.tenant_id)
                .with_for_update()
            )
            if not run:
                raise HTTPException(404, "Review not found")
            case = self.case(session, run.case_id, actor.tenant_id)
            if actor.id in {case.created_by, run.requested_by}:
                raise HTTPException(
                    403,
                    "Approval requires a different person from the submitter and requesting analyst",
                )
            existing = session.scalar(select(Decision).where(Decision.run_id == run_id))
            if existing:
                if (
                    existing.decision,
                    existing.proposal_hash,
                    existing.actor_id,
                    existing.comment,
                ) == (data.decision, data.proposal_hash, actor.id, data.comment):
                    return existing
                raise HTTPException(409, "A decision is already recorded for this review")
            if run.status != "awaiting_approval" or case.current_run_id != run_id:
                raise HTTPException(409, "This review is not awaiting approval")
            if run.review.get("verdict") != "pass" or data.proposal_hash != proposal_hash(
                run.analysis
            ):
                raise HTTPException(
                    409, "The reviewed proposal has changed; refresh before deciding"
                )
            result = session.execute(
                update(Run)
                .where(Run.id == run_id, Run.status == "awaiting_approval")
                .values(status="queued", updated_at=now())
            )
            if result.rowcount != 1:
                raise HTTPException(409, "Another approver already decided this review")
            decision = Decision(run_id=run_id, actor_id=actor.id, **data.model_dump())
            session.add(decision)
            case.status = "queued"
            case.updated_at = now()
            session.add(
                AuditEvent(
                    tenant_id=actor.tenant_id,
                    case_id=case.id,
                    run_id=run_id,
                    actor=actor.id,
                    event=f"approval.{data.decision}",
                    detail=data.comment,
                )
            )
        return decision
