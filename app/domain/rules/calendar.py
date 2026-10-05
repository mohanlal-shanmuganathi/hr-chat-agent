"""Working-day arithmetic. Weekly offs and public holidays inside a leave period are not counted."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from app.domain.hr import HolidayView
from app.domain.rules.config import CalendarRules

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MAX_SPAN_DAYS = 366


class InvalidDateRangeError(ValueError):
    pass


@dataclass(frozen=True)
class ExcludedDay:
    day: date
    reason: str  # "weekend" or the holiday name


@dataclass(frozen=True)
class WorkingDays:
    start: date
    end: date
    calendar_days: int
    working_days: Decimal
    excluded: list[ExcludedDay]


def count_working_days(
    start: date,
    end: date,
    holidays: Iterable[HolidayView],
    rules: CalendarRules,
    half_day: bool = False,
) -> WorkingDays:
    if end < start:
        raise InvalidDateRangeError("End date is before start date.")
    span = (end - start).days + 1
    if span > MAX_SPAN_DAYS:
        raise InvalidDateRangeError(f"Date range is longer than {MAX_SPAN_DAYS} days.")
    if half_day and start != end:
        raise InvalidDateRangeError("A half day must start and end on the same date.")

    weekend = {_WEEKDAYS.index(d) for d in rules.weekend_days}
    holiday_names = {h.holiday_date: h.name for h in holidays}
    excluded: list[ExcludedDay] = []
    working = 0
    for offset in range(span):
        day = start + timedelta(days=offset)
        if day.weekday() in weekend:
            excluded.append(ExcludedDay(day, "weekend"))
        elif day in holiday_names:
            excluded.append(ExcludedDay(day, holiday_names[day]))
        else:
            working += 1

    days = Decimal(working)
    if half_day and working == 1:
        days = Decimal("0.5")
    return WorkingDays(
        start=start, end=end, calendar_days=span, working_days=days, excluded=excluded
    )
