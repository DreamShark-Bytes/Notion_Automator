"""Tests for the default status row title."""
import hub


def test_status_row_default_title_is_not_hub_status():
    assert hub._STATUS_ROW_NAME == "Automator Notion Status"
