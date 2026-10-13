# Catalog

Reusable templates for agents and teams. `index.yaml` lists them with their
versions. Installed agents record the template name, version and catalog source
they came from. Templates contain no memory, chat IDs, personal data or credentials.

- `base/`: shared base instructions (`BASE.md`), memory contract and helper, and
  the files every agent workspace receives.
- `agents/<template>/`: one role: `agent.yaml`, `SOUL.md`, `PROFILE.md`, `PLAYBOOK.md`.
- `teams/<template>/`: `team.yaml` (roles, sidebar section, room) and `ROOM.md`.

Text files may use placeholders such as `{{project}}`, `{{team}}`, `{{role}}`,
`{{bot}}`, `{{display_name}}`, `{{host}}` and any template parameter name.

## Parameters

Every template parameter has a `question`, and either a `default` or
`required: true`. Optional `type` (text, choice) and `choices` describe answers.
At install the owner either uses defaults or answers the questions; values given
in the request win. The installed `agent.yaml` records each `value` and its
`source` (owner, default or inferred).

Install and manage teams with `scripts/team.py`; see `docs/operations/teams.md`.
