"""Tests for hub._is_valid_id and recurring_tasks duplicate pair detection."""
import sys
import os
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import hub


def test_is_valid_id_plain_32_hex():
    assert hub._is_valid_id("a" * 32) is True


def test_is_valid_id_upper_hex():
    assert hub._is_valid_id("A" * 32) is True


def test_is_valid_id_with_dashes():
    # Notion IDs are often formatted as 8-4-4-4-12
    notion_id = "12345678-1234-1234-1234-123456789abc"
    assert hub._is_valid_id(notion_id) is True


def test_is_valid_id_too_short():
    assert hub._is_valid_id("a" * 31) is False


def test_is_valid_id_too_long():
    assert hub._is_valid_id("a" * 33) is False


def test_is_valid_id_non_hex():
    assert hub._is_valid_id("g" * 32) is False


def test_is_valid_id_empty():
    assert hub._is_valid_id("") is False


def test_is_valid_id_none():
    assert hub._is_valid_id(None) is False


# ------------------------------------------------------------------ #
#  Duplicate pair detection via configure_pairs in recurring_tasks
# ------------------------------------------------------------------ #

import recurring_tasks


def _make_pair(tasks_id: str, defs_id: str) -> dict:
    return {
        "definitions_data_source_id": defs_id,
        "tasks_data_source_id":       tasks_id,
        "closed_date_field": "Closed Date",
        "non_completion_statuses": [],
        "inherited_fields":  [],
    }


def test_configure_pairs_single_pair():
    recurring_tasks.configure_pairs(
        [_make_pair("a" * 32, "b" * 32)],
        week_start_day=6,
        day_start_hour=3,
    )
    assert len(recurring_tasks._rtd_pairs) == 1
    assert "a" * 32 in recurring_tasks._pair_by_tasks_data_source


def test_configure_pairs_with_no_pairs_stops_every_pair():
    """A config read with no Active recurring_tasks table must stop the running pairs (no restart needed)."""
    recurring_tasks.configure_pairs([_make_pair("a" * 32, "b" * 32)], week_start_day=0, day_start_hour=3)
    assert recurring_tasks._rtd_pairs
    recurring_tasks.configure_pairs([], week_start_day=0, day_start_hour=3)
    assert recurring_tasks._rtd_pairs == [] and recurring_tasks._pair_by_tasks_data_source == {}


def test_configure_pairs_duplicate_tasks_db(caplog):
    with caplog.at_level(logging.ERROR, logger="recurring_tasks"):
        recurring_tasks.configure_pairs(
            [
                _make_pair("a" * 32, "b" * 32),
                _make_pair("a" * 32, "c" * 32),  # same tasks_data_source_id
            ],
            week_start_day=6,
            day_start_hour=3,
        )
    assert len(recurring_tasks._rtd_pairs) == 0
    assert "both disabled" in caplog.text.lower()


def test_configure_pairs_duplicate_defs_db(caplog):
    with caplog.at_level(logging.ERROR, logger="recurring_tasks"):
        recurring_tasks.configure_pairs(
            [
                _make_pair("a" * 32, "b" * 32),
                _make_pair("c" * 32, "b" * 32),  # same definitions_data_source_id
            ],
            week_start_day=6,
            day_start_hour=3,
        )
    assert len(recurring_tasks._rtd_pairs) == 0


def test_configure_pairs_two_valid_pairs():
    recurring_tasks.configure_pairs(
        [
            _make_pair("a" * 32, "b" * 32),
            _make_pair("c" * 32, "d" * 32),
        ],
        week_start_day=6,
        day_start_hour=3,
    )
    assert len(recurring_tasks._rtd_pairs) == 2
    assert "a" * 32 in recurring_tasks._pair_by_tasks_data_source
    assert "c" * 32 in recurring_tasks._pair_by_tasks_data_source
