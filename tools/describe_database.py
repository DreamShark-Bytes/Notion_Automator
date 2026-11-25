"""
describe_database.py

Read-only: prints every property of a Notion data source (name, property ID, type,
and type-specific details such as select options or relation target) so you can
see the property IDs the Notion UI does not show.

Accepts a data source ID or a database ID (a database with exactly one data source).
Uses the Notion API version from config.toml `notion_version` (default 2026-03-11);
on 2022-06-28 only database IDs work. See Notion's upgrade guides:
https://developers.notion.com/guides/get-started/upgrade-guide-2025-09-03
https://developers.notion.com/guides/get-started/upgrade-guide-2026-03-11

Usage:
    venv/bin/python tools/describe_database.py <id>
    venv/bin/python tools/describe_database.py <id> --json
    venv/bin/python tools/describe_database.py <id> --config my.toml
"""

import sys
import json
import argparse
from pathlib import Path

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        sys.exit("Python < 3.11 detected: install tomli with 'pip install tomli'")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_FORMULA_PREVIEW = 80


def _details(prop: dict) -> str:
    """Type-specific detail text for one property definition."""
    ptype = prop.get("type", "")
    body = prop.get(ptype) or {}
    if ptype in ("select", "multi_select", "status"):
        names = [o.get("name", "") for o in body.get("options", [])]
        return ", ".join(names)
    if ptype == "relation":
        ds_id = body.get("data_source_id", "")
        db_id = body.get("database_id", "")
        kind = body.get("type", "")
        synced = (body.get(kind) or {}).get("synced_property_name") if kind else None
        if ds_id and db_id:
            text = f"-> data source {ds_id} (database {db_id})"
        elif ds_id:
            text = f"-> data source {ds_id}"
        elif db_id:
            text = f"-> database {db_id}"
        else:
            text = ""
        if synced:
            text += f" (reverse property: {synced})"
        return text
    if ptype == "rollup":
        return (
            f"{body.get('function', '')} of {body.get('rollup_property_name', '')} "
            f"via {body.get('relation_property_name', '')}"
        ).strip()
    if ptype == "formula":
        expr = body.get("expression", "")
        return expr if len(expr) <= _FORMULA_PREVIEW else expr[:_FORMULA_PREVIEW] + "..."
    return ""


def describe_properties(db: dict) -> list[dict]:
    """Return one row per property: name, id, type, details. Sorted by name, case-insensitive."""
    rows = []
    for name, prop in (db.get("properties") or {}).items():
        rows.append({
            "name": name,
            "id": prop.get("id", ""),
            "type": prop.get("type", ""),
            "details": _details(prop),
        })
    return sorted(rows, key=lambda r: r["name"].lower())


def database_title(db: dict) -> str:
    return "".join(t.get("plain_text", "") for t in db.get("title", [])) or "(untitled)"


def header_line(source: dict, property_count: int) -> str:
    """One line naming what was described, with its own ID (the data source ID on 2025-09-03+)."""
    kind = source.get("object") or "source"
    sid = source.get("id", "")
    return f"{kind}: {database_title(source)}  id={sid}  ({property_count} properties)"


def format_table(rows: list[dict]) -> str:
    """Plain-text table with aligned columns."""
    headers = ("Property", "Property ID", "Type", "Details")
    keys = ("name", "id", "type", "details")
    widths = [max(len(h), *(len(r[k]) for r in rows)) if rows else len(h) for h, k in zip(headers, keys)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    for r in rows:
        lines.append("  ".join(r[k].ljust(w) for k, w in zip(keys, widths)).rstrip())
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Print a Notion data source's properties with their IDs")
    parser.add_argument("id", help="data source ID or database ID")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args()

    with open(args.config, "rb") as f:
        cfg = tomllib.load(f)
    from notion_api import NotionClient
    from hub import resolve_notion_version, resolve_token, TOKEN_ENV_VAR

    token = resolve_token(cfg)
    if not token:
        sys.exit(f"No Notion token: set the {TOKEN_ENV_VAR} environment variable.")

    client = NotionClient(token, notion_version=resolve_notion_version(cfg.get("notion_version")))
    source = client.get_schema(args.id.replace("-", ""))
    rows = describe_properties(source)
    if args.json:
        print(json.dumps(
            {"object": source.get("object"), "id": source.get("id"), "title": database_title(source), "properties": rows},
            indent=2, ensure_ascii=False,
        ))
    else:
        print(header_line(source, len(rows)) + "\n")
        print(format_table(rows))


if __name__ == "__main__":
    main()
