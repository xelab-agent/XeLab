#!/usr/bin/env python3
"""Read-only PR acceptance evidence: exact SHAs, head-bound checks and reviews. Never writes."""
import argparse
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener, HTTPRedirectHandler

SCHEMA = 2
UNSUCCESSFUL = {"failure", "timed_out", "cancelled", "startup_failure", "stale"}
THREADS_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!,$cursor:String){"
    "repository(owner:$owner,name:$name){pullRequest(number:$number){"
    "reviewThreads(first:100,after:$cursor){nodes{isResolved isOutdated}"
    "pageInfo{hasNextPage endCursor}}}}}"
)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward an authorization header to a redirect target.
        raise ValueError("GitHub redirect refused; verify the current repository name")


class GitHub:
    def __init__(self, repo, token):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or any(part in {".", ".."} for part in repo.split("/")):
            raise ValueError("repository must be owner/name")
        self.repo = repo
        self.base = "https://api.github.com/repos/" + repo
        self.token = token
        self.opener = build_opener(NoRedirect())

    def request(self, path):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        with self.opener.open(Request(self.base + path, headers=headers, method="GET"), timeout=30) as response:
            return json.load(response)

    def graphql(self, query, variables):
        if not query.lstrip().startswith("query"):
            raise ValueError("only read-only GraphQL queries are allowed")
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + self.token}
        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        request = Request("https://api.github.com/graphql", data=payload, headers=headers, method="POST")
        with self.opener.open(request, timeout=30) as response:
            body = json.load(response)
        if body.get("errors"):
            raise ValueError("GraphQL query returned errors")
        return body["data"]


