from __future__ import annotations

import threading
from typing import Any

from ..constants import MAX_ATTEMPTS_DEFAULT
from ..errors import BudgetExhausted
from ..serialization import utc_now_iso


class AttemptBudget:
    def __init__(self, maximum: int = MAX_ATTEMPTS_DEFAULT) -> None:
        if maximum <= 0:
            raise ValueError("Attempt budget must be positive.")
        self.maximum = maximum
        self._used = 0
        self._claims: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return self.maximum - self._used

    def claim(
        self,
        *,
        logical_request_id: str,
        reason: str,
    ) -> int:
        with self._lock:
            if self._used >= self.maximum:
                raise BudgetExhausted(
                    f"HTTP attempt cap {self.maximum} is exhausted."
                )
            self._used += 1
            number = self._used
            self._claims.append(
                {
                    "attempt_number": number,
                    "logical_request_id": logical_request_id,
                    "reason": reason,
                    "claimed_at": utc_now_iso(),
                }
            )
            return number

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "maximum": self.maximum,
                "used": self._used,
                "remaining": self.maximum - self._used,
                "claims": list(self._claims),
            }
