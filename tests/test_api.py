import time

import pytest
from fastapi.testclient import TestClient

import pipeline.api as api
import pipeline.contract as contract
import pipeline.db as db
import pipeline.stages as stages
from pipeline.contract import StageResult

KEY = {"X-API-Key": "test-key"}


def ok_stage(ctx, params):  # noqa: ANN001, ANN201
    ctx.con.execute("""INSERT INTO company (company_id, company_name, status, tab, first_seen_run, last_changed_run)
                       VALUES ('c1', 'Neu Hausverwaltung GmbH', 'confirmed', 'companies', ?, ?),
                              ('c2', NULL, NULL, 'excluded', ?, ?)""", [ctx.run_id] * 4)
    return StageResult(run_id=ctx.run_id, stage="ok_stage", counts={"mode": params["mode"]}).finish()


def serper_stage(ctx, params):  # noqa: ANN001, ANN201
    return StageResult(run_id=ctx.run_id, stage="serper_stage", counts={"skipped": "0 credits"}).finish("skipped")


def bad_stage(ctx, params):  # noqa: ANN001, ANN201
    raise RuntimeError("boom")


@pytest.fixture
def client(tmp_path, monkeypatch):  # noqa: ANN001, ANN201
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.duckdb")
    monkeypatch.setattr(contract, "LOCK_PATH", tmp_path / "t.lock")
    monkeypatch.setattr(api, "LOCK_PATH", tmp_path / "t.lock")
    monkeypatch.setenv("PIPELINE_API_KEY", "test-key")
    monkeypatch.setattr(stages, "STAGES", {"ok_stage": ok_stage, "serper_stage": serper_stage, "bad_stage": bad_stage})
    calls: list = []
    monkeypatch.setattr(api, "_callback", lambda url, payload: calls.append((url, payload)) or "ok 200")
    api._runs.clear()
    c = TestClient(api.app)
    c.callbacks = calls
    return c


def wait(c: TestClient, run_id: str) -> dict:
    for _ in range(100):
        r = c.get(f"/runs/{run_id}", headers=KEY).json()
        if r["status"] != "running" and not contract.LOCK_PATH.exists():
            return r
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_api_key_required(client: TestClient) -> None:
    assert client.get("/health").status_code == 401
    assert client.get("/health", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/health", headers=KEY).json() == {"status": "ok", "run_active": False}


def test_run_with_callback_and_changes(client: TestClient) -> None:
    r = client.post("/runs", headers=KEY, json={"stages": ["ok_stage", "serper_stage"], "callback_url": "http://cb"})
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    run = wait(client, run_id)
    assert run["status"] == "ok"                          # a skipped stage does not fail the run
    assert [s["status"] for s in run["stages"]] == ["ok", "skipped"]
    assert run["stages"][0]["counts"]["mode"] == "incremental"   # default mode
    url, payload = client.callbacks[0]
    assert url == "http://cb" and payload["run_id"] == run_id and payload["status"] == "ok"
    ch = client.get(f"/runs/{run_id}/changes", headers=KEY).json()
    assert ch["counts"]["new"] == 1 and ch["new"][0]["company_name"] == "Neu Hausverwaltung GmbH"   # excluded row left out
    assert client.get(f"/runs/{run_id}/changes?include_excluded=true", headers=KEY).json()["counts"]["new"] == 2
    api._runs.clear()   # status is also readable from run_log (another process, or after a restart)
    assert client.get(f"/runs/{run_id}", headers=KEY).json()["status"] == "ok"


def test_failed_stage_stops_run(client: TestClient) -> None:
    run_id = client.post("/runs", headers=KEY, json={"stages": ["bad_stage", "ok_stage"]}).json()["run_id"]
    run = wait(client, run_id)
    assert run["status"] == "failed" and len(run["stages"]) == 1
    assert run["stages"][0]["stage"] == "bad_stage" and "boom" in run["stages"][0]["errors"][0]


def test_busy_and_bad_requests(client: TestClient) -> None:
    contract.LOCK_PATH.write_text('{"run_id": "other"}')
    assert client.post("/runs", headers=KEY, json={"stages": ["ok_stage"]}).status_code == 409
    contract.LOCK_PATH.unlink()
    assert client.post("/runs", headers=KEY, json={"stages": ["nope"]}).status_code == 422
    assert client.get("/runs/unknown", headers=KEY).status_code == 404
