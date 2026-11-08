"""Hard counters for paid APIs: abort above the per-run cap or the monthly cap."""
from __future__ import annotations

from datetime import datetime, timezone

import duckdb


class CapExceeded(RuntimeError):
    pass


class Budget:
    """Reserve one unit before every paid call.

    run_cap   -- limit for this run (or this stage, for Tavily stage budgets)
    month_cap -- limit for the calendar month, summed over all runs (free tier is monthly)
    """

    def __init__(
        self,
        con: duckdb.DuckDBPyConnection,
        api: str,
        run_cap: int,
        month_cap: int | None = None,
        all_time: bool = False,
    ) -> None:
        """all_time=True: month_cap applies to the total over all months (one-time free
        credits, e.g. Serper signup credits)."""
        self.con = con
        self.api = api
        self.run_cap = run_cap
        self.month_cap = month_cap if month_cap is not None else run_cap
        self.all_time = all_time
        self.used = 0
        self.month = datetime.now(timezone.utc).strftime("%Y-%m")

    def month_used(self) -> int:
        if self.all_time:
            row = self.con.execute(
                "SELECT sum(calls) FROM api_usage WHERE api = ?", [self.api]
            ).fetchone()
        else:
            row = self.con.execute(
                "SELECT calls FROM api_usage WHERE month = ? AND api = ?", [self.month, self.api]
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def remaining(self) -> int:
        return max(0, min(self.run_cap - self.used, self.month_cap - self.month_used()))

    def spend(self, units: int = 1) -> None:
        if self.used + units > self.run_cap:
            raise CapExceeded(f"{self.api}: run cap {self.run_cap} reached")
        if self.month_used() + units > self.month_cap:
            raise CapExceeded(f"{self.api}: monthly cap {self.month_cap} reached")
        self.used += units
        self.con.execute(
            """INSERT INTO api_usage VALUES (?, ?, ?)
               ON CONFLICT (month, api) DO UPDATE SET calls = calls + excluded.calls""",
            [self.month, self.api, units],
        )
