#!/usr/bin/env python3
#==============================================================================
# pylint: disable=too-many-locals, too-many-branches, too-many-statements
# pylint: disable=line-too-long, global-statement
#==============================================================================
"""Import Eversource 15-minute interval data into Home Assistant states table.
#
# App: Eversource Downloader and Importer
# File: import_electric_usage.py
# Version: 0.9.0
# Copyright Jeff Kosowsky
# Date: August 2026
#

Reads incremental kWh values from an Eversource CSV and UPDATES existing
'unknown' rows in the HA SQLite database so that the cumulative energy
sensor stays continuous.

Fully compliant with pylint and mypy --strict.
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# User Variables (constant)
# ---------------------------------------------------------------------------
INTERVAL_SECONDS: int = 900       # 15 minutes
TIMEOUT_SECONDS: int = 30         # Max wait for DB lock
MAX_MISSING_GAPS: int = 3         # Abort if more gaps than this in CSV
MAX_MISSING_DURATION: int = 3600  # Abort if any CSV file gap greater than this

DEFAULT_DB_PATH: str = "/homeassistant/home-assistant_v2.db"

# ---------------------------------------------------------------------------
# Other Global Variables
# ---------------------------------------------------------------------------

LOCAL_TZ: ZoneInfo

# ---------------------------------------------------------------------------
# Command-line interface
# ---------------------------------------------------------------------------
def parse_pos_int(value: str) -> int:
    """Accept a positive integer only."""
    try:
        number = int(value)
        if number > 0:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError(f"Argument must be positive integer: {value}")

parser = argparse.ArgumentParser(
    description="Fill HA energy sensor unknowns from Eversource CSV"
)

parser.add_argument(
    "-d", "--database",
    type=str,
    default=DEFAULT_DB_PATH,
    help=f"Path to home-assistant_v2.db [default: {DEFAULT_DB_PATH}]",
)
parser.add_argument(
    "-m", "--metadata-id",
    type=int,
    default=None,
    help="metadata_id of the energy sensor",
)
parser.add_argument(
    "-e", "--entity-id",
    type=str,
    default=None,
    help="entity_id of the energy sensor (alternative to -m)",
)
parser.add_argument(
    "-a", "--attrib-id",
    type=parse_pos_int,
    nargs="?",
    const=-1,   # bare '-a' (use last non-unknown/unavailable attributes_id)
    default=None,   # flag omitted (don't update attributes_id)
    help="If -a alone: use latest non-unknown/non-unavailable attributes_id; "
         "if -a N: use N (where N>0); if omitted: leave attributes_id unchanged",
)
parser.add_argument(
    "-t", "--test",
    action="store_true",
    help="Dry-run: perform all work then roll back the transaction",
)
parser.add_argument(
    "-v", "--verbose",
    action="count",
    default=0,
    help="Increase verbosity (-v, -vv, ...)",
)
parser.add_argument(
    "-f", "--file",
    type=str,
    required=True,
    help="Path to the Eversource energy_data.csv file",
)

args = parser.parse_args()

# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------
def log(level: int, msg: str) -> None:
    """Print a timestamped message when verbosity is high enough."""
    if args.verbose >= level:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] {msg}")

def fmt_ts(ts: float, no_sec: bool = False) -> str:
    """Return a human-readable local timestamp string."""    
    dt = datetime.fromtimestamp(ts, tz=LOCAL_TZ)
    if no_sec:
        return dt.strftime("%Y-%m-%d %H:%M %Z")
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")

def _last_csv_entry_summary(path: str) -> str:
    """Return a short 'YYYY-MM-DD HH:MM-HH:MM' string for the last CSV row."""
    last_line = ""
    with open(path, "r", encoding="utf-8", newline="") as fh:
        for line in fh:
            if line.strip():
                last_line = line
    # Expected format after header: TYPE,DATE,START,END,...
    parts = last_line.strip().split(",")
    if len(parts) >= 4:
        return f"{parts[1]} {parts[2]}-{parts[3]} [{parts[4]} kWh]"
    return "unknown"


# ---------------------------------------------------------------------------
# Data classes for clarity
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CsvInterval:
    """One row from the Eversource CSV."""
    end_ts: float          # Unix timestamp of the interval end
    delta_kwh: float       # incremental usage for this interval
    start_str: str         # original start time string for logging
    end_str: str           # original end time string for logging
    date_str: str          # original date string for logging


@dataclass
class HaUnknown:
    """An existing 'unknown' row that we may update."""
    state_id: int
    last_updated_ts: float


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def connect_db(path: str) -> sqlite3.Connection:
    """Open the HA database with a sensible timeout."""
    log(2, f"Opening database: {path}")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Database not found: {path}")
    conn = sqlite3.connect(path, timeout=TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    return conn


def resolve_sensor_ids(
    conn: sqlite3.Connection,
    metadata_id: int | None,
    entity_id: str | None,
) -> tuple[int, str]:
    """Return (metadata_id, entity_id).  Exactly one of the arguments
    must be supplied; the other is looked up.
    """
    if metadata_id is not None and entity_id is not None:
        raise ValueError("Supply either -m/--metadata-id or -e/--entity-id, not both")
    if metadata_id is None and entity_id is None:
        raise ValueError("One of -m/--metadata-id or -e/--entity-id is required")

    cur = conn.cursor()
    if metadata_id is not None:
        cur.execute(
            "SELECT entity_id FROM states_meta WHERE metadata_id = ?",
            (metadata_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"metadata_id {metadata_id} not found in states_meta")
        entity_id = str(row["entity_id"])
    else:
        assert entity_id is not None  # for mypy
        cur.execute(
            "SELECT metadata_id FROM states_meta WHERE entity_id = ?",
            (entity_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"entity_id {entity_id!r} not found in states_meta")
        metadata_id = int(row["metadata_id"])

    return metadata_id, entity_id

def resolve_attrib_id(
    conn: sqlite3.Connection,
    metadata_id: int,
    explicit: int | None,
) -> int | None:
    """Return attributes_id to update for every imported row."""
    if explicit is None:
        return None  # -a not given, so don't set attributes_id
    if explicit > 0:
        return explicit

    # Otherwise, retrieve latest non-unknown/unavailable attributes_id for given sensor
    cur = conn.cursor()
    cur.execute(
        """
        SELECT attributes_id
          FROM states
         WHERE metadata_id = ?
           AND state NOT IN ('unknown', 'unavailable')
         ORDER BY last_updated_ts DESC, state_id DESC
         LIMIT 1
        """,
        (metadata_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise RuntimeError(
            f"No non-unknown/non-unavailable state for metadata_id={metadata_id}; "
            "specify -a/--attrib-id"
        )
    return int(row["attributes_id"])

def get_last_good_state(
    conn: sqlite3.Connection,
    metadata_id: int,
) -> tuple[float | None, float | None]:
    """Return (last_updated_ts, state_value) of the newest non-unknown row.

    Returns (None, None) when no good row exists yet.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT last_updated_ts, state
          FROM states
         WHERE metadata_id = ?
           AND state != 'unknown'
         ORDER BY last_updated_ts DESC
         LIMIT 1
        """,
        (metadata_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None, None

    ts = float(row["last_updated_ts"])
    try:
        value = float(row["state"])
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Last good state is not numeric: {row['state']!r}"
        ) from exc

    return ts, value


def load_unknowns_after(
    conn: sqlite3.Connection,
    metadata_id: int,
    after_ts: float | None,
) -> list[HaUnknown]:
    """Return all 'unknown' rows with last_updated_ts >= after_ts, ordered."""
    if after_ts is None:
        after_ts = 0.0

    cur = conn.cursor()
    cur.execute(
        """
        SELECT state_id, last_updated_ts
          FROM states
         WHERE metadata_id = ?
           AND state = 'unknown'
           AND last_updated_ts >= ?
         ORDER BY last_updated_ts ASC
        """,
        (metadata_id, after_ts),
    )
    result = [
        HaUnknown(state_id=int(r["state_id"]), last_updated_ts=float(r["last_updated_ts"]))
        for r in cur.fetchall()
    ]
    log(2, f"Found {len(result)} unknown rows after {after_ts}")
    return result


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------
def end_ts_from_csv(date_str: str, end_str: str, prev_end: float | None, tz: ZoneInfo) -> float:
    """ Treat dst transitions properly"""
    naive = datetime.strptime(f"{date_str} {end_str}", "%Y-%m-%d %H:%M")
    candidates = [
        naive.replace(tzinfo=tz, fold=0).timestamp(),
        naive.replace(tzinfo=tz, fold=1).timestamp(),
    ]
    # Unique (non-ambiguous times yield the same ts twice)
    candidates = sorted(set(candidates))

    if prev_end is None:
        return candidates[0]

    # Prefer the candidate ~ one interval after prev_end
    def score(ts: float) -> float:
        return abs((ts - prev_end) - INTERVAL_SECONDS)

    viable = [ts for ts in candidates if ts > prev_end - 1.0]
    if not viable:
        return max(candidates)
    return min(viable, key=score)

def load_csv_intervals(path: str, after_ts: float | None) -> list[CsvInterval]:
    """Parse the Eversource CSV and return intervals whose end_time > after_ts.

    The CSV columns are expected to be:
        TYPE,DATE,START TIME,END TIME,USAGE (kWh),COST,NOTES
    """
    log(2, f"Reading CSV: {path}")
    if after_ts is None:
        after_ts = 0.0
    intervals: list[CsvInterval] = []
    missing_gaps = 0
    prev_end: float | None = None

    with open(path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise RuntimeError("CSV has no header row")
        field_map = {name.strip().upper(): name for name in reader.fieldnames}

        required = ["DATE", "START TIME", "END TIME", "USAGE (KWH)"]
        for col in required:
            if col not in field_map:
                raise RuntimeError(f"CSV missing required column: {col}")

        for row in reader:
            date_str = row[field_map["DATE"]].strip()
            start_str = row[field_map["START TIME"]].strip()
            end_str = row[field_map["END TIME"]].strip()
            usage_str = row[field_map["USAGE (KWH)"]].strip()

            end_ts = end_ts_from_csv(date_str, end_str, prev_end, LOCAL_TZ)

            if end_ts <= after_ts:
                prev_end = end_ts
                continue

            try:
                delta = float(usage_str)
            except ValueError as exc:
                raise RuntimeError(f"Bad USAGE value: {usage_str!r}") from exc

            # Gap detection between consecutive CSV rows
            if prev_end is not None:
                gap = end_ts - prev_end
                if gap > INTERVAL_SECONDS + 1:  # allow 1 s tolerance
                    missing_gaps += 1
                    if gap > MAX_MISSING_DURATION:
                        raise RuntimeError(
                            f"Large CSV gap detected: {fmt_ts(prev_end)} ({prev_end}) -> "
                            f"{fmt_ts(end_ts)} ({end_ts}) [{gap:.0f}s > {MAX_MISSING_DURATION}s]")
                    log(1, f"CSV gap detected: {fmt_ts(prev_end)} ({prev_end}) -> {fmt_ts(end_ts)} ({end_ts}) [{gap:.0f}s]")
            prev_end = end_ts

            intervals.append(
                CsvInterval(
                    end_ts=end_ts,
                    delta_kwh=delta,
                    start_str=start_str,
                    end_str=end_str,
                    date_str=date_str,
                )
            )

    if missing_gaps > MAX_MISSING_GAPS:
        raise RuntimeError(
            f"Too many gaps in CSV ({missing_gaps} > {MAX_MISSING_GAPS})"
        )

    return intervals


# ---------------------------------------------------------------------------
# Core update logic
# ---------------------------------------------------------------------------
def update_row(
cur: sqlite3.Cursor,
    target: HaUnknown,
    cumulative: float,
    pending_delta: float,
    attributes_id: int | None,
) -> float:
    """Update state, attributes_id, and last_changed_ts for given row"""

    cumulative += pending_delta
    new_state = f"{cumulative:.2f}"
    log(2, f"UPDATE state_id={target.state_id} "
        f"ts={target.last_updated_ts} delta={pending_delta:.2f} -> {new_state}")
    if attributes_id is None:
        cur.execute(
            """
            UPDATE states
               SET state = ?,
                   last_changed_ts = last_updated_ts
             WHERE state_id = ?
            """,
            (new_state, target.state_id),
        )
    else:
        cur.execute(
            """
            UPDATE states
               SET state = ?,
                   attributes_id = ?,
                   last_changed_ts = last_updated_ts
             WHERE state_id = ?
            """,
            (new_state, attributes_id, target.state_id),
        )
    return cumulative

def apply_intervals(
    conn: sqlite3.Connection,
    intervals: list[CsvInterval],
    start_value: float,
    unknowns: list[HaUnknown],
    attributes_id: int | None,
) -> tuple[int, float, float | None]:
    """Walk the CSV intervals and UPDATE matching unknown HA rows.

    Returns (rows_updated, final_cumulative, last_updated_ts).
    """
    if not intervals:
        return 0, start_value, None

    cumulative = start_value
    unknown_idx = 0
    updated = 0
    last_ts: float | None = None
    pending_delta = 0.0
    pending_target: HaUnknown | None = None

    cur = conn.cursor()

    for i, iv in enumerate(intervals):
        # Advance until we find an unknown row >= this interval's end
        while (unknown_idx < len(unknowns)
               and unknowns[unknown_idx].last_updated_ts < iv.end_ts):
            unknown_idx += 1

        if unknown_idx >= len(unknowns):
            log(1, f"No unknown HA row after {iv.end_ts} - stopping")
            break

        target = unknowns[unknown_idx]

        # If we switched to a new HA row, flush the previous accumulation
        if pending_target is not None and pending_target.state_id != target.state_id:
            cumulative = update_row(cur, pending_target, cumulative, pending_delta, attributes_id)
            updated += 1
            last_ts = pending_target.last_updated_ts
            pending_delta = 0.0

        pending_delta += iv.delta_kwh
        pending_target = target

        # Decide whether to flush now: if this is the last interval or
        # the next interval maps to a different HA row.
        flush_now = True
        if i + 1 < len(intervals):
            next_iv = intervals[i + 1]
            tmp_idx = unknown_idx
            while (tmp_idx < len(unknowns)
                   and unknowns[tmp_idx].last_updated_ts < next_iv.end_ts):
                tmp_idx += 1
            if (tmp_idx < len(unknowns)
                    and unknowns[tmp_idx].state_id == target.state_id):
                flush_now = False

        if flush_now and pending_target is not None:
            cumulative = update_row(cur, pending_target, cumulative, pending_delta, attributes_id)
            updated += 1
            last_ts = pending_target.last_updated_ts
            pending_delta = 0.0
            pending_target = None

    return updated, cumulative, last_ts


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def main() -> None:
    """Orchestrate the whole import."""

    try:
        conn = connect_db(args.database)
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}")
        sys.exit(1)

    try:
        global LOCAL_TZ
        name = os.environ.get("TZ")
        if not name:
            raise RuntimeError("TZ environment variable is not set")
        LOCAL_TZ = ZoneInfo(name)

        # ----- resolve identifiers -----
        metadata_id, entity_id = resolve_sensor_ids(
            conn, args.metadata_id, args.entity_id
        )

        attributes_id = resolve_attrib_id(conn, metadata_id, args.attrib_id)

        # ----- find last good HA state -----
        last_ts, last_value = get_last_good_state(conn, metadata_id)
        if last_value is None:
            last_value = 0.0

        # ----- load CSV intervals newer than last good HA timestamp -----
        intervals = load_csv_intervals(args.file, last_ts)

        # ----- load candidate unknown rows -----
        unknowns = load_unknowns_after(conn, metadata_id, last_ts)

        # ----- header banner -----
        ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if args.test:
            print(f"[{ts_now}] ** DRY RUN **")

        log(1,
            f"Electricity sensor: {entity_id} "
            f"(metadata_id={metadata_id}, attributes_id={attributes_id if attributes_id is not None else 'unchanged'})")

        if last_ts is None:
            log(1, "Starting HA total (kWh): 0.0 [no prior state data]")
        else:
            log(1, f"Starting HA total (kWh): {last_value:.2f} [{fmt_ts(last_ts)} ({last_ts})]")

        if not intervals:
            last_csv_msg = _last_csv_entry_summary(args.file)
            log(1, f"No new CSV intervals to apply. Last CSV entry: {last_csv_msg}")
            return

        first = intervals[0]
        last = intervals[-1]
        log(1, f"Loaded {len(intervals)} new CSV intervals: "
            f"{first.date_str} {first.start_str}-{first.end_str} --> {last.date_str} {last.start_str}-{last.end_str}")

        if not unknowns:
            print("[ERROR] No 'unknown' rows found after last good timestamp")
            sys.exit(2)

        # ----- begin transaction -----
        log(2, "BEGIN IMMEDIATE")
        conn.execute("BEGIN IMMEDIATE")

        updated, final_value, last_written_ts = apply_intervals(conn, intervals, last_value, unknowns, attributes_id)

        if updated > 0 and last_written_ts is not None:
            # Reconstruct approximate first applied HA timestamp for the log
            first_applied_ts = unknowns[0].last_updated_ts
            log(1, f"Applied {updated} new CSV intervals: "
                f"{fmt_ts(first_applied_ts)} ({first_applied_ts}) --> {fmt_ts(last_written_ts)} ({last_written_ts})")
        else:
            log(1, f"Applied {updated} new CSV intervals")

        if last_written_ts is not None:
            log(1, f"Ending HA total (kWh): {final_value:.2f} [{fmt_ts(last_written_ts)} ({last_written_ts})]")
        else:
            log(1, f"Ending HA total (kWh): {final_value:.2f}")

        if args.test:
            conn.rollback()
            log(1, "Transaction rolled back (no changes written)")
        else:
            conn.commit()
            log(1, "Transaction committed successfully!")

    except Exception as exc:  # pylint: disable=broad-except
        print(f"[ERROR] {exc}")
        try:
            conn.rollback()
        except Exception:  # pylint: disable=broad-except
            pass
        sys.exit(3)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
