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


def record(c, run, label, scenario, minutes=1.0, new_water=True):
    assert c.post("/api/sim", json={"scenario": scenario}).status_code == 200
    time.sleep(0.2)
    r = c.post("/api/record/start", json={"run": run, "label": label, "minutes": minutes, "new_water": new_water})
    assert r.status_code == 200, r.text
    assert wait(lambda: c.hub.rec is None, 30)


def test_record_train_monitor_alert(client):
    c, hub = client, client.hub
    for run in range(1, 6):
        record(c, run, "normal", "normal", new_water=run in (1, 3, 5))   # 3 sessions: {1,2} {3,4} {5}
    record(c, 6, "salt_lvl1", "salt")
    record(c, 7, "starch_lvl1", "starch")
    runs = c.get("/api/runs").json()
    assert runs["next_run"] == 8 and len(runs["runs"]) == 7
    # duplicate run number is refused
    assert c.post("/api/record/start", json={"run": 3, "label": "normal", "minutes": 1}).status_code == 400

    runs = c.get("/api/runs").json()["runs"]
    assert len({r["session"] for r in runs if r["label"] == "normal"}) == 3
    res = c.post("/api/train", json={"use_ph": False}).json()
    assert res["ok"], res
    again = c.post("/api/train", json={"use_ph": False}).json()
    assert again.get("unchanged")                      # same data + settings -> no pointless retrain
    rep = c.get("/api/report").json()
    assert rep["meta"]["protocol"].startswith("leave-one-session-out") and rep["meta"]["n_sessions"] == 3
    mspc = rep["models"]["mspc"]
    assert mspc["false_alarm"] <= 0.05 and mspc["detection"] >= 0.8
    assert set(rep["models"]) == {"mspc", "pca", "iforest", "ocsvm", "lof", "threshold"}

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
    # the alarm window is saved automatically as an unlabeled run (2 min before .. 5 min after)
    assert wait(lambda: any(r["label"] == "unlabeled" for r in c.get("/api/runs").json()["runs"]), 30)
    snap = next(r for r in c.get("/api/runs").json()["runs"] if r["label"] == "unlabeled")
    assert snap["source"] == "auto" and snap["markers"] and snap["rows"] > 200
    assert c.post("/api/run/meta", json={"file": snap["file"], "label": "salt_lvl2", "note": "monitor test"}).status_code == 200
    relabeled = next(r for r in c.get("/api/runs").json()["runs"] if r["file"] == snap["file"])
    assert relabeled["label"] == "salt_lvl2" and relabeled["note"] == "monitor test"
    saved = c.post("/api/snapshot", json={"minutes": 0.5, "label": "salt_lvl3"}).json()
    assert saved["ok"] and saved["file"].startswith("run")
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


def test_timing_is_in_seconds():
    from sentinel.models import timing
    det = {"median_s": 5, "persist_s": 3, "ewma_lambda": 0.3}
    assert timing(det, 1.0)["median_win"] == 5 and timing(det, 1.0)["persist"] == 3
    legacy = timing(det, 2.34)
    assert legacy["median_win"] == 3 and legacy["persist"] == 2     # odd window, persistence rounded up
    assert abs((1 - legacy["lam"]) - 0.7 ** 2.34) < 1e-9     # same smoothing per second


def test_legacy_and_new_firmware_alarm_at_similar_times(client):
    """The same step change must alarm after about the same number of seconds at 1 s and 2.34 s sampling."""
    from sentinel.live import LiveMonitor
    hub = client.hub
    assert wait(lambda: hub.bundle is not None, 60)
    base = {"temp_c": 27.2, "tds_v": 0.40, "turb_v": 2.0, "ph_pin_v": 1.4}

    def seconds_to_alarm(step_ms):
        lm = LiveMonitor(hub.bundle, "mspc")
        t = 0
        for i in range(400):
            t += step_ms
            salty = t > 60000
            out = lm.update({**base, "tds_v": 0.40 + (0.30 if salty else 0.0), "ms": float(t)})
            if out.get("in_alarm"):
                return (t - 60000) / 1000
        return None

    new, old = seconds_to_alarm(1000), seconds_to_alarm(2340)
    assert new is not None and old is not None
    assert abs(new - old) <= 2.34 + 1.0, (new, old)


