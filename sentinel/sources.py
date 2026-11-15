"""Where readings come from.

  serial  ESP32 over USB (this laptop). Auto-detects CP210x / CH340 boards, reconnects on unplug.
  sim     built-in simulator, for demos and testing without hardware (port = "SIM").
  wifi    an ESP32 with WiFi can POST readings to /api/ingest (see server.py); no thread needed here.

Every source hands the hub the same raw dict:
  {"ms": float, "temp_c": float, "tds_v": float, "turb_v": float, "ph_pin_v": float}
"""
import random
import threading
import time

USB_CHIPS = {(0x10C4, 0xEA60): "CP210x", (0x1A86, 0x7523): "CH340",
             (0x1A86, 0x55D4): "CH9102", (0x0403, 0x6001): "FTDI"}


def _num(s):
    s = s.strip().lower()
    return float("nan") if s in ("", "nan") else float(s)


def parse_data_line(line):
    """New firmware:  DATA,ms,temp_c,tds_v,turb_v,ph_pin_v
    Old firmware:     DATA,ms,run_id,label,temp_c,tds_v,tds_ppm,turb_v   (still accepted)"""
    if not line.startswith("DATA,"):
        return None
    p = line.strip().split(",")[1:]
    try:
        if len(p) == 5:
            ms, t, tds, turb, ph = p
        elif len(p) == 7:
            ms, _run, _label, t, tds, _ppm, turb = p
            ph = "nan"
        else:
            return None
        return {"ms": float(ms), "temp_c": _num(t), "tds_v": _num(tds),
                "turb_v": _num(turb), "ph_pin_v": _num(ph)}
    except ValueError:
        return None


def list_ports():
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        return []
    out = []
    for p in lp.comports():
        chip = USB_CHIPS.get((p.vid, p.pid)) if p.vid is not None else None
        out.append({"device": p.device, "desc": p.description or "", "chip": chip})
    return out


def resolve_port(pref):
    if pref and pref != "auto":
        return pref
    for p in list_ports():
        if p["chip"]:          # only USB-serial chips used on ESP32 boards, never Bluetooth COM ports
            return p["device"]
    return None


class Simulator:
    """Fake ESP32: steady water with realistic noise; scenarios mix in over ~10 s like a real dip."""

    SCENARIOS = {
        "normal": ("น้ำปกติ", {}),
        "salt": ("เติมเกลือ", {"tds_v": 0.30}),
        "baking_soda": ("เติมเบกกิ้งโซดา", {"tds_v": 0.12, "ph_pin_v": -0.05}),
        "starch": ("เติมแป้ง (ขุ่น)", {"turb_v": -0.45, "tds_v": 0.01}),
        "vinegar": ("เติมน้ำส้มสายชู", {"ph_pin_v": 0.16, "tds_v": 0.04}),
        "hot_water": ("น้ำร้อน", {"temp_c": 15.0}),
        "probe_out": ("ยกหัววัดขึ้นจากน้ำ", {}),
    }

    def __init__(self):
        self.ms = 0
        self.base = {"temp_c": 27.2, "tds_v": 0.40, "turb_v": 2.00, "ph_pin_v": 1.40}
        self.off = {k: 0.0 for k in self.base}
        self.name = "normal"

    def set(self, name):
        if name not in self.SCENARIOS:
            raise ValueError(f"ไม่รู้จักสถานการณ์ {name}")
        self.name = name

    def sample(self):
        self.ms += 1000
        target = self.SCENARIOS[self.name][1]
        for k in self.off:
            self.off[k] += (target.get(k, 0.0) - self.off[k]) * 0.25
        g = random.gauss
        temp = self.base["temp_c"] + self.off["temp_c"] + g(0, 0.03)
        tds = self.base["tds_v"] + self.off["tds_v"] + 0.0008 * self.off["temp_c"] + g(0, 0.003)
        if random.random() < 0.01:
            tds += random.choice([-1, 1]) * 0.05          # occasional bubble spike
        if self.name == "probe_out":
            tds = 0.142
        return {"ms": float(self.ms), "temp_c": round(temp / 0.0625) * 0.0625,
                "tds_v": round(tds, 3),
                "turb_v": round(self.base["turb_v"] + self.off["turb_v"] + g(0, 0.004), 3),
                "ph_pin_v": round(self.base["ph_pin_v"] + self.off["ph_pin_v"] + g(0, 0.003), 3)}


class SourceThread(threading.Thread):
    def __init__(self, hub):
        super().__init__(daemon=True, name="sensor-source")
        self.hub = hub
        self.sim = Simulator()
        self.stopping = False

    def run(self):
        while not self.stopping:
            pref = self.hub.port_pref()
            if pref == "SIM":
                self._run_sim()
                continue
            port = resolve_port(pref)
            if not port:
                self.hub.set_link(False, "ไม่พบบอร์ด ESP32: เสียบสาย USB หรือเลือกพอร์ตในหน้าตั้งค่า", None)
                time.sleep(2)
                continue
            self._run_serial(port)

    def _run_sim(self):
        gen = self.hub.gen
        self.hub.set_link(True, "โหมดจำลอง (ไม่ได้ใช้บอร์ดจริง)", "SIM")
        while not self.stopping and gen == self.hub.gen:
            self.hub.on_sample(self.sim.sample(), "sim")
            time.sleep(1.0 / max(0.1, self.hub.sim_speed))

    def _run_serial(self, port):
        gen = self.hub.gen
        try:
            import serial
        except ImportError:
            self.hub.set_link(False, "ยังไม่ได้ติดตั้ง pyserial: py -m pip install pyserial", port)
            time.sleep(5)
            return
        try:
            with serial.Serial(port, self.hub.cfg["baud"], timeout=1) as ser:
                self.hub.set_link(True, f"เชื่อมต่อ {port} แล้ว กำลังรอข้อมูลจากบอร์ด...", port)
                last = time.time()
                while not self.stopping and gen == self.hub.gen:
                    raw = parse_data_line(ser.readline().decode(errors="replace").strip())
                    if raw:
                        last = time.time()
                        self.hub.on_sample(raw, "serial")
                    elif time.time() - last > 8:
                        self.hub.set_link(True, f"ต่อ {port} ได้ แต่บอร์ดไม่ส่งข้อมูล: อัปโหลด firmware แล้วหรือยัง?", port)
                        last = time.time()
        except Exception as e:  # port busy, unplugged, permission
            self.hub.set_link(False, f"ใช้ {port} ไม่ได้ ({type(e).__name__}): ปิด Arduino Serial Monitor "
                                     "หรือโปรแกรมอื่นที่ใช้พอร์ตนี้ แล้วระบบจะต่อใหม่เอง", port)
            time.sleep(2)
