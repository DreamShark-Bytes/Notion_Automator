"""
Tests for the bugs found by the user's manual testing on 2026-11-27 (STATUS.md "Manual test findings 2026-11-27").

Every test here describes behaviour the user asked for after hand testing on 2026-11-27. They were written first
and failed (`xfail(strict)`); each fix removed its `@bug` marker in the same change, so they are now ordinary
regression tests. Keep the `bug()` helper for the next batch of findings.

Spec used for the message format (Slice 6; the user accepted it on 2026-11-27):
  - a message is shown as "N. [tag] header: text", numbered from 1 separately in the Errors and Warnings columns;
  - the [tag] is colored (any color but the default);
  - the header (text between the tag and the first colon) is bold and underlined;
  - key words are bold: config key names (inherit_include_properties, ...) and quoted names ('Priority');
  - every message ends with a short "Fix: ..." sentence (wording not agreed yet, so not tested yet);
  - a message about a recurring_tasks block names that block by its definitions data source id in the header
    ("recurring_tasks <32-character id>: ..."), because a definitions data source is in exactly one block;
  - one message per problem, except where one check finds several names: those are ONE message naming all of them;
  - a column never holds more than 100 text objects, 1900 characters or 2000 characters in one object; what does not
    fit is replaced by a last line "(N more, see the log)", N being the exact number of messages left out.
"""
import logging
import os
import re

import pytest

import automations
import daemon
import hub
import recurring_tasks as rt
from helpers import fake_status_group
from tests.test_daemon_apply import _run_main
from tests.test_inheritance import _make_pair
from tests.test_hub_reader import (  # noqa: F401  (ready is a fixture)
    CFG, DEFS, DEFS2, FakeClient as HubFake, TASKS, TASKS2, TASKS_BLOCK, _client_with, _code, _row, ready,
)

ROOT = os.path.dirname(os.path.dirname(__file__))


def bug(reason):
    return pytest.mark.xfail(strict=True, reason=reason)


# ---- helpers: what the user would see in Notion ------------------------------------------

def _segments(client, column):
    """The rich_text segments of the last write to `column` of row r1."""
    for page_id, props in reversed(client.updates):
        if page_id == "r1" and column in props:
            return props[column]["rich_text"]
    raise AssertionError(f"nothing was written to {column}")


def _plain(segments):
    return "".join(s["text"]["content"] for s in segments)


def _chars(segments):
    """One (character, annotations) pair per character, so a test can ask about a phrase wherever it was split."""
    out = []
    for s in segments:
        ann = s.get("annotations", {})
        out += [(ch, ann) for ch in s["text"]["content"]]
    return out


def _annotations_of(segments, phrase):
    chars = _chars(segments)
    text = "".join(ch for ch, _ in chars)
    start = text.find(phrase)
    assert start >= 0, f"{phrase!r} is not in the written text {text!r}"
    return [ann for _, ann in chars[start:start + len(phrase)]]


def _show(column, messages, source="config"):
    """Report messages for the Automator's row, flush them, return the client."""
    client = _client_with((_row("r1"), TASKS_BLOCK))
    hub.report(source, **{column.lower(): messages})
    hub.flush(client)
    return client


# ============================ SLICE 6: message formatting ====================================

HEADER_MSG = "property_automations #1: unknown key 'colour' ignored."
EXCLUDE_MSG = 'recurring_tasks: inherit_exclude_properties has no effect unless inherit_include_properties = "all".'


@pytest.mark.parametrize("column", ["Errors", "Warnings"])
def test_messages_are_a_numbered_list_per_column(ready, column):
    client = _show(column, ["first problem: a.", "second problem: b.", "third problem: c."])
    lines = _plain(_segments(client, column)).split("\n")
    assert [l.split(" ", 1)[0] for l in lines] == ["1.", "2.", "3."]
    assert all(l.split(" ", 2)[1].startswith("[config]") for l in lines)


def test_each_column_is_numbered_from_one(ready):
    client = _client_with((_row("r1"), TASKS_BLOCK))
    hub.report("config", errors=["e one: a.", "e two: b."], warnings=["w one: c."])
    hub.flush(client)
    assert _plain(_segments(client, "Warnings")).startswith("1. [config]")
    assert _plain(_segments(client, "Errors")).startswith("1. [config]")


