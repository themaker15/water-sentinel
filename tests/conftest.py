import json
import os
import pathlib
import sys
import tempfile

# Point data/model/config at a throw-away folder BEFORE sentinel is imported.
HOME = pathlib.Path(tempfile.mkdtemp(prefix="sentinel-test-"))
os.environ["SENTINEL_HOME"] = str(HOME)
(HOME / "config.json").write_text(json.dumps({"detector": {"warmup_s": 5}}), encoding="utf-8")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