def pages(api, path):
    page = 1
    while True:
        batch = api.request(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
        yield from batch
        if len(batch) < 100:
            return
        page += 1


def check_runs(api, head_sha):
    runs = []
    page = 1
    while True:
        batch = api.request(f"/commits/{head_sha}/check-runs?per_page=100&page={page}")["check_runs"]
        runs.extend(batch)
        if len(batch) < 100:
            return runs
        page += 1


def workflow_runs(api, head_sha):
    runs = []
    page = 1
    while True:
        batch = api.request(f"/actions/runs?head_sha={head_sha}&per_page=100&page={page}").get("workflow_runs", [])
        for run in batch:
            if run.get("head_sha") != head_sha:
                raise ValueError("workflow run head SHA does not match the pull request head")
            runs.append({
                "name": run["name"],
                "event": run.get("event"),
                "status": run["status"],
                "conclusion": run.get("conclusion"),
                "url": run["html_url"],
            })
        if len(batch) < 100:
            return runs
        page += 1


def classify(checks, statuses, workflows=()):
    if not checks and not statuses and not workflows:
        return "none-reported"
    if (any(c["status"] == "completed" and c["conclusion"] in UNSUCCESSFUL for c in checks)
            or any(c["status"] == "completed" and c["conclusion"] in UNSUCCESSFUL for c in workflows)
            or any(s["state"] in {"failure", "error"} for s in statuses)):
        return "failed"
    if (any(c["status"] == "completed" and c["conclusion"] == "action_required" for c in checks)
            or any(c.get("conclusion") == "action_required" or c.get("status") == "action_required" for c in workflows)):
        return "action-required"
    if (any(c["status"] != "completed" for c in checks)
            or any(c["status"] != "completed" for c in workflows)
            or any(s["state"] == "pending" for s in statuses)):
        return "pending"
    return "passed"


def required_checks(api, base_ref, checks, statuses, workflows=()):
    try:
        protection = api.request("/branches/" + quote(base_ref, safe="") + "/protection")
    except HTTPError:
        return {"state": "unknown"}
    required = protection.get("required_status_checks") or {}
    names = sorted({c["context"] for c in required.get("checks", [])} | set(required.get("contexts", [])))
    awaiting_approval = any(c.get("conclusion") == "action_required" or c.get("status") == "action_required" for c in workflows)
    results = []
    for name in names:
        matched = [c for c in checks if c["name"] == name]
        matched_statuses = [s for s in statuses if s["context"] == name]
        if matched or matched_statuses:
            state = classify(matched, matched_statuses)
        else:
            state = "action-required" if awaiting_approval else "missing"
        results.append({"name": name, "state": state})
    return {"state": "readable", "checks": results}


def review_threads(api, number):
    owner, name = api.repo.split("/")
    cursor = None
    total = unresolved = current = 0
    try:
        while True:
            data = api.graphql(THREADS_QUERY, {"owner": owner, "name": name, "number": number, "cursor": cursor})
            threads = data["repository"]["pullRequest"]["reviewThreads"]
            for node in threads["nodes"]:
                total += 1
                if not node["isResolved"]:
                    unresolved += 1
                    if not node["isOutdated"]:
                        current += 1
            if not threads["pageInfo"]["hasNextPage"]:
                return {"state": "known", "total": total, "unresolved": unresolved, "unresolved_current": current}
            cursor = threads["pageInfo"]["endCursor"]
    except (HTTPError, ValueError, KeyError):
        return {"state": "unknown"}


def build_report(api, number):
    pr = api.request(f"/pulls/{number}")
    head_sha = pr["head"]["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise ValueError("unexpected head SHA from GitHub")
    runs = [{"name": c["name"], "status": c["status"], "conclusion": c["conclusion"], "url": c["html_url"]} for c in check_runs(api, head_sha)]
    runs.sort(key=lambda c: (c["name"], c["url"]))
    statuses = [{"context": s["context"], "state": s["state"], "url": s.get("target_url")} for s in pages(api, f"/commits/{head_sha}/statuses")]
    statuses.sort(key=lambda s: (s["context"], s["url"] or ""))
    combined = api.request(f"/commits/{head_sha}/status")["state"]
    try:
        workflows = workflow_runs(api, head_sha)
        workflow_state = "known"
    except HTTPError as error:
        if error.code not in {403, 404}:
            raise
        workflows, workflow_state = [], "unknown"
    workflows.sort(key=lambda w: (w["name"], w["url"]))
    reviews = [{"user": r["user"]["login"], "state": r["state"], "commit_id": r.get("commit_id"), "submitted_at": r.get("submitted_at")} for r in pages(api, f"/pulls/{number}/reviews")]
    # GitHub returns reviews chronologically. A later opinion from the same
    # reviewer supersedes the prior one; comments do not revoke an opinion.
    latest_opinion = {}
    for review in reviews:
        if review["state"] in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            latest_opinion[review["user"].casefold()] = review
    reviews.sort(key=lambda r: (r["submitted_at"] or "", r["user"], r["state"], r["commit_id"] or ""))
    approvals = [r for r in latest_opinion.values() if r["state"] == "APPROVED"]
    return {
        "schema": SCHEMA,
        "repository": api.repo,
        "pull_request": number,
        "url": pr["html_url"],
        "state": pr["state"],
        "draft": pr["draft"],
        "base": {"ref": pr["base"]["ref"], "sha": pr["base"]["sha"]},
        "head": {"ref": pr["head"]["ref"], "sha": head_sha},
        "mergeable": pr["mergeable"],
        "mergeable_state": pr["mergeable_state"],
        "checks": {
            "state": classify(runs, statuses, workflows),
            "workflow_runs_state": workflow_state,
            "combined_status": combined,
            "check_runs": runs,
            "commit_statuses": statuses,
            "workflow_runs": workflows,
        },
        "required_checks": required_checks(api, pr["base"]["ref"], runs, statuses, workflows),
        "reviews": {
            "total": len(reviews),
            "approvals_on_head": sum(1 for r in approvals if r["commit_id"] == head_sha),
            "approvals_on_older_commits": sum(1 for r in approvals if r["commit_id"] != head_sha),
            "entries": reviews,
        },
        "review_threads": review_threads(api, number),
        "merge_authorization": "not-assessed",
        "note": "Completed checks are not merge approval; maintainers decide against the real gates.",
    }


def render_text(report):
    checks = report["checks"]
    required = report["required_checks"]
    reviews = report["reviews"]
    threads = report["review_threads"]
    lines = [
        f"PR #{report['pull_request']} {report['repository']} state={report['state']} draft={str(report['draft']).lower()}",
        f"base {report['base']['ref']}@{report['base']['sha']} head {report['head']['ref']}@{report['head']['sha']}",
        f"mergeable={json.dumps(report['mergeable'])} mergeable_state={report['mergeable_state']}",
        f"checks: {checks['state']} ({len(checks['check_runs'])} check runs, {len(checks['commit_statuses'])} commit statuses, {len(checks['workflow_runs'])} workflow runs, combined={checks['combined_status']})",
    ]
    for run in checks["check_runs"]:
        lines.append(f"  check {run['name']}: {run['status']}/{run['conclusion']}")
    for run in checks["workflow_runs"]:
        lines.append(f"  workflow {run['name']}: {run['status']}/{run['conclusion']} ({run['event']})")
    if checks["workflow_runs_state"] == "unknown":
        lines.append("workflow runs: unknown (Actions unreadable or disabled; check runs/statuses only)")
    if required["state"] == "unknown":
        lines.append("required checks: unknown (branch protection unreadable)")
    else:
        lines.append("required checks: " + (", ".join(f"{c['name']}={c['state']}" for c in required["checks"]) or "none configured"))
    lines.append(f"reviews: {reviews['total']} total, {reviews['approvals_on_head']} approvals on head, {reviews['approvals_on_older_commits']} on older commits")
    if threads["state"] == "unknown":
        lines.append("review threads: unknown")
    else:
        lines.append(f"review threads: {threads['unresolved']} unresolved ({threads['unresolved_current']} current) of {threads['total']}")
    lines.append("merge authorization: not assessed — " + report["note"])
    lines.append(report["url"])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--json", action="store_true", help="emit stable machine-readable JSON instead of text")
    args = parser.parse_args(argv)
    try:
        if args.pr < 1:
            raise ValueError("PR number must be positive")
        api = GitHub(args.repo, os.environ.get("GITHUB_TOKEN", ""))
        report = build_report(api, args.pr)
        print(json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False) if args.json else render_text(report))
    except HTTPError as error:
        print(f"GitHub API returned HTTP {error.code}; stopped. Review permissions/rate limits and retry.", file=sys.stderr)
        return 1
    except (ValueError, OSError, URLError) as error:
        print(f"PR status report stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