@pytest.mark.parametrize("column", ["Errors", "Warnings"])
def test_the_tag_is_colored(ready, column):
    client = _show(column, [HEADER_MSG])
    colors = {a.get("color", "default") for a in _annotations_of(_segments(client, column), "[config]")}
    assert colors and "default" not in colors


@pytest.mark.parametrize("column", ["Errors", "Warnings"])
def test_the_header_is_bold_and_underlined_and_the_text_after_it_is_not_underlined(ready, column):
    client = _show(column, [HEADER_MSG])
    segments = _segments(client, column)
    assert all(a.get("bold") and a.get("underline") for a in _annotations_of(segments, "property_automations #1"))
    assert not any(a.get("underline") for a in _annotations_of(segments, "unknown key"))


def test_config_key_names_in_a_message_are_bold_and_the_words_around_them_are_not(ready):
    client = _show("Warnings", [EXCLUDE_MSG])
    segments = _segments(client, "Warnings")
    for key in ("inherit_exclude_properties", "inherit_include_properties"):
        assert all(a.get("bold") for a in _annotations_of(segments, key)), f"{key} is not bold"
    assert not any(a.get("bold") for a in _annotations_of(segments, "has no effect unless"))


def test_quoted_names_in_a_message_are_bold(ready):
    client = _show("Warnings", ["recurring_tasks: inherit_include_properties: 'Priority' is not a property of the tasks data source."])
    assert all(a.get("bold") for a in _annotations_of(_segments(client, "Warnings"), "'Priority'"))


def test_the_real_exclude_without_all_warning_is_formatted(ready):
    toml = (f'[[recurring_tasks]]\ndefinitions_data_source_id = "{DEFS}"\ntasks_data_source_id = "{TASKS}"\n'
            'inherit_include_properties = ["Priority"]\ninherit_exclude_properties = ["Notes"]\n')
    client = _client_with((_row("r1"), toml))
    hub.read_config(client)
    hub.flush(client)
    segments = _segments(client, "Warnings")
    assert _plain(segments).startswith("1. [config] ")
    assert all(a.get("bold") for a in _annotations_of(segments, "inherit_exclude_properties"))


def test_a_link_in_a_message_is_still_a_link(ready):
    """Guard: formatting must not break the [label](url) links the recreation warning relies on. Passes today."""
    client = _show("Warnings", ["config row: restored, see [the old row](https://www.notion.so/abc)."])
    links = [s["text"].get("link", {}).get("url") for s in _segments(client, "Warnings") if s["text"].get("link")]
    assert links == ["https://www.notion.so/abc"]


def test_formatted_messages_are_not_rewritten_when_nothing_changed(ready):
    client = _show("Warnings", [HEADER_MSG])
    shown = _plain(_segments(client, "Warnings"))
    assert shown.startswith("1. ")                       # without this the test would pass for the wrong reason
    again = _client_with((_row("r1", warnings=shown), TASKS_BLOCK))
    hub.report("config", warnings=["something else: x."])
    hub.report("config", warnings=[HEADER_MSG])         # same text as before
    hub._dirty = True
    hub.flush(again)
    assert again.updates == []


# ============= SLICE 6: a warning about config.toml clears without a restart =================

class _Writable(HubFake):
    """A Hub fake whose writes show up in the rows it returns next time, like Notion."""
    def update_page_properties(self, page_id, properties, icon=None):
        for rows in self.rows.values():
            for row in rows:
                if row["id"] == page_id:
                    for column, value in properties.items():
                        if "rich_text" in value:
                            row["properties"][column] = {"type": "rich_text", "rich_text": [
                                {"plain_text": s["text"]["content"]} for s in value["rich_text"]]}
        return super().update_page_properties(page_id, properties, icon)


def _warnings_shown(fake):
    row = fake.rows[hub._normalize_uuid(CFG)][0]
    return "".join(s["plain_text"] for s in row["properties"]["Warnings"]["rich_text"])


