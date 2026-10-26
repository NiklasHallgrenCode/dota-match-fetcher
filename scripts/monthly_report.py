"""Build a monthly lane report image for the tracked accounts and optionally post it to Discord.

The report is an HTML page rendered to PNG with headless Chrome (Playwright).

Usage: python3 scripts/monthly_report.py [--month YYYY-MM] [--post] [--no-profiles]

Posting needs the DISCORD_WEBHOOK_URL environment variable. CHROME_PATH can point
at a Chrome/Chromium binary; otherwise the installed Google Chrome is used.
"""
import argparse
import base64
import html
import itertools
import json
import os
import time
import urllib.request
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = ROOT / "reports"
HEADERS = {"User-Agent": "dota-match-fetcher (GitHub Actions)"}

# A lane won by at least this much net worth at 8:00 counts as a stomp
STOMP_LEAD = 2000
# Rankings: minimum sample sizes and lane win rate to be listed, and how many to show
LANER_MIN_LANES = 20
PARTNER_MIN_LANES = 5
MIN_LANE_WIN_PCT = 51
DUO_MIN_GAMES = 10
TOP_N = 5

LANE_TYPES = {("radiant", "bot"): "Safe lane", ("dire", "top"): "Safe lane",
              ("radiant", "top"): "Offlane", ("dire", "bot"): "Offlane",
              ("radiant", "mid"): "Mid", ("dire", "mid"): "Mid"}


def previous_month(month=None):
    if month:
        year, mon = map(int, month.split("-"))
    else:
        now = datetime.now(timezone.utc)
        year, mon = now.year, now.month
    year, mon = (year - 1, 12) if mon == 1 else (year, mon - 1)
    return f"{year}-{mon:02d}"


def load_matches(month):
    return [json.loads(p.read_text()) for p in sorted((ROOT / "matches" / month).glob("*.json"))]


def new_lane_stats():
    return {"win": 0, "draw": 0, "loss": 0, "stomps": 0, "lead_sum": 0, "lanes": Counter()}


def add_lane(stats, result, lead, lane_type):
    stats[result] += 1
    stats["lead_sum"] += lead
    stats["lanes"][lane_type] += 1
    if result == "win" and lead >= STOMP_LEAD:
        stats["stomps"] += 1


def pct(a, b):
    return 100 * a / b if b else None


def lane_pct(s):
    """Lane win rate with draws not counted."""
    return pct(s["win"], s["win"] + s["loss"])


def compute(matches, tracked):
    """All statistics for one month."""
    laners = defaultdict(new_lane_stats)
    partners = defaultdict(new_lane_stats)
    duos = defaultdict(lambda: {"games": 0, "wins": 0})
    header = {"matches": len(matches), "group_games": 0, "group_wins": 0, "lane_win": 0, "lane_loss": 0,
              "won_lane_games": 0, "won_lane_wins": 0, "lost_lane_games": 0, "lost_lane_wins": 0}

    for m in matches:
        ours = sorted((p for p in m["players"] if p["account_id"] in tracked), key=lambda p: p["account_id"])

        def won(team):
            return m["radiant_win"] is not None and m["radiant_win"] == (team == "radiant")

        def lead_for(p):
            lead = m["lanes"][p["lane"]].get("radiant_lead") or 0
            return lead if p["team"] == "radiant" else -lead

        for p in ours:
            if p["lane_result"] and p["lane"] in ("top", "mid", "bot"):
                add_lane(laners[p["account_id"]], p["lane_result"], lead_for(p), LANE_TYPES[(p["team"], p["lane"])])
        for a, b in itertools.combinations(ours, 2):
            if a["team"] != b["team"]:
                continue
            duo = duos[(a["account_id"], b["account_id"])]
            duo["games"] += 1
            duo["wins"] += won(a["team"])
            if a["lane"] == b["lane"] and a["lane"] in ("top", "mid", "bot") and a["lane_result"]:
                add_lane(partners[(a["account_id"], b["account_id"])], a["lane_result"], lead_for(a),
                         LANE_TYPES[(a["team"], a["lane"])])

        # Group totals use the side with 2+ tracked players (skipped if both sides have them)
        sides = [t for t, n in Counter(p["team"] for p in ours).items() if n >= 2]
        if len(sides) != 1 or m["radiant_win"] is None:
            continue
        team = sides[0]
        header["group_games"] += 1
        header["group_wins"] += won(team)
        for lane in {p["lane"] for p in ours if p["team"] == team and p["lane"] in ("top", "mid", "bot")}:
            winner = m["lanes"][lane]["winner"]
            if winner in (None, "draw"):
                continue
            key = "won_lane" if winner == team else "lost_lane"
            header["lane_win" if winner == team else "lane_loss"] += 1
            header[f"{key}_games"] += 1
            header[f"{key}_wins"] += won(team)
    return {"header": header, "laners": laners, "partners": partners, "duos": duos}


