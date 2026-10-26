# dota-match-fetcher

A GitHub Action that downloads a Dota 2 match from the [OpenDota API](https://docs.opendota.com/) (no API key) and saves it as JSON.

## Usage

1. Go to **Actions → Fetch Dota match → Run workflow**.
2. Enter a match ID (defaults to `9040073871`).
3. The match is saved to `matches/<match_id>.json` and committed to the repo. It is also attached to the run as a downloadable artifact.

The keyless OpenDota API is rate limited (about 60 requests/minute, 2,000/day).
