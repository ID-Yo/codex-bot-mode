# Host release discovery and cache

This host-only layer joins the signed release verifier (issue #153) to the
future Office updates view (issue #157). It checks compatible release metadata,
stages a verified envelope and bundle in an explicitly managed cache, and
enumerates cached releases after restart. It never extracts a bundle, writes an
installation, activates an agent or creates publisher trust.

## Stable Python API

The Office host owns the config and cache paths, obtains its pinned publisher
policy and installed-version map from host-local policy, and passes those values
separately:

```python
from scripts.component_update_host import HostCache

host = HostCache(config_path, cache_path, platform)
check = host.check(trust=local_trust, project_map=host_component_versions)
if check['state'] == 'verified':
    entry = host.stage(check['release_id'], trust=local_trust,
                       project_map=host_component_versions)
entries = host.verified_entries(trust=local_trust,
                                project_map=host_component_versions)
status = host.status()
```

`project_map` maps component IDs to explicit numeric `major.minor.patch`
versions. It is a compatibility input, not inventory discovery: legacy profile
integers (for example, profile 2 and reference 1) are not converted to SemVer.
The host must omit unknown versions or supply a separately approved mapping;
the verifier keeps its existing downgrade and dependency rules.

`check()` downloads only the manifest. On success it returns `state=verified`,
the authenticated `payload`, release ID, component versions, source revision,
manifest hash, `downloaded=false` and `applied=false`. Its durable last-check
record contains discovery metadata, not the payload, URLs, trust or local paths.
An unavailable or untrusted source returns `state=unavailable`; it never means
up to date. A successful check is required before staging that release ID.

`stage(release_id, ...)` downloads and verifies the envelope and bundle, then
atomically commits both with cache metadata under `<cache>/verified/<release_id>/`.
It returns `VerifiedEntry(release_id, payload, envelope, bundle_path,
applied=False)`. `bundle_path` is a host-side path; an Office endpoint must not
serialize or accept it. `verified_entries(...)` enumerates all durable entries
and re-verifies their local hashes, current signature trust, platform, component
compatibility and archive contents on every read. Trust and project versions are
inputs to verification and are never returned or persisted into the cache.
`status()` returns last-check/failure metadata
and cached-entry inventory with byte-check metadata. Call `verified_entries(...)`
before consuming cached payloads because `status()` alone does not check publisher
revocation, expiry, platform or installed-version compatibility.

UI operations accept only a selected `release_id`; configured URLs, trust files,
project-version maps and cache paths remain host-owned inputs. Do not treat the
release's signed `requires` field as an installed-project map. If local version
provenance is unknown, preserve that unknown state and keep the release
unavailable pending an explicit compatibility policy.

## Host configuration and CLI

The host-local source config is a JSON object with exactly these fields:

```json
{"schema":1,"manifest_url":"https://releases.example/manifest.json","bundle_url":"https://releases.example/bundle.zip"}
```

Both URLs must use HTTPS and a host in the separately pinned trust policy's
`download_hosts`; every redirect is checked by the release verifier. The
existing release limits apply: 1 MiB manifest, 512 MiB total package, 64 MiB
per file, 4096 files and 128 components. Cache directories/files reject
symlinks, Windows junctions and other reparse points. A failed check or stage
does not replace a prior cached entry. Reuse verifies the existing entry and
rejects a changed manifest or bundle under the same release ID.

```powershell
python -B scripts/component_update_host.py check --config source.json --cache update-cache --trust publisher.json --project-map installed-versions.json
python -B scripts/component_update_host.py stage fixture-release --config source.json --cache update-cache --trust publisher.json --project-map installed-versions.json
python -B scripts/component_update_host.py status --cache update-cache
```

The `check` and `stage` CLI commands output discovery/staging metadata only;
they do not print trust, project maps, envelope contents or bundle paths.
The target platform defaults to `windows-x86_64` and can be overridden with
`--platform` for an explicitly supported host target.
`stage` reports `downloaded=true` only after a verified cache commit and always
reports `applied=false`. Interruptions remove the partial staging directory and
return an unavailable result from the CLI. A successful check and staged entry
survive process restart. `status` reads the persisted cache inventory; Python
`verified_entries(...)` revalidates every entry before use. No production source,
publisher trust, installer, Office endpoint or live update has been provisioned
by this host module.

## Verification

```powershell
$env:TEMP = 'D:\Temp'; $env:TMP = 'D:\Temp'
python -B -m unittest discover -s tests -p test_component_update_host.py -v
```

The focused synthetic HTTPS tests use ephemeral Ed25519 fixtures and cover
manifest-only checks, staging/restart enumeration, offline and untrusted sources,
same-ID byte changes, interrupted downloads, cached-byte re-verification and
symlink rejection. They do not exercise production publisher bootstrap, Office
HTTP/CSRF/origin protection, installation transactions, Windows distribution,
activation or a public release.

The managed cache is limited to 32 verified releases and 2 GiB total (metadata, manifests and bundles). Status and reuse reject excessive inventories before hashing all payloads; new stages refuse a full cache before final commit. Operators explicitly remove reviewed old release directories; no automatic deletion occurs. Network and filesystem failures persist generic categories, never raw URL query strings or private paths.

A cross-process OS-held commit lock encloses the final capacity check and cache rename. Concurrent stage commits return a controlled busy error; process exit releases the lock. Payload downloads remain separate and are never considered staged before commit.
