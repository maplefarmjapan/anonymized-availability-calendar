#!/usr/bin/env python3

"""
Improved iCal anonymizer

Features:
- Configurable source/output via CLI flags and environment variables
- Robust HTTP fetching with retries and timeouts
- Deterministic anonymized UIDs to preserve client deduplication
- Broader anonymization of sensitive properties (summary, description, attendees, etc.)
- Atomic file writes to avoid partial updates

Environment variables (fallbacks for CLI):
- SOURCE_CAL_URL: iCal source URL
- OUTPUT_CAL_PATH: Path to write anonymized iCal (default: ./output.ics)
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
from datetime import date, datetime, timezone
from tempfile import NamedTemporaryFile
from typing import Optional

import requests
from icalendar import Calendar, vDatetime, Timezone, TimezoneStandard
from zoneinfo import ZoneInfo
from dateutil.relativedelta import relativedelta
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


DEFAULT_OUTPUT = "./output.ics"


def build_session(retries: int, backoff: float) -> requests.Session:
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods={"GET"},
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    sess = requests.Session()
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)
    sess.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0 Safari/537.36"
            )
        }
    )
    return sess


def fetch_ical(url: str, timeout: float, retries: int, backoff: float) -> bytes:
    session = build_session(retries=retries, backoff=backoff)
    resp = session.get(url, timeout=timeout)
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        # Log response snippet for diagnostics
        snippet = resp.text[:200] if hasattr(resp, "text") else ""
        logging.error("HTTP error fetching iCal: %s | %s", e, snippet)
        raise
    return resp.content


def _norm_dt(value) -> str:
    # value may be date or datetime (possibly tz-aware)
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return f"{value.isoformat()}(DATE)"
    return str(value)


def make_anonymized_uid(component) -> str:
    dtstart = component.get("DTSTART")
    dtend = component.get("DTEND")
    duration = component.get("DURATION")
    rrule = component.get("RRULE")
    rdate = component.get("RDATE")
    exdate = component.get("EXDATE")
    rid = component.get("RECURRENCE-ID")

    parts = [
        _norm_dt(dtstart.dt) if dtstart is not None else "",
        _norm_dt(dtend.dt) if dtend is not None else "",
        str(duration) if duration is not None else "",
        str(rrule) if rrule is not None else "",
        str(rdate) if rdate is not None else "",
        str(exdate) if exdate is not None else "",
        _norm_dt(rid.dt) if rid is not None else "",
    ]
    basis = "|".join(parts)
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]
    return f"anon-{digest}@anonymized"


SENSITIVE_PROPS = [
    "ORGANIZER",
    "ATTENDEE",
    "CONTACT",
    "URL",
    "COMMENT",
    "RESOURCES",
    "GEO",
    "CATEGORIES",
    "RELATED-TO",
    "ATTACH",
]


def remove_all(component, prop: str) -> None:
    # Repeated properties may exist; loop until gone
    while prop in component:  # type: ignore[operator]
        try:
            component.pop(prop)
        except Exception:
            # Some versions require del
            try:
                del component[prop]  # type: ignore[index]
            except Exception:
                break


def _to_jst(dt):
    jst = ZoneInfo("Asia/Tokyo")
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            # Treat naive times as local JST
            return dt.replace(tzinfo=jst)
        return dt.astimezone(jst)
    return dt


def _normalize_calendar_metadata(cal: Calendar) -> None:
    # Neutral, non-identifying calendar metadata
    cal["PRODID"] = "-//anonymized-availability//ical-anonymizer//EN"
    cal["CALSCALE"] = "GREGORIAN"
    # Drop potentially identifying headers often set by sources/clients
    for key in (
        "X-WR-CALNAME",
        "X-WR-CALDESC",
        "REFRESH-INTERVAL",
        "X-PUBLISHED-TTL",
    ):
        try:
            del cal[key]
        except Exception:
            pass

    # Ensure calendar advertises JST
    cal["X-WR-TIMEZONE"] = "Asia/Tokyo"


def _ensure_vtimezone_jst(cal: Calendar) -> None:
    # If a JST VTIMEZONE exists, keep it
    try:
        for tz in cal.walk("VTIMEZONE"):
            tzid = tz.get("TZID")
            if str(tzid) == "Asia/Tokyo":
                return
    except Exception:
        pass

    # Add a minimal JST VTIMEZONE definition
    vtz = Timezone()
    vtz.add("tzid", "Asia/Tokyo")
    std = TimezoneStandard()
    std.add("dtstart", datetime(1951, 9, 9, 0, 0, 0))
    std.add("tzname", "JST")
    std.add("tzoffsetfrom", "+1000")
    std.add("tzoffsetto", "+0900")
    vtz.add_component(std)
    cal.add_component(vtz)


def anonymize_calendar(cal: Calendar, summary_text: str, description_text: str, clear_location: bool) -> Calendar:
    _normalize_calendar_metadata(cal)
    _ensure_vtimezone_jst(cal)
    to_remove = []
    jst = ZoneInfo("Asia/Tokyo")
    now_jst = datetime.now(tz=jst)
    cutoff = now_jst - relativedelta(years=1)

    def _as_dt_jst(value):
        # Convert date or datetime to aware datetime in JST
        if isinstance(value, datetime):
            return _to_jst(value)
        if isinstance(value, date):
            # Treat DATE values as end-of-day for inclusive comparison
            return datetime(value.year, value.month, value.day, 23, 59, 59, tzinfo=jst)
        return now_jst  # fallback safe default

    for component in cal.walk("VEVENT"):
        component["SUMMARY"] = summary_text
        component["DESCRIPTION"] = description_text

        # Remove or clear other potentially identifying fields
        for p in SENSITIVE_PROPS:
            remove_all(component, p)

        if clear_location:
            remove_all(component, "LOCATION")

        # Normalize key timestamps to JST and set TZID
        for key in ("DTSTART", "DTEND", "RECURRENCE-ID", "DTSTAMP"):
            prop = component.get(key)
            if prop is None:
                continue
            try:
                value = getattr(prop, "dt", prop)
                value_jst = _to_jst(value)
                if isinstance(value_jst, datetime):
                    component[key] = vDatetime(value_jst)
                    try:
                        component[key].params["TZID"] = "Asia/Tokyo"
                    except Exception:
                        pass
                else:
                    component[key] = value_jst
            except Exception:
                # If normalization fails, keep original value but continue
                pass

        # Convert overnight stays to all-day bars (JST) for better visibility in Google Calendar
        try:
            jst = ZoneInfo("Asia/Tokyo")
            ds = component.get("DTSTART")
            de = component.get("DTEND")
            ds_val = getattr(ds, "dt", None) if ds is not None else None
            de_val = getattr(de, "dt", None) if de is not None else None
            if isinstance(ds_val, datetime) and isinstance(de_val, datetime):
                ds_local = ds_val.astimezone(jst)
                de_local = de_val.astimezone(jst)
                if de_local.date() > ds_local.date():
                    # Overnight or multi-night: use all-day exclusive dates
                    component["DTSTART"] = ds_local.date()
                    component["DTEND"] = de_local.date()
                    # Remove TZID if present (date-only values should not carry TZID)
                    try:
                        if hasattr(component["DTSTART"], "params"):
                            component["DTSTART"].params.pop("TZID", None)
                        if hasattr(component["DTEND"], "params"):
                            component["DTEND"].params.pop("TZID", None)
                    except Exception:
                        pass
                    # Mark as opaque (busy)
                    component["TRANSP"] = "OPAQUE"
        except Exception:
            # Non-fatal if we can't convert; keep event as-is
            pass

        # Reset SEQUENCE to a stable value and set anonymized UID
        if "SEQUENCE" in component:
            component["SEQUENCE"] = 0

        component["UID"] = make_anonymized_uid(component)

        # Determine event end for trimming
        dtend_prop = component.get("DTEND")
        dtstart_prop = component.get("DTSTART")
        end_dt = None
        if dtend_prop is not None:
            end_dt = _as_dt_jst(getattr(dtend_prop, "dt", dtend_prop))
        elif dtstart_prop is not None:
            end_dt = _as_dt_jst(getattr(dtstart_prop, "dt", dtstart_prop))

        if end_dt is not None and end_dt < cutoff:
            to_remove.append(component)

    # Remove old events
    for comp in to_remove:
        try:
            cal.subcomponents.remove(comp)  # type: ignore[attr-defined]
        except Exception:
            pass

    return cal


def atomic_write_bytes(path: str, data: bytes) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    with NamedTemporaryFile("wb", delete=False, dir=directory, prefix=".tmp-", suffix=".ics") as tmp:
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = tmp.name
    os.replace(tmp_path, path)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch and anonymize an iCal feed")
    parser.add_argument(
        "--source",
        dest="source",
        default=os.getenv("SOURCE_CAL_URL", None),
        help="Source iCal URL (can also set SOURCE_CAL_URL)",
    )
    parser.add_argument(
        "--output",
        dest="output",
        default=os.getenv("OUTPUT_CAL_PATH", DEFAULT_OUTPUT),
        help="Output .ics path (default: ./output.ics or OUTPUT_CAL_PATH)",
    )
    parser.add_argument(
        "--summary",
        dest="summary",
        default="Unavailable",
        help='Replacement SUMMARY text (default: "Unavailable")',
    )
    parser.add_argument(
        "--description",
        dest="description",
        default="Unavailable",
        help='Replacement DESCRIPTION text (default: "Unavailable")',
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="HTTP timeout in seconds (default: 10)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="HTTP retries for transient errors (default: 3)",
    )
    parser.add_argument(
        "--backoff",
        type=float,
        default=0.5,
        help="Exponential backoff factor between retries (default: 0.5)",
    )
    parser.add_argument(
        "--keep-location",
        dest="clear_location",
        action="store_false",
        help="Keep LOCATION instead of clearing it",
    )
    parser.set_defaults(clear_location=True)
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase verbosity (can repeat)",
    )
    return parser.parse_args(argv)


def configure_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity >= 2:
        level = logging.DEBUG
    elif verbosity == 1:
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose)

    source = args.source or os.getenv("SOURCE_CAL_URL")
    if not source:
        print("Error: Source URL is required. Provide --source or set SOURCE_CAL_URL.", file=sys.stderr)
        return 2

    try:
        logging.info("Fetching calendar from %s", source)
        ical_bytes = fetch_ical(source, timeout=args.timeout, retries=args.retries, backoff=args.backoff)

        logging.debug("Parsing calendar bytes (%d bytes)", len(ical_bytes))
        cal = Calendar.from_ical(ical_bytes)

        logging.info("Anonymizing events")
        cal = anonymize_calendar(
            cal,
            summary_text=args.summary,
            description_text=args.description,
            clear_location=args.clear_location,
        )

        # Validate by re-parsing serialized output
        out_bytes = cal.to_ical()
        _ = Calendar.from_ical(out_bytes)

        logging.info("Writing output to %s atomically", args.output)
        atomic_write_bytes(args.output, out_bytes)

        print(f"Calendar anonymized successfully to {args.output}")
        return 0
    except Exception as e:
        logging.exception("Failed to process calendar: %s", e)
        print(f"Error processing calendar: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
