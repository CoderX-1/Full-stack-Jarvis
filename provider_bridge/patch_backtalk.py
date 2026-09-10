#!/usr/bin/env python3
"""Apply the Gemini/OpenAI compatibility overlay to a Backtalk checkout."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count == 0 and new in text:
        return text
    if count != 1:
        raise RuntimeError(f"Could not patch {label}: expected one match, found {count}")
    return text.replace(old, new, 1)


def patch(target: Path) -> None:
    target = target.resolve()
    package = target / "backtalk"
    pyproject = target / "pyproject.toml"
    main_file = package / "main.py"
    config_file = package / "config.py"
    for required in (package / "brain.py", pyproject, main_file, config_file):
        if not required.is_file():
            raise RuntimeError(f"Not a Backtalk checkout: missing {required}")

    source_brain = Path(__file__).resolve().parent / "brain.py"
    shutil.copyfile(source_brain, package / "brain.py")

    project = pyproject.read_text(encoding="utf-8")
    project = "\n".join(
        line for line in project.splitlines() if "claude-agent-sdk" not in line
    ) + "\n"
    pyproject.write_text(project, encoding="utf-8")

    main = main_file.read_text(encoding="utf-8")
    main = replace_once(
        main,
        "    from claude_agent_sdk import (PermissionResultAllow,\n                                  PermissionResultDeny)",
        "    from backtalk.brain import (PermissionResultAllow,\n                                PermissionResultDeny)",
        "permission result import",
    )
    main = main.replace(
        "couldn't reach my brain, the Claude Code session.",
        "couldn't reach my configured AI provider.",
    ).replace(
        "Claude Code isn't signed in",
        "the API key is missing",
    ).replace(
        "or the plan is out of usage.",
        "or the account is out of API credit.",
    )
    main_file.write_text(main, encoding="utf-8")

    config = config_file.read_text(encoding="utf-8")
    config = config.replace('    "model": "claude-sonnet-5",', '    "model": "",')
    config = config.replace('    "deep_model": "claude-opus-5",', '    "deep_model": "",')
    anchor = '    "name": "Assistant",\n'
    addition = (
        anchor
        + '    # Model API. Keys stay in GEMINI_API_KEY, OPENAI_API_KEY, or AI_API_KEY.\n'
        + '    "provider": "openai",\n'
        + '    "base_url": "",\n'
    )
    if '    "provider": "openai",' not in config:
        config = replace_once(config, anchor, addition, "provider defaults")
    config_file.write_text(config, encoding="utf-8")

    (target / "PROVIDER_COMPATIBILITY.md").write_text(
        "# Gemini and OpenAI compatibility\n\n"
        "This checkout is patched by the sibling `fullstack-agent` repository. "
        "The brain uses an OpenAI-compatible Chat Completions API instead of the "
        "Claude Agent SDK.\n\n"
        "Set `provider` and `model` in `backtalk.json`. Keep the secret out of "
        "that file: use `GEMINI_API_KEY`, `OPENAI_API_KEY`, or `AI_API_KEY` in "
        "the launcher's environment. Custom services also need `base_url` in "
        "the config. Re-run `fullstack-agent/provider_bridge/patch_backtalk.py` "
        "after manually updating Backtalk.\n",
        encoding="utf-8",
    )
    print(f"Patched Backtalk for Gemini/OpenAI APIs: {target}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("backtalk_dir", type=Path)
    args = parser.parse_args()
    patch(args.backtalk_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
