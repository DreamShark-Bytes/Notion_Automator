"""The .gitignore must keep local-only files out of git and must not hide files that belong in it.

A path is checked with `git check-ignore`, so the file does not need to exist. This exists because a scratch archive
was once created before its ignore entry, and a docs folder was once ignored by mistake.
"""
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or not os.path.isdir(os.path.join(ROOT, ".git")), reason="needs a git checkout"
)

MUST_BE_IGNORED = [
    "scratch.md",
    "scratch-archive.md",
    "STATUS.md",
    "config.toml",
    "config_secrets.toml",
    "config_hub_state.toml",
    "config_automations.toml",
    "notion_wiki_drafts/welcome.md",
    ".pytest_cache/README.md",
    "__pycache__/x.pyc",
    "notion_daemon.log",
    "AGENTS.md",
    "AI_AGENT_INSTRUCTIONS.md",
    "tools/config.toml",
    "tools/config_throw_away.toml",
    "tools/fix_backup_20260519.csv",
    "tools/migrate_relationships.py",
]

MUST_NOT_BE_IGNORED = [
    "docs/upgrade_steps.md",
    "docs/create_your_own_automations.md",
    "config_example.toml",
    "config_secrets_example.toml",
    "README.md",
    "PLANNED.md",
    "DESIGN.md",
    "tests/test_gitignore.py",
    "tools/describe_database.py",  # tests/test_describe_database.py imports it, so it must ship with the test
]


def _is_ignored(path: str) -> bool:
    result = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT)
    return result.returncode == 0


@pytest.mark.parametrize("path", MUST_BE_IGNORED)
def test_local_only_file_is_ignored(path):
    assert _is_ignored(path), f"{path} must be listed in .gitignore"


@pytest.mark.parametrize("path", MUST_NOT_BE_IGNORED)
def test_tracked_file_is_not_ignored(path):
    assert not _is_ignored(path), f"{path} must not be ignored"
