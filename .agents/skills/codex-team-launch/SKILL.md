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

## 3. Optional Open Dreamdocs

Run `team.py dreamdocs-offer --project <p> --place team-launch`. If `ask_now` is false (the owner
already said Yes or No, was already asked here, or the project already has the Dreamdocs
documentation index and memory folder), skip this step silently. Otherwise show the offer once
together with the plan and ask for an explicit Yes, No or Skip:

> Optional: Open Dreamdocs. It gives this project one canonical place for project, product and
> marketing documentation, selective indexes, your own source materials, and an evidence and memory
> structure. It does not replace Codex Bot Mode: Codex Bot Mode holds roles, teams, task coordination
> and working instructions; Dreamdocs holds organised project knowledge. Each works alone. Nothing is
> installed unless you choose Yes and then approve the exact files.
> Yes: inspect this project and show an install plan. No: continue without it. Skip: decide later.

Record only what the owner said: `team.py dreamdocs-offer --project <p> --place team-launch --set yes|no|skip`.
No answer means Skip, never Yes. A No stays until the owner asks to change it (`--owner-change`).
The team install never waits for this answer.

On Yes, work through these steps; none of them is covered by the team install approval:

1. Use the official Open Dreamdocs repository the owner names (or the one the project's documentation already
   names); if neither exists, ask the owner for its address and never pick one by search. Find its latest
   stable release now (`gh release list --repo <owner/repo> --exclude-pre-releases --limit 1`) and that tag's
   commit (`git ls-remote https://github.com/<owner/repo> "refs/tags/<tag>^{}" "refs/tags/<tag>"`).
   Never use `main`, a release candidate or unreleased templates.
2. Inspect the project's existing AGENTS.md, PROJECT.md, docs index, governance, tracking rules,
   memory guidance, .gitignore and any `team/` or `.bot-mode/` files. Read the release's
   `docs/operations/adopting-dreamdocs.md` and follow it.
3. Show the owner the exact install scope: files to add, every colliding path, and the smallest
   proposed merge for each. Contradictory rules become owner decisions (with a linked issue if work stops).
4. Wait for explicit approval of that scope. Then copy only missing files and merge the agreed
   sections. Never overwrite, never create a second AGENTS.md or PROJECT.md, keep existing source-role
   instructions and private materials as they are.
5. Run the release's link check and tests, record the result in the project's own tracker, then
   `team.py dreamdocs-offer --project <p> --adopted-tag <tag> --adopted-commit <commit>`.
6. Do not run `dreamdocs.py setup`, install hooks or schedulers, publish, change native Codex memory
   or appoint agents as part of this step; each needs its own owner request.

## 4. Install

After confirmation: `team.py install-team <template> --project <p> --answers <file> --approval once --confirm`
or `team.py add-agent ... --actor <you> --approval once --confirm`. Use
`--approval standing:<id>` only when `team.py approvals` shows a matching owner
record. If you proposed a custom agent, pass `--proposed-by <you>`.

## 5. Chats

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

## 6. Sidebar

Reuse the section named in the plan from `list_threads`, or create it once with
`create_sidebar_section`. Move every new chat into it with
`move_thread_to_sidebar_section`. Re-running never creates a second section.

## 7. Report

Reply with `roster.py links`: each agent, role and chat link; the section; the
room; and the Director proposal if any. Codex Office picks up roster changes when
it restarts. Update your STATE.md.
