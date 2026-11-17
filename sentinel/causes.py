"""EXPERIMENTAL: which kind of change is consistent with an alarm.

Not a chemical identification. It looks at the *direction* of the change across sensors and says which
class of cause is consistent with it ("consistent with", never "is"). Nothing here says anything about
safety. Three sensors cannot separate everything: plain salt and baking soda both just raise TDS.

Fingerprint = median z-vector of the alarm window (z in units of each sensor's floor), turned into a unit
vector, so only the direction counts. Two layers share one scoring rule (cosine similarity):
  rules    prototype directions written from physical reasoning; work with zero labelled anomalies
  learned  nearest centroid of the user's own labelled anomaly runs, one centroid per label family
Open set: best similarity < OPEN_SET or the top two within AMBIGUOUS -> "unknown" / "ambiguous".
"""
import re

import numpy as np

from .features import SENSORS

OPEN_SET = 0.80       # below this the pattern matches nothing we know
AMBIGUOUS = 0.05      # top two closer than this -> cannot tell them apart
MIN_NORM = 2.5        # |z| (floor units) below this is too small to interpret
ACTIVE_Z = 3.0        # a sensor counts as "moved" beyond this

# direction of each cause class in feature space (z sign: turbidity VOLTAGE falls when water gets cloudier)
PROTOS = {
    "dissolved_salts": {"name": "สารละลาย/เกลือ (ไอออนละลายน้ำเพิ่มขึ้น)", "w": {"tds_ppm": 1.0}, "needs": ["tds_ppm"]},
    "alkaline_salt": {"name": "เกลือที่เป็นด่าง (เช่น เบกกิ้งโซดา)", "w": {"tds_ppm": 1.0, "ph": 1.0}, "needs": ["tds_ppm", "ph"]},
    "acid": {"name": "กรด (เช่น น้ำส้มสายชู)", "w": {"ph": -1.0, "tds_ppm": 0.3}, "needs": ["ph"]},
    "suspended_solids": {"name": "ของแข็งแขวนลอย/ความขุ่น (เช่น แป้ง ดิน)", "w": {"turb_v": -1.0, "tds_ppm": 0.1}, "needs": ["turb_v"]},
    "warmer": {"name": "น้ำอุ่นขึ้น/น้ำร้อน", "w": {"temp_c": 1.0, "tds_ppm": 0.15}, "needs": ["temp_c"]},
    "cooler": {"name": "น้ำเย็นลง", "w": {"temp_c": -1.0, "tds_ppm": -0.15}, "needs": ["temp_c"]},
}
PROBE = {"name": "หัววัดถูกรบกวน (ยก/ขยับ/มีฟองอากาศ)"}

# the user's own labels -> the cause class they most likely mean (used only to score the rules honestly)
LABEL_HINTS = {"salt": "dissolved_salts", "nacl": "dissolved_salts", "baking_soda": "alkaline_salt",
               "soda": "alkaline_salt", "vinegar": "acid", "acid": "acid", "starch": "suspended_solids",
               "milk": "suspended_solids", "kaolin": "suspended_solids", "soil": "suspended_solids",
               "hot_water": "warmer", "hot": "warmer", "cold_water": "cooler", "ice": "cooler"}


PROTOCOL_SUFFIX = re.compile(r"(?:[_\-]?(?:lvl|level)\d*|[_\-](?:ramp|pulse|step|drip|slow|gradual|test|\d+))+$")


def family(label):
    """The substance, not the protocol: salt_lvl2 / salt_ramp -> salt, Baking-Soda_lvl1 -> baking_soda"""
    s = PROTOCOL_SUFFIX.sub("", str(label).strip().lower())
    return re.sub(r"[\s\-]+", "_", s) or "unknown"


def unit(v):
    v = np.asarray(v, float)
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-12 else v * 0.0


def _cos(a, b):
    return float(np.dot(unit(a), unit(b)))


def fingerprint(z_rows):
    """Median z over the alarm window (rows x sensors) -> one vector."""
    return np.median(np.atleast_2d(np.asarray(z_rows, float)), axis=0)


