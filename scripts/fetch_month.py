"""Fetch every match the accounts in accounts.txt played in a given month.

Each match is saved once as matches/<match_id>.json; matches already saved
are skipped, so re-running is safe and resumes where a previous run stopped.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.opendota.com/api"
ROOT = Path(__file__).resolve().parent.parent
MATCHES_DIR = ROOT / "matches"
# The keyless API allows 60 requests/minute.
REQUEST_INTERVAL = 1.1
HEADERS = {"User-Agent": "dota-match-fetcher (GitHub Actions)", "Accept": "application/json"}


def get_json(url):
    for attempt in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                wait = 30 * (attempt + 1)
                print(f"  HTTP {e.code}, retrying in {wait}s", flush=True)
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"  {e}, retrying in 10s", flush=True)
            time.sleep(10)
    raise RuntimeError(f"Giving up on {url}")


def month_range(month):
    """Return (start, end) unix timestamps for 'YYYY-MM', or last full month if empty."""
    if month:
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
    else:
        now = datetime.now(timezone.utc)
        year, mon = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
        start = datetime(year, mon, 1, tzinfo=timezone.utc)
    year, mon = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    end = datetime(year, mon, 1, tzinfo=timezone.utc)
    return start, end


def main():
    start, end = month_range(os.environ.get("MONTH", "").strip())
    print(f"Collecting matches from {start:%Y-%m-%d} to {end:%Y-%m-%d} (UTC)")
    start_ts, end_ts = int(start.timestamp()), int(end.timestamp())
    days_back = (datetime.now(timezone.utc) - start).days + 1

    accounts = [line.strip() for line in (ROOT / "accounts.txt").read_text().splitlines() if line.strip()]
    match_ids = set()
    failed_accounts = []
    for account in accounts:
        # significant=0 includes all game modes (e.g. Turbo), not just ranked/normal.
        try:
            rows = get_json(f"{API}/players/{account}/matches?date={days_back}&significant=0")
        except Exception as e:
            print(f"::error::Account {account}: {e}", flush=True)
            failed_accounts.append(account)
            continue
        found = {r["match_id"] for r in rows if start_ts <= r["start_time"] < end_ts}
        print(f"Account {account}: {len(found)} matches")
        match_ids |= found
        time.sleep(REQUEST_INTERVAL)

    MATCHES_DIR.mkdir(exist_ok=True)
    todo = sorted(m for m in match_ids if not (MATCHES_DIR / f"{m}.json").exists())
    print(f"{len(match_ids)} unique matches, {len(match_ids) - len(todo)} already saved, {len(todo)} to fetch")

    failed = []
    for i, match_id in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {match_id}", flush=True)
        try:
            data = get_json(f"{API}/matches/{match_id}")
            if data.get("match_id") != match_id:
                raise ValueError(f"unexpected response: {str(data)[:200]}")
            (MATCHES_DIR / f"{match_id}.json").write_text(json.dumps(data, separators=(",", ":")))
        except Exception as e:
            print(f"::warning::Match {match_id}: {e}", flush=True)
            failed.append(match_id)
        time.sleep(REQUEST_INTERVAL)

    saved = len(todo) - len(failed)
    print(f"::notice::{start:%Y-%m}: {len(match_ids)} unique matches, {saved} newly saved, "
          f"{len(failed)} failed, {len(failed_accounts)} accounts failed")
    if failed_accounts and len(failed_accounts) == len(accounts):
        sys.exit(1)


if __name__ == "__main__":
    main()
