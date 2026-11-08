import json

import pytest

from pipeline import contract
from pipeline.contract import RunLock, RunLocked, StageResult, deep_merge


def test_stage_result_json_roundtrip() -> None:
    r = StageResult(run_id="r1", stage="discover", counts={"osm": 3}).finish()
    d = json.loads(r.model_dump_json())
    assert d["status"] == "ok" and d["counts"] == {"osm": 3} and d["finished_at"]


def test_second_run_fails_fast(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(contract, "LOCK_PATH", tmp_path / "pipeline.lock")
    with RunLock("run-a"):
        with pytest.raises(RunLocked, match="another run is active"):
            with RunLock("run-b"):
                pass
    # released after the first run ends
    with RunLock("run-c"):
        pass


def test_params_override_config() -> None:
    base = {"sources": {"osm": {"enabled": True}, "places": {"enabled": False}}, "caps": {"x": 1}}
    merged = deep_merge(base, {"sources": {"osm": {"enabled": False}}})
    assert merged["sources"]["osm"]["enabled"] is False
    assert merged["sources"]["places"]["enabled"] is False
    assert base["sources"]["osm"]["enabled"] is True  # original untouched
