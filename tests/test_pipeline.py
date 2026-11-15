"""End-to-end checks with the built-in simulator (no hardware).  Run:  py -m pytest -q"""
import time

import pytest
from fastapi.testclient import TestClient

from sentinel.hub import Hub
from sentinel.server import create_app
from sentinel.sources import parse_data_line


def test_parse_both_firmware_formats():
    new = parse_data_line("DATA,1000,27.25,0.401,2.001,1.402")
    old = parse_data_line("DATA,2777,1,normal,26.62,0.439,114,2.006")
    assert new["ph_pin_v"] == pytest.approx(1.402)
    assert old["tds_v"] == pytest.approx(0.439) and old["ph_pin_v"] != old["ph_pin_v"]  # NaN
    assert parse_data_line("hello") is None and parse_data_line("DATA,1,2") is None


def wait(cond, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture(scope="module")
def client():
    hub = Hub(sim=True, sim_speed=200)
    hub.start()
    c = TestClient(create_app(hub))
    c.hub = hub
    assert wait(lambda: hub.latest is not None)
    yield c
    hub.shutdown()


def record(c, run, label, scenario, minutes=1.0):
    assert c.post("/api/sim", json={"scenario": scenario}).status_code == 200
    time.sleep(0.2)
    r = c.post("/api/record/start", json={"run": run, "label": label, "minutes": minutes})
    assert r.status_code == 200, r.text
    assert wait(lambda: c.hub.rec is None, 30)


def test_record_train_monitor_alert(client):
    c, hub = client, client.hub
    for run in range(1, 6):
        record(c, run, "normal", "normal")
    record(c, 6, "salt_lvl1", "salt")
    record(c, 7, "starch_lvl1", "starch")
    runs = c.get("/api/runs").json()
    assert runs["next_run"] == 8 and len(runs["runs"]) == 7
    # duplicate run number is refused
    assert c.post("/api/record/start", json={"run": 3, "label": "normal", "minutes": 1}).status_code == 400

    res = c.post("/api/train", json={"use_ph": False}).json()
    assert res["ok"], res
    rep = c.get("/api/report").json()
    mspc = rep["models"]["mspc"]
    assert mspc["false_alarm"] <= 0.05 and mspc["detection"] >= 0.8
    assert set(rep["models"]) == {"mspc", "pca", "iforest", "ocsvm", "lof"}

    c.post("/api/model", json={"key": "mspc"})
    c.post("/api/sim", json={"scenario": "normal"})
    time.sleep(1.0)                      # let the previous (starch) scenario wash out
    assert c.post("/api/monitor", json={"on": True}).status_code == 200
    assert wait(lambda: hub.det_out["status"] == "normal")
    time.sleep(1.0)
    assert not any(e["kind"] == "anomaly" for e in hub.events), "false alarm on normal water"
    c.post("/api/sim", json={"scenario": "salt"})
    assert wait(lambda: any(e["kind"] == "anomaly" for e in hub.events))
    cause = c.get("/api/state").json()["detector"]["cause"]
    assert cause and cause[0]["sensor"] == "tds_ppm"
    c.post("/api/sim", json={"scenario": "probe_out"})
    assert wait(lambda: hub.det_out["status"] == "fault")
    c.post("/api/sim", json={"scenario": "normal"})
    assert wait(lambda: any(e["kind"] == "recovered" for e in hub.events), 40)

    for m in ("pca", "tsne"):
        e = c.get(f"/api/embedding?method={m}").json()
        assert len(e["xy"]) == len(e["label"]) > 50


def test_security(client):
    c = client
    assert c.post("/api/monitor", content="on=1", headers={"content-type": "text/plain"}).status_code == 415
    assert c.get("/api/state", headers={"host": "evil.example.com"}).status_code == 403
    assert c.post("/api/ingest", json={"tds_v": 1}).status_code == 401
    c.post("/api/config", json={"ingest_token": "abc", "unknown_key": 1, "alerts": {"line_token": "secret"}})
    cfg = c.get("/api/config").json()
    assert "unknown_key" not in cfg and cfg["alerts"]["line_token"].startswith("••••")
    c.post("/api/config", json={"alerts": {"line_token": cfg["alerts"]["line_token"]}})
    assert client.hub.cfg["alerts"]["line_token"] == "secret"     # masked value does not overwrite
    ok = c.post("/api/ingest", json={"temp_c": 27, "tds_v": 0.4, "turb_v": 2, "ph_pin_v": 1.4},
                headers={"x-token": "abc"})
    assert ok.status_code == 200
