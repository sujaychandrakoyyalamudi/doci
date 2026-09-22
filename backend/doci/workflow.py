import logging
import time
from contextlib import contextmanager
from typing import TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy import select

from doci import observability
from doci.agents import Agents, citation_errors
from doci.config import Settings
from doci.db import Database
from doci.documents import DocumentService
from doci.models import Action, AuditEvent, Case, Decision, Run, now, uid
from doci.repository import proposal_hash
from doci.schemas import Analysis, Review

logger = logging.getLogger(__name__)
TERMINAL = {"resolved", "rejected", "escalated", "needs_information"}


class State(TypedDict, total=False):
    run_id: str
    case_id: str
    tenant_id: str
    title: str
    description: str
    evidence: list[dict]
    analysis: dict
    review: dict
    revision: int
    decision: str
    outcome: str


class Workflow:
    def __init__(
        self, settings: Settings, db: Database, documents: DocumentService, *, telemetry=None
    ):
        self.settings, self.db, self.documents = settings, db, documents
        self.agents = Agents(settings)
        self.telemetry = telemetry or observability.configure(settings)

    @contextmanager
    def checkpointer(self):
        if self.settings.checkpoint_url.startswith("postgresql://"):
            from langgraph.checkpoint.postgres import PostgresSaver

            with PostgresSaver.from_conn_string(self.settings.checkpoint_url) as saver:
                yield saver
        else:
            from langgraph.checkpoint.sqlite import SqliteSaver

            with SqliteSaver.from_conn_string(self.settings.checkpoint_url) as saver:
                yield saver

    def initialize(self):
        with self.db.workflow_lock("checkpoint-schema", wait=True) as acquired:
            if acquired:
                with self.checkpointer() as saver:
                    saver.setup()

    def record(self, state: State, step: str, **updates):
        with self.db.session.begin() as session:
            run = session.get(Run, state["run_id"])
            run.step, run.updated_at = step, now()
            for key, value in updates.items():
                setattr(run, key, value)
            if updates.get("status") in TERMINAL | {"processing", "awaiting_approval"}:
                case = session.get(Case, run.case_id)
                if case.current_run_id == run.id:
                    case.status, case.updated_at = updates["status"], now()
            dedupe_key = f"{run.id}:{step}:{state.get('revision', 0)}"
            if not session.scalar(select(AuditEvent.id).where(AuditEvent.dedupe_key == dedupe_key)):
                session.add(
                    AuditEvent(
                        tenant_id=run.tenant_id,
                        case_id=run.case_id,
                        run_id=run.id,
                        actor=f"agent:{step}",
                        event=f"workflow.{step}",
                        detail=step.replace("_", " ").capitalize(),
                        dedupe_key=dedupe_key,
                    )
                )
        observability.event("workflow.step", review_id=state["run_id"], step=step)

    def gather_evidence(self, state: State):
        self.record(state, "evidence", status="processing")
        evidence = self.documents.search(
            state["tenant_id"], state["case_id"], f"{state['title']} {state['description']}"
        )
        self.record(state, "evidence", evidence=evidence)
        return {"evidence": evidence}

    def analyze(self, state: State):
        self.record(state, "analysis")
        analysis = self.agents.analyze(state)
        self.record(
            state,
            "analysis",
            analysis=analysis.model_dump(),
            confidence=analysis.confidence,
            revision=state.get("revision", 0),
        )
        return {"analysis": analysis.model_dump()}

    def independent_review(self, state: State):
        self.record(state, "independent_review")
        analysis = Analysis.model_validate(state["analysis"])
        errors = citation_errors(analysis, state["evidence"])
        kinds = {item["kind"] for item in state["evidence"]}
        if not {"evidence", "policy"}.issubset(kinds):
            review = Review(
                verdict="escalate",
                reasoning="Case evidence and a published policy are both required.",
                concerns=["Missing source documents."],
            )
        elif errors:
            review = Review(
                verdict="revise", reasoning="Citation validation failed.", concerns=errors
            )
        elif analysis.action == "escalate":
            review = Review(
                verdict="escalate",
                reasoning=analysis.rationale,
                concerns=analysis.missing_information,
            )
        else:
            review = self.agents.review(state["evidence"], state["analysis"])
        self.record(state, "independent_review", review=review.model_dump())
        return {"review": review.model_dump()}

    def after_review(self, state: State) -> str:
        verdict = state["review"]["verdict"]
        if verdict == "pass":
            return "publish_proposal"
        if verdict == "revise" and state.get("revision", 0) < self.settings.max_revisions:
            return "revise"
        return "escalate"

    def publish_proposal(self, state: State):
        self.record(state, "human_approval", status="awaiting_approval")
        return {}

    def human_approval(self, state: State):
        # No side effects before interrupt: LangGraph replays this node on resume.
        resumed = interrupt(
            {
                "run_id": state["run_id"],
                "analysis": state["analysis"],
                "proposal_hash": proposal_hash(state["analysis"]),
            }
        )
        with self.db.session() as session:
            decision = session.scalar(select(Decision).where(Decision.run_id == state["run_id"]))
            if not decision or resumed.get("decision_id") != decision.id:
                raise ValueError("A persisted human decision is required")
            if decision.proposal_hash != proposal_hash(state["analysis"]):
                raise ValueError("Approval does not match the checkpointed proposal")
            return {"decision": decision.decision}

    def apply_action(self, state: State):
        # Action and case state change share a transaction. Replayed nodes reuse the record.
        with self.db.session.begin() as session:
            run = session.get(Run, state["run_id"])
            case = session.get(Case, state["case_id"])
            decision = session.scalar(select(Decision).where(Decision.run_id == run.id))
            if (
                not decision
                or decision.decision != "approve"
                or decision.proposal_hash != proposal_hash(state["analysis"])
                or decision.proposal_hash != proposal_hash(run.analysis)
                or run.review.get("verdict") != "pass"
                or case.current_run_id != run.id
                or decision.actor_id in {case.created_by, run.requested_by}
            ):
                raise ValueError("Action authorization failed")
            kind = state["analysis"]["action"]
            outcomes = {
                "record_resolution": "resolved",
                "request_information": "needs_information",
                "escalate": "escalated",
            }
            outcome = outcomes[kind]
            if not session.scalar(select(Action.id).where(Action.run_id == run.id)):
                session.add(
                    Action(
                        run_id=run.id,
                        case_id=case.id,
                        kind=kind,
                        payload=state["analysis"],
                        approved_by=decision.actor_id,
                    )
                )
                session.add(
                    AuditEvent(
                        tenant_id=run.tenant_id,
                        case_id=case.id,
                        run_id=run.id,
                        actor="action-service",
                        event="action.recorded",
                        detail="Approved internal disposition recorded; no external system action performed",
                        dedupe_key=f"{run.id}:action",
                    )
                )
            case.status, case.updated_at = outcome, now()
        return {"outcome": outcome}

    def build(self, saver):
        graph = StateGraph(State)
        graph.add_node("evidence", self.gather_evidence)
        graph.add_node("analysis", self.analyze)
        graph.add_node("independent_review", self.independent_review)
        graph.add_node("revise", lambda state: {"revision": state.get("revision", 0) + 1})
        graph.add_node("publish_proposal", self.publish_proposal)
        graph.add_node("human_approval", self.human_approval)
        graph.add_node("apply_action", self.apply_action)
        graph.add_node("reject", lambda _: {"outcome": "rejected"})
        graph.add_node("escalate", lambda _: {"outcome": "escalated"})
        graph.add_edge(START, "evidence")
        graph.add_edge("evidence", "analysis")
        graph.add_edge("analysis", "independent_review")
        graph.add_conditional_edges(
            "independent_review",
            self.after_review,
            {key: key for key in ("publish_proposal", "revise", "escalate")},
        )
        graph.add_edge("revise", "analysis")
        graph.add_edge("publish_proposal", "human_approval")
        graph.add_conditional_edges(
            "human_approval",
            lambda s: "apply_action" if s["decision"] == "approve" else "reject",
            {"apply_action": "apply_action", "reject": "reject"},
        )
        for node in ("apply_action", "reject", "escalate"):
            graph.add_edge(node, END)
        return graph.compile(checkpointer=saver)

    def execute(self, run_id: str, raise_errors: bool = False):
        with self.db.workflow_lock(run_id) as acquired:
            if not acquired:
                observability.event("workflow.skipped", review_id=run_id, reason="locked")
                if raise_errors:
                    raise RuntimeError("This run is being processed; retry later")
                return
            try:
                with self.db.session.begin() as session:
                    run = session.get(Run, run_id)
                    if not run or run.status in TERMINAL:
                        return
                    case = session.get(Case, run.case_id)
                    if case.current_run_id != run_id:
                        return
                    initial: State = {
                        "run_id": run.id,
                        "case_id": case.id,
                        "tenant_id": run.tenant_id,
                        "title": case.title,
                        "description": case.description,
                        "revision": 0,
                    }
                    decision = session.scalar(select(Decision).where(Decision.run_id == run_id))
                    run.error = None
                config = {
                    "configurable": {"thread_id": run_id},
                    "recursion_limit": 30,
                    "run_name": "document_review",
                    "metadata": {"review_id": run_id, "mode": self.settings.model_provider},
                }
                with self.checkpointer() as saver:
                    graph = self.build(saver)

                    def invoke(value):
                        # Each execution/resumption has its own LangSmith trace; review_id
                        # correlates them without reusing an existing root trace UUID.
                        trace_id = uid()
                        with self.db.session.begin() as session:
                            session.get(Run, run_id).trace_id = trace_id
                        started = time.perf_counter()
                        duration_ms = None
                        status = "failed"
                        with self.telemetry.span(
                            "workflow.execute",
                            {"doci.review_id": run_id, "doci.trace_id": trace_id},
                        ) as workflow_span:
                            with self.telemetry.review_trace(run_id, trace_id) as (
                                trace_config,
                                sampled,
                            ):
                                if sampled:
                                    with self.db.session.begin() as session:
                                        session.add(
                                            AuditEvent(
                                                tenant_id=initial["tenant_id"],
                                                case_id=initial["case_id"],
                                                run_id=run_id,
                                                actor="observability",
                                                event="observability.trace_started",
                                                detail=f"Trace reference: {trace_id}",
                                                dedupe_key=f"trace:{trace_id}",
                                            )
                                        )
                                try:
                                    result = graph.invoke(
                                        value, {**config, **trace_config, "run_id": UUID(trace_id)}
                                    )
                                    status = result.get("outcome") or (
                                        "awaiting_approval"
                                        if result.get("__interrupt__")
                                        else "processing"
                                    )
                                    duration_ms = round((time.perf_counter() - started) * 1000, 3)
                                    if sampled:
                                        try:
                                            self.telemetry.feedback(trace_id, result)
                                        except Exception as exc:
                                            self.telemetry.delivery_error(exc)
                                    return result
                                finally:
                                    if workflow_span:
                                        from opentelemetry.trace import Status, StatusCode

                                        workflow_span.set_attribute("doci.status", status)
                                        if status == "failed":
                                            workflow_span.set_status(Status(StatusCode.ERROR))
                                    observability.event(
                                        "workflow.execution_finished",
                                        review_id=run_id,
                                        trace_id=trace_id,
                                        status=status,
                                        sampled=sampled,
                                        duration_ms=duration_ms
                                        if duration_ms is not None
                                        else round((time.perf_counter() - started) * 1000, 3),
                                    )
                                    if sampled:
                                        self.telemetry.flush()

                    snapshot = graph.get_state(config)
                    if not snapshot.values:
                        result = invoke(initial)
                    elif any(task.interrupts for task in snapshot.tasks):
                        if not decision:
                            self.publish_proposal(snapshot.values)
                            return
                        result = invoke(Command(resume={"decision_id": decision.id}))
                    elif snapshot.next:
                        result = invoke(None)
                    else:
                        result = snapshot.values
                    # A crash may occur after publishing the proposal but before saving
                    # its interrupt. A decision already saved during that window must
                    # resume the newly recovered interrupt in this delivery.
                    if result.get("__interrupt__") and decision:
                        result = invoke(Command(resume={"decision_id": decision.id}))
                if result.get("outcome"):
                    outcome = result["outcome"]
                    self.record(initial, "complete", status=outcome)
            except Exception as exc:
                # Do not log exception bodies: provider errors may contain document text.
                observability.event(
                    "workflow.failed",
                    level=logging.ERROR,
                    review_id=run_id,
                    exception_type=type(exc).__name__,
                )
                with self.db.session.begin() as session:
                    run = session.get(Run, run_id)
                    if run:
                        run.status = "failed"
                        run.error = f"Review failed at {run.step}. Retry this run to resume its saved checkpoint."
                        session.add(
                            AuditEvent(
                                tenant_id=run.tenant_id,
                                case_id=run.case_id,
                                run_id=run.id,
                                actor="workflow-service",
                                event="workflow.failed",
                                detail=run.error,
                            )
                        )
                        case = session.get(Case, run.case_id)
                        if case.current_run_id == run_id:
                            case.status = "failed"
                if raise_errors:
                    raise
