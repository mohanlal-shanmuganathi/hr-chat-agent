"""Deterministic input guardrails, applied before the model sees a message.

- Sensitive topics (harassment, discrimination, self-harm, ...) get a fixed, reviewed reply that
  routes the employee to people, instead of model improvisation.
- Obvious prompt-injection attempts are refused.
- Oversized input is rejected.

These are cheap first-line checks, not the only defence: tools still enforce identity and
permissions, and write actions still need the employee's confirmation.
"""

import re
from dataclasses import dataclass
from enum import StrEnum


class GuardKind(StrEnum):
    ALLOW = "allow"
    SENSITIVE = "sensitive"
    CRISIS = "crisis"
    INJECTION = "injection"
    OUT_OF_SCOPE = "out_of_scope"  # decided by the graph's model-based scope check
    TOO_LONG = "too_long"
    EMPTY = "empty"


@dataclass(frozen=True)
class GuardDecision:
    kind: GuardKind
    reply: str | None = None  # fixed reply when the message must not reach the model


_CRISIS = re.compile(
    r"\b(suicid\w*|kill(ing)? myself|end (my|it all) life|self[- ]?harm|hurt(ing)? myself|"
    r"want to die)\b",
    re.IGNORECASE,
)
_SENSITIVE = re.compile(
    r"\b(harass\w*|sexual(ly)?|molest\w*|assault\w*|abus(e|ed|ive)|bully\w*|"
    r"discriminat\w*|retaliat\w*|grievance|whistle ?blow\w*|threaten\w*|violence|"
    r"unsafe at work|hostile work)\b",
    re.IGNORECASE,
)
_FIRST_PERSON = re.compile(r"\b(i|i'm|im|i've|me|my|mine|we|us|our)\b", re.IGNORECASE)
_TICKET_REQUEST = re.compile(
    r"\b(open|create|raise|file|log)\s+(a\s+|an\s+|the\s+)?(confidential\s+)?(hr\s+)?ticket\b",
    re.IGNORECASE,
)
_INJECTION = re.compile(
    r"(ignore|disregard|forget|override)\s+(all\s+|any\s+|the\s+|your\s+)?"
    r"(previous|prior|above|earlier|system)?\s*(instructions|prompts?|rules)"
    r"|(reveal|show|print|repeat)\s+(me\s+)?(your|the)\s+(system\s+)?(prompt|instructions)"
    r"|\byou are now\b|\bdeveloper mode\b|\bjailbreak\b|\bDAN mode\b"
    r"|\b(act|pretend)\s+(as|to be)\s+(an?\s+)?(admin|administrator|hr manager|system)",
    re.IGNORECASE,
)


def check_input(text: str, max_chars: int, hr_email: str) -> GuardDecision:
    stripped = text.strip()
    if not stripped:
        return GuardDecision(GuardKind.EMPTY, "Please type your question.")
    if len(stripped) > max_chars:
        return GuardDecision(
            GuardKind.TOO_LONG,
            f"That message is too long (over {max_chars} characters). Please shorten it.",
        )
    if _CRISIS.search(stripped):
        return GuardDecision(
            GuardKind.CRISIS,
            "I'm really sorry you're going through this. You don't have to handle it alone. "
            "If you are in immediate danger, please call the emergency number 112 (India) or 911 "
            "(USA) now, or reach someone you trust. You can also contact the HR team "
            f"confidentially at {hr_email}; they can connect you with support.",
        )
    if (
        _SENSITIVE.search(stripped)
        and _FIRST_PERSON.search(stripped)
        and not _TICKET_REQUEST.search(stripped)
    ):
        # A personal disclosure goes to people. Informational questions ("what does the policy
        # say about harassment?") reach the agent and are answered from the policies. An
        # explicit ticket request also goes through so the agent can open it (with confirmation).
        return GuardDecision(
            GuardKind.SENSITIVE,
            "I'm sorry you're dealing with this. Because it's a sensitive matter, it should be "
            "handled directly and confidentially by the HR team rather than by an automated "
            f"assistant. You can reach HR at {hr_email}. The Code of Conduct and Whistle Blower "
            "policies describe the confidential reporting channels and protection from "
            "retaliation.\n\nIf you'd like, I can open a confidential HR ticket for you. Just "
            'reply "open a ticket" with a short, factual summary of what you\'d like HR to '
            "know, and you'll be asked to confirm before anything is sent.",
        )
    if _INJECTION.search(stripped):
        return GuardDecision(
            GuardKind.INJECTION,
            "I can't change how I work or share my instructions. I'm happy to help with HR "
            "policies or your own leave, WFH, loan or certification questions.",
        )
    return GuardDecision(GuardKind.ALLOW)
