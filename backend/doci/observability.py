"""Content-free workflow tracing, request telemetry, and bounded export lifecycle."""

import hashlib
import json
import logging
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from threading import BoundedSemaphore
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from doci.config import Settings

logger = logging.getLogger("doci.telemetry")
SAFE_METADATA = {
    "review_id",
    "thread_id",
    "environment",
    "service",
    "release",
    "mode",
    "gcp_trace_id",
    "langgraph_node",
    "langgraph_step",
    "ls_provider",
    "ls_model_name",
    "ls_model_type",
    "ls_temperature",
    "ls_max_tokens",
    "ls_integration",
}
SAFE_NAMES = {
    "document_review",
    "LangGraph",
    "evidence",
    "analysis",
    "independent_review",
    "revise",
    "publish_proposal",
    "human_approval",
    "apply_action",
    "reject",
    "escalate",
    "ChatPromptTemplate",
    "ChatGoogleGenerativeAI",
    "RunnableSequence",
    "RunnableLambda",
    "PydanticToolsParser",
    "JsonOutputKeyToolsParser",
    "RunnablePassthrough",
    "demo_case_analyst",
    "demo_independent_reviewer",
    "observability_check",
}
LOG_FIELDS = {
    "event",
    "review_id",
    "trace_id",
    "request_id",
    "route",
    "method",
    "status",
    "duration_ms",
    "step",
    "exception_type",
    "provider",
    "revision",
    "evidence_count",
    "verdict",
    "sampled",
    "reason",
    "outcome",
}


def numeric_usage(value):
    if isinstance(value, dict):
        allowed = {"audio", "text", "image", "video", "reasoning", "cache_read", "cache_creation"}
        return {
            key: numeric_usage(item)
            for key, item in value.items()
            if key in allowed and isinstance(item, (dict, int, float))
        }
    if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
        return value
    return 0


def safe_metadata(value: dict) -> dict:
    result = {}
    for key, item in value.items():
        if key == "usage_metadata" and isinstance(item, dict):
            result[key] = {
                name: numeric_usage(item[name])
                for name in (
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "input_token_details",
                    "output_token_details",
                )
                if name in item
            }
        elif key in SAFE_METADATA:
            if isinstance(item, (int, float, bool)) and math.isfinite(item):
                result[key] = item
            elif isinstance(item, str) and re.fullmatch(r"[a-zA-Z0-9._:/-]{1,200}", item):
                result[key] = item
    return result


def anonymize_trace(value: dict) -> dict:
    if set(value) == {"error"}:
        error = value["error"]
        if error is None or str(error).startswith(("GraphInterrupt(", "NodeInterrupt(")):
            return {"error": None}
        return {"error": "Execution failed; exception content withheld by telemetry policy."}
    return safe_metadata(value)


def scrub_run_operations(operations):
    """Final export boundary: remove payloads, serialized objects, and event content."""
    cleaned = []
    for operation in operations:
        run = dict(operation)
        for key in ("inputs", "outputs"):
            if key in run:
                run[key] = {}
        for key in (
            "serialized",
            "attachments",
            "events",
            "runtime",
            "manifest_id",
            "manifest_s3_id",
        ):
            run.pop(key, None)
        if "error" in run:
            run["error"] = anonymize_trace({"error": run["error"]})["error"]
        if "name" in run and run["name"] not in SAFE_NAMES:
            run["name"] = "component"
        if "extra" in run:
            run["extra"] = {
                "metadata": safe_metadata((run.get("extra") or {}).get("metadata") or {})
            }
        if "tags" in run:
            run["tags"] = [
                tag
                for tag in run["tags"] or []
                if re.fullmatch(
                    r"(?:env|service|release|seq|graph|langsmith):[a-zA-Z0-9_.:/-]{1,100}", tag
                )
            ]
        cleaned.append(run)
    return cleaned


def sampled_review(review_id: str, rate: float) -> bool:
    value = int.from_bytes(hashlib.sha256(review_id.encode()).digest()[:8], "big") / 2**64
    return value < rate


def trace_context(project: str) -> dict:
    try:
        from opentelemetry import trace

        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            return {
                "logging.googleapis.com/trace": f"projects/{project}/traces/{context.trace_id:032x}",
                "logging.googleapis.com/spanId": f"{context.span_id:016x}",
                "logging.googleapis.com/trace_sampled": bool(context.trace_flags.sampled),
            }
    except ImportError:
        pass
    return {}


