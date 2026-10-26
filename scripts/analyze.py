"""Turn the raw replay extract (from extractor/) into the match summary we store.

Lane:  where the hero spent most of its time between LANE_FROM and LANE_UNTIL
       (game time, seconds), ignoring samples where it was dead or in a base.
Role:  safe lane core = pos 1, mid = pos 2, off lane core = pos 3, off lane
       support = pos 4, safe lane support = pos 5. The core of a lane is the
       player with the most last hits at LANE_UNTIL.
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
    mid = sorted((p for p in team_players if p["lane"] == "mid"), key=by_lh, reverse=True)
    off = sorted((p for p in team_players if p["lane"] == OFF_LANE[team]), key=by_lh, reverse=True)
    standard = len(safe) == 2 and len(mid) == 1 and len(off) == 2

    # Cores first: the top last-hitter in each lane
    for role, lane in ((1, safe), (2, mid), (3, off)):
        if lane:
            lane[0]["role"] = role
    # Supports: the remaining player in the off lane is 4, in the safe lane 5
    for role, lane in ((4, off), (5, safe)):
        rest = [p for p in lane if p["role"] is None]
        if rest:
            rest[0]["role"] = role
    # Anyone left (jungle, trilanes, extra mids) takes the free roles,
    # most last hits getting the lowest role number
    free = [r for r in range(1, 6) if r not in {p["role"] for p in team_players}]
    for p, role in zip(sorted((p for p in team_players if p["role"] is None), key=by_lh, reverse=True), free):
        p["role"] = role
    return standard


if __name__ == "__main__":
    with open(sys.argv[1]) as f:
        print(json.dumps(analyze(json.load(f)), indent=2))