def drivers(z, feats, top=2):
    order = np.argsort(-np.abs(z))[:top]
    out = []
    for i in order:
        if abs(z[i]) < 1.5:
            continue
        info = SENSORS[feats[i]]
        out.append({"sensor": feats[i], "name": info["name"], "z": round(float(z[i]), 1),
                    "text": info["up"] if z[i] > 0 else info["down"]})
    return out


def rule_candidates(z, feats):
    """-> (candidates sorted best first, notes). Prototypes that need a missing sensor are skipped."""
    z = np.asarray(z, float)
    cands, missing = [], set()
    for key, p in PROTOS.items():
        if any(n not in feats for n in p["needs"]):
            missing.update(n for n in p["needs"] if n not in feats)
            continue
        vec = np.array([p["w"].get(f, 0.0) for f in feats])
        cands.append({"family": key, "name": p["name"], "similarity": round(_cos(z, vec), 3), "source": "rule"})
    active = [f for f, v in zip(feats, z) if abs(v) >= ACTIVE_Z]
    share = np.abs(z) / max(np.linalg.norm(z), 1e-12)
    if len(active) >= 3 and share.max() < 0.6:
        cands.append({"family": "probe_disturbed", "name": PROBE["name"], "similarity": 0.85, "source": "rule"})
    cands.sort(key=lambda c: -c["similarity"])
    return cands, sorted(missing)


def learned_candidates(z, centroids):
    out = [{"family": f, "name": f, "similarity": round(_cos(z, np.asarray(c, float)), 3), "source": "learned"}
           for f, c in (centroids or {}).items()]
    out.sort(key=lambda c: -c["similarity"])
    return out


def _verdict(cands):
    if not cands or cands[0]["similarity"] < OPEN_SET:
        return "unknown"
    if len(cands) > 1 and cands[0]["similarity"] - cands[1]["similarity"] < AMBIGUOUS \
            and cands[1]["similarity"] >= OPEN_SET:
        return "ambiguous"
    return "match"


def infer(z, feats, cause_model=None, recent_fault=False):
    """One alarm fingerprint -> {"verdict", "headline", "candidates"(top 3), "drivers", "notes", "disambiguate"}.

    cause_model: {"features": [...], "centroids": {family: unit vector}} from training, or None.
    """
    z = np.asarray(z, float)
    out = {"experimental": True, "fingerprint": {f: round(float(v), 1) for f, v in zip(feats, z)},
           "drivers": drivers(z, feats), "notes": [], "disambiguate": [], "candidates": []}
    if float(np.linalg.norm(z)) < MIN_NORM:
        out.update(verdict="too_small", headline="การเปลี่ยนแปลงยังเล็กเกินกว่าจะบอกชนิดได้")
        return out
    rules, missing = rule_candidates(z, feats)
    learned = learned_candidates(z, (cause_model or {}).get("centroids")) \
        if cause_model and cause_model.get("features") == feats else []
    if recent_fault:
        out["notes"].append("เซนเซอร์ผิดพลาดหลังเริ่มเตือน: เป็นไปได้ว่าหัววัดถูกยกหรือขยับ")
    if learned and learned[0]["similarity"] >= OPEN_SET:
        cands = learned[:3]
        out["notes"].append("เทียบกับน้ำผิดปกติที่คุณเก็บและตั้งชื่อไว้")
    elif rules and rules[0]["similarity"] >= OPEN_SET:
        cands = rules[:3]
        if learned:
            out["notes"].append("ไม่ตรงกับน้ำผิดปกติที่คุณเคยเก็บ จึงตีความตามกฎทั่วไปของทิศทางการเปลี่ยน")
    else:
        cands = sorted(learned[:1] + rules[:2], key=lambda c: -c["similarity"])
    if recent_fault and not any(c["family"] == "probe_disturbed" for c in cands):
        cands = [{"family": "probe_disturbed", "name": PROBE["name"], "similarity": 0.85, "source": "rule"}] + cands
        cands = cands[:3]
    out["candidates"] = cands
    out["verdict"] = _verdict(sorted(cands, key=lambda c: -c["similarity"]))
    top = cands[0] if cands else None
    if out["verdict"] == "match" and top:
        out["headline"] = f"สอดคล้องกับ: {top['name']}"
    elif out["verdict"] == "ambiguous":
        tied = [c for c in sorted(cands, key=lambda c: -c["similarity"])
                if c["similarity"] >= OPEN_SET and cands[0]["similarity"] - c["similarity"] < AMBIGUOUS] or cands[:2]
        out["headline"] = "แยกไม่ออกระหว่าง " + " / ".join(c["name"] for c in tied)
    else:
        out["headline"] = "ไม่ตรงกับรูปแบบที่รู้จัก (ไม่ทราบสาเหตุ)"
    if "ph" in missing or "ph" not in feats:
        if out["verdict"] == "match" and top and top["family"] in ("dissolved_salts", "suspended_solids"):
            out["disambiguate"].append("เพิ่มเซนเซอร์ pH เพื่อแยกเกลือธรรมดาออกจากเบกกิ้งโซดาหรือกรด")
    if out["verdict"] == "ambiguous":
        if "ph" not in feats:
            out["disambiguate"].append("เพิ่มเซนเซอร์ pH: เกลือ เบกกิ้งโซดา และกรด เปลี่ยน pH ต่างกัน จึงแยกได้")
        out["disambiguate"].append("เก็บรอบ anomaly ของสารทั้งสองชนิดแล้วเทรนใหม่ ระบบจะแยกได้ถ้าทิศทางต่างกันพอ")
    out["disambiguate"] = list(dict.fromkeys(out["disambiguate"]))
    return out


