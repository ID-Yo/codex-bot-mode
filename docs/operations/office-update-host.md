# Configure Office updates

Office stays unconfigured until the operator creates `.bot-mode/local/office-updates.json` in the served project. This is local configuration, never a downloaded policy or browser request. It requires exactly:

```json
{
  "schema": 1,
  "platform": "windows-x86_64",
  "source": "D:/operator/release-source.json",
  "cache": "D:/operator/cbm-cache",
  "trust": "D:/operator/publisher-trust.json",
  "projects": {"chosen-project-id": "D:/chosen-project"},
  "installed_versions": {}
}
```

Source/cache/trust paths must be absolute. Every project ID and root must already match the Office registry; the update host cannot invent an arbitrary project or silently select all projects. Source config contains schema, manifest_url and bundle_url. Publisher trust is locally pinned and is never accepted from an artifact. Installed versions must be explicit release-component mappings; do not reinterpret legacy catalog integers as semver automatically.

Startup reconstructs verified cached releases and durable transaction status without network or agent calls. Check verifies remote metadata; Download verifies and stages a selected release. Inventory, preview and exact-digest Apply use the same transaction engine as the CLI. Apply is explicit and does not mean activation. Malformed/incomplete configuration is an error; it never falls back to an untrusted release or path.

This integration uses the accepted cache and transaction APIs. Operator-supplied trust is required. No publisher private key, installer signing identity, auto-start, global software update or automatic profile application is included.

Validation: four host construction tests pass. A signed synthetic HTTPS fixture completed check, stage, registration and restart replay. A curated export audit returned zero findings and the complete exported updater import graph loaded. These are isolated fixtures; no live installation or native agent was changed.
