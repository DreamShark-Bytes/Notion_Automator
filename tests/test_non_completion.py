"""
Tests for _is_non_completion with per-pair and built-in statuses.

Covers: default fallback to hardcoded frozenset (pair=None), per-pair override,
case/punctuation normalization, and None/empty input handling.
"""
import re
import recurring_tasks


def _make_pair_with_statuses(statuses):
    norm = frozenset(re.sub(r'[^a-z]', '', s.lower()) for s in statuses)
    return {"non_completion_statuses": norm}


# ------------------------------------------------------------------ #
#  Fallback: hardcoded NON_COMPLETION_STATUSES (pair=None)
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
#  Per-pair override
# ------------------------------------------------------------------ #

def test_configured_status_is_non_completion():
    pair = _make_pair_with_statuses(["Cancelled", "Handed off"])
    assert recurring_tasks._is_non_completion("Cancelled", pair) is True
    assert recurring_tasks._is_non_completion("Handed off", pair) is True

def test_configured_overrides_hardcoded_set():
    """A status in the hardcoded set but not in pair config should not be a non-completion."""
    pair = _make_pair_with_statuses(["Cancelled"])
    assert recurring_tasks._is_non_completion("skipped", pair) is False

def test_configured_done_is_not_non_completion():
    pair = _make_pair_with_statuses(["Cancelled"])
    assert recurring_tasks._is_non_completion("Done", pair) is False


# ------------------------------------------------------------------ #
#  Normalization
# ------------------------------------------------------------------ #

def test_case_insensitive():
    pair = _make_pair_with_statuses(["Cancelled"])
    assert recurring_tasks._is_non_completion("cancelled", pair) is True
    assert recurring_tasks._is_non_completion("CANCELLED", pair) is True
    assert recurring_tasks._is_non_completion("Cancelled", pair) is True

def test_punctuation_stripped():
    pair = _make_pair_with_statuses(["Handed off"])
    assert recurring_tasks._is_non_completion("Handed off", pair) is True
    assert recurring_tasks._is_non_completion("handed-off", pair) is True
    assert recurring_tasks._is_non_completion("handedoff", pair) is True
