"""Command line tools (no web page needed).

  py -m sentinel train          train + compare all models (3 or 4 sensors from config.json)
  py -m sentinel train --ph     force 4 sensors (with pH)
  py -m sentinel train --dataset NAME   train on a saved dataset (data/datasets.json)
  py -m sentinel test FILE.csv  score one recorded run with every model
  py -m sentinel export-edge    write model/edge_model.json + model/edge_model.h for the ESP32
  py -m sentinel edge-sim       pretend to be the ESP32: simulated sensors -> MSPC -> ThingSpeak/ntfy
                                (--script normal:120,salt:90  --dry-run ; see sentinel/edge_sim.py)
"""
import pathlib
import sys

import numpy as np

from .config import deep_merge, load_config
from .datasets import dataset_selection
from .features import prepare_run, read_run
from .models import MODEL_NAMES, alarms, normalized
from .training import TrainError, _pct, explain, load_bundle, train


def main(args):
    cfg = load_config()
    if args and args[0] == "edge-sim":
        from . import edge_sim
        return edge_sim.main(args[1:])
    if args and args[0] == "export-edge":
        from .config import MODEL_DIR
        from .edge import export_model, save_model, to_c_header
        bundle = load_bundle()
        if not bundle:
            print("No trained model yet.")
            return 1
        edge = export_model(bundle)
        p = save_model(edge)
        (MODEL_DIR / "edge_model.h").write_text(to_c_header(edge), encoding="utf-8")
        print(f"wrote {p} and {MODEL_DIR / 'edge_model.h'}")
        return 0
    if not args or args[0] not in ("train", "test"):
        print(__doc__)
        return 0
    if args[0] == "train":
        try:
            sel = None
            if "--dataset" in args:
                i = args.index("--dataset")
                if i + 1 >= len(args):
                    print("ระบุชื่อชุดข้อมูล: py -m sentinel train --dataset NAME")
                    return 1
                sel = dataset_selection(args[i + 1])
            report = train(cfg, use_ph=True if "--ph" in args else None, selection=sel)
        except (TrainError, ValueError) as e:
            print(f"เทรนไม่สำเร็จ: {e}")
            return 1
        print("\nสรุป (false alarm ยิ่งต่ำยิ่งดี, detection/AUC ยิ่งสูงยิ่งดี):")
        for r in report["models"].values():
            star = "  <- แนะนำ" if r["key"] == report["meta"]["best"] else ""
            delay = "-" if r["delay_s"] is None else f"{r['delay_s']:.0f}"
            test = "" if r.get("test_false_alarm") is None else f"  test-only FA {_pct(r['test_false_alarm'])}"
            print(f"  {r['name']:34s} FA {_pct(r['false_alarm'])}  DET {_pct(r['detection'])}  delay {delay} s{test}{star}")
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
    clean, faults, dt = prepare_run(read_run(path), tcfg, meta["use_ph"], meta["calib"], meta["ph_divider"], warmup=False)
    if clean.empty:
        print(f"ไม่มีข้อมูลที่ใช้ได้ (ค่าเซนเซอร์ผิดพลาด: {faults})")
        return 1
    X = clean[meta["features"]].to_numpy(float)
    t = clean["t_s"].to_numpy()
    print(f"{path.name}: {len(X)} แถว, ทุก {dt:.2f} s, ค่าเซนเซอร์ผิดพลาด {sum(faults.values())} แถว {faults or ''}")
    for k, bm in bundle["models"].items():
        a = alarms(normalized(bm["model"], X, bm["thr"], bm["med"], det, dt), det, dt)
        first = f"{t[np.argmax(a)]:.0f} s" if a.any() else "-"
        print(f"  {MODEL_NAMES[k]:34s} เตือน {a.mean() * 100:5.1f}%  เตือนครั้งแรกที่ {first}")
    mspc = bundle["models"]["mspc"]["model"]
    for c in explain(np.median((X - mspc.mu) / mspc.sd, axis=0), meta["features"]):
        print(f"  สาเหตุที่น่าจะเป็น: {c['text']} (z = {c['z']})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
