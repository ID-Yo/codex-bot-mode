# Local update workflow adapter

`scripts/component_update_service.py` is a partial #157 foundation. It adapts
the same planner/applier API for CLI/Office callers, with explicit configured
project identities, verified release registration, immutable copied previews,
exact plan-digest approval and conflict refusal. It never dispatches agents,
executes commands or accepts an arbitrary project path from the UI.

The adapter receives trusted host configuration and authenticated calls. It does
not provide an HTTP/authentication boundary itself. The later Office endpoints
must enforce loopback Host/Origin/token/session rules before invoking it. Never
expose `register_download` with client-controlled file paths or trust policies.

`register_download` verifies all bundled files. `preview` calls the real
transaction planner and retains an opaque preview handle. `apply` sends the
unchanged plan, approval digest and verified component inputs back to the same
engine for its own lock/hash/context validation. Failed apply never records
success. Success is `applied_pending_activation`, not proof of agent loading.
Repeated use of an already accepted preview does not execute another apply.

The current handles/history are process-local. The transaction engine's journal
is the durable authority; rebuilding the UI adapter must discover those journals
before presenting persisted progress. That reconciliation, Office endpoint/UI,
release check/download cache and lifecycle integration are remaining work. An
adapter restart makes old handles unavailable and requires a new preview; do not
report an empty process history as no installed updates.

Tests use injected planner/applier failures and synthetic scope/approval inputs.
An additional local integration check exercised the actual `profile_update`
planner/applier against its signed synthetic fixture and verified rendered
profile bytes and pending-activation result. No live project was updated. This
adapter is not a completed Updates view, and #157 remains open.
