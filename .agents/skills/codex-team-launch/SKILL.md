---
name: codex-team-launch
description: Launch a team from the catalog into the current project, add a team to an existing project, or appoint one more agent (catalog, custom or Project Director), creating and placing the Codex chats. Use when the owner asks to start, create or add a team, Director or agent.
---

# Launch a team or appoint an agent

Run from a Codex Desktop chat in the target project. Commands use Codex Bot Mode's
`scripts/team.py` and the project's `.bot-mode/roster.py`, with
`--project <absolute project root>`. Codex Bot Mode is the folder that contains
`scripts/team.py`: two folders above this SKILL.md when it comes from the plugin,
the repository root when it comes from the Codex Bot Mode repository, or the
`project.tooling` path shown by `.bot-mode/roster.py show` in an installed project.
Never create anything before the owner confirms the reviewed plan.

## 1. Setup mode

Ask: "Use defaults, or answer a few setup questions?" If the owner already gave
values in the request, use them and fill the rest by the chosen mode.
For questions, run `team.py questions --team-template <t>` (or `--template <a>`) and
ask in the owner's language, a few at a time, as multiple choice with the default
marked. Write the answers to a JSON file:
`{"team": {...}, "agents": {"<role>": {...}}}` for a team, `{...}` for one agent.

## 2. Plan and confirm

Obtain an explicit tracking mode: github, local or both, even when using defaults.
Both also requires the authoritative status record (github or local). Include
`tracking_mode` and `tracking_authority` in team answers. Preserve existing team
choices when adding agents; do not infer a mode from tool availability.

Run `team.py plan-team <template> --project <p> [--answers <file>]` or
`team.py plan-agent --project <p> --team <name> --template <a> [--answers <file>]`.
Show the owner: agents with names and workspaces, every value and its source, the
sidebar section, the room, new project files, and any Director proposal. Ask for
confirmation.

## 3. Install

After confirmation: `team.py install-team <template> --project <p> --answers <file> --approval once --confirm`
or `team.py add-agent ... --actor <you> --approval once --confirm`. Use
`--approval standing:<id>` only when `team.py approvals` shows a matching owner
record. If you proposed a custom agent, pass `--proposed-by <you>`.

## 4. Chats

- First team in a project: if this chat has no role yet, it becomes the team
  Manager: `roster.py --project <p> set-thread <manager-bot> $CODEX_THREAD_ID --reason "first chat becomes Manager"`,
  then load that Manager workspace's AGENTS.md, BASE.md, SOUL.md, PROFILE.md and STATE.md.
- Every other new agent: `list_projects` to find the project ID (if the project is
  not added in Codex, ask the owner to add it and stop here), then `create_thread`
  with environment local and this prompt:
  "You are appointed as <display name> (<role>) in project <name>. Your workspace is
  <absolute workspace>. Load <absolute workspace>/.agents/skills/codex-<bot>/SKILL.md,
  then your profile and memory with --project <absolute workspace>, and place
  yourself in the sidebar section '<section>'."
  Title the chat with the display name. Then
  `roster.py set-thread <bot> <threadId> --reason "appointed"`.

## 5. Sidebar

Reuse the section named in the plan from `list_threads`, or create it once with
`create_sidebar_section`. Move every new chat into it with
`move_thread_to_sidebar_section`. Re-running never creates a second section.

## 6. Report

Reply with `roster.py links`: each agent, role and chat link; the section; the
room; and the Director proposal if any. Codex Office picks up roster changes when
it restarts. Update your STATE.md.