class JsonFormatter(logging.Formatter):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings

    def format(self, record):
        data = {
            "timestamp": datetime.now(UTC).isoformat(),
            "severity": record.levelname,
            "message": record.getMessage()
            if record.name.startswith("doci")
            else "Service diagnostic; details withheld.",
            "logger": record.name,
            "service": "doci",
            "environment": self.settings.app_env,
            "release": os.getenv("K_REVISION", "local"),
        }
        data.update({key: getattr(record, key) for key in LOG_FIELDS if hasattr(record, key)})
        data.update(trace_context(self.settings.google_cloud_project))
        return json.dumps(data, ensure_ascii=False)


def event(name: str, *, level=logging.INFO, **fields):
    logger.log(
        level,
        name,
        extra={"event": name, **{key: value for key, value in fields.items() if key in LOG_FIELDS}},
    )


class Telemetry:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = None
        self.trace_provider = None
        self.tracer = None
        self._feedback_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="doci-telemetry"
        )
        self._feedback_slots = BoundedSemaphore(8)
        self.release = os.getenv("K_REVISION", "local")
        if settings.langsmith_tracing:
            from langsmith import Client
            from urllib3.util.retry import Retry

            self.client = Client(
                api_url=settings.langsmith_endpoint,
                api_key=settings.langsmith_api_key.get_secret_value(),
                workspace_id=settings.langsmith_workspace_id or None,
                hide_inputs=True,
                hide_outputs=True,
                anonymizer=anonymize_trace,
                omit_traced_runtime_info=True,
                process_buffered_run_ops=scrub_run_operations,
                run_ops_buffer_size=50,
                run_ops_buffer_timeout_ms=500,
                tracing_sampling_rate=1.0,
                timeout_ms=(2000, 5000),
                retry_config=Retry(total=1, backoff_factor=0.1),
                tracing_error_callback=self.delivery_error,
            )

    def delivery_error(self, error):
        event(
            "telemetry.delivery_failed",
            level=logging.WARNING,
            provider="langsmith",
            exception_type=type(error).__name__,
        )

    def instrument(self, app):
        if self.settings.app_env == "production":
            from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

            self.trace_provider = TracerProvider(
                resource=Resource.create(
                    {
                        "service.name": "doci-api",
                        "service.version": self.release,
                        "deployment.environment.name": self.settings.app_env,
                    }
                ),
                sampler=TraceIdRatioBased(self.settings.cloud_trace_sampling_rate),
            )
            self.trace_provider.add_span_processor(
                BatchSpanProcessor(
                    CloudTraceSpanExporter(project_id=self.settings.google_cloud_project)
                )
            )
            self.tracer = self.trace_provider.get_tracer("doci")
        app.add_middleware(RequestTelemetryMiddleware, telemetry=self, routes=app.router.routes)

    @contextmanager
    def span(self, name: str, attributes=None, context=None, end_on_exit=True):
        if not self.tracer:
            yield None
            return
        with self.tracer.start_as_current_span(
            name,
            context=context,
            attributes=attributes,
            record_exception=False,
            set_status_on_exception=False,
            end_on_exit=end_on_exit,
        ) as span:
            yield span

    @contextmanager
    def review_trace(self, review_id: str, trace_id: str):
        from langsmith.run_helpers import tracing_context

        sampled = self.client is not None and sampled_review(
            review_id, self.settings.langsmith_sampling_rate
        )
        metadata = {
            "review_id": review_id,
            "thread_id": review_id,
            "environment": self.settings.app_env,
            "service": "doci",
            "release": self.release,
            "mode": self.settings.model_provider,
        }
        gcp = trace_context(self.settings.google_cloud_project).get("logging.googleapis.com/trace")
        if gcp:
            metadata["gcp_trace_id"] = gcp.rsplit("/", 1)[-1]
        from langchain_core.tracers.context import tracing_v2_enabled

        tags = [f"env:{self.settings.app_env}", "service:doci", f"release:{self.release}"]
        callback_context = (
            tracing_v2_enabled(
                project_name=self.settings.langsmith_project, client=self.client, tags=tags
            )
            if sampled
            else nullcontext(None)
        )
        with (
            tracing_context(
                enabled=sampled,
                client=self.client,
                project_name=self.settings.langsmith_project,
                metadata=metadata,
                tags=tags,
            ),
            callback_context as callback,
        ):
            yield (
                {"callbacks": [callback] if callback else [], "metadata": metadata, "tags": tags},
                sampled,
            )

    def feedback(self, trace_id: str, state: dict):
        if not self.client:
            return
        from doci.agents import citation_errors
        from doci.schemas import Analysis

        scores = {}
        if state.get("analysis"):
            try:
                analysis = Analysis.model_validate(state["analysis"])
                scores["citation_integrity"] = int(
                    not citation_errors(analysis, state.get("evidence") or [])
                )
                scores["review_confidence"] = analysis.confidence
            except (ValueError, KeyError, TypeError) as exc:
                self.delivery_error(exc)
        if state.get("review"):
            scores["reviewer_pass"] = int(state["review"].get("verdict") == "pass")
        if state.get("decision") in {"approve", "reject"}:
            scores["human_approved"] = int(state["decision"] == "approve")
        values = {key: {"score": score} for key, score in scores.items()}
        outcome = state.get("outcome") or (
            "awaiting_approval" if state.get("__interrupt__") else None
        )
        if outcome in {
            "awaiting_approval",
            "resolved",
            "rejected",
            "needs_information",
            "escalated",
        }:
            values["workflow_outcome"] = {"value": outcome}
        if not values:
            return
        if not self._feedback_slots.acquire(blocking=False):
            event(
                "telemetry.delivery_failed",
                level=logging.WARNING,
                provider="langsmith",
                reason="feedback_queue_full",
                trace_id=trace_id,
            )
            return

        def send_feedback():
            try:
                # Non-retention-extending feedback uses a synchronous SDK endpoint.
                # Submit its trace first, and never enter the SDK's long 404 retry loop.
                self.client.flush(timeout=self.settings.telemetry_flush_timeout)
                from langsmith.utils import LangSmithNotFoundError

                deadline = time.monotonic() + self.settings.telemetry_flush_timeout
                while True:
                    try:
                        traced_run = self.client.read_run(trace_id)
                        break
                    except LangSmithNotFoundError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.2)
                for key, payload in values.items():
                    self.client.create_feedback(
                        run_id=trace_id,
                        trace_id=trace_id,
                        session_id=traced_run.session_id,
                        start_time=traced_run.start_time,
                        key=key,
                        **payload,
                        feedback_id=uuid5(NAMESPACE_URL, f"{trace_id}:{key}"),
                        extend_trace_retention=False,
                        stop_after_attempt=1,
                    )
            except Exception as exc:
                self.delivery_error(exc)

        try:
            future = self._feedback_executor.submit(send_feedback)
        except RuntimeError as exc:
            self._feedback_slots.release()
            self.delivery_error(exc)
            return
        future.add_done_callback(lambda _: self._feedback_slots.release())
        try:
            future.result(timeout=self.settings.telemetry_flush_timeout)
        except FutureTimeoutError:
            event(
                "telemetry.delivery_failed",
                level=logging.WARNING,
                provider="langsmith",
                reason="feedback_timeout",
                trace_id=trace_id,
            )

    def flush(self):
        if self.client:
            try:
                self.client.flush(timeout=self.settings.telemetry_flush_timeout)
            except Exception as exc:
                self.delivery_error(exc)

    def shutdown(self):
        self._feedback_executor.shutdown(wait=False, cancel_futures=True)
        self.flush()
        if self.client:
            try:
                self.client.close(timeout=self.settings.telemetry_flush_timeout)
            except Exception as exc:
                self.delivery_error(exc)
        if self.trace_provider:
            self.trace_provider.force_flush(
                timeout_millis=int(self.settings.telemetry_flush_timeout * 1000)
            )
            self.trace_provider.shutdown()

    def status(self):
        return {
            "langsmith_enabled": self.client is not None,
            "langsmith_project": self.settings.langsmith_project,
            "langsmith_project_url": self.settings.langsmith_project_url,
            "sampling_rate": self.settings.langsmith_sampling_rate,
            "content_hidden": True,
            "dashboard_url": self.settings.monitoring_dashboard_url,
            "environment": self.settings.app_env,
            "release": self.release,
        }

    def run_url(self, review_id: str, trace_id: str):
        if (
            not self.client
            or not self.settings.langsmith_project_url
            or not sampled_review(review_id, self.settings.langsmith_sampling_rate)
        ):
            return None
        try:
            trace_id = str(UUID(trace_id))
        except (TypeError, ValueError):
            return None
        return f"{self.settings.langsmith_project_url}/r/{trace_id}?poll=true"


