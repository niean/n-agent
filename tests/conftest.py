import warnings
import sys

import httpx
from langchain_core._api.deprecation import LangChainPendingDeprecationWarning
from starlette.exceptions import StarletteDeprecationWarning


sys.modules.setdefault("httpx2", httpx)

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
