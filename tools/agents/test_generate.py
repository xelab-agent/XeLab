"""Offline checks for deterministic, credential-free agent instruction generation."""
import contextlib
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import generate

ROOT = generate.ROOT
CONFIG = ROOT / "tools/agents/config.json"
TEMPLATES = ROOT / "tools/agents/templates"
EXECUTOR_NAMES = ("chatgpt", "claude", "gemini", "copilot", "codex", "qoder", "cursor", "openai", "anthropic", "gpt-4", "gpt-5")


def load_config():
    return generate.load_config(CONFIG)


def render(config=None):
    return generate.render(load_config() if config is None else config, TEMPLATES)


def write_config(directory, config):
    path = Path(directory) / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


class GenerateTests(unittest.TestCase):
    def test_same_input_is_byte_stable(self):
        config = load_config()
        expected = render(config)
        self.assertEqual(render(config), expected)
        self.assertEqual(render(dict(reversed(list(config.items())))), expected)

    def test_checked_in_outputs_match_config(self):
        for target, content in render().items():
            self.assertEqual((ROOT / target).read_text(encoding="utf-8"), content, target)

    def test_outputs_and_templates_are_credential_free(self):
        for target, content in render().items():
            self.assertIsNone(generate.CREDENTIAL.search(content), target)
        for template in sorted(TEMPLATES.iterdir()):
            self.assertIsNone(generate.CREDENTIAL.search(template.read_text(encoding="utf-8")), template.name)

    def test_no_hardcoded_executor_or_model_names(self):
        for target, content in render().items():
            for name in EXECUTOR_NAMES:
                self.assertNotIn(name, content.casefold(), target)
        for template in sorted(TEMPLATES.iterdir()):
            for name in EXECUTOR_NAMES:
                self.assertNotIn(name, template.read_text(encoding="utf-8").casefold(), template.name)

    def test_contributor_instructions_grant_no_merge(self):
        agents = render()["AGENTS.md"]
        self.assertIn("禁止合并", agents)
        for phrase in ("才合并", "执行合并", "调用合并"):
            self.assertNotIn(phrase, agents)

    def test_maintenance_gate_lives_only_in_trusted_template(self):
        maintenance = render()["tools/agents/pr-maintenance.md"]
        for phrase in ("全部满足才", "独立受信机器", "不自动安装", "head SHA", "去重"):
            self.assertIn(phrase, maintenance)

    def test_permission_refusal_is_instructed(self):
        for content in render().values():
            self.assertIn("权限", content)
            self.assertIn("拒绝", content)

    def test_generator_never_reads_environment_or_network(self):
        source = (ROOT / "tools/agents/generate.py").read_text(encoding="utf-8")
        for forbidden in ("environ", "urllib", "requests", "socket", "subprocess"):
            self.assertNotIn(forbidden, source)

    def test_required_documents_are_referenced_and_exist(self):
        config = load_config()
        agents = render()["AGENTS.md"]
        for key in ("contributing", "issue_workflow", "issue_automation", "security"):
            self.assertIn(config[key], agents)
            self.assertTrue((ROOT / config[key]).is_file(), config[key])

    def test_generated_links_resolve_offline(self):
        for target, content in render().items():
            base = (ROOT / target).parent
            for link in re.findall(r"(?<!!)\[[^\]]*\]\(([^\n)]+)\)", content):
                if "://" in link or link.startswith("#"):
                    continue
                self.assertTrue((base / link).resolve().exists(), target + " -> " + link)

    def test_interval_is_config_driven(self):
        config = load_config()
        config["maintenance_interval_minutes"] = 15
        self.assertIn("每 15 分钟", render(config)["tools/agents/pr-maintenance.md"])
        self.assertIn("每 30 分钟", render()["tools/agents/pr-maintenance.md"])

    def test_config_rejects_credential_like_values(self):
        cases = [("contributing", "ghp_" + "a" * 36 + ".md"), ("default_branch", "feat/X_" + "MERGE" + "_TOKEN"), ("issue_workflow", "docs/github_pat_" + "b" * 36 + ".md"), ("security", "AGENT" + "_TOKEN" + ".md")]
        for key, value in cases:
            config = load_config()
            config[key] = value
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(ValueError):
                    generate.load_config(write_config(temporary, config))

    def test_config_rejects_unknown_missing_or_duplicate_keys(self):
        extra = load_config()
        extra["unexpected"] = "x"
        missing = load_config()
        del missing["repository"]
        for config in (extra, missing):
            with tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(ValueError):
                    generate.load_config(write_config(temporary, config))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text('{"repository": "a/b", "repository": "c/d"}', encoding="utf-8")
            with self.assertRaises(ValueError):
                generate.load_config(path)

    def test_config_rejects_unsafe_paths_and_intervals(self):
        cases = [("repository", "https://evil.invalid/repo"), ("repository", "a/../b"), ("contributing", "../secret.md"), ("contributing", "/etc/passwd.md"), ("default_branch", "../main"), ("maintenance_interval_minutes", 0), ("maintenance_interval_minutes", True), ("maintenance_interval_minutes", "30")]
        for key, value in cases:
            config = load_config()
            config[key] = value
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(ValueError):
                    generate.load_config(write_config(temporary, config))

    def test_unknown_placeholder_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "agents.md.tmpl").write_text("{{unknown_key}}\n", encoding="utf-8")
            (directory / "pr-maintenance.md.tmpl").write_text("ok\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                generate.render(load_config(), directory)

    def test_invalid_config_writes_nothing(self):
        before = {target: (ROOT / target).read_bytes() for target in generate.OUTPUTS}
        with tempfile.TemporaryDirectory() as temporary:
            path = write_config(temporary, {})
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(generate.main(["--config", str(path)]), 1)
        self.assertEqual({target: (ROOT / target).read_bytes() for target in generate.OUTPUTS}, before)

    def test_missing_configured_document_rejects_without_writing(self):
        for missing_key in generate.DOC_KEYS:
            with self.subTest(missing_key=missing_key), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = load_config()
                for key in generate.DOC_KEYS:
                    if key != missing_key:
                        document = root / config[key]
                        document.parent.mkdir(parents=True, exist_ok=True)
                        document.write_text('fixture', encoding='utf-8')
                outputs = {}
                for target in generate.OUTPUTS:
                    output = root / target
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text('unchanged', encoding='utf-8')
                    outputs[target] = output
                config_path = write_config(root, config)
                with patch.object(generate, 'ROOT', root), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(generate.main(['--config', str(config_path)]), 1)
                self.assertEqual({target: output.read_text(encoding='utf-8') for target, output in outputs.items()},
                                 {target: 'unchanged' for target in generate.OUTPUTS})


if __name__ == "__main__":
    unittest.main()
