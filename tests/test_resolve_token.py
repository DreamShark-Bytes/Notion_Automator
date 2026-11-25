"""Tests for hub.load_secrets / hub.resolve_token: config_secrets.toml first, deprecated config.toml fallback, the token itself never logged."""
import logging
import os
import subprocess
import sys

import pytest

import hub

ROOT = os.path.dirname(os.path.dirname(__file__))
SECRET = "ntn_SECRETVALUE123"  # fake value used to prove a token never reaches a log; secret-scan: allow


def _write(path, text, mode=0o600):
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return str(path)


# ---- resolve_token ---------------------------------------------------------

def test_secrets_file_wins_over_config(caplog):
    with caplog.at_level(logging.INFO, logger="hub"):
        assert hub.resolve_token({"token": "from-config"}, {"token": "from-secrets"}) == "from-secrets"
    assert [r.levelno for r in caplog.records] == [logging.INFO]  # no deprecation warning
    assert "secrets file" in caplog.records[0].getMessage()


def test_config_fallback_warns_without_echoing_the_token(caplog):
    with caplog.at_level(logging.INFO, logger="hub"):
        assert hub.resolve_token({"token": SECRET}, {}) == SECRET
    assert len(caplog.records) == 1 and caplog.records[0].levelno == logging.WARNING
    msg = caplog.records[0].getMessage()
    assert "deprecated" in msg and hub.SECRETS_FILE_NAME in msg
    assert SECRET not in msg


def test_logging_never_contains_the_token(caplog):
    with caplog.at_level(logging.DEBUG, logger="hub"):
        hub.resolve_token({}, {"token": SECRET})
    assert SECRET not in caplog.text


def test_value_is_stripped():
    assert hub.resolve_token({}, {"token": "  tok  \n"}) == "tok"
    assert hub.resolve_token({"token": " tok2 "}, {}) == "tok2"


def test_blank_secrets_token_falls_through_to_config():
    assert hub.resolve_token({"token": "from-config"}, {"token": "   "}) == "from-config"


def test_no_token_anywhere_returns_none(caplog):
    with caplog.at_level(logging.INFO, logger="hub"):
        assert hub.resolve_token({}, None) is None
        assert hub.resolve_token({}, {}) is None
        assert hub.resolve_token({"token": ""}, {"token": ""}) is None
    assert not caplog.records


def test_environment_variable_is_not_a_source(monkeypatch):
    for name in ("NOTION_AUTOMATOR_TOKEN", "NOTION_TOKEN"):
        monkeypatch.setenv(name, "from-env")
    assert hub.resolve_token({}, {}) is None


# ---- load_secrets ----------------------------------------------------------

def test_load_secrets_reads_token(tmp_path):
    path = _write(tmp_path / "config_secrets.toml", f'token = "{SECRET}"\n')
    assert hub.load_secrets(path) == {"token": SECRET}


def test_missing_file_is_not_an_error(tmp_path):
    assert hub.load_secrets(str(tmp_path / "config_secrets.toml")) == {}


def test_malformed_file_raises_without_echoing_the_value(tmp_path):
    path = _write(tmp_path / "config_secrets.toml", f"token = {SECRET}\n")  # unquoted
    with pytest.raises(hub.tomllib.TOMLDecodeError) as exc:
        hub.load_secrets(path)
    assert SECRET not in str(exc.value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_loose_permissions_warn(tmp_path, caplog):
    path = _write(tmp_path / "config_secrets.toml", f'token = "{SECRET}"\n', mode=0o644)
    with caplog.at_level(logging.WARNING, logger="hub"):
        assert hub.load_secrets(path) == {"token": SECRET}
    assert "chmod 600" in caplog.text and SECRET not in caplog.text


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_private_file_does_not_warn(tmp_path, caplog):
    path = _write(tmp_path / "config_secrets.toml", f'token = "{SECRET}"\n', mode=0o600)
    with caplog.at_level(logging.WARNING, logger="hub"):
        hub.load_secrets(path)
    assert not caplog.records


# ---- end to end: the daemon process ---------------------------------------

def _run_daemon(cwd, *args):
    return subprocess.run(
        [sys.executable, os.path.join(ROOT, "daemon.py"), *args],
        cwd=cwd, capture_output=True, text=True, timeout=60,
        env={k: v for k, v in os.environ.items() if k != "NOTION_AUTOMATOR_TOKEN"},
    )


def test_daemon_without_a_token_names_the_secrets_file(tmp_path):
    cfg = _write(tmp_path / "config.toml", "poll_interval = 60\n")
    result = _run_daemon(tmp_path, "--config", cfg)
    assert result.returncode != 0
    assert os.path.join(str(tmp_path), "config_secrets.toml") in result.stderr  # defaults to next to the config file
    assert "No Notion token" in result.stderr


def test_daemon_hints_when_the_old_secrets_file_name_is_still_there(tmp_path):
    cfg = _write(tmp_path / "config.toml", "poll_interval = 60\n")
    _write(tmp_path / "secrets.toml", f'token = "{SECRET}"\n')
    result = _run_daemon(tmp_path, "--config", cfg)
    assert result.returncode != 0
    assert "was renamed" in result.stderr and SECRET not in result.stderr


def test_daemon_secrets_flag_overrides_the_default_location(tmp_path):
    cfg = _write(tmp_path / "config.toml", "poll_interval = 60\n")
    other = str(tmp_path / "elsewhere.toml")
    result = _run_daemon(tmp_path, "--config", cfg, "--secrets", other)
    assert other in result.stderr


def test_daemon_with_a_malformed_secrets_file_fails_without_leaking_it(tmp_path):
    cfg = _write(tmp_path / "config.toml", "poll_interval = 60\n")
    _write(tmp_path / "config_secrets.toml", f"token = {SECRET}\n")
    result = _run_daemon(tmp_path, "--config", cfg)
    assert result.returncode != 0
    assert "TOMLDecodeError" in result.stderr
    assert SECRET not in result.stderr
