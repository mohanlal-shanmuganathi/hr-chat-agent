from datetime import date

import pytest

from app.hr.seed import quarters_credited


@pytest.mark.parametrize(
    ("as_of", "joined", "expected"),
    [
        (date(2026, 1, 1), date(2020, 1, 1), 1),  # Q1 credited on its first day
        (date(2026, 3, 31), date(2020, 1, 1), 1),
        (date(2026, 10, 3), date(2020, 1, 1), 4),
        (date(2026, 10, 3), date(2026, 8, 3), 2),  # joined in Q3: Q3 + Q4
        (date(2026, 10, 3), date(2026, 9, 30), 2),  # last day of Q3 still counts Q3
        (date(2026, 10, 3), date(2026, 10, 1), 1),
    ],
)
def test_quarters_credited(as_of: date, joined: date, expected: int) -> None:
    assert quarters_credited(2026, as_of, joined) == expected
