"""Composition-root equivalent wiring for outlet integration tests."""
from app.application.telemetry_contract import (
    CONTENT_ATTRIBUTE_KEYS, EXECUTION_ATTRIBUTE_KEYS, completion_event,
    project_execution_attributes,
)
from app.infrastructure.observability import logging as obs


def configure(snapshot=None, **kwargs):
    obs.configure_execution_context(snapshot, project_context=project_execution_attributes,
        completion_event=completion_event, execution_keys=EXECUTION_ATTRIBUTE_KEYS,
        content_keys=CONTENT_ATTRIBUTE_KEYS, **kwargs)