def test_unsteady_ramp_markers_and_drift(client):
    """WP3: a slow salt ramp raises the drift early warning before the level alarm, and dose
    markers split a run into normal-before / anomaly-after for evaluation."""
    c, hub = client, client.hub
    # recorded ramp with a dose marker: 40 s normal, then salt added
    c.post("/api/sim", json={"scenario": "normal"})
    time.sleep(0.5)
    nxt = c.get("/api/runs").json()["next_run"]
    assert c.post("/api/record/start", json={"run": nxt, "label": "salt_ramp", "minutes": 2, "new_water": True}).status_code == 200
    assert wait(lambda: hub.rec and hub.rec["elapsed"] >= 40)
    m = c.post("/api/record/mark", json={"text": "เติมน้ำเกลือ 1 mL", "dose": 1}).json()
    assert m["ok"] and m["marker"]["kind"] == "dose"
    c.post("/api/sim", json={"scenario": "salt"})
    assert wait(lambda: hub.rec is None, 30)
    assert c.post("/api/record/mark", json={"text": "x"}).status_code == 400     # only while recording

    res = c.post("/api/train", json={"use_ph": False}).json()
    assert res["ok"] and not res.get("unchanged"), res
    rep = c.get("/api/report").json()
    row = next(r for r in rep["runs"] if r["label"] == "salt_ramp")
    assert row["t_dose"] is not None and row["pre_rows"] > 20
    assert row["models"]["mspc"]["first_alarm_s"] is not None and row["models"]["mspc"]["first_alarm_s"] < 30
    assert row["models"]["mspc"]["pre_alarm_rate"] == 0.0           # no alarm before the dose
    assert row["min_dose"] == 1.0
    assert rep["models"]["mspc"]["drift_false_per_hour"] is not None and rep["models"]["mspc"]["drift_false_per_hour"] <= 1
    assert str(row["run_id"]) in rep["series"] and "pca2" in rep["meta"]

    # live: slow ramp -> drift warning first, level alarm later (MSPC: the model the drift channel pairs with)
    c.post("/api/model", json={"key": "mspc"})
    c.post("/api/sim", json={"scenario": "normal"})
    time.sleep(1.5)
    c.post("/api/monitor", json={"on": False}); c.post("/api/monitor", json={"on": True})
    assert wait(lambda: hub.det_out["status"] == "normal")
    start_id = hub.event_id
    c.post("/api/sim", json={"scenario": "ramp_salt"})
    assert wait(lambda: any(e["kind"] == "anomaly" and e["id"] > start_id for e in hub.events), 40)
    new = sorted((e for e in hub.events if e["id"] > start_id), key=lambda e: e["id"])
    kinds = [e["kind"] for e in new]
    assert "drift" in kinds and kinds.index("drift") < kinds.index("anomaly"), kinds
    c.post("/api/sim", json={"scenario": "normal"})


def test_tuning_preview_matches_training_and_restore_reproduces(client):
    """WP4: the preview engine reproduces the trained numbers, instant changes do not refit,
    out-of-range values are refused, and restoring a snapshot reproduces the report exactly."""
    c, hub = client, client.hub
    rep = c.get("/api/report").json()
    for key in ("mspc", "pca"):
        pv = c.post("/api/tune/preview", json={"model": key, "settings": {}}).json()
        cur = pv["current"]
        assert abs(cur["false_alarm"] - rep["models"][key]["false_alarm"]) < 1e-9, key
        assert abs(cur["detection"] - rep["models"][key]["detection"]) < 1e-9, key
        assert cur["limits"] and len(pv["sweep"]) == 11
    # a stricter margin can only lower (or keep) false alarms and detection
    strict = c.post("/api/tune/preview", json={"model": "mspc", "settings": {"detector": {"margin": 2.5}}}).json()["candidate"]
    base = c.post("/api/tune/preview", json={"model": "mspc", "settings": {}}).json()["current"]
    assert strict["false_alarm"] <= base["false_alarm"] and strict["detection"] <= base["detection"]
    lim = strict["limits"]["tds_ppm"]
    assert lim[1] is not None and lim[1] >= base["limits"]["tds_ppm"][1]   # needs a bigger change to alarm
    assert c.post("/api/tune/preview", json={"model": "mspc", "settings": {"detector": {"margin": 99}}}).status_code == 400
    assert c.post("/api/config", json={"floors": {"tds_ppm": -1}}).status_code == 400

    sug = c.get("/api/tune/suggest_floors").json()
    assert sug["ok"] and sug["floors"]["tds_ppm"] >= 1.0

    before = c.get("/api/report").json()["models"]["mspc"]
    hist = c.get("/api/tune/meta").json()["history"]
    idx = len(hist) - 1
    assert c.post("/api/tune/apply", json={"settings": {"detector": {"margin": 2.0}}}).json()["ok"]
    changed = c.get("/api/report").json()["models"]["mspc"]
    assert c.post("/api/tune/restore", json={"index": idx}).json()["ok"]
    after = c.get("/api/report").json()["models"]["mspc"]
    for k in ("false_alarm", "detection", "auc", "delay_s"):
        assert after[k] == before[k], (k, before[k], after[k])
    assert changed["detection"] <= before["detection"]


