"""Water Sentinel: ESP32 water-quality anomaly monitor (edge app)."""
__version__ = "1.0.0"

# --- compatibility for files saved before the 2026-11-21 restructure -------------------------
# Model bundles (runtime/model/**/bundle.joblib) are pickles that remember the module path of each
# class, e.g. "sentinel.models.MSPC". The modules now live in subpackages, so old names are mapped
# to their new home here. Safe to remove once every saved model has been re-trained.
import importlib as _importlib
import importlib.abc as _abc
import importlib.util as _util
import sys as _sys

_MOVED = {
    "sources": "acquisition.sources", "features": "acquisition.features", "turbidity": "acquisition.turbidity",
    "runs": "storage.runs", "datasets": "storage.datasets", "explorer": "storage.explorer",
    "models": "detection.models", "training": "detection.training", "tuning": "detection.tuning",
    "drift": "detection.drift", "causes": "detection.causes", "live": "detection.live",
    "edge_sim": "edge.simulator",   # old edge.py is now the edge/ package (edge/export.py)
    "hub": "app.hub", "server": "app.server", "alerts": "app.alerts", "share": "app.share",
}


class _OldModuleNames(_abc.MetaPathFinder, _abc.Loader):
    """Makes `import sentinel.models` return `sentinel.detection.models` (only for the old flat names)."""

    def find_spec(self, name, path=None, target=None):
        short = name[len(__name__) + 1:] if name.startswith(__name__ + ".") else None
        if short in _MOVED and _MOVED[short] != short:
            return _util.spec_from_loader(name, self)
        return None

    def create_module(self, spec):
        return _importlib.import_module(f"{__name__}.{_MOVED[spec.name[len(__name__) + 1:]]}")

    def exec_module(self, module):
        pass


_sys.meta_path.append(_OldModuleNames())
