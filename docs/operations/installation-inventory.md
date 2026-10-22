# Installed Component Inventory (preview/adoption stage)

The inventory is local project metadata at `.bot-mode/local/installations.json`.
It describes components only; it is not a release manifest, trust store, update
authorization, install plan or proof that an agent loaded its current profile.
This first stage does read-only discovery, records baselines for new catalog
installs, and explicitly adopts legacy components as unknown-baseline metadata.
It does not download, render, replace, activate or remove any profile or tool.

## CLI

Use an explicit project root. The command does not scan arbitrary disks:

```text
python -B scripts/installation.py inventory --project <project-root>
python -B scripts/installation.py adopt --project <project-root>
python -B scripts/installation.py adopt --project <project-root> --confirm
```

`inventory` and `adopt` without `--confirm` are read-only. Confirmed adoption
writes only the local inventory record, adding stable component IDs that are not
already recorded. Repeating adoption adds no duplicates. Review the preview and
its unsafe-path/manual-review warnings before confirming. This CLI confirmation
records inventory only; it is not consent to modify any installed component.

Discovery is scoped to the explicitly selected root: roster workspaces, known
legacy `agents/project-manager`, exported root `agent/`, direct team workspaces,
project shared references, and known CBM project helper paths. Paths are
relative to the selected root, containment-checked and never followed through
links. No filesystem crawl, native Codex memory/history, chat bindings or
contents of protected state are inventoried. Absolute project roots are not
stored in the metadata.

## Schema 1 component record

Each record has a stable `id`, `kind` (`profile`, `shared_reference`, `tool`),
project-relative component `path`, catalog `source`, nullable immutable
`source_revision`, installed `version`, `component_schema`, `owned_paths`, and a
per-file `baseline`. A known baseline contains SHA-256 only. It stores no original
file bodies. New catalog installs record the exact rendered bytes they create;
files that pre-existed a shared bundle remain unknown. The shared reference is
one project component, not one copy per dependent role.

Legacy adoption cannot reconstruct a baseline from today's template, matching
version number, current file content or a mutable source label. Missing
immutable source revision means `unknown_source`; an unverifiable baseline or
unsafe path means `manual_review`. Known baselines classify present identical
files as `unchanged`, differing bytes as `local_changes`, and missing files as
`removed_files`. A shared bundle with any unknown original file remains manual
review until a separate trusted process can prove its baseline.

The current profiles, shared reference contents, learned/native memory, STATE,
roster, chat bindings/history, tasks, request ledger, approvals, credentials,
project configuration and all files outside inventoried owned paths remain
untouched by discovery/adoption. Do not infer that matching `agent.yaml` version
means the files are pristine. Never convert an unknown original into a clean
baseline without independently verifiable original bytes and rendering inputs.

## Component interface boundary

Schema 1 deliberately records only local identity/version/source/baseline
metadata needed by later stages. A later trusted-release verifier may map a
verified signed payload to a component ID/kind/version, compatible platform and
required component IDs. A later transaction stage must render against the
verified original template and recorded original answers, compare against this
baseline, preview conflicts and require explicit per-update approval. It must
never treat this inventory as a signature, release key, trusted source, or
permission to overwrite local edits. Native Codex memory and identity/history
remain protected. Release trust/signature validation and transactional apply
are separate stages and interfaces.
