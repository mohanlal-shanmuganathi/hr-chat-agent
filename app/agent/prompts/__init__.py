"""Versioned prompts. The version is recorded on every trace so answers can be tied to a prompt."""

from datetime import date
from pathlib import Path

SYSTEM_PROMPT_VERSION = "system_v4"
_DIR = Path(__file__).parent


def render_system_prompt(
    *,
    company: str,
    employee_name: str,
    employee_location: str,
    today: date,
    hr_email: str,
    policy_scope: str,
) -> str:
    template = (_DIR / f"{SYSTEM_PROMPT_VERSION}.md").read_text(encoding="utf-8")
    return template.format(
        company=company,
        employee_name=employee_name,
        employee_location=employee_location,
        today=today.strftime("%d %b %Y"),
        weekday=today.strftime("%A"),
        hr_email=hr_email,
        policy_scope=policy_scope,
    )