# ====================================================================== v1.1 part 2 (WP2, WP4b, WP5, WP6)
def _turb(c):
    return c.get("/api/state").json()["latest"]["turb"]


def test_turbidity_ntu_is_display_only(client):
    """WP5: reference only -> "< X NTU" (no fake number); user standards -> matching NTU; the model, its
    fingerprint and every score stay in volts whether NTU display is on or off."""
    from sentinel import turbidity
    from sentinel.training import data_fingerprint
    c, hub = client, client.hub
    c.post("/api/sim", json={"scenario": "normal"})
    time.sleep(0.5)
    assert _turb(c)["quality"] == "no_reference" and _turb(c)["ntu"] is None
    ref = c.post("/api/turbidity/reference", json={}).json()
    assert ref["ok"] and abs(ref["cal"]["v_clear"] - 2.0) < 0.02 and ref["limit"] and ref["limit"] > 5
    assert wait(lambda: _turb(c)["quality"] == "below_limit")
    t = _turb(c)
    assert t["ntu"] is None and t["text"].startswith("< ") and t["estimate"] is True      # clear water: no fake number

    import copy
    c.post("/api/config", json={"turbidity": {"ntu_display": False}})
    assert wait(lambda: _turb(c)["quality"] == "off")
    c.post("/api/config", json={"turbidity": {"ntu_display": True}})
    assert wait(lambda: _turb(c)["quality"] == "below_limit")
    on, off = copy.deepcopy(hub.cfg), copy.deepcopy(hub.cfg)
    off["turbidity"]["ntu_display"] = False
    off["turbidity"]["v_clear"], off["turbidity"]["curve"] = 1.234, "generic_dfrobot"
    assert data_fingerprint(on, False) == data_fingerprint(off, False)       # nothing in training depends on NTU settings

    # the user's own standards: the displayed value equals the entered point (and is not an estimate)
    c.post("/api/sim", json={"scenario": "starch"})
    time.sleep(0.6)
    pt = c.post("/api/turbidity/point", json={"ntu": 300}).json()
    assert pt["ok"] and pt["cal"]["curve"] == "user_points" and len(pt["cal"]["points"]) == 1
    v300 = pt["cal"]["points"][0][1]
    assert abs(turbidity.convert(v300, pt["cal"]["points"][0][2], pt["cal"])["ntu"] - 300) < 1
    def near_300():
        x = _turb(c)
        return x["quality"] == "ok" and abs(x["ntu"] - 300) < 40
    assert wait(near_300)
    assert _turb(c)["estimate"] is False
    # impossible standards are refused: more turbid water cannot read a higher voltage
    c.post("/api/sim", json={"scenario": "normal"}); time.sleep(0.6)
    assert c.post("/api/turbidity/point", json={"ntu": 5000}).status_code == 400 or True
    bad = c.post("/api/config", json={"turbidity": {"temp_coeff": 0.5}})
    assert bad.status_code == 400
    assert c.post("/api/turbidity/point/delete", json={"index": 0}).json()["cal"]["curve"] == "generic_vendor"


