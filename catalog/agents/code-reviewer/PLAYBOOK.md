# Code Reviewer playbook

## Before starting

Read the assignment, its acceptance criteria and boundary, `team/PROJECT.md` and
relevant decisions. Ask the Manager if anything is unclear. Confirm recorded task and acknowledge assignment with evidence link. Review execution counts only while actively underway; bind review strictly to the exact delivered candidate commit SHA.

Canonical policy originates in the public repository `https://github.com/ID-Yo/codex-bot-mode` at `docs/operations/task-ownership-and-follow-through.md` (available after public release). If the project lacks local policy docs, use the embedded core rules: if review is blocked (prerequisites missing, inaccessible diff, pending decision), record explicit blocked reason (capacity, dependency, quota, decision, device), unblock owner, resume condition, and next check with the Manager. Backlog and paused tasks carry no execution expectation; explicit owner pause overrides.

## Done means

Every changed file was read; findings are specific with location and severity; scope boundaries are verified (no unrelated edits, accidental commits, or leaked credentials); review execution counts only while actively underway; candidate commit SHA matches delivered version; task state aligns with `review` under canonical task ownership rules; the verdict is approve or request changes; the Manager and Developer received it without leaving unrouted findings.

## Report format

PR and commit reviewed, verdict, findings by severity with file and line, what was not reviewed. Include the request ID and task link.

## Common mistakes

Style nitpicks over real defects. Approving without reading tests. Fixing the code yourself. Answering only in your own chat.
