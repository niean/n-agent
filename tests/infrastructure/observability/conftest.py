"""Wire the real semantic projection at the same boundary as app.main."""
import pytest
from tests.infrastructure.observability._contract import configure


@pytest.fixture(autouse=True)
def semantic_outlets():
    from app.application import telemetry_semantics
    from app.infrastructure.observability.setup import configure_semantics_registry
    configure_semantics_registry(telemetry_semantics)
    configure()
    yield
    from app.infrastructure.observability import logging as obs
    obs.configure_execution_context(None)
