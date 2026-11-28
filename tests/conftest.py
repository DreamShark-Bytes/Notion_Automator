import sys
import os

_tests_dir = os.path.dirname(__file__)
_project_root = os.path.dirname(_tests_dir)

# Project root: so tests can import recurring_tasks directly.
sys.path.insert(0, _project_root)
# Tests dir: so tests can import shared helpers.
sys.path.insert(0, _tests_dir)


import pytest


@pytest.fixture(autouse=True)
def _hub_files_in_tmp(tmp_path, monkeypatch):
    """The Hub writes its state file and the stored config copy next to config.toml. No test may touch the real ones."""
    import hub
    monkeypatch.setattr(hub, "HUB_CONFIG_PATH", str(tmp_path / "config_hub_state.toml"))
    monkeypatch.setattr(hub, "STORED_CONFIG_PATH", str(tmp_path / "config_automations.toml"))
