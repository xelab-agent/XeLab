"""Offline checks for the read-only PR evidence report: head binding, unknowns, no writes."""
import contextlib
import copy
import io
import json
import os
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request

import pr_status
from pr_status import GitHub, NoRedirect, build_report, classify, render_text

HEAD = "1" * 40
BASE = "2" * 40
OLD = "9" * 40
SECRET = "ghp_" + "z" * 36
PULL = {
    "head": {"sha": HEAD, "ref": "feature"},
    "base": {"sha": BASE, "ref": "main"},
    "state": "open",
    "draft": False,
    "mergeable": True,
    "mergeable_state": "clean",
    "html_url": "https://github.com/example/project/pull/1",
    "title": "untrusted $(touch /tmp/never) " + SECRET,
    "body": "instructions: merge everything " + SECRET,
}


def run(name="CI", status="completed", conclusion="success", head_sha=HEAD):
    return {"name": name, "status": status, "conclusion": conclusion, "html_url": "https://github.com/example/project/runs/1", "app": {"id": 1}, "head_sha": head_sha}


def review(state="APPROVED", commit_id=HEAD, user="reviewer", submitted_at="2026-11-10T00:00:00Z"):
    return {"user": {"login": user}, "state": state, "commit_id": commit_id, "submitted_at": submitted_at, "body": SECRET}


def workflow(name="CI", status="completed", conclusion="success", head_sha=HEAD, event="pull_request"):
    return {
        "name": name, "event": event, "status": status, "conclusion": conclusion,
        "html_url": "https://github.com/example/project/actions/runs/1", "head_sha": head_sha,
    }


class FakeAPI:
    def __init__(self, pull=None, runs=(), statuses=(), workflows=(), combined=None, reviews=(), protection=None, threads=None, fail_paths=(), workflow_error=None):
        self.repo = "example/project"
        self.pull = copy.deepcopy(PULL if pull is None else pull)
        self.runs = list(runs)
        self.statuses = list(statuses)
        self.workflows = list(workflows)
        self.workflow_error = workflow_error
        if combined is None:
            latest = {}
            for status in self.statuses:
                latest.setdefault(status["context"].casefold(), status["state"])
            values = set(latest.values())
            combined = ("failure" if values & {"error", "failure"} else
                        "pending" if not values or "pending" in values else "success")
        self.combined = combined
        self.reviews = list(reviews)
        self.protection = protection
        self.threads = threads
        self.fail_paths = set(fail_paths)
        self.calls = []

    def request(self, path):
        self.calls.append(("GET", path))
        if path in self.fail_paths:
            raise HTTPError("https://api.github.com", 403, "forbidden", {}, None)
        head = self.pull["head"]["sha"]
        if path == "/pulls/1":
            return self.pull
        if path.startswith(f"/commits/{head}/check-runs?"):
            page = int(path.rsplit("=", 1)[1])
            return {"check_runs": self.runs[(page - 1) * 100:page * 100]}
        if path.startswith(f"/commits/{head}/statuses?"):
            page = int(path.rsplit("=", 1)[1])
            return self.statuses[(page - 1) * 100:page * 100]
        if path.startswith(f"/actions/runs?head_sha={head}&"):
            if self.workflow_error is not None:
                raise HTTPError("https://api.github.com", self.workflow_error, "denied", {}, None)
            page = int(path.rsplit("=", 1)[1])
            return {"total_count": len(self.workflows), "workflow_runs": self.workflows[(page - 1) * 100:page * 100]}
        if path == f"/commits/{head}/status":
            return {"state": self.combined}
        if path.startswith("/pulls/1/reviews?"):
            page = int(path.rsplit("=", 1)[1])
            return self.reviews[(page - 1) * 100:page * 100]
        if path == "/branches/main/protection":
            if self.protection is None:
                raise HTTPError("https://api.github.com", 403, "forbidden", {}, None)
            return self.protection
        raise AssertionError("unexpected path " + path)

    def graphql(self, query, variables):
        self.calls.append(("GRAPHQL", query, variables))
        if not query.lstrip().startswith("query"):
            raise ValueError("only read-only GraphQL queries are allowed")
        if self.threads is None:
            raise ValueError("GraphQL query returned errors")
        cursor = variables["cursor"]
        nodes, next_cursor = self.threads(cursor)
        return {"repository": {"pullRequest": {"reviewThreads": {
            "nodes": nodes, "pageInfo": {"hasNextPage": next_cursor is not None, "endCursor": next_cursor}}}}}