def test_a_config_toml_warning_clears_after_the_key_is_removed_without_a_restart(monkeypatch, tmp_path):
    V1 = "2026-11-27T10:00:00.000+00:00"
    row = _row("r1", name="Automator")
    row["properties"][hub._VERSION_PROP] = {"type": "date", "date": {"start": V1}}
    row["properties"][hub._REPORT_PROP] = {"type": "rich_text", "rich_text": []}
    cfg_key = hub._normalize_uuid(CFG)
    fake = _Writable(
        rows={cfg_key: [row]}, bodies={"r1": [_code(TASKS_BLOCK)]},
        schemas={cfg_key: {"properties": {hub._VERSION_PROP: {"type": "date"}, hub._REPORT_PROP: {"type": "rich_text"},
                                          "Errors": {"type": "rich_text"}, "Warnings": {"type": "rich_text"},
                                          "Status": {"status": {"options": [{"name": "Active"}]}}}}})
    seen = []

    def user_removes_the_key_and_saves_the_file():
        seen.append(_warnings_shown(fake))
        (tmp_path / "config.toml").write_text(f'config_hub_data_source_id = "{CFG}"\n', encoding="utf-8")

    quiet = lambda: None
    _run_main(monkeypatch, tmp_path, fake, f'config_hub_data_source_id = "{CFG}"\nstatus_data_source_id = "{"9" * 32}"\n',
              [user_removes_the_key_and_saves_the_file, quiet, quiet, quiet])
    assert "status_data_source_id" in seen[0], "the warning was never shown, so this test proves nothing"
    assert "status_data_source_id" not in _warnings_shown(fake)


# ===== SLICE 6 (user decision 2026-11-27): group where one check finds several names; stay inside Notion's limits ====

def _warnings(problems):
    return [message for severity, message in problems if severity == "warning"]


def _block(extra):
    return f'[[property_automations]]\ndata_source_id = "{TASKS}"\n{extra}\n'


def test_several_unknown_keys_in_one_block_are_one_warning():
    _, _, problems = hub.parse_row_config(_block('colour = 1\nsize = 2'))
    found = _warnings(problems)
    assert len(found) == 1 and "colour" in found[0] and "size" in found[0]


def test_one_unknown_key_is_still_one_warning_that_names_it():
    """Guard: grouping must not lose the single-name case. Passes today."""
    _, _, problems = hub.parse_row_config(_block('colour = 1'))
    found = _warnings(problems)
    assert len(found) == 1 and "colour" in found[0]


def _inherit_messages(monkeypatch, include, exclude=()):
    pair = _make_pair(inherit_include_properties=list(include))
    pair["inherit_exclude_properties"] = list(exclude)
    pair["_task_types"] = {"Priority": "select", "Total": "formula", "Closed Date": "date"}
    monkeypatch.setattr(rt, "_rtd_pairs", [pair])
    return rt.inherit_warnings()


def test_names_that_are_not_properties_are_one_warning_per_setting(monkeypatch):
    messages = _inherit_messages(monkeypatch, ["Typo1", "Typo2", "Typo3"], ["Gone1", "Gone2"])
    assert len(messages) == 2
    include = next(m for m in messages if "inherit_include_properties" in m)
    exclude = next(m for m in messages if "inherit_exclude_properties" in m)
    assert all(name in include for name in ("Typo1", "Typo2", "Typo3")) and "Gone1" not in include
    assert all(name in exclude for name in ("Gone1", "Gone2"))


def test_names_with_different_problems_are_not_merged(monkeypatch):
    """Guard: a name that is not a property and a name that cannot be copied need different fixes, so two messages. Passes today."""
    messages = _inherit_messages(monkeypatch, ["Typo", "Total"])
    assert len(messages) == 2


def _shown(segments):
    """(numbered lines shown, the number left out or None) from what was written to a column."""
    lines = _plain(segments).split("\n")
    left_out = None
    match = re.fullmatch(r"\((\d+) more, see the log\)", lines[-1])
    if match:
        left_out, lines = int(match.group(1)), lines[:-1]
    return lines, left_out


@pytest.mark.parametrize("count, make", [
    (150, lambda i: f"check {i}: x."),                      # short: the 100-object cap is reached first
    (40, lambda i: f"check {i}: " + "y" * 100),            # long: the 1900-character cap is reached first
])
def test_a_long_list_is_cut_to_the_limits_and_says_how_many_were_left_out(ready, count, make):
    messages = [make(i) for i in range(count)]
    client = _show("Warnings", messages)
    segments = _segments(client, "Warnings")
    lines, left_out = _shown(segments)
    assert left_out is not None, "nothing was cut, so the limits were not reached or not enforced"
    assert len(segments) <= 100 and len(_plain(segments)) <= 1900
    assert all(len(s["text"]["content"]) <= 2000 for s in segments)
    assert [l.split(" ", 1)[0] for l in lines] == [f"{n}." for n in range(1, len(lines) + 1)]
    assert len(lines) + left_out == count