def configure(settings: Settings) -> Telemetry:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(settings))
    logging.getLogger("doci").handlers = [handler]
    logging.getLogger("doci").setLevel(logging.INFO)
    for name in (
        "langsmith",
        "langchain_core",
        "langgraph",
        "uvicorn.error",
        "google",
        "pypdf",
        "sqlalchemy",
        "opentelemetry",
    ):
        target = logging.getLogger(name)
        target.handlers = [handler]
        target.propagate = False
    # Explicit per-review tracing prevents unrelated background work using a global client.
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGSMITH_HIDE_INPUTS"] = "true"
    os.environ["LANGSMITH_HIDE_OUTPUTS"] = "true"
    return Telemetry(settings)


def incoming_trace_context(scope):
    from opentelemetry import trace
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, TraceState

    headers = {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }
    value = headers.get("x-cloud-trace-context", "")
    match = re.fullmatch(r"([0-9a-fA-F]{32})/(\d{1,20})(?:;o=([01]))?", value)
    if match:
        trace_id, span_id = int(match[1], 16), int(match[2])
        if trace_id and 0 < span_id < 2**64:
            context = SpanContext(
                trace_id, span_id, True, TraceFlags(1 if match[3] == "1" else 0), TraceState()
            )
            return trace.set_span_in_context(NonRecordingSpan(context))
    return None


