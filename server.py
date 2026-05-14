"""Aurora 4x MCP server — read-only, query-registry-driven."""

import json
import os
import re
import sqlite3
import sys
from datetime import date
from typing import Any

from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
import mcp.types as mcp_types

from jump_network import (
    tool_systems_near,
    tool_refresh_jump_network,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
QUERIES_PATH = os.path.join(_SCRIPT_DIR, "queries.json")
DB_PATH = os.environ.get("AURORA_DB_PATH", os.path.join(_SCRIPT_DIR, "Aurora.db"))
MAX_ROWS = int(os.environ.get("AURORA_MAX_ROWS", "200"))

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_queries: dict = {}          # reloaded from disk on every list_tools / call_tool
_session_ctx: dict | None = None   # {game_id, race_id} after first bootstrap

# ---------------------------------------------------------------------------
# queries.json loader — called on every request, not just startup
# ---------------------------------------------------------------------------

def load_queries(warn: bool = False) -> dict:
    with open(QUERIES_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    if warn:
        schema_keys = set(data.get("schema", {}).keys())
        for name, q in data.get("queries", {}).items():
            for table in q.get("tables_used", []):
                if table not in schema_keys:
                    print(
                        f"WARN: query '{name}' references unknown table '{table}'",
                        file=sys.stderr,
                    )
    return data


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=0)
    conn.row_factory = sqlite3.Row
    return conn


def bootstrap_context(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT MAX(GameID) AS gid FROM FCT_Game WHERE GameName != 'Sample'"
    ).fetchone()
    if row is None or row["gid"] is None:
        raise RuntimeError(
            "No active campaign found in database. "
            "Confirm DB path and that a game has been started."
        )
    game_id = row["gid"]

    row = conn.execute(
        "SELECT MIN(RaceID) AS rid FROM FCT_Race WHERE GameID = :game_id AND NPR = 0",
        {"game_id": game_id},
    ).fetchone()
    if row is None or row["rid"] is None:
        raise RuntimeError(
            f"No player race found for GameID {game_id}. "
            "Confirm NPR column name and that a player race exists."
        )
    return {"game_id": game_id, "race_id": row["rid"]}


def ensure_context() -> dict:
    global _session_ctx
    if _session_ctx is not None:
        return _session_ctx
    conn = get_db()
    try:
        _session_ctx = bootstrap_context(conn)
    finally:
        conn.close()
    return _session_ctx


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def rows_to_markdown(columns: list[str], rows: list) -> str:
    if not rows:
        return "No results."
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    body = "\n".join(
        "| " + " | ".join(str(cell) if cell is not None else "" for cell in row) + " |"
        for row in rows
    )
    return "\n".join([header, sep, body])


# ---------------------------------------------------------------------------
# Safety checks
# ---------------------------------------------------------------------------

def is_unsafe_table(name: str) -> bool:
    unsafe = set(_queries.get("unsafe_tables", []))
    patterns = _queries.get("unsafe_table_patterns", [])
    if name in unsafe:
        return True
    for pat in patterns:
        if name.endswith(pat):
            return True
    return False


def sql_references_unsafe_table(sql: str) -> str | None:
    """Return the first unsafe table found in sql, or None."""
    unsafe = set(_queries.get("unsafe_tables", []))
    patterns = _queries.get("unsafe_table_patterns", [])
    # Extract word tokens that look like table names (FCT_*)
    tokens = set(re.findall(r'\bFCT_\w+', sql, re.IGNORECASE))
    for token in tokens:
        if token in unsafe:
            return token
        for pat in patterns:
            if token.endswith(pat):
                return token
    return None


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------

def execute_registered_query(name: str, user_params: dict[str, str]) -> str:
    try:
        ctx = ensure_context()
    except RuntimeError as e:
        return str(e)

    query = _queries["queries"].get(name)
    if query is None:
        return f"Unknown query: '{name}'"

    params = {**user_params, "game_id": ctx["game_id"], "race_id": ctx["race_id"]}

    try:
        conn = get_db()
        try:
            cursor = conn.execute(query["sql"], params)
            columns = [d[0] for d in cursor.description]
            rows = cursor.fetchmany(MAX_ROWS)
            return rows_to_markdown(columns, rows)
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower():
            return "Aurora database is locked. Save the game first."
        return f"Database error: {e}"


# ---------------------------------------------------------------------------
# MCP server setup
# ---------------------------------------------------------------------------

server = Server("aurora4x")


@server.list_tools()
async def list_tools() -> list[Tool]:
    global _queries
    _queries = load_queries()
    tools: list[Tool] = []

    # --- registered query tools ---
    for name, q in _queries.get("queries", {}).items():
        desc = q["description"]
        if not q.get("verified", False):
            desc += " [DRAFT - unverified]"

        properties: dict[str, Any] = {}
        for param in q.get("params", []):
            properties[param] = {"type": "string", "description": param}

        tools.append(Tool(
            name=name,
            description=desc,
            inputSchema={
                "type": "object",
                "properties": properties,
                "required": q.get("params", []),
            },
        ))

    # --- ad-hoc execution tool ---
    tools.append(Tool(
        name="execute_sql",
        description=(
            "Execute arbitrary read-only SQL against the Aurora database. "
            "Use for exploration and one-off queries — no registration needed. "
            "The variables :game_id and :race_id are automatically available in your SQL. "
            "Results capped at AURORA_MAX_ROWS rows."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "Full SQL query to execute. May reference :game_id and :race_id."},
            },
            "required": ["sql"],
        },
    ))

    # --- introspection tools ---
    tools.append(Tool(
        name="describe_table",
        description="Returns PRAGMA table_info for a single Aurora table: column names, types, nullability, and primary key flags.",
        inputSchema={
            "type": "object",
            "properties": {"table_name": {"type": "string", "description": "Aurora table name, e.g. FCT_Population"}},
            "required": ["table_name"],
        },
    ))
    tools.append(Tool(
        name="list_all_tables",
        description="Lists every table present in the Aurora database. Use this to discover tables before calling describe_table.",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ))
    tools.append(Tool(
        name="list_safe_tables",
        description="Returns the schema block from queries.json: known-good tables with primary keys, foreign keys, safe columns, and notes.",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ))
    tools.append(Tool(
        name="list_queries",
        description="Lists all queries registered in queries.json with their verified status, params, and description.",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ))

    # --- registry tools ---
    tools.append(Tool(
        name="register_query",
        description=(
            "Appends a new query entry to queries.json with verified=false. "
            "The query is NOT executed — verification is always manual. "
            "Use :game_id and :race_id freely in SQL; do NOT list them in params. "
            "Name will be prefixed with DRAFT_ if not already. "
            "The new tool is available immediately — no restart required."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name":        {"type": "string", "description": "Query key (will be prefixed DRAFT_ if missing)"},
                "description": {"type": "string", "description": "Human-readable description exposed as MCP tool description"},
                "sql":         {"type": "string", "description": "Parameterized SQL using :param_name style. Use :game_id and :race_id freely."},
                "params":      {"type": "array",  "items": {"type": "string"}, "description": "User-facing named params. Never include game_id or race_id."},
                "tables_used": {"type": "array",  "items": {"type": "string"}, "description": "Tables this query touches"},
                "notes":       {"type": "string", "description": "Interpretation notes, units, caveats"},
            },
            "required": ["name", "description", "sql", "params", "tables_used"],
        },
    ))
    tools.append(Tool(
        name="update_query",
        description=(
            "Overwrites an existing query entry in queries.json. "
            "Use this to fix SQL, update descriptions, or change params on a query that already exists. "
            "Resets verified to false. Changes are live immediately."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name":        {"type": "string", "description": "Exact key of the query to update (including DRAFT_ prefix if present)"},
                "description": {"type": "string", "description": "Updated description"},
                "sql":         {"type": "string", "description": "Updated SQL"},
                "params":      {"type": "array",  "items": {"type": "string"}, "description": "Updated param list. Never include game_id or race_id."},
                "tables_used": {"type": "array",  "items": {"type": "string"}, "description": "Updated tables list"},
                "notes":       {"type": "string", "description": "Updated notes"},
            },
            "required": ["name", "description", "sql", "params", "tables_used"],
        },
    ))
    tools.append(Tool(
        name="delete_query",
        description=(
            "Removes a query entry from queries.json by name. "
            "Use this to clear broken or superseded DRAFTs before re-registering. "
            "Cannot delete built-in introspection tools. Change is live immediately."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Exact key of the query to delete"},
            },
            "required": ["name"],
        },
    ))
    tools.append(Tool(
        name="promote_query",
        description=(
            "Marks a DRAFT query as verified and removes the DRAFT_ prefix from its name. "
            "Use this once you have confirmed the query returns correct results. "
            "The promoted tool replaces the DRAFT tool immediately — no restart required."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Current key of the query (with DRAFT_ prefix)"},
            },
            "required": ["name"],
        },
    ))

    # --- jump network tools ---
    tools.append(Tool(
        name="systems_near",
        description=(
            "Returns all systems reachable within N hops of a source system using "
            "a Dijkstra graph traversal over the jump network. Much faster than the "
            "recursive SQL approach. Use this to answer 'systems near X rich in Y' "
            "queries — call this first, then join the returned system IDs against "
            "mineral or colony data."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "source_system_id": {"type": "integer", "description": "SystemID to search outward from"},
                "max_hops": {"type": "integer", "description": "Maximum jump hops (default 8, max practical ~20)"},
            },
            "required": ["source_system_id"],
        },
    ))
    tools.append(Tool(
        name="refresh_jump_network",
        description=(
            "Invalidates the cached jump network. Call this after survey vessels "
            "discover new jump points so the next routing call re-fetches from the DB. "
            "Takes no arguments — game context is resolved automatically."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ))

    return tools


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    global _queries
    _queries = load_queries()
    result = _dispatch(name, arguments)

    # After any registry mutation, reload and notify Claude Desktop so tool
    # list updates immediately without a server restart.
    _MUTATING_TOOLS = {"register_query", "update_query", "delete_query", "promote_query"}
    if name in _MUTATING_TOOLS and not result.startswith("Error") and not result.startswith("Missing") and not result.startswith("Query") and not result.startswith("Cannot"):
        _queries = load_queries()
        try:
            await server.request_context.session.send_notification(
                mcp_types.ServerNotification(
                    root=mcp_types.ToolListChangedNotification(
                        method="notifications/tools/list_changed"
                    )
                )
            )
        except Exception as e:
            print(f"WARN: could not send tools/list_changed notification: {e}", file=sys.stderr)

    return [TextContent(type="text", text=result)]


def _dispatch(name: str, args: dict) -> str:
    # introspection
    if name == "execute_sql":
        return _tool_execute_sql(args.get("sql", ""))
    if name == "describe_table":
        return _tool_describe_table(args.get("table_name", ""))
    if name == "list_all_tables":
        return _tool_list_all_tables()
    if name == "list_safe_tables":
        return _tool_list_safe_tables()
    if name == "list_queries":
        return _tool_list_queries()
    if name == "register_query":
        return _tool_register_query(args)
    if name == "update_query":
        return _tool_update_query(args)
    if name == "delete_query":
        return _tool_delete_query(args)
    if name == "promote_query":
        return _tool_promote_query(args)

    # jump network tools
    if name in ("systems_near", "refresh_jump_network"):
        return _dispatch_jump_network(name, args)

    # registered queries
    if name in _queries.get("queries", {}):
        query = _queries["queries"][name]
        user_params = {p: args.get(p, "") for p in query.get("params", [])}
        return execute_registered_query(name, user_params)

    return f"Unknown tool: '{name}'"


# ---------------------------------------------------------------------------
# Introspection tool implementations
# ---------------------------------------------------------------------------

def _tool_execute_sql(sql: str) -> str:
    if not sql.strip():
        return "sql is required."
    try:
        ctx = ensure_context()
    except RuntimeError as e:
        return str(e)
    try:
        conn = get_db()
        try:
            cursor = conn.execute(sql, {"game_id": ctx["game_id"], "race_id": ctx["race_id"]})
            if cursor.description is None:
                return "Query executed but returned no columns."
            columns = [d[0] for d in cursor.description]
            rows = cursor.fetchmany(MAX_ROWS)
            return rows_to_markdown(columns, rows)
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower():
            return "Aurora database is locked. Save the game first."
        return f"Database error: {e}"


def _tool_describe_table(table_name: str) -> str:
    if not table_name:
        return "table_name is required."
    try:
        conn = get_db()
        try:
            rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            if not rows:
                return f"Table '{table_name}' not found or has no columns."
            columns = ["cid", "name", "type", "notnull", "dflt_value", "pk"]
            return rows_to_markdown(columns, [tuple(r) for r in rows])
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower():
            return "Aurora database is locked. Save the game first."
        return f"Database error: {e}"


def _tool_list_all_tables() -> str:
    try:
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            if not rows:
                return "No tables found."
            return rows_to_markdown(["name"], [(r["name"],) for r in rows])
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower():
            return "Aurora database is locked. Save the game first."
        return f"Database error: {e}"


def _tool_list_safe_tables() -> str:
    schema = _queries.get("schema", {})
    if not schema:
        return "No schema entries found in queries.json."
    lines = ["| Table | PK | Foreign Keys | Safe Columns | Notes |",
             "| --- | --- | --- | --- | --- |"]
    for table, info in sorted(schema.items()):
        fk_str = ", ".join(f"{k}→{v}" for k, v in info.get("fk", {}).items()) or ""
        cols = ", ".join(info.get("safe_columns", []))
        notes = info.get("notes", "")
        lines.append(f"| {table} | {info.get('pk','')} | {fk_str} | {cols} | {notes} |")
    return "\n".join(lines)


def _tool_list_queries() -> str:
    queries = _queries.get("queries", {})
    if not queries:
        return "No queries registered."
    lines = ["| Name | Verified | Params | Description |",
             "| --- | --- | --- | --- |"]
    for qname, q in sorted(queries.items()):
        verified = "yes" if q.get("verified", False) else "DRAFT"
        params = ", ".join(q.get("params", [])) or "(none)"
        desc = q.get("description", "")
        lines.append(f"| {qname} | {verified} | {params} | {desc} |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Register query tool implementation
# ---------------------------------------------------------------------------

def _tool_register_query(args: dict) -> str:
    required = ["name", "description", "sql", "params", "tables_used"]
    missing = [f for f in required if f not in args]
    if missing:
        return f"Missing required fields: {', '.join(missing)}"

    name = args["name"]
    if not name.startswith("DRAFT_"):
        name = "DRAFT_" + name

    existing = _queries.get("queries", {})
    if name in existing:
        return f"Query '{name}' already exists. Choose a different name."

    sql = args["sql"]
    bad_table = sql_references_unsafe_table(sql)
    if bad_table:
        return f"SQL references unsafe table '{bad_table}'. Register rejected."

    entry = {
        "description": args["description"],
        "params": args["params"],
        "sql": sql,
        "tables_used": args["tables_used"],
        "notes": args.get("notes", ""),
        "verified": False,
        "added": date.today().isoformat(),
    }

    _queries["queries"][name] = entry
    _save_queries()

    return (
        f"Registered '{name}' in queries.json (verified=false). "
        "Sending tools/list_changed — the new tool should appear momentarily."
    )


# ---------------------------------------------------------------------------
# Update / delete / promote query tool implementations
# ---------------------------------------------------------------------------

_BUILTIN_TOOLS = {
    "execute_sql",
    "describe_table", "list_all_tables", "list_safe_tables", "list_queries",
    "register_query", "update_query", "delete_query", "promote_query",
    "systems_near", "refresh_jump_network",
}

def _dispatch_jump_network(name: str, args: dict) -> str:
    try:
        ctx = ensure_context()
    except RuntimeError as e:
        return str(e)

    game_id = ctx["game_id"]
    race_id = ctx["race_id"]

    if name == "refresh_jump_network":
        return tool_refresh_jump_network(game_id=game_id)

    try:
        conn = get_db()
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower():
            return "Aurora database is locked. Save the game first."
        return f"Database error: {e}"

    try:
        def db_execute(sql: str, params: list):
            return conn.execute(sql, params).fetchall()

        if name == "systems_near":
            src = int(args["source_system_id"])
            hops = int(args.get("max_hops", 8))
            return tool_systems_near(
                source_system_id=src,
                game_id=game_id,
                race_id=race_id,
                db_execute=db_execute,
                max_hops=hops,
            )

        return f"Unknown jump network tool: '{name}'"

    except (KeyError, ValueError, TypeError) as e:
        return f"Invalid arguments for '{name}': {e}"
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower():
            return "Aurora database is locked. Save the game first."
        return f"Database error: {e}"
    finally:
        conn.close()


def _save_queries() -> None:
    with open(QUERIES_PATH, "w", encoding="utf-8") as f:
        json.dump(_queries, f, indent=2)


def _tool_update_query(args: dict) -> str:
    required = ["name", "description", "sql", "params", "tables_used"]
    missing = [f for f in required if f not in args]
    if missing:
        return f"Missing required fields: {', '.join(missing)}"

    name = args["name"]
    existing = _queries.get("queries", {})
    if name not in existing:
        return f"Query '{name}' not found. Use register_query to create it."

    sql = args["sql"]
    bad_table = sql_references_unsafe_table(sql)
    if bad_table:
        return f"SQL references unsafe table '{bad_table}'. Update rejected."

    entry = existing[name].copy()
    entry.update({
        "description": args["description"],
        "params": args["params"],
        "sql": sql,
        "tables_used": args["tables_used"],
        "notes": args.get("notes", entry.get("notes", "")),
        "verified": False,
        "updated": date.today().isoformat(),
    })
    _queries["queries"][name] = entry
    _save_queries()
    new_params = args["params"]
    param_str = ", ".join(new_params) if new_params else "(none)"
    return (
        f"Updated '{name}' (verified reset to false). "
        f"New params: {param_str}. "
        f"IMPORTANT: run ToolSearch for '{name}' now to reload the updated schema before calling it."
    )


def _tool_delete_query(args: dict) -> str:
    name = args.get("name", "").strip()
    if not name:
        return "name is required."
    if name in _BUILTIN_TOOLS:
        return f"Cannot delete built-in tool '{name}'."
    if name not in _queries.get("queries", {}):
        return f"Query '{name}' not found."
    del _queries["queries"][name]
    _save_queries()
    return f"Deleted '{name}' from queries.json. Tool removed."


def _tool_promote_query(args: dict) -> str:
    name = args.get("name", "").strip()
    if not name:
        return "name is required."
    queries = _queries.get("queries", {})
    if name not in queries:
        return f"Query '{name}' not found."

    entry = queries[name].copy()
    entry["verified"] = True

    # Strip DRAFT_ prefix for the promoted name
    new_name = name[len("DRAFT_"):] if name.startswith("DRAFT_") else name

    if new_name != name:
        if new_name in queries:
            return f"Cannot promote: '{new_name}' already exists. Delete it first."
        del _queries["queries"][name]

    _queries["queries"][new_name] = entry
    _save_queries()

    if new_name != name:
        return (
            f"Promoted '{name}' → '{new_name}' (verified=true). Old DRAFT tool removed. "
            f"IMPORTANT: run ToolSearch for '{new_name}' now to load the promoted tool's schema."
        )
    return f"Marked '{name}' as verified=true."


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    global _queries
    _queries = load_queries(warn=True)
    query_count = len(_queries.get("queries", {}))
    print(f"Aurora 4x MCP server starting", file=sys.stderr)
    print(f"  DB path   : {DB_PATH}", file=sys.stderr)
    print(f"  Queries   : {query_count} registered", file=sys.stderr)
    print(f"  Max rows  : {MAX_ROWS}", file=sys.stderr)
    print(f"Waiting for MCP host (Claude Desktop)...", file=sys.stderr)
    init_options = InitializationOptions(
        server_name="aurora4x",
        server_version="0.1.0",
        capabilities=server.get_capabilities(
            notification_options=NotificationOptions(tools_changed=True),
            experimental_capabilities={},
        ),
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_options)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
