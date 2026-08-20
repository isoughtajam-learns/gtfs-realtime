"""
Wires Python's standard `logging` module to PostHog via OpenTelemetry, so any
module doing `logging.getLogger(__name__)` ships records to PostHog once this
has been called - regardless of which entrypoint (uvicorn, fetcher.py's CLI,
a celery worker/beat process) is running.

Call configure_posthog_logging() once, early, in each entrypoint; safe to
call more than once (idempotent).
"""

import logging

from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

from src.settings import get_settings

_configured = False

# DEBUG-level output (e.g. fetcher.py's --diagnose report body) is routine,
# high-volume, and meant for local/console viewing - PostHog is for errors
# and notable events, not a mirror of everything printed locally.
POSTHOG_LOG_LEVEL = logging.INFO


def configure_posthog_logging() -> None:
    global _configured
    if _configured:
        return
    _configured = True

    settings = get_settings()
    if not settings.posthog_enabled:
        return

    if not settings.posthog_project_token or not settings.posthog_host:
        if settings.debug:
            raise RuntimeError(
                "POSTHOG_PROJECT_TOKEN/POSTHOG_HOST required for log shipping are "
                "missing or un-configured - logs will be silently dropped. This "
                "error stops appearing once both are configured."
            )
        return

    logger_provider = LoggerProvider()
    set_logger_provider(logger_provider)
    otlp_exporter = OTLPLogExporter(
        endpoint=f"{settings.posthog_host}/i/v1/logs",
        headers={"Authorization": f"Bearer {settings.posthog_project_token}"},
    )
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(otlp_exporter))

    handler = LoggingHandler(logger_provider=logger_provider)
    handler.setLevel(POSTHOG_LOG_LEVEL)
    logging.getLogger().addHandler(handler)
