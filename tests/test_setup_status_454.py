"""Setup status (#454): what is finished after a team is installed, and what is only waiting for the owner.

Everything runs in temporary projects and a temporary Office checkout. The Office registry is only ever read."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.team_room import outbox, roster, setup_status

MANAGER = "00000000-0000-0000-0000-0000000000a1"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Case(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / "proj"
        (self.project / ".bot-mode").mkdir(parents=True)
        self.office = self.base / "office"
        (self.office / ".bot-mode" / "local").mkdir(parents=True)
        self.registry = self.office / ".bot-mode" / "local" / "office-project-roots.json"
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("CODEX_OFFICE_REGISTRY", None)

    def register(self, *roots):
        projects = [{"id": "p%d" % number, "root": str(root)} for number, root in enumerate(roots)]
        self.registry.write_text(json.dumps({"schema": 1, "projects": projects, "standalone": []}), encoding="utf-8")

    def bind_manager(self, thread=MANAGER):
        current = roster.load(self.project)
        data = json.loads(json.dumps(current))
        if data["participants"]:
            data["participants"][0]["thread_id"] = thread
        else:
            roster.add_participant(data, "manager", "Manager", thread_id=thread)
        roster.save(self.project, data, roster.version(current))

    def install_outbox(self):
        (self.project / ".bot-mode" / "outbox.py").write_text("# outbox", encoding="utf-8")

    def collect(self):
        return setup_status.collect(self.project, self.office)

    def queue(self):
        return outbox.OutboxDB(self.project / ".bot-mode" / "outbox" / "queue.sqlite")


class RegistrationTests(Case):
    def test_an_unregistered_project_waits_for_the_owner(self):
        self.register(self.base / "another")
        step = self.collect()["steps"]["office_registration"]
        self.assertEqual(step["state"], "waiting for owner consent")
        self.assertIn("owner's consent", step["detail"])

    def test_a_missing_registry_file_waits_for_the_owner_and_is_not_created(self):
        self.assertEqual(self.collect()["steps"]["office_registration"]["state"], "waiting for owner consent")
        self.assertFalse(self.registry.exists())

    def test_a_registered_project_is_done_whatever_the_spelling_of_its_path(self):
        self.register(str(self.project).replace("\\", "/"))
        self.assertEqual(self.collect()["steps"]["office_registration"]["state"], "done")

    def test_the_office_own_project_needs_no_registration(self):
        status = setup_status.collect(self.office, self.office)
        self.assertEqual(status["steps"]["office_registration"]["state"], "not applicable")
        self.assertNotIn("office_registration", status["incomplete"])

    def test_an_unreadable_registry_is_unknown_and_counts_as_not_finished(self):
        self.registry.write_text("{not json", encoding="utf-8")
        status = self.collect()
        self.assertEqual(status["steps"]["office_registration"]["state"], "unknown")
        self.assertIn("office_registration", status["incomplete"])

    def test_the_registry_named_by_the_environment_is_the_one_that_is_read(self):
        elsewhere = self.base / "elsewhere.json"
        elsewhere.write_text(json.dumps({"schema": 1, "projects": [{"id": "p0", "root": str(self.project)}]}), encoding="utf-8")
        os.environ["CODEX_OFFICE_REGISTRY"] = str(elsewhere)
        self.assertEqual(self.collect()["steps"]["office_registration"]["state"], "done")


class StepTests(Case):
    def test_the_roster_step_needs_a_bound_manager_chat(self):
        self.assertEqual(self.collect()["steps"]["roster"]["state"], "incomplete")
        self.bind_manager()
        self.assertEqual(self.collect()["steps"]["roster"]["state"], "done")
        self.bind_manager(thread=None)
        self.assertEqual(self.collect()["steps"]["roster"]["state"], "incomplete")

    def test_the_outbox_step_needs_the_installed_outbox(self):
        self.assertEqual(self.collect()["steps"]["outbox"]["state"], "incomplete")
        self.install_outbox()
        self.assertEqual(self.collect()["steps"]["outbox"]["state"], "done")

    def test_the_relay_step_needs_a_descriptor_and_an_answering_port(self):
        self.assertEqual(self.collect()["steps"]["relay_route"]["state"], "incomplete")
        relay = self.project / ".bot-mode" / "desktop-relay"
        relay.mkdir()
        (relay / "relay.json").write_text(json.dumps({"url": "http://127.0.0.1:1", "token": "t"}), encoding="utf-8")
        with mock.patch.object(outbox, "relay_reachable", return_value=False):
            step = self.collect()["steps"]["relay_route"]
        self.assertEqual(step["state"], "incomplete")
        self.assertIn("does not accept connections", step["detail"])
        with mock.patch.object(outbox, "relay_reachable", return_value=True):
            self.assertEqual(self.collect()["steps"]["relay_route"]["state"], "done")


class DeliveryProofTests(Case):
    def test_without_a_queue_there_is_no_proof_and_none_is_created(self):
        status = self.collect()
        self.assertEqual((status["delivery_proof"]["state"], status["delivery"]), ("none", "not proven"))
        self.assertFalse((self.project / ".bot-mode" / "outbox").exists())

    def test_a_queue_with_only_pending_work_proves_nothing(self):
        db = self.queue()
        try:
            db.enqueue("developer", "manager", None, "report")
            db.canary_start("manager", 600)
        finally:
            db.close()
        status = self.collect()
        self.assertEqual((status["delivery_proof"]["state"], status["delivery"]), ("none", "not proven"))

    def test_a_processed_canary_is_a_canary_proof(self):
        db = self.queue()
        try:
            db.canary_done(db.canary_start("manager", 600), 0.5, 0.1, "idle")
        finally:
            db.close()
        status = self.collect()
        self.assertEqual((status["delivery_proof"]["state"], status["delivery"]), ("canary delivered", "proven earlier"))  # steps are unfinished

    def test_a_delivered_report_is_the_strongest_proof(self):
        db = self.queue()
        try:
            db.canary_done(db.canary_start("manager", 600), 0.5, 0.1, "idle")
            db.mark_delivered_many([db.enqueue("developer", "manager", None, "report")])
        finally:
            db.close()
        status = self.collect()
        self.assertEqual((status["delivery_proof"]["state"], status["delivery"]), ("real report delivered", "proven earlier"))

    def test_a_row_marked_delivered_by_hand_is_not_proof(self):
        db = self.queue()
        try:
            row = db.enqueue("developer", "manager", None, "report")
            db.mark_ambiguous(row, "unknown")
            db.resolve(row, "delivered", "arrived")
            self.assertEqual(db.count_state("delivered"), 1)
        finally:
            db.close()
        status = self.collect()
        self.assertEqual((status["delivery_proof"]["state"], status["delivery"]), ("none", "not proven"))

    def test_a_row_decided_by_hand_to_retry_and_then_delivered_by_the_pump_is_proof(self):
        db = self.queue()
        try:
            row = db.enqueue("developer", "manager", None, "report")
            db.mark_ambiguous(row, "unknown")
            db.resolve(row, "retry")
            db.mark_delivered_many([row])
        finally:
            db.close()
        self.assertEqual(self.collect()["delivery_proof"]["state"], "real report delivered")

    def test_a_hand_decided_row_does_not_hide_a_real_delivery(self):
        db = self.queue()
        try:
            by_hand = db.enqueue("developer", "manager", None, "first")
            db.mark_ambiguous(by_hand, "unknown")
            db.resolve(by_hand, "delivered")
            db.mark_delivered_many([db.enqueue("developer", "manager", None, "second")])
        finally:
            db.close()
        self.assertEqual(self.collect()["delivery_proof"]["state"], "real report delivered")

    def test_a_queue_from_before_the_canary_still_proves_a_delivered_report(self):
        db = self.queue()
        try:
            db.mark_delivered_many([db.enqueue("developer", "manager", None, "report")])
            with db.conn:
                db.conn.execute("DROP TABLE canary")
        finally:
            db.close()
        self.assertEqual(self.collect()["delivery_proof"]["state"], "real report delivered")

    def test_a_queue_from_before_the_canary_without_a_delivery_has_no_proof_and_is_not_unknown(self):
        db = self.queue()
        try:
            db.enqueue("developer", "manager", None, "report")
            with db.conn:
                db.conn.execute("DROP TABLE canary")
        finally:
            db.close()
        self.assertEqual(self.collect()["delivery_proof"]["state"], "none")

    def test_a_project_folder_with_hash_and_percent_in_its_name_is_read_and_nothing_is_created(self):
        parent = self.base / "odd"
        parent.mkdir()
        self.project = parent / "hash#1 %41"
        (self.project / ".bot-mode").mkdir(parents=True)
        db = self.queue()
        try:
            db.mark_delivered_many([db.enqueue("developer", "manager", None, "report")])
        finally:
            db.close()
        before = sorted(path.relative_to(parent).as_posix() for path in parent.rglob("*"))
        self.assertEqual(self.collect()["delivery_proof"]["state"], "real report delivered")
        self.assertEqual(sorted(path.relative_to(parent).as_posix() for path in parent.rglob("*")), before)
        self.assertEqual(sorted(path.name for path in parent.iterdir()), ["hash#1 %41"])  # no stray file next to the project

    def test_a_missing_queue_in_an_odd_folder_creates_nothing(self):
        parent = self.base / "odd2"
        parent.mkdir()
        self.project = parent / "hash#2 %41"
        (self.project / ".bot-mode").mkdir(parents=True)
        self.assertEqual(self.collect()["delivery_proof"]["state"], "none")
        self.assertEqual(sorted(path.name for path in parent.iterdir()), ["hash#2 %41"])

    def test_an_unreadable_queue_is_unknown_and_not_proof(self):
        folder = self.project / ".bot-mode" / "outbox"
        folder.mkdir()
        (folder / "queue.sqlite").write_bytes(b"this is not a database" * 100)
        status = self.collect()
        self.assertEqual((status["delivery_proof"]["state"], status["delivery"]), ("unknown", "not proven"))

    def test_reading_the_proof_leaves_the_queue_unchanged(self):
        db = self.queue()
        try:
            db.mark_delivered_many([db.enqueue("developer", "manager", None, "report")])
        finally:
            db.close()
        path = self.project / ".bot-mode" / "outbox" / "queue.sqlite"
        before = digest(path)
        self.collect()
        self.assertEqual(digest(path), before)


class CompleteTests(Case):
    def finish_everything(self):
        self.bind_manager()
        self.install_outbox()
        self.register(self.project)
        relay = self.project / ".bot-mode" / "desktop-relay"
        relay.mkdir()
        (relay / "relay.json").write_text(json.dumps({"url": "http://127.0.0.1:1", "token": "t"}), encoding="utf-8")

    def test_every_step_done_is_still_not_complete_without_proof(self):
        self.finish_everything()
        with mock.patch.object(outbox, "relay_reachable", return_value=True):
            status = self.collect()
        self.assertEqual(status["incomplete"], [])
        self.assertFalse(status["complete"])
        self.assertIn("Delivery not proven", "\n".join(setup_status.render(status)))

    def test_all_steps_and_a_proof_make_it_complete_and_the_text_says_delivery_works(self):
        self.finish_everything()
        db = self.queue()
        try:
            db.mark_delivered_many([db.enqueue("developer", "manager", None, "report")])
        finally:
            db.close()
        with mock.patch.object(outbox, "relay_reachable", return_value=True):
            status = self.collect()
        self.assertTrue(status["complete"])
        self.assertEqual(status["delivery"], "works")
        self.assertIn("Delivery works.", setup_status.render(status))

    def test_an_unregistered_project_is_listed_as_not_finished_even_with_a_delivered_canary(self):
        self.finish_everything()
        self.register(self.base / "another")
        db = self.queue()
        try:
            db.canary_done(db.canary_start("manager", 600), 0.5, 0.1, "idle")
        finally:
            db.close()
        with mock.patch.object(outbox, "relay_reachable", return_value=True):
            status = self.collect()
        self.assertEqual(status["incomplete"], ["office_registration"])
        self.assertFalse(status["complete"])
        text = "\n".join(setup_status.render(status))
        self.assertIn("Not finished: Office registration. Report each of them as incomplete.", text)
        self.assertIn("waiting for owner consent", text)
        self.assertIn("Delivery was proven earlier (canary delivered), but a step is unfinished now.", text)
        self.assertNotIn("Delivery works.", text)


class WriteTests(Case):
    def test_the_status_is_stored_in_the_project_and_the_registry_is_never_written(self):
        self.register(self.base / "another")
        before = digest(self.registry)
        path = setup_status.write(self.project, self.collect())
        self.assertEqual(path, self.project / ".bot-mode" / "local" / "setup-status.json")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["steps"]["office_registration"]["state"], "waiting for owner consent")
        self.assertEqual(digest(self.registry), before)
        self.assertEqual(sorted(entry.name for entry in self.registry.parent.iterdir()), ["office-project-roots.json"])
        self.assertEqual(sorted(entry.name for entry in path.parent.iterdir()), ["setup-status.json"])
        self.registry.unlink()
        setup_status.write(self.project, self.collect())
        self.assertFalse(self.registry.exists())


if __name__ == "__main__":
    unittest.main()
