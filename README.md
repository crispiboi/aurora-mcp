# Aurora 4x MCP Server

A read-only MCP (Model Context Protocol) server that lets Claude query your Aurora 4x campaign database directly. Claude can look up colonies, fleets, minerals, commanders, ordnance, and more. It can register, edit, and promote new queries against your live DB without restarting. You can have natural language conversations with the LLM about your game state, ask for reports, and do analysis across the board.

AI/LLM DISCLAIMER: This tool was generated with the assistance of claude code by a user who has a solid grasp on the game. This tool is built specificially to query the DB, and nothing else. If you write to the DB you may cause unintended consquence and will not be able to receive support by the larger community.

---

## Getting started

Once the server is connected, start a new Claude Code session and say:

> Please verify my current game and give me a description of the available tools.

Claude will call `get_session_context` to confirm the right campaign is loaded, let you know that query results are limited to 200 rows by default, and ask whether you'd like to disable that limit for the session. It will then summarize what each tool does. From there you can ask natural-language questions about your empire — minerals, colonies, fleets, commanders, ordnance — and Claude will pick the right tools automatically.

---

## Prerequisites

- Python 3.10 or later
- Aurora 4x installed with at least one active campaign saved
- [Claude Code](https://claude.ai/code) or Claude Desktop

---

## Installation

### 1. Place the server files

Copy the server directory somewhere permanent. These docs assume:

```
D:\Aurora\aurora-mcp\
  server.py
  queries.json
  pyproject.toml
```

Adjust all paths below if you use a different location.

### 2. Install the Python dependency

Open a terminal and run:

```powershell
pip install "mcp>=1.0.0"
```

Or install from the project directory using the included `pyproject.toml`:

```powershell
cd D:\Aurora\aurora-mcp
pip install -e .
```

### 3. Locate your Aurora database

Aurora 4x always stores its database in its own install folder. The file you need is `AuroraDB.db`, sitting directly inside the Aurora directory — the same folder that contains `Aurora.exe`.

```
D:\Aurora\AuroraDB.db    ← example; your install path may differ
```

You must set `AURORA_DB_PATH` to this file's full path in the config below. There is no default that will work out of the box.

### 4. Connect to Claude Desktop / Claude Code

Edit the MCP configuration file:

```powershell
notepad "$env:APPDATA\Claude\claude_desktop_config.json"
```

If the file doesn't exist yet, create it. Add the following, replacing the paths with your actual paths:

```json
{
  "mcpServers": {
    "aurora4x": {
      "command": "python",
      "args": [
        "D:/Aurora/aurora-mcp/server.py"
      ],
      "env": {
        "AURORA_DB_PATH": "D:/Aurora/AuroraDB.db"
      }
    }
  }
}
```

> **Path format:** Use forward slashes (`/`) even on Windows, or double backslashes (`\\`). Single backslashes will cause a JSON parse error.

> **If you already have other MCP servers** in your config, add the `"aurora4x"` block inside the existing `"mcpServers"` object — don't create a second one.

### 5. Restart and verify

Close and reopen Claude Desktop / Claude Code. In a new session, ask:

> Call `get_session_context` to confirm the right campaign is loaded.

Claude should return your GameID, GameName, RaceID, and RaceName. If it does, you're good to go.

---

## Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `AURORA_DB_PATH` | `Aurora.db` next to `server.py` | Full path to `AuroraDB.db` |

---

## Available tools

### Ad-hoc query

| Tool | Description |
| --- | --- |
| `execute_sql` | Run any read-only SQL directly. `:game_id` and `:race_id` are automatically available as bind parameters. Results are capped at 200 rows by default — set `limit_query: false` to retrieve the full result set at the cost of more tokens. Use this for exploration and one-off queries before registering them. |

### Introspection

| Tool | Description |
| --- | --- |
| `get_session_context` | Returns the GameID and RaceID resolved for this session — call first to confirm the right campaign is active |
| `list_queries` | Lists all registered queries with verified status, params, and description |
| `list_safe_tables` | Shows the known-good table schema from `queries.json` |
| `list_all_tables` | Lists every table in the Aurora database |
| `describe_table` | Returns column names, types, and primary key flags for a specific table |

### Query registry

| Tool | Description |
| --- | --- |
| `register_query` | Saves a new query to `queries.json` as `DRAFT_<name>`. Available immediately — no restart needed. |
| `update_query` | Overwrites an existing query's SQL, params, or description. Resets `verified` to false. Live immediately. |
| `delete_query` | Removes a query from `queries.json` by name. Live immediately. |
| `promote_query` | Marks a `DRAFT_` query as verified and renames it by stripping the `DRAFT_` prefix. Live immediately. |

### Jump network

| Tool | Description |
| --- | --- |
| `systems_near` | Returns all systems within N jump hops of a source system using Dijkstra traversal. Use this first for "what's near X" queries, then join the returned system IDs against mineral or colony data. Params: `source_system_id`, `max_hops` (default 8). |
| `refresh_jump_network` | Invalidates the cached jump network. Call after survey vessels discover new jump points so the next routing call re-fetches from the DB. |

### Registered queries

Queries are stored in `queries.json` and loaded on every request — no restart needed after changes. All registered queries also accept `limit_query: false` to bypass the 200-row cap.

**Empire**

| Query | Params | Description |
| --- | --- | --- |
| `get_session_context` | — | Active game and race info |
| `game_log` | `days`, `hours` | Recent game log entries, looking back N days or hours |
| `mineral_survey` | — | Body-level mineral survey — every surveyed body with amount, accessibility, and accessible amount |
| `mineral_survey_by_system` | `mineral_name` | Minerals summarised by system; filter by mineral name or leave empty for all |
| `minerals_near_system` | `source_system_id`, `max_hops`, `mineral_name` | Mineral totals for all systems within N hops of a given system |
| `colony_report` | — | Full colony report: population, species, stockpiles, installations, and production queues |
| `commander_assignment` | `name_fragment` | Commander details by name fragment — type, rank, assignment, homeworld, and career history |
| `commander_profile` | `name_fragment` | Full RP/lore profile — skills, traits, medals, career history, health, loyalty |
| `ship_design_report` | — | Full ship design report — propulsion, armour, sensors, capacity, EW, component manifest, and capture provenance |
| `ship_class_classifications` | — | Quick hull classification lookup — class name, type (BB/CA/DD etc), military vs commercial |
| `class_ordnance_templates` | `class_name` | Ordnance load-out templates per class with full missile stats and colony stock |
| `missile_catalogue` | `missile_name` | Complete missile catalogue — speed, range, warhead, ECM/ECCM, staging, and total stock |
| `colony_ordnance_stockpiles` | `missile_name` | Missile stockpiles held at colonies — stock count, size, and key combat stats |
| `ship_ordnance_status` | — | Per-ship ordnance status — loaded vs template vs deficit, with ship location and collier flag |
| `system_distances` | `source_system_id`, `destination_system_id`, `fleet_name` | Shortest route between two systems with hop-by-hop distance and per-class fuel cost |

**Alien intelligence**

> These queries expose information about other races and may reveal spoilers. `SPOILER_GUARD_other_races` should be called first to confirm the player wants to proceed.

| Query | Params | Description |
| --- | --- | --- |
| `SPOILER_GUARD_other_races` | — | Lists all non-player races in the game. Call before any alien query to confirm the player wants to view potentially spoiler information. |
| `alien_race_contact_roster` | — | All alien races known to the empire — contact status, comms status, first detected date, diplomatic points, and treaty flags |
| `alien_species_autopsy` | `alien_race_id` | Habitat tolerances (gravity, temperature, oxygen, pressure) for a given alien species. Only returns data if an autopsy has been completed. |
| `alien_class_capabilities` | `alien_race_id` | Observed ship class capabilities — hull stats, weapons with ranges, and detected technology |
| `alien_ship_roster` | `alien_race_id` | Individual alien hulls by name and class — status, damage taken, last known system, and last contact date |

---

## Row limits

All query tools cap results at **200 rows** by default. This is a fixed limit — it cannot be changed in config. When results are truncated, the response will include a notice.

To retrieve the full result set, pass `limit_query: false` on any tool call. This bypasses the cap entirely and may return a large number of rows, consuming significantly more tokens.

---

## Query lifecycle

The server reloads `queries.json` on every request, so all registry changes take effect immediately.

```
execute_sql          ← explore with ad-hoc SQL
    ↓
register_query       ← save a promising query as DRAFT_<name>
    ↓
update_query         ← fix SQL or params as needed
    ↓
promote_query        ← once verified, strip DRAFT_ and mark verified=true
    ↓
delete_query         ← remove broken or superseded drafts
```

---

## Important: save before querying

---

## Troubleshooting

**Server doesn't appear in Claude Code**
- Confirm `python` resolves in your PATH: run `python --version` in a terminal.
- Confirm the path to `server.py` in `args` is correct and uses forward slashes or `\\`.
- Check Claude Code's MCP logs (Developer → MCP Servers) for startup errors.

**"No active campaign found"**
- The server finds the campaign via `MAX(GameID) WHERE GameName != 'Sample'`. Make sure you have a real saved game, not just the built-in sample campaign.

**"No player race found"**
- The server looks for `MIN(RaceID)` with `NPR = 0`. If that doesn't match your setup, inspect `FCT_Race` with `describe_table` and `execute_sql`.

**Wrong campaign loaded**
- The server picks the highest `GameID`. If you have multiple campaigns, the most recently created one wins. Verify with `get_session_context`.

**A DRAFT query returns wrong column names**
- Use `describe_table` on the relevant table to check exact column names, then fix the query with `update_query`.
