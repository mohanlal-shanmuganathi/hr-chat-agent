"""Policy scope: which policies cover which locations (fictional example config only)."""

from pathlib import Path

from app.db.models import Location
from app.domain.policy_scope import (
    EXAMPLE_POLICY_SCOPE,
    PolicyScope,
    PolicyScopeRule,
    load_policy_scope,
)

SCOPE = PolicyScope(
    policies=[
        PolicyScopeRule(
            name="Leave Policy",
            match="leave policy",
            applies_to=[Location.CHENNAI, Location.KARNATAKA],
        )
    ]
)


def test_matches_whole_words_of_the_policy_key() -> None:
    assert SCOPE.locations_for("revised leave policy i2i") == [
        Location.CHENNAI,
        Location.KARNATAKA,
    ]
    assert SCOPE.locations_for("work from home policy") is None
    assert SCOPE.locations_for("sick leave policyholder guide") is None


def test_describe_for_names_policies_that_do_not_cover_the_location() -> None:
    usa = SCOPE.describe_for(Location.USA)
    assert "(USA)" in usa and "the Leave Policy covers Chennai (Tamil Nadu)" in usa
    assert "do not apply" in usa
    for covered in (Location.CHENNAI, Location.KARNATAKA, None):
        assert SCOPE.describe_for(covered).startswith("All company policies")
    assert PolicyScope().describe_for(Location.USA).startswith("All company policies")


def test_private_scope_file_wins_over_the_example(tmp_path: Path) -> None:
    assert load_policy_scope(tmp_path) == load_policy_scope(EXAMPLE_POLICY_SCOPE.parent)
    assert load_policy_scope(tmp_path).locations_for("leave policy") == [
        Location.CHENNAI,
        Location.KARNATAKA,
    ]
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "policy_scope.yaml").write_text(
        "policies:\n  - {name: WFH Policy, match: wfh policy, applies_to: [usa]}\n"
    )
    assert [r.name for r in load_policy_scope(tmp_path).policies] == ["WFH Policy"]
