"""Check that the configured LLM is reachable and usable, outside the agent.

Usage:  python -m app.agent.llm_check
Prints the provider, model, and either the model's reply or the full error chain (never the key).
"""

import asyncio
import sys

from app.agent.graph import message_text
from app.agent.llm import LLMConfigurationError, create_chat_model
from app.config import get_settings


async def main() -> int:
    settings = get_settings()
    secret = (
        settings.google_api_key if settings.llm_provider == "google_genai" else settings.llm_api_key
    )
    key = secret.get_secret_value() if secret else ""
    print(f"provider={settings.llm_provider} model={settings.llm_model}")
    if settings.llm_base_url and settings.llm_provider != "google_genai":
        print(f"base url={settings.llm_base_url}")
    print(f"api key set={bool(key)} length={len(key)}")
    try:
        model = create_chat_model(settings)
        reply = await model.ainvoke("Reply with the single word OK.")
    except LLMConfigurationError as exc:
        print(f"CONFIG ERROR: {exc}")
        return 2
    except Exception as exc:
        print("CALL FAILED")
        err: BaseException | None = exc
        while err is not None:
            print(f"  {type(err).__module__}.{type(err).__name__}: {str(err)[:500]}")
            err = err.__cause__ or err.__context__
        return 1
    print(f"OK: model replied {message_text(reply)!r}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
