import json
import logging
import os

from doci.config import Settings


class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(
            {"severity": record.levelname, "message": record.getMessage(), "logger": record.name},
            ensure_ascii=False,
        )


def configure(settings: Settings):
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.getLogger("doci").handlers = [handler]
    logging.getLogger("doci").setLevel(logging.INFO)
    for key in ("tracing", "project", "hide_inputs", "hide_outputs"):
        value = getattr(settings, f"langsmith_{key}")
        os.environ[f"LANGSMITH_{key.upper()}"] = (
            str(value).lower() if isinstance(value, bool) else value
        )
    if settings.langsmith_api_key:
        os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key


def instrument(app, settings: Settings):
    if settings.app_env != "production":
        return
    from opentelemetry import trace
    from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": "doci-api"}))
    provider.add_span_processor(
        BatchSpanProcessor(CloudTraceSpanExporter(project_id=settings.google_cloud_project))
    )
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz")