def ranked_lanes(stats, min_lanes):
    rows = []
    for key, s in stats.items():
        p = lane_pct(s)
        if s["win"] + s["draw"] + s["loss"] >= min_lanes and p is not None and p >= MIN_LANE_WIN_PCT:
            rows.append((key, s, p))
    rows.sort(key=lambda r: (-r[2], -(r[1]["win"] + r[1]["loss"])))
    return rows[:TOP_N]


def ranked_duos(stats):
    rows = [(key, s, pct(s["wins"], s["games"])) for key, s in stats.items() if s["games"] >= DUO_MIN_GAMES]
    rows.sort(key=lambda r: (-r[2], -r[1]["games"]))
    return rows[:TOP_N]


def trend_html(current, previous, prev_label):
    """Change in percentage points vs. last month, or "new" if not ranked-eligible then."""
    if previous is None:
        return '<div class="trend muted">new</div>'
    delta = round(current - previous)
    if delta == 0:
        return f'<div class="trend muted">± 0% vs {prev_label}</div>'
    arrow, cls = ("▲", "up") if delta > 0 else ("▼", "down")
    return f'<div class="trend {cls}">{arrow} {abs(delta)}% vs {prev_label}</div>'


# --- profiles (names and avatars) ---

def download(url, attempts=3):
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=30) as resp:
                return resp.read()
        except Exception as e:
            if attempt == attempts - 1:
                print(f"Could not download {url}: {e}")
            else:
                time.sleep(3 * (attempt + 1))
    return None


def fetch_profile(account_id):
    data = download(f"https://api.opendota.com/api/players/{account_id}")
    profile = (json.loads(data).get("profile") or {}) if data else {}
    image = download(profile["avatarmedium"]) if profile.get("avatarmedium") else None
    return {"name": profile.get("personaname") or str(account_id),
            "avatar": "data:image/jpeg;base64," + base64.b64encode(image).decode() if image else None}


def avatar_html(profile):
    if profile["avatar"]:
        return f'<img class="avatar" src="{profile["avatar"]}" alt="">'
    return f'<div class="avatar initial">{html.escape(profile["name"][:1].upper())}</div>'


# --- HTML ---

def bar_html(win, draw, loss):
    segs = "".join(f'<span class="{cls}" style="flex:{n}"></span>'
                   for cls, n in (("won", win), ("drawn", draw), ("lost", loss)) if n)
    return f'<div class="bar">{segs}</div>' if segs else ""


def lane_detail(s):
    lane_type = s["lanes"].most_common(1)[0][0]
    stomps = f' · {s["stomps"]} stomp{"" if s["stomps"] == 1 else "s"}' if s["stomps"] else ""
    lead = s["lead_sum"] / (s["win"] + s["draw"] + s["loss"])
    cls = "pos" if lead > 0 else "neg" if lead < 0 else ""
    return (f'{lane_type} · {s["win"]}-{s["draw"]}-{s["loss"]}{stomps} · '
            f'NW@8 <span class="{cls}">{lead:+.0f}</span>')


def row_html(rank, avatars, title, detail, bar, value, trend):
    rank_cls = {1: "gold", 2: "silver", 3: "bronze"}.get(rank, "")
    return f"""
      <div class="row">
        <div class="rank {rank_cls}">{rank}</div>
        <div class="avatars">{avatars}</div>
        <div class="body">
          <div class="title">{title}</div>
          <div class="detail">{detail}</div>
          {bar}
        </div>
        <div class="value">
          <div class="pct">{value:.0f}%</div>
          <div class="label">Win rate</div>
          {trend}
        </div>
      </div>"""


