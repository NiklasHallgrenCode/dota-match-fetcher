"""Turn the raw replay extract (from extractor/) into the match summary we store.

Lane:  where the hero spent most of its time between LANE_FROM and LANE_UNTIL
       (game time, seconds), ignoring samples where it was dead or in a base.
Role:  safe lane core = pos 1, mid = pos 2, off lane core = pos 3, off lane
       support = pos 4, safe lane support = pos 5. The core of a lane is the
       player with the most last hits at LANE_UNTIL. When a team didn't play a
       2-1-2, its 3 top last-hitters are the cores (mid 2, safe lane 1, other 3)
       and the other two the supports (off lane 4, safe lane 5).
Lane winner: the sides' summed net worth in each lane at LANE_UNTIL. A lead of
       DRAW_MAX or less is a draw.
"""
import json
import sys
from collections import Counter

LANE_FROM = 60
LANE_UNTIL = 480
DRAW_MAX = 250
STEAM_ID_OFFSET = 76561197960265728

RADIANT, DIRE = 2, 3
TEAM_NAMES = {RADIANT: "radiant", DIRE: "dire"}
SAFE_LANE = {RADIANT: "bot", DIRE: "top"}
OFF_LANE = {RADIANT: "top", DIRE: "bot"}

# Map coordinates in the extract are in "cells" (world units / 128, offset so the
# map centre is 128; the playable area is roughly 64-192). They are normalised
# to -1..1 with radiant's base in the bottom-left corner.
MAP_CENTRE = 128.0
MAP_HALF_WIDTH = 64.0
BASE = 0.6       # both coordinates beyond this (same sign): inside a base
MID_BAND = 0.2   # |u - v| below this: the mid lane diagonal
LANE_EDGE = 0.35 # outer lanes run along the edges, beyond this on either axis


def lane_of(x, y):
    """Classify a position as top, mid, bot, base or jungle."""
    u = (x - MAP_CENTRE) / MAP_HALF_WIDTH
    v = (y - MAP_CENTRE) / MAP_HALF_WIDTH
    if (u < -BASE and v < -BASE) or (u > BASE and v > BASE):
        return "base"
    if abs(u - v) < MID_BAND:
        return "mid"
    if v > u:
        return "top" if u < -LANE_EDGE or v > LANE_EDGE else "jungle"
    return "bot" if u > LANE_EDGE or v < -LANE_EDGE else "jungle"


def analyze(extract, start_time=None):
    fields = extract["sample_fields"]
    X, Y, ALIVE, NW, GOLD, LH = (fields.index(f) for f in ("x", "y", "alive", "networth", "gold", "lh"))
    samples = {s[0]: s[1:] for s in extract["samples"]}
    at_end = samples.get(LANE_UNTIL)

    heroes = {p["steamid"]: p.get("hero") for p in extract.get("file_players", [])}
    players = []
    for i, p in enumerate(extract["players"]):
        seen = Counter()
        for t, row in samples.items():
            s = row[i]
            if LANE_FROM <= t <= LANE_UNTIL and s[ALIVE] == 1 and s[X] is not None:
                seen[lane_of(s[X], s[Y])] += 1
        seen.pop("base", None)
        lanes = Counter({k: n for k, n in seen.items() if k != "jungle"})
        lane = lanes.most_common(1)[0][0] if lanes else ("jungle" if seen else None)
        end = at_end[i] if at_end else None
        steamid = p["steamid"]
        players.append({
            "account_id": steamid - STEAM_ID_OFFSET if steamid else None,
            "team": TEAM_NAMES.get(p["team"]),
            "hero_id": p["hero_id"],
            "hero": (heroes.get(steamid) or "").replace("npc_dota_hero_", "") or None,
            "lane": lane,
            "lane_time_share": round(seen[lane] / sum(seen.values()), 2) if lane and seen else None,
            "role": None,
            "networth_8min": end[NW] if end else None,
            "gold_8min": end[GOLD] if end else None,
            "last_hits_8min": end[LH] if end else None,
            "_team_id": p["team"],
        })

    standard = True
    for team in (RADIANT, DIRE):
        standard &= assign_roles([p for p in players if p["_team_id"] == team], team)

    lanes = {}
    for lane in ("top", "mid", "bot"):
        sides = {}
        for team in (RADIANT, DIRE):
            members = [p for p in players if p["_team_id"] == team and p["lane"] == lane]
            sides[TEAM_NAMES[team]] = {
                "account_ids": [p["account_id"] for p in members],
                "roles": sorted(p["role"] for p in members if p["role"]),
                "networth_8min": sum(p["networth_8min"] or 0 for p in members) if at_end else None,
            }
        result = None
        if at_end:
            lead = sides["radiant"]["networth_8min"] - sides["dire"]["networth_8min"]
            result = "draw" if abs(lead) <= DRAW_MAX else "radiant" if lead > 0 else "dire"
            sides["radiant_lead"] = lead
        sides["winner"] = result
        lanes[lane] = sides

    for p in players:
        team = p.pop("_team_id")
        winner = lanes.get(p["lane"], {}).get("winner")
        p["lane_result"] = None if winner is None else "draw" if winner == "draw" else \
            "win" if winner == TEAM_NAMES[team] else "loss"

    winner = extract.get("game_winner")
    return {
        "match_id": extract.get("match_id"),
        "start_time": start_time,
        "game_mode": extract.get("game_mode"),
        "radiant_win": True if winner == RADIANT else False if winner == DIRE else None,
        "standard_lanes": standard,
        "lanes": lanes,
        "players": players,
    }


def assign_roles(team_players, team):
    """Set each player's role (1-5). Returns False if the lanes weren't a normal 2-1-2."""
    by_lh = lambda p: p["last_hits_8min"] or 0
    safe = sorted((p for p in team_players if p["lane"] == SAFE_LANE[team]), key=by_lh, reverse=True)
    mid = [p for p in team_players if p["lane"] == "mid"]
    off = sorted((p for p in team_players if p["lane"] == OFF_LANE[team]), key=by_lh, reverse=True)

    if len(safe) == 2 and len(mid) == 1 and len(off) == 2:
        safe[0]["role"], safe[1]["role"] = 1, 5
        mid[0]["role"] = 2
        off[0]["role"], off[1]["role"] = 3, 4
        return True

    # Trilanes, junglers, roaming supports: the three players with the most last
    # hits are the cores and the other two the supports, then lanes decide the order
    ranked = sorted(team_players, key=by_lh, reverse=True)
    cores, supports = ranked[:3], ranked[3:]
    mid_cores = [p for p in cores if p["lane"] == "mid"]
    pos2 = mid_cores[0] if mid_cores else None
    rest = [p for p in cores if p is not pos2]
    safe_cores = [p for p in rest if p["lane"] == SAFE_LANE[team]]
    pos1 = safe_cores[0] if safe_cores else rest[0]
    rest.remove(pos1)
    if pos2 is None:
        pos2 = rest.pop(0)
    pos1["role"], pos2["role"], rest[0]["role"] = 1, 2, 3

    off_supports = [p for p in supports if p["lane"] == OFF_LANE[team]]
    pos4 = off_supports[0] if off_supports else supports[0]
    pos4["role"] = 4
    next(p for p in supports if p is not pos4)["role"] = 5
    return False


if __name__ == "__main__":
    with open(sys.argv[1]) as f:
        print(json.dumps(analyze(json.load(f)), indent=2))