def call_main(api, *extra):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, {"GITHUB_TOKEN": SECRET}), mock.patch.object(pr_status, "GitHub", return_value=api):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pr_status.main(["--repo", "example/project", "--pr", "1", *extra])
    return code, out.getvalue(), err.getvalue()


class ReportTests(unittest.TestCase):
    def test_evidence_binds_to_exact_head(self):
        api = FakeAPI(runs=[run()], reviews=[review()])
        report = build_report(api, 1)
        self.assertEqual(report["head"]["sha"], HEAD)
        for kind in ("check-runs?", "statuses?", "status"):
            self.assertIn(("GET", f"/commits/{HEAD}/{kind}" if kind == "status" else f"/commits/{HEAD}/{kind}per_page=100&page=1"), api.calls)
        self.assertIn(("GET", f"/actions/runs?head_sha={HEAD}&per_page=100&page=1"), api.calls)
        self.assertEqual(report["reviews"]["approvals_on_head"], 1)

    def test_head_change_rebinds_checks_and_stales_approval(self):
        api = FakeAPI(runs=[run()], reviews=[review()])
        first = build_report(api, 1)
        api.pull["head"]["sha"] = OLD
        api.runs = [run(name="CI-on-new-head", head_sha=OLD)]
        second = build_report(api, 1)
        self.assertEqual(first["reviews"]["approvals_on_head"], 1)
        self.assertEqual(second["reviews"]["approvals_on_head"], 0)
        self.assertEqual(second["reviews"]["approvals_on_older_commits"], 1)
        self.assertIn(("GET", f"/commits/{OLD}/check-runs?per_page=100&page=1"), api.calls)
        self.assertEqual(second["checks"]["check_runs"][0]["name"], "CI-on-new-head")

    def test_mismatched_or_missing_check_head_fails_closed(self):
        missing = run()
        missing.pop("head_sha")
        for item in (run(head_sha=OLD), missing):
            with self.subTest(item=item), self.assertRaisesRegex(ValueError, "check run head SHA"):
                build_report(FakeAPI(runs=[item]), 1)

    def test_only_read_calls(self):
        api = FakeAPI(runs=[run()], reviews=[review()], threads=lambda cursor: ([], None))
        build_report(api, 1)
        for call in api.calls:
            if call[0] == "GRAPHQL":
                self.assertTrue(call[1].lstrip().startswith("query"))
            else:
                self.assertEqual(call, ("GET", call[1]))

    def test_ci_pending_failed_action_required_and_missing(self):
        cases = [
            ([run(status="in_progress", conclusion=None)], [], "pending"),
            ([run(status="queued", conclusion=None)], [], "pending"),
            ([run(conclusion="failure")], [], "failed"),
            ([run(conclusion="timed_out")], [], "failed"),
            ([run(conclusion="action_required")], [], "action-required"),
            ([run()], [{"context": "legacy", "state": "failure", "target_url": None}], "failed"),
            ([], [{"context": "legacy", "state": "pending", "target_url": None}], "pending"),
            ([], [], "none-reported"),
            ([run()], [], "passed"),
            ([run(conclusion="skipped")], [], "passed"),
        ]
        for runs, statuses, expected in cases:
            with self.subTest(expected=expected, runs=runs):
                self.assertEqual(build_report(FakeAPI(runs=runs, statuses=statuses), 1)["checks"]["state"], expected)

    def test_fork_pr_workflow_approval_is_action_required_not_none_reported(self):
        protection = {"required_status_checks": {"contexts": ["CI"], "checks": [{"context": "CI", "app_id": 1}]}}
        api = FakeAPI(workflows=[workflow(conclusion="action_required")], protection=protection)
        report = build_report(api, 1)
        self.assertEqual(report["checks"]["state"], "action-required")
        self.assertEqual(report["checks"]["workflow_runs_state"], "known")
        self.assertEqual(report["checks"]["workflow_runs"], [{
            "name": "CI", "event": "pull_request", "status": "completed",
            "conclusion": "action_required", "url": "https://github.com/example/project/actions/runs/1"}])
        self.assertEqual(report["required_checks"]["checks"], [{"name": "CI", "app_id": 1, "state": "action-required"}])
        text = render_text(report)
        self.assertIn("workflow CI: completed/action_required", text)

    def test_workflow_states_classified(self):
        cases = [
            ([workflow(status="queued", conclusion=None)], "pending"),
            ([workflow(status="in_progress", conclusion=None)], "pending"),
            ([workflow(conclusion="failure")], "failed"),
            ([workflow(conclusion="timed_out")], "failed"),
            ([workflow(conclusion="action_required")], "action-required"),
            ([workflow(conclusion="skipped")], "passed"),
            ([workflow()], "passed"),
        ]
        for workflows, expected in cases:
            with self.subTest(expected=expected, workflows=workflows):
                self.assertEqual(build_report(FakeAPI(workflows=workflows), 1)["checks"]["state"], expected)

    def test_workflow_failure_outranks_approval_and_pending(self):
        report = build_report(FakeAPI(workflows=[workflow(conclusion="failure"), workflow(name="Other", conclusion="action_required")]), 1)
        self.assertEqual(report["checks"]["state"], "failed")

    def test_workflow_head_mismatch_fails_closed(self):
        with self.assertRaises(ValueError):
            build_report(FakeAPI(workflows=[workflow(head_sha=OLD)]), 1)

    def test_workflow_pagination(self):
        api = FakeAPI(workflows=[workflow(name=f"wf-{n}") for n in range(101)])
        report = build_report(api, 1)
        self.assertEqual(len(report["checks"]["workflow_runs"]), 101)
        self.assertIn(("GET", f"/actions/runs?head_sha={HEAD}&per_page=100&page=2"), api.calls)

    def test_workflow_query_403_or_404_degrades_to_unknown(self):
        for code in (403, 404):
            with self.subTest(code=code):
                report = build_report(FakeAPI(runs=[run()], workflow_error=code), 1)
                self.assertEqual(report["checks"]["workflow_runs_state"], "unknown")
                self.assertEqual(report["checks"]["workflow_runs"], [])
                self.assertEqual(report["checks"]["state"], "passed")
                self.assertIn("workflow runs: unknown", render_text(report))

    def test_workflow_query_other_errors_abort(self):
        with self.assertRaises(HTTPError):
            build_report(FakeAPI(workflow_error=500), 1)

    def test_protection_unreadable_is_unknown_not_guess(self):
        report = build_report(FakeAPI(runs=[run()]), 1)
        self.assertEqual(report["required_checks"], {"state": "unknown"})
        self.assertIn("required checks: unknown", render_text(report))

    def test_required_checks_missing_and_passed(self):
        protection = {"required_status_checks": {"contexts": ["CI"], "checks": [{"context": "CI", "app_id": 1}, {"context": "Lint", "app_id": 1}]}}
        report = build_report(FakeAPI(runs=[run()], protection=protection), 1)
        self.assertEqual(report["required_checks"], {"state": "readable", "checks": [{"name": "CI", "app_id": 1, "state": "passed"}, {"name": "Lint", "app_id": 1, "state": "missing"}]})

    def test_app_bound_rules_reject_wrong_or_unverifiable_sources(self):
        protection = {"required_status_checks": {"contexts": ["build"], "checks": [{"context": "build", "app_id": 77}]}}
        wrong, right, absent = run("build"), run("build"), run("build")
        wrong["app"] = {"id": 88}
        right["app"] = {"id": 77}
        absent.pop("app")
        status = {"context": "build", "state": "success", "target_url": None, "creator": {"id": 77}}
        for runs, statuses, expected in [
            ([wrong], [], "missing"), ([right], [], "passed"),
            ([absent], [], "unknown"), ([right, absent], [], "unknown"),
            ([], [status], "unknown"), ([right], [status], "unknown"),
        ]:
            with self.subTest(expected=expected, runs=runs, statuses=statuses):
                report = build_report(FakeAPI(runs=runs, statuses=statuses, protection=protection), 1)
                self.assertEqual(report["required_checks"]["checks"], [{"name": "build", "app_id": 77, "state": expected}])
        self.assertEqual(build_report(FakeAPI(runs=[right], protection=protection), 1)["checks"]["check_runs"][0]["app_id"], 77)

    def test_unbound_or_unclear_rule_sources_do_not_guess(self):
        good = run("build")
        for rule in ({"context": "build"}, {"context": "build", "app_id": None}, {"context": "build", "app_id": 0}):
            with self.subTest(rule=rule):
                protection = {"required_status_checks": {"checks": [rule], "contexts": ["build"]}}
                self.assertEqual(build_report(FakeAPI(runs=[good], protection=protection), 1)["required_checks"]["checks"][0]["state"], "unknown")
        self.assertEqual(build_report(FakeAPI(runs=[good], protection={"required_status_checks": {"contexts": ["build"]}}), 1)["required_checks"]["checks"][0]["state"], "unknown")
        self.assertEqual(build_report(FakeAPI(protection={}), 1)["required_checks"], {"state": "unknown"})
        self.assertEqual(build_report(FakeAPI(protection={"required_status_checks": None}), 1)["required_checks"], {"state": "readable", "checks": []})
        self.assertEqual(build_report(FakeAPI(runs=[good], protection={"required_status_checks": {"checks": [{"context": "build", "app_id": True}]}}), 1)["required_checks"], {"state": "unknown"})
        self.assertEqual(build_report(FakeAPI(protection={"required_status_checks": {"contexts": "build"}}), 1)["required_checks"], {"state": "unknown"})
        self.assertEqual(build_report(FakeAPI(protection={"required_status_checks": {"contexts": None, "checks": None}}), 1)["required_checks"], {"state": "unknown"})

    def test_any_app_requires_both_same_named_check_and_status(self):
        protection = {"required_status_checks": {"checks": [{"context": "build", "app_id": -1}]}}
        success = run("build")
        failure = run("build", conclusion="failure")
        status_ok = {"context": "BUILD", "state": "success", "target_url": None}
        status_bad = {"context": "BUILD", "state": "failure", "target_url": None}
        for checks, statuses, expected in [([success], [status_bad], "failed"), ([failure], [status_ok], "failed"), ([success], [status_ok], "passed")]:
            with self.subTest(expected=expected, checks=checks, statuses=statuses):
                self.assertEqual(build_report(FakeAPI(runs=checks, statuses=statuses, protection=protection), 1)["required_checks"]["checks"][0]["state"], expected)

    def test_latest_status_is_case_insensitive_across_pages(self):
        newest = {"context": "CI", "state": "success", "target_url": None}
        old = {"context": "ci", "state": "failure", "target_url": None}
        filler = [{"context": f"other-{i}", "state": "success", "target_url": None} for i in range(99)]
        protection = {"required_status_checks": {"checks": [{"context": "ci", "app_id": -1}]}}
        api = FakeAPI(statuses=[newest, *filler, old], protection=protection)
        report = build_report(api, 1)
        self.assertEqual(report["checks"]["state"], "passed")
        self.assertEqual(report["checks"]["commit_status_history_count"], 101)
        self.assertEqual(len(report["checks"]["commit_statuses"]), 100)
        self.assertEqual(report["required_checks"]["checks"][0]["state"], "passed")
        self.assertIn(("GET", f"/commits/{HEAD}/statuses?per_page=100&page=2"), api.calls)
        report = build_report(FakeAPI(statuses=[old, newest], protection=protection), 1)
        self.assertEqual(report["checks"]["state"], "failed")
        self.assertEqual(report["required_checks"]["checks"][0]["state"], "failed")

    def test_mismatched_or_unknown_status_evidence_never_reports_passed(self):
        good = run("build")
        protection = {"required_status_checks": {"checks": [{"context": "build", "app_id": -1}]}}
        status = {"context": "build", "state": "success", "target_url": None}
        for statuses, combined in [([status], "failure"), ([status], "pending"), ([], "success")]:
            with self.subTest(statuses=statuses, combined=combined):
                r = build_report(FakeAPI(runs=[good], statuses=statuses, combined=combined, protection=protection), 1)
                self.assertEqual(r["checks"]["state"], "unknown")
                self.assertEqual(r["checks"]["commit_status_snapshot"], "unknown")
                self.assertEqual(r["required_checks"]["checks"][0]["state"], "unknown")
        r = build_report(FakeAPI(runs=[good], combined="pending", protection=protection), 1)
        self.assertEqual(r["checks"]["state"], "passed")
        self.assertEqual(r["required_checks"]["checks"][0]["state"], "passed")
        r = build_report(FakeAPI(combined="failure", protection=protection), 1)
        self.assertEqual(r["checks"]["state"], "unknown")
        self.assertEqual(r["required_checks"]["checks"][0]["state"], "unknown")

    def test_undocumented_or_ambiguous_results_are_unknown(self):
        for bad in [run(status="completed", conclusion=None), run(status="completed", conclusion="future"), run(status="future", conclusion=None)]:
            self.assertEqual(build_report(FakeAPI(runs=[bad]), 1)["checks"]["state"], "unknown")
        status = {"context": "ci", "state": "future", "target_url": None}
        self.assertEqual(build_report(FakeAPI(statuses=[status]), 1)["checks"]["state"], "unknown")
        non_ascii = {"context": "ß", "state": "success", "target_url": None}
        r = build_report(FakeAPI(statuses=[non_ascii]), 1)
        self.assertEqual(r["checks"]["state"], "unknown")
        protection = {"required_status_checks": {"checks": [{"context": "build", "app_id": -1}]}}
        self.assertEqual(build_report(FakeAPI(runs=[run("BUILD")], protection=protection), 1)["required_checks"]["checks"][0]["state"], "unknown")
        conflicting = {"required_status_checks": {"checks": [{"context": "build", "app_id": -1}], "contexts": ["BUILD"]}}
        self.assertTrue(all(x["state"] == "unknown" for x in build_report(FakeAPI(runs=[run("build")], protection=conflicting), 1)["required_checks"]["checks"]))

    def test_stale_approval_counted_separately(self):
        api = FakeAPI(reviews=[review(commit_id=OLD, user="old-reviewer"), review(user="current-reviewer"), review(state="COMMENTED", user="commenter")])
        reviews = build_report(api, 1)["reviews"]
        self.assertEqual((reviews["total"], reviews["approvals_on_head"], reviews["approvals_on_older_commits"]), (3, 1, 1))

    def test_pagination_of_reviews_and_check_runs(self):
        api = FakeAPI(runs=[run(name=f"check-{n}") for n in range(101)], reviews=[review(user=f"r{n}", submitted_at=f"2026-11-10T00:00:{n % 60:02d}Z") for n in range(101)])
        report = build_report(api, 1)
        self.assertEqual(len(report["checks"]["check_runs"]), 101)
        self.assertEqual(report["reviews"]["total"], 101)
        self.assertIn(("GET", f"/commits/{HEAD}/check-runs?per_page=100&page=2"), api.calls)
        self.assertIn(("GET", "/pulls/1/reviews?per_page=100&page=2"), api.calls)

    def test_threads_counts_and_pagination(self):
        pages = {
            None: ([{"isResolved": False, "isOutdated": False}, {"isResolved": True, "isOutdated": False}], "c1"),
            "c1": ([{"isResolved": False, "isOutdated": True}], None),
        }
        report = build_report(FakeAPI(threads=lambda cursor: pages[cursor]), 1)
        self.assertEqual(report["review_threads"], {"state": "known", "total": 3, "unresolved": 2, "unresolved_current": 1})

    def test_threads_unknown_on_graphql_error(self):
        report = build_report(FakeAPI(), 1)
        self.assertEqual(report["review_threads"], {"state": "unknown"})
        self.assertEqual(report["checks"]["state"], "none-reported")

    def test_api_error_stops_safely(self):
        code, out, err = call_main(FakeAPI(fail_paths={"/pulls/1"}))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("HTTP 403", err)
        self.assertNotIn(SECRET, err)

    def test_token_and_untrusted_text_never_emitted(self):
        api = FakeAPI(runs=[run()], reviews=[review()])
        for extra in ([], ["--json"]):
            code, out, err = call_main(api, *extra)
            self.assertEqual(code, 0)
            self.assertNotIn(SECRET, out + err)
            self.assertNotIn("touch /tmp/never", out)
            self.assertNotIn("merge everything", out)
        report = build_report(api, 1)
        self.assertNotIn("title", report)
        self.assertNotIn("body", json.dumps(report))

    def test_json_output_is_stable_and_complete(self):
        api = FakeAPI(runs=[run()], reviews=[review()])
        code1, out1, _ = call_main(api, "--json")
        code2, out2, _ = call_main(api, "--json")
        self.assertEqual((code1, code2), (0, 0))
        self.assertEqual(out1, out2)
        report = json.loads(out1)
        self.assertEqual(report["merge_authorization"], "not-assessed")
        self.assertEqual(report["head"]["sha"], HEAD)
        self.assertEqual(report["base"]["sha"], BASE)

    def test_example_report_tracks_current_schema(self):
        with open(os.path.join(os.path.dirname(__file__), "example-report.json"), encoding="utf-8") as handle:
            example = json.load(handle)
        protection = {"required_status_checks": {"contexts": ["CI"]}}
        report = build_report(FakeAPI(
            workflows=[workflow()], reviews=[review()], protection=protection,
            threads=lambda cursor: ([], None)), 1)
        self.assertEqual(example["schema"], pr_status.SCHEMA)
        self.assertEqual(set(example), set(report))
        for section in ("base", "head", "checks", "required_checks", "reviews", "review_threads"):
            self.assertEqual(set(example[section]), set(report[section]), section)
        self.assertEqual(set(example["reviews"]["entries"][0]), set(report["reviews"]["entries"][0]))
        self.assertEqual(set(example["checks"]["workflow_runs"][0]), set(report["checks"]["workflow_runs"][0]))

    def test_text_distinguishes_checks_from_merge_approval(self):
        text = render_text(build_report(FakeAPI(runs=[run()]), 1))
        self.assertIn("checks: passed", text)
        self.assertIn("merge authorization: not assessed", text)

    def test_repository_cannot_supply_host_query_or_traversal(self):
        for repo in ["https://evil.invalid/repo", "a/b?token=1", "a/b/../c", "a/..", "../b"]:
            with self.subTest(repo=repo), self.assertRaises(ValueError):
                GitHub(repo, "not-a-real-token")

    def test_redirect_cannot_forward_authorization(self):
        with self.assertRaises(ValueError):
            NoRedirect().redirect_request(Request("https://api.github.com"), None, 302, "", {}, "https://evil.invalid")

    def test_graphql_rejects_mutations(self):
        with self.assertRaises(ValueError):
            GitHub("example/project", "not-a-real-token").graphql("mutation { addComment(input: {}) { } }", {})

    def test_invalid_pr_number_stops(self):
        code, out, err = call_main(FakeAPI(), "--pr", "0")
        self.assertEqual(code, 1)
        self.assertIn("positive", err)


if __name__ == "__main__":
    unittest.main()
