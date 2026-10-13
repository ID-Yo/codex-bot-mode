# Memory contract

This agent's learned memory lives in `agent/memory/` of its own workspace and is
committed with the project, so it travels to other computers and is shared by
everyone working in the repository. A new installation starts empty.

Each validated save adds one file to `episodes/` with a unique, time-ordered name,
so saves from different computers merge in Git without conflicts. `summary.md` is
derived and rebuilt locally; `pending/` and `writer.lock` stay local.

Lessons need a stable key, a relative source inside the workspace, an exact quote
and its SHA-256. Changed or missing sources suppress a lesson. Reusing a key
replaces active recall and keeps older episodes. Never store credentials or
personal data: memory is visible to everyone with access to the repository.
Native Codex-generated memory is separate.
