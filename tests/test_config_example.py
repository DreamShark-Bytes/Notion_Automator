"""Tests for config_example.toml: it must parse, hold only placeholders, and document only keys the daemon reads."""
import os
import re
import tomllib

ROOT = os.path.dirname(os.path.dirname(__file__))
EXAMPLE = os.path.join(ROOT, "config_example.toml")


def _text() -> str:
    with open(EXAMPLE, encoding="utf-8") as f:
        return f.read()


def _uncomment(text: str) -> str:
    """Turn '# key = value' and '# [table]' lines into live TOML; leave prose comments alone."""
    out = []
    for line in text.splitlines():
        m = re.match(r"^# ((?:\[\[?[A-Za-z_]+\]?\]|[A-Za-z_]+\s*=).*)$", line)
        out.append(m.group(1) if m else line)
    return "\n".join(out)


def test_example_parses_as_toml():
    cfg = tomllib.loads(_text())
    assert "token" not in cfg  # the token lives in config_secrets.toml
    assert cfg["poll_interval"] == 60 and cfg["day_start_hour"] == 3 and cfg["week_start"] == "Sunday"


def test_example_with_every_option_uncommented_still_parses():
    cfg = tomllib.loads(_uncomment(_text()))
    assert cfg["token"] == "ntn_your_token_here"
    assert cfg["notion_version"] == "2026-03-11"
    assert "automation_hub_page_id" not in cfg  # no longer read
    assert cfg["config_hub_data_source_id"] == "your-config-data-source-id"
    assert "status_data_source_id" not in cfg  # no separate status data source any more
    assert cfg["non_completion_statuses"] and all(isinstance(x, str) for x in cfg["non_completion_statuses"])
    assert cfg["databases"][0]["id"] == "your-task-database-id"
    assert cfg["recurring_tasks"]["enabled"] is True


def test_example_contains_only_placeholders():
    text = _text()
    assert not re.search(r"[0-9a-fA-F]{32}", text)
    assert not re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", text)
    assert not re.search(r"(secret_|ntn_)[A-Za-z0-9]{20,}", text)


def test_every_top_level_key_is_read_by_the_daemon_or_the_live_sandbox_script():
    read_keys: set[str] = set()
    for name in ("daemon.py", "hub.py", os.path.join("tests", "live_sandbox.py")):
        with open(os.path.join(ROOT, name), encoding="utf-8") as f:
            read_keys |= set(re.findall(r'cfg\.get\("(\w+)"', f.read()))
    documented = set(tomllib.loads(_uncomment(_text())).keys())
    assert documented - read_keys == set(), f"documented but never read by daemon.py, hub.py or tests/live_sandbox.py: {documented - read_keys}"


def test_every_database_flag_is_understood():
    with open(os.path.join(ROOT, "daemon.py"), encoding="utf-8") as f:
        src = f.read()
    flags = set(tomllib.loads(_uncomment(_text()))["databases"][0]) - {"id"}
    # Flags the config.toml fallback translates itself, or hands to the automations as-is.
    translated = {"closed_date", "reopen_count"}
    for flag in flags - translated:
        assert flag in open(os.path.join(ROOT, "automations.py"), encoding="utf-8").read(), flag
    for flag in translated:
        assert f'db.get("{flag}")' in src, flag


def test_secrets_example_parses_and_holds_only_a_placeholder():
    with open(os.path.join(ROOT, "config_secrets_example.toml"), "rb") as f:
        secrets = tomllib.load(f)
    assert set(secrets) == {"token", "sandbox_token"}
    assert set(secrets.values()) == {"ntn_your_token_here"}  # placeholders only


def test_secrets_file_is_gitignored():
    with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as f:
        lines = {line.strip() for line in f}
    assert "config_secrets.toml" in lines
