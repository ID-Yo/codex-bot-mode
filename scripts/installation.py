#!/usr/bin/env python3
"""Read-only inventory and explicit metadata-only adoption of CBM installations."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from team_room.installation_inventory import main


if __name__ == '__main__':
    raise SystemExit(main())