def test_one_huge_message_never_makes_an_invalid_write(ready):
    """Guard: a single message longer than any limit is shortened, not rejected by Notion. Passes today."""
    client = _show("Warnings", ["huge problem: " + "z" * 5000])
    segments = _segments(client, "Warnings")
    assert len(segments) <= 100 and len(_plain(segments)) <= 1900
    assert all(len(s["text"]["content"]) <= 2000 for s in segments)


# ============================ SLICE 7: example files are linked ================================

EXAMPLES = ["config_example.toml", "config_secrets_example.toml", os.path.join("docs", "config_row_example.toml")]
DOCS = ["README.md"] + [os.path.join("docs", f) for f in sorted(os.listdir(os.path.join(ROOT, "docs"))) if f.endswith(".md")]
LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def _doc_text(doc):
    """The doc with fenced code blocks blanked out (a shell command that names a file is not a mention to link)."""
    with open(os.path.join(ROOT, doc), encoding="utf-8") as f:
        text = f.read()
    return re.sub(r"```.*?```", lambda m: " " * len(m.group(0)), text, flags=re.S)


def _link_problems(doc):
    text = _doc_text(doc)
    spans = [m.span() for m in LINK.finditer(text)]
    problems = []
    for example in EXAMPLES:
        name = os.path.basename(example)
        for m in re.finditer(re.escape(name), text):
            if not any(start <= m.start() < end for start, end in spans):
                problems.append(f"'{name}' is not a link (offset {m.start()})")
    for m in LINK.finditer(text):
        label, target = m.group(1), m.group(2)
        for example in EXAMPLES:
            if os.path.basename(target.split("#")[0]) != os.path.basename(example):
                continue
            opened = os.path.normpath(os.path.join(ROOT, os.path.dirname(doc), target.split("#")[0]))
            if opened != os.path.join(ROOT, example):
                problems.append(f"link '{target}' does not open {example}")
            if example.replace(os.sep, "/") not in label:
                problems.append(f"link text '{label}' does not show the location {example}")
    return [f"{doc}: {p}" for p in problems]


def test_every_mention_of_an_example_file_is_a_link_that_opens_it_and_shows_where_it_is():
    problems = [p for doc in DOCS for p in _link_problems(doc)]
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("doc", ["README.md", os.path.join("docs", "config_reference.md")])
def test_the_readme_and_the_reference_link_every_example_file(doc):
    opened = {os.path.normpath(os.path.join(ROOT, os.path.dirname(doc), m.group(2).split("#")[0]))
              for m in LINK.finditer(_doc_text(doc))}
    missing = [e for e in EXAMPLES if os.path.join(ROOT, e) not in opened]
    assert not missing, f"{doc} has no link to: {missing}"


def test_the_example_files_exist():
    """Guard for the two tests above: a missing example would make them fail for the wrong reason. Passes today."""
    for example in EXAMPLES:
        assert os.path.isfile(os.path.join(ROOT, example)), example


# ============ OTHER 2: the first value of Closed Date is stamped on the first pass ===========

DS = "d" * 32


def _closing_page(status, closed=None):
    def date(value):
        return {"type": "date", "date": {"start": value, "end": None} if value else None}
    return {
        "id": "page-1", "parent": {"data_source_id": DS},
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": "Task"}]},
            "Status": {"type": "status", "status": {"id": f"{status.lower()}-id", "name": status}},
            "Closed Date": date(closed),
            "First Closed Date": date(None),
        },
    }


@pytest.fixture
def closing(monkeypatch):
    from unittest.mock import MagicMock
    monkeypatch.setattr(automations, "_data_source_configs", {DS: {
        "closed_date_field": "Closed Date", "reopen_count_field": None,
        "first_value_fields": ["Closed Date"], "update_count_fields": []}})
    monkeypatch.setattr(automations, "_schema_cache", {DS: {
        "Status": "status", "Closed Date": "date", "First Closed Date": "date"}})
    monkeypatch.setattr(automations, "_get_status_group", fake_status_group)
    client = MagicMock()
    client.update_page_properties.return_value = {}
    return client


