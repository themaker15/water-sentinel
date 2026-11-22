"""Pretend to be the ESP32 (no board needed): simulated sensors -> on-device MSPC -> ThingSpeak + ntfy.

    py -m sentinel edge-sim                       run, scenario "normal"; type a scenario name + Enter to switch
    py -m sentinel edge-sim --script normal:120,salt:90,normal:120     run a timed script (loops)
    py -m sentinel edge-sim --dry-run             print what would be sent, send nothing
    py -m sentinel edge-sim --raw                 use the built-in fake water instead of matching the trained model

Keys live in edge_secrets.json next to config.json (never committed):
    {"thingspeak_write_key": "...", "ntfy_topic": "water-sentinel-xxxx", "ntfy_server": "https://ntfy.sh"}

ThingSpeak channel fields (create them in this order):
    1 temperature (C)  2 TDS (ppm)  3 turbidity (V)  4 pH  5 score (1 = alarm line)  6 status (0 start, 1 normal,
    2 anomaly, 3 sensor fault)  7 driver sensor (1 temp, 2 TDS, 3 turbidity, 4 pH)  8 driver z
"""
import json
import sys
import threading
import time
import urllib.parse
import urllib.request

from ..config import HOME, MODEL_DIR
from .export import EDGE_FILE, STATUS_CODE, EdgeDetector, export_model, save_model, tds_ppm
from ..acquisition.sources import Simulator

SECRETS = HOME / "edge_secrets.json"
TS_URL = "https://api.thingspeak.com/update"
TS_MIN_S = 15.5            # free ThingSpeak accepts one update per 15 s
DRIVER_CODE = {"temp_c": 1, "tds_ppm": 2, "turb_v": 3, "ph": 4}


def load_secrets():
    try:
        return json.loads(SECRETS.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def load_edge():
    """Always export from the current trained model, so the simulator never runs a stale copy."""
    from ..detection.training import load_bundle
    bundle = load_bundle()
    if not bundle:
        p = MODEL_DIR / EDGE_FILE
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        raise SystemExit("No trained model yet: train in the app first, then run this again.")
    edge = export_model(bundle)
    save_model(edge)
    return edge


def match_model(sim, edge):
    """Centre the fake water on what the model was trained on (your tap water), so "normal" really is normal.
    Scenarios (salt, starch, ...) are volt offsets on top of this baseline, so they still push the model off."""
    f, mu, c = edge["features"], dict(zip(edge["features"], edge["mu"])), edge["calib"]
    temp = mu.get("temp_c", sim.base["temp_c"])
    sim.base["temp_c"] = temp
    if "turb_v" in mu:
        sim.base["turb_v"] = mu["turb_v"]
    if "tds_ppm" in mu:
        lo, hi = c["tds_offset_v"], c["tds_offset_v"] + 3.3     # tds_ppm rises with voltage: bisect
        for _ in range(60):
            mid = (lo + hi) / 2
            if tds_ppm(mid, temp, c["tds_offset_v"], c["tds_k"]) < mu["tds_ppm"]:
                lo = mid
            else:
                hi = mid
        sim.base["tds_v"] = round((lo + hi) / 2, 4)
    if "ph" in mu:
        (p1, v1), (p2, v2) = c["ph_points"]
        sim.base["ph_pin_v"] = round(((mu["ph"] - p1) * (v2 - v1) / (p2 - p1) + v1) / edge["ph_divider"], 4)
    return sim.base


def _post(url, data=None, headers=None, body=None):
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=headers or {}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode("utf-8", "replace").strip()


class Uplink:
    """Collects one window of results and sends it every TS_MIN_S seconds (worst status, highest score)."""

    def __init__(self, secrets, dry):
        self.key = secrets.get("thingspeak_write_key", "")
        self.topic = secrets.get("ntfy_topic", "")
        self.ntfy = secrets.get("ntfy_server", "https://ntfy.sh").rstrip("/")
        self.dry = dry or not self.key
        self.win, self.last = [], 0.0

    def add(self, r):
        self.win.append(r)
        if time.time() - self.last >= TS_MIN_S:
            self.flush()

    def flush(self):
        rs, self.win, self.last = self.win, [], time.time()
        scored = [r for r in rs if r.get("ratio") is not None]
        if not rs:
            return
        last = scored[-1] if scored else rs[-1]
        worst = max(rs, key=lambda r: STATUS_CODE[r["status"]] if r["status"] != "starting" else -1)
        peak = max(scored, key=lambda r: r["ratio"]) if scored else None
        med = last.get("median") or {}
        vals = last.get("values") or {}
        fields = {"field1": med.get("temp_c", vals.get("temp_c")), "field2": med.get("tds_ppm", vals.get("tds_ppm")),
                  "field3": med.get("turb_v", vals.get("turb_v")), "field4": med.get("ph", vals.get("ph")),
                  "field5": None if peak is None else round(peak["ratio"], 3),
                  "field6": STATUS_CODE[worst["status"]],
                  "field7": DRIVER_CODE.get(peak["driver"]) if peak else None,
                  "field8": None if peak is None else round(peak["driver_z"], 2)}
        fields = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in fields.items() if v is not None}
        if self.dry:
            print(f"  [ThingSpeak dry-run] {fields}")
            return
        try:
            entry = _post(TS_URL, {"api_key": self.key, **fields})
            print(f"  [ThingSpeak] sent entry #{entry}" if entry != "0" else "  [ThingSpeak] rejected (too fast or wrong key)")
        except Exception as e:      # network down: the board keeps detecting, just no upload this time
            print(f"  [ThingSpeak] failed: {e}")

    def notify(self, title, text, urgent):
        if not self.topic:
            print(f"  [ntfy not set] {title}: {text}")
            return
        if self.dry:
            print(f"  [ntfy dry-run] {title}: {text}")
            return
        try:
            _post(f"{self.ntfy}/{urllib.parse.quote(self.topic)}", body=text.encode("utf-8"),
                  headers={"Title": title,
                           "Priority": "urgent" if urgent else "default",
                           "Tags": "rotating_light" if urgent else "white_check_mark"})
            print(f"  [ntfy] {title}")
        except Exception as e:
            print(f"  [ntfy] failed: {e}")


