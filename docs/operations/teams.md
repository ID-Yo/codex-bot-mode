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
For cross-project execution and stall monitoring, see [agent-monitor.md](agent-monitor.md). For branch, worktree and process closeout rules, see [repository-hygiene.md](repository-hygiene.md).

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

**Setup status (#454).** `install-team` ends by printing, and storing in `.bot-mode/local/setup-status.json`, which steps of
the setup are finished: the roster (a Manager chat is bound), the outbox (`outbox.py` installed), the relay route (a usable
descriptor whose port answers), the Office registration and the delivery proof. `python -B scripts/team.py setup-status
--project <p>` checks it again at any time and is what the launching agent runs after the chats are recorded. The Office
registration is `done` when the project is in the Office registry, `waiting for owner consent` when it is not (registering is
the owner's explicit step, the project onboarding in the Office panel, and no install, doctor or supervisor does it),
`not applicable` for the Office's own project and `unknown` when the registry cannot be read. The delivery proof is `none`,
`canary delivered` (the zero-token canary of the outbox was processed: the path to the chat works, nothing was sent) or
`real report delivered`. The status says "Delivery works" only when every step is finished and the proof is one of the last two;
a proof from the past with an unfinished step reads "Delivery was proven earlier ... but a step is unfinished now", and
without a proof it reads "Delivery not proven". Every step that is not finished is listed as not finished. The proof
counts delivered rows (not a row that a person marked delivered by hand with `outbox.py resolve`, whose error text starts with
`resolved by operator`) and processed canaries in the queue, which is opened read-only and never created (a queue from before
the canary still proves a delivered row). The file is project-local and ignored by Git. `setup-status` refuses a path that
has no `.bot-mode` folder and creates nothing there. A status that cannot be stored (for example a folder or a locked file
in its place) is a warning on stderr after `install-team`, which still prints its result and exits 0, and an error for
`setup-status`.

The JSON that `install-team` prints on stdout keeps `installed`, `section`, `room` and `director_proposal` and has the
additional key `setup_status` (the same object that is stored; its details name the project and relay paths). Known
limitation: the registry is read from the checkout that holds `team.py` (or from the file named by `CODEX_OFFICE_REGISTRY`,
as the relay does). The built plugin has no registry of its own, so there a project that is registered in the real Office
still reads `waiting for owner consent`, which is the conservative answer, unless `CODEX_OFFICE_REGISTRY` names the
Office's `.bot-mode/local/office-project-roots.json`.

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

## Replace a temporary hand-made agent with a catalog role (#461)

A project can already have an agent of the same role that somebody added by hand: a roster entry without a chat, sometimes
with a workspace folder and memory (for example a temporary Infrastructure Engineer made before the catalog role existed).
Nothing in the tools adopts such an entry or overwrites its folder. `plan-agent` derives the bot ID from the role and, when
any participant (retired ones too) already has that ID, prefixes it with the team (`development-infrastructure-engineer`). It
refuses a workspace folder that already exists. `roster.py` can add, set-thread and retire, never delete. The supported way, in
this order (a step that fails stops the procedure and the later steps do not run), ends with one active executor, the old memory untouched and the new agent unbound until its chat exists:

1. **Inspect, read only.** `roster.py show` (the temporary entry and whether its `thread_id` is set), its workspace folder if it
   has one (`agent/memory`, `agent/STATE.md`), and the open requests for that bot in the ledger. Do not edit or delete anything.
2. **Owner decision, recorded in the task.** What, if anything, of the temporary agent's memory is worth carrying over, and
   whether its open work finishes first or is handed over.
3. **Plan, read only, then the owner approves it.** `team.py plan-agent --project <p> --team <Team> --template <template>` changes nothing. It shows the new bot ID and
   workspace, which differ from the temporary ones, and the bot ID is the same as the one `add-agent` will use because every participant counts,
   retired or not. When the plan refuses, for example because the folder of the new workspace already exists, stop and ask the owner:
   nothing has been retired yet, the temporary agent stays the active executor and no folder is overwritten or reused.
4. **Retire the temporary entry, only after the plan was accepted.** `roster.py retire <temporary-bot> --reason "replaced by catalog <template> (#<issue>)"`.
   It keeps the history, clears the chat binding and removes the entry from `links`, so a second executor for the role never exists.
   The supported tools cannot undo it (`set-thread` refuses a retired entry and `add` refuses its name), which is why it comes after the plan.
   It does not touch the chat itself: when the temporary agent had a chat, the Manager or the owner renames it
   "<display name> (replaced)" in Codex by hand so that nobody uses it by mistake.
5. **Add.** `team.py add-agent ... --actor <you> --approval once --confirm` with the plan the owner approved.
6. **Memory.** The new agent starts with empty memory. Facts the owner chose to keep are given in the appointment handoff (as
   in `codex-replace-chat`) or added as reviewed entries through the memory helper of the new agent. Folders and files of the
   temporary agent are never copied over or merged.
7. **Bind.** Create the chat as in `codex-team-launch` step 5 and `roster.py set-thread <new bot> <thread> --reason appointed`.
   Until then `thread_id` is null and the agent receives nothing.
8. **Check.** `roster.py links` lists exactly one active participant of the role, with a chat; the retired one is absent; the old
   workspace is as it was.

An installed catalog role has no in-place update: a newer template version is applied by the same steps (plan, retire, add,
memory handoff, bind). `tests/test_teams.py` runs this sequence against a project with a hand-made unbound entry, with and
without a workspace folder and memory, and against a folder that blocks the plan.

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
