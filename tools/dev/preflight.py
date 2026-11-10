#!/usr/bin/env python3
"""Read-only host/dependency preflight for local development. Never modifies the host."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys

SCHEMA = 1
CGROUP_ROOT = Path("/sys/fs/cgroup")
REQUIRED_BINARIES = ("ctr", "runc", "ip", "nft")
BUILD_BINARIES = ("cargo", "rustc")
OPTIONAL_BINARIES = ("psql",)
DEFAULT_SOCKET = "/run/containerd/containerd.sock"
NOTE = ("Read-only diagnostic for local development. Socket detection is not execution capability; "
        "resource limits are visible-hierarchy upper bounds, and hidden ancestors may be tighter. "
        "RUN-03, AWS and production acceptance are tracked separately and never claimed here.")


class Probe:
    """All host interaction is read-only: PATH lookup, file reads, stat and access checks."""

    def which(self, name):
        return shutil.which(name)

    def read(self, path):
        return Path(path).read_text(encoding="utf-8")

    def kind(self, path):
        try:
            mode = Path(path).stat().st_mode
        except FileNotFoundError:
            return "missing"
        except OSError:
            return "unknown"
        return "socket" if stat.S_ISSOCK(mode) else "other"

    def affinity(self):
        return len(os.sched_getaffinity(0))

    def euid(self):
        return os.geteuid()

    def uname(self):
        info = os.uname()
        return info.sysname, info.release

    def writable(self, path):
        return os.access(path, os.W_OK)


def read_optional(probe, path):
    try:
        return "ok", probe.read(path)
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "unknown", None


def binary_checks(probe):
    checks = {}
    for group, names in (("required", REQUIRED_BINARIES), ("build", BUILD_BINARIES), ("optional", OPTIONAL_BINARIES)):
        for name in names:
            checks[f"command_{name}"] = {
                "group": group,
                "state": "ok" if probe.which(name) else "unavailable",
            }
    return checks


def cgroup_controllers(probe):
    state, content = read_optional(probe, CGROUP_ROOT / "cgroup.controllers")
    if state != "ok":
        return {"state": "unavailable" if state == "missing" else "unknown"}
    controllers = sorted(content.split())
    missing = sorted({"cpu", "memory", "pids"} - set(controllers))
    return {"state": "ok" if not missing else "unavailable", "controllers": controllers, "missing_required": missing}


def effective_resources(probe):
    state, meminfo = read_optional(probe, "/proc/meminfo")
    memory = None
    if state == "ok":
        match = re.search(r"^MemTotal:\s+(\d+) kB$", meminfo, re.MULTILINE)
        memory = int(match.group(1)) * 1024 if match else None
    try:
        cpu = probe.affinity() * 1000
    except (OSError, AttributeError, NotImplementedError):
        cpu = None
    # Host totals are observations, not effective cgroup limits. Even after
    # visible checks, cgroup namespaces can hide more restrictive ancestors.
    result = {"memtotal_bytes": memory, "affinity_cpu_millis": cpu,
              "effective_cpu_millis": None, "effective_memory_bytes": None,
              "limit_scope": "visible-hierarchy-upper-bound"}
    if memory is None or cpu is None:
        return dict(result, state="unknown" if state != "missing" else "unavailable")
    state, content = read_optional(probe, "/proc/self/cgroup")
    if state != "ok":
        return dict(result, state="unavailable" if state == "missing" else "unknown")
    relative = next((line[3:] for line in content.splitlines() if line.startswith("0::")), None)
    if relative is None:
        return dict(result, state="unavailable")
    current = (CGROUP_ROOT / relative.lstrip("/")).resolve()
    if not current.is_relative_to(CGROUP_ROOT):
        return dict(result, state="unavailable")
    unknown = False
    for ancestor in [current, *current.parents]:
        if not ancestor.is_relative_to(CGROUP_ROOT):
            break
        marker, _ = read_optional(probe, ancestor / "cgroup.controllers")
        if marker != "ok":
            unknown = True

        def missing_limit_is_unknown(controller):
            # The hierarchy root normally has no resource-control files. A
            # child can also lack one when its parent disabled that controller.
            if ancestor == CGROUP_ROOT:
                return False
            parent_state, enabled = read_optional(probe, ancestor.parent / "cgroup.subtree_control")
            return parent_state != "ok" or controller in enabled.split()

        state, content = read_optional(probe, ancestor / "cpu.max")
        if state == "unknown" or (state == "missing" and missing_limit_is_unknown("cpu")):
            unknown = True
        elif state == "ok":
            quota, period = content.split()
            if quota != "max":
                cpu = min(cpu, int(quota) * 1000 // int(period))
        state, content = read_optional(probe, ancestor / "memory.max")
        if state == "unknown" or (state == "missing" and missing_limit_is_unknown("memory")):
            unknown = True
        elif state == "ok" and content.strip() != "max":
            memory = min(memory, int(content))
    final_state, final_content = read_optional(probe, "/proc/self/cgroup")
    final_relative = (next((line[3:] for line in final_content.splitlines() if line.startswith("0::")), None)
                      if final_state == "ok" else None)
    if final_relative != relative:
        unknown = True
    return dict(result, effective_cpu_millis=None if unknown else cpu, effective_memory_bytes=None if unknown else memory,
                state="unknown" if unknown else "ok")


def socket_check(probe, socket_path):
    kind = probe.kind(socket_path)
    if kind == "socket":
        return {"state": "detected", "capability": "not-proven", "note": "socket exists; no connection attempted"}
    return {"state": "unavailable" if kind in {"missing", "other"} else "unknown"}


def container_environment(probe):
    kinds = {probe.kind("/.dockerenv"), probe.kind("/run/.containerenv")}
    if kinds & {"other", "socket"}:
        return {"state": "detected"}
    return {"state": "unknown"} if "unknown" in kinds else {"state": "ok"}


def build_report(probe, socket_path=DEFAULT_SOCKET):
    sysname, release = probe.uname()
    checks = {
        "python": {"state": "ok" if sys.version_info >= (3, 11) else "unavailable", "version": platform_version()},
        "kernel": {"state": "ok" if sysname == "Linux" else "unavailable", "release": release},
        "host_administration": {"state": "ok" if probe.euid() == 0 else "unavailable",
                                "note": "non-root; diagnostic still runs" if probe.euid() != 0 else "root"},
        "cgroup_controllers": cgroup_controllers(probe),
        "cgroup_writable": {"state": "ok" if probe.writable(CGROUP_ROOT) else "unavailable"},
        "container_environment": container_environment(probe),
        "containerd_socket": socket_check(probe, socket_path),
        "resources": effective_resources(probe),
    }
    checks.update(binary_checks(probe))
    summary = {"ok": 0, "detected": 0, "unavailable": 0, "unknown": 0}
    for check in checks.values():
        summary[check["state"]] += 1
    return {"schema": SCHEMA, "tool": "dev-preflight", "acceptance": "not-assessed", "note": NOTE,
            "checks": checks, "summary": summary}


def platform_version():
    return ".".join(str(part) for part in sys.version_info[:3])


def render_text(report):
    lines = [f"dev-preflight (read-only): {report['summary']['ok']} ok, {report['summary']['detected']} detected, "
             f"{report['summary']['unavailable']} unavailable, {report['summary']['unknown']} unknown"]
    for name, check in sorted(report["checks"].items()):
        extra = ""
        if name == "resources":
            extra = f" (visible cpu upper bound={check['effective_cpu_millis']} memory upper bound={check['effective_memory_bytes']})"
        elif name == "kernel":
            extra = f" ({check['release']})"
        elif name == "containerd_socket" and check["state"] == "detected":
            extra = " (capability not proven)"
        lines.append(f"  {name}: {check['state']}{extra}")
    lines.append("acceptance: not-assessed — " + report["note"])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit stable machine-readable JSON instead of text")
    parser.add_argument("--containerd-socket", default=DEFAULT_SOCKET, help="containerd socket path to inspect (stat only)")
    args = parser.parse_args(argv)
    try:
        report = build_report(Probe(), args.containerd_socket)
        print(json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False) if args.json else render_text(report))
    except (ValueError, OSError) as error:
        print(f"preflight stopped: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
