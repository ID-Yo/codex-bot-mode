# Teams

How to launch teams, add agents and keep chat links current. Templates are in
[catalog/](../../catalog/README.md); the roster format is in
[project-roster.md](../design/project-roster.md).

## Structure

```text
<project>/
  scripts/roster.py, scripts/team.py   (from Codex Bot Mode)
  .bot-mode/roster.json                teams, participants, current chats (local)
  team/
    PROJECT.md  OWNER.md  DECISIONS.md shared by every agent
    approvals.yaml                     standing approvals (owner only, optional)
    <Team>/ROOM.md
    <Team>/<bot>/                      one workspace per agent
      AGENTS.md
      agent/BASE.md SOUL.md PROFILE.md PLAYBOOK.md STATE.md agent.yaml
      agent/memory/
      .agents/skills/codex-<bot>/
      scripts/bot_mode.py
    project-director/                  only with two or more teams
```

Chain of work: Owner or Chief of Staff -> Project Director -> team Manager ->
specialist. The owner may talk to anyone directly.

## Launch a team

In Codex, ask the project chat to start a team; it uses the
`codex-team-launch` skill. The same steps by hand:

```text
python -B scripts/team.py catalog
python -B scripts/team.py questions --team-template development
python -B scripts/team.py plan-team development --project <p> [--answers a.json]
python -B scripts/team.py install-team development --project <p> --tracking-mode github --approval once --confirm
```

The installer never overwrites an existing team, workspace or memory, and rolls
back if a step fails. New agents start with empty memory and `thread_id: null`
until their chats are created and recorded with `roster.py set-thread`. The chat
that starts the first team becomes its Manager.

If `~/.codex-bot-mode/config.json` contains `{"chief_of_staff": "<path>"}`, every
team launch also adds the project to that Chief of Staff's `knowledge/projects.md`
(once, with the purpose left for the owner to confirm).

Answers file: `{"team": {"team_name": "Platform"}, "agents": {"developer": {"display_name": "Ana"}}}`.
Without answers optional values use defaults; tracking mode must be explicit.
Starter also requires `team.specialist` from its listed catalog choices. Each installed
`agent/agent.yaml` records values and their source (owner, default, inferred).

## Add a team to an existing project

Same commands with another team name. Bot IDs stay unique (`platform-manager`).
When the project gets its second team, the plan proposes a Project Director:

```text
python -B scripts/team.py add-agent --project <p> --template director --actor owner --approval once --confirm
```

## Add one agent

From the catalog: `plan-agent` / `add-agent --team <T> --template <name>`.
For a role without a template use `--template blank` with interview answers
(role, purpose, responsibilities, done_means, verification; optional tools, tone,
reports_to). A Manager or Director may propose such an agent
(`--proposed-by`), but nothing is created without owner approval.

## Approvals

`--approval once` means the owner approved this exact case in the conversation.
`--approval standing:<id>` uses a record in `team/approvals.yaml`, which only the
owner creates, changes or revokes:

```yaml
approvals:
  - id: catalog-specialists
    granted_by: owner
    used_by: [manager, project-director]
    covers: [catalog-agent]        # team, catalog-agent, custom-agent, director
    teams: [Development]           # optional
    templates: [verifier]          # optional
    max_per_month: 2               # optional
    expires: 2027-01-01            # optional
```

Every use is appended under `uses` and must be reported to the owner. Out of
scope, expired or revoked approvals fall back to one-time approval. The file is
plain text in the project: the rule that only the owner edits it is enforced by
agent instructions, not by the operating system.

## Replace a chat

When a chat breaks or grows too long, use the `codex-replace-chat` skill. It
creates a new chat for the same agent with a handoff from `STATE.md`, records it
with `roster.py set-thread` (the old chat goes to history) and notifies the Manager.

## Retire an agent

`python -B scripts/roster.py --project <p> retire <bot> --reason "<why>"`. The
workspace, memory and history stay; the agent disappears from `links`.

## Small starter

Use `questions --team-template starter` to see the real specialist choices.
Example answers file:

```json
{"team": {"specialist": "research-analyst", "tracking_mode": "local"}}
```

Run `plan-team starter --project <p> --answers a.json`, then the usual
owner-approved `install-team starter --project <p> --answers a.json
--approval once --confirm`. This installs exactly Manager and the selected
catalog specialist. Missing/invalid choices fail before installation. Office
uses the existing team questions and forwards the selected specialist in its
launch answers; this path still needs a live Desktop exercise.