def test_first_closed_date_is_stamped_in_the_same_write_as_closed_date(closing):
    daemon.run_automations_on_page(closing, _closing_page("Done"), _closing_page("In progress"))
    assert closing.update_page_properties.call_count == 1, "expected one write holding both properties"
    written = closing.update_page_properties.call_args.args[1]
    assert "Closed Date" in written                       # without this the test would pass for the wrong reason
    assert written.get("First Closed Date") == written["Closed Date"]


def test_a_closed_date_the_user_filled_in_is_copied_to_first_closed_date_on_the_first_pass(closing):
    """Guard: the already-working case. Passes today."""
    daemon.run_automations_on_page(closing, _closing_page("Done", closed="2026-11-26"), _closing_page("In progress", closed="2026-11-26"))
    written = closing.update_page_properties.call_args.args[1]
    assert written["First Closed Date"]["date"]["start"] == "2026-11-26"


# ===== Found while reading the code: a stray entry in the AUTOMATIONS list (commit 8e48189) ======

def test_running_the_automations_on_a_page_logs_no_automation_failure(closing, caplog):
    with caplog.at_level(logging.ERROR):
        daemon.run_automations_on_page(closing, _closing_page("In progress"), _closing_page("In progress"))
    assert [r.getMessage() for r in caplog.records if "failed on page" in r.getMessage()] == []


# ============ OTHER 3: [[recurring_tasks]] works like [[property_automations]] ===============

def _rtd(defs, tasks, extra=""):
    return f'[[recurring_tasks]]\ndefinitions_data_source_id = "{defs}"\ntasks_data_source_id = "{tasks}"\n{extra}\n'


def _error_text(client):
    return "".join(_plain(props["Errors"]["rich_text"]) for page_id, props in client.updates if page_id == "r1" and "Errors" in props)


def test_one_recurring_tasks_block_in_double_brackets_makes_one_pair(ready):
    client = _client_with((_row("r1"), _rtd(DEFS, TASKS)))
    cfg = hub.read_config(client)
    assert cfg is not None
    assert [(p["definitions_data_source_id"], p["tasks_data_source_id"]) for p in cfg["rtd_pairs"]] == [(DEFS, TASKS)]
    hub.flush(client)
    assert _error_text(client) == ""


def test_two_recurring_tasks_blocks_in_one_row_make_two_pairs_with_their_own_settings(ready):
    body = _rtd(DEFS, TASKS, 'non_completion_statuses = ["Skipped"]') + _rtd(DEFS2, TASKS2, 'inherit_include_properties = "all"')
    client = _client_with((_row("r1"), body))
    cfg = hub.read_config(client)
    assert cfg is not None and len(cfg["rtd_pairs"]) == 2
    first, second = cfg["rtd_pairs"]
    assert (first["tasks_data_source_id"], first["non_completion_statuses"]) == (TASKS, ["Skipped"])
    assert (second["tasks_data_source_id"], second["inherit_include_properties"]) == (TASKS2, "all")
    hub.flush(client)
    assert _error_text(client) == ""


def test_two_blocks_with_the_same_tasks_data_source_are_both_disabled_with_an_error(ready):
    client = _client_with((_row("r1"), _rtd(DEFS, TASKS) + _rtd(DEFS2, TASKS)))
    cfg = hub.read_config(client)
    assert cfg is not None
    assert cfg["rtd_pairs"] == []
    hub.flush(client)
    assert "same tasks data source" in _error_text(client)


def test_the_old_single_recurring_tasks_table_is_an_error_and_does_not_run(ready):
    old = _rtd(DEFS, TASKS).replace("[[recurring_tasks]]", "[recurring_tasks]")
    client = _client_with((_row("r1"), TASKS_BLOCK + "\n" + old))
    cfg = hub.read_config(client)
    assert cfg is not None and cfg["rtd_pairs"] == []
    hub.flush(client)
    assert "[[recurring_tasks]]" in _error_text(client)


# ===== SLICE 6 (user decision 2026-11-27): recurring_tasks messages are headed by the definitions data source id =====

DEFS3, TASKS3 = "e" * 32, "f" * 32


def _header(message):
    return message.split(":", 1)[0]


