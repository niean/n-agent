import asyncio

import pytest


@pytest.fixture(autouse=True)
def close_idle_event_loop_before_sync_test(request):
    """Do not let asyncio.run() orphan pytest-asyncio's idle clean loop."""
    if request.node.get_closest_marker("asyncio") is not None:
        yield
        return

    policy = asyncio.get_event_loop_policy()
    try:
        loop = policy.get_event_loop()
    except RuntimeError:
        loop = None
    if loop is not None and not loop.is_running():
        loop.close()
        policy.set_event_loop(None)

    yield
