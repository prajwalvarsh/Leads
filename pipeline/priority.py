"""Task 2, stage priority: High / Medium / Low from the KPI columns (rules in docs/task2.md).

Checked top to bottom, the first match wins:
- Low:    no WEG or Miet segment found, or units_managed stated below priority.low_units_below
- High:   WEG or Miet, units_managed >= priority.high_units_min, and open to switching
          (remote reading not mentioned, or no metering provider named)
- Medium: everything else (incl. WEG/Miet with unknown units)
`why` is built in code from the matched rule; housing companies add "housing company: buys via tenders".
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from pipeline.contract import Ctx, StageResult

STAGE = "priority"


def _units(n: int) -> str:
    return f"{n:,} units"


def decide(segments: str | None, units: int | None, remote_reading: str | None, provider: str | None,
           housing: str | None, low_below: int, high_min: int) -> tuple[str, str]:
    segs = [s.strip() for s in (segments or "").split(",") if s.strip()]
    rental = [s for s in segs if s in ("WEG", "Miet")]
    no_remote = remote_reading != "yes"
    no_provider = not provider or provider == "not named"
    if not rental:
        prio, why = "Low", "no WEG or Miet segment found" + (f" ({', '.join(segs)} only)" if segs else "")
    elif units is not None and units < low_below:
        prio, why = "Low", f"{', '.join(rental)}, only {_units(units)}"
    elif units is not None and units >= high_min and (no_remote or no_provider):
        reasons = [r for r, on in (("no remote reading mentioned", no_remote), ("no metering provider named", no_provider)) if on]
        prio, why = "High", f"{', '.join(rental)}, {_units(units)}, {', '.join(reasons)}"
    elif units is None:
        prio, why = "Medium", f"{', '.join(rental)}, units unknown"
    elif units >= high_min:
        prio, why = "Medium", f"{', '.join(rental)}, {_units(units)}, remote reading mentioned and provider {provider}"
    else:
        prio, why = "Medium", f"{', '.join(rental)}, {_units(units)} (below {high_min})"
    if housing == "yes":
        why += "; housing company: buys via tenders"
    return prio, why


def run_priority(ctx: Ctx, params: dict[str, Any] | None = None) -> StageResult:
    res = StageResult(run_id=ctx.run_id, stage=STAGE)
    pc = ctx.cfg["priority"]
    rows = ctx.con.execute(
        """SELECT k.company_id, k.segments, k.units_managed, k.remote_reading_mentioned, k.metering_provider,
                  k.is_housing_company
           FROM kpi k JOIN company co USING (company_id)
           WHERE co.tab = 'companies' AND co.status = 'confirmed (website)'"""
    ).fetchall()
    out = [[cid, *decide(s, u, r, p, h, pc["low_units_below"], pc["high_units_min"])]
           for cid, s, u, r, p, h in rows]
    ctx.con.execute("DELETE FROM priority")
    if out:
        ctx.con.executemany("INSERT INTO priority VALUES (?, ?, ?)", out)
    missing = ctx.con.execute(
        """SELECT count(*) FROM company co LEFT JOIN kpi k USING (company_id)
           WHERE co.tab = 'companies' AND co.status = 'confirmed (website)' AND k.company_id IS NULL"""
    ).fetchone()[0]
    counts = Counter(o[1] for o in out)
    res.counts = {"companies": len(out), "High": counts["High"], "Medium": counts["Medium"], "Low": counts["Low"],
                  "confirmed_without_kpi": missing}
    for k, v in res.counts.items():
        ctx.log(STAGE, k, v)
    if missing:
        res.errors.append(f"{missing} confirmed companies have no KPI row: run kpi_extract")
    return res.finish("failed" if missing else "ok")