def build_html(month, stats, prev, profiles):
    prev_label = datetime.strptime(previous_month(month), "%Y-%m").strftime("%b")
    month_title = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    h = stats["header"]

    def name(a):
        return html.escape(profiles[a]["name"])

    def fmt(v):
        return "–" if v is None else f"{v:.0f}%"

    def prev_lane_pct(table, key, min_lanes):
        s = prev[table].get(key)
        return lane_pct(s) if s and s["win"] + s["draw"] + s["loss"] >= min_lanes else None

    def prev_duo_pct(key):
        s = prev["duos"].get(key)
        return pct(s["wins"], s["games"]) if s and s["games"] >= DUO_MIN_GAMES else None

    header_stats = [
        (str(h["matches"]), "Matches"),
        (fmt(pct(h["group_wins"], h["group_games"])), "Game win rate"),
        (fmt(pct(h["lane_win"], h["lane_win"] + h["lane_loss"])), "Lane win rate"),
        (f'{fmt(pct(h["won_lane_wins"], h["won_lane_games"]))} / '
         f'{fmt(pct(h["lost_lane_wins"], h["lost_lane_games"]))}', "Win after lane won / lost"),
    ]
    laner_rows = "".join(
        row_html(i, avatar_html(profiles[a]), name(a), lane_detail(s), bar_html(s["win"], s["draw"], s["loss"]),
                 p, trend_html(p, prev_lane_pct("laners", a, LANER_MIN_LANES), prev_label))
        for i, (a, s, p) in enumerate(ranked_lanes(stats["laners"], LANER_MIN_LANES), 1))
    partner_rows = "".join(
        row_html(i, avatar_html(profiles[a]) + avatar_html(profiles[b]), f"{name(a)} + {name(b)}",
                 lane_detail(s), bar_html(s["win"], s["draw"], s["loss"]),
                 p, trend_html(p, prev_lane_pct("partners", (a, b), PARTNER_MIN_LANES), prev_label))
        for i, ((a, b), s, p) in enumerate(ranked_lanes(stats["partners"], PARTNER_MIN_LANES), 1))
    duo_rows = [
        row_html(i, avatar_html(profiles[a]) + avatar_html(profiles[b]), f"{name(a)} + {name(b)}",
                 f'{s["games"]} games · {s["wins"]}-{s["games"] - s["wins"]}',
                 bar_html(s["wins"], 0, s["games"] - s["wins"]),
                 p, trend_html(p, prev_duo_pct((a, b)), prev_label))
        for i, ((a, b), s, p) in enumerate(ranked_duos(stats["duos"]), 1)]
    split = (len(duo_rows) + 1) // 2
    empty = '<div class="empty">Not enough games this month</div>'
    duos = (f'<div class="cols"><div>{"".join(duo_rows[:split])}</div>'
            f'<div>{"".join(duo_rows[split:])}</div></div>') if duo_rows else empty
    stats_html = "".join(f'<div class="stat"><div class="num">{v}</div><div class="cap">{c}</div></div>'
                         for v, c in header_stats)

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=block" rel="stylesheet">
<style>
  :root {{
    --bg: #0d1117; --card: #151b23; --border: #2a313c; --rule: #232a33;
    --text: #e6edf3; --text-2: #b4bcc6; --muted: #7d8590;
    --accent: #d29922; --gold: #e3b341; --silver: #c9d1d9; --bronze: #db8b4d;
    --won: #3fb950; --drawn: #5c636c; --lost: #f85149;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: Inter, "Segoe UI", system-ui, sans-serif;
         -webkit-font-smoothing: antialiased; }}
  .report {{ width: 1152px; padding: 40px 46px 36px; background: var(--bg); }}
  header {{ display: flex; justify-content: space-between; align-items: flex-end;
           padding-bottom: 22px; border-bottom: 1px solid var(--rule); }}
  .eyebrow {{ color: var(--accent); font-size: 12px; font-weight: 600; letter-spacing: .18em; }}
  h1 {{ font-size: 40px; font-weight: 800; letter-spacing: -.01em; margin-top: 10px; }}
  .stats {{ display: flex; gap: 28px; text-align: center; }}
  .stat .num {{ font-size: 24px; font-weight: 700; }}
  .stat .cap {{ color: var(--muted); font-size: 11px; letter-spacing: .06em; text-transform: uppercase; margin-top: 4px; }}
  .grid {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 24px; margin-top: 28px; align-items: start; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 22px 21px 10px; }}
  .card.wide {{ grid-column: 1 / -1; }}
  .card-head {{ display: flex; justify-content: space-between; align-items: baseline; }}
  .card h2 {{ font-size: 16px; font-weight: 700; }}
  .card .rule {{ color: var(--muted); font-size: 11px; }}
  .card .sub {{ color: var(--text-2); font-size: 12px; margin-top: 6px; margin-bottom: 8px; }}
  .cols {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); column-gap: 28px; }}
  .row {{ display: flex; align-items: center; gap: 14px; padding: 12px 0; border-bottom: 1px solid var(--rule); }}
  .row:last-child {{ border-bottom: none; }}
  .rank {{ width: 16px; font-size: 17px; font-weight: 700; color: var(--muted); text-align: center; }}
  .rank.gold {{ color: var(--gold); }} .rank.silver {{ color: var(--silver); }} .rank.bronze {{ color: var(--bronze); }}
  .avatars {{ display: flex; }}
  .avatars .avatar + .avatar {{ margin-left: -10px; }}
  .avatar {{ width: 40px; height: 40px; border-radius: 50%; border: 2px solid var(--card); object-fit: cover; background: #2d333b; }}
  .avatar.initial {{ display: flex; align-items: center; justify-content: center; font-weight: 700; color: var(--text-2); }}
  .body {{ flex: 1; min-width: 0; }}
  .title {{ font-size: 14px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .detail {{ color: var(--text-2); font-size: 12px; margin-top: 3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .pos {{ color: var(--won); }} .neg {{ color: var(--lost); }}
  .bar {{ display: flex; gap: 2px; height: 5px; margin-top: 7px; }}
  .bar span {{ border-radius: 2px; }}
  .bar .won {{ background: var(--won); }} .bar .drawn {{ background: var(--drawn); }} .bar .lost {{ background: var(--lost); }}
  .value {{ width: 82px; text-align: right; }}
  .pct {{ font-size: 22px; font-weight: 700; }}
  .label {{ color: var(--muted); font-size: 10px; letter-spacing: .08em; text-transform: uppercase; margin-top: 2px; }}
  .trend {{ font-size: 11px; font-weight: 500; margin-top: 3px; }}
  .trend.up {{ color: var(--won); }} .trend.down {{ color: var(--lost); }} .trend.muted {{ color: var(--muted); }}
  .empty {{ color: var(--muted); font-size: 13px; padding: 16px 0 18px; }}
  footer {{ display: flex; justify-content: space-between; gap: 24px; color: var(--muted); font-size: 11px; margin-top: 22px; }}
  .key {{ display: inline-block; width: 9px; height: 9px; border-radius: 2px; margin: 0 4px 0 6px; vertical-align: -1px; }}
</style></head>
<body><div class="report">
  <header>
    <div><div class="eyebrow">LANE REPORT</div><h1>{month_title}</h1></div>
    <div class="stats">{stats_html}</div>
  </header>
  <div class="grid">
    <section class="card">
      <div class="card-head"><h2>Top laners</h2><span class="rule">{MIN_LANE_WIN_PCT}%+ · min {LANER_MIN_LANES} lanes</span></div>
      <div class="sub">Lanes won vs. lost (win–draw–loss, draws not counted)</div>
      {laner_rows or empty}
    </section>
    <section class="card">
      <div class="card-head"><h2>Top lane partners</h2><span class="rule">{MIN_LANE_WIN_PCT}%+ · min {PARTNER_MIN_LANES} lanes</span></div>
      <div class="sub">Two of the group in the same lane</div>
      {partner_rows or empty}
    </section>
    <section class="card wide">
      <div class="card-head"><h2>Best duos</h2><span class="rule">min {DUO_MIN_GAMES} games</span></div>
      <div class="sub">Game win rate on the same team, any lanes</div>
      {duos}
    </section>
  </div>
  <footer>
    <div>Lane result<span class="key" style="background:var(--won)"></span>won<span class="key"
      style="background:var(--drawn)"></span>drawn<span class="key" style="background:var(--lost)"></span>lost
      &nbsp;·&nbsp; NW@8: lane net worth vs. opposing lane at 8:00 &nbsp;·&nbsp; stomp: lane won by {STOMP_LEAD:,}+
      &nbsp;·&nbsp; ▲▼ change in percentage points</div>
    <div>Own replay analysis · generated {datetime.now(timezone.utc):%Y-%m-%d}</div>
  </footer>
</div></body></html>"""


def render(page_html, out):
    from playwright.sync_api import sync_playwright

    out.parent.mkdir(parents=True, exist_ok=True)
    html_path = out.with_suffix(".html")
    html_path.write_text(page_html)
    try:
        with sync_playwright() as p:
            chrome = os.environ.get("CHROME_PATH")
            browser = p.chromium.launch(executable_path=chrome) if chrome else p.chromium.launch(channel="chrome")
            page = browser.new_page(viewport={"width": 1152, "height": 800}, device_scale_factor=2)
            page.goto(html_path.as_uri(), wait_until="networkidle")
            page.evaluate("document.fonts.ready")
            page.locator(".report").screenshot(path=str(out))
            browser.close()
    finally:
        html_path.unlink()


def post_to_discord(image, month):
    """Send the image to the Discord channel behind DISCORD_WEBHOOK_URL."""
    title_month = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    payload = {"content": f"**Lane report — {title_month}**",
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
    parser.add_argument("--no-profiles", action="store_true", help="don't look up names and avatars")
    args = parser.parse_args()

    tracked = {int(line.strip()) for line in (ROOT / "accounts.txt").read_text().splitlines() if line.strip()}
    stats = compute(load_matches(args.month), tracked)
    prev = compute(load_matches(previous_month(args.month)), tracked)
    profiles = {a: {"name": str(a), "avatar": None} if args.no_profiles else fetch_profile(a) for a in tracked}
    out = REPORTS_DIR / f"{args.month}.png"
    render(build_html(args.month, stats, prev, profiles), out)
    print(f"Wrote {out}")
    if args.post:
        post_to_discord(out, args.month)


if __name__ == "__main__":
    main()
