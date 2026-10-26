"""Build last month's lane win % leaderboard for the tracked accounts as an image,
and optionally post it to Discord.

Usage: python3 scripts/monthly_report.py [--month YYYY-MM] [--post]

Posting needs the DISCORD_WEBHOOK_URL environment variable.
"""
import argparse
import json
import os
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = ROOT / "reports"
HEADERS = {"User-Agent": "dota-match-fetcher (GitHub Actions)"}
# Players need at least this many matches in the month to be on the leaderboard
MIN_GAMES = 1
# Longer names are shortened so they fit the name column
MAX_NAME_LENGTH = 22

# Dark theme (Discord is usually viewed dark)
SURFACE = "#1a1a19"
TEXT_PRIMARY = "#f0efec"
TEXT_SECONDARY = "#c3c2b7"
TEXT_MUTED = "#8f8e86"
GRID = "#3a3a37"
BAR = "#3987e5"


def previous_month():
    now = datetime.now(timezone.utc)
    year, mon = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    return f"{year}-{mon:02d}"


def player_stats(month, accounts):
    """Per tracked account: lane wins/draws/losses, games and game wins in the month."""
    stats = {a: {"lane_win": 0, "lane_draw": 0, "lane_loss": 0, "games": 0, "wins": 0} for a in accounts}
    for path in sorted((ROOT / "matches" / month).glob("*.json")):
        match = json.loads(path.read_text())
        for p in match["players"]:
            s = stats.get(p["account_id"])
            if s is None:
                continue
            s["games"] += 1
            radiant = p["team"] == "radiant"
            if match["radiant_win"] is not None and match["radiant_win"] == radiant:
                s["wins"] += 1
            if p["lane_result"]:
                s["lane_" + p["lane_result"]] += 1
    return stats


def player_name(account_id):
    try:
        url = f"https://api.opendota.com/api/players/{account_id}"
        with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=30) as resp:
            name = (json.load(resp).get("profile") or {}).get("personaname")
            if name:
                return name
    except Exception as e:
        print(f"Could not look up name for {account_id}: {e}")
    return str(account_id)


def leaderboard(stats, names):
    rows = []
    for account, s in stats.items():
        lanes = s["lane_win"] + s["lane_draw"] + s["lane_loss"]
        if s["games"] < MIN_GAMES or lanes == 0:
            continue
        name = names[account]
        rows.append({
            "name": name if len(name) <= MAX_NAME_LENGTH else name[:MAX_NAME_LENGTH - 1].rstrip() + "…",
            "lane_pct": 100 * s["lane_win"] / lanes,
            "record": f'{s["lane_win"]}-{s["lane_draw"]}-{s["lane_loss"]}',
            "games": s["games"],
            "win_pct": 100 * s["wins"] / s["games"],
        })
    rows.sort(key=lambda r: (-r["lane_pct"], -r["games"]))
    return rows