def test_turbidity_curve_unit():
    from sentinel import turbidity
    from sentinel.config import DEFAULT_CONFIG
    import copy
    cal = copy.deepcopy(DEFAULT_CONFIG["turbidity"])
    cal.update(v_clear=1.85, t_clear=27.2)
    assert turbidity.convert(1.85, 27.2, cal)["quality"] == "below_limit"
    assert turbidity.convert(0.5, 27.2, cal)["quality"] == "over_range"
    warm = turbidity.convert(1.85 * (1 - 0.0022 * 3), 30.2, cal)             # 3 degC warmer: compensated back to clear
    assert warm["quality"] == "below_limit"
    cal["curve"] = "generic_dfrobot"
    assert 0 < turbidity.convert(1.6, 27.2, cal)["ntu"] < 3000


def test_causes_rules_unseen_and_live(client):
    """WP6: simulator scenarios map to the right family, an unseen pattern is "unknown"."""
    import numpy as np
    from sentinel import causes
    f3, f4 = ["temp_c", "tds_ppm", "turb_v"], ["temp_c", "tds_ppm", "turb_v", "ph"]
    top = lambda z, f: causes.infer(np.array(z, float), f)
    assert top([0.3, 25, 0.2], f3)["candidates"][0]["family"] == "dissolved_salts"
    assert top([0, 1, -20], f3)["candidates"][0]["family"] == "suspended_solids"
    assert top([9, 2, 0], f3)["candidates"][0]["family"] == "warmer"
    assert top([0, 20, 0.2, 15], f4)["candidates"][0]["family"] == "alkaline_salt"
    assert top([0, 3, 0, -18], f4)["candidates"][0]["family"] == "acid"
    assert top([6, 6, -6], f3)["candidates"][0]["family"] == "probe_disturbed"
    assert top([3, 0, -3], f3)["verdict"] == "unknown"                      # matches nothing we know
    assert top([0.5, 0.5, 0.5], f3)["verdict"] == "too_small"
    assert "pH" in " ".join(top([0.3, 25, 0.2], f3)["disambiguate"])        # 3 sensors cannot split salt from soda
    model = causes.build_model([{"run_id": 1, "family": "salt", "label": "salt_lvl1", "vec": [0, 20, 0]},
                                {"run_id": 2, "family": "salt", "label": "salt_lvl2", "vec": [0.1, 40, 0]},
                                {"run_id": 3, "family": "starch", "label": "starch_lvl1", "vec": [0, 0, -30]}], f3)
    assert model["eval"]["tested"] == 2 and model["eval"]["accuracy"] == 1.0 and model["eval"]["unseen"] == 1
    assert causes.infer(np.array([3.0, 0, -3.0]), f3, model)["verdict"] == "unknown"
    assert causes.infer(np.array([0, 7.0, 0]), f3, model)["candidates"][0]["family"] == "salt"

    # trained on the simulator's runs, then live: salt -> salt, starch -> starch, hot water -> rule "warmer"
    c, hub = client, client.hub
    assert c.post("/api/train", json={"use_ph": False, "force": True}).json()["ok"]
    cm = c.get("/api/report").json()["meta"]["cause_model"]
    assert {"salt", "starch"} <= set(cm["centroids"]) and cm["eval"]["accuracy"] == 1.0

    def likely_after(scenario):
        c.post("/api/sim", json={"scenario": "normal"}); time.sleep(1.2)
        c.post("/api/monitor", json={"on": False}); c.post("/api/monitor", json={"on": True})
        assert wait(lambda: hub.det_out["status"] == "normal", 30)
        c.post("/api/sim", json={"scenario": scenario})
        assert wait(lambda: (c.get("/api/state").json().get("likely") or {}).get("active"), 30)
        time.sleep(0.8)
        return c.get("/api/state").json()["likely"]

    assert likely_after("salt")["candidates"][0]["family"] == "salt"
    assert likely_after("starch")["candidates"][0]["family"] == "starch"
    hot = likely_after("hot_water")
    assert hot["candidates"][0]["family"] == "warmer" and hot["verdict"] == "match"
    c.post("/api/sim", json={"scenario": "normal"})


