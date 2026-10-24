"""Task status from authoritative sources for the Agent Monitor.

Sources per project root, in this order of authority:
1. GitHub issue state labels (state:ready, state:active, state:review, state:needs-owner,
   backlog, future) of the repository from the registry or the git origin; closed issues
   and closed or merged pull requests make linked work done.
2. The request ledger .bot-mode/local/requests.json (one task per linked issue or
   pull request, otherwise one per request).
3. Task records in work/tasks/*.md that declare an explicit state.

A task without status evidence is never reported as backlog. A project that yields no
status-bearing task is UNKNOWN unless the registry entry declares "tracked_work": "none".
Read only: nothing is dispatched or repaired.
"""
import json
import re
import shutil
import subprocess
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import task_continuity as tc
except ImportError:  # imported as scripts.status_sources
    from scripts import task_continuity as tc

LEDGER = ".bot-mode/local/requests.json"
LABEL_STATUS = {"state:needs-owner": "blocked", "state:review": "review", "state:active": "in-progress",
                "state:ready": "ready", "state:backlog": "backlog", "backlog": "backlog",
                "state:future": "future", "future": "future"}
LABEL_ORDER = list(LABEL_STATUS)
LEDGER_STATUS = {"open": "in-progress", "in-progress": "in-progress", "review": "review", "blocked": "blocked",
                 "backlog": "backlog", "ready": "ready", "paused": "paused", "future": "future"}
NO_HANDOFF = {"backlog", "ready", "paused", "future"}
STARTED = {"in-progress", "review", "blocked", "done"}
STALE_DAYS = 3  # an unfinished outcome without GitHub state older than this is history, not a live stall
WORK_URL = re.compile(r"https://github\.com/([^/]+/[^/]+)/(issues|pull)/(\d+)")


