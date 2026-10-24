# Codex Office desktop controller

This is a small Windows tray controller for the existing Codex Office Python
service. It starts the service bundled beside the Tauri application; it does
not install Python system-wide or replace the existing Office web interface.
The web interface remains in Chrome at `http://localhost:24242`.

## Build and artifact status

The repository contains a Windows workflow intended to build an x64 PyInstaller
sidecar and a per-user NSIS installer, then retain them as private Actions
artifacts. That is an optional remote build path, not evidence that an installer
exists. Under the current local-only CI policy, automatic remote runs are
disabled and the workflow has not been run successfully.

The native Tauri build has not been compiled. Rust/Cargo, MSVC, and the Windows
SDK are absent from the checked local toolchain locations, so the native
compiler is the current local build blocker; no workstation toolchain was
installed. At the 2026-10-21 check, a fresh PATH and standard toolchain
locations had no Cargo, rustc, MSVC, Windows SDK, or NSIS. Available disk space
was about 0.02 GiB on C:, 8.83 GiB on D:, and 66.25 GiB on E:; recheck before
planning a local build because these values change. Earlier optional Actions
runs were rejected before starting a runner because GitHub reported an account
payment or spending-limit block. Those failures are separate from the missing
local compiler and provided no compile evidence. The local PyInstaller check
produced a sidecar only. No NSIS installer exists from that check. Wait for a
locally available approved toolchain or an explicitly authorized remote build,
a successful native build, and an explicit trusted-install decision before
installing anything.

The NSIS package is configured for the current Windows user. No service,
elevation, login autostart, or automatic update is configured. Codex Desktop
must already be installed; the controller searches for its `codex.exe` and
offers a file picker if it cannot find it.

### Installation when a release is approved

1. Obtain the signed installer from the approved private distribution location
   named by that release's instructions. Verify its publisher and checksum
   against the release evidence.
2. Do not install the unsigned PR or Actions test artifact described above.
   If Windows reports an unexpected or invalid signature, stop and contact the
   release owner instead of bypassing the warning.
3. Run the verified installer as the current user. It does not require
   elevation. Launch **Codex Office** from the Start menu when you choose to
   use it; installation does not enable login autostart.

## First run

1. Start **Codex Office** from the per-user Start menu shortcut.
2. Choose the project folder that contains the Office profiles you want to
   serve. The controller stores its service runtime under that project's
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

This controller has no automatic update mechanism. Do not replace the
application or bundled service with an unsigned build. Updates require a
reviewed release and a verified signing/trust process; that process is not
configured by this draft.

There is no support-bundle export in this build. For a support report, copy the
displayed service state, safety state, counts, reason, and recent-activity lines
manually, then review them for local project paths before sharing. Do not send
`controller.json` or `desktop.json`: controller settings contain local paths,
and the service descriptor contains its authentication token. The service log
is bounded and redacts known token/secret fields, but review its contents before
sharing.

## Maintainer build notes

The **Codex Office desktop (Windows)** workflow is manual-only and is not a
current validation route under the local-only CI policy. It is not being
dispatched. A future authorized Windows runner could resolve the Cargo lockfile,
install locked npm dependencies, build the sidecar, compile Tauri, and create
an unsigned test installer. Do not infer any of those outputs from the workflow
definition. The local sidecar `--help` smoke check proves only that the
packaged CLI starts; it does not prove service startup, authenticated health,
tray behavior, safe shutdown, or native compilation. Verify those separately
before treating a package as accepted.


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

## Stale service state and upgrades

If the service named in `desktop.json` is gone after a forced stop or an upgrade, the controller moves `desktop.json` and `running.json` aside as `desktop.stale-<UTC time>.json` and `running.stale-<UTC time>.json`, shows **Not started** and enables Start. A descriptor counts as stale only when its process no longer exists (or is not an `office-service` program) and nothing accepts connections on its port. A live but unresponsive listener, a live Office service, an unreadable descriptor and a clean-stop record are left untouched and keep the controller **Unverified** where they did before.

The installer runs `installer/stop-previous-office.ps1` before it replaces or removes files. The script sends the same authenticated `/desktop/shutdown` request as the Stop button and waits for the service to exit. It never terminates a process; if Office is busy, still draining or unresponsive, the installer stops with a message asking you to use Quit Office first.
