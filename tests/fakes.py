"""A scripted chat model for deterministic agent tests (no network, no cost)."""

from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import BaseModel, PrivateAttr

# A step is a reply, a callable (e.g. one that raises), or a dict for a structured-output call.
Step = AIMessage | Callable[[list[BaseMessage]], Any] | dict[str, Any]


def call(name: str, call_id: str = "c1", **args: Any) -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}]
    )


def say(text: str) -> AIMessage:
    return AIMessage(
        content=text, usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    )


def scope(in_scope: bool) -> dict[str, Any]:
    """The scope check's structured reply."""
    return {"in_scope": in_scope}


def fail(error: Exception) -> Callable[[list[BaseMessage]], AIMessage]:
    """A script step that raises, like a provider error."""

    def step(messages: list[BaseMessage]) -> AIMessage:
        raise error

    return step


class ScriptedChatModel(BaseChatModel):
    """Returns scripted responses in order; records every prompt it was given.

    Structured-output calls take their step from the same script and are recorded separately
    in `structured_prompts`, so `prompts` holds only the agent's own calls.
    """

    script: list[Any]  # of Step; not validated (pydantic would coerce callables)
    _calls: list[list[BaseMessage]] = PrivateAttr(default_factory=list)
    _structured_calls: list[list[BaseMessage]] = PrivateAttr(default_factory=list)
    _bound_tools: list[Any] = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    @property
    def prompts(self) -> list[list[BaseMessage]]:
        return self._calls

    @property
    def structured_prompts(self) -> list[list[BaseMessage]]:
        return self._structured_calls

    @property
    def bound_tools(self) -> list[Any]:
        return self._bound_tools

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ScriptedChatModel":
        self._bound_tools = list(tools)
        return self

    def with_structured_output(  # type: ignore[override]
        self, schema: type[BaseModel], **kwargs: Any
    ) -> Runnable[list[BaseMessage], BaseModel]:
        def run(messages: list[BaseMessage]) -> BaseModel:
            self._structured_calls.append(list(messages))
            if not self.script:
                raise AssertionError("ScriptedChatModel ran out of responses")
            step = self.script.pop(0)
            value = step(messages) if callable(step) else step
            return schema.model_validate(value)

        return RunnableLambda(run)

    def _next(self, messages: list[BaseMessage]) -> ChatResult:
        self._calls.append(list(messages))
        if not self.script:
            raise AssertionError("ScriptedChatModel ran out of responses")
        step = self.script.pop(0)
        message = step(messages) if callable(step) else step
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._next(messages)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._next(messages)
