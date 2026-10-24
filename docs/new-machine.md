# Set up Codex Bot Mode on a new computer

One ordered guide for a new Windows computer: Chief of Staff first, then your project teams, then Codex Office, the Desktop relay and the Agent Monitor. Every step has a command, the result to expect and a check. Each step says what differs between the two ways of arriving:

- **Fresh**: a new setup with no earlier Codex Bot Mode data.
- **Move**: you already run Codex Bot Mode on another computer and bring it here.

Codex chats exist only in the Codex Desktop where they were created. Their IDs (`codex://threads/<id>`) are local to one computer. The team, profiles, knowledge and learned memory travel with Git; the chats do not, so a move means one **replacement chat per agent**, with the same profile and memory (Step 4).

Placeholders: `<cbm>` is your clone of this repository, `<cos>` the private Chief of Staff folder, `<project>` a project folder. Use absolute paths. Commands are PowerShell; only Windows is tested.

## Before you leave the old computer (Move only)

1. Commit and push the Chief of Staff repository and every project repository: `git status` must be clean, `git log origin/<branch>..` must be empty.
2. Finish or cancel open requests. The request ledger (`.bot-mode/local/requests.json`) is local and does not travel. List what is open: `python -B .bot-mode/requests.py --project <project> list`.
3. Note anything stored outside Git: connectors and sign-ins in Codex, the Monitor automation, the signing certificate you trusted for Office, and any `.env` files. None of it moves by itself.
4. Keep the old computer usable until Step 7. Two Monitors or two Offices on the same project at once produce double reports and two writers per thread (see Step 8).

## 1. Prerequisites

Install Codex Desktop and sign in, Python 3.10 or newer, Git, and the GitHub CLI.

```text
python --version
git --version
gh auth login
gh auth status
```

Expected: Python 3.10+, a Git version, and `gh auth status` reports you as logged in. Check that Codex Desktop starts and shows your account.

Suggested layout (any location works): the Codex Bot Mode clone, the Chief of Staff folder and your projects as siblings, for example `C:\work\codex-bot-mode`, `C:\work\chief-of-staff`, `C:\work\<project>`. If the drive letters differ from the old computer, you will edit a few absolute paths in Steps 3, 4 and 7.

## 2. Clone this repository and install the plugin

```text
git clone <repository URL> <cbm>
cd <cbm>
python -B scripts/build_plugin.py
```

Expected: `Built <home>\plugins\codex-bot-mode` and `Registered in <home>\.agents\plugins\marketplace.json`. In Codex open **Plugins > Personal** and install **Codex Bot Mode**. Restart Codex.

Check: Settings > Plugins shows the version that `<home>\plugins\codex-bot-mode\.codex-plugin\plugin.json` states (0.1.5 at the time of writing). Codex can keep showing an older copy after a rebuild; if the version is old after a restart, uninstall the plugin in Plugins > Personal, install it again, and restart once more. The plugin provides the team launch and chat replacement skills used in Steps 3 and 4.

```text
python -B scripts/build_plugin.py --out <any empty folder> --no-register
```

builds into a folder without touching Codex; useful as a dry run.

## 3. Chief of Staff first

The Chief of Staff (COS) is your single entry point across projects, so it comes before any project team. It lives in its own **private** repository, never inside a product repository and never public.

**Fresh.** Create the folder from the template, make it a private repository and push:

```text
New-Item -ItemType Directory -Force <cos>
Copy-Item -Recurse -Force <cbm>\packages\chief-of-staff-v1\* <cos>
cd <cos>
git init
git add -A
git commit -m "Chief of Staff from template"
gh repo create <name> --private --source . --push
```

(The folder is new, so overwriting is safe. The `*` also copies the hidden `.agents` folder and `.gitignore`.)

**Move.** Clone your existing private repository. Do not copy the template over it: the knowledge files, `agent/STATE.md` and `agent/memory/` are populated and must stay byte for byte. To adopt only template files that are missing, use the "copy only absent files" command in `packages/chief-of-staff-v1/README.md`.

