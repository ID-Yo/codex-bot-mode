# AGENTS.md

Instructions for coding agents working on this repository. README.md explains the product; this file says how to change it safely.

## Commands

- Test: `python -m unittest discover -s tests` (run before every commit; standard library only)
- Plan and install a team in a scratch folder: `python -B scripts/team.py plan-team development --project <dir>`, then `install-team ... --approval once --confirm`
- Roster of a project: `python -B <project>/.bot-mode/roster.py --project <project> links`
- Build the plugin without registering it: `python -B scripts/build_plugin.py --out <dir> --no-register`
- Privacy check of a tree: `python -B scripts/public_audit.py <dir>` (must report 0 findings)
- Codex Office: `python -B -m scripts.team_room --project <project> --codex-path <codex executable>` (needs Codex Desktop)

## Structure

- `catalog/`: `base/` (BASE.md shared rules, workspace and skill templates, memory contract and helper), `agents/<role>/` (agent.yaml, SOUL, PROFILE, PLAYBOOK), `teams/<team>/` (team.yaml, ROOM.md), `index.yaml`
- `scripts/team.py`, `scripts/team_room/teams.py`: catalog, questions, dry-run plans, install, approvals
- `scripts/team_room/roster.py`, `requests.py`: standalone modules, copied into each project's `.bot-mode/`
- `scripts/team_room/manifest.py`: YAML subset reader and writer (stdlib)
- `scripts/team_room/` (other modules and `static/`): Codex Office
- `.agents/skills/`: `codex-team-launch`, `codex-replace-chat`
- `packages/chief-of-staff-v1/`: personal agent template
- `tests/`: unit tests; no Codex, no network

## Conventions

- Python 3.10+, standard library only. A dependency needs a strong reason and an issue first.
- Documentation, templates and code comments in English. Agents answer users in the user's language.
- Templates contain no memory, chat IDs, personal data or credentials. Every template parameter has a `question` and a `default` or `required: true`.
- Versions follow SemVer; add a CHANGELOG entry for user-visible changes.
- Keep the smallest complete change; do not refactor unrelated code.

## Boundaries

- Never commit secrets, personal data, local absolute paths, chat IDs or `.bot-mode/local/`.
- Codex Desktop internals (app tools such as `create_thread`, `send_message_to_thread`, sidebar sections; `CODEX_THREAD_ID`) are not covered by unit tests. Check changes against an installed Codex and say which version.
- Agents act only with the owner's approval for appointments, external messages, publishing, spending and deletion. Do not weaken these rules in templates.
- Ask before: adding dependencies, changing roster, ledger or memory formats, anything that adds network access.

## Done means

- Tests pass, `public_audit.py` reports 0 findings, and changed behaviour was exercised, not only read.
- README, docs and CHANGELOG updated when behaviour changes.
