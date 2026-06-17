"""
Tests for _is_non_completion and configure_non_completion_statuses.

Covers: default fallback to hardcoded frozenset, config-driven override,
case/punctuation normalization, and None/empty input handling.
"""
import pytest
import recurring_tasks


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setattr(recurring_tasks, "_configured_non_completion_statuses", None)


# ------------------------------------------------------------------ #
#  Fallback: hardcoded NON_COMPLETION_STATUSES
# ------------------------------------------------------------------ #

def test_fallback_cancelled_is_non_completion():
    assert recurring_tasks._is_non_completion("Cancelled") is True

def test_fallback_skipped_is_non_completion():
    assert recurring_tasks._is_non_completion("skipped") is True

def test_fallback_done_is_not_non_completion():
    assert recurring_tasks._is_non_completion("Done") is False

def test_fallback_in_progress_is_not_non_completion():
    assert recurring_tasks._is_non_completion("In progress") is False

def test_fallback_none_input():
    assert recurring_tasks._is_non_completion(None) is False

def test_fallback_empty_string():
    assert recurring_tasks._is_non_completion("") is False


# ------------------------------------------------------------------ #
#  Config-driven override
# ------------------------------------------------------------------ #

def test_configured_status_is_non_completion():
    recurring_tasks.configure_non_completion_statuses(["Cancelled", "Handed off"])
    assert recurring_tasks._is_non_completion("Cancelled") is True
    assert recurring_tasks._is_non_completion("Handed off") is True

def test_configured_overrides_hardcoded_set():
    """A status in the hardcoded set but not in config should not be a non-completion."""
    recurring_tasks.configure_non_completion_statuses(["Cancelled"])
    assert recurring_tasks._is_non_completion("skipped") is False

def test_configured_done_is_not_non_completion():
    recurring_tasks.configure_non_completion_statuses(["Cancelled"])
    assert recurring_tasks._is_non_completion("Done") is False


# ------------------------------------------------------------------ #
#  Normalization
# ------------------------------------------------------------------ #

def test_case_insensitive():
    recurring_tasks.configure_non_completion_statuses(["Cancelled"])
    assert recurring_tasks._is_non_completion("cancelled") is True
    assert recurring_tasks._is_non_completion("CANCELLED") is True
    assert recurring_tasks._is_non_completion("Cancelled") is True

def test_punctuation_stripped():
    recurring_tasks.configure_non_completion_statuses(["Handed off"])
    assert recurring_tasks._is_non_completion("Handed off") is True
    assert recurring_tasks._is_non_completion("handed-off") is True
    assert recurring_tasks._is_non_completion("handedoff") is True
