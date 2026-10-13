"""Read and update the project roster: teams, participants and current chat links.

Projects get the same command as .bot-mode/roster.py when a team is installed.

Examples:
  python -B scripts/roster.py --project . links
  python -B scripts/roster.py --project . set-thread developer <thread-id> --reason "chat too long"
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from team_room.roster import main  # noqa: E402

if __name__ == '__main__':
    sys.exit(main())