```text
git clone <private COS repository URL> <cos>
```

**Appoint.** In Codex add `<cos>` as a project, start a new chat in it and say:

> Use $codex-chief-of-staff as my Chief of Staff and load its profile and memory.

The chat moves itself into a sidebar section named "Chief of Staff". On a move this is a **new chat**: the old computer's COS chat does not exist here. It continues from `agent/STATE.md`, `knowledge/` and `agent/memory/` in the repository.

**Memory check.**

```text
python -B <cos>\scripts\bot_mode.py --project <cos> --bot chief-of-staff memory
```

Expected: `[]` on a fresh install; the stored lessons (each with source and evidence) on a move. An error or an empty list on a move means the clone is incomplete: check `git status` and `agent/memory/`.

**Link projects to the COS.** So that team launches add new projects to its knowledge automatically, create the user-level config (outside every project, per computer):

```text
New-Item -ItemType Directory -Force $HOME\.codex-bot-mode
Set-Content -Encoding utf8 $HOME\.codex-bot-mode\config.json '{"chief_of_staff": "<cos with forward slashes>"}'
```

Check: `Get-Content $HOME\.codex-bot-mode\config.json` shows the path. On a move, `knowledge/projects.md` lists the old computer's paths; update them to this computer's.

**Per computer, not in Git:** the connectors (mail, calendar, tasks, files) in Codex for the COS project (sign in again; credentials stay in Codex), and the backup schedule. To back up hourly on Windows, schedule `<cos>\scripts\backup.ps1` in Task Scheduler on **one** computer only; two computers committing and pushing the same repository by schedule collide.

## 4. Projects and teams

Do one project at a time. For each project the Manager comes first, then the specialists.

**Fresh.** In Codex add `<project>` as a project, open a chat in it and say "Start a Development team in this project." Review the plan, confirm. Your chat becomes the Manager and the other agents get their own chats in a sidebar section. Details: [Getting started](getting-started.md). Without the plugin: `python -B scripts/team.py plan-team development --project <project>`, then the same with `install-team --tracking-mode github --approval once --confirm`, then one chat per agent and `roster.py set-thread` (see "Replace the chats" below).

**Move.** Clone the project from Git. The team is already there (`team/<Team>/<agent>/`, `.bot-mode/roster.json`, learned memory in each `agent/memory/`), but the chat links are not: they live in `.bot-mode/local/bindings.json`, which is not in Git.

```text
git clone <project repository URL> <project>
python -B <project>\.bot-mode\roster.py --project <project> links
```

Expected after a clone: every participant with `"link": null`. If you copied the folder instead of cloning it, the old computer's links are still listed; replacing them (below) moves them to history.

**Replace the chats.** One replacement chat per agent, Manager first. For each agent, in a new Codex chat inside the project, say "Replace the <Name> chat." (skill `codex-replace-chat`). On a new computer the old chat cannot be read, so the new chat continues from the agent's `agent/STATE.md` and memory. It asks you to confirm, creates the chat with the appointment prompt, records it and moves it into the team's sidebar section. You can ask the Chief of Staff chat to start these replacements one agent at a time, Manager first; each one still needs your confirmation.

By hand, for one agent:

1. Create a chat in the project with this first message: "You are appointed as <display name> (<role>) in project <name>. Your workspace is <absolute workspace>. Load <absolute workspace>/.agents/skills/codex-<bot>/SKILL.md, then your profile and memory with --project <absolute workspace>, and place yourself in the sidebar section '<section>'."
2. Copy its thread ID from the chat link (`codex://threads/<thread-id>`) and record it: `python -B <project>\.bot-mode\roster.py --project <project> set-thread <bot> <thread-id> --reason "this computer"`

