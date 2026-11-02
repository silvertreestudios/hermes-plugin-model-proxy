"""Directory plugin entry; use the same registration as the installed wheel."""
from pathlib import Path
import sys

# Hermes loads directory plugins by file path, without installing their src tree.
_source = str(Path(__file__).resolve().parent / "src")
if _source not in sys.path:
    sys.path.insert(0, _source)
from hermes_model_proxy.model_proxy import register  # noqa: E402, F401