def parse_script(s):
    out = []
    for part in s.split(","):
        name, _, sec = part.partition(":")
        out.append((name.strip(), float(sec or 60)))
    return out


def main(args):
    dry = "--dry-run" in args
    script = parse_script(args[args.index("--script") + 1]) if "--script" in args else None
    edge = load_edge()
    secrets = load_secrets()
    up = Uplink(secrets, dry)
    det = EdgeDetector(edge, dt=1.0)
    sim = Simulator()
    if "--raw" not in args:
        b = match_model(sim, edge)
        print("Fake water matched to the model: " + ", ".join(f"{k} {v}" for k, v in b.items() if k != "ph_pin_v" or "ph" in edge["features"]))
    names = {"temp_c": "temperature", "tds_ppm": "TDS", "turb_v": "turbidity", "ph": "pH"}
    print(f"Edge simulator: MSPC on {', '.join(names[f] for f in edge['features'])} · model {edge.get('created')}")
    print("ThingSpeak:", "DRY-RUN (nothing sent)" if up.dry else "sending every 15 s",
          "· ntfy:", up.topic or "not set")
    print(f"Scenarios: {', '.join(Simulator.SCENARIOS)}  (type one + Enter to switch, Ctrl+C to stop)\n")

    if not script:
        def read_input():
            for line in sys.stdin:
                name = line.strip()
                if name in Simulator.SCENARIOS:
                    sim.set(name)
                    print(f"  -> scenario {name}")
                elif name:
                    print(f"  unknown scenario '{name}'")
        threading.Thread(target=read_input, daemon=True).start()

    step, step_left = 0, (script[0][1] if script else None)
    if script:
        sim.set(script[0][0])
    was = was_print = False
    n = 0
    try:
        while True:
            t0 = time.time()
            if script:
                step_left -= 1
                if step_left <= 0:
                    step = (step + 1) % len(script)
                    sim.set(script[step][0])
                    step_left = script[step][1]
                    print(f"  -> scenario {script[step][0]}")
            raw = sim.sample()
            if "ph" not in edge["features"]:
                raw.pop("ph_pin_v", None)
            r = det.update(raw)
            up.add(r)
            if r["alarm"] and not was:
                drv = names.get(r.get("driver"), "?")
                up.notify("Water Sentinel: anomaly", f"Abnormal water detected by the board ({drv} moved most, "
                          f"score {r['ratio']:.2f}).", urgent=True)
            elif was and not r["alarm"]:
                up.notify("Water Sentinel: back to normal", "Water looks normal again.", urgent=False)
            was = r["alarm"]
            n += 1
            if n % 5 == 0 or r["alarm"] != was_print:
                score = "  -  " if r["ratio"] is None else f"{r['ratio']:5.2f}"
                print(f"{time.strftime('%H:%M:%S')}  {sim.name:<12} {r['status']:<8} score {score}")
                was_print = r["alarm"]
            time.sleep(max(0.0, 1.0 - (time.time() - t0)))
    except KeyboardInterrupt:
        print("\nstopped")
        up.flush()
    return 0
