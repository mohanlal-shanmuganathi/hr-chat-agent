"""Location resolution for location-specific policies (e.g. holiday calendars).

Deterministic rule (the same answer whatever the model does):
- the employee named a location we have -> use it;
- they named nothing -> use the location on their profile, and report the other available
  locations so the answer can offer them;
- they named a location we don't have, or their own location has no list -> ask them to choose
  from the available locations.
"""

from dataclasses import dataclass

from app.db.models import Location

_ALIASES: dict[str, Location] = {
    "chennai": Location.CHENNAI,
    "tamil nadu": Location.CHENNAI,
    "tamilnadu": Location.CHENNAI,
    "tn": Location.CHENNAI,
    "karnataka": Location.KARNATAKA,
    "bangalore": Location.KARNATAKA,
    "bengaluru": Location.KARNATAKA,
    "usa": Location.USA,
    "us": Location.USA,
    "united states": Location.USA,
    "america": Location.USA,
    "plano": Location.USA,
}

DISPLAY = {
    Location.CHENNAI: "Chennai (Tamil Nadu)",
    Location.KARNATAKA: "Karnataka (Bengaluru)",
    Location.USA: "USA",
}


@dataclass(frozen=True)
class LocationResolution:
    location: Location | None
    needs_confirmation: bool
    options: list[str]
    suggested: str | None
    message: str | None = None
    from_profile: bool = False  # the employee named no location; their profile's was used


def parse_location(text: str) -> Location | None:
    key = " ".join(text.lower().replace(",", " ").split())
    if key in _ALIASES:
        return _ALIASES[key]
    return next((loc for alias, loc in _ALIASES.items() if alias in key.split()), None)


def resolve_location(
    requested: str | None,
    profile_location: Location,
    available: list[Location],
) -> LocationResolution:
    options = [DISPLAY[loc] for loc in available]
    suggested = DISPLAY[profile_location] if profile_location in available else None
    if requested:
        loc = parse_location(requested)
        if loc is not None and loc in available:
            return LocationResolution(loc, False, options, suggested)
        return LocationResolution(
            None,
            True,
            options,
            suggested,
            message=f"No policy found for '{requested}'.",
        )
    if profile_location in available:
        return LocationResolution(profile_location, False, options, suggested, from_profile=True)
    return LocationResolution(
        None,
        True,
        options,
        None,
        message=f"No holiday list for the location on record ({DISPLAY[profile_location]}).",
    )