def render(rows, month, out):
    title_month = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    row_h = 0.42
    fig_h = 1.5 + row_h * max(len(rows), 1) + 0.5
    fig = plt.figure(figsize=(9, fig_h), dpi=150, facecolor=SURFACE)
    plt.rcParams["font.family"] = "DejaVu Sans"

    # Layout in inches: name column, bar area, detail column
    left, bar_w, right = 2.1, 3.9, 3.0
    width = left + bar_w + right
    top = 1.35
    ax = fig.add_axes([left / width * 0.98, 0.5 / fig_h, bar_w / width, (fig_h - top - 0.5) / fig_h])
    ax.set_facecolor(SURFACE)
    ax.set_xlim(0, 100)
    ax.set_ylim(len(rows) - 0.5, -0.5)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_yticks([])
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xticklabels(["0%", "25%", "50%", "75%", "100%"], color=TEXT_MUTED, fontsize=8)
    ax.tick_params(axis="x", length=0, pad=6)
    for x in (25, 50, 75, 100):
        ax.axvline(x, color=GRID, linewidth=0.8 if x != 50 else 1.2, linestyle="-" if x == 50 else ":", zorder=0)

    fig.text(0.03, 1 - 0.42 / fig_h, f"Lane win % — {title_month}", color=TEXT_PRIMARY,
             fontsize=16, fontweight="bold", va="center")
    fig.text(0.03, 1 - 0.8 / fig_h,
             "Lane won = more lane net worth than the opponents at 8:00 (within 250 is a draw). "
             "Matches with 2+ tracked players, no Turbo.",
             color=TEXT_MUTED, fontsize=7.5, va="center")
    # Detail columns, right-aligned at these x positions (in bar-axis units, past 100%)
    columns = (("Lane W-D-L", 132), ("Games", 150), ("Game win %", 172))
    for label, x in columns:
        ax.text(x, -0.85, label, color=TEXT_MUTED, fontsize=8, va="center", ha="right", clip_on=False)

    bar_h = 0.5  # in row units; ~21px at this size, under the 24px cap
    radius = 1.2
    for i, r in enumerate(rows):
        pct = r["lane_pct"]
        if pct > 0:
            # Rounded data end, square at the baseline: a rounded box plus a square patch over its start
            ax.add_patch(FancyBboxPatch((0, i - bar_h / 2), pct, bar_h,
                                        boxstyle=f"round,pad=0,rounding_size={min(radius, pct / 2)}",
                                        mutation_aspect=bar_h / 6, linewidth=0, facecolor=BAR, zorder=2))
            ax.add_patch(plt.Rectangle((0, i - bar_h / 2), min(radius, pct), bar_h,
                                       linewidth=0, facecolor=BAR, zorder=2))
        ax.text(pct + 1.5, i, f"{pct:.0f}%", color=TEXT_PRIMARY, fontsize=9.5, fontweight="bold",
                va="center", ha="left", zorder=3)
        ax.text(-3, i, f"{i + 1}. {r['name']}", color=TEXT_PRIMARY, fontsize=9.5,
                va="center", ha="right", clip_on=False)
        for (_, x), value in zip(columns, (r["record"], str(r["games"]), f"{r['win_pct']:.0f}%")):
            ax.text(x, i, value, color=TEXT_SECONDARY, fontsize=9, va="center", ha="right", clip_on=False)
    if not rows:
        ax.text(50, 0, "No matches this month", color=TEXT_SECONDARY, ha="center", va="center")

    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=SURFACE)
    plt.close(fig)


def post_to_discord(image, month):
    """Send the image to the Discord channel behind DISCORD_WEBHOOK_URL."""
    title_month = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    payload = {"content": f"**Lane win % — {title_month}**",
               "attachments": [{"id": 0, "filename": image.name}]}
    boundary = uuid.uuid4().hex
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="payload_json"\r\n',
        b"Content-Type: application/json\r\n\r\n",
        json.dumps(payload).encode(), b"\r\n",
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="files[0]"; filename="{image.name}"\r\n'.encode(),
        b"Content-Type: image/png\r\n\r\n",
        image.read_bytes(), b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(os.environ["DISCORD_WEBHOOK_URL"], data=body, method="POST", headers={
        **HEADERS, "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        print(f"Posted to Discord (HTTP {resp.status})")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", default=previous_month(), help="YYYY-MM (default: last month)")
    parser.add_argument("--post", action="store_true", help="post the image to Discord")
    parser.add_argument("--no-names", action="store_true", help="show account IDs instead of looking up names")
    args = parser.parse_args()

    accounts = [int(line.strip()) for line in (ROOT / "accounts.txt").read_text().splitlines() if line.strip()]
    stats = player_stats(args.month, accounts)
    names = {a: str(a) if args.no_names or stats[a]["games"] == 0 else player_name(a) for a in accounts}
    rows = leaderboard(stats, names)
    out = REPORTS_DIR / f"{args.month}.png"
    render(rows, args.month, out)
    print(f"Wrote {out} ({len(rows)} players)")
    if args.post:
        post_to_discord(out, args.month)


if __name__ == "__main__":
    main()