class RequestTelemetryMiddleware:
    def __init__(self, app, *, telemetry: Telemetry, routes):
        self.app, self.telemetry, self.routes = app, telemetry, routes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in {"/healthz", "/readyz"}:
            await self.app(scope, receive, send)
            return
        from starlette.routing import Match

        route = "unmatched"
        for candidate in self.routes:
            matched, _ = candidate.matches(scope)
            if matched == Match.FULL:
                route = getattr(candidate, "path", "static")
                break
        method = scope.get("method", "OTHER")
        if method not in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}:
            method = "OTHER"
        request_id = str(uuid4())
        started, status = time.perf_counter(), 500
        context = incoming_trace_context(scope) if self.telemetry.tracer else None
        manager = (
            self.telemetry.span(
                f"{method} {route}",
                {"http.request.method": method, "http.route": route},
                context=context,
                end_on_exit=False,
            )
            if self.telemetry.tracer
            else nullcontext(None)
        )
        with manager as span:
            span_finished = False

            async def finish_span():
                nonlocal span_finished
                if span is None or span_finished:
                    return
                from opentelemetry.trace import Status, StatusCode

                span.set_attribute("http.response.status_code", status)
                if status >= 500:
                    span.set_status(Status(StatusCode.ERROR))
                recording = span.is_recording()
                span.end()
                span_finished = True
                if recording and self.telemetry.trace_provider:
                    import anyio

                    try:
                        flushed = await anyio.to_thread.run_sync(
                            lambda: self.telemetry.trace_provider.force_flush(
                                timeout_millis=int(
                                    self.telemetry.settings.telemetry_flush_timeout * 1000
                                )
                            )
                        )
                        if not flushed:
                            event(
                                "telemetry.delivery_failed",
                                level=logging.WARNING,
                                provider="cloud_trace",
                                reason="flush_timeout",
                            )
                    except Exception as exc:
                        event(
                            "telemetry.delivery_failed",
                            level=logging.WARNING,
                            provider="cloud_trace",
                            exception_type=type(exc).__name__,
                        )

            async def send_response(message):
                nonlocal status
                if message["type"] == "http.response.start":
                    status = message["status"]
                    message = {
                        **message,
                        "headers": [
                            *message.get("headers", []),
                            (b"x-request-id", request_id.encode()),
                        ],
                    }
                if message["type"] == "http.response.body" and not message.get("more_body", False):
                    # Flush before Cloud Run can throttle CPU after the final response.
                    await finish_span()
                await send(message)

            try:
                await self.app(scope, receive, send_response)
            except Exception as exc:
                event(
                    "http.request_failed",
                    level=logging.ERROR,
                    request_id=request_id,
                    route=route,
                    method=method,
                    exception_type=type(exc).__name__,
                )
                raise
            finally:
                await finish_span()
                event(
                    "http.request_completed",
                    request_id=request_id,
                    route=route,
                    method=method,
                    status=status,
                    duration_ms=round((time.perf_counter() - started) * 1000, 3),
                )
