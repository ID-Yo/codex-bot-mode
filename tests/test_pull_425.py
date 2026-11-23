"""The profile text for the Manager pull and the agent fallback names commands that exist (#425, S5-B)."""
from datetime import datetime, timedelta, timezone
import io
import contextlib
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch

from scripts.team_room import outbox, requests

ROOT = Path(__file__).resolve().parents[1]
BASE = (ROOT / "catalog" / "base" / "BASE.md").read_text(encoding="utf-8")
PLAYBOOK = (ROOT / "catalog" / "agents" / "manager" / "PLAYBOOK.md").read_text(encoding="utf-8")


def section(text, title):
    match = re.search(r"^## " + re.escape(title) + r"\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    assert match, title
    return " ".join(match.group(1).split())


class ManagerPull(unittest.TestCase):
    def test_the_playbook_pulls_at_every_turn_start_and_heartbeat_with_the_real_command(self):
        text = section(PLAYBOOK, "Pull at every turn start and heartbeat")
        self.assertIn("The request ledger is the truth; the outbox only wakes you.", text)
        self.assertIn("start of every turn", text)
        self.assertIn("every heartbeat", text)
        self.assertIn("python -B {{host}}/.bot-mode/requests.py --project {{host}} pending", text)
        self.assertIn("UNKNOWN", text)
        self.assertIn("resolve <id> --as delivered|skipped|retry", text)

    def test_the_resolutions_the_playbook_names_are_the_ones_outbox_accepts(self):
        self.assertEqual(set(outbox.RESOLUTIONS), {"delivered", "skipped", "retry"})

    def test_the_pull_section_comes_before_the_stage_handoff_rules(self):
        self.assertLess(PLAYBOOK.index("## Pull at every turn start and heartbeat"), PLAYBOOK.index("## Stage handoff and ready work"))

    def test_the_playbook_describes_exactly_what_pending_covers(self):
        text = section(PLAYBOOK, "Pull at every turn start and heartbeat")
        self.assertIn(f"answered in the last {requests.ANSWERED_HOURS} hours", text)
        self.assertIn("answered `partial` and not accepted yet", text)
        self.assertIn("with their reply link", text)
        self.assertIn("It reads only the ledger and the outbox: a comment on a linked issue is found by opening that issue.", text)


class PendingMatchesTheText(unittest.TestCase):
    """What the playbook and BASE promise is what requests.py pending prints, and nothing it does not read."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="test_pull_425_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_open_overdue_answered_with_link_and_undelivered_rows_are_all_found_and_no_issue_is_read(self):
        past = datetime.now(timezone.utc) - timedelta(hours=3)
        link = "https://github.com/o/r/pull/7"

        def setup(data):
            requests.open_request(data, "to-manager", "developer", "manager", "decide the plan", link)
            requests.open_request(data, "late", "manager", "verifier", "check the build", link, due_minutes=60, at=past)
            requests.open_request(data, "done-one", "manager", "developer", "build the helper", link)
            requests.open_request(data, "half", "manager", "developer", "build the rest", link, at=past)
            requests.reply(data, "done-one", "developer", link + "#done")
            requests.reply(data, "half", "developer", link + "#half", outcome="partial", at=past)
        requests.update(self.tmp, setup)
        db = outbox.OutboxDB(self.tmp / ".bot-mode" / "outbox" / "queue.sqlite")
        try:
            db.enqueue("developer", "manager", None, "report that did not arrive", None)
        finally:
            db.close()
        with patch("subprocess.run", side_effect=AssertionError("pending must not call anything")), \
                patch("subprocess.Popen", side_effect=AssertionError("pending must not call anything")):
            text = "\n".join(requests.pending_lines(self.tmp, "manager"))
        self.assertIn("to-manager  from developer", text)                       # open, addressed to the Manager
        self.assertIn("late  to verifier  OVERDUE", text)                       # waited on and overdue
        self.assertIn("done-one  by developer  done", text)                     # answered: found through the ledger
        self.assertIn(link + "#done", text)                                     # ... with its reply link
        self.assertIn("half  by developer  partial", text)                      # partial and not accepted, at any age
        self.assertIn("partial, not accepted yet", text)
        self.assertIn("report that did not arrive", text)                       # undelivered outbox row
        self.assertIn("a comment on the linked issue is not read here", text)   # the limit the text states

    def test_the_command_the_text_names_runs_from_the_installed_cli_and_exits_zero(self):
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="utf-8")
        with contextlib.redirect_stdout(out):
            code = requests.main(["--project", str(self.tmp), "pending"])
            out.flush()
        self.assertEqual(code, 0)
        self.assertIn("Nothing waiting for manager.", raw.getvalue().decode("utf-8"))


class AgentFallback(unittest.TestCase):
    def test_base_names_the_three_fallbacks_in_order_and_what_to_report(self):
        text = section(BASE, "Reporting back and delivery")
        self.assertIn("The ledger is the truth; the outbox only wakes the Manager.", text)
        thread, ledger, issue = (text.index(x) for x in ("(1) write the outcome directly in the Manager's chat with the thread tool",
                                                          "(2) close your request with `requests.py reply --link <evidence>`",
                                                          "(3) comment the outcome and evidence on the linked issue"))
        self.assertLess(thread, ledger)
        self.assertLess(ledger, issue)
        self.assertIn("do not retry in a loop", text)
        self.assertIn("which of the three reached the Manager", text)
        self.assertIn("requests.py pending", text)
        self.assertIn("a closed request and its reply link are found without the relay; the issue comment is found by opening the linked issue", text)

    def test_the_fallbacks_match_what_send_tells_the_agent(self):
        for fallback in ("thread tool", "requests.py reply", "linked issue"):
            self.assertIn(fallback, outbox.SEND_FALLBACKS)
            self.assertIn(fallback, section(BASE, "Reporting back and delivery"))


if __name__ == "__main__":
    unittest.main()
