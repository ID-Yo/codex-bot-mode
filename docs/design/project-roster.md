# Project roster

Every project keeps one roster of its teams and participants. Agents and Codex
Office read the same data.

## Shared and local parts

| File | Contents | In Git |
|---|---|---|
| `.bot-mode/roster.json` | Project name, teams (sidebar section, room), participants (bot, role, team, workspace, status) | Yes |
| `.bot-mode/local/bindings.json` | Each participant's current Codex chat and the history of replaced chats | No |
| `.bot-mode/roster.py`, `.bot-mode/requests.py` | Commands | Yes |
| `.bot-mode/local/requests.json` | Request ledger | No |
| `.bot-mode/.gitignore` | Keeps `local/`, Office and relay data, locks and derived summaries out of Git | Yes |

Codex chats exist only in the Codex Desktop where they were created. On a second
computer, or for a second person working in the same repository, the team and
its memory come with Git, and each person records their own chats for the same
agents with `set-thread`. The deep link of a participant is
`codex://threads/<thread_id>`.

## Commands

```text
python -B .bot-mode/roster.py --project <project> links
python -B .bot-mode/roster.py --project <project> show
python -B .bot-mode/roster.py --project <project> add-team <name> [--section <s>] [--room <r>]
python -B .bot-mode/roster.py --project <project> add <bot> --role <role> [--team <t>] [--workspace <w>] [--thread <id>]
python -B .bot-mode/roster.py --project <project> set-thread <bot> <thread-id> --reason "<why>"
python -B .bot-mode/roster.py --project <project> retire <bot> --reason "<why>"
```

## Rules

- Writes validate the whole roster, are atomic and refuse a stale version.
- Identifiers are lowercase; workspaces are relative paths inside the project; one
  chat cannot belong to two participants; values that look like credentials are refused.
- Older rosters that stored chats in roster.json still load; the next write moves
  the chats to `local/bindings.json`.
- Codex Office reads participants and chats from the roster when it starts.
