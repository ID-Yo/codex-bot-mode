# Codex Office desktop controller

This is a small Windows tray controller for the existing Codex Office Python
service. It starts the service bundled beside the Tauri application; it does
not install Python system-wide or replace the existing Office web interface.
The web interface remains in Chrome at `http://localhost:24242`.

## Windows release 0.1.1

The Windows x64 controller and bundled Python service were built locally on
2026-10-22 using Rust/Cargo 1.90.0, MSVC v143 and Windows SDK 26100.
The release installer is an unsigned Windows executable. Authenticode publisher
signing is not configured; Ed25519 component signatures are a separate mechanism.
Use the release checksum to compare the downloaded bytes. A checksum alone is
not a Windows publisher identity.

Native Rust tests (15), controller source checks (12), packaged service health,
safe shutdown and child survival were verified. These checks do not establish
clean-machine installation, interactive tray behavior or uninstall acceptance.

The installer targets the current user. It does not configure a Windows service,
login autostart or automatic application updates. Codex Desktop must already be
installed. Python, Node and Rust are build dependencies and are not required by
the person running the packaged application. Chrome is the primary work browser.

Download the Windows x64 setup file and SHA256SUMS.txt from the Office desktop
release. Compare the SHA-256 using PowerShell Get-FileHash, then run the installer
and launch Codex Office from the Start menu.

## First run

1. Start **Codex Office** from the per-user Start menu shortcut.
2. Choose the root folder of an existing CBM project, containing its
   `.bot-mode/roster.json` and installed team profiles. This is the Office host
   workspace, not the Codex installation directory. If this workspace has an
   Office project registry, the Projects tab can show its other configured
   projects as well. A folder choice does not launch an agent turn. The controller stores its service runtime under that project's
   `.bot-mode/team-room-v1/desktop-controller` directory.
3. Confirm the detected Codex Desktop runtime, or use **Locate** to select its
   `codex.exe`.
4. Select **Start**. Wait until the controller shows authenticated health and
   the service state as running before opening Office in Chrome.

The service claims its configured loopback port (24242). If another listener
has that port, startup reports a collision; the controller does not attach to,
stop, or select a different port for that listener.

Before creating the controller runtime or reading its service descriptor, it
checks each existing `.bot-mode`, `team-room-v1`, and `desktop-controller`
path component for links/reparse points and verifies canonical containment
within the selected project.

## Everyday controls

- **Status and activity:** The window refreshes service status and the bounded,
  redacted recent-activity log. A missing or mismatched owner descriptor,
  token-authenticated health response, or service identity appears as
  unverified; controls that could affect the service stay disabled.
- **Open Office in Chrome:** Opens the fixed local Office URL. If Chrome cannot
  be opened, the controller asks whether to use the default browser instead.
  Closing that browser does not stop Office.
- **Stop:** Requests a safe drain. Active work is allowed to reach a safe
  boundary; unknown ownership or work state refuses the stop. A pending stop
  can be cancelled before shutdown commits. Queued work remains saved.
- **Restart:** Starts again only after the owned service confirms a safe stop.
  A pending or refused stop does not trigger a second service.
- **Startup recheck:** If the spawned service has not yet proved its identity or
  health, **Recheck owned service** retries that check against the already
  tracked child. It never spawns another process or terminates one. Stop and
  Quit remain unavailable while ownership or health is unverified.
- **Preserve an unverified child:** If repeated startup checks cannot establish
  identity or health, the explicit **Leave child running and quit controller**
  action asks for confirmation, records the exact child PID in controller
  settings, and exits without sending a stop or kill request. After relaunch,
  the saved safety lock disables Start and other service controls. Clear it only
  after manually verifying that the recorded process has stopped; clearing the
  lock does not terminate or attach to any process.
  On a later launch, **Quit controller and keep safety lock** exits while
  retaining that block if you are not ready to clear it. This bypasses the
  normal drain only for the controller UI; Office work may remain active and
  will no longer be monitored by this window.
  Settings writes retain the previous `controller.json` as `.bak` until the
  staged `.tmp` is promoted. If either journal file remains after interruption
  or an I/O failure, startup and live controls refuse to proceed, even if the
  main file exists. Keep all three files for operator recovery; do not delete
  them or reset settings while a preserved child may still be running. Recovery
  requires verifying that child has stopped and reviewing the retained settings
  with the release owner; the controller does not guess which copy is current.
