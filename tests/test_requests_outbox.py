import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import warnings
from unittest.mock import patch

from scripts.team_room import requests


def run_main(args):
    """requests.main with its output captured; main reconfigures stdout, so it needs a real text wrapper."""
    out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), warnings.catch_warnings():
        # A ResourceWarning is printed when the garbage collector frees an unclosed file of an earlier test, so it can land
        # in this test's stderr at any time. It says nothing about requests.main.
        warnings.simplefilter("ignore", ResourceWarning)
        code = requests.main(args)
    out.flush()
    return code, out.buffer.getvalue().decode("utf-8"), err.getvalue()


class TestRequestOutbox(unittest.TestCase):
    def project(self, with_outbox=True):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        project = Path(folder.name)
        if with_outbox:
            outbox = project / ".bot-mode" / "outbox.py"
            outbox.parent.mkdir(parents=True)
            outbox.write_text("# test outbox\n", encoding="utf-8")
        self.assertEqual(run_main(["--project", str(project), "open", "REQ-1", "--from", "manager", "--to", "developer",
                                   "--summary", "Review candidate"])[0], 0)
        return project

    def state(self, project):
        return requests.load(project)["requests"][0]

    def test_reply_report_is_enqueued_through_project_outbox(self):
        project = self.project()
        outbox = project / ".bot-mode" / "outbox.py"
        with patch("scripts.team_room.requests.subprocess.run") as run:
            run.return_value.returncode = 0
            requests.enqueue_outbox_report(
                project, {"id": "req-1", "summary": "Review candidate"},
                "code-reviewer", "done", "https://example.test/review", None
            )
        run.assert_called_once()
        args = run.call_args.args[0]
        self.assertEqual(args[:3], [requests.sys.executable, "-B", str(outbox)])
        self.assertEqual(args[3:5], ["--project", str(project.resolve())])
        self.assertIn("send", args)
        self.assertIn("req-1", " ".join(args))
        self.assertIn("code-reviewer", " ".join(args))
        self.assertIn("request:req-1:code-reviewer:done", args)

    def check_closeout_survives(self, failure):
        project = self.project()
        with patch("scripts.team_room.requests.subprocess.run", **failure):
            code, out, err = run_main(["--project", str(project), "reply", "REQ-1", "--by", "developer",
                                       "--link", "https://github.com/o/r/pull/1"])
        self.assertEqual(code, 0)
        self.assertEqual(self.state(project)["state"], "answered")
        self.assertEqual(json.loads(out)["state"], "answered")  # the closed item is still printed
        self.assertIn("warning:", err)
        self.assertIn("is closed", err)
        self.assertIn("Do not repeat the closeout", err)
        self.assertIn("outbox.py", err)
        self.assertIn("send", err)
        self.assertIn("request:REQ-1:developer:done", err)
        self.assertNotIn("Traceback", err)
        return project

    def test_reply_survives_a_nonzero_outbox_exit(self):
        self.check_closeout_survives({"return_value": subprocess.CompletedProcess([], 1, "", "boom")})

    def test_reply_survives_an_outbox_timeout(self):
        self.check_closeout_survives({"side_effect": subprocess.TimeoutExpired(["outbox"], 15)})

    def test_reply_survives_an_unexpected_enqueue_error(self):
        self.check_closeout_survives({"side_effect": OSError("cannot start python")})

    def test_repeating_the_reply_after_a_warning_keeps_the_ledger_and_retries_the_same_dedupe_key(self):
        project = self.check_closeout_survives({"return_value": subprocess.CompletedProcess([], 1, "", "boom")})
        before = self.state(project)
        with patch("scripts.team_room.requests.subprocess.run") as run:
            run.return_value.returncode = 0
            code, _, err = run_main(["--project", str(project), "reply", "REQ-1", "--by", "developer"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.state(project), before)
        self.assertIn("request:REQ-1:developer:done", run.call_args.args[0])

    def test_reply_without_a_project_outbox_queues_nothing_and_says_so(self):
        project = self.project(with_outbox=False)
        with patch("scripts.team_room.requests.subprocess.run") as run:
            code, out, err = run_main(["--project", str(project), "reply", "REQ-1", "--by", "developer"])
        self.assertEqual(code, 0)
        self.assertEqual(self.state(project)["state"], "answered")  # the closeout still happened
        self.assertEqual(json.loads(out)["state"], "answered")
        self.assertIn("warning: request REQ-1 is closed", err)
        self.assertIn("no .bot-mode/outbox.py", err)
        self.assertIn("Do not repeat the closeout", err)
        run.assert_not_called()

    def test_verdict_closeout_survives_an_outbox_failure(self):
        project = self.project()
        comment = "https://github.com/o/r/pull/1#issuecomment-5"
        for failure in ({"return_value": subprocess.CompletedProcess([], 1, "", "boom")},
                        {"side_effect": subprocess.TimeoutExpired(["outbox"], 15)}):
            with patch("scripts.team_room.requests.post_verdict", return_value=comment), \
                 patch("scripts.team_room.requests.subprocess.run", **failure):
                code, out, err = run_main(["--project", str(project), "verdict", "REQ-1", "--by", "developer",
                                           "--verdict", "approve", "--candidate", "a" * 40, "--body-file", __file__])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["id"], "REQ-1")
            self.assertIn("warning:", err)
            self.assertIn("request:REQ-1:developer:done", err)

    def test_the_manual_command_is_the_same_send_the_enqueue_would_run(self):
        project = self.project()
        item = {"id": "REQ-1", "summary": "It's a test"}
        args = requests._outbox_send_args(project, item, "developer", "done", "https://x/1")
        self.assertEqual(args[args.index("--to") + 1], "manager")
        self.assertEqual(args[args.index("--dedupe-key") + 1], "request:REQ-1:developer:done")
        self.assertIn("It's a test", args[-1])


if __name__ == "__main__":
    unittest.main()
