"""Tests for hub.py pure-function helpers: _parse_csv, _is_valid_db_id, and duplicate pair detection."""
import sys
import os
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import hub


def test_parse_csv_basic():
    assert hub._parse_csv("A, B, C") == ["A", "B", "C"]


def test_parse_csv_whitespace():
    assert hub._parse_csv("  A ,  B ,C  ") == ["A", "B", "C"]


def test_parse_csv_empty_string():
    assert hub._parse_csv("") == []


def test_parse_csv_blank_whitespace():
    assert hub._parse_csv("   ") == []


def test_parse_csv_single_token():
    assert hub._parse_csv("Priority") == ["Priority"]


def test_parse_csv_trailing_comma():
    assert hub._parse_csv("A, B,") == ["A", "B"]


def test_parse_csv_quoted_with_comma():
    assert hub._parse_csv('"Field, A", B') == ["Field, A", "B"]


def test_parse_csv_quoted_multiple():
    assert hub._parse_csv('"One, Two", "Three, Four", Five') == ["One, Two", "Three, Four", "Five"]


def test_is_valid_db_id_plain_32_hex():
    assert hub._is_valid_db_id("a" * 32) is True


def test_is_valid_db_id_upper_hex():
    assert hub._is_valid_db_id("A" * 32) is True


def test_is_valid_db_id_with_dashes():
    # Notion IDs are often formatted as 8-4-4-4-12
    notion_id = "12345678-1234-1234-1234-123456789abc"
    assert hub._is_valid_db_id(notion_id) is True


def test_is_valid_db_id_too_short():
    assert hub._is_valid_db_id("a" * 31) is False


def test_is_valid_db_id_too_long():
    assert hub._is_valid_db_id("a" * 33) is False


def test_is_valid_db_id_non_hex():
    assert hub._is_valid_db_id("g" * 32) is False


def test_is_valid_db_id_empty():
    assert hub._is_valid_db_id("") is False


def test_is_valid_db_id_none():
    assert hub._is_valid_db_id(None) is False


# ------------------------------------------------------------------ #
#  Duplicate pair detection via configure_pairs in recurring_tasks
# ------------------------------------------------------------------ #

import recurring_tasks


def _make_pair(tasks_id: str, defs_id: str) -> dict:
    return {
        "definitions_db_id": defs_id,
        "tasks_db_id":       tasks_id,
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
    assert "a" * 32 in recurring_tasks._pair_by_tasks_db


def test_configure_pairs_duplicate_tasks_db(caplog):
    with caplog.at_level(logging.ERROR, logger="recurring_tasks"):
        recurring_tasks.configure_pairs(
            [
                _make_pair("a" * 32, "b" * 32),
                _make_pair("a" * 32, "c" * 32),  # same tasks_db_id
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
                _make_pair("c" * 32, "b" * 32),  # same definitions_db_id
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
    assert "a" * 32 in recurring_tasks._pair_by_tasks_db
    assert "c" * 32 in recurring_tasks._pair_by_tasks_db
