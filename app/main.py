from __future__ import annotations

from fastapi import FastAPI

from app.application.agent_graph import AgentGraphRunner
from app.application.chat_service import ChatCompletionService
from app.application.model_service import ModelService
from app.application.session_service import SessionService
from app.application.tool_service import ToolService, builtin_tool_definitions
from app.config import Settings
from app.infrastructure.llm.openai_compatible import OpenAICompatibleProvider
from app.infrastructure.memory.heuristic_summarizer import HeuristicSummarizer
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.infrastructure.tools.builtin import build_builtin_tool_executor
from app.interfaces.http.dashboard import create_dashboard_router
from app.interfaces.http.openai import create_openai_router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    memory_store = SQLiteMemoryStore(settings.sqlite_path)
    summarizer = HeuristicSummarizer()
    provider = OpenAICompatibleProvider(
        settings.provider_base_url,
        settings.provider_api_key,
        settings.provider_model,
    )
    tool_executor = build_builtin_tool_executor(settings.workspace_root)
    tool_service = ToolService(tool_executor, builtin_tool_definitions())
    graph_runner = AgentGraphRunner(
        provider,
        tool_service,
        memory_store,
        summarizer,
        settings.agent_iteration_limit,
    )
    chat_service = ChatCompletionService(memory_store, graph_runner)
    model_service = ModelService(provider, settings.provider_model)
    session_service = SessionService(memory_store)

    app = FastAPI(title="N-Agent")
    app.include_router(create_openai_router(chat_service, model_service))
    app.include_router(create_dashboard_router(session_service))
    return app


app = create_app()