Check: `roster.py --project <project> links` lists every active participant with a `codex://threads/...` link, and each agent's memory loads: `python -B <workspace>\scripts\bot_mode.py --project <workspace> --bot <bot> memory`.

Notes for a move: `project.tooling` in `roster.json` names the old computer's tooling folder; the launch skills find the plugin or the clone by themselves, so leave it unless a skill reports it missing. A Project Director and Managers read colleagues' chats from the roster, so replace all agents of a project before assigning work. Open requests were finished or cancelled before the move; new ones are recorded in the new local ledger.

**Register the projects for Office and the Monitor.** Office and the Monitor read an explicit list of projects from `<cbm>\.bot-mode\local\office-project-roots.json` (local, not in Git). Create it, or copy it from the old computer and fix every `root`:

```json
{"schema": 1,
 "projects": [{"id": "my-project", "name": "My Project", "root": "C:/work/my-project", "repository": "owner/repo"}],
 "standalone": [{"id": "chief-of-staff", "name": "Chief of Staff", "root": "C:/work/chief-of-staff", "thread_id": "<this computer's COS chat id>"}]}
```

`root` must be absolute; `repository` and `thread_id` are optional; ids are lowercase letters, digits and dashes. A project with no ledger rows or task records is reported by the Monitor as UNKNOWN unless you add `"tracked_work": "none"`. Check (run from `<cbm>`):

```text
python -B -c "from scripts.team_room import registry as r; d=r.load_registry('<cbm>/.bot-mode/local/office-project-roots.json'); print(d['status'], [p['id'] for p in d['projects']])"
```

Expected: `ok` and your project ids. Office's **New project** page can also add an existing project.

## 5. Codex Office

Office is the local web app on http://localhost:24242 (team rooms, personal chats, settings). It serves one project, whose roster supplies the participants. Start it either way; both need Codex Desktop installed.

**From the repository (works today).**

```text
powershell -File <cbm>\scripts\start_codex_office.ps1 -Project <project>
```

(or double-click `Codex Office.cmd` to serve this repository). Expected: `Codex Office is ready: http://localhost:24242`. Check: `Invoke-RestMethod http://localhost:24242/api/state` returns the participants of the project. If it fails, read `<project>\.bot-mode\team-room-v1\office\office.stderr.log`. After roster changes, restart Office.

**Windows installer (tray controller).** *Waits on the Office 0.1.6 release: the sources are at 0.1.5, and 0.1.6 (the service CPU fix) is not released yet. Until it is, use the repository start above. This paragraph needs the released version and its checksum once they exist.* The controller (in the development repository under `desktop/`, which a public clone does not contain) is a signed per-user installer with no elevation, no autostart and no automatic update. Every step is local:

1. Get the signed installer from the release instructions (there is no public download) and compare its checksum with the release evidence: `Get-FileHash <installer>.exe -Algorithm SHA256`.
2. The signing certificate is self-signed; the release notes give its publisher name and thumbprint, and the public certificate file is in the `desktop` folder of the development repository. Windows calls the signature untrusted until you import the public certificate, and only after you compared the thumbprint with the installer's signature:

```text
Import-Certificate -FilePath <cbm>\desktop\<certificate file>.cer -CertStoreLocation Cert:\CurrentUser\Root
Import-Certificate -FilePath <cbm>\desktop\<certificate file>.cer -CertStoreLocation Cert:\CurrentUser\TrustedPublisher
```

3. Run the installer as the current user, then start **Codex Office** from the Start menu. On the first run choose the project folder, confirm the detected Codex Desktop (or **Locate** its `codex.exe`), and select **Start**. Wait for authenticated health and the running state, then open Office in Chrome.
4. **Quit Office** from the tray menu requests a safe stop. Controls, stale-state recovery and the safety lock are described in `desktop/README.md` of the development repository.

Only one program may hold port 24242: stop the repository-started Office before the controller starts one. The controller never attaches to an existing listener.

## 6. Desktop relay (optional, recommended)

