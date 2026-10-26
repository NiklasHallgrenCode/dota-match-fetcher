"""Find, download and analyze replays of matches played by the tracked accounts.

A match is included when at least MIN_TRACKED of the accounts in accounts.txt
played it and it isn't Turbo. Matches are found through Stratz (falling back to
OpenDota), and replay download links come from OpenDota. Each match is saved
once as matches/<YYYY-MM>/<match_id>.json. Valve deletes replays after a couple
of weeks, so this runs daily; matches whose replay is gone are recorded in
state/unavailable.json and not retried.
"""
import bz2
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze import analyze  # noqa: E402

API = "https://api.opendota.com/api"
STRATZ_API = "https://api.stratz.com/graphql"
ROOT = Path(__file__).resolve().parent.parent
MATCHES_DIR = ROOT / "matches"
UNAVAILABLE_FILE = ROOT / "state" / "unavailable.json"
EXTRACTOR = ROOT / "extractor" / "target" / "extractor.jar"
HEADERS = {"User-Agent": "dota-match-fetcher (GitHub Actions)", "Accept": "application/json"}
# The keyless API allows 60 requests/minute.
REQUEST_INTERVAL = 1.1
MIN_TRACKED = 2
TURBO = 23
# Replays older than this that still can't be downloaded are given up on
REPLAY_MAX_AGE_DAYS = 21
# After asking OpenDota to look up missing replay links, wait this long before checking again
LOOKUP_WAIT = 150
# In GitHub Actions, commit and push results every this many saved matches
CHECKPOINT_EVERY = 10
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def request(url, method="GET", data=None, headers=HEADERS):
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                wait = 30 * (attempt + 1)
                print(f"  HTTP {e.code} from {url}, retrying in {wait}s", flush=True)
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"  {e} from {url}, retrying in 10s", flush=True)
            time.sleep(10)
    raise RuntimeError(f"Giving up on {url}")


def window_start():
    """Start of the previous month (UTC), so a month is complete once it has passed."""
    now = datetime.now(timezone.utc)
    year, mon = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    return datetime(year, mon, 1, tzinfo=timezone.utc)


def stratz_matches(account, since):
    """Yield (match_id, start_time, game_mode) for an account's matches since `since`, via Stratz."""
    query = """query($id: Long!, $start: Long!, $skip: Int!) {
      player(steamAccountId: $id) {
        matches(request: {startDateTime: $start, take: 100, skip: $skip}) { id startDateTime gameMode }
      }
    }"""
    headers = {"Authorization": f"Bearer {os.environ['STRATZ_TOKEN']}", "User-Agent": "STRATZ_API",
               "Content-Type": "application/json"}
    skip = 0
    while True:
        body = json.dumps({"query": query, "variables": {
            "id": int(account), "start": int(since.timestamp()), "skip": skip}}).encode()
        resp = request(STRATZ_API, "POST", body, headers)
        if resp.get("errors"):
            raise RuntimeError(resp["errors"][0].get("message"))
        rows = ((resp.get("data") or {}).get("player") or {}).get("matches") or []
        if not rows:
            return
        for r in rows:
            yield r["id"], r["startDateTime"], "turbo" if r["gameMode"] == "TURBO" else r["gameMode"]
        # Stratz may return fewer than requested per page, so page until it returns nothing
        skip += len(rows)


def opendota_matches(account, since):
    """Same as stratz_matches, via OpenDota."""
    days = (datetime.now(timezone.utc) - since).days + 1
    for r in request(f"{API}/players/{account}/matches?date={days}&significant=0"):
        yield r["match_id"], r["start_time"], "turbo" if r.get("game_mode") == TURBO else r.get("game_mode")
    time.sleep(REQUEST_INTERVAL)


def find_matches(accounts, since):
    """Return {match_id: start_time} for non-Turbo matches with MIN_TRACKED+ tracked accounts."""
    sources = [stratz_matches, opendota_matches] if os.environ.get("STRATZ_TOKEN") else [opendota_matches]
    counts, start_times, failed = Counter(), {}, []
    for account in accounts:
        for source in sources:
            try:
                rows = list(source(account, since))
                break
            except Exception as e:
                print(f"::warning::Account {account} ({source.__name__}): {e}", flush=True)
        else:
            failed.append(account)
            continue
        for match_id, start_time, mode in rows:
            if mode != "turbo" and start_time >= since.timestamp():
                counts[match_id] += 1
                start_times[match_id] = start_time
    if len(failed) == len(accounts):
        raise RuntimeError("Could not look up any account")
    return {m: start_times[m] for m, n in counts.items() if n >= MIN_TRACKED}, failed


def match_info(match_id):
    """OpenDota's match data (replay link, duration, winner), or None if it doesn't have the match."""
    try:
        data = request(f"{API}/matches/{match_id}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    finally:
        time.sleep(REQUEST_INTERVAL)
    return data


def request_lookup(match_id):
    """Ask OpenDota to process a match, which makes it look up the replay link."""
    try:
        request(f"{API}/request/{match_id}", method="POST")
    except Exception as e:
        print(f"  lookup request failed: {e}", flush=True)
    time.sleep(REQUEST_INTERVAL)


def download_replay(url, dest):
    """Download and decompress a replay. Returns False if Valve no longer has it."""
    compressed = dest.with_suffix(".compressed")
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=300) as resp, open(compressed, "wb") as out:
            while chunk := resp.read(1 << 20):
                out.write(chunk)
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return False
        raise
    try:
        with open(compressed, "rb") as f:
            magic = f.read(4)
        # Valve serves zstd replays despite the .bz2 file name; older ones are bzip2
        if magic == ZSTD_MAGIC:
            subprocess.run(["zstd", "-q", "-d", "-f", str(compressed), "-o", str(dest)], check=True)
        elif magic[:3] == b"BZh":
            with open(compressed, "rb") as f, open(dest, "wb") as out:
                decomp = bz2.BZ2Decompressor()
                while chunk := f.read(1 << 20):
                    out.write(decomp.decompress(chunk))
        else:
            compressed.rename(dest)
    finally:
        compressed.unlink(missing_ok=True)
    return True