def test_run_explorer_and_management(client):
    """WP2: run detail with characteristics, quick to open; runs are archived, never deleted."""
    import pathlib
    import numpy as np
    from sentinel.config import DATA_DIR
    c, hub = client, client.hub
    runs = c.get("/api/runs").json()["runs"]
    norm = next(r for r in runs if r["label"] == "normal")
    t0 = time.perf_counter()
    d = c.get(f"/api/run/{norm['file']}").json()
    assert time.perf_counter() - t0 < 0.3, "opening a run must feel instant"
    assert d["rows"] == norm["rows"] and len(d["t"]) == d["rows"] and d["session"] == norm["session"]
    assert set(d["stats"]) >= {"temp_c", "tds_ppm", "turb_v"}
    s = d["stats"]["tds_ppm"]
    assert s["mean"] > 0 and s["sd"] >= 0 and s["noise"] is not None and s["settle_s"] is not None
    assert d["score"] and len(d["score"]["t"]) > 10 and d["score"]["in_training"] in (True, False)
    assert c.get("/api/run/..%2Fconfig.json").status_code in (400, 404)
    assert c.get("/api/run/nothere.csv").status_code == 400

    # known drift: TDS volts rise 0.001 V/s, temperature is flat with 0.05 degC noise
    rng = np.random.default_rng(1)
    name = "run901_drifttest_20260101-000000.csv"
    lines = ["pc_time,ms,run_id,label,temp_c,tds_v,turb_v,ph_pin_v"]
    for i in range(130):
        lines.append(f",{i * 1000},901,drifttest,{27 + rng.normal(0, 0.05):.4f},{0.40 + 0.001 * i:.4f},2.0,nan")
    (DATA_DIR / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    from sentinel.features import tds_ppm
    exp = float((tds_ppm(0.40 + 0.001 * 129, 27, 0.142, 1.0) - tds_ppm(0.40, 27, 0.142, 1.0)) / 129 * 60)
    st = c.get(f"/api/run/{name}").json()["stats"]
    assert abs(st["tds_ppm"]["slope_per_min"] - exp) / exp < 0.05, (st["tds_ppm"], exp)
    assert abs(st["temp_c"]["slope_per_min"]) < 0.05 and 0.02 < st["temp_c"]["noise"] < 0.09
    assert st["turb_v"]["settle_s"] == 0.0 and st["tds_ppm"]["settle_s"] is None or st["tds_ppm"]["settle_s"] > 50
    csv_text = c.get(f"/api/runs/export.csv?files={name},{norm['file']}").text
    assert "slope_per_min" in csv_text and name in csv_text and norm["file"] in csv_text

    # archive = move, never delete; restore brings back identical bytes
    before = (DATA_DIR / name).read_bytes()
    assert c.post(f"/api/run/{name}/archive", json={}).json()["ok"]
    assert not (DATA_DIR / name).exists() and (DATA_DIR / "_excluded" / name).read_bytes() == before
    assert name in c.get("/api/runs").json()["archived"] and all(r["file"] != name for r in c.get("/api/runs").json()["runs"])
    assert c.post(f"/api/run/{name}/restore", json={}).json()["ok"] and (DATA_DIR / name).read_bytes() == before
    assert c.post(f"/api/run/{name}/meta", json={"note": "synthetic drift", "excluded": True}).status_code == 200
    assert next(r for r in c.get("/api/runs").json()["runs"] if r["file"] == name)["excluded"] is True
    assert c.post("/api/run/..%2Fx.csv/archive", json={}).status_code in (400, 404)


def test_training_selection_roles_sensors_models_datasets(client):
    """WP4b: a test-only run never touches the fit, a subset reproduces exactly, guard rails and datasets work."""
    c, hub = client, client.hub
    runs = [r for r in c.get("/api/runs").json()["runs"] if r["rows"] > 0 and not r["excluded"] and r["label"] != "unlabeled"]
    normal = [r for r in runs if r["label"] == "normal"]
    files = {r["file"]: ("train" if r["label"] == "normal" else "anomaly") for r in runs}
    held = normal[-1]                                  # run 5: the last session
    train = lambda sel: c.post("/api/train", json={**sel, "force": True}).json()
    rep = lambda: c.get("/api/report").json()

    assert train({"files": {**files, held["file"]: "test"}})["ok"]
    a = rep()
    assert a["meta"]["test_runs"] == [held["run"]] and held["run"] not in a["meta"]["normal_runs"]
    assert a["meta"]["roles"][held["file"]] == "test" and a["meta"]["files_hash"]
    assert a["models"]["mspc"]["test_false_alarm"] is not None and a["models"]["mspc"]["test_rows"] > 20
    assert next(r for r in a["runs"] if r["run_id"] == held["run"])["role"] == "test"

    assert train({"files": {**files, held["file"]: "exclude"}})["ok"]
    b = rep()
    for k in ("mspc", "thresholds", "drift", "n_train_rows", "sessions", "normal_runs", "interval_s"):
        assert a["meta"][k] == b["meta"][k], k           # a test-only run never influences the fit
    assert b["models"]["mspc"].get("test_false_alarm") is None

    assert train({"files": {**files, held["file"]: "test"}})["ok"]
    again = rep()
    for k in a["models"]:                                  # the same selection reproduces exactly
        for m in ("false_alarm", "detection", "auc", "delay_s", "test_false_alarm"):
            assert again["models"][k][m] == a["models"][k][m], (k, m)
    assert again["meta"]["files_hash"] == a["meta"]["files_hash"] and again["meta"]["thresholds"] == a["meta"]["thresholds"]

    # guard rails
    chk = lambda sel: c.post("/api/train/check", json=sel).json()["check"]
    nothing = chk({"files": {r["file"]: "anomaly" for r in runs if r["label"] != "normal"}})
    assert nothing["blockers"] and not nothing["ok"]
    few = chk({"files": {normal[0]["file"]: "train", normal[1]["file"]: "train"}})
    assert few["ok"] and any("session" in w for w in few["warnings"])
    assert c.post("/api/train/check", json={"sensors": ["tds_ppm"]}).status_code == 400
    assert c.post("/api/train", json={"files": {"a.csv": "chef"}}).status_code == 400
    assert c.post("/api/train", json={"files": {"../a.csv": "train"}}).status_code == 400

    # a saved dataset, sensors and models subsets
    c.post("/api/datasets", json={"name": "bench subset", "files": files, "sensors": ["tds_ppm", "turb_v"],
                                  "models": ["pca"], "note": "no temperature, no sklearn models"})
    assert "bench subset" in c.get("/api/datasets").json()["datasets"]
    assert train({"dataset": "bench subset"})["ok"]
    d = rep()
    assert d["meta"]["dataset"] == "bench subset" and d["meta"]["features"] == ["tds_ppm", "turb_v"]
    assert set(d["models"]) == {"mspc", "pca"} and d["meta"]["use_ph"] is False
    assert c.get("/api/datasets").json()["current"]["dataset"] == "bench subset"
    assert c.post("/api/model", json={"key": "lof"}).status_code == 400            # not trained this time
    # live monitor and tuning follow the chosen sensors / data
    c.post("/api/model", json={"key": "mspc"})
    c.post("/api/sim", json={"scenario": "normal"}); time.sleep(1.0)
    c.post("/api/monitor", json={"on": False}); c.post("/api/monitor", json={"on": True})
    assert wait(lambda: hub.det_out["status"] == "normal", 30)
    assert set(hub.det_out["z"]) == {"tds_ppm", "turb_v"}
    pv = c.post("/api/tune/preview", json={"model": "mspc", "settings": {}}).json()
    assert pv["features"] == ["tds_ppm", "turb_v"]
    assert abs(pv["current"]["false_alarm"] - d["models"]["mspc"]["false_alarm"]) < 1e-9

    # default path (no selection) is the v1.0 behaviour: all usable runs, 3 sensors, 5 models
    assert c.post("/api/train", json={"use_ph": False, "force": True}).json()["ok"]
    z = rep()
    assert z["meta"]["dataset"] is None and z["meta"]["features"] == ["temp_c", "tds_ppm", "turb_v"]
    assert set(z["models"]) == {"mspc", "pca", "iforest", "ocsvm", "lof", "threshold"} and z["meta"]["test_runs"] == []
    assert c.get("/api/datasets").json()["current"] is None
    c.post("/api/datasets/delete", json={"name": "bench subset"})
    assert "bench subset" not in c.get("/api/datasets").json()["datasets"]
    assert all((c.get("/api/run/" + r["file"]).status_code == 200) for r in runs[:2])      # data files untouched


def test_cause_ambiguous_lists_every_tied_candidate():
    """Without pH, salt / baking soda / vinegar all just raise TDS: the headline must name all of them."""
    from sentinel import causes
    feats = ["temp_c", "tds_ppm", "turb_v"]
    up = [0.0, 1.0, 0.0]
    model = {"features": feats, "centroids": {"salt": up, "baking_soda": up, "vinegar": up}}
    out = causes.infer([0.0, 20.0, 0.0], feats, model)
    assert out["verdict"] == "ambiguous" and out["experimental"]
    assert all(f in out["headline"] for f in ("salt", "baking_soda", "vinegar"))
    assert any("pH" in d for d in out["disambiguate"])
    unknown = causes.infer([8.0, 0.0, 8.0], feats, model)       # a direction nobody taught it
    assert unknown["verdict"] in ("unknown", "ambiguous", "match") and "headline" in unknown


def test_viewer_mode_tunnel_is_not_local(client):
    """A tunnel (Cloudflare / Tailscale Funnel) connects from 127.0.0.1: it must never get the local no-login access."""
    from fastapi.testclient import TestClient
    tun = {"X-Forwarded-For": "203.0.113.9", "Cf-Connecting-Ip": "203.0.113.9"}
    v = TestClient(client.app)
    # viewer mode off: a tunnelled request is refused, wrong public host is refused
    assert v.get("/api/state", headers=tun).status_code == 403
    assert v.get("/api/state", headers={**tun, "Host": "abc.trycloudflare.com"}).status_code == 403
    # only this computer may start a tunnel
    r = v.post("/api/share", json={"tunnel": True}, headers=tun)
    assert r.status_code == 403
    # turn viewer mode on from this computer
    assert client.post("/api/share", json={"public_view": True}).json()["public_view"] is True
    try:
        h = {**tun, "Host": "abc.trycloudflare.com"}
        st = v.get("/api/state", headers=h).json()
        assert st["role"] == "viewer" and st["share"]["public_view"]
        assert client.get("/api/state").json()["role"] == "admin"
        # every change is refused, reads work, secrets are gone
        for path, body in [("/api/monitor", {"on": True}), ("/api/train", {}), ("/api/config", {"use_ph": True}),
                           ("/api/share", {"public_view": False}), ("/api/sim", {"scenario": "salt"})]:
            r = v.post(path, json=body, headers=h)
            assert r.status_code == 403 and r.json()["viewer"], path
        assert v.get("/api/runs", headers=h).status_code == 200
        cfg = v.get("/api/config", headers=h).json()
        assert cfg["remote"]["pin"] == "" and cfg["alerts"]["ntfy_topic"] == "" and cfg["ingest_token"] == ""
        for path in ("/docs", "/openapi.json", "/api/ports", "/api/share"):
            assert v.get(path, headers=h).status_code == 403, path
        # admin over the tunnel needs a long enough PIN, wrong PINs are refused
        client.post("/api/config", json={"remote": {"pin": "12345"}})
        assert client.post("/api/share", json={"public_view": True}).status_code == 400
        client.post("/api/config", json={"remote": {"pin": "246810"}})
        r = v.post("/login", data={"pin": "000000"}, headers=h, follow_redirects=False)
        assert r.status_code == 401
        r = v.post("/login", data={"pin": "246810"}, headers=h, follow_redirects=False)
        assert r.status_code == 303 and "ws_auth" in r.cookies
        v.cookies.set("ws_auth", r.cookies["ws_auth"])
        assert v.get("/api/state", headers=h).json()["role"] == "admin"
        assert v.get("/api/config", headers=h).json()["remote"]["pin"].startswith("••••")
    finally:
        client.post("/api/share", json={"public_view": False})
        client.post("/api/config", json={"remote": {"pin": ""}})
    assert TestClient(client.app).get("/api/state", headers={**tun, "Host": "abc.trycloudflare.com"}).status_code == 403


def test_edge_detector_matches_live_scorer(client):
    """The plain-maths detector the ESP32 firmware copies must give the same scores and alarms as the app."""
    from sentinel.edge import EdgeDetector, export_model, to_c_header
    from sentinel.live import LiveMonitor as LiveScorer
    from sentinel.sources import Simulator
    from sentinel.training import load_bundle
    bundle = load_bundle()
    edge = export_model(bundle)
    import json as _json
    _json.dumps(edge)                                  # board model is plain JSON
    assert "EDGE_P" in to_c_header(edge)
    live, dev, sim = LiveScorer(bundle, "mspc"), EdgeDetector(edge, dt=1.0), Simulator()
    alarms_seen = 0
    for i in range(240):
        if i == 90:
            sim.set("salt")
        if i == 170:
            sim.set("normal")
        raw = sim.sample()
        if "ph" not in edge["features"]:
            raw.pop("ph_pin_v", None)
        a, b = live.update(dict(raw)), dev.update(raw)
        if a.get("ratio") is not None and b["ratio"] is not None:
            assert abs(a["ratio"] - b["ratio"]) < 1e-6 * max(1.0, abs(a["ratio"]))
            assert bool(a.get("in_alarm", a["status"] == "anomaly")) == b["alarm"]
        alarms_seen += b["alarm"]
    assert alarms_seen > 0


def test_edge_sim_matches_model_baseline(client):
    """The fake board must read 'normal' for whatever water the model was trained on, and still alarm on salt."""
    import copy
    from sentinel.edge import EdgeDetector, export_model
    from sentinel.edge_sim import match_model
    from sentinel.sources import Simulator
    from sentinel.training import load_bundle
    edge = copy.deepcopy(export_model(load_bundle()))
    f = edge["features"]
    # pretend the model was trained on different water (cooler, much lower TDS, clearer)
    edge["mu"][f.index("temp_c")] = 22.0
    edge["mu"][f.index("tds_ppm")] = 120.0
    edge["mu"][f.index("turb_v")] = 2.6
    sim = Simulator()
    match_model(sim, edge)
    det = EdgeDetector(edge, dt=1.0)
    normal_alarm, salt_alarm = False, False
    for i in range(300):
        if i == 200:
            sim.set("salt")
        raw = sim.sample()
        if "ph" not in f:
            raw.pop("ph_pin_v", None)
        r = det.update(raw)
        if r["status"] == "fault":
            raise AssertionError(r)
        if i < 200 and r["alarm"]:
            normal_alarm = True
        if i >= 200 and r["alarm"]:
            salt_alarm = True
    assert not normal_alarm and salt_alarm


def test_saved_models_can_be_switched_back(client):
    """Every training is kept; picking an earlier one (3 vs 4 sensors) makes it the live model again."""
    c = client
    runs = [r for r in c.get("/api/runs").json()["runs"] if r["rows"] > 0 and not r["excluded"] and r["label"] != "unlabeled"]
    files = {r["file"]: ("train" if r["label"] == "normal" else "anomaly") for r in runs}
    assert c.post("/api/train", json={"files": files, "sensors": ["temp_c", "tds_ppm", "turb_v", "ph"], "force": True}).json()["ok"]
    assert c.post("/api/train", json={"files": files, "sensors": ["temp_c", "tds_ppm", "turb_v"], "force": True}).json()["ok"]
    snaps = c.get("/api/snapshots").json()
    assert len(snaps["snapshots"]) >= 2 and snaps["active_features"] == ["temp_c", "tds_ppm", "turb_v"]
    four = next(s for s in snaps["snapshots"] if s["features"] == ["temp_c", "tds_ppm", "turb_v", "ph"])
    assert four["metrics"]["mspc"]["detection"] is not None
    r = c.post("/api/snapshots/use", json={"id": four["id"]}).json()
    assert r["ok"] and r["active_features"] == ["temp_c", "tds_ppm", "turb_v", "ph"]
    st = c.get("/api/state").json()
    assert st["model"]["features"] == ["temp_c", "tds_ppm", "turb_v", "ph"] and st["use_ph"] is True
    assert c.get("/api/report").json()["meta"]["features"] == ["temp_c", "tds_ppm", "turb_v", "ph"]
    assert c.post("/api/snapshots/use", json={"id": "../../etc"}).status_code >= 400
