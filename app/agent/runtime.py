"""Composition root: wires settings -> database, tools, model, checkpointer -> ChatService.

Used by the API (lifespan) and the terminal chat, so both run exactly the same agent. If the LLM
is not configured the runtime still starts (health checks, login and data access work) and
`chat` is None with the reason in `chat_unavailable`.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from langchain_core.language_models import BaseChatModel
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agent.checkpoint import postgres_checkpointer
from app.agent.graph import AgentSettings, build_agent_graph
from app.agent.llm import LLMConfigurationError, create_chat_model
from app.agent.service import ChatService
from app.config import Settings
from app.core.logging import get_logger
from app.db.session import create_engine, create_session_factory
from app.domain.policy_scope import load_policy_scope
from app.domain.rules.config import load_rules
from app.hr.db_provider import DbHRDataProvider
from app.observability.audit import DbAuditSink
from app.rag.embeddings import create_embedder
from app.rag.retriever import PolicyRetriever
from app.tools.base import ToolDeps
from app.tools.hr_tools import HR_TOOLS

log = get_logger(__name__)


@dataclass(frozen=True)
class Runtime:
    settings: Settings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    deps: ToolDeps
    chat: ChatService | None
    chat_unavailable: str | None = None

    def today(self) -> date:
        return datetime.now(ZoneInfo(self.settings.timezone)).date()


def agent_settings(settings: Settings) -> AgentSettings:
    return AgentSettings(
        company="Ideas2IT",
        hr_email=settings.hr_contact_email,
        max_tool_rounds=settings.agent_max_tool_rounds,
        max_input_chars=settings.agent_max_input_chars,
        history_messages=settings.agent_history_messages,
        tool_timeout_s=settings.tool_timeout_s,
        scope_check=settings.scope_check_enabled,
        policy_scope=load_policy_scope(Path(settings.policy_dir)),
    )


def configure_tracing(settings: Settings) -> bool:
    """LangChain/LangGraph read tracing settings from the process environment."""
    if not (settings.langsmith_tracing and settings.langsmith_api_key):
        os.environ["LANGSMITH_TRACING"] = "false"
        return False
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key.get_secret_value()
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    if settings.langsmith_hide_io:
        os.environ["LANGSMITH_HIDE_INPUTS"] = "true"
        os.environ["LANGSMITH_HIDE_OUTPUTS"] = "true"
    log.info("runtime.tracing_enabled", project=settings.langsmith_project)
    return True


@asynccontextmanager
async def build_runtime(
    settings: Settings, model: BaseChatModel | None = None
) -> AsyncIterator[Runtime]:
    configure_tracing(settings)
    engine = create_engine(settings)
    sessions = create_session_factory(engine)
    rules_path = Path(settings.policy_dir) / "config" / "rules.yaml"
    deps = ToolDeps(
        hr=DbHRDataProvider(sessions),
        retriever=PolicyRetriever(sessions, create_embedder(settings)),
        rules=load_rules(rules_path if rules_path.exists() else None),
        audit=DbAuditSink(sessions),
        hr_contact_email=settings.hr_contact_email,
        search_top_k=settings.policy_search_top_k,
    )
    try:
        chat_model, reason = model, None
        if chat_model is None:
            try:
                chat_model = create_chat_model(settings)
            except LLMConfigurationError as exc:
                reason = str(exc)
                log.warning("runtime.chat_unavailable", reason=reason)
        async with postgres_checkpointer(settings) as saver:
            chat = None
            if chat_model is not None:
                graph = build_agent_graph(
                    chat_model, HR_TOOLS, deps, agent_settings(settings), saver
                )
                chat = ChatService(
                    graph,
                    settings.agent_turn_timeout_s,
                    settings.agent_max_tool_rounds,
                    (settings.llm_cost_input_per_mtok_usd, settings.llm_cost_output_per_mtok_usd),
                )
            yield Runtime(settings, engine, sessions, deps, chat, reason)
    finally:
        await engine.dispose()
