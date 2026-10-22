# Durable update status

`component_update_status.read_history(configured_projects)` reads bounded transaction journals from configured project roots after a host/browser restart. It never applies, recovers, rolls back or wakes agents. It returns only project/transaction/component IDs, phase and plan digest; file content, backups, memory and credentials are excluded.

Applied journals become `applied_pending_activation`. No file journal proves a running agent loaded the revision. Interrupted stages remain pending and need the recovery CLI. Rollback and failed evidence remain distinct. Missing transaction folders are empty; unreadable, linked, malformed or excessive evidence produces partial coverage and warnings rather than a fabricated clean state.

The helper is a read-only projection, not journal authentication or permission to execute. The transaction engine revalidates journals and backups before recovery. Limits: 256 entries per configured project and 4 MiB per journal. Actual Office host construction uses this alongside the shared update service; no private paths are accepted from browser requests.
