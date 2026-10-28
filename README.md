# dota-match-fetcher

A GitHub Action that downloads the replays of Dota 2 matches played by the accounts in [`accounts.txt`](accounts.txt), analyzes them, and stores a summary of each match as JSON.

A match is included when **at least 2 of the tracked accounts** played in it and it is **not Turbo**.

## How it works

Runs daily at 04:23 UTC, which is 05:23 Stockholm time in winter and 06:23 in summer (and on demand from **Actions → Fetch and analyze matches → Run workflow**):

1. Finds the tracked accounts' matches since the start of the previous month using both [Stratz](https://stratz.com/api) and [OpenDota](https://docs.opendota.com/), combining their lists since each misses some matches.
2. Gets each replay's download link from OpenDota. If OpenDota doesn't have it yet, it asks OpenDota to look it up and checks again a few minutes later (or on the next run).
3. Downloads the `.dem` replay from Valve and reads it with [`extractor/`](extractor), a small Java program built on the [clarity](https://github.com/skadistats/clarity) replay parser. It records each hero's position, net worth, earned gold and last hits every second of the first 10 minutes.
4. [`scripts/analyze.py`](scripts/analyze.py) turns that into lanes, roles and lane results, saved to `matches/<YYYY-MM>/<match_id>.json`. The replay itself is not kept.

Valve deletes replays after roughly two weeks, which is why it runs every day. Matches whose replay is already gone are listed in `state/unavailable.json` and not retried.

## What's in a match file

- `radiant_win`, `start_time`, `duration`, `tracked_account_ids`
- `players`: for each player their `account_id`, `team`, `hero`, `lane` (top/mid/bot), `role` (1-5), net worth, earned gold and last hits at 8:00, and `lane_result` (win/loss/draw)
- `lanes`: for top, mid and bot, which players each side had there, their summed net worth at 8:00, Radiant's lead and the `winner` (radiant/dire/draw)
- `standard_lanes`: `false` when a team didn't play a normal 2-1-2 (trilane, jungler, etc.); roles are still assigned but are less certain

## Rules

- **Lane:** the lane the hero spent the most time in between 1:00 and 8:00 (time dead or in base is ignored).
- **Role:** safe lane core = 1, mid = 2, off lane core = 3, off lane support = 4, safe lane support = 5. The core is whoever had more last hits at 8:00. Radiant's safe lane is bot, Dire's is top.
  If a team didn't play a 2-1-2, its 3 players with the most last hits are the cores (mid = 2, safe lane = 1, the other = 3) and the other two are supports (off lane = 4, other = 5).
- **Lane winner:** compare the two sides' summed net worth in that lane at 8:00. A lead of 250 or less is a draw; 251 or more is a win.

These are constants at the top of `scripts/analyze.py` and easy to change.

## Setup

- `STRATZ_TOKEN` repository secret: a Stratz API token. Stratz allows a token to be used from only 2 IP addresses per 15 minutes, so running the workflow several times in quick succession can make Stratz refuse requests; OpenDota still finds the matches then.
- To change who is tracked, edit `accounts.txt` (one account ID per line).


## Monthly Discord report

At 10:48 UTC on the 1st of every month (11:48 Stockholm time in winter, 12:48 in summer), [`monthly-report.yml`](.github/workflows/monthly-report.yml) posts a lane report image for the previous month to Discord, and saves it to `reports/<YYYY-MM>.png`:

- **Header:** matches, the group's game win rate and lane win rate, and game win rate after winning / losing the lane.
- **Top laners:** tracked players by lane win rate (draws not counted), with their usual lane, W-D-L, stomps and average lane net worth lead at 8:00 (NW@8).
- **Top lane partners:** the same for two tracked players sharing a lane.
- **Best duos:** game win rate of two tracked players on the same team.
- Changes vs. the month before in percentage points; "new" when there wasn't enough data that month.

Names and avatars are the players' current Steam profiles (via OpenDota). The page is built as HTML and screenshotted with headless Chrome. Thresholds (minimum lanes/games, 51%+, stomp = lane won by 2,000+) are constants at the top of [`scripts/monthly_report.py`](scripts/monthly_report.py).

- Needs a `DISCORD_WEBHOOK_URL` repository secret (Discord channel → Edit Channel → Integrations → Webhooks → New Webhook → Copy Webhook URL).
- Run it by hand from **Actions → Monthly lane report → Run workflow**; pick a month, or untick "Post to Discord" to only save the image.
- **Test monthly lane report** (manual only) posts to a test server instead, using the `DISCORD_WEBHOOK_URL_TEST` secret. It doesn't commit anything; the image is attached to the run.
