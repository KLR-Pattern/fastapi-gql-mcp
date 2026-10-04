"""Optional OpenTelemetry wiring — env-gated, zero code on the hot path.

Set ``OTEL_OTLP_ENDPOINT`` (e.g. ``http://localhost:4317``) to export spans;
without it — or without the ``otel`` dependency group installed — this is a
no-op. The spans themselves come from fastmcp (tool level) and
FastAPI >= 0.142 (route level) natively; nothing instruments the business
code (see examples/otel_smoke.md for the standalone proof).
"""

from __future__ import annotations

import atexit
import logging
import os

logger = logging.getLogger(__name__)

SERVICE_NAME = "notes-oauth"


def install_otel() -> str | None:
    """Install the OTLP exporter when configured; return the service name,
    or None when tracing is off (unset env or missing otel packages)."""
    endpoint = os.environ.get("OTEL_OTLP_ENDPOINT")
    if not endpoint:
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "OTEL_OTLP_ENDPOINT is set but the otel packages are missing "
            "— run: uv sync --group otel"
        )
        return None

    provider = TracerProvider(
        resource=Resource.create({"service.name": SERVICE_NAME})
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
    )
    trace.set_tracer_provider(provider)
    atexit.register(provider.shutdown)  # flush pending spans on exit
    return SERVICE_NAME
