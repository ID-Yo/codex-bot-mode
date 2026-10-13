# Getting started

This guide takes you from a fresh clone to a working team. Run commands from the
Codex Bot Mode folder unless noted. `<project>` is the absolute path of your own
project folder.

## 1. Check requirements

```text
python --version        # 3.10 or newer
git --version
gh auth status          # optional, needed for GitHub issues and catalog fetches
```

## 2. Install the plugin

```text
python -B scripts/build_plugin.py
```

Expected: `Built <home>/plugins/codex-bot-mode` and `Registered in <home>/.agents/plugins/marketplace.json`.
In Codex: Plugins → Personal → Codex Bot Mode → Install. If the plugin does not
appear, restart Codex.

## 3. Launch your first team

1. In Codex, add `<project>` as a project and start a new chat in it.
2. Ask: "Start a Development team in this project."
3. Answer "defaults" or the setup questions (names, model, reasoning level).
4. Review the plan: agents, folders under `team/Development/`, sidebar section,
   room. Confirm.
5. Result: this chat is the Manager; Developer, Code Reviewer, Designer and
   Verifier chats appear in the section "<project> · Development".

Check the roster:

```text
python -B <project>/.bot-mode/roster.py --project <project> links
```

Expected: one entry per agent with a `codex://threads/...` link.

Without the plugin, the same launch works from the command line:

```text
python -B scripts/team.py plan-team development --project <project>
python -B scripts/team.py install-team development --project <project> --approval once --confirm
```

Then create one chat per agent and record it with
`roster.py set-thread <bot> <thread-id> --reason appointed`.

## 4. Fill the shared project files

Edit `<project>/team/PROJECT.md` (purpose, build and test commands, conventions),
`OWNER.md` (how you want to work) and `DECISIONS.md`. Every agent reads them.

## 5. Give the first task

Tell the Manager what you want. It plans, assigns work with request IDs, tracks
replies, and reports back with evidence. Requests are recorded in
`.bot-mode/requests.py`; overdue ones are reminded automatically when Codex Office runs.

## 6. Open Codex Office (optional)

```text
powershell -File scripts/start_codex_office.ps1 -Project <project>
```

Open http://localhost:24242: team rooms, personal chats with each agent, settings.

## When something fails

| Problem | What to do |
|---|---|
| `Team ... already exists` | Choose another team name or use `add-agent` |
| `Workspace already exists` | A folder is in the way; nothing was overwritten. Rename or remove it |
| Plugin not listed | Rerun `build_plugin.py`, restart Codex |
| Agent did not move to its sidebar section | Ask it to place itself again (BASE.md, Sidebar) or move it by hand |
| Office will not start | Read `<project>/.bot-mode/team-room-v1/office/office.stderr.log` |
