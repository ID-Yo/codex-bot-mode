# Safe profile and shared-reference updates

`scripts/profile_update.py` plans and applies a verified signed release to
explicitly mapped schema-1 installation records. It updates only profile
`agent/BASE.md`, `SOUL.md`, `PROFILE.md`, and `PLAYBOOK.md` files and validated
shared-reference files. It never runs package code, install hooks, tools, or
activation. A successful copy is recorded as `pending_activation`; it is not
evidence that a Desktop profile loaded.

## Target mapping and supported layouts

Provide a schema-1 target map that connects release component IDs to inventory
component IDs. The inventory supplies each managed root, installed version,
owned paths, and baseline hashes:

```json
{
  "schema": 1,
  "targets": {
    "manager-profile": "profile:team/Development/manager",
    "editorial-handbook": "reference:editorial"
  }
}
```

Map dependencies too when their installed versions are in the project
inventory, even if they are not selected for writing. Inventory versions are
translated to release component IDs before `component_release.verify_bundle`
checks dependencies and downgrade protection. Any additional `installed`
map passed to the Python API must also be keyed by release component ID.
Schema-1 shared-reference records may also carry `dependents`, the captured
list of dependent role names. The updater preserves this inventory field.

A normal `team/...` or roster-managed `agents/...` profile must have a matching
active roster participant and readable `agent/agent.yaml` with matching
`bot_id` and `role`. Original answers in that file are read only for rendering;
unresolved placeholders stop planning.

Two legacy roots follow the existing `scripts/team_room/project.py`
`profile_location()` rules. An inventory root of `.` is accepted only when the
canonical root `agent/` profile exists and an active roster participant names
workspace `.`. Its verified package paths map to `agent/<file>` in the project.
The exact `agents/project-manager` root is accepted as the legacy flat Manager
profile when the active `manager` roster entry selects that path, or when the
project's legacy Manager fallback selects it. Its verified `agent/<file>` paths
map to direct `agents/project-manager/<file>` targets. A present `agent.yaml`
is checked and hashed as read-only identity/rendering context. Without it, the
root-export layout requires roster identity; the flat fallback uses only its
fixed Manager identity and any template placeholder without a verified value
stops planning. Map the release component to the corresponding adopted
inventory ID (`profile:.` for root export or `profile:agents/project-manager`
for flat Manager); the target map never accepts a filesystem path. Other roots
are not inferred from directory names or searched. Without a roster, flat
Manager metadata must use the fallback `project-manager` bot ID and Manager
role; otherwise identity review fails.

All layouts can write or delete only `agent/BASE.md`, `SOUL.md`, `PROFILE.md`,
and `PLAYBOOK.md` through the selected layout mapping. Identity metadata,
`STATE.md`, memory, roster, chat bindings, credentials, and other profile files
remain protected. Shared references must map to exactly
`team/references/<bundle>`.

## Plan and apply

The library API is:

```python
plan = plan_update(project, envelope, bundle, trust, platform, targets, components,
                   legacy_version_resolutions=None)
result = apply_plan(plan, approved_plan_hash, envelope, bundle, trust, platform, targets)
recover(project, transaction_id)
rollback(project, transaction_id)
```

`plan_update` verifies the signed envelope and every archive entry, then returns
the target mapping, proposed file hashes, unified diffs, conflicts, reference
dependents, and `plan_sha256`. Profile targets include `profile_layout` and
`profile_root` in the approved plan and journal so recovery validates the same
canonical-to-project path mapping. The pending activation target path is the
recognized profile directory (`agent` for root export and the inventory root
for flat Manager). Unknown or missing baselines are manual-review
conflicts; a caller can resolve one only by explicitly approving replacement
against the exact current file hash shown in the conflict. Existing files
outside known unchanged ownership are not adopted as clean. New files are
eligible only when the destination is absent. Deletion requires an explicit
`delete_paths` entry for a known, unchanged owned file omitted by the release;
profile deletions are limited to the same four-file allowlist as writes, and
reference deletions must pass the verified reference package-path policy.

### Explicit legacy-version mapping

