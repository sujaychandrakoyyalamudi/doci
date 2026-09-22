import logging
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text

from doci import observability
from doci.auth import Actor, current_actor, require_roles, verify_task
from doci.config import Settings
from doci.db import Database
from doci.documents import DocumentService
from doci.middleware import BodyLimitMiddleware
from doci.models import Case, Document, Run
from doci.queue import Queue
from doci.repository import Repository, serialize
from doci.schemas import ApprovalInput, CreateCase
from doci.workflow import Workflow

ActorDep = Annotated[Actor, Depends(current_actor)]
WriteDep = Annotated[Actor, Depends(require_roles("submitter", "analyst"))]
AnalystDep = Annotated[Actor, Depends(require_roles("analyst", "reviewer"))]
ApproverDep = Annotated[Actor, Depends(require_roles("approver"))]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    telemetry = observability.configure(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(settings)
        logging.getLogger("doci.startup").info("Initializing application database")
        db.initialize()
        logging.getLogger("doci.startup").info("Application database ready; initializing agents")
        documents = DocumentService(settings, db)
        workflow = Workflow(settings, db, documents, telemetry=telemetry)
        logging.getLogger("doci.startup").info("Initializing workflow checkpoints")
        workflow.initialize()
        logging.getLogger("doci.startup").info("Application startup complete")
        services = SimpleNamespace(
            settings=settings,
            db=db,
            repo=Repository(db),
            documents=documents,
            workflow=workflow,
            queue=Queue(settings, workflow),
            telemetry=telemetry,
        )
        app.state.services = services
        try:
            yield
        finally:
            try:
                telemetry.shutdown()
            finally:
                db.close()

    app = FastAPI(
        title="Doci · Document Review & Case Resolution", version="0.1.0", lifespan=lifespan
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "X-Demo-Role"],
    )
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_upload_bytes + 65536)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Google authentication checks the browser key's allowed origin. Send
        # only the origin to cross-origin auth requests, never document paths.
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def svc(request: Request):
        return request.app.state.services

    def dispatch(services, run_id, background):
        try:
            services.queue.dispatch(run_id, background)
        except Exception as exc:
            observability.event(
                "queue.dispatch_failed",
                level=logging.ERROR,
                review_id=run_id,
                exception_type=type(exc).__name__,
            )
            raise HTTPException(
                503, "Review saved but queue dispatch failed. Use Retry to dispatch the saved run."
            ) from exc

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/readyz")
    def ready(request: Request):
        with svc(request).db.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ready"}

    @app.get("/api/config")
    def public_config():
        return {
            "auth_mode": settings.auth_mode,
            "model_provider": settings.model_provider,
            "environment": settings.app_env,
            "version": "0.1.0",
            "firebase_api_key": settings.firebase_api_key,
            "firebase_auth_domain": settings.firebase_auth_domain,
            "project_id": settings.google_cloud_project,
            "synthetic_workspace": settings.synthetic_workspace,
        }

    @app.get("/api/me")
    def me(actor: ActorDep):
        return actor

    @app.get("/api/monitoring")
    def monitoring(actor: Annotated[Actor, Depends(require_roles("admin"))]):
        return telemetry.status()

    @app.get("/api/cases")
    def list_cases(request: Request, actor: ActorDep):
        return svc(request).repo.list_cases(actor.tenant_id)

    @app.post("/api/cases", status_code=201)
    def create_case(data: CreateCase, request: Request, actor: WriteDep):
        return svc(request).repo.create_case(actor, data)

    @app.get("/api/cases/{case_id}")
    def case_detail(case_id: str, request: Request, actor: ActorDep):
        detail = svc(request).repo.case_detail(case_id, actor.tenant_id)
        if detail.get("run"):
            run = detail["run"]
            traced = any(
                event["event"] == "observability.trace_started"
                and event["detail"] == f"Trace reference: {run['trace_id']}"
                for event in detail["events"]
            )
            run["trace_url"] = (
                telemetry.run_url(run["id"], run["trace_id"])
                if actor.role == "admin" and traced
                else None
            )
        return detail

    @app.post("/api/cases/{case_id}/reviews", status_code=202)
    def start_review(
        case_id: str, request: Request, background: BackgroundTasks, actor: AnalystDep
    ):
        services = svc(request)
        run = services.repo.create_run(actor, case_id, settings.model_provider)
        dispatch(services, run.id, background)
        return {"run_id": run.id, "status": "queued"}

    @app.post("/api/runs/{run_id}/decision", status_code=202)
    def decide(
        run_id: str,
        data: ApprovalInput,
        request: Request,
        background: BackgroundTasks,
        actor: ApproverDep,
    ):
        services = svc(request)
        decision = services.repo.decide(actor, run_id, data)
        observability.event("review.human_decision", review_id=run_id, outcome=data.decision)
        dispatch(services, run_id, background)
        return serialize(decision)

    @app.post("/api/runs/{run_id}/retry", status_code=202)
    def retry(run_id: str, request: Request, background: BackgroundTasks, actor: AnalystDep):
        services = svc(request)
        with services.db.session() as session:
            run = session.scalar(
                select(Run).where(Run.id == run_id, Run.tenant_id == actor.tenant_id)
            )
            if not run:
                raise HTTPException(404, "Review not found")
            case = session.get(Case, run.case_id)
            if (
                run.status not in {"failed", "queued", "processing"}
                or case.current_run_id != run_id
            ):
                raise HTTPException(409, "This run cannot be retried")
        dispatch(services, run_id, background)
        return {"run_id": run_id, "status": "queued"}

    @app.get("/api/documents")
    def list_documents(request: Request, actor: ActorDep):
        return svc(request).repo.documents(actor.tenant_id)

    @app.post("/api/documents", status_code=201)
    def upload(
        request: Request,
        actor: Annotated[Actor, Depends(require_roles("submitter", "analyst", "reviewer"))],
        file: Annotated[UploadFile, File()],
        case_id: Annotated[str | None, Form()] = None,
    ):
        # Reading one bounded buffer prevents an unbounded in-memory copy.
        data = file.file.read(settings.max_upload_bytes + 1)
        document = svc(request).documents.upload(actor, file.filename or "document", data, case_id)
        return serialize(document, ("storage_key", "tenant_id"))

    @app.get("/api/documents/{document_id}/content")
    def download(document_id: str, request: Request, actor: ActorDep):
        services = svc(request)
        with services.db.session() as session:
            doc = session.scalar(
                select(Document).where(
                    Document.id == document_id, Document.tenant_id == actor.tenant_id
                )
            )
            if not doc:
                raise HTTPException(404, "Document not found")
        from urllib.parse import quote

        return Response(
            services.documents.read(doc),
            media_type=doc.content_type,
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(doc.filename)}"},
        )

    @app.get("/api/cases/{case_id}/search")
    def search(
        case_id: str,
        request: Request,
        actor: ActorDep,
        q: str = Query(min_length=2, max_length=1000),
    ):
        services = svc(request)
        with services.db.session() as session:
            services.repo.case(session, case_id, actor.tenant_id)
        return services.documents.search(actor.tenant_id, case_id, q)

    @app.get("/api/activity")
    def activity(request: Request, actor: ActorDep):
        return svc(request).repo.events(actor.tenant_id)

    @app.post("/api/internal/runs/{run_id}", dependencies=[Depends(verify_task)])
    def worker(run_id: str, request: Request):
        try:
            svc(request).workflow.execute(run_id, raise_errors=True)
        except Exception as exc:
            raise HTTPException(503, "Workflow incomplete; retry delivery") from exc
        return {"status": "processed"}

    static = Path(__file__).resolve().parent / "static"
    if static.exists():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def frontend(path: str):
            if path.startswith(("api/", "healthz", "readyz")):
                raise HTTPException(404, "Not found")
            return FileResponse(static / "index.html", headers={"Cache-Control": "no-cache"})

    telemetry.instrument(app)
    return app


app = create_app()