The relay is a small loopback service (127.0.0.1:24243) that lets Office show your native Desktop chats and lets the Monitor check whether a request's recipient has started. Without it Office runs in discussion mode and the Monitor reports `request_start_unavailable`. It lives in `<cbm>\.bot-mode\desktop-relay` (local; its `relay.json` holds a token, never commit or share it).

Only the **lead Manager's chat of the Codex Bot Mode clone** can register it, because it needs that chat's Desktop context. This applies to the development repository, which carries its own team and roster; a public clone has no appointed Manager of its own, so it cannot register a relay. Replace that Manager chat first (Step 4 applied to `<cbm>`), then **in that chat**, with Office stopped:

```text
powershell -File <cbm>\scripts\start_codex_office.ps1
```

Check:

```text
cd <cbm>
python -B -m scripts.team_room.desktop_relay --root .bot-mode\desktop-relay --probe
```

Expected: `Registered Desktop relay is already available.` (exit 0). `Desktop relay is unavailable.` (exit 1) means it is not registered. The launcher without `-Project` then starts Office with `--desktop-relay`; the tray controller does the same whenever `<project>/.bot-mode/desktop-relay/relay.json` exists in the project you chose, so choose `<cbm>` to use it.

The relay depends on the running Desktop instance. After a Codex Desktop restart: stop Office, write `shutdown` into `.bot-mode\desktop-relay\shutdown.flag`, wait for the relay process to exit, then repeat the registration from the Manager chat. Closing Office does not stop the relay. Details: `docs/operations/team-room.md` of the development repository.

## 7. Agent Monitor

The Monitor is a read-only agent in the Chief of Staff project that audits all registered projects every 20 minutes and reports to the COS. It has three parts: its workspace (`<cos>\team\Monitoring\agent-monitor`), its local settings (`agent\monitoring.json`) and a scheduled Codex automation. See `docs/operations/agent-monitor.md` for what it reports.

**Move.** The workspace, its scripts and `monitoring.json` came with the COS clone. Then:

1. Edit `agent\monitoring.json`: `registry_path` (and `desktop_relay_root` if you set it) to this computer's paths, `report_target_thread_id` to **this computer's COS chat id** (the chat from Step 3). Keep the rest.
2. Bring the scripts to the current version with the maintainers' installer (it is in the development repository under `work/rollout/cbm227/`; a public clone installs the scripts by copying, see the note after the Fresh commands). Backups go outside any repository:

```text
python -B <cbm>\work\rollout\cbm227\apply_rollout.py update --monitor <cos>\team\Monitoring\agent-monitor --repo <cbm> --backup-root <backup folder outside every repository>
```

3. **Pause the Monitor automation on the old computer** before enabling the new one, otherwise both report.

**Fresh.**

```text
python -B <cbm>\scripts\roster.py --project <cos> add-team Monitoring --section "Chief of Staff"
python -B <cbm>\scripts\team.py plan-agent --project <cos> --team Monitoring --template agent-monitor
python -B <cbm>\scripts\team.py add-agent --project <cos> --team Monitoring --template agent-monitor --actor chief-of-staff --approval once --confirm
python -B <cbm>\work\rollout\cbm227\apply_rollout.py apply --monitor <cos>\team\Monitoring\agent-monitor --repo <cbm> --backup-root <backup folder outside every repository>
```

(`plan-agent` is the dry run; run `add-agent` only after you reviewed it.) Create a chat for the Monitor (appointment prompt as in Step 4, bot `agent-monitor`) and record it with `roster.py set-thread`.

Public clone without the installer: copy `scripts/task_continuity.py`, `monitor_checkpoint.py`, `monitor_run.py` and `status_sources.py` into the Monitor workspace's `scripts/`, and `scripts/team_room/__init__.py`, `relay_url.py`, `registry.py`, `manifest.py` and `roster.py` into its `scripts/team_room/`. All nine files are needed; without the `team_room` ones the Manager signals are skipped.

