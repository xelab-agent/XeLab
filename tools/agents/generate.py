#!/usr/bin/env python3
"""Render contributor agent instructions and a trusted PR-maintenance template from reviewed config."""
import argparse
import json
from pathlib import Path
import posixpath
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = ROOT / "tools/agents/templates"
CONFIG_KEYS = {"repository", "default_branch", "contributing", "issue_workflow", "issue_automation", "security", "maintenance_interval_minutes"}
DOC_KEYS = ("contributing", "issue_workflow", "issue_automation", "security")
OUTPUTS = {"AGENTS.md": "agents.md.tmpl", "tools/agents/pr-maintenance.md": "pr-maintenance.md.tmpl"}
CREDENTIAL = re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|MERGE_TOKEN|AGENT_TOKEN", re.IGNORECASE)
PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate config key")
        result[key] = value
    return result


def load_config(path):
    config = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_keys)
    if not isinstance(config, dict) or set(config) != CONFIG_KEYS:
        raise ValueError("config must contain exactly: " + ", ".join(sorted(CONFIG_KEYS)))
    repository = config["repository"]
    if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or any(part in {".", ".."} for part in repository.split("/")):
        raise ValueError("repository must be owner/name")
    branch = config["default_branch"]
    if not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch) or ".." in branch or branch.startswith("/") or branch.endswith("/") or "//" in branch:
        raise ValueError("invalid default_branch")
    for key in DOC_KEYS:
        value = config[key]
        if not isinstance(value, str) or not value.endswith(".md") or any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError(key + " must be a repo-relative Markdown path without traversal")
        if not (ROOT / value).is_file():
            raise ValueError(key + " must refer to an existing Markdown file")
    interval = config["maintenance_interval_minutes"]
    if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 1440:
        raise ValueError("maintenance_interval_minutes must be an integer between 1 and 1440")
    for value in config.values():
        if isinstance(value, str) and CREDENTIAL.search(value):
            raise ValueError("config contains a credential-like value; credentials never belong in reviewed config")
    return config


def render(config, template_dir=TEMPLATE_DIR):
    outputs = {}
    for target, template_name in OUTPUTS.items():
        template = (template_dir / template_name).read_text(encoding="utf-8")
        values = {key: str(value) for key, value in config.items()}
        for key in DOC_KEYS:
            values[key] = posixpath.relpath(config[key], posixpath.dirname(target) or ".")

        def replace(match):
            key = match.group(1)
            if key not in values:
                raise ValueError("unknown template placeholder: " + key)
            return values[key]

        content = PLACEHOLDER.sub(replace, template)
        if CREDENTIAL.search(content):
            raise ValueError("refusing to emit credential-like content in " + target)
        outputs[target] = content
    return outputs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "tools/agents/config.json")
    parser.add_argument("--check", action="store_true", help="verify generated files match the config without writing")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        outputs = render(config)
        drift = []
        for target, content in outputs.items():
            path = ROOT / target
            if args.check:
                if not path.is_file() or path.read_text(encoding="utf-8") != content:
                    drift.append(target)
            else:
                path.write_text(content, encoding="utf-8")
        if drift:
            print("generated files drifted: " + ", ".join(drift), file=sys.stderr)
            return 1
        print(("verified" if args.check else "wrote") + f" {len(outputs)} generated files")
    except (ValueError, OSError) as error:
        print(f"agent instruction generation stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
