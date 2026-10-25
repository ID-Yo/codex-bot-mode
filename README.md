# Codex Bot Mode (CBM)

[English](README.md) · [Български](README.bg.md)

> Persistent agents and teams inside Codex Desktop: a Manager per team, a Project Director for multi-team projects, a personal Chief of Staff, shared memory and a local Office for team rooms.

Unofficial community project; not affiliated with or endorsed by OpenAI. "Codex" is the name of an OpenAI product.

## Windows Office app

A Windows x64 installer is available in the Office desktop release. It provides
Start, Stop, Restart, service status and recent activity, with the work UI in
external Chrome. Codex Desktop is required; a separate Python/Node/Rust install
is not required to run the packaged app. Executables are signed with a self-signed
certificate (`CN=Codex Office (ID-Yo)`, thumbprint `2C4510778788894A8CAA3C891FE3BCB899A0C0F0`);
install the public certificate `desktop/Codex-Office-ID-Yo.cer` to trust it.
Compare its published SHA-256 before installation.
Release 0.1.6 notes are in [CHANGELOG.md](CHANGELOG.md).

On first run select the root folder of your existing CBM project (the folder
containing `.bot-mode/roster.json` and your teams), then Start and Open Office
in Chrome. The folder is the service workspace. Other configured projects are
listed through that workspace's Office registry. See [desktop instructions](desktop/README.md).

Profile/reference updates require an explicitly configured trusted source and
approval of an exact preview. Installation does not automatically update all
agents. See [update configuration](docs/operations/office-update-host.md).

## Why

Most multi-agent tools build a new environment: their own runtime, their own chat UI, their own permissions. Codex Desktop already has what an agent needs: chats that keep context, project folders, skills, plugins, connectors, sandboxed tools and your approval rules. CBM does not replace any of that. It adds structure on top of it:

- **Identity.** Each agent is an ordinary Codex chat that loads a profile (role, character, playbook, rules) and its own validated memory.
- **Teams.** Agents are grouped into teams with a Manager; projects with several teams get a Project Director; your Chief of Staff is the single entry point across projects.
- **Coordination.** A roster keeps every agent's current chat link; a request ledger makes sure every request gets a reply in the requester's channel, with reminders when it does not.
- **Memory that travels.** Learned lessons are evidence-backed files committed with your project, so they work on a second computer and for everyone on the team.

Nothing runs behind your back: work happens in your Codex chats, with your permissions, and appointments need your approval.

## How it works

![Four tiers: Human owner (HO), Chief of Staff (COS), Project Director, and Teams A, B and C. Each own Manager reports to the Director. Each Team contains its Manager and Specialists S1/S2, with two-way Manager-specialist and within-team specialist communication.](docs/assets/working-structure.svg)

Four tiers, from top to bottom: Human owner (HO), Chief of Staff (COS), Project Director, and Teams A, B and C. The Project groups its Director and all three Teams. Each Team Manager reports to the Director; the Director coordinates with COS, and COS with HO. Each Team contains its own Manager and specialists (S1/S2). Arrows show two-way communication, including between specialists within the same Team. These internal Team roles are part of the bottom tier, not additional tiers. No specialist connection grants cross-team or cross-project authority.

| Piece | What it is |
|---|---|
| Agent | A Codex chat + a workspace folder: `agent/BASE.md` (shared rules), `SOUL.md`, `PROFILE.md`, `PLAYBOOK.md`, `STATE.md`, `agent.yaml`, `memory/` |
| Team | Agents from a catalog template, a sidebar section and an Office room |
| Roster | `.bot-mode/roster.json` (shared) + `.bot-mode/local/bindings.json` (your chats) |
| Request ledger | `.bot-mode/local/requests.json`: every request waits for its reply |
| Codex Office | Local web app on http://localhost:24242: team rooms, personal chats, settings |

## Catalog

| Team | Roles |
|---|---|
| Development | Manager, Developer, Code Reviewer, Designer, Verifier |
| Marketing | Manager, Strategist, Content Writer, Content Reviewer |
| Sales (drafts only) | Manager, Lead Researcher, Outreach Writer, Pipeline Analyst |
| Any project | Project Director (two or more teams); custom roles from the blank template |
| Personal | Chief of Staff: your assistant and entry point across projects |

Each role is also available on its own. See [catalog/](catalog/README.md).

## Quick start

**New computer, or moving an existing setup?** Follow [Set up on a new computer](docs/new-machine.md) first: one ordered guide (Chief of Staff, project teams, Office, relay, Monitor), for a fresh setup and for a move.

Requirements: Codex Desktop (Windows tested; other platforms untested), Python 3.10+ (standard library only), Git. Optional: GitHub CLI, Chrome or Edge for Office voice features.

1. Clone this repository.
2. Build and register the plugin: `python -B scripts/build_plugin.py`. In Codex open **Plugins > Personal** and install **Codex Bot Mode**.
3. Add your project folder in Codex, start a chat there and say: *"Start a Development team in this project."*
4. Choose defaults or answer the setup questions, review the plan, confirm. Your chat becomes the Manager; the other agents get their own chats in a sidebar section.
5. Give the Manager a task.

Choose a work tracking mode at appointment: GitHub, local, or both with one
authoritative status record. Task recording is mandatory in every mode. See
[Team work tracking](docs/design/team-work-tracking.md).

Step-by-step with expected results: [Getting started](docs/getting-started.md). Daily use: [Working with teams](docs/working-with-teams.md).

## Documentation

| Read | For |
|---|---|
| [Set up on a new computer](docs/new-machine.md) | Ordered setup of a new machine, fresh or moved: Chief of Staff, teams, Office, relay, Monitor |
| [Getting started](docs/getting-started.md) | First install and first team |
| [Working with teams](docs/working-with-teams.md) | Who to talk to, adding teams and agents, two computers |
| [Teams reference](docs/operations/teams.md) | Commands, approvals, structure |
| [Roster](docs/design/project-roster.md) | Shared and local parts, commands |
| [Catalog](catalog/README.md) | Templates and their parameters |
| [Chief of Staff](packages/chief-of-staff-v1/README.md) | Personal agent setup |
| [Documentation index](docs/00-index.md) | Canonical documentation index, marketing routes, and templates |
| [AGENTS.md](AGENTS.md) | Instructions for coding agents working on this repository |

## Status

Early (0.1). The design is tested with unit tests and live Codex Desktop runs on Windows. Expect changes; see [CHANGELOG.md](CHANGELOG.md).

## Contributing and security

[CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) · [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)

## License

[MIT](LICENSE)
