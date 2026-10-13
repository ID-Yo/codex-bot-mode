# Contributing to Codex Bot Mode

Thanks for helping. Small, focused changes are easiest to review.

## Request, contribute or update a profile

Use the Catalog profile request or update issue form for a new individual role,
team bundle, contribution or revision. Search existing issues and catalog roles
first. Include purpose, concrete deliverables, parameters, required versus optional
tools, approval boundaries and acceptance examples. Requests need no implementation.

Contributions place role files under `catalog/agents/<id>/` and team bundles under
`catalog/teams/<id>/`; update `catalog/index.yaml`. Provide a synthetic successful
exercise and a missing-facts/boundary exercise, targeted tests and installation
evidence. Templates ship empty memory and no chat bindings or private history.
Document source/license provenance compatible with MIT.

Updates identify the existing template and version, bump its manifest and index
version, describe compatibility and provide an identity-preserving migration plan.
Installing a new version must preserve existing IDs, owner choices, memory and
history; the current installer refuses existing workspaces, so do not recommend
deleting or overwriting an installed agent. A profile is ready only after its
required parameters, review and acceptance exercises are satisfied. Maintainers
review the linked PR before publishing the curated catalog.

1. Open an issue first for anything larger than a small fix, so we can agree on the approach. New catalog roles are welcome: describe the role, its typical tasks, what "done" means and how its work is checked.
2. Fork the repository, create a branch and make the change.
3. Run the tests: `python -m unittest discover -s tests` and the privacy check `python -B scripts/public_audit.py .`.
4. Open a pull request and fill in the template.

Rules:

- Record implementation before starting, with goal, scope, acceptance, owner,
  status and evidence. Public contributions use an issue and linked PR; installed
  teams may explicitly choose github/local/both. See [tracking](docs/design/team-work-tracking.md).
- If GitHub CI is unavailable, run local CI and report exact results and limits.

- One topic per pull request; add or update tests for logic changes.
- Behaviour that depends on Codex Desktop (creating chats, sidebar, messages between chats) cannot be unit tested: say how you checked it and with which Codex version.
- No secrets, personal data, local absolute paths or chat IDs in the repository.
- Follow [AGENTS.md](AGENTS.md); it applies to people too.