def process(match_id, start_time, info, tracked, workdir):
    """Download, extract and analyze one replay. Returns a problem string, or None when saved."""
    dem = workdir / f"{match_id}.dem"
    raw = workdir / f"{match_id}.raw.json"
    try:
        if not download_replay(info["replay_url"], dem):
            return "replay expired"
        subprocess.run(["java", "-jar", str(EXTRACTOR), str(dem), str(raw)], check=True,
                       stdout=subprocess.DEVNULL, timeout=600)
        summary = analyze(json.loads(raw.read_text()), start_time)
    finally:
        dem.unlink(missing_ok=True)
        raw.unlink(missing_ok=True)
    summary["match_id"] = match_id
    summary["duration"] = info.get("duration")
    if summary["radiant_win"] is None:
        summary["radiant_win"] = info.get("radiant_win")
    summary["tracked_account_ids"] = sorted(
        p["account_id"] for p in summary["players"] if p["account_id"] in tracked)
    month = datetime.fromtimestamp(start_time, timezone.utc).strftime("%Y-%m")
    out = MATCHES_DIR / month / f"{match_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2) + "\n")
    return None


def save_state(unavailable):
    UNAVAILABLE_FILE.parent.mkdir(exist_ok=True)
    UNAVAILABLE_FILE.write_text(json.dumps(dict(sorted(unavailable.items())), indent=1) + "\n")


def checkpoint(message):
    """Commit and push the results so far, so a long run doesn't lose its work."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    git = lambda *args: subprocess.run(["git", *args], cwd=ROOT, check=True)
    git("add", "matches", "state")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
        return
    git("commit", "-q", "-m", message)
    for attempt in range(3):
        try:
            git("pull", "-q", "--rebase", "origin", "main")
            git("push", "-q", "origin", "HEAD:main")
            return
        except subprocess.CalledProcessError:
            time.sleep(5 * (attempt + 1))
    print("::warning::Could not push checkpoint; results will be pushed at the end", flush=True)


def main():
    accounts = [line.strip() for line in (ROOT / "accounts.txt").read_text().splitlines() if line.strip()]
    since = window_start()
    matches, failed_accounts = find_matches(accounts, since)
    saved = {int(p.stem) for p in MATCHES_DIR.glob("*/*.json")}
    unavailable = json.loads(UNAVAILABLE_FILE.read_text()) if UNAVAILABLE_FILE.exists() else {}
    todo = sorted((m for m in matches if m not in saved and str(m) not in unavailable), reverse=True)
    print(f"::notice::{len(matches)} qualifying matches since {since:%Y-%m-%d}, {len(todo)} to process", flush=True)

    tracked = {int(a) for a in accounts}
    done, waiting, errors = 0, 0, 0
    now = time.time()

    def handle(match_id, info, workdir):
        nonlocal done, errors
        try:
            problem = process(match_id, matches[match_id], info, tracked, workdir)
        except Exception as e:
            print(f"::warning::Match {match_id}: {e}", flush=True)
            errors += 1
            return
        if problem is None:
            done += 1
            if done % CHECKPOINT_EVERY == 0:
                save_state(unavailable)
                checkpoint(f"Add matches (progress: {done} saved this run)")
        else:
            unavailable[str(match_id)] = problem
            print(f"  {problem}, giving up", flush=True)

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        # First pass: replays OpenDota already has a link for; ask it to look up the rest
        missing = {}
        for i, match_id in enumerate(todo, 1):
            print(f"[{i}/{len(todo)}] {match_id}", flush=True)
            try:
                info = match_info(match_id)
            except Exception as e:
                print(f"::warning::Match {match_id}: {e}", flush=True)
                errors += 1
                continue
            if info and info.get("replay_url"):
                handle(match_id, info, workdir)
            else:
                request_lookup(match_id)
                missing[match_id] = info

        # Second pass: check the looked-up matches again
        if missing:
            print(f"Waiting {LOOKUP_WAIT}s for OpenDota to find {len(missing)} replay links", flush=True)
            time.sleep(LOOKUP_WAIT)
        for match_id in missing:
            print(f"[retry] {match_id}", flush=True)
            try:
                info = match_info(match_id)
            except Exception as e:
                print(f"::warning::Match {match_id}: {e}", flush=True)
                errors += 1
                continue
            if info and info.get("replay_url"):
                handle(match_id, info, workdir)
            elif (now - matches[match_id]) / 86400 > REPLAY_MAX_AGE_DAYS:
                unavailable[str(match_id)] = "no replay link"
            else:
                waiting += 1
                print("  no replay link yet, will retry next run", flush=True)

    save_state(unavailable)
    print(f"::notice::{done} matches saved, {waiting} waiting for a replay, {errors} errors, "
          f"{len(unavailable)} unavailable in total, {len(failed_accounts)} account lookups failed")


if __name__ == "__main__":
    main()