Schema-1 inventory may contain legacy integer versions such as profile `2` or
reference `1`. These values are not semantic versions, and the updater never
converts them automatically. An initial plan reports a candidate with the
inventory digest, inventory and release component IDs, exact legacy integer,
authenticated release ID and declared version, and a projection digest bound to
the current target files, baselines, original profile context, manifest and
bundle. Review that candidate and, if the compatibility decision is accepted,
pass `inventory_sha256`, `component`, `legacy_version`, `release_id`,
`declared_version` and `projection_sha256`, keyed by inventory component ID,
in a JSON file:

```text
python -B scripts/profile_update.py plan ... --legacy-version-resolutions reviewed-legacy-map.json
```

The mapping acknowledges treating this legacy installation as compatible with
the named verified release version for this update. It does not claim that the
historical integer meant that semantic version. Mapping fields and the
projection are checked against authenticated release metadata and the current
inventory/files. The mapping is included in the plan hash and reused from the
approved plan at apply; changed files or inventory require a new review.
Unknown file baselines remain independent manual conflicts and require their
own exact-hash replacement decision.

`apply_plan` snapshots caller-owned JSON inputs, requires the exact reviewed
plan SHA-256, and applies only the verified reconstructed snapshot. It
reconstructs the plan before staging and again under the update and inventory locks. It uses the
schema-1 writer's `.bot-mode/local/installations.lock`, checks the inventory
digest before target commit, stages new bytes and backups, applies files, then
atomically commits updated baselines and component versions. The journal makes
interrupted work recoverable. `recover` completes an already committed
transaction or restores its pre-update files. `rollback` restores a completed
transaction only while all installed targets still have their recorded
post-update hashes and all backups pass integrity checks.

Updater locks use exclusive Windows handles with delete-on-close, so normal or
abrupt process exit releases the same `profile-updates.lock` and inventory
`.lock` paths. A pre-existing lock file, including the inventory writer's legacy
empty marker, is treated as active or unknown and is never deleted or overridden.
Other operating systems are not supported by this lock implementation.

The CLI exposes the same flow:

```text
python -B scripts/profile_update.py plan --project <root> --manifest <envelope.json> --bundle <release.zip> --trust <publishers.json> --targets <targets.json> --platform windows-x86_64 --component manager-profile
python -B scripts/profile_update.py apply --plan <reviewed-plan.json> --approve <exact-plan-sha256> --manifest <envelope.json> --bundle <release.zip> --trust <publishers.json> --targets <targets.json> --platform windows-x86_64
python -B scripts/profile_update.py recover --project <root> --transaction <uuid>
python -B scripts/profile_update.py rollback --project <root> --transaction <uuid>
```

The apply result and durable journal retain the manifest hash, bundle hash,
approved plan hash, release ID/revision, target inventory and release IDs,
installed version, and exact new file hashes. Each target's `installed_sha256`
is SHA-256 of the canonical JSON object mapping project-relative target paths
to their file SHA-256 values. `pending_activation.activated` remains `false`.

## Shared-reference dependents

For each selected reference, the plan preserves its recorded dependent roles
and resolves those roles to active roster participants only when each
participant's workspace and `agent/agent.yaml` identity match. It reports the
roster ID, workspace, profile inventory ID when present, and installed version.
Unresolved roles, a missing role list, or an unavailable roster are reported in
`unknown`, with `coverage: "unknown"`; the plan never claims those consumers
were resolved. This union of inventory roles and verified current roster IDs
does not use text search. The resolver reads only the roster and profile
identity metadata, and does not read memory, STATE, credentials, or arbitrary
project files.

## Validation

The focused synthetic suite uses generated test-only signing keys and temporary
projects:

```text
python -B -m unittest discover -s tests -p test_profile_update.py -v
```

It covers inventory compare-and-swap and lock handling, unknown-baseline
resolution, exact-plan staleness, safe paths, idempotence, recovery fault
points, backup integrity, protected files, and rollback guards. It does not
perform a live project update or validate profile activation. Integer legacy
profile and shared-reference versions and both legacy profile layouts are
exercised with generated synthetic signing keys; historical versions are never
inferred. No live main Manager or exported profile was read or changed.
