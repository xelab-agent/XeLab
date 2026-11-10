"""Offline fixture checks for the read-only artifact consistency preflight."""
import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from check_artifact import (CheckError, CONTRACT_FILES, build_report, json_values_equal,
                            main, render_text, strict_json)

ROOT = Path(__file__).resolve().parents[2]
PUBLISHER = "00000000-0000-4000-8000-000000000001"


def load_builder(relative, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ECHO = load_builder("examples/echo/build_artifact.py", "echo_builder")
HTTP = load_builder("examples/http/build_artifact.py", "http_builder")


def tar_member(archive, name, data=b"", kind=tarfile.REGTYPE, linkname=""):
    info = tarfile.TarInfo(name)
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    info.type = kind
    info.linkname = linkname
    archive.addfile(info, io.BytesIO(data))


def run_main(path, *extra):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(["--path", str(path), *extra])
    return code, out.getvalue()


class ArtifactCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def echo(self, **kwargs):
        directory = self.root / "artifact"
        ECHO.build(directory, PUBLISHER, **kwargs)
        return directory

    def http(self, **kwargs):
        directory = self.root / "artifact"
        HTTP.build(directory, PUBLISHER, **kwargs)
        return directory

    def manifest(self, directory):
        return json.loads((directory / "xelab/plugin.json").read_bytes())

    def write_manifest(self, directory, manifest):
        (directory / "xelab/plugin.json").write_text(json.dumps(manifest), encoding="utf-8")

    def contract_path(self, directory, file, index=0):
        contract = self.manifest(directory)["capabilities"][index]["contract"]
        return directory / "xelab/contracts" / contract["name"] / contract["version"] / file

    def tar_of(self, directory, extra=()):
        path = self.root / "artifact.tar"
        with tarfile.open(path, "w") as archive:
            for file in sorted(directory.rglob("*")):
                if file.is_file():
                    archive.add(file, arcname=str(file.relative_to(directory)))
            for args in extra:
                tar_member(archive, *args)
        return path


class ConsistencyTests(ArtifactCase):
    def test_contract_schema_compares_json_types_like_kernel(self):
        directory = self.echo()
        original_manifest = self.manifest(directory)
        input_path = self.contract_path(directory, "input.schema.json")
        original_schema = json.loads(input_path.read_bytes())
        cases = (
            ("bool_vs_integer", True, 1, "inconsistent"),
            ("integer_vs_bool", 0, False, "inconsistent"),
            ("integer_vs_float", 1, 1.0, "inconsistent"),
            ("same_float", 1.0, 1.0, "consistent"),
            ("large_integer_is_float_in_kernel", 2 ** 64, float(2 ** 64), "consistent"),
        )
        for name, manifest_value, member_value, expected in cases:
            with self.subTest(name=name):
                manifest = json.loads(json.dumps(original_manifest))
                schema = json.loads(json.dumps(original_schema))
                capability = manifest["capabilities"][0]
                capability["input_schema"]["properties"]["message"] = {"enum": [manifest_value]}
                schema["properties"]["message"] = {"enum": [member_value]}
                input_path.write_text(json.dumps(schema))
                checks = "".join(f"{file} {hashlib.sha256((input_path.parent / file).read_bytes()).hexdigest()}\n"
                                 for file in CONTRACT_FILES)
                digest = "sha256:" + hashlib.sha256(checks.encode()).hexdigest()
                contract = capability["contract"]
                contract["schema_digest"] = digest
                for skill in manifest["skills"]:
                    reference = skill["target"].get("contract", {})
                    if (reference.get("name"), reference.get("version")) == (contract["name"], contract["version"]):
                        reference["schema_digest"] = digest
                self.write_manifest(directory, manifest)
                report = build_report(directory)
                self.assertEqual(report["status"], expected, report["errors"])
                if expected == "inconsistent":
                    self.assertTrue(any("differs from signed manifest" in error for error in report["errors"]))

    def test_strict_json_rejects_overflowing_exponent(self):
        with self.assertRaises(CheckError):
            strict_json(b"1e400", 1024)

    def test_strict_json_number_representation(self):
        minus_zero = strict_json(b"-0", 1024)
        self.assertTrue(json_values_equal(minus_zero, strict_json(b"0.0", 1024)))
        self.assertFalse(json_values_equal(minus_zero, strict_json(b"0", 1024)))
        self.assertFalse(json_values_equal(strict_json(b"1", 1024), strict_json(b"1.0", 1024)))

    def test_manifest_whitespace_uses_json_grammar(self):
        directory = self.echo()
        member = directory / "xelab/plugin.json"
        original = member.read_bytes()
        for name, raw, expected in (
            ("leading", b" \t\r\n" + original, "consistent"),
            ("trailing", original + b" \t\r\n", "consistent"),
            ("non_json_trailing", original + "\u00a0".encode(), "inconsistent"),
        ):
            with self.subTest(name=name):
                member.write_bytes(raw)
                self.assertEqual(build_report(directory)["status"], expected)

    def test_unhashable_skill_contract_reference_produces_json_report(self):
        directory = self.echo()
        original = self.manifest(directory)
        for field in ("name", "version"):
            for invalid in ([], {}):
                with self.subTest(field=field, invalid=invalid):
                    manifest = json.loads(json.dumps(original))
                    manifest["skills"][0]["target"]["contract"][field] = invalid
                    self.write_manifest(directory, manifest)
                    code, output = run_main(directory, "--json")
                    report = json.loads(output)
                    self.assertEqual((code, report["status"]), (1, "inconsistent"))
                    self.assertTrue(any("unknown or drifted capability contract" in error
                                        for error in report["errors"]))

    def test_echo_rust_directory_is_consistent(self):
        report = build_report(self.echo())
        self.assertEqual(report["status"], "consistent")
        self.assertEqual(report["input_format"], "directory")
        self.assertEqual(report["capabilities_checked"], 6)
        self.assertEqual(report["signature_verification"], "not-performed")
        self.assertTrue(report["manifest_digest"].startswith("sha256:"))
        self.assertEqual(report["errors"], [])

    def test_echo_python_directory_is_consistent(self):
        report = build_report(self.echo(implementation="python"))
        self.assertEqual((report["status"], report["capabilities_checked"]), ("consistent", 1))

    def test_http_directory_is_consistent(self):
        report = build_report(self.http(include_rpc=True))
        self.assertEqual(report["status"], "consistent")
        self.assertTrue(report["http_contract_checked"])
        self.assertEqual(report["capabilities_checked"], 1)

    def test_tar_matches_directory_result(self):
        directory = self.echo()
        report = build_report(self.tar_of(directory))
        self.assertEqual((report["status"], report["input_format"]), ("consistent", "tar"))
        self.assertEqual(report["manifest_digest"], build_report(directory)["manifest_digest"])

    def test_contract_digest_mismatch(self):
        directory = self.echo()
        member = self.contract_path(directory, "input.schema.json")
        member.write_bytes(member.read_bytes() + b"\n")
        report = build_report(directory)
        self.assertEqual(report["status"], "inconsistent")
        self.assertTrue(any("package digest differs" in e for e in report["errors"]))

    def test_schema_drift_against_manifest(self):
        directory = self.echo()
        self.contract_path(directory, "input.schema.json").write_text('{"type": "null"}\n')
        report = build_report(directory)
        self.assertTrue(any("differs from signed manifest" in e for e in report["errors"]))

    def test_missing_member(self):
        directory = self.echo()
        self.contract_path(directory, "semantics.json").unlink()
        report = build_report(directory)
        self.assertTrue(any("missing member" in e for e in report["errors"]))

    def test_side_effect_mismatch(self):
        directory = self.echo()
        manifest = self.manifest(directory)
        manifest["capabilities"][0]["side_effect"] = "queryable"
        self.write_manifest(directory, manifest)
        report = build_report(directory)
        self.assertTrue(any("side effects differ" in e for e in report["errors"]))

    def test_skill_contract_drift(self):
        directory = self.echo()
        manifest = self.manifest(directory)
        manifest["skills"][0]["target"]["contract"]["schema_digest"] = "sha256:" + "0" * 64
        self.write_manifest(directory, manifest)
        report = build_report(directory)
        self.assertTrue(any("unknown or drifted capability contract" in e for e in report["errors"]))

    def test_unsafe_contract_name(self):
        directory = self.echo()
        manifest = self.manifest(directory)
        manifest["capabilities"][0]["contract"]["name"] = "../evil"
        self.write_manifest(directory, manifest)
        report = build_report(directory)
        self.assertTrue(any("safe single path components" in e for e in report["errors"]))

    def test_http_digest_mismatch(self):
        directory = self.http()
        member = directory / "xelab/http/openapi.json"
        member.write_bytes(member.read_bytes() + b"\n")
        report = build_report(directory)
        self.assertTrue(any("HTTP contract digest differs" in e for e in report["errors"]))

    def test_unknown_http_operation(self):
        directory = self.http()
        manifest = self.manifest(directory)
        manifest["skills"][0]["target"]["http_operation_id"] = "missingOperation"
        self.write_manifest(directory, manifest)
        report = build_report(directory)
        self.assertTrue(any("unknown http_operation_id" in e for e in report["errors"]))


class SafetyTests(ArtifactCase):
    def test_tar_path_traversal(self):
        path = self.tar_of(self.echo(), extra=[("../../evil", b"x")])
        report = build_report(path)
        self.assertEqual(report["status"], "inconsistent")
        self.assertTrue(any("unsafe tar member name" in e for e in report["errors"]))

    def test_tar_absolute_member(self):
        path = self.tar_of(self.echo(), extra=[("/evil", b"x")])
        self.assertTrue(any("unsafe tar member name" in e for e in build_report(path)["errors"]))

    def test_tar_control_character_member_is_rejected_without_output(self):
        path = self.tar_of(self.echo(), extra=[("xelab/\x9b", b"x")])
        report = build_report(path)
        self.assertEqual(report["status"], "inconsistent")
        self.assertTrue(any("unsafe tar member name" in e for e in report["errors"]))
        self.assertNotIn("\x9b", render_text(report))

    def test_tar_duplicate_member(self):
        path = self.tar_of(self.echo(), extra=[("xelab/plugin.json", b"{}")])
        self.assertTrue(any("duplicate tar member" in e for e in build_report(path)["errors"]))

    def test_tar_symlink_member(self):
        path = self.tar_of(self.echo(), extra=[("link", b"", tarfile.SYMTYPE, "/etc")])
        self.assertTrue(any("regular file or directory" in e for e in build_report(path)["errors"]))

    def test_directory_symlink_member(self):
        directory = self.echo()
        member = self.contract_path(directory, "semantics.json")
        target = member.with_suffix(".real")
        member.rename(target)
        member.symlink_to(target.name)
        report = build_report(directory)
        self.assertTrue(any("symbolic link" in e for e in report["errors"]))

    def test_non_regular_directory_member(self):
        directory = self.echo()
        member = self.contract_path(directory, "semantics.json")
        member.unlink()
        member.mkdir()
        report = build_report(directory)
        self.assertTrue(any("regular file" in e for e in report["errors"]))

    def test_non_strict_json_inputs(self):
        directory = self.echo()
        member = directory / "xelab/plugin.json"
        for raw in [b'{"a":1,"a":2}', b'{} {}', b'NaN', b'\xff\xfe', b'{"a": Infinity}']:
            with self.subTest(raw=raw):
                member.write_bytes(raw)
                report = build_report(directory)
                self.assertEqual(report["status"], "inconsistent")

    def test_oversized_manifest(self):
        directory = self.echo()
        (directory / "xelab/plugin.json").write_bytes(b" " * (1048576 + 1))
        report = build_report(directory)
        self.assertTrue(any("exceeds" in e for e in report["errors"]))

    def test_unsupported_manifest_version_fails_closed(self):
        directory = self.echo()
        manifest = self.manifest(directory)
        manifest["manifest_version"] = "2"
        self.write_manifest(directory, manifest)
        code, out = run_main(directory, "--json")
        report = json.loads(out)
        self.assertEqual((code, report["status"]), (1, "unsupported"))
        self.assertEqual(report["signature_verification"], "not-performed")

    def test_compressed_tar_is_unsupported(self):
        path = self.root / "artifact.tar.gz"
        path.write_bytes(gzip.compress(self.tar_of(self.echo()).read_bytes()))
        report = build_report(path)
        self.assertEqual(report["status"], "unsupported")

    def test_missing_path_is_not_checked(self):
        code, out = run_main(self.root / "absent", "--json")
        report = json.loads(out)
        self.assertEqual((code, report["status"]), (2, "not-checked"))
        self.assertIn("nothing was checked", report["errors"][0])

    def test_directory_without_manifest_is_inconsistent(self):
        directory = self.root / "empty"
        directory.mkdir()
        report = build_report(directory)
        self.assertEqual(report["status"], "inconsistent")
        self.assertTrue(any("missing member" in e for e in report["errors"]))


class BoundaryTests(ArtifactCase):
    def test_no_network_or_execution_in_source(self):
        source = (ROOT / "tools/artifacts/check_artifact.py").read_text()
        for token in ("urllib", "subprocess", "socket", "http.client", "os.system", "eval(", "exec("):
            self.assertNotIn(token, source)

    def test_never_claims_signature_valid(self):
        for path in (self.echo(), self.tar_of(self.echo())):
            report = build_report(path)
            self.assertEqual(report["signature_verification"], "not-performed")
            self.assertIn("NOT performed", render_text(report))
            self.assertNotIn("signature valid", json.dumps(report).replace("never claims a signature is valid", ""))

    def test_json_output_is_stable_and_exit_codes_match(self):
        directory = self.echo()
        code1, out1 = run_main(directory, "--json")
        code2, out2 = run_main(directory, "--json")
        self.assertEqual((code1, code2, out1), (0, 0, out2))
        self.assertEqual(json.loads(out1)["status"], "consistent")

    def test_control_characters_stay_out_of_text_report(self):
        directory = self.echo()
        manifest = self.manifest(directory)
        manifest["plugin_id"] = "org.xelab.echo\x1b[31m"
        manifest["capabilities"][0]["contract"]["name"] = "evil\x1b[31m\x9b\u202e"
        self.write_manifest(directory, manifest)
        report = build_report(directory)
        self.assertNotIn("plugin_id", report)
        text = render_text(report)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x9b", text)
        self.assertNotIn("\u202e", text)

    def test_control_characters_in_http_operation_id_are_rejected(self):
        directory = self.http()
        manifest = self.manifest(directory)
        manifest["skills"][0]["target"]["http_operation_id"] = "missing\x1b[31m\x9b\u202e"
        self.write_manifest(directory, manifest)
        report = build_report(directory)
        self.assertEqual(report["status"], "inconsistent")
        self.assertTrue(any("invalid http_operation_id" in e for e in report["errors"]))
        text = render_text(report)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x9b", text)
        self.assertNotIn("\u202e", text)


if __name__ == "__main__":
    unittest.main()
