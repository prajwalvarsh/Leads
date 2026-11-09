"""Stage 5b: investigate (optional agent for the review queue). Not built yet: the user chose to skip
it for now. With agent.enabled = false the stage returns status=skipped, as the spec requires."""
from __future__ import annotations

from typing import Any

from pipeline.contract import Ctx, StageResult

STAGE = "investigate"


def run_investigate(ctx: Ctx, params: dict[str, Any] | None = None) -> StageResult:
    res = StageResult(run_id=ctx.run_id, stage=STAGE)
    if not ctx.cfg.get("agent", {}).get("enabled"):
        ctx.log(STAGE, "skipped", "agent.enabled = false")
        return res.finish("skipped")
    res.errors.append("investigate agent is not built yet; set agent.enabled: false")
    return res.finish("failed")