def build_model(items, feats):
    """Centroids + leave-one-anomaly-run-out confusion matrix.

    items: [{"run_id", "family", "label", "vec"}] with the fingerprint of every labelled anomaly run.
    A family with a single run cannot be tested honestly (nothing left to learn it from), so it is
    reported as "ไม่มีรอบอื่นของ family เดียวกัน" and counted in `unseen`, not in accuracy.
    """
    def centroids(rows):
        by = {}
        for it in rows:
            by.setdefault(it["family"], []).append(unit(it["vec"]))
        return {f: unit(np.mean(v, axis=0)).round(4).tolist() for f, v in by.items()}

    model = {"features": feats, "centroids": centroids(items),
             "n_runs": {f: sum(1 for it in items if it["family"] == f) for f in {it["family"] for it in items}}}
    fams = sorted(model["n_runs"])
    cols = fams + ["unknown", "ambiguous"]
    matrix = {f: {c: 0 for c in cols} for f in fams}
    correct = tested = unseen = 0
    for i, it in enumerate(items):
        rest = [x for j, x in enumerate(items) if j != i]
        cand = learned_candidates(it["vec"], centroids(rest))
        v = _verdict(cand)
        pred = cand[0]["family"] if v == "match" else v
        matrix[it["family"]][pred if pred in cols else "unknown"] += 1
        if model["n_runs"][it["family"]] < 2:
            unseen += 1
            continue
        tested += 1
        correct += int(pred == it["family"])
    # the rules against the user's own labels (only labels we recognise)
    rule_ok = rule_n = 0
    for it in items:
        hint = LABEL_HINTS.get(it["family"])
        if hint and hint in PROTOS and all(n in feats for n in PROTOS[hint]["needs"]):
            cands, _ = rule_candidates(it["vec"], feats)
            rule_n += 1
            rule_ok += int(bool(cands) and cands[0]["family"] == hint and _verdict(cands) == "match")
    model["eval"] = {"families": fams, "columns": cols, "matrix": [[matrix[f][c] for c in cols] for f in fams],
                     "tested": tested, "correct": correct, "unseen": unseen,
                     "accuracy": (correct / tested) if tested else None,
                     "rule_tested": rule_n, "rule_correct": rule_ok}
    return model
