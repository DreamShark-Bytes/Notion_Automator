"""Offline tests for tests/live_sandbox.py: its refusals, its cleanup rules and a full run against an in-memory fake Notion.
The real script connects to Notion only when the user runs it by hand."""
import itertools

import pytest

import hub
import live_sandbox as sb

PAGE = "a" * 32   # the sandbox page; the fake's own IDs below count up from 1 so they never collide with it


# ---- settings ---------------------------------------------------------------------

def _write(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return str(path)


def test_load_settings_returns_the_page_and_token(tmp_path):
    cfg = _write(tmp_path / "config.toml", f'sandbox_page_id = "{PAGE}"\ntoken = "normal-token"\n')
    sec = _write(tmp_path / "config_secrets.toml", 'sandbox_token = "sandbox-token"\n')
    _, page_id, token = sb.load_settings(cfg, sec)
    assert page_id == hub._normalize_uuid(PAGE) and token == "sandbox-token"


@pytest.mark.parametrize("config_text, secrets_text, needle", [
    ('token = "t"\n', 'sandbox_token = "s"\n', "sandbox_page_id"),
    (f'sandbox_page_id = "{PAGE}"\n', 'token = "t"\n', "sandbox_token"),
    (f'sandbox_page_id = "{PAGE}"\n', 'token = "same-secret-value"\nsandbox_token = "same-secret-value"\n', "equals the normal token"),
])
def test_load_settings_refuses_and_never_prints_a_token(tmp_path, config_text, secrets_text, needle):
    cfg = _write(tmp_path / "config.toml", config_text)
    sec = _write(tmp_path / "config_secrets.toml", secrets_text)
    with pytest.raises(sb.SandboxRefused) as err:
        sb.load_settings(cfg, sec)
    assert needle in str(err.value) and "same-secret-value" not in str(err.value)


# ---- an in-memory Notion ----------------------------------------------------------

class FakeNotion:
    """Just enough of NotionClient for the sandbox script: databases, pages, code blocks, trash."""
    notion_version = "2026-03-11"

    def __init__(self):
        self.ids = itertools.count(1)
        self.clock = itertools.count(1)
        self.schemas, self.pages, self.blocks, self.trashed = {}, {}, {}, set()   # pages: id -> {"ds", "properties"}
        self.blocks[PAGE] = [{"id": "c0ffee", "type": "callout", "callout": {"rich_text": []}}]
        self.touched = []   # ids of every block, page or database written or trashed

    def _new(self, prefix):
        return f"{next(self.ids):032x}"   # a valid Notion ID, as the Hub checks IDs; real Notion accepts them with or without dashes

    # databases
    def create_database(self, parent_page_id, title, properties, **kw):
        assert parent_page_id == PAGE
        db_id, schema = self._new("db"), {}
        for name, definition in properties.items():
            kind = next(iter(definition))
            if kind == "status":
                options = definition["status"]["options"]
                opts = [{"id": f"{db_id}-o{i}", "name": o["name"]} for i, o in enumerate(options)]
                groups = {}
                for opt, req in zip(opts, options):
                    groups.setdefault(req["group"], []).append(opt["id"])
                schema[name] = {"type": "status", "status": {
                    "options": opts, "groups": [{"name": g, "option_ids": ids} for g, ids in groups.items()],
                    "default_option": options[0]["name"]}}
            else:
                schema[name] = {"type": kind}
        self.schemas[db_id] = schema
        self.touched.append(db_id)
        return {"id": db_id}

    def get_schema(self, any_id):
        return {"properties": self.schemas[any_id.replace("-", "")]}

    # pages
    def _response_properties(self, request):
        out = {}
        for name, value in request.items():
            if "title" in value:
                out[name] = {"title": [{"plain_text": t["text"]["content"]} for t in value["title"]]}
            elif "rich_text" in value:
                out[name] = {"rich_text": [{"plain_text": t["text"]["content"]} for t in value["rich_text"]]}
            elif "status" in value:
                out[name] = {"status": {"name": value["status"]["name"]}}
            elif "date" in value:
                out[name] = {"date": {"start": value["date"]["start"], "end": None, "time_zone": None}}
            else:
                out[name] = value
            out[name]["type"] = next(iter(value))   # real responses carry the property type
        return out

    def create_page(self, ds_id, properties, icon=None, template=None):
        page_id = self._new("page")
        ds = ds_id.replace("-", "")
        empty = {"title": [], "rich_text": [], "status": None, "date": None}   # real rows list every column, set or not
        props = {name: {"type": d["type"], d["type"]: empty[d["type"]]} for name, d in self.schemas[ds].items()}
        props.update(self._response_properties(properties))
        self.pages[page_id] = {"ds": ds, "properties": props, "edited": next(self.clock)}
        self.blocks[page_id] = []
        self.touched.append(page_id)
        return self._page(page_id)

    def _page(self, page_id):
        p = self.pages[page_id]
        return {"id": page_id, "last_edited_time": f"t{p['edited']:05d}", "properties": p["properties"]}

    def update_page_properties(self, page_id, properties, icon=None):
        page_id = page_id.replace("-", "")
        page = self.pages[page_id]
        page["properties"].update(self._response_properties(properties))
        page["edited"] = next(self.clock)
        self.touched.append(page_id)
        return self._page(page_id)

    def query_database(self, ds_id, filter_payload=None):
        return [self._page(pid) for pid, p in self.pages.items() if p["ds"] == ds_id.replace("-", "") and pid not in self.trashed]

    def archive_page(self, page_id):
        self.trashed.add(page_id)
        self.touched.append(page_id)

    # blocks
    def append_blocks(self, block_id, children):
        block_id = block_id.replace("-", "")
        for child in children:
            block = {"id": self._new("blk"), "type": child["type"], child["type"]: {
                **child[child["type"]],
                "rich_text": [{"plain_text": t["text"]["content"]} for t in child[child["type"]]["rich_text"]]}}
            self.blocks[block_id].append(block)
        self.touched.append(block_id)
        return {}

    def get_blocks(self, block_id):
        return list(self.blocks[block_id.replace("-", "")])

    def _patch(self, path, body):
        kind, object_id = path.strip("/").split("/")
        self.touched.append(object_id)
        if kind == "databases":
            assert body in ({"in_trash": True}, {"archived": True})
            self.trashed.add(object_id)
        elif kind == "blocks":
            for blocks in self.blocks.values():
                for block in blocks:
                    if block["id"] == object_id:
                        block["code"]["rich_text"] = [{"plain_text": t["text"]["content"]} for t in body["code"]["rich_text"]]
        else:
            raise AssertionError(path)


# ---- the script's own rules -------------------------------------------------------

def test_callout_warning_only_when_the_first_block_is_not_a_callout():
    notion = FakeNotion()
    assert sb.callout_warning(notion, PAGE) is None
    notion.blocks[PAGE].insert(0, {"id": "p1", "type": "paragraph"})
    assert "not a callout" in sb.callout_warning(notion, PAGE)
    notion.blocks[PAGE] = []
    assert "not a callout" in sb.callout_warning(notion, PAGE)


def test_cleanup_trashes_only_what_was_created_and_keeps_going_after_a_failure():
    notion = FakeNotion()
    created = []
    db_id = sb.create_data_source(notion, PAGE, "Throwaway", {"Name": {"title": {}}}, created)
    page = notion.create_page(db_id, {"Name": {"title": [{"text": {"content": "x"}}]}})
    created += [("page", "does-not-exist"), ("page", page["id"])]
    notion.archive_page = lambda pid, _orig=notion.archive_page: (_ for _ in ()).throw(RuntimeError("boom")) if pid == "does-not-exist" else _orig(pid)
    failures = sb.cleanup(notion, created)
    assert len(failures) == 1 and "does-not-exist" in failures[0]
    assert notion.trashed == {db_id, page["id"]}
    assert "c0ffee" not in notion.touched and notion.blocks[PAGE][0]["type"] == "callout"


# ---- a whole run -------------------------------------------------------------------

def test_a_full_run_passes_and_leaves_the_sandbox_page_as_it_found_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hub, "HUB_CONFIG_PATH", str(tmp_path / "real_state_file.toml"))
    notion = FakeNotion()
    failed = sb.run(notion, PAGE)
    out = capsys.readouterr().out
    assert failed == 0, out
    assert "0 failed" in out and "WARNING" not in out
    # Everything the run created is in the trash; the callout and the page's own block list are untouched.
    created_dbs = set(notion.schemas)
    assert created_dbs and created_dbs <= notion.trashed
    assert notion.blocks[PAGE] == [{"id": "c0ffee", "type": "callout", "callout": {"rich_text": []}}]
    assert "c0ffee" not in notion.touched
    # The real hub state file path was restored, and the run never wrote to it.
    assert hub.HUB_CONFIG_PATH == str(tmp_path / "real_state_file.toml")
    assert not (tmp_path / "real_state_file.toml").exists()


def test_a_run_warns_when_the_callout_is_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hub, "HUB_CONFIG_PATH", str(tmp_path / "state.toml"))
    notion = FakeNotion()
    notion.blocks[PAGE] = []
    sb.run(notion, PAGE)
    assert "WARNING  The first block of the sandbox page is not a callout" in capsys.readouterr().out
