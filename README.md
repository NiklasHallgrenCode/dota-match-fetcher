# dota-match-fetcher

A GitHub Action that downloads every Dota 2 match played by the accounts in [`accounts.txt`](accounts.txt) during the last full month, using the [OpenDota API](https://docs.opendota.com/) (no API key).

Each match is saved once as `matches/<match_id>.json`. A match played by several of the accounts is only stored once, and matches that are already saved are skipped, so re-running the workflow only fetches what is missing.

## Usage

- **Automatic:** runs on the 1st of every month and fetches the month that just ended.
- **Manual:** **Actions → Fetch monthly matches → Run workflow**. Leave the month empty for the last full month, or enter one as `YYYY-MM` (e.g. `2026-08`).

To change who is tracked, edit `accounts.txt` (one account ID per line).

## Notes

- All game modes are included (Turbo etc.), not only ranked/normal.
- Accounts with a private match history return no matches.
- The keyless API allows about 60 requests/minute and 2,000/day. The script waits between requests; if the daily limit is hit, failed matches are fetched on the next run.
- Dates are in UTC.
