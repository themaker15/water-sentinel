"""Command line tools (no web page needed).

  py -m sentinel train          train + compare all models (3 or 4 sensors from config.json)
  py -m sentinel train --ph     force 4 sensors (with pH)
  py -m sentinel test FILE.csv  score one recorded run with every model
"""
import pathlib
import sys

import numpy as np

from .config import deep_merge, load_config
from .features import prepare_run, read_run
from .models import MODEL_NAMES, alarm_states, normalized
from .training import TrainError, _pct, explain, load_bundle, train


def main(args):
    cfg = load_config()
    if not args or args[0] not in ("train", "test"):
        print(__doc__)
        return 0
    if args[0] == "train":
        try:
            report = train(cfg, use_ph=True if "--ph" in args else None)
        except TrainError as e:
            print(f"เทรนไม่สำเร็จ: {e}")
            return 1
        print("\nสรุป (false alarm ยิ่งต่ำยิ่งดี, detection/AUC ยิ่งสูงยิ่งดี):")
        for r in report["models"].values():
            star = "  <- แนะนำ" if r["key"] == report["meta"]["best"] else ""
            delay = "-" if r["delay_s"] is None else f"{r['delay_s']:.0f}"
            print(f"  {r['name']:34s} FA {_pct(r['false_alarm'])}  DET {_pct(r['detection'])}  delay {delay} s{star}")
        return 0
    if len(args) < 2:
        print("ระบุไฟล์: py -m sentinel test data\\runXXX_label_....csv")
        return 1
    bundle = load_bundle()
    if bundle is None:
        print("ยังไม่มีโมเดล รัน  py -m sentinel train  ก่อน")
        return 1
    meta = bundle["meta"]
    det = meta["detector"]
    path = pathlib.Path(args[1])
    tcfg = deep_merge(cfg, {"detector": det, "limits": meta["limits"]})
    clean, faults = prepare_run(read_run(path), tcfg, meta["use_ph"], meta["calib"], meta["ph_divider"], warmup=False)
    if clean.empty:
        print(f"ไม่มีข้อมูลที่ใช้ได้ (ค่าเซนเซอร์ผิดพลาด: {faults})")
        return 1
    X = clean[meta["features"]].to_numpy(float)
    t = clean["t_s"].to_numpy()
    print(f"{path.name}: {len(X)} แถว, ค่าเซนเซอร์ผิดพลาด {sum(faults.values())} แถว {faults or ''}")
    for k, bm in bundle["models"].items():
        a = alarm_states(normalized(bm["model"], X, bm["thr"], bm["med"], det), 1.0, det["persist"], det["release"])
        first = f"{t[np.argmax(a)]:.0f} s" if a.any() else "-"
        print(f"  {MODEL_NAMES[k]:34s} เตือน {a.mean() * 100:5.1f}%  เตือนครั้งแรกที่ {first}")
    mspc = bundle["models"]["mspc"]["model"]
    for c in explain(np.median((X - mspc.mu) / mspc.sd, axis=0), meta["features"]):
        print(f"  สาเหตุที่น่าจะเป็น: {c['text']} (z = {c['z']})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
