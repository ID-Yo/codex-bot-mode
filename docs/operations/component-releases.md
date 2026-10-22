# Trusted component release contract

Issue #153 / task #161, engine schema 1. This contract authenticates releases;
it does not apply files, create publisher keys or claim a public update exists.
The profile transaction and desktop packaging stories consume it separately.

## Publisher and local trust

Use Ed25519 through the pinned `requirements-updates.txt` dependency. Normal Office
does not import it; the update verifier fails closed if it is missing. Package the
dependency in the desktop sidecar; a developer may install it in a dedicated venv.
Do not implement cryptography or use an artifact checksum as publisher identity.

The release envelope is exactly `payload`, `key_id`, `signature`. Signature is
base64 Ed25519 over `component_release.canonical(payload)`: UTF-8, sorted JSON keys,
ASCII escapes, no whitespace, no nonfinite numbers. Reject duplicate JSON keys.
Use `Ed25519PrivateKey.sign` in a controlled publisher job; never put private keys
in a release, repository, log or user profile. Production key provisioning is an
explicit publisher operation; no publisher secret has been created by this work.

The installed local publisher policy has `schema: 1`, `keys` mapping approved key
IDs to base64 raw 32-byte public keys, `revoked` key IDs, `download_hosts` and
`component_paths` for executable components. Trust is bootstrapped by an explicitly
approved installer/publisher identity, never from the downloaded package. Empty
trust means updates unavailable, not an invitation to trust the first download.
Tool/application allowlists are per component ID, not per arbitrary package.

Key rotation requires an existing trusted application update or an explicit owner
policy change to add the next public key, then a verified transition before retiring
the old key. Revocation is local policy delivered through that same trusted route;
offline clients cannot claim knowledge of unseen revocations. Show policy freshness
and do not automatically accept a key advertised by an untrusted endpoint.

## Payload schema

Required fields: `schema: 1`, a stable `release_id`, immutable 40-hex public
`source_revision`, timezone-aware `published_at`/`expires_at`, integer
`minimum_engine`, human-readable `changelog`, and `components`.

Each component has `id`, `kind` (`profile`, `reference`, `tool`, `application`),
numeric three-part `version`, `platforms` (for example `windows-x86_64` or `any`),
`requires` mapping component IDs to exact compatible versions, and `files`.
Each file maps its component-relative path to `sha256` and uncompressed `size`.
ZIP entries are exactly `<component-id>/<file-path>`; no extra files, directory
entries, links, special files or install hooks. Versions are independent of
existing integer role template versions; do not infer compatibility from those.

Profiles may deliver BASE/SOUL/PROFILE/PLAYBOOK only, always through later local
baseline/conflict checks. References are narrowly limited to README.md, index.yaml,
methods/*.md and templates/*.md (including ordinary subdirectories); private owner,
state/approval paths and hidden segments are excluded. Other namespaces require
an explicit reviewed schema change, not an extension-based guess. References do
not deliver profile or project configuration. Tool/application files require the local per-component file allowlist.
Identity manifests, STATE, learned/native memory, credentials, owner approvals and
project configuration are outside this content contract. A signed package alone
never authorizes overwrite, activation or widening permissions.

Manifest/policy JSON is at most 1 MiB; at most 128 components and 4096 files;
each expanded file is at most 64 MiB and total content at most 512 MiB. Windows
ambiguous/reserved, traversal, absolute and case-colliding paths are rejected.
Compatibility rejects missing dependencies, wrong platforms and unsupported
schemas before target writes. Downgrade is refused; a separately reviewed rollback
transaction must preserve current user state rather than abusing a normal update.

## Verification and discovery

```
python -B scripts/component_release.py manifest.json bundle.zip --trust publisher.json --platform windows-x86_64 --installed versions.json
```

Success reports `verified` and `applied: false`. Failure returns exit 2 and
`unavailable`, never `up to date`. The library `fetch_verified` checks an explicit
HTTPS manifest/bundle pair from approved hosts with bounded downloads and verifies
before returning; download scratch is removed and no installation is changed.
It rejects redirects to hosts outside local policy. No network is needed for
verification of an already downloaded pair, but expiry still applies. A cached
manifest must be reverified; cache age/source freshness and update availability
are distinct. The UI/discovery story owns persisted cache/version presentation.

The publisher must build from a frozen public curated export with zero privacy
audit findings, calculate each file hash/size, sign canonical payload, and publish
the matching immutable manifest and ZIP together. No private development history
or installed owner profiles are release input. Publishing a newer handbook can
advance only the reference component while keeping the runtime unchanged.

The curated source export includes `scripts/runtime_update.py`,
`scripts/office_runtime_adapter.py`, their runtime/adapter operations guides, and
the pinned `requirements-updates.txt`. The adapter supports verified source slots
on an existing Python host; these exported files do not establish native EXE,
Tauri, installer or live-update support. `tests/test_component_export.py` checks
the actual curated export for zero audit findings, imports its update/launcher
graph in an isolated interpreter, and runs a signed synthetic source slot using
the exported adapter and HTTP/lifecycle code with stub controllers. Its signing
key is ephemeral and its process/data belong only to a temporary test project.

## Verification and remaining integration

`python -B -m unittest discover -s tests -p test_component_release.py -v` uses
ephemeral test-only keys. Cases include tampering, wrong/revoked keys, incompatible
schema/platform/dependencies/downgrade, expired/future metadata, ownership, Windows
paths, archive corruption/links/extras, byte limits and unavailable network.
The verifier never extracts a ZIP or changes an installation; the transaction
story must separately revalidate target containment and symlink/junction state.

Production publisher trust/bootstrap, actual release publication, a signed
installer and application update are not claimed by these test fixtures. They
remain acceptance gates of #153/#163/#159 and must not be closed from unit tests.

API reference: [cryptography Ed25519](https://cryptography.io/en/latest/hazmat/primitives/asymmetric/ed25519/),
checked 2026-10-21. The library provides signing and verification; canonical
manifest, ownership and compatibility policy here are CBM's application contract.
