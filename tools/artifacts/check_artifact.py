#!/usr/bin/env python3
"""Read-only offline consistency preflight for pre-built XeLab plugin artifacts.

Mirrors the member/digest rules of crates/kernel/src/artifact.rs without
executing anything, downloading images or verifying signatures. Ed25519
verification stays in the kernel install path; this tool never claims a
signature is valid. Exit codes: 0 consistent, 1 inconsistent/unsupported,
2 not checked (input path absent).
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tarfile
import unicodedata
import uuid

SCHEMA = 1
MEMBER_LIMIT = 1048576
HTTP_CONTRACT_LIMIT = 4 * 1024 * 1024
MAX_TAR_MEMBERS = 4096
CONTRACT_FILES = ("input.schema.json", "output.schema.json", "stream.schema.json", "semantics.json")
NOTE = ("Read-only consistency preflight. No image was downloaded or executed and no file was "
        "modified. Ed25519 signature verification is NOT performed here; it belongs to the "
        "kernel install path with a trusted publisher key. This report does not replace "
        "kernel admission or host acceptance.")


class CheckError(Exception):
    pass


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def strict_json(raw, maximum):
    if len(raw) > maximum:
        raise CheckError("JSON member exceeds its byte limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise CheckError("JSON member is not valid UTF-8") from None

    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise CheckError("duplicate JSON key")
            value[key] = item
        return value

    def constant(token):
        raise CheckError("non-finite JSON number: " + token)

    try:
        # decode() consumes only JSON's four whitespace characters on both
        # sides and rejects trailing data, matching the kernel parser.
        value = json.JSONDecoder(object_pairs_hook=pairs, parse_constant=constant).decode(text)
    except json.JSONDecodeError:
        raise CheckError("invalid JSON syntax") from None
    return value


def safe_components(relative):
    parts = PurePosixPath(relative).parts
    if not parts or any(part in {".", "..", "/"} or "\\" in part
                        or any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in part)
                        for part in parts):
        raise CheckError("unsafe artifact path: " + repr(relative))
    return parts


class DirectoryReader:
    format = "directory"

    def __init__(self, root):
        self.root = root
        info = root.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise CheckError("artifact root must be a real directory, not a symlink")

    def read(self, relative, maximum):
        parts = safe_components(relative)
        path = self.root
        for index, part in enumerate(parts):
            path = path / part
            try:
                info = path.lstat()
            except (FileNotFoundError, NotADirectoryError):
                raise CheckError("missing member: " + relative) from None
            if stat.S_ISLNK(info.st_mode):
                raise CheckError("artifact member path contains a symbolic link: " + relative)
            expected = stat.S_ISREG(info.st_mode) if index == len(parts) - 1 else stat.S_ISDIR(info.st_mode)
            if not expected:
                raise CheckError("artifact member must be a regular file: " + relative)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise CheckError("artifact member exceeds its size limit: " + relative)
        return raw


class TarReader:
    format = "tar"

    def __init__(self, path):
        with path.open("rb") as stream:
            magic = stream.read(6)
        if magic[:2] == b"\x1f\x8b" or magic[:3] == b"BZh" or magic[:6] == b"\xfd7zXZ\x00":
            raise UnsupportedFormat("compressed tar is not supported; provide an uncompressed tar or a directory")
        try:
            self.archive = tarfile.open(path, mode="r:")
        except tarfile.TarError:
            raise CheckError("input is not a readable uncompressed tar archive") from None
        try:
            self._scan_members()
        except BaseException:
            self.archive.close()
            raise

    def _scan_members(self):
        self.members = {}
        seen = set()
        infos = self.archive.getmembers()
        if len(infos) > MAX_TAR_MEMBERS:
            raise CheckError("tar archive has too many members")
        for info in infos:
            name = info.name
            normalized = str(PurePosixPath(name))
            if name.startswith("/") or normalized != name.rstrip("/") or any(
                    part in {".", ".."} for part in PurePosixPath(name).parts) or "\\" in name or any(
                    unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in name):
                raise CheckError("unsafe tar member name: " + repr(name))
            if normalized in seen:
                raise CheckError("duplicate tar member: " + repr(name))
            seen.add(normalized)
            if info.isdir():
                continue
            if not info.isreg():
                raise CheckError("tar member must be a regular file or directory: " + repr(name))
            self.members[normalized] = info

    def read(self, relative, maximum):
        safe_components(relative)
        info = self.members.get(relative)
        if info is None:
            raise CheckError("missing member: " + relative)
        if info.size > maximum:
            raise CheckError("artifact member exceeds its size limit: " + relative)
        with self.archive.extractfile(info) as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise CheckError("artifact member exceeds its size limit: " + relative)
        return raw

    def close(self):
        self.archive.close()


class UnsupportedFormat(Exception):
    pass


def single_line_string(value):
    return isinstance(value, str) and value != "" and not any(
        unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in value)


def check_manifest(reader):
    errors = []
    report = {"capabilities_checked": 0, "http_contract_checked": False,
              "signature_verification": "not-performed"}
    raw = reader.read("xelab/plugin.json", MEMBER_LIMIT)
    manifest = strict_json(raw, MEMBER_LIMIT)
    report["manifest_digest"] = digest(raw)
    if not isinstance(manifest, dict):
        raise CheckError("xelab/plugin.json must be a JSON object")
    if manifest.get("manifest_version") != "1":
        raise UnsupportedFormat("unsupported manifest_version; only the existing v1 profile is checked")
    for field in ("plugin_id", "version"):
        if not single_line_string(manifest.get(field)):
            errors.append(f"manifest {field} must be a non-empty single-line string")
        elif not any(ord(c) < 32 or ord(c) == 127 for c in manifest[field]):
            report[field] = manifest[field]
    publisher = manifest.get("publisher_id")
    try:
        if not isinstance(publisher, str):
            raise ValueError
        uuid.UUID(publisher)
    except ValueError:
        errors.append("manifest publisher_id must be a UUID string")

    contracts = {}
    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, list):
        errors.append("manifest capabilities must be a list")
        capabilities = []
    for index, cap in enumerate(capabilities):
        label = f"capabilities[{index}]"
        if not isinstance(cap, dict) or not isinstance(cap.get("contract"), dict):
            errors.append(label + " must contain a contract object")
            continue
        contract = cap["contract"]
        name, version = contract.get("name"), contract.get("version")
        if not single_line_string(name) or not single_line_string(version) or "/" in name + version:
            errors.append(label + " contract name/version must be safe single path components")
            continue
        checks = []
        ok = True
        for file, field in zip(CONTRACT_FILES, ("input_schema", "output_schema", "stream_item_schema", "")):
            relative = f"xelab/contracts/{name}/{version}/{file}"
            try:
                member = reader.read(relative, MEMBER_LIMIT)
                value = strict_json(member, MEMBER_LIMIT)
            except CheckError as error:
                errors.append(f"{label}: {error}")
                ok = False
                continue
            if field:
                if value != cap.get(field):
                    errors.append(f"{label}: {relative} differs from signed manifest")
                    ok = False
            elif not isinstance(value, dict) or value.get("side_effect") != cap.get("side_effect"):
                errors.append(f"{label}: contract side effects differ")
                ok = False
            checks.append(f"{file} {hashlib.sha256(member).hexdigest()}\n")
        if ok:
            if digest("".join(checks).encode()) != contract.get("schema_digest"):
                errors.append(f"{label}: contract package digest differs")
            else:
                contracts[(name, version)] = contract.get("schema_digest")
                report["capabilities_checked"] += 1

    routes = manifest.get("routes")
    routes = routes if isinstance(routes, list) else []
    http_references = [(f"routes[{i}]", r.get("http_contract_digest")) for i, r in enumerate(routes)
                       if isinstance(r, dict) and r.get("kind") == "http"]
    skills = manifest.get("skills")
    if not isinstance(skills, list):
        errors.append("manifest skills must be a list")
        skills = []
    operation_ids = set()
    for index, skill in enumerate(skills):
        target = skill.get("target") if isinstance(skill, dict) else None
        if not isinstance(target, dict):
            errors.append(f"skills[{index}] must contain a target object")
            continue
        kind = target.get("kind")
        if kind == "capability":
            reference = target.get("contract")
            key = ((reference["name"], reference["version"])
                   if isinstance(reference, dict)
                   and isinstance(reference.get("name"), str)
                   and isinstance(reference.get("version"), str) else None)
            if key not in contracts or contracts[key] != reference.get("schema_digest"):
                errors.append(f"skills[{index}] references an unknown or drifted capability contract")
        elif kind == "http":
            http_references.append((f"skills[{index}]", target.get("http_contract_digest")))
            operation = target.get("http_operation_id")
            if isinstance(operation, str) and single_line_string(operation):
                operation_ids.add(operation)
            elif isinstance(operation, str):
                errors.append(f"skills[{index}] has an invalid http_operation_id")
        else:
            errors.append(f"skills[{index}] has an unsupported target kind")

    if http_references:
        member = reader.read("xelab/http/openapi.json", HTTP_CONTRACT_LIMIT)
        contract = strict_json(member, HTTP_CONTRACT_LIMIT)
        actual = digest(member)
        report["http_contract_checked"] = True
        for label, expected in http_references:
            if not isinstance(expected, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", expected):
                errors.append(f"{label} has an invalid http_contract_digest")
            elif expected != actual:
                errors.append(f"{label}: HTTP contract digest differs from xelab/http/openapi.json")
        if not isinstance(contract, dict) or not isinstance(contract.get("paths"), dict):
            errors.append("HTTP contract must be an OpenAPI object with paths")
        else:
            available = {op["operationId"] for methods in contract["paths"].values()
                         if isinstance(methods, dict) for op in methods.values()
                         if isinstance(op, dict) and isinstance(op.get("operationId"), str)}
            for operation in sorted(operation_ids - available):
                errors.append("skill references an unknown http_operation_id: " + operation)
    return errors, report


def build_report(path):
    base = {"schema": SCHEMA, "kind": "artifact-consistency-report", "path": str(path),
            "signature_verification": "not-performed", "note": NOTE}
    if not os.path.lexists(path):
        return dict(base, status="not-checked", input_format="absent", errors=["input path does not exist; nothing was checked"])
    reader = None
    try:
        if path.is_dir():
            reader = DirectoryReader(path)
        elif path.is_file():
            reader = TarReader(path)
        else:
            raise UnsupportedFormat("input must be an artifact directory or an uncompressed tar file")
        errors, details = check_manifest(reader)
        status = "inconsistent" if errors else "consistent"
        return dict(base, status=status, input_format=reader.format, errors=errors, **details)
    except UnsupportedFormat as error:
        return dict(base, status="unsupported", input_format=reader.format if reader else "unknown", errors=[str(error)])
    except CheckError as error:
        return dict(base, status="inconsistent", input_format=reader.format if reader else "unknown", errors=[str(error)])
    except (OSError, tarfile.TarError) as error:
        return dict(base, status="inconsistent", input_format=reader.format if reader else "unknown",
                    errors=["cannot read artifact: " + str(error)])
    finally:
        if isinstance(reader, TarReader):
            reader.close()


def render_text(report):
    lines = [f"{report['path']}: {report['status']} ({report['input_format']})"]
    if "plugin_id" in report:
        lines.append(f"plugin {report['plugin_id']} version {report.get('version', '?')}")
    if "manifest_digest" in report:
        lines.append(f"manifest {report['manifest_digest']}")
    lines.append(f"capabilities checked: {report.get('capabilities_checked', 0)}; "
                 f"HTTP contract checked: {report.get('http_contract_checked', False)}")
    lines.append("signature verification: not-performed (kernel install path only)")
    for error in report["errors"]:
        lines.append("  error: " + error)
    lines.append(report["note"])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True, type=Path, help="artifact root directory or uncompressed tar file")
    parser.add_argument("--json", action="store_true", help="emit stable machine-readable JSON instead of text")
    args = parser.parse_args(argv)
    report = build_report(args.path)
    print(json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False) if args.json else render_text(report))
    return {"consistent": 0, "not-checked": 2}.get(report["status"], 1)


if __name__ == "__main__":
    sys.exit(main())