Then create `agent\monitoring.json`:

```json
{"schema": 1, "report_target_thread_id": "<this computer's COS chat id>", "registry_path": "<cbm>/.bot-mode/local/office-project-roots.json", "interval_minutes": 20}
```

**Both: check one cycle by hand** from the Monitor workspace:

```text
python -B scripts\monitor_run.py --run-id first-check
```

Expected for a healthy setup: `"ok": true`, `"audit_complete": true`, `"coverage": "complete"`. A project the Monitor cannot see any work in gives `coverage: partial` and an UNKNOWN scope (Step 4, `tracked_work`). When a run sets `report_required: true` (any new finding), record how the report went, or the next run in the same window reports `concurrent_audit`:

```text
python -B scripts\monitor_checkpoint.py --state <state_file from the output> --set-delivery-status sent
```

(use `delivery_unknown` if you did not send the report). Check the project list the Monitor sees: the `projects` count in the output.

**Schedule it.** Create a project automation in Codex (20 minutes, GPT-6 Luna, low reasoning) in the COS project with the prompt from `<cbm>\work\rollout\cbm227\automation-prompt.md`, after changing the workspace path in its first lines to this computer's Monitor folder. Ask the COS chat to do it ("create the Monitor's 20 minute project automation") or use Codex's automations page. Acceptance: the next scheduled turn runs `monitor_run.py` first and either reports to the COS chat or finishes cleanly with `audit_complete true`. A scheduled turn that completes is not proof of a finished audit; look for the checkpoint block at the top of `agent\STATE.md` (`audit_status: complete`).

## 8. Final checklist and troubleshooting

Tick every line:

- [ ] `python --version`, `git --version`, `gh auth status` are fine; Codex Desktop is signed in.
- [ ] The plugin shows the expected version after a Codex restart.
- [ ] The COS chat is appointed and `bot_mode.py ... memory` returns the expected memory; `config.json` names `<cos>`.
- [ ] For each project: `roster.py links` has a link for every active agent; each agent's memory loads.
- [ ] `office-project-roots.json` loads (`ok`) and lists every project with this computer's paths.
- [ ] Office answers on http://localhost:24242 (`/api/state`).
- [ ] Relay probe exits 0 (if used).
- [ ] `monitor_run.py` ends with `audit_complete: true`; the automation is created on this computer and paused or deleted on the old one.
- [ ] Move only: nothing important is left uncommitted on the old computer.

| Symptom | Likely cause and fix |
|---|---|
| `roster.py links` shows `null` links after a clone | Expected: chats are local. Replace the chats (Step 4) |
| An agent answers as itself but not from its memory | Wrong `--project`: pass the agent's own workspace folder, not the repository root |
| Plugin version is old | Restart Codex; otherwise uninstall and install it again, then restart (Step 2) |
| Office will not start | Port 24242 is taken (another Office or the controller), or Codex Desktop is not installed; read `office.stderr.log` |
| Office has no native chats | Relay not registered or Desktop restarted; probe it and register again (Step 6) |
| Monitor: `concurrent_audit` | A previous run's delivery was never recorded; set its delivery status (Step 7) |
| Monitor: `manager_delivery_unavailable` | A project has no Manager with a chat; replace the Manager chat (Step 4) |
| Monitor: `request_start_unavailable` | No relay for that project's chats (Step 6) |
| Two computers report the same alerts | Two Monitor automations are active; keep one |

**Running two computers at once.** Keep one active writer per chat and per schedule. Each computer has its own chats for the same agents; do not paste one chat's work into the other's, and hand over through the repository (commit, push, pull) and `agent/STATE.md`. Run the Office, the relay and the Monitor automation on one computer at a time; scheduled backups on one. Learned memory merges through Git without conflicts. See [Working with teams: Two computers](working-with-teams.md#two-computers-or-several-people).

