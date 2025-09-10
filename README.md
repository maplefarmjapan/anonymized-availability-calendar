# iCal Anonymizer (chatgpt version)

This variant improves the original script by adding configuration via flags/env, robust HTTP retries, deterministic anonymized UIDs (so clients can deduplicate), broader scrubbing of sensitive fields, and atomic writes.

Primary entrypoint: `./convertiCal-chatgpt.py`

## What It Does
- Fetches a source iCalendar feed (`.ics`).
- Replaces human-readable fields (SUMMARY, DESCRIPTION) with non-identifying values.
- Removes sensitive metadata (ORGANIZER, ATTENDEE, CONTACT, URL, COMMENT, RESOURCES, GEO, CATEGORIES, RELATED-TO, ATTACH). Optionally clears LOCATION.
- Generates deterministic anonymized `UID`s based on event timing/recurrence, instead of deleting them.
- Writes the sanitized calendar atomically to `output.ics` (or a custom path).

## Requirements
- Python 3.9+
- Packages listed in `requirements.txt.easy_install`

## Setup
```bash
cd iCal-conversion-code/
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt.easy_install
```

## Usage
A source URL is required via `--source` or `SOURCE_CAL_URL`.

Run with explicit source and output:
```bash
./convertiCal-chatgpt.py \
  --source 'https://example.com/path/to/feed.ics' \
  --output ./output.ics
```

Environment variables alternative:
```bash
export SOURCE_CAL_URL='https://example.com/feed.ics'
export OUTPUT_CAL_PATH='./output.ics'
./convertiCal-chatgpt.py
```

Keep the LOCATION field (by default it is cleared):
```bash
./convertiCal-chatgpt.py --source "$SOURCE_CAL_URL" --keep-location
```

Force all events to appear as full-day blocks (useful when the source has timed entries like "4pm" but you want the entire day marked busy):
```bash
./convertiCal-chatgpt.py --source "$SOURCE_CAL_URL" --force-all-day
```

Increase verbosity (info/debug):
```bash
./convertiCal-chatgpt.py --source "$SOURCE_CAL_URL" -v    # info
./convertiCal-chatgpt.py --source "$SOURCE_CAL_URL" -vv   # debug
```

HTTP behavior flags:
- `--timeout 10` (seconds)
- `--retries 3` (transient errors)
- `--backoff 0.5` (exponential retry backoff)

## Publishing `output.ics` to a public repo
After generating a fresh `output.ics`, copy and push it to the repo that other calendars consume (example mirrors the original README flow):
```bash
cp -p ./output.ics ~/anonymized-availability-calendar
pushd ~/anonymized-availability-calendar/
git commit -a -m "updated output.ics"
git push
popd
```

## Cron Example
Run every 15 minutes, with logs captured. Adjust paths as needed.
```cron
*/15 * * * * cd /path/to/iCal-conversion-code && \
  /usr/bin/env bash -lc 'source venv/bin/activate && \
  ./convertiCal-chatgpt.py --source "$SOURCE_CAL_URL" --output ./output.ics -v && \
  cp -p ./output.ics ~/anonymized-availability-calendar && \
  cd ~/anonymized-availability-calendar && git commit -a -m "updated output.ics" && git push' \
  >> /var/log/ical-anonymizer.log 2>&1
```

Tip: set `SOURCE_CAL_URL` in the crontab environment or source it from a private file rather than hard-coding.

## GitHub Actions Workflow
An automated workflow in `.github/workflows/anonymize.yml` keeps `output.ics` fresh.

- What it does: on a 15‑minute schedule and on manual runs, it installs dependencies, runs the anonymizer, and commits `output.ics` only if it changed.
- Secret required: set `SOURCE_CAL_URL` in Settings → Security → Secrets and variables → Actions.
- Manual trigger: go to Actions → “Anonymize Calendar” → Run workflow.
- Permissions: workflow sets `contents: write` to push updates.
- Customize: edit the `cron` line for schedule, Python version, or commands.

Snippet:
```yaml
uses: actions/setup-python@v5
run: |
  pip install -r requirements.txt.easy_install
  python ./convertiCal-chatgpt.py --output output.ics
```
Commits use `chore: update output.ics [skip ci]` when there are changes.

## Notes & Behavior
- UID policy: the script assigns a deterministic UID derived from DTSTART/DTEND/recurrence info to preserve client deduplication across refreshes. This differs from the original, which deleted UIDs.
- Timezone: event timestamps are normalized to Asia/Tokyo (JST) with `TZID=Asia/Tokyo`, and a `VTIMEZONE` component is included. All-day events remain date-only.
- Overnight visibility: stays that cross midnight are emitted as all-day events with exclusive `DTEND` (checkout day not shaded), so month views in embedded Google Calendar show clear bars.
- Atomic writes: output is written to a temp file and then moved into place to avoid partial reads by consumers.
- Scrubbing: SUMMARY/ DESCRIPTION are set to "Unavailable" by default; pass `--summary` and `--description` to override. LOCATION is cleared unless `--keep-location` is set.
- Validation: the script re-parses the generated `.ics` to catch serialization issues early.
 - Trimming: events whose end time is more than 1 year in the past (relative to current JST) are omitted from the output to keep the calendar concise.

## Migrating from `convertiCal-grok.py`
- Replace existing invocations with `./convertiCal-chatgpt.py`.
- If you relied on deleted UIDs, be aware the new script uses anonymized, stable UIDs instead.
- Configure the source URL via `--source` or `SOURCE_CAL_URL` to avoid committing sensitive tokens.

## Troubleshooting
- Use `-vv` for detailed debug logs.
- HTTP 429/5xx errors are retried automatically; increase `--retries` or `--backoff` if needed.
- If a consumer treats all events as new, confirm that it honors UIDs and that the output file path is stable between runs.