def when(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def stamp(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def repository(project):
    if project.get("repository"):
        return project["repository"]
    try:
        out = subprocess.run(["git", "-C", project["root"], "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?\s*$", out.stdout) if out.returncode == 0 else None
    return match.group(1) if match else None


def github_fetch(repo):
    """Issues and pull requests of one repository through gh; raises when gh or the network fails."""
    gh = shutil.which("gh")
    if not gh:
        raise RuntimeError("gh not installed")
    rows = {}
    for kind, fields in (("issue", "number,state,labels,updatedAt,url,title"), ("pr", "number,state,updatedAt,url,title,headRefOid")):
        done = subprocess.run([gh, kind, "list", "-R", repo, "--state", "all", "--limit", "500", "--json", fields],
                              capture_output=True, text=True, encoding="utf-8", timeout=90)
        if done.returncode != 0:
            raise RuntimeError(f"gh {kind} list failed")
        rows[kind] = json.loads(done.stdout)
    return rows


def work_key(url):
    match = WORK_URL.fullmatch(url or "")
    return (match.group(2), match.group(3)) if match else None


def stage_of(request):
    worker, rid = request.get("to", ""), request.get("id", "")
    if worker in ("code-reviewer", "reviewer") or "review" in rid:
        return "review"
    if worker in ("verifier", "qa") or "qa" in rid:
        return "qa"
    return "implementation"


REVIEW_ROLES = {"reviewer", "verifier", "qa"}


def is_review_work(request):
    """True only for a request addressed to a reviewer or verifier; the request id never decides it."""
    worker = str(request.get("to") or "")
    # whole components of the bot id: code-reviewer-2 counts, preview-developer and nonverifier-developer do not
    return bool(REVIEW_ROLES & set(worker.lower().split("-"))) and worker != request.get("author")


def ledger_requests(root, problems):
    path = Path(root) / LEDGER
    if not path.is_file():
        return [], None
    try:
        rows = json.loads(path.read_text(encoding="utf-8-sig"))["requests"]
        return [r for r in rows if isinstance(r, dict) and r.get("id") and r.get("state") != "cancelled"], path
    except (OSError, ValueError, KeyError, TypeError):
        problems.append("ledger unreadable")
        return [], path


def status_label(labels):
    names = {label.get("name") for label in labels or [] if isinstance(label, dict)}
    for name in LABEL_ORDER:
        if name in names:
            return LABEL_STATUS[name]
    return None


def linked_pulls(issue_number):
    """Pull requests whose title closes this issue (closing reference only, never owner/repo#N)."""
    pattern = re.compile(r"\b(?:fixes|closes|resolves)\s+(?<![\w/])#" + str(issue_number) + r"\b", re.IGNORECASE)
    rows = getattr(make_task, "issues_map", None) or {}
    return [row for k, row in rows.items() if k[0] == "pull" and pattern.search(row.get("title", ""))]


def make_task(key, requests, github, ledger_path, now, interval, project_requests=()):
    """(status, task) of one work item from its ledger requests and GitHub issue; task is None when done."""
    requests = sorted(requests, key=lambda r: when(r.get("opened")) or datetime.min.replace(tzinfo=timezone.utc))
    latest = requests[-1] if requests else {}
    live = [r for r in requests if r.get("state") != "answered"]
    current = live[-1] if live else latest
    if github and github.get("state") in ("CLOSED", "MERGED"):
        return "done", None
    pulls = linked_pulls(github["number"]) if github and github.get("number") and "/pull/" not in str(github.get("url")) else []
    # Review stage only: issue labelled state:review, no live implementation or correction request, and every
    # linked PR merged or closed. An active issue or a live implementation request stays in progress.
    if (pulls and not any(p.get("state") == "OPEN" for p in pulls)
            and status_label(github.get("labels")) == "review"
            and all(is_review_work(r) for r in live)):
        return "done", None
    label = status_label(github.get("labels")) if github else None
    if label:
        status = label
    elif live:
        status = LEDGER_STATUS.get(current.get("state"), "in-progress")
        if status == "in-progress" and stage_of(current) == "review":
            status = "review"
    elif latest.get("outcome") == "done" or not latest:
        return "done", None
    elif now - (when(latest.get("answered")) or now) > timedelta(days=STALE_DAYS):
        return "stale-partial", None
    else:
        status = "in-progress"
    url = (github or {}).get("url") or (latest.get("link") if work_key(latest.get("link")) else None)
    next_check = stamp(now + timedelta(minutes=interval))
    due = when(current.get("due")) if live and not label else None
    if due:
        next_check = stamp(due)
    handoffs, candidate = [], None
    if requests and status not in NO_HANDOFF:
        opened = stamp(when(latest.get("opened")) or now)
        answered = when(latest.get("answered")) if latest.get("state") == "answered" else None
        handoffs = [{"id": latest["id"], "worker": latest.get("to") or "unknown", "stage": stage_of(latest),
                     "requested_at": opened, "acknowledged_at": opened, "started_at": opened,
                     "finished_at": stamp(answered) if answered else None,
                     "evidence": [latest.get("link") or f"ref:ledger:{ledger_path}#{latest['id']}"]}]
        # Candidate and author resolution:
        # 1. Structured candidate/author fields in the request (--candidate / --author)
        # 2. Head SHA of linked PR (read from GitHub, authoritative)
        # 3. Fallback: 40-hex SHA and author from summary (e.g. 'author <bot>' or 'author: <bot>')
        # 4. Fallback: SHA from request ID / GitHub PR and implementation chain author
        sha = latest.get("candidate")
        ambiguous = False
        author = latest.get("author")

        # Source 2: Linked PR head SHA
        if not sha:
            if github and github.get("headRefOid"):
                sha = github["headRefOid"]
            elif hasattr(make_task, "issues_map") and make_task.issues_map:
                for r in reversed(requests):
                    lk = work_key(r.get("link"))
                    if lk and lk[0] == "pull":
                        pr_item = make_task.issues_map.get(lk)
                        if pr_item and pr_item.get("headRefOid"):
                            sha = pr_item["headRefOid"]
                            break
                if not sha and pulls:
                    heads = {p["headRefOid"] for p in pulls if p.get("state") == "OPEN" and p.get("headRefOid")}
                    if len(heads) == 1:
                        sha = heads.pop()
                    elif heads:
                        ambiguous = True  # several open PRs with different heads: no guess

        summary_text = latest.get("summary") or ""
        if not sha and not ambiguous:
            shas_40 = re.findall(r"(?<![0-9A-Za-z])[0-9a-f]{40}(?![0-9A-Za-z])", summary_text)
            unique_shas = list(dict.fromkeys(s.lower() for s in shas_40))
            if len(unique_shas) == 1:
                sha = unique_shas[0]
            elif len(unique_shas) > 1:
                marker_match = re.search(r"\b(?:candidate|commit)[:\s]+([0-9a-f]{40})\b", summary_text, re.IGNORECASE)
                if marker_match:
                    sha = marker_match.group(1).lower()
        if not author:
            author_match = re.search(r"\bauthor[:\s]+([a-z][a-z0-9-]{0,47})\b", summary_text, re.IGNORECASE)
            if author_match:
                author = author_match.group(1).lower()
        if not sha and not ambiguous:
            shas = re.findall(r"(?<![0-9A-Za-z])[0-9a-f]{7,40}(?![0-9A-Za-z])", latest["id"])
            sha = shas[-1] if shas else (github or {}).get("headRefOid")
        if not author:
            # The author is the latest implementation worker of this work item, found by link or by the
            # request id prefix the Manager uses for the whole chain (cbm244-review-x belongs to cbm244-*).
            prefix = re.split(r"-(?:review|qa)-", latest["id"])[0]
            chain = [r for r in project_requests if r.get("id", "").startswith(prefix + "-") and stage_of(r) == "implementation"]
            author = next((r.get("to") for r in reversed(requests[:-1] or chain) if stage_of(r) == "implementation"), None)
            if not author and latest.get("from") and latest.get("from") not in ("manager", "owner"):
                author = latest.get("from")

        if (status == "review" or stage_of(latest) == "review") and sha and author:
            candidate = {"id": sha, "author": author}
    blocker = None
    if status == "blocked":
        reason = current.get("blocker") or (label and "label state:needs-owner")
        owner = current.get("unblock_owner") or (label and "owner")
        resume = current.get("resume_condition") or (label and "owner decision recorded on the issue")
        if reason and owner and resume:
            blocker = {"reason": str(reason), "owner": str(owner), "resume_condition": str(resume),
                       "next_check_at": stamp(when(current.get("next_check")) or now + timedelta(minutes=interval)),
                       "evidence": [url or f"ref:ledger:{ledger_path}#{current.get('id')}"]}
    updated = max([t for t in (when(latest.get("answered")), when(latest.get("opened")),
                               when((github or {}).get("updatedAt"))) if t] or [now])
    evidence = [f"ref:github:{url}"] if github and url else []
    if requests:
        evidence.append(f"ref:ledger:{Path(ledger_path).as_posix()}#{','.join(r['id'] for r in requests[-3:])}")
    acceptance = current.get("acceptance")
    return status, {
        "id": key, "manager": str(current.get("responsible_manager") or "manager"),
        "executor": handoffs[-1]["worker"] if handoffs else (current.get("executor") or None),
        "status": status, "start_approved": status in STARTED,
        "next_action": str(current.get("next_action") or (github or {}).get("title") or latest.get("summary") or key),
        "acceptance": [acceptance if isinstance(acceptance, str) and acceptance.strip() else f"See {url or key}"],
        "evidence": evidence or [f"ref:work-item:{key}"], "updated_at": stamp(min(updated, now)),
        "next_check_at": next_check, "handoffs": handoffs, "blocker": blocker,
        "candidate": candidate, "review": None, "accepted": None,
        "source": "github+ledger" if github and requests else ("github" if github else "ledger")}


def project_status(project, now, interval=20, fetch=None):
    """(tasks, summary) of one registry project. summary lists sources, counts and problems."""
    root = Path(project["root"])
    if not root.is_dir():
        raise FileNotFoundError("project root unavailable")
    problems, tasks, counts, sources, hygiene = [], [], Counter(), [], []

    if (root / "work" / "tasks").is_dir():
        try:
            records = tc.produce_snapshot(root, now, explicit_only=True)["tasks"]
        except Exception as error:
            records = []
            problems.append(f"task records unreadable ({type(error).__name__})")
        for task in records:
            task["source"] = "task-record"
            tasks.append(task)
            counts[task["status"]] += 1
        if records:
            sources.append(f"work/tasks:{len(records)}")

    requests, ledger_path = ledger_requests(root, problems)
    groups = {}
    for request in requests:
        key = work_key(request.get("link"))
        groups.setdefault(key or ("request", request["id"]), []).append(request)

    issues = {}
    repo = repository(project)
    if repo:
        try:
            data = (fetch or github_fetch)(repo)
            sources.append(f"github:{repo}")
            for row in data.get("issue", []):
                issues[("issues", str(row["number"]))] = row
            for row in data.get("pr", []):
                issues[("pull", str(row["number"]))] = row
        except Exception as error:
            problems.append(f"github unavailable ({type(error).__name__}: {error})")
    # GitHub is authoritative: a record without an issue link belongs to the issue that names its id in the title.
    for key in [k for k in groups if k[0] == "request"]:
        rid = key[1].casefold()
        match = next((k for k, row in issues.items() if len(rid) >= 8 and rid in str(row.get("title", "")).casefold()), None)
        if match is not None:
            linked = groups.pop(key)
            groups.setdefault(match, []).extend(linked)
    for key, row in issues.items():
        if key[0] == "issues" and row.get("state") == "OPEN" and status_label(row.get("labels")):
            groups.setdefault(key, [])
    if requests:
        sources.append(f"ledger:{len(requests)} requests")

    make_task.issues_map = issues
    for key, items in groups.items():
        name = "req-" + key[1] if key[0] == "request" else ("issue-" if key[0] == "issues" else "pr-") + key[1]
        status, task = make_task(name, items, issues.get(key), ledger_path, now, interval, requests)
        counts[status] += 1
        if status == "done" and any(r.get("state") != "answered" for r in items) and issues.get(key, {}).get("state") in ("CLOSED", "MERGED"):
            hygiene.extend(r["id"] for r in items if r.get("state") != "answered")
        if task:
            tasks.append(task)

    for task in tasks:
        task["id"] = f"{project['id']}/{task['id']}"
        for handoff in task.get("handoffs", []):
            handoff["id"] = f"{project['id']}/{handoff['id']}"
    declared_none = project.get("tracked_work") == "none"
    state = "known" if counts else ("declared-none" if declared_none else "unknown")
    return tasks, {"state": state, "total": sum(counts.values()), "by_status": dict(counts),
                   "sources": sources, "problems": problems, "hygiene": hygiene}

