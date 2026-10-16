# Chief of Staff Template

A universal personal agent for one owner: executive assistant, chief of staff and
the owner's link to the digital world. It is the owner's single entry point to all
projects and teams. Requires Codex Desktop and Python 3.10+.

The template contains no personal data, memory, chat IDs or credentials. Each
owner installs it once, in a separate private project, so its memory and tools stay
apart from any product repository.

## Communication chain

```text
Owner -> Chief of Staff -> Project Director -> Team Manager -> Specialist
```

With one team in a project, the Chief of Staff talks to that team's Manager. The
owner can still talk directly to any Director, Manager or specialist; whoever
receives work directly informs the next level up.

## Install

1. Create a new private project folder in the owner's user folder, for example
   `~/chief-of-staff` on macOS/Linux or `%USERPROFILE%\chief-of-staff` on Windows.
   Another location is fine if the owner prefers it. Do not install it inside a
   product repository or a public repository.
2. Copy this package's contents into the private project folder.
   Manual adoption and update procedure:
   - When setting up for the first time, copy the template files into the empty folder.
   - When adopting or updating the Chief of Staff template in an existing project
     that already has populated owner data, copy only new or absent template files;
     preserve all existing populated files in `knowledge/` (`personal.md`, `owner.md`,
     `preferences.md`, `people.md`, `projects.md`, `accounts.md`, `routines.md`, `README.md`),
     as well as `agent/STATE.md` and `agent/memory/`, byte-for-byte. Never overwrite
     existing populated knowledge or state with the empty template scaffold.
   - On Linux/macOS, copy only absent files including hidden files:
     `rsync -a --ignore-existing <package>/ <target>/`
   - On Windows PowerShell, copy absent files only:
     `$src = "<package>"; $dst = "<target>"; Get-ChildItem -Recurse -Force $src | Where-Object { -not $_.PSIsContainer } | ForEach-Object { $rel = $_.FullName.Substring($src.Length).TrimStart('\/'); $targetFile = Join-Path $dst $rel; if (-not (Test-Path $targetFile)) { $parent = Split-Path $targetFile; if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }; Copy-Item $_.FullName -Destination $targetFile } }`
   Initialize the folder as its own private Git repository and push regularly so the
   owner knowledge and memory are backed up. Never make that repository public.
   On Windows, schedule `scripts/backup.ps1` (for example hourly with Task
   Scheduler). It commits and pushes only when something changed and writes the
   last failure to `.backup/last-error.txt`.
3. Open the folder in Codex Desktop, start a chat and appoint it:
   "Use $codex-chief-of-staff as my Chief of Staff and load its profile and memory."
4. Fill `knowledge/` with the owner during the first conversations, recording
   confirmed facts with dates and confidence (confirmed vs. unknown). Never store
   credentials. Consult `knowledge/personal.md` selectively when relevant to personal
   errands, and never share personal context automatically with project agents.
5. Add the projects it coordinates to `knowledge/projects.md` with their paths. To
   have team launches add new projects automatically, create
   `~/.codex-bot-mode/config.json` with `{"chief_of_staff": "<this project's path>"}`.
6. Connect the tools it needs (mail, calendar, tasks, files) in Codex for this
   project. Credentials stay in Codex and its connectors, never in these files.

Verify fresh recall:

```text
python -B scripts/bot_mode.py --project <absolute-project-folder> --bot chief-of-staff memory
```

A clean installation returns [].
