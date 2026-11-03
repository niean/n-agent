import asyncio

import pytest


@pytest.fixture(autouse=True)
def close_idle_event_loop_before_sync_test(request):
    """Do not let asyncio.run() orphan pytest-asyncio's idle clean loop."""
    if request.node.get_closest_marker("asyncio") is not None:
        yield
        return

    policy = asyncio.get_event_loop_policy()
    # The default policy's get_event_loop() creates a fresh idle loop when
    # none exists (and warns on Python 3.12). This cleanup fixture must only
    # close an existing idle loop, never create one merely to close it.
    if isinstance(policy, asyncio.DefaultEventLoopPolicy):
        loop = getattr(policy._local, '_loop', None)
    else:
        try:
            loop = policy.get_event_loop()
        except RuntimeError:
            loop = None
    if loop is not None and not loop.is_running():
        loop.close()
        policy.set_event_loop(None)

    yield
