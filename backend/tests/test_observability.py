import json
import logging
from contextlib import contextmanager
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from doci import observability
from doci.auth import Actor, current_actor
from doci.config import Settings
from doci.main import create_app
from fastapi.testclient import TestClient
from pydantic import ValidationError

from .conftest import ready_case


def test_trace_export_removes_content_errors_and_credentials():
    hidden = "private-content-must-not-be-exported"
    run = {
        "id": str(uuid4()),
        "name": "analysis",
        "inputs": {"document": hidden},
        "outputs": {"answer": hidden},
        "serialized": {"credential": hidden},
        "attachments": {"file": hidden},
        "events": [{"name": "new_token", "kwargs": {"token": hidden}}],
        "error": f"RuntimeError({hidden})",
        "extra": {
            "invocation_params": {"token": hidden},
            "metadata": {
                "review_id": str(uuid4()),
                "email": hidden,
                "ls_model_name": "gemini-model",
                "usage_metadata": {
                    "input_tokens": 12,
                    "output_tokens": 7,
                    "total_tokens": 19,
                    "output_token_details": {"reasoning": 2, "private": hidden},
                },
            },
        },
        "tags": ["service:doci", hidden],
    }
    cleaned = observability.scrub_run_operations([run])[0]
    assert hidden not in json.dumps(cleaned)
    assert cleaned["inputs"] == cleaned["outputs"] == {}
    assert cleaned["extra"]["metadata"]["usage_metadata"]["total_tokens"] == 19
    assert cleaned["extra"]["metadata"]["usage_metadata"]["output_token_details"] == {
        "reasoning": 2
    }
    assert run["inputs"]["document"] == hidden
    assert observability.anonymize_trace({"error": f"GraphInterrupt({hidden})"}) == {"error": None}


def test_sampling_is_stable_and_handles_boundaries():
    review_ids = [str(uuid4()) for _ in range(100)]
    assert not any(observability.sampled_review(value, 0) for value in review_ids)
    assert all(observability.sampled_review(value, 1) for value in review_ids)
    first = [observability.sampled_review(value, 0.5) for value in review_ids]
    assert first == [observability.sampled_review(value, 0.5) for value in review_ids]
    assert any(first) and not all(first)


def test_monitoring_requires_admin_and_never_returns_credentials(client, app):
    assert client.get("/api/monitoring").status_code == 403
    app.dependency_overrides[current_actor] = lambda: Actor("operator", "demo", "admin", "Operator")
    response = client.get("/api/monitoring")
    assert response.status_code == 200
    assert response.json()["content_hidden"] is True
    assert "api_key" not in response.text
    assert "password" not in response.text


def test_request_logs_use_routes_without_query_or_identifier_content(client, caplog):
    hidden = "private-query-value"
    with caplog.at_level(logging.INFO, logger="doci"):
        response = client.get(
            f"/api/cases/{hidden}/search?q={hidden}", headers={"Authorization": hidden}
        )
    assert response.status_code == 404
    UUID(response.headers["x-request-id"])
    records = [record for record in caplog.records if record.name.startswith("doci")]
    formatted = [
        observability.JsonFormatter(Settings(_env_file=None)).format(record) for record in records
    ]
    assert records and all(hidden not in value for value in formatted)
    completed = [
        record for record in records if getattr(record, "event", None) == "http.request_completed"
    ]
    assert completed[-1].route == "/api/cases/{case_id}/search"


def test_feedback_outage_does_not_change_the_review_outcome(client, app, monkeypatch):
    telemetry = app.state.services.telemetry

    @contextmanager
    def context(*args):
        yield {"callbacks": [], "metadata": {}, "tags": []}, True

    monkeypatch.setattr(telemetry, "review_trace", context)
    monkeypatch.setattr(telemetry, "feedback", Mock(side_effect=RuntimeError("private-error-body")))
    detail = ready_case(client)
    assert detail["status"] == "awaiting_approval"
    assert detail["decision"] is None
    assert detail["action"] is None
    assert any(event["event"] == "observability.trace_started" for event in detail["events"])


