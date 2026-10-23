"""Static safety checks for the Rust controller when Rust is unavailable locally."""
from pathlib import Path
import re
import unittest


SOURCE = Path(__file__).parents[1] / "src-tauri" / "src" / "main.rs"


def function_body(source, name):
    pattern = r"\bfn\s+" + re.escape(name) + r"(?:<[^>]*>)?\s*\([^)]*\)[^{]*\{"
    match = re.search(pattern, source)
    if not match:
        raise AssertionError(f"Rust function {name} was not found")
    opening = source.index("{", match.start())
    depth = 0
    # Ignore braces in Rust string literals and comments (malformed JSON fixtures
    # intentionally contain unmatched braces). Keep offsets for the returned body.
    masked = re.sub(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/',
                    lambda match: " " * len(match.group()), source, flags=re.S)
    for index in range(opening, len(source)):
        if masked[index] == "{":
            depth += 1
        elif masked[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1:index]
    raise AssertionError(f"Rust function {name} has no closing brace")


def first_block_after(source, marker):
    start = source.index(marker)
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1:index]
    raise AssertionError(f"Block after {marker!r} has no closing brace")


class NativeSourceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE.read_text(encoding="utf-8")

    def test_settings_journal_blocks_ambiguous_reload_and_never_deletes_main(self):
        load = function_body(self.source, "load_settings")
        self.assertLess(load.index("ensure_settings_settled"), load.index("fs::read"))
        guard = function_body(self.source, "ensure_settings_settled")
        self.assertIn('"json.tmp"', guard)
        self.assertIn('"json.bak"', guard)
        self.assertIn("try_exists()", guard)
        save = function_body(self.source, "write_settings")
        self.assertNotIn("remove_file(settings_file)", save)
        self.assertIn("create_new(true)", save)
        self.assertLess(save.index("sync_all()"), save.index("fs::rename(settings_file"))
        self.assertLess(save.index("fs::rename(settings_file"), save.index("fs::rename(&temporary"))
        self.assertLess(save.index("fs::rename(&temporary"), save.index("fs::remove_file(&backup)"))
        for fixture in ("existing_preserved_marker_survives_settings_rewrite",
                        "interrupted_marker_rewrite_refuses_reload_at_every_stage",
                        "failed_marker_clear_retains_memory_and_disk_lock"):
            self.assertIn("fn " + fixture, self.source)

    def test_controller_fixtures_use_concrete_child_and_refusals_keep_exit_blocked(self):
        fixture = function_body(self.source, "explicit_preserve_persists_lock_and_does_not_terminate_owned_child")
        self.assertNotIn("child: Mutex::new(child)", fixture)
        fixture = function_body(self.source, "preserved_child_safety_lock_can_be_cleared_only_after_restart_confirmation")
        self.assertNotIn("let mut active_child = fake_startup_child()", fixture)
        fixture = function_body(self.source, "preserved_controller_exit_requires_confirmation_and_keeps_the_marker")
        self.assertIn("safe_to_exit: false", fixture)

    def test_runtime_preflights_each_component_and_checks_project_containment(self):
        body = function_body(self.source, "runtime_for")
        preflight = body.index("inspect_runtime_component")
        creation = body.index("fs::create_dir(")
        self.assertLess(preflight, creation)
        inspection = function_body(self.source, "inspect_runtime_component")
        self.assertIn("canonical.starts_with(project)", inspection)
        self.assertIn("is_link_or_reparse", inspection)
        self.assertNotIn("fs::create_dir_all(", body)
        self.assertIn("FILE_ATTRIBUTE_REPARSE_POINT", self.source)
        descriptor = function_body(self.source, "read_descriptor_at_port")
        self.assertLess(descriptor.index("checked_existing_runtime(project)"), descriptor.index("fs::read("))
        self.assertIn("checked_existing_runtime(&project)", body)
        fixture = function_body(self.source, "runtime_refuses_a_link_at_each_existing_component_before_creating_children")
        self.assertIn("sentinel", fixture)
        self.assertIn("external_child != outside", fixture)
        self.assertIn("external sentinel changed", fixture)

    def test_startup_failure_keeps_the_exact_live_child_recoverable(self):
        body = function_body(self.source, "record_startup_failure_state")
        retained = first_block_after(body, "if state.running && state.child.is_some()")
        self.assertIn("state.startup_error = Some(reason)", retained)
        self.assertIn("state.starting = false", body)
        self.assertNotRegex(retained, r"state\.(?:child|pid|running)\s*=")
        failure = function_body(self.source, "record_startup_failure")
        self.assertIn("if let Ok(mut child) = controller.child_state()", failure)
        self.assertIn("record_startup_failure_state(&mut child, reason)", failure)

    def test_startup_requires_matching_health_and_status_before_ready(self):
        probes = function_body(self.source, "verify_startup_probes")
        self.assertIn('"/desktop/health"', probes)
        self.assertIn('"/desktop/status"', probes)
        self.assertIn('health.get("state").and_then(Value::as_str) != Some("running")', probes)
        self.assertGreaterEqual(probes.count("identity_matches(descriptor"), 2)
        wait = function_body(self.source, "wait_for_startup")
        polling = function_body(self.source, "wait_for_descriptor")
        self.assertIn("verify_startup_probes(&descriptor)", polling)
        self.assertLess(wait.index("let view = make_view(controller)?"), wait.index("child.startup_error = None"))
        self.assertIn('view.status.state != "running"', wait)

    def test_recovery_requires_live_child_pid_and_recorded_startup_failure(self):
        predicate = function_body(self.source, "startup_recovery_available")
        for condition in ("state.running", "!state.starting", "state.pid.is_some()",
                          "state.child.is_some()", "state.startup_error.is_some()"):
            self.assertIn(condition, predicate)

    def test_poisoned_lifecycle_state_fails_closed(self):
        getter = function_body(self.source, "child_state")
        self.assertIn("map_err", getter)
        self.assertNotIn("into_inner", getter)
        status = function_body(self.source, "status_for")
        self.assertIn('status_template("unverified", &reason)', status)
        self.assertIn("unwrap_or(false)", self.source)

    def test_startup_and_recovery_route_probe_errors_through_state_cleanup(self):
        source = self.source
        self.assertIn("record_startup_failure(controller, error.clone())", source)
        recovery = function_body(source, "recover_startup_inner")
        self.assertIn("wait_for_startup", recovery)
        self.assertNotIn(".spawn(", recovery)
        self.assertNotIn(".kill(", source)

    def test_unverified_owned_child_exposes_recheck_without_enabling_foreign_control(self):
        status = function_body(self.source, "status_for")
        self.assertIn("can_recover", status)
        self.assertIn("startup_recovery_available(&child)", status)
        self.assertIn("can_stop: false", function_body(self.source, "status_template"))
        self.assertIn("recover_startup", self.source)

    def test_sidecar_uses_basename_for_flat_installed_layout(self):
        body = function_body(self.source, "start_inner")
        self.assertIn('.sidecar("office-service")', body)
        self.assertNotIn('.sidecar("binaries/', body)
        self.assertNotIn('.sidecar("binaries\\', body)

    def test_unverified_startup_has_an_explicit_preserve_and_exit_boundary(self):
        helper = function_body(self.source, "preserved_child_for")
        self.assertIn("startup_recovery_available(state)", helper)
        preserve = function_body(self.source, "preserve_startup_child_inner")
        self.assertIn("preserved_child_for", preserve)
        self.assertIn("preserved_child", preserve)
        self.assertIn("safe_to_exit = true", preserve)
        self.assertLess(preserve.index("controller.save_settings()?"), preserve.index("safe_to_exit = true"))
        self.assertNotIn(".kill(", preserve)
        self.assertNotIn("http_request", preserve)
        preserve_command = function_body(self.source, "preserve_startup_child")
        self.assertIn("preserve_startup_child_inner(&controller, confirmed)", preserve_command)
        clear = function_body(self.source, "clear_preserved_child_inner")
        self.assertIn("confirmed_stopped", clear)
        self.assertIn("child.running || child.starting || child.child.is_some()", clear)
        self.assertIn("clear_preserved_child", self.source)
        quit_helper = function_body(self.source, "prepare_preserved_controller_exit")
        self.assertIn("has_preserved_child", quit_helper)
        self.assertIn("child.running || child.starting || child.child.is_some()", quit_helper)
        self.assertIn("quit_preserved_controller", self.source)
        ui = Path(__file__).parents[1].joinpath("ui", "app.js").read_text(encoding="utf-8").lower()
        self.assertIn("canquitpreserved", ui)
        self.assertIn("office work may still be active", ui)
        self.assertIn("invoke('preserve_startup_child', { confirmed: true })", ui)
        self.assertIn("invoke('clear_preserved_child', { confirmedstopped: true })", ui)
        self.assertIn("invoke('quit_preserved_controller', { confirmed: true })", ui)
        status = function_body(self.source, "status_for")
        self.assertLess(status.index("settings.preserved_child"), status.index("read_descriptor_at_port(project,"))
        self.assertIn('status_template("preserved"', function_body(self.source, "preserved_child_status"))
        self.assertIn("no_tracked_child", status)

    def test_persistent_startup_probe_failure_fixtures_keep_the_child_preservable(self):
        for fixture, expected_probe in (
            ("persistent_missing_descriptor_can_be_preserved", "read_descriptor(&project)"),
            ("persistent_malformed_descriptor_can_be_preserved", "read_descriptor(&project)"),
            ("persistent_http_failure_can_be_preserved", "http_request(&descriptor"),
        ):
            body = function_body(self.source, fixture)
            self.assertIn(expected_probe, body)
            self.assertIn("record_startup_failure_state", body)
            self.assertIn("preserved_child_for", body)
            self.assertIn("fake-owned-child", body)


if __name__ == "__main__":
    unittest.main()
