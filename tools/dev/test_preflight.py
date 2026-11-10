"""Offline checks for the read-only dev preflight: fixtures, unknowns, redaction, no writes."""
import contextlib
import io
import json
import unittest
from unittest import mock

import preflight
from preflight import build_report, render_text

ALL_BINARIES = ("ctr", "runc", "ip", "nft", "cargo", "rustc", "psql")
CGROUP = "/sys/fs/cgroup"
MEMINFO = "MemTotal:       32768000 kB\nMemFree:        1000000 kB\n"
SELF_CGROUP = "0::/user.slice/user-1000.slice/session-3.scope\n"
MEMBER = CGROUP + "/user.slice/user-1000.slice/session-3.scope"


class FakeProbe:
    def __init__(self, files=None, kinds=None, binaries=ALL_BINARIES, affinity=8, euid=1000,
                 uname=("Linux", "6.0.0-test"), writable=False, affinity_error=False):
        self.files = dict(files or {})
        self.kinds = dict(kinds or {})
        self.binaries = set(binaries)
        self._affinity = affinity
        self._euid = euid
        self._uname = uname
        self._writable = writable
        self.affinity_error = affinity_error

    def which(self, name):
        return "/home/testuser/bin/" + name if name in self.binaries else None

    def read(self, path):
        try:
            value = self.files[str(path)]
        except KeyError:
            raise FileNotFoundError(str(path))
        if isinstance(value, OSError):
            raise value
        return value

    def kind(self, path):
        return self.kinds.get(str(path), "missing")

    def affinity(self):
        if self.affinity_error:
            raise OSError("restricted")
        return self._affinity

    def euid(self):
        return self._euid

    def uname(self):
        return self._uname

    def writable(self, path):
        return self._writable


def healthy(**overrides):
    ancestors = [CGROUP, CGROUP + "/user.slice", CGROUP + "/user.slice/user-1000.slice", MEMBER]
    files = {path + "/cgroup.controllers": "cpu memory pids io\n" for path in ancestors}
    files.update({path + "/cgroup.subtree_control": "\n" for path in ancestors[:-1]})
    files.update({"/proc/meminfo": MEMINFO, "/proc/self/cgroup": SELF_CGROUP})
    defaults = dict(
        files=files,
        kinds={"/run/containerd/containerd.sock": "socket"},
    )
    defaults.update(overrides)
    return FakeProbe(**defaults)


def call_main(probe, *extra):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(preflight, "Probe", return_value=probe):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = preflight.main(list(extra))
    return code, out.getvalue(), err.getvalue()


