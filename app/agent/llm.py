"""Chat-model factory: the only place that knows which LLM provider is used.

Switching provider is a configuration change (LLM_PROVIDER, LLM_MODEL) plus installing that
provider's LangChain package. Behaviour still differs between models, so re-run the evals after
any switch.
"""

from typing import Any

from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from app.config import Settings


class LLMConfigurationError(RuntimeError):
    pass


def create_chat_model(settings: Settings) -> BaseChatModel:
    if settings.llm_provider == "google_genai":
        if settings.google_api_key is None or not settings.google_api_key.get_secret_value():
            raise LLMConfigurationError(
                "GOOGLE_API_KEY is not set. Add it to your local .env (never commit it)."
            )
        from langchain_google_genai import ChatGoogleGenerativeAI

        extra: dict[str, Any] = {}
        if settings.llm_thinking_level and settings.llm_model.startswith("gemini-3"):
            extra["thinking_level"] = settings.llm_thinking_level
        return ChatGoogleGenerativeAI(
            model=settings.llm_model,
            google_api_key=settings.google_api_key,
            temperature=settings.llm_temperature,
            max_output_tokens=settings.llm_max_output_tokens,
            timeout=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
            **extra,
        )
    if settings.llm_provider == "openai_compatible":
        if not settings.llm_base_url:
            raise LLMConfigurationError(
                "LLM_BASE_URL is not set (e.g. https://api.groq.com/openai/v1)."
            )
        from langchain_openai import ChatOpenAI

        key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else ""
        return ChatOpenAI(
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            api_key=SecretStr(key or "not-needed"),  # a local Ollama needs no key
            temperature=settings.llm_temperature,
            max_completion_tokens=settings.llm_max_output_tokens,
            timeout=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
        )
    raise LLMConfigurationError(f"Unsupported LLM provider: {settings.llm_provider}")


# Errors a retry or a new question will not fix: the operator must change the configuration.
CONFIG_ERROR_CODES = frozenset({"llm_auth", "llm_model"})
# Errors an immediate retry cannot fix (the provider says when to come back).
NO_RETRY_CODES = CONFIG_ERROR_CODES | {"llm_rate_limited", "llm_quota_exhausted"}


def classify_llm_error(exc: BaseException) -> tuple[str, str] | None:
    """Map common LLM provider failures to actionable messages (no secrets in them)."""
    text = ""
    err: BaseException | None = exc
    while err is not None and len(text) < 4000:
        text += f" {type(err).__name__} {err}"
        err = err.__cause__ or err.__context__
    lowered = text.lower()
    if (
        "api_key_invalid" in lowered
        or "api key not valid" in lowered
        or "invalid api key" in lowered
        or "permission_denied" in lowered
        or "authenticationerror" in lowered
    ):
        return "llm_auth", "The AI service rejected the API key. Check the API key in .env."
    if ("not_found" in lowered or "notfounderror" in lowered) and "model" in lowered:
        return "llm_model", "The configured AI model was not found. Check LLM_MODEL in .env."
    if "perday" in lowered or "per day" in lowered:
        return (
            "llm_quota_exhausted",
            "The AI service's daily free quota is used up. It resets daily; meanwhile an "
            "administrator can switch LLM_MODEL or LLM_PROVIDER.",
        )
    if (
        "resource_exhausted" in lowered
        or "429" in lowered
        or "quota" in lowered
        or "ratelimiterror" in lowered
    ):
        return "llm_rate_limited", "The AI service rate limit was reached. Please wait a minute."
    if any(
        k in lowered
        for k in ("connecterror", "connection refused", "name resolution", "certificate_verify")
    ):
        return "llm_unreachable", "Cannot reach the AI service from this network."
    if any(
        k in lowered
        for k in ("unavailable", "overloaded", "503", "500 internal", "deadline", "timeout")
    ):
        return "llm_unavailable", "The AI service is busy or timed out. Please try again."
    return None
