"""Application configuration, loaded from environment variables (and `.env` locally)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "hr-chat-agent"
    environment: Literal["local", "test", "staging", "production"] = "local"
    log_level: str = "INFO"
    log_json: bool = Field(default=True, description="JSON logs; set false for pretty console logs")

    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://hr_agent:hr_agent@localhost:5432/hr_agent"
    )
    db_pool_size: int = 5
    db_connect_timeout_s: float = 5.0

    # Authentication
    session_secret: SecretStr | None = None  # required to sign sessions; see .env.example
    session_ttl_minutes: int = 480
    cookie_secure: bool = False  # set true when served over HTTPS
    auth_dev_login_enabled: bool = False  # pick a fictional employee without SSO (demo only)
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    google_allowed_domain: str = "ideas2it.com"
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"
    mock_hr_autoprovision: bool = False  # mock HR only: create a demo profile on first SSO login
    mock_hr_template_email: str = "priya.r@example.com"
    rate_limit_per_minute: int = 20

    # Organisation
    timezone: str = "Asia/Kolkata"
    hr_contact_email: str = "hr@ideas2it.com"

    # LLM (provider-independent; switch with LLM_PROVIDER / LLM_MODEL)
    # google_genai: Gemini (GOOGLE_API_KEY). openai_compatible: any OpenAI-style chat API with
    # tool calling (Groq, OpenRouter, a local Ollama, ...) at LLM_BASE_URL with LLM_API_KEY.
    llm_provider: Literal["google_genai", "openai_compatible"] = "google_genai"
    llm_model: str = "gemini-3.5-flash-lite"
    google_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = None
    llm_temperature: float = 0.1
    # Gemini 3 "thinking" tokens count against the output budget, so keep it generous; a small
    # budget truncates answers after long tool results (policy excerpts).
    llm_max_output_tokens: int = 8192
    llm_thinking_level: Literal["minimal", "low", "medium", "high"] | None = "low"
    llm_timeout_s: float = 45.0
    llm_max_retries: int = 2
    # Pricing used only for the per-turn cost estimate (0 on a free tier).
    llm_cost_input_per_mtok_usd: float = 0.0
    llm_cost_output_per_mtok_usd: float = 0.0

    # Tracing (opt-in). LangSmith is a third-party service: traces contain prompts, tool results
    # and policy excerpts. Keep off unless that is acceptable; LANGSMITH_HIDE_IO masks them.
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "hr-chat-agent"
    langsmith_hide_io: bool = False

    # Agent limits
    agent_max_tool_rounds: int = 6
    agent_turn_timeout_s: float = 90.0
    agent_history_messages: int = 24
    agent_max_input_chars: int = 2000
    # One extra model call per message: off-topic questions get a fixed reply, never an answer.
    scope_check_enabled: bool = True

    # Tools
    tool_timeout_s: float = 8.0
    policy_search_top_k: int = 5

    # Policy knowledge base
    policy_dir: str = "data/private_policies"
    embedding_provider: Literal["fastembed", "hashing"] = "fastembed"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_cache_dir: str = ".cache/fastembed"
    chunk_target_words: int = 320
    chunk_overlap_words: int = 40


@lru_cache
def get_settings() -> Settings:
    return Settings()
