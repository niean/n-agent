import os
import tempfile
import warnings
import sys

import httpx
from langchain_core._api.deprecation import LangChainPendingDeprecationWarning
from starlette.exceptions import StarletteDeprecationWarning


# Ensure artifacts_root points to a host-writable directory for tests that
# call create_app() without explicit Settings. The default (/app/locals/
# artifacts) is a Docker-only path that cannot be created on the host.
# setdefault preserves any caller-provided value (e.g. artifact wiring tests
# that pass their own artifacts_root via Settings).
_test_artifacts_root = os.path.join(tempfile.gettempdir(), "n-agent-test-artifacts")
os.makedirs(_test_artifacts_root, exist_ok=True)
os.environ.setdefault("N_AGENT_ARTIFACTS_ROOT", _test_artifacts_root)


sys.modules.setdefault("httpx2", httpx)

# Pre-import acp.schema so lazy imports in app code (e.g., ACP auth helpers)
# find it cached in sys.modules. This works around pytest's prepend import
# mode shadowing the installed `acp` SDK with the local
# tests/interfaces/cli/commands/acp/ test package directory. We pop `acp`
# itself afterward so pytest can still import the local test package as
# `acp.test_auth` during collection.
import acp.schema  # noqa: F401
sys.modules.pop("acp", None)

warnings.filterwarnings(
    "ignore",
    message="The default value of `allowed_objects` will change in a future version.*",
    category=LangChainPendingDeprecationWarning,
)
warnings.filterwarnings(
    "ignore",
    message="Using `httpx` with `starlette.testclient` is deprecated.*",
    category=StarletteDeprecationWarning,
)

with warnings.catch_warnings():
    warnings.simplefilter("ignore", LangChainPendingDeprecationWarning)
    import langgraph.graph  # noqa: F401