def test_configuration_keeps_secret_values_private_and_requires_https():
    settings = Settings(_env_file=None, langsmith_api_key=" private-key-value \n")
    assert settings.langsmith_api_key.get_secret_value() == "private-key-value"
    assert "private-key-value" not in repr(settings)
    with pytest.raises(ValidationError, match="API key"):
        Settings(_env_file=None, langsmith_tracing=True)
    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(_env_file=None, langsmith_endpoint="http://example.invalid")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, langsmith_sampling_rate=1.5)


def test_human_wait_is_not_reported_as_failed_execution(client, caplog):
    with caplog.at_level(logging.INFO, logger="doci"):
        detail = ready_case(client)
    completed = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "workflow.execution_finished"
    ]
    assert completed[-1].status == "awaiting_approval"
    assert completed[-1].review_id == detail["run"]["id"]
    assert completed[-1].duration_ms >= 0


def test_cloud_trace_is_flushed_before_the_response_finishes(settings, monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    original = observability.Telemetry.instrument

    def instrument(self, app):
        self.trace_provider = provider
        self.tracer = provider.get_tracer("unit")
        original(self, app)

    monkeypatch.setattr(observability.Telemetry, "instrument", instrument)
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/config?ignored=private-query-value")
        assert response.status_code == 200
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "GET /api/config"
        assert "private-query-value" not in str(spans[0].attributes)
        assert spans[0].attributes["http.response.status_code"] == 200


def test_feedback_flushes_trace_before_posting_and_bounds_retries(settings):
    project_id = str(uuid4())
    settings.langsmith_project_url = (
        f"https://smith.langchain.com/o/{uuid4()}/projects/p/{project_id}"
    )
    telemetry = observability.Telemetry(settings)
    telemetry.client = Mock()
    telemetry.client.read_run.return_value.session_id = project_id
    try:
        telemetry.feedback(str(uuid4()), {"review": {"verdict": "pass"}})
        calls = telemetry.client.mock_calls
        assert calls[0][0] == "flush"
        assert calls[1][0] == "read_run"
        assert calls[2][0] == "create_feedback"
        assert calls[2].kwargs["stop_after_attempt"] == 1
        assert calls[2].kwargs["extend_trace_retention"] is False
        assert calls[2].kwargs["session_id"] == project_id
        assert "project_id" not in calls[2].kwargs
    finally:
        telemetry.shutdown()


def test_slow_feedback_does_not_hold_the_request_indefinitely(settings, caplog):
    import threading
    import time

    settings.telemetry_flush_timeout = 0.1
    telemetry = observability.Telemetry(settings)
    telemetry.client = Mock()
    release = threading.Event()
    telemetry.client.create_feedback.side_effect = lambda **kwargs: release.wait(1)
    try:
        started = time.perf_counter()
        with caplog.at_level(logging.WARNING, logger="doci"):
            telemetry.feedback(str(uuid4()), {"review": {"verdict": "pass"}})
        assert time.perf_counter() - started < 0.75
        assert any(
            getattr(record, "reason", None) == "feedback_timeout" for record in caplog.records
        )
    finally:
        release.set()
        telemetry.shutdown()


def test_feedback_waits_for_trace_visibility_and_uses_its_timestamp(settings):
    from types import SimpleNamespace

    from langsmith.utils import LangSmithNotFoundError

    telemetry = observability.Telemetry(settings)
    telemetry.client = Mock()
    timestamp = "2026-01-01T00:00:00+00:00"
    project_id = str(uuid4())
    telemetry.client.read_run.side_effect = [
        LangSmithNotFoundError("Trace indexing is pending"),
        SimpleNamespace(session_id=project_id, start_time=timestamp),
    ]
    try:
        telemetry.feedback(str(uuid4()), {"review": {"verdict": "pass"}})
        assert telemetry.client.read_run.call_count == 2
        values = telemetry.client.create_feedback.call_args.kwargs
        assert values["session_id"] == project_id
        assert values["start_time"] == timestamp
        assert values["stop_after_attempt"] == 1
    finally:
        telemetry.shutdown()
