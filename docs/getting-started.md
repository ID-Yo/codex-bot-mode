# Getting started

This guide takes you from a fresh clone to a working team. Run commands from the
Codex Bot Mode folder unless noted. `<project>` is the absolute path of your own
project folder.

## 1. Check requirements

```text
python --version        # 3.12 or newer
git --version
gh auth status          # optional, needed for GitHub issues and catalog fetches
```

Then run the doctor. It checks everything agents need to report back to their Manager and says what to do for each problem:

```text
python -B scripts/doctor.py          # read-only: Python, Git, Codex Desktop, relay, supervisor task, project outboxes and relay routes
python -B scripts/doctor.py --fix    # also installs the supervisor task, restarts the relay, installs a MISSING outbox (an installed one is never overwritten)
python -B scripts/doctor.py --project <project> --canary   # also delivers one real test message to that project's Manager (it wakes the Manager)
```

Expected on a healthy setup: every line `[ ok ]` and `OK: everything needed works.` Warnings are allowed, `[FAIL]` lines carry a `fix:` command. The plain check changes nothing: it reads the queue file read-only and never starts a pump. `--canary` always names its project; `--canary-all-projects` opts in to one test message per checked project.

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

Check that the agents can report back to the Manager (run once after the team exists, and again after any problem):

```text
python -B scripts/doctor.py --fix --project <project> --canary
```

Expected: `<project>: canary: delivered to the Manager in <n> s` and a short test message in the Manager chat.

Without the plugin, the same launch works from the command line:

```text
python -B scripts/team.py plan-team development --project <project>
python -B scripts/team.py install-team development --project <project> --tracking-mode github --approval once --confirm
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
| Agents finish but the Manager hears nothing | Run `python -B scripts/doctor.py --fix`; the supervisor task (`scripts/supervisor_task.ps1`) restarts the relay and the outbox pumps by itself every 2 minutes |
| Agent did not move to its sidebar section | Ask it to place itself again (BASE.md, Sidebar) or move it by hand |
| Office will not start | Read `<project>/.bot-mode/team-room-v1/office/office.stderr.log` |
