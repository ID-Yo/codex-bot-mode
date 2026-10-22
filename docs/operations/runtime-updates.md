# CBM-owned runtime version slots

`scripts/runtime_update.py` is a local foundation for updating files under one
explicit CBM-owned installation root. It does not update an existing executable
in place, write project data, change global software, fetch releases, or terminate
processes. Release envelopes and bundles are checked with
`component_release.verify_bundle`; executable ownership still comes from the local
publisher `component_paths` policy.

Initialize only an empty directory dedicated to this application:

```text
python -B scripts/runtime_update.py init --install-root <owned-runtime-root> --owner codex-office
python -B scripts/runtime_update.py prepare --install-root <owned-runtime-root> --manifest <signed-manifest.json> --bundle <bundle.zip> --trust <local-trust.json> --platform windows-x86_64 --component office
python -B scripts/runtime_update.py activate --install-root <owned-runtime-root> --plan <plan-sha256> --approve <exact-plan-sha256>
python -B scripts/runtime_update.py recover --install-root <owned-runtime-root>
python -B scripts/runtime_update.py rollback --install-root <owned-runtime-root> --component office --slot <owned-version-slot> --approve <exact-rollback-plan-sha256>
```

`prepare` validates a frozen local bundle, stages each selected tool/application
under `versions/<component>/<version-release-content-id>/`, verifies each file,
and stores the approved plan under `.plans/`. A partial `.staging-*` directory is
never referenced by `current.json`; `recover` removes only incomplete staging
directories carrying this root's ownership marker. Completed slots remain
available for rollback. Project files, identity, memory and data directories are
outside the root boundary.

The library `activate(root, plan, approve, adapter)` requires the exact plan hash.
The host-owned adapter contract has four methods: `status()` returns
`ownership: owned`, `state: idle`, `paused: false`, and `pending_writes: false`;
`stop_owned()` safely stops only this owned service; `start_owned(slot_path)`
starts that application slot; and `health_check(slot_path, release_id,
slot_sha256)` returns a matching healthy identity. Missing, busy, paused or unknown
state defers activation. This module never accepts executable commands or process
IDs. The CLI has no host adapter and therefore reports `deferred` for application
activation/rollback; Desktop/controller integration remains a separate #156 gate.
The adapter receives only a copied, hash-verified plan target; mutating a caller's
plan during a status callback cannot redirect activation. If candidate shutdown is
not positively confirmed after failed health, the result is `recovery_required`
and the previous slot is not started alongside it.

Application activation starts and health-checks the candidate while the old
`current.json` remains authoritative, then atomically switches the pointer after
health succeeds. Failed checks retain the old pointer and restart/check the old
slot. Tool-only updates change the component pointer without restarting the
application. Exact-plan approval is required for rollback; normal prepares reject
downgrades. A runtime lock rejects overlapping operations, and a repeated apply
of the active slot is a no-op. All pointer writers use `commit_current()` and its
compare-and-swap under `.current-pointer.lock`; competing pointer updates are kept
and cause activation to defer/recover rather than overwrite them.

The signed package validator, fake owned-service adapter and synthetic fixtures
cover verified staging, ownership/containment, interruption recovery, pointer
rollback, readiness deferral, exact rollback approval, idempotence and the rule
that foreign processes are never targeted. This foundation does not prove a real
Office service adapter, installer bootstrap, production signing, or live upgrade.