- **Close window:** Hides the window in the notification area; Office keeps
  running. Use the tray menu to show the window again.
- **Quit Office:** Requests the same safe stop before exiting. If work is busy,
  pending, or unverified, the controller stays open in the tray.

## Updates and support information

The controller has no automatic application self-update. Component updates in
the web UI require explicit local source/trust configuration, inventory, a reviewed
preview and explicit Apply. See ../docs/operations/office-update-host.md.
Applied files remain pending activation until loading evidence is recorded.
Installing this controller alone does not configure trusted update sources.

There is no support-bundle export in this build. For a support report, copy the
displayed service state, safety state, counts, reason, and recent-activity lines
manually, then review them for local project paths before sharing. Do not send
`controller.json` or `desktop.json`: controller settings contain local paths,
and the service descriptor contains its authentication token. The service log
is bounded and redacts known token/secret fields, but review its contents before
sharing.

## Maintainer build notes

Install the pinned frontend dependencies with npm ci in desktop/. Build the
sidecar with build-sidecar.ps1, run cargo test --locked --manifest-path
src-tauri/Cargo.toml, then npm run build. The committed Cargo.lock records the
resolved native dependencies. These commands require the Windows build tools;
regular users download the packaged installer instead.

Validation is local. No hosted CI build is required or automatically dispatched
for this publication. Generated binaries, icons and native build outputs are
excluded from source control; release binaries are attached separately.


## Packaged profile regression check

The 0.1.2 service includes `tomllib` and the profile helper import graph in its
frozen bundle. Existing projects can load both the root Manager helper and the
separately installed agent helper without a Python installation.

After building, run the following with the **synthetic** Codex protocol fixture
(the check refuses a real Codex executable). It creates temporary profiles using
the actual repository helpers, verifies authenticated health, the bundled
Projects visibility asset and safe shutdown, and removes only its own processes:

```powershell
python -B desktop/tests/smoke_packaged_profiles.py --sidecar <built-office-service.exe> --codex-fixture <fixture_codex.exe>
```

This check does not claim real Desktop/provider, interactive installer, tray or
uninstall acceptance. The native controller's startup failure message remains
generic; richer sanitized diagnostics are a separate follow-up.


## Controller 0.1.3 startup and landscape layout

The wide desktop window places service controls and project diagnostics in two
columns, with a compact single-column fallback. Start uses a preissued secret
to bind the service worker to its actual launcher when their PIDs differ.
Authenticated status has a ten-second read budget. Unknown shutdown safety
permits opening a healthy Office in Chrome while Stop and Restart remain disabled.

The native regression runner is `desktop/tests/smoke_native_packaged.py`. Compile
`desktop/tests/fixture_codex.c` to a scratch `fixture_codex.exe`, build the service
and Rust test executable, then run:

```powershell
python -B desktop/tests/smoke_native_packaged.py --rust-tests <rust-test-executable> --sidecar <built-service.exe> --codex-fixture <scratch/fixture_codex.exe>
```

This explicitly executes the otherwise ignored native packaged test for both real
helper layouts, covering actual Rust startup, Running controls, safe stop and
second-launch recovery on an isolated port. Browser UI mocks are a separate check.
Synthetic evidence does not establish real provider or interactive installer,
tray and uninstall acceptance. Existing profiles still require explicit update
preview and Apply; installing the controller alone does not update profiles.
