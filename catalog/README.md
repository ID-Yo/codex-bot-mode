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

## Launch inventory and readiness

Agent Monitor provides read-only cross-project execution and activity monitoring reporting to the Chief of Staff, plus the documented direct Manager signals to the owning project Manager; see [docs/operations/agent-monitor.md](../docs/operations/agent-monitor.md) and [docs/operations/repository-hygiene.md](../docs/operations/repository-hygiene.md).


Catalog v2 retains the original 13 agent templates and adds 10 distinct
specialists: Business Analyst, Research Analyst, Product Manager, Data Analyst,
Operations Specialist, Knowledge Writer, Support Specialist, Learning Designer,
Career Advisor and Financial Analyst. All support individual appointment through
`plan-agent`/`add-agent` into an existing team. Team Manager coordinates work;
Product Manager supplies product outcomes and requirements without dispatch rights.
Chief of Staff is the personal assistant in `packages/chief-of-staff-v1/`; there
is no duplicate personal-assistant catalog profile or automatic CoS appointment.

The 12 core bundles are development, marketing, sales, startup, product, research,
content-studio, operations, customer-support, learning, career and
business-reporting. They contain 3-5 roles. Starter adds a smaller Manager plus
one owner-selected catalog specialist; it never invents a custom role.

New profiles include synthetic input/output and boundary acceptance exercises in
PLAYBOOK.md, installed with the profile. Their PROFILE.md separates required
source/output access from optional tools. Honor the user's language and requested
artifact language; availability, model capability and correctness are not promises.

Readiness: owner-approved inventory implemented as templates; automated planning
and temporary installation checks are template validation only. New roles and
bundles have no verified live Desktop appointment, memory/restart recovery,
requester-channel handoff or independently accepted deliverable yet. The proposal
in work/reports/COS-004-catalog-roles.md remains historical research; the owner's
launch decisions are recorded in issue #87. Public export/publication and live
exercises are separate work.
