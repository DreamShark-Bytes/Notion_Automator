"""Tests for the recurring-task copy settings: inherit_include_properties, inherit_exclude_properties, inherit_icon."""
import hub
import recurring_tasks as rt
from test_inheritance import _make_pair, _make_task

DEFS, TASKS = "3" * 32, "1" * 32


def _parse(extra):
    text = f'[[recurring_tasks]]\ndefinitions_data_source_id = "{DEFS}"\ntasks_data_source_id = "{TASKS}"\n{extra}\n'
    return hub.parse_row_config(text)


def _pair_of(extra):
    _, recurring, problems = _parse(extra)
    return (recurring[0] if recurring else None), problems


# ---- parsing ---------------------------------------------------------------------

def test_defaults_copy_nothing_and_keep_the_icon():
    recurring, problems = _pair_of("")
    assert recurring["inherit_include_properties"] == [] and recurring["inherit_exclude_properties"] == []
    assert recurring["inherit_icon"] is True and problems == []


def test_the_string_all_is_accepted_and_a_list_named_all_is_just_a_name():
    assert _pair_of('inherit_include_properties = "all"')[0]["inherit_include_properties"] == "all"
    recurring, problems = _pair_of('inherit_include_properties = ["all"]')
    assert recurring["inherit_include_properties"] == ["all"] and problems == []


def test_a_bare_string_other_than_all_is_an_error_that_shows_the_array_form():
    recurring, problems = _pair_of('inherit_include_properties = "Priority"')
    assert recurring["inherit_include_properties"] == []
    (severity, text), = problems
    assert severity == "error" and '["Priority"]' in text and '"all"' in text


def test_exclude_needs_all_and_must_be_a_list():
    assert [s for s, _ in _pair_of('inherit_include_properties = ["A"]\ninherit_exclude_properties = ["B"]')[1]] == ["warning"]
    assert _pair_of('inherit_include_properties = "all"\ninherit_exclude_properties = ["B"]')[1] == []
    _, problems = _pair_of('inherit_include_properties = "all"\ninherit_exclude_properties = "all"')
    assert [s for s, _ in problems] == ["error"]


def test_inherit_icon_must_be_true_or_false_and_false_is_kept():
    assert _pair_of("inherit_icon = false")[0]["inherit_icon"] is False
    recurring, problems = _pair_of('inherit_icon = "no"')
    assert recurring["inherit_icon"] is True and [s for s, _ in problems] == ["error"]


def test_the_old_key_is_an_unknown_key_warning_that_names_the_new_one():
    _, problems = _pair_of('inherited_properties = ["Priority"]')
    assert any("renamed to inherit_include_properties" in text for _, text in problems)


# ---- copying ---------------------------------------------------------------------

def _task():
    task = _make_task(Priority="High", Effort="Low", Notes="n")
    task["properties"]["Name"] = {"type": "title", "title": []}
    task["properties"]["Total"] = {"type": "formula", "formula": {}}
    task["properties"]["Closed Date"] = {"type": "date", "date": None}
    return task


def test_all_copies_every_copyable_property_except_the_fixed_and_excluded_ones():
    pair = _make_pair(inherit_include_properties="all")
    assert set(rt._copy_task_fields(_task(), pair)) == {"Priority", "Effort", "Notes"}
    pair["inherit_exclude_properties"] = ["Notes"]
    assert set(rt._copy_task_fields(_task(), pair)) == {"Priority", "Effort"}


def test_the_exclude_list_means_nothing_for_a_list():
    pair = _make_pair(inherit_include_properties=["Priority", "Notes"])
    pair["inherit_exclude_properties"] = ["Notes"]
    assert set(rt._copy_task_fields(_task(), pair)) == {"Priority", "Notes"}


def test_configure_pairs_carries_the_settings():
    rt.configure_pairs([{"definitions_data_source_id": DEFS, "tasks_data_source_id": TASKS, "inherit_include_properties": "all",
                         "inherit_exclude_properties": ["Notes"], "inherit_icon": False}], 0, 3)
    pair = rt._rtd_pairs[0]
    assert (pair["inherit_include_properties"], pair["inherit_exclude_properties"], pair["inherit_icon"]) == ("all", ["Notes"], False)
    rt.configure_pairs([], 0, 3)


# ---- warnings against the live schema ---------------------------------------------

def test_inherit_warnings_name_missing_unwritable_and_managed_properties(monkeypatch):
    pair = _make_pair(inherit_include_properties=["Priority", "Typo", "Total", "Status", "all"])
    pair["inherit_exclude_properties"] = ["Gone"]
    pair["_task_types"] = {"Priority": "select", "Total": "formula", "Status": "status", "Closed Date": "date"}
    monkeypatch.setattr(rt, "_rtd_pairs", [pair])
    text = "\n".join(rt.inherit_warnings())
    assert "'Typo', 'all' are not properties" in text and "'Total' cannot be copied (a formula property" in text
    assert "'Status' cannot be copied (managed by the bot)" in text and "'Gone' is not a property" in text
    assert 'use the string "all" without brackets' in text and "'Priority'" not in text


def test_inherit_warnings_skip_pairs_whose_schema_is_not_loaded(monkeypatch):
    monkeypatch.setattr(rt, "_rtd_pairs", [_make_pair(inherit_include_properties=["Typo"])])
    assert rt.inherit_warnings() == []
