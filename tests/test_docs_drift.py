"""Drift checks: the docs, the example files and the code must name the same keys, columns and message sources."""
import os
import re

import hub

ROOT = os.path.dirname(os.path.dirname(__file__))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


REFERENCE = _read("docs", "config_reference.md")
README = _read("README.md")

# Keys the daemon still looks at only to warn that they are gone, or that belong to the old config.toml format.
RETIRED_CONFIG_KEYS = {"automation_hub_page_id", "status_data_source_id", "token", "databases", "recurring_tasks", "non_completion_statuses"}


def test_every_row_key_the_parser_accepts_is_in_the_reference_and_the_example():
    example = _read("docs", "config_row_example.toml")
    for key in hub._PROPERTY_AUTOMATION_KEYS | hub._RECURRING_TASKS_KEYS:
        assert f"`{key}`" in REFERENCE, f"{key} is missing from docs/config_reference.md"
        assert re.search(rf"^# {key} =", example, re.MULTILINE), f"{key} is missing from docs/config_row_example.toml"


def test_every_config_toml_key_the_daemon_reads_is_in_the_reference_and_the_example():
    example = _read("config_example.toml")
    read = set()
    for name in ("daemon.py", "hub.py", os.path.join("tests", "live_sandbox.py")):
        read |= set(re.findall(r'cfg\.get\("(\w+)"', _read(name)))
    for key in read - RETIRED_CONFIG_KEYS:
        assert f"`{key}`" in REFERENCE, f"{key} is missing from docs/config_reference.md"
        assert re.search(rf"^# {key} =|^{key} =", example, re.MULTILINE), f"{key} is missing from config_example.toml"


def test_every_column_the_code_uses_is_in_the_readme_and_the_reference():
    columns = {"Name", "Status", *hub._REQUIRED_CONFIG_PROPS, hub._VERIFIED_PROP, hub._VERSION_PROP, hub._REPORT_PROP}
    for column in columns:
        assert f"| {column} |" in README, f"column {column} is missing from the README table"
        assert f"| {column} |" in REFERENCE, f"column {column} is missing from docs/config_reference.md"


def test_every_message_source_the_code_uses_is_explained_in_the_reference():
    sources = set()
    for name in ("hub.py", "daemon.py"):
        sources |= set(re.findall(r'\breport\("([\w.\-:]+)"', _read(name)))
    assert sources, "no message sources found: the pattern is out of date"
    for source in sources - {"config-edit"}:
        assert f"`[{source}]`" in REFERENCE, f"message source [{source}] is not explained in docs/config_reference.md"
    assert "`[config-edit]`" in REFERENCE


def test_removed_names_do_not_appear_in_the_docs_outside_the_upgrade_notes():
    removed = ["inherited_properties", "definitions_db_id", "tasks_db_id", "automation_hub_page_id"]
    for name in ("README.md", os.path.join("docs", "initial_setup.md"), os.path.join("docs", "config_reference.md"),
                 os.path.join("docs", "create_your_own_automations.md"), "config_example.toml"):
        text = _read(name)
        for old in removed:
            assert old not in text, f"{name} still mentions {old}"
    # The old [[databases]] table is named only where the move is explained.
    for name in ("docs/initial_setup.md", "docs/create_your_own_automations.md"):
        assert "[[databases]]" not in _read(name), name
