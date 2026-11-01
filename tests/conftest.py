"""Fusion's adsk modules exist only inside Fusion. The tests cover the pure logic, so a stub
that hands out empty base classes is enough to import the add-in."""

import sys
import types
from pathlib import Path


class _StubModule(types.ModuleType):
    def __getattr__(self, name):
        cls = type(name, (), {})
        setattr(self, name, cls)
        return cls


adsk = _StubModule("adsk")
for sub in ("core", "fusion", "drawing"):
    module = _StubModule(f"adsk.{sub}")
    setattr(adsk, sub, module)
    sys.modules[f"adsk.{sub}"] = module
sys.modules["adsk"] = adsk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "FusionExport"))
