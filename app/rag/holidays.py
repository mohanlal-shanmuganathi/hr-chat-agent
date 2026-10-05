"""Parse holiday-list tables into structured rows."""

import re
from dataclasses import dataclass
from datetime import date

from app.rag.metadata import parse_date

_ROW = re.compile(r"^(\d{1,2})\s+(.*?)\s+(\d{1,2}-[A-Za-z]{3,9}-\d{4})\s+([A-Za-z]+day)$")
_HEADER = re.compile(r"^S\.?\s*No\.?\b", re.IGNORECASE)


@dataclass(frozen=True)
class HolidayRow:
    holiday_date: date
    name: str
    weekday_matches: bool  # printed day name agrees with the date


def parse_holiday_rows(lines: list[str]) -> list[HolidayRow]:
    """Rows look like `<n> <name> <dd-Mon-yyyy> <Weekday>`.

    A name can wrap onto the line *above* its row (e.g. "Independence Day, July 4th, is Observed"
    followed by "5 on Friday, July 3rd 03-July-2026 Friday"); such a line is prefixed to the name.
    """
    rows: list[HolidayRow] = []
    in_table = False
    carry: list[str] = []
    for line in lines:
        if _HEADER.match(line):
            in_table, carry = True, []
            continue
        if not in_table:
            continue
        match = _ROW.match(line)
        if match is None:
            carry.append(line)
            continue
        _, name, raw_date, weekday = match.groups()
        parsed = parse_date(raw_date)
        if parsed is None:
            carry = []
            continue
        full_name = " ".join([*carry, " ".join(name.split())]).strip()
        rows.append(
            HolidayRow(
                holiday_date=parsed,
                name=full_name,
                weekday_matches=parsed.strftime("%A").lower() == weekday.lower(),
            )
        )
        carry = []
    return rows
