"""pytest: isolate the whole session from the owner's real user locations (see isolation.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolation  # noqa: E402

isolation.isolate()