def test_inherit_warnings_name_the_pair_by_its_definitions_data_source_id(monkeypatch):
    pairs = []
    for defs, tasks in ((DEFS, TASKS), (DEFS2, TASKS2)):
        pair = _make_pair(inherit_include_properties=["Typo"])
        pair["definitions_data_source_id"], pair["tasks_data_source_id"] = defs, tasks
        pair["_task_types"] = {"Priority": "select"}
        pairs.append(pair)
    monkeypatch.setattr(rt, "_rtd_pairs", pairs)
    messages = rt.inherit_warnings()
    assert len(messages) == 2
    for defs, other in ((DEFS, DEFS2), (DEFS2, DEFS)):
        mine = next(m for m in messages if defs in _header(m))
        assert other not in _header(mine)


def test_parse_messages_name_each_block_by_its_definitions_data_source_id():
    body = _rtd(DEFS, TASKS, "colour = 1") + _rtd(DEFS2, TASKS2, "size = 2")
    _, _, problems = hub.parse_row_config(body)
    warnings = _warnings(problems)
    assert len(warnings) == 2
    colour = next(m for m in warnings if "colour" in m)
    size = next(m for m in warnings if "size" in m)
    assert DEFS in _header(colour) and DEFS2 not in _header(colour)
    assert DEFS2 in _header(size) and DEFS not in _header(size)


# ===== OTHER 3 (user decision 2026-11-27): a definitions id is in one block only, a tasks id is in one block only ====

def test_two_blocks_with_the_same_definitions_data_source_are_both_disabled_and_a_third_block_survives(ready):
    body = _rtd(DEFS, TASKS) + _rtd(DEFS, TASKS2) + _rtd(DEFS3, TASKS3)
    client = _client_with((_row("r1"), body))
    cfg = hub.read_config(client)
    assert cfg is not None
    assert [p["definitions_data_source_id"] for p in cfg["rtd_pairs"]] == [DEFS3]
    hub.flush(client)
    assert "same definitions data source" in _error_text(client)


def test_two_blocks_with_the_same_tasks_data_source_leave_a_third_block_running(ready):
    body = _rtd(DEFS, TASKS) + _rtd(DEFS2, TASKS) + _rtd(DEFS3, TASKS3)
    cfg = hub.read_config(_client_with((_row("r1"), body)))
    assert cfg is not None
    assert [p["tasks_data_source_id"] for p in cfg["rtd_pairs"]] == [TASKS3]


def test_the_same_definitions_data_source_in_two_rows_disables_both_pairs(ready):
    """Guard: the cross-row rule already exists for definitions (and for tasks, in test_hub_reader). Passes today."""
    second, first = _rtd(DEFS, TASKS2), _rtd(DEFS, TASKS)
    client = _client_with((_row("r1", name="A"), first), (_row("r2", name="B"), second))
    cfg = hub.read_config(client)
    assert cfg["rtd_pairs"] == []
    hub.flush(client)
    assert any("same definitions data source" in _plain(props["Errors"]["rich_text"])
               for page_id, props in client.updates if "Errors" in props)


def test_one_block_with_the_same_data_source_as_definitions_and_tasks_is_disabled(ready):
    client = _client_with((_row("r1"), _rtd(DEFS, DEFS) + _rtd(DEFS3, TASKS3)))
    cfg = hub.read_config(client)
    assert cfg is not None
    assert [p["definitions_data_source_id"] for p in cfg["rtd_pairs"]] == [DEFS3]
    hub.flush(client)
    assert DEFS in _error_text(client)


def test_a_data_source_that_is_definitions_in_one_block_and_tasks_in_another_disables_both(ready):
    client = _client_with((_row("r1"), _rtd(DEFS, TASKS) + _rtd(DEFS2, DEFS) + _rtd(DEFS3, TASKS3)))
    cfg = hub.read_config(client)
    assert cfg is not None
    assert [p["definitions_data_source_id"] for p in cfg["rtd_pairs"]] == [DEFS3]
    hub.flush(client)
    assert DEFS in _error_text(client)


def test_the_stored_config_file_accepts_two_recurring_tasks_blocks():
    with open(hub.STORED_CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(_rtd(DEFS, TASKS) + _rtd(DEFS2, TASKS2))
    cfg = hub.load_stored_config(hub_configured=False)
    assert cfg is not None
    assert [p["tasks_data_source_id"] for p in cfg["rtd_pairs"]] == [TASKS, TASKS2]