class PreflightTests(unittest.TestCase):
    def test_healthy_host(self):
        report = build_report(healthy())
        checks = report["checks"]
        for name in ALL_BINARIES:
            self.assertEqual(checks[f"command_{name}"]["state"], "ok")
        self.assertEqual(checks["containerd_socket"], {"state": "detected", "capability": "not-proven", "note": "socket exists; no connection attempted"})
        self.assertEqual(checks["cgroup_controllers"]["state"], "ok")
        self.assertEqual(checks["resources"]["state"], "ok")
        self.assertEqual(checks["resources"]["effective_cpu_millis"], 8000)
        self.assertEqual(checks["resources"]["effective_memory_bytes"], 32768000 * 1024)
        self.assertEqual(checks["resources"]["limit_scope"], "visible-hierarchy-upper-bound")
        self.assertEqual(report["acceptance"], "not-assessed")
        self.assertEqual(sum(report["summary"].values()), len(checks))

    def test_missing_commands_and_socket_are_unavailable_not_success(self):
        report = build_report(healthy(binaries=(), kinds={}))
        checks = report["checks"]
        for name in ALL_BINARIES:
            self.assertEqual(checks[f"command_{name}"]["state"], "unavailable")
        self.assertEqual(checks["containerd_socket"]["state"], "unavailable")
        self.assertNotIn("success", json.dumps(report))
        self.assertNotIn("passed", json.dumps(report))
        self.assertGreater(report["summary"]["unavailable"], 0)

    def test_restricted_cgroup_is_unknown(self):
        probe = healthy()
        probe.files[CGROUP + "/cgroup.controllers"] = PermissionError("restricted")
        self.assertEqual(build_report(probe)["checks"]["cgroup_controllers"]["state"], "unknown")

    def test_cgroup_v1_is_unavailable(self):
        probe = healthy()
        del probe.files[CGROUP + "/cgroup.controllers"]
        probe.files["/proc/self/cgroup"] = "11:cpu:/user.slice\n"
        checks = build_report(probe)["checks"]
        self.assertEqual(checks["cgroup_controllers"]["state"], "unavailable")
        self.assertEqual(checks["resources"]["state"], "unavailable")

    def test_ancestor_limits_cap_effective_resources(self):
        probe = healthy()
        probe.files[CGROUP + "/cgroup.subtree_control"] = "cpu memory\n"
        probe.files[CGROUP + "/user.slice/cpu.max"] = "200000 100000\n"
        probe.files[CGROUP + "/user.slice/memory.max"] = "17179869184\n"
        resources = build_report(probe)["checks"]["resources"]
        self.assertEqual(resources["state"], "ok")
        self.assertEqual(resources["effective_cpu_millis"], 2000)
        self.assertEqual(resources["effective_memory_bytes"], 17179869184)
        self.assertEqual(resources["affinity_cpu_millis"], 8000)

    def test_unreadable_ancestor_limit_makes_effective_unknown(self):
        probe = healthy()
        probe.files[CGROUP + "/cgroup.subtree_control"] = "cpu\n"
        probe.files[CGROUP + "/user.slice/cpu.max"] = PermissionError("restricted")
        resources = build_report(probe)["checks"]["resources"]
        self.assertEqual(resources["state"], "unknown")
        self.assertIsNone(resources["effective_cpu_millis"])
        self.assertIsNone(resources["effective_memory_bytes"])
        self.assertEqual(resources["memtotal_bytes"], 32768000 * 1024)

    def test_affinity_failure_is_unknown(self):
        resources = build_report(healthy(affinity_error=True))["checks"]["resources"]
        self.assertEqual(resources["state"], "unknown")
        self.assertIsNone(resources["affinity_cpu_millis"])

    def test_missing_visible_cgroup_directory_is_not_success(self):
        probe = healthy()
        del probe.files[MEMBER + "/cgroup.controllers"]
        resources = build_report(probe)["checks"]["resources"]
        self.assertEqual(resources["state"], "unknown")
        self.assertIsNone(resources["effective_cpu_millis"])
        self.assertIsNone(resources["effective_memory_bytes"])

    def test_missing_enabled_controller_limit_is_not_unlimited(self):
        for controller in ("cpu", "memory"):
            with self.subTest(controller=controller):
                probe = healthy()
                probe.files[CGROUP + "/cgroup.subtree_control"] = controller + "\n"
                resources = build_report(probe)["checks"]["resources"]
                self.assertEqual(resources["state"], "unknown")
                self.assertIsNone(resources["effective_cpu_millis"])
                self.assertIsNone(resources["effective_memory_bytes"])

    def test_unreadable_parent_controller_state_is_not_unlimited(self):
        probe = healthy()
        probe.files[CGROUP + "/cgroup.subtree_control"] = PermissionError("restricted")
        resources = build_report(probe)["checks"]["resources"]
        self.assertEqual(resources["state"], "unknown")
        self.assertIsNone(resources["effective_cpu_millis"])
        self.assertIsNone(resources["effective_memory_bytes"])

    def test_membership_change_during_scan_is_not_success(self):
        probe = healthy()
        original = probe.read
        reads = []

        def moving(path):
            if str(path) == "/proc/self/cgroup":
                reads.append(path)
                return SELF_CGROUP if len(reads) == 1 else "0::/another-cgroup\n"
            return original(path)

        with mock.patch.object(probe, "read", side_effect=moving):
            resources = build_report(probe)["checks"]["resources"]
        self.assertEqual(resources["state"], "unknown")
        self.assertIsNone(resources["effective_cpu_millis"])
        self.assertIsNone(resources["effective_memory_bytes"])
        self.assertEqual(len(reads), 2)

    def test_unavailable_cgroup_membership_never_claims_effective_totals(self):
        for value, expected in ((None, "unavailable"), (PermissionError("restricted"), "unknown"),
                                ("11:cpu:/user.slice\n", "unavailable"),
                                ("0::/../../etc\n", "unavailable")):
            with self.subTest(expected=expected, value=value):
                probe = healthy()
                if value is None:
                    del probe.files["/proc/self/cgroup"]
                else:
                    probe.files["/proc/self/cgroup"] = value
                resources = build_report(probe)["checks"]["resources"]
                self.assertEqual(resources["state"], expected)
                self.assertIsNone(resources["effective_cpu_millis"])
                self.assertIsNone(resources["effective_memory_bytes"])
                self.assertEqual(resources["affinity_cpu_millis"], 8000)
                self.assertEqual(resources["memtotal_bytes"], 32768000 * 1024)

    def test_missing_affinity_does_not_claim_effective_memory(self):
        resources = build_report(healthy(affinity_error=True))["checks"]["resources"]
        self.assertEqual(resources["state"], "unknown")
        self.assertIsNone(resources["effective_cpu_millis"])
        self.assertIsNone(resources["effective_memory_bytes"])

    def test_cgroup_path_escape_is_unavailable(self):
        probe = healthy()
        probe.files["/proc/self/cgroup"] = "0::/../../etc\n"
        self.assertEqual(build_report(probe)["checks"]["resources"]["state"], "unavailable")

    def test_socket_kinds(self):
        for kind, expected in (("socket", "detected"), ("missing", "unavailable"), ("other", "unavailable"), ("unknown", "unknown")):
            with self.subTest(kind=kind):
                probe = healthy(kinds={"/run/containerd/containerd.sock": kind})
                self.assertEqual(build_report(probe)["checks"]["containerd_socket"]["state"], expected)

    def test_container_markers(self):
        self.assertEqual(build_report(healthy(kinds={"/run/containerd/containerd.sock": "socket", "/.dockerenv": "other"}))["checks"]["container_environment"]["state"], "detected")
        self.assertEqual(build_report(healthy(kinds={"/run/containerd/containerd.sock": "socket", "/.dockerenv": "unknown"}))["checks"]["container_environment"]["state"], "unknown")
        self.assertEqual(build_report(healthy())["checks"]["container_environment"]["state"], "ok")

    def test_non_root_and_non_linux(self):
        checks = build_report(healthy(euid=1000))["checks"]
        self.assertEqual(checks["host_administration"]["state"], "unavailable")
        self.assertEqual(build_report(healthy(euid=0))["checks"]["host_administration"]["state"], "ok")
        self.assertEqual(build_report(healthy(uname=("Darwin", "24.0")))["checks"]["kernel"]["state"], "unavailable")

    def test_output_redacts_personal_paths_and_cgroup_names(self):
        probe = healthy()
        for extra in ([], ["--json"]):
            code, out, err = call_main(probe, *extra)
            self.assertEqual(code, 0)
            for forbidden in ("testuser", "/home/", "session-3", "user-1000"):
                self.assertNotIn(forbidden, out + err)

    def test_source_has_no_environment_network_or_write_surface(self):
        source = (preflight.Path(preflight.__file__)).read_text(encoding="utf-8")
        for forbidden in ("os.environ", "getenv", "subprocess", "urllib", "write_text", "mkdir", "rmdir", "unlink", "connect("):
            self.assertNotIn(forbidden, source)

    def test_json_is_stable_and_summary_consistent(self):
        probe = healthy()
        code1, out1, _ = call_main(probe, "--json")
        code2, out2, _ = call_main(probe, "--json")
        self.assertEqual((code1, code2, out1), (0, 0, out2))
        report = json.loads(out1)
        counts = {"ok": 0, "detected": 0, "unavailable": 0, "unknown": 0}
        for check in report["checks"].values():
            counts[check["state"]] += 1
        self.assertEqual(report["summary"], counts)

    def test_text_output_marks_acceptance_not_assessed(self):
        code, out, _ = call_main(healthy())
        self.assertEqual(code, 0)
        self.assertIn("acceptance: not-assessed", out)
        self.assertIn("capability not proven", out)
        self.assertIn("RUN-03", out)

    def test_custom_socket_path_not_echoed(self):
        code, out, _ = call_main(healthy(kinds={"/home/testuser/private.sock": "socket"}), "--containerd-socket", "/home/testuser/private.sock")
        self.assertEqual(code, 0)
        self.assertNotIn("testuser", out)
        self.assertIn("containerd_socket: detected", out)


if __name__ == "__main__":
    unittest.main()
