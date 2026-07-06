"""Effective-date resolution shared by code-set and policy retrieval.

CLAUDE.md guardrail 5 requires every lookup to resolve against the date of
service, not today. This builds the "in force on the date of service" filter
used by every retrieval path, so no query can drift in how it defines
validity. Both the code-set and payer-policy models expose effective_start and
effective_end columns, so the helper takes the model class and reads those two
columns generically.
"""

import datetime
from typing import Any


def effective_date_window(model: Any, date_of_service: datetime.date) -> Any:
    """SQLAlchemy condition selecting model rows in force on the date of service.

    A row is in force when its effective_start is on or before the date of
    service and its effective_end is null (still active) or on or after the
    date of service. effective_end is treated as the last valid date
    (inclusive), matching how annual code-set and policy retirements are dated.
    """
    return (model.effective_start <= date_of_service) & (
        model.effective_end.is_(None) | (model.effective_end >= date_of_service)
    )
