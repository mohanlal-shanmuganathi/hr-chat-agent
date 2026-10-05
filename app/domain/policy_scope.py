"""Which locations each policy document covers.

Some policies cover only part of the company: the leave policy, for example, is written for the
India entity, so it does not apply to employees in the USA. The scope is configuration (the
private file when present, else the fictional example), applied at ingestion time to each
document, so search results can say who a passage applies to. Policies not listed apply to every
location.
"""

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.db.models import Location
from app.domain.rules.location import DISPLAY

EXAMPLE_POLICY_SCOPE = Path("data/seed/policy_scope.example.yaml")


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PolicyScopeRule(_Strict):
    name: str  # how the policy is named to the employee, e.g. "Leave Policy"
    match: str  # words matched against the document's policy key, e.g. "leave policy"
    applies_to: list[Location] = Field(min_length=1)

    def matches(self, policy_key: str) -> bool:
        words = r"\s+".join(re.escape(w) for w in self.match.lower().split())
        return re.search(rf"\b{words}\b", policy_key.lower()) is not None


class PolicyScope(_Strict):
    policies: list[PolicyScopeRule] = []

    def locations_for(self, policy_key: str) -> list[Location] | None:
        """Locations a policy covers, or None when it covers every location."""
        for rule in self.policies:
            if rule.matches(policy_key):
                return list(rule.applies_to)
        return None

    def describe_for(self, location: Location | None) -> str:
        """One plain sentence for the system prompt: which policies do not cover `location`."""
        excluded = [r for r in self.policies if location not in r.applies_to]
        if location is None or not excluded:
            return "All company policies in the knowledge base apply to their location."
        parts = [f"the {r.name} covers {covered_label(r.applies_to)} only" for r in excluded]
        return (
            f"Policy scope for their location ({DISPLAY[location]}): {'; '.join(parts)}. "
            "These policies do not apply to them."
        )


def covered_label(locations: list[Location]) -> str:
    return ", ".join(DISPLAY[loc] for loc in locations)


def load_policy_scope(policy_dir: Path) -> PolicyScope:
    """`<policy_dir>/config/policy_scope.yaml` when it exists, else the fictional example."""
    private = policy_dir / "config" / "policy_scope.yaml"
    chosen = private if private.exists() else EXAMPLE_POLICY_SCOPE
    return PolicyScope.model_validate(yaml.safe_load(chosen.read_text(encoding="utf-8")))
