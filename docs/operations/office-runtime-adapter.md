# Office owned-runtime adapter

`scripts/office_runtime_adapter.py` implements the #155 service adapter for a
verified `office` application slot. It launches only the fixed
`scripts.team_room` package entrypoint from that slot, using the already-running
trusted Python host (`sys.executable`) in isolated mode. The Python executable
is not accepted from the slot or from a browser request. The Codex CLI path is
provided by the trusted local host configuration and is passed as one argument;
no shell or arbitrary command string is used.
Frozen sidecar executables are explicitly rejected as interpreters. This source
adapter does not prove or supply native executable update support.

Before spawning, the adapter validates the runtime ownership marker, full slot
manifest/content hashes, application component identity, release ID, slot hash,
and packaged `scripts/team_room/__main__.py` entrypoint. Slots must contain only
manifest-listed files plus the updater's `slot.json` and `.stage.json` metadata;
bytecode and `__pycache__` are rejected so Python cannot import unverified code.
Office startup repeats
the slot validation and passes a verified identity object into
`DesktopLifecycle`. Legacy launchers may still start without these options, but
their health response omits runtime identity and this adapter will not manage
them.

The controller descriptor and authenticated `/desktop/health` response expose
the parent PID, child PID, instance ID, component ID, release ID, slot digest,
slot-relative path, and verified entrypoint. Adapter operations bind all of
those values to the exact `Popen` child it created. Health additionally requires
Office's existing `/api/state` response shape. Status checks its durable `hold`
flag and defers while paused or when pause state is unavailable. Shutdown uses the existing authenticated
`/desktop/status` and `/desktop/shutdown` lifecycle; it returns success only
after a safe status, an accepted shutdown, exit of that exact child with code zero,
and a matching stopped descriptor. Busy,
paused, unknown, replaced, or unattached instances defer without signaling a
process.

The adapter never attaches to or stops a legacy/external Office process. The
Office application host must keep one adapter instance across launch, update,
and shutdown operations; a fresh unattached adapter reports ownership unknown.
Synthetic tests run signed fixture slots, the actual HTTP handler/lifecycle with
stubbed controllers, and a two-slot activation through real child processes.
No live Office runtime, global software, project data, or
foreign process is changed by those tests. Native packaging/installer identity
and Tauri wiring remain separate integration checks.

For local verification, run the adapter, lifecycle, launcher, runtime-updater,
component-release and web unittest suites with process-local `TEMP`/`TMP` on a
drive with adequate space. All subprocesses in these tests are synthetic fixtures;
test cleanup retains their exact `Popen` handles and removes only those fixtures.
