# Working with teams

## Who to talk to

- One team: talk to its Manager.
- Two or more teams: the Project Director is the entry point. You can still talk
  to anyone directly; they inform the next level up.
- Chief of Staff (optional, see `packages/chief-of-staff-v1`): your personal agent
  across all projects; it routes work to each project's entry point.

## Rules every agent follows

Each agent loads `agent/BASE.md`: reply in your language, keep documentation in
English, always reply to whoever asked in their channel, find colleagues' current
chats in the roster, record requests in the ledger, keep `agent/STATE.md`
current, save only evidenced lessons, and ask you before sensitive actions.

## Add a team or an agent

"Add a Marketing team" or "Add a Designer to Development". The launcher shows a
plan; nothing is created until you confirm. Adding a second team proposes a
Project Director. Roles without a template are built from the blank template
through a short interview.

## Standing approvals

To let a Manager or Director appoint routine agents without asking each time,
add a record to `team/approvals.yaml` (only you edit this file). See
`docs/operations/teams.md` for the format. Every use is logged and reported.

## Replace a chat

## Two computers or several people

Commit and pull as usual. The team, profiles, shared project files and every
agent's learned memory travel with the repository; memory saves from different
computers merge without conflicts. Codex chats do not travel: on each computer,
create a chat for each agent you use there and record it with
`python -B .bot-mode/roster.py --project . set-thread <bot> <thread-id> --reason "this computer"`.
Never store secrets or personal data in memory; everyone with repository access can read it.

## Replace a chat

When a chat breaks or gets too long, ask: "Replace the Developer chat." A new chat
continues from `agent/STATE.md` with the same memory; the roster keeps the old
link in history.

## Reference

- Teams, approvals, commands: `docs/operations/teams.md`
- Roster format: `docs/design/project-roster.md`
- Catalog and template format: `catalog/README.md`
