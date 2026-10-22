# Component activation evidence

The pure transition helper in `scripts/component_activation.py` supports #158.
It is a partial foundation, not an installed Desktop integration or dispatcher.
Callers must persist the returned record through their owned transaction lock.

After files commit, `pending(component, revision, consumers)` creates a record
with an exact installed content digest and explicit current bot/thread consumers.
For shared references all affected roles must appear. File copy means
`pending_activation`, never `verified_active`.

`eligibility` accepts only fresh explicit idle evidence from the correct binding,
no pending writes, no owner pause and known native/Office ownership. Missing,
stale, future, busy or paused evidence defers activation. Its success explicitly
does not authorize dispatch: use the existing authorized normal turn path, never
a competing AppServer, replacement chat or automatic wakeup on discovery.

`acknowledge` accepts an exact revision/component/bot/thread/completed-turn
readback supplied by a trusted host adapter, compared with newly hashed installed
files and the current roster binding. A chat assertion alone is insufficient.
The `host_verified` flag is an internal adapter contract, not a capability or
authentication primitive. A public endpoint must never accept this flag from an
arbitrary request or convert agent text into host verification.

Per-consumer state stays visible; one successful role gives `partial_activation`
until every affected role is evidenced. A newer installed digest or replaced chat
invalidates older evidence. Repeating the identical receipt is idempotent.
The helper does not change model settings, ownership, holds, files or processes.

Remaining integration: durable transaction storage and inventory mapping; actual
host readback producer and authentication boundary; UI display and authorized
next-turn loading; runtime health/version activation in the desktop lifecycle.
No live agent has been reloaded by these fixtures, and #158 stays open until that
integration is independently accepted.
