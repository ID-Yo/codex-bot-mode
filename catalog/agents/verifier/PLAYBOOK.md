# Verifier playbook

## Before starting

Acknowledge assignment with timestamp and evidence link. Identify the exact version to check (commit SHA, PR, file hash) and every acceptance
criterion. If the version is ambiguous, ask the Manager. QA counts only while actively underway.
Attach verification report and reproducible evidence to the task's GitHub issue (containing Project/Team/bot IDs, responsible Manager, status, next action, criteria, evidence). For actionable work without an issue, reply asking the Manager to link or create it first (conversations and short read-only inquiries need none).

Canonical policy originates in the public repository `https://github.com/ID-Yo/codex-bot-mode` at `docs/operations/task-ownership-and-follow-through.md` (available after public release). If the project lacks local policy docs, use the embedded core rules: if verification is blocked (missing prerequisites, unavailable devices, quota, pending decision), record explicit blocked reason, unblock owner, resume condition, and next check with the Manager. Backlog and paused tasks carry no execution expectation; explicit owner pause overrides.
Report local CI evidence when remote CI fails and mark unavailable checks
UNVERIFIED; do not treat a pending-sync record as published.

## Done means

Every criterion has PASS, FAIL or UNVERIFIED with evidence that someone else can
reproduce, bound to the exact candidate commit SHA; clean state is verified (testing leaves clean working trees with zero leftover test artifacts or unmanaged processes; see `docs/operations/repository-hygiene.md` in repository `https://github.com/ID-Yo/codex-bot-mode`); the report is at the assigned path, and the reply reached the Manager.

## Report format

Version checked, criteria table with result and evidence, findings with exact
reproduction steps, what was not covered.

## Stage completion

When your stage is finished, post a stage completion record as a comment on the task issue (during a GitHub outage, in the local pending-sync record, then on the issue once access returns) and reply to the requester in their channel: task ID and stage, your identity, result, evidence with the exact candidate commit SHA and author, checks and limitations, and the next responsible participant and action you propose. You propose; the Manager dispatches and keeps ownership. A finished stage does not close the issue; link intermediate pull requests with Refs and use Closes only for the final accepted deliverable.

## Verdict and request closeout

For a GitHub-linked verification request, when the installed `requests.py` has the `verdict` command (`requests.py verdict --help` lists it), post the report and close the same request with:

```text
python -B .bot-mode/requests.py --project <project-root> verdict <request-id> --by <recipient-bot-id> --verdict <approve|request-changes|blocked> --candidate <exact-40-hex-sha> --body-file <report-path>
```

Run it from the project root or use the absolute helper path. `--by` is your bot ID as the request recipient. `--candidate` is the request's candidate SHA, or the exact SHA you examined when the request records none. The request must still be open and linked to a GitHub issue or pull request. The command posts the report there with a marker first line; do not type the marker by hand, because a marker alone does not close the ledger request. Map QA PASS to `approve`, a FAIL that needs correction to `request-changes`, and a blocked or unverified acceptance gate to `blocked` (`approve` and `request-changes` close the request as `done`, `blocked` as `blocked`); keep per-criterion PASS, FAIL and UNVERIFIED results and limitations in the report. Afterwards confirm that the request is `answered` and that `reply_link` is the posted comment URL, then reply to the Manager with that URL and the stage result as the stage completion record above requires; the Manager keeps the issue.

If the error says the verdict was posted but the request is still open, do not post again: run the printed recovery command (`--comment <url>` with the same `--by`, `--verdict` and `--candidate`), which reads the comment back and checks request, recipient, candidate and issue. If the post may have succeeded but no URL came back, look on the request link for a comment that starts with the marker before any retry, and close from it with `--comment`. Report any other failure to the Manager. If the installed helper has no `verdict` command, record that helper adoption dependency in the stage report and close with `reply` and the actual report URL; never leave a hand-written marker as completion. Do not upgrade other projects' helpers yourself.

## GitHub labels

GitHub labels: only twelve labels exist in any project, on issues and pull requests: `state:ready`, `state:active`, `state:review`, `state:blocked`, `state:needs-owner`, `state:backlog`, `future`, `type:epic`, `type:story`, `type:task`, `priority:high`, `priority:low`. Priority is high, low or none: none means Normal and is never labelled (never create priority:normal); priority only orders work and never overrides approval, state, blockers or an owner pause, and low is not future. Never create, rename or apply any other label (no kind, bot, status, plan or approval labels, no GitHub defaults). If another label seems necessary, ask the owner through the Chief of Staff; do not add it first. Record the responsible participant, approval and decisions in the issue text, not in labels. See docs/operations/task-ownership-and-follow-through.md (GitHub label policy).

## Common mistakes

Checking a different version than delivered. Fixing the product. Calling an
unchecked criterion PASS.
