"""
Jump network graph engine for Aurora 4x MCP server.

Replaces the recursive SQL CTE with a Python Dijkstra over a cached adjacency
list. One flat DB read at first call; subsequent calls hit the in-process cache.

db_execute convention: callable(sql: str, params: list) -> list[sqlite3.Row]
Rows support indexed access (row[0], row[1], ...) — matches sqlite3.Row factory.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from heapq import heappush, heappop
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class JumpEdge:
    dest_system_id: int
    distance: float       # raw Aurora units; divide by 1e9 for Bkm
    dest_xcor: float
    dest_ycor: float


@dataclass
class ReachableSystem:
    system_id: int
    hops: int
    total_distance: float  # raw Aurora units
    path: list[int]        # ordered SystemIDs, source first

    @property
    def total_distance_bkm(self) -> float:
        return round(self.total_distance / 1_000_000_000.0, 2)

    @property
    def path_str(self) -> str:
        return " -> ".join(str(s) for s in self.path)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

@dataclass
class NetworkCache:
    game_id: int
    adjacency: dict[int, list[JumpEdge]]
    loaded_at: float = field(default_factory=time.time)
    ttl_seconds: float = 600.0  # bust after survey results via refresh_jump_network

    def is_stale(self) -> bool:
        return (time.time() - self.loaded_at) > self.ttl_seconds


_cache: dict[int, NetworkCache] = {}


# ---------------------------------------------------------------------------
# Network loader
# ---------------------------------------------------------------------------

def load_network(game_id: int, db_execute: Callable) -> dict[int, list[JumpEdge]]:
    """
    Return the adjacency dict for game_id, loading from DB if cache is cold/stale.

    One flat self-join of FCT_JumpPoint — no recursion, no LIKE anti-cycle checks.
    Edge weight is the Euclidean distance (km) between the two linked jump-point
    coordinates, which share a common galactic frame.
    """
    global _cache
    cached = _cache.get(game_id)
    if cached and not cached.is_stale():
        return cached.adjacency

    # Edge weight = Euclidean distance between the two linked jump points in the
    # shared galactic coordinate frame (km).  FCT_JumpPoint.Distance is the AU
    # distance from the system's star to the jump point — NOT inter-system distance
    # — so it is intentionally excluded here.
    rows = db_execute(
        """
        SELECT
            jp_src.SystemID  AS src_system,
            jp_dst.SystemID  AS dst_system,
            jp_src.Xcor      AS src_xcor,
            jp_src.Ycor      AS src_ycor,
            jp_dst.Xcor      AS dst_xcor,
            jp_dst.Ycor      AS dst_ycor
        FROM FCT_JumpPoint jp_src
        JOIN FCT_JumpPoint jp_dst
            ON  jp_src.WPLink  = jp_dst.WarpPointID
            AND jp_dst.GameID  = jp_src.GameID
        WHERE jp_src.GameID  = ?
          AND jp_src.WPLink  > 0
        """,
        [game_id],
    )

    adjacency: dict[int, list[JumpEdge]] = {}
    for row in rows:
        src = row[0]
        dst = row[1]
        src_xcor = float(row[2]) if row[2] else 0.0
        src_ycor = float(row[3]) if row[3] else 0.0
        dst_xcor = float(row[4]) if row[4] else 0.0
        dst_ycor = float(row[5]) if row[5] else 0.0
        dx = dst_xcor - src_xcor
        dy = dst_ycor - src_ycor
        dist = (dx * dx + dy * dy) ** 0.5
        adjacency.setdefault(src, []).append(JumpEdge(
            dest_system_id=dst,
            distance=dist,
            dest_xcor=dst_xcor,
            dest_ycor=dst_ycor,
        ))

    _cache[game_id] = NetworkCache(game_id=game_id, adjacency=adjacency)
    return adjacency


# ---------------------------------------------------------------------------
# Core algorithm
# ---------------------------------------------------------------------------

def dijkstra(
    adjacency: dict[int, list[JumpEdge]],
    source_id: int,
    *,
    destination_id: Optional[int] = None,
    max_hops: int = 15,
) -> dict[int, ReachableSystem]:
    """
    Dijkstra over the jump network from source_id.

    destination_id: early-exit once the destination is settled (point-to-point).
    max_hops: hard depth cap; default 15 covers all practical empire operations,
              pass 50 for full exploration or long-range routing.

    Returns {system_id: ReachableSystem} for all settled nodes.
    """
    # heap: (total_distance, hops, system_id, path)
    heap: list[tuple[float, int, int, list[int]]] = [
        (0.0, 0, source_id, [source_id])
    ]
    settled: dict[int, ReachableSystem] = {}

    while heap:
        dist, hops, sys_id, path = heappop(heap)
        if sys_id in settled:
            continue
        settled[sys_id] = ReachableSystem(
            system_id=sys_id,
            hops=hops,
            total_distance=dist,
            path=path,
        )
        if destination_id is not None and sys_id == destination_id:
            break
        if hops >= max_hops:
            continue
        for edge in adjacency.get(sys_id, []):
            if edge.dest_system_id not in settled:
                heappush(heap, (
                    dist + edge.distance,
                    hops + 1,
                    edge.dest_system_id,
                    path + [edge.dest_system_id],
                ))

    return settled


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_systems_near(
    source_system_id: int,
    game_id: int,
    race_id: int,
    db_execute: Callable,
    max_hops: int = 8,
) -> str:
    """
    Return all systems reachable within max_hops of source_system_id.
    Caller can join the returned system IDs against mineral/colony data.
    """
    adjacency = load_network(game_id, db_execute)
    settled = dijkstra(adjacency, source_system_id, max_hops=max_hops)

    system_ids = list(settled.keys())
    if not system_ids:
        return f"No systems reachable from {source_system_id} within {max_hops} hops."

    placeholders = ",".join("?" * len(system_ids))
    name_rows = db_execute(
        f"SELECT SystemID, Name FROM FCT_RaceSysSurvey "
        f"WHERE GameID = ? AND RaceID = ? AND SystemID IN ({placeholders})",
        [game_id, race_id] + system_ids,
    )
    names = {row[0]: row[1] for row in name_rows}

    results = sorted(settled.values(), key=lambda x: (x.hops, x.total_distance))
    src_name = names.get(source_system_id, f"System {source_system_id}")

    lines = [
        f"Systems within {max_hops} hops of {src_name} (ID {source_system_id}) — {len(results)} found",
        "",
        "| System ID | Name | Hops | Distance (Bkm) | Path |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in results:
        name = names.get(r.system_id, f"Unsurveyed {r.system_id}")
        lines.append(
            f"| {r.system_id} | {name} | {r.hops} | {r.total_distance_bkm} | {r.path_str} |"
        )
    return "\n".join(lines)


def tool_route_between_systems(
    source_system_id: int,
    destination_system_id: int,
    game_id: int,
    race_id: int,
    db_execute: Callable,
) -> str:
    """
    Shortest route (by jump distance) between two systems.
    Early-exits once destination is settled — fast on large maps.
    """
    adjacency = load_network(game_id, db_execute)
    settled = dijkstra(
        adjacency,
        source_system_id,
        destination_id=destination_system_id,
        max_hops=50,
    )

    result = settled.get(destination_system_id)
    if result is None:
        return (
            f"No route found from system {source_system_id} to "
            f"system {destination_system_id} within 50 hops."
        )

    placeholders = ",".join("?" * len(result.path))
    name_rows = db_execute(
        f"SELECT SystemID, Name FROM FCT_RaceSysSurvey "
        f"WHERE GameID = ? AND RaceID = ? AND SystemID IN ({placeholders})",
        [game_id, race_id] + result.path,
    )
    names = {row[0]: row[1] for row in name_rows}
    named_path = [names.get(s, f"Unsurveyed {s}") for s in result.path]

    lines = [
        f"Route: {named_path[0]} → {named_path[-1]}",
        f"Hops: {result.hops}  |  Total distance: {result.total_distance_bkm} Bkm",
        "",
        "**Path:** " + " -> ".join(named_path),
        "",
        "| Hop | System ID | Name | Leg Distance (Bkm) |",
        "| --- | --- | --- | --- |",
    ]
    for i, sys_id in enumerate(result.path):
        leg_bkm = ""
        if i > 0:
            prev_id = result.path[i - 1]
            for edge in adjacency.get(prev_id, []):
                if edge.dest_system_id == sys_id:
                    leg_bkm = str(round(edge.distance / 1_000_000_000.0, 2))
                    break
        name = names.get(sys_id, f"Unsurveyed {sys_id}")
        lines.append(f"| {i} | {sys_id} | {name} | {leg_bkm} |")

    return "\n".join(lines)


def tool_logistics_audit(
    path_system_ids: list[int],
    game_id: int,
    race_id: int,
    db_execute: Callable,
) -> str:
    """
    Audit each waypoint in a route for refuel capability.
    Provides per-leg distances and colony presence; fuel burn calc is left to caller.
    """
    if not path_system_ids:
        return "No systems in path."

    placeholders = ",".join("?" * len(path_system_ids))

    name_rows = db_execute(
        f"SELECT SystemID, Name FROM FCT_RaceSysSurvey "
        f"WHERE GameID = ? AND RaceID = ? AND SystemID IN ({placeholders})",
        [game_id, race_id] + path_system_ids,
    )
    names = {row[0]: row[1] for row in name_rows}

    # FCT_Population has SystemID directly — no join to FCT_SystemBody needed.
    colony_rows = db_execute(
        f"SELECT SystemID, SUM(Population) AS TotalPop "
        f"FROM FCT_Population "
        f"WHERE GameID = ? AND RaceID = ? AND SystemID IN ({placeholders}) "
        f"GROUP BY SystemID",
        [game_id, race_id] + path_system_ids,
    )
    colonies = {row[0]: row[1] for row in colony_rows}

    # Ensure network is loaded so leg distances are always available.
    adj = load_network(game_id, db_execute)

    src_name = names.get(path_system_ids[0], f"System {path_system_ids[0]}")
    dst_name = names.get(path_system_ids[-1], f"System {path_system_ids[-1]}")

    lines = [
        f"Logistics audit: {src_name} → {dst_name} ({len(path_system_ids)} waypoints)",
        "",
        "| # | System ID | Name | Refuel | Population (M) | Leg Distance (Bkm) |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for i, sys_id in enumerate(path_system_ids):
        leg_bkm = ""
        if i > 0:
            prev_id = path_system_ids[i - 1]
            for edge in adj.get(prev_id, []):
                if edge.dest_system_id == sys_id:
                    leg_bkm = str(round(edge.distance / 1_000_000_000.0, 2))
                    break

        has_colony = sys_id in colonies
        pop_m = round(colonies[sys_id] / 1_000_000, 2) if has_colony else ""
        refuel = "Yes" if has_colony else "No"
        name = names.get(sys_id, f"Unsurveyed {sys_id}")
        lines.append(f"| {i} | {sys_id} | {name} | {refuel} | {pop_m} | {leg_bkm} |")

    refuel_count = sum(1 for s in path_system_ids if s in colonies)
    lines += [
        "",
        f"Refuel points available: {refuel_count} of {len(path_system_ids)} waypoints",
    ]
    return "\n".join(lines)


def tool_refresh_jump_network(game_id: int) -> str:
    """Bust the cache for game_id. Next routing call re-fetches from DB."""
    if game_id in _cache:
        del _cache[game_id]
        return (
            f"Jump network cache invalidated for game {game_id}. "
            "Next routing call will re-fetch from the database."
        )
    return f"No cached jump network for game {game_id} — nothing to invalidate."
