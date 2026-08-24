#!/usr/bin/env python3
#===============================================================================
# pylint: disable=too-many-arguments, too-many-positional-arguments, too-many-locals
# pylint: disable=too-many-branches, too-many-statements
# pylint: disable=line-too-long
#===============================================================================
"""Recalculate Home Assistant 'sum' statistics from a start timestamp.
#
# App: Eversource Downloader and Importer
# File: redo_sum_statistics.py
# Version: 0.9.0
# Copyright Jeff Kosowsky
# Date: August 2026
#

Mirrors the Home Assistant recorder's sum-statistic compiler as closely as
possible.  Source of truth is the states table.

Algorithm
---------
1. Resolve statistic_id / metadata_id / entity_id (exactly one must be given).
2. Confirm the statistic is a sum type (has_sum = 1).
3. For each requested table (statistics_short_term and/or statistics):
   a. Start with the first statistic bucket containing start_ts.
      If start_ts is omitted, start with the bucket that contains the most
      recent statistic for that table.
   b. Initialize running sum/state from the last existing stats row before
      the range.  If none exists, use the newest numeric state before the
      range.  If none exists, sum = state = 0.
   c. Walk every bucket in chronological order.
      For each bucket:
        - If the bucket ends in a numeric state, or there is no state in the
          bucket but a previous statistic exists:
              INSERT/UPDATE the bucket with the current sum/state
              (carry forward when there is no new state).
        - Otherwise:
              DELETE any existing statistics row for that bucket.
      Note: a drop of >= 10% between consecutive numeric states is treated
      as a meter reset (HA total_increasing rule).
   d. Fill through the earlier of:
        - the last completed bucket before now, and
        - the bucket just before the first unknown/unavailable state.
      Then DELETE any statistics rows from the first unknown/unavailable
      bucket through the end of existing data.
4. Commit the SQLite transaction unless -t/--test.

CLI
---
  redo_sum_statistics.py [-d DB] [-t] [-v]
      (-s STAT_ID | -m META_ID | -e ENTITY)
      [-T short|long|both] [START_TS]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime
from decimal import Decimal

# ---------------------------------------------------------------------------
# Constants matching Home Assistant
# ---------------------------------------------------------------------------
SHORT_SEC = 5 * 60
LONG_SEC = 60 * 60
TIMEOUT = 30
DEFAULT_DB = "/homeassistant/home-assistant_v2.db"
RESET_RATIO = Decimal("0.9")  # >= 10% drop => meter reset

# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser(
    description="Recalculate HA sum statistics from a start timestamp"
)
parser.add_argument(
    "start_ts", type=float, nargs="?", default=None,
    help="Unix timestamp; start at the bucket that contains it. "
         "If omitted, each table starts at its own newest numeric stats row.",
)
parser.add_argument(
    "-T", "--table", default="both", choices=("short", "long", "both"),
    help="statistics_short_term, statistics, or both",
)
parser.add_argument("-d", "--database", default=DEFAULT_DB,
                    help="HA database path")
parser.add_argument("-s", "--statistic-id", type=int, default=None,
                    help="statistics_meta.id")
parser.add_argument("-m", "--metadata-id", type=int, default=None,
                    help="states_meta.metadata_id")
parser.add_argument("-e", "--entity-id", type=str, default=None,
                    help="entity_id")
parser.add_argument("-t", "--test", action="store_true",
                    help="Dry-run: roll back at the end")
parser.add_argument("-v", "--verbose", action="count", default=0,
                    help="Verbosity (repeat for more detail)")
args = parser.parse_args()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def log(level: int, msg: str) -> None:
    """Timestamped line controlled by -v."""
    if args.verbose >= level:
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}")


def fmt(ts: float) -> str:
    """Local-time string for a Unix timestamp."""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def last_start_ts(conn: sqlite3.Connection, table: str, sid: int) -> float:
    """Newest start_ts in *table* that has a non-NULL numeric state."""
    cur = conn.cursor()
    cur.execute(
        f"SELECT start_ts FROM {table} "
        f"WHERE metadata_id=? AND state IS NOT NULL "
        f"ORDER BY start_ts DESC LIMIT 1",
        (sid,),
    )
    row = cur.fetchone()
    if not row:
        raise SystemExit(
            f"No valid statistics row in {table} -- cannot infer start_ts"
        )
    return float(row[0])


def state_endpoints(conn: sqlite3.Connection, mid: int, table: str) -> tuple[float | None, float | None, float | None]:
    """Return (last_numeric_ts, first_invalid_ts, last_state_ts)."""
    cur = conn.cursor()

    cur.execute(
        "SELECT last_updated_ts, state FROM states "
        "WHERE metadata_id=? AND state NOT IN ('unknown','unavailable') "
        "ORDER BY last_updated_ts DESC LIMIT 1",
        (mid,),
    )
    row = cur.fetchone()
    last_numeric = float(row[0]) if row else None
    last_numeric_val = row[1] if row else None

    first_invalid = None
    if last_numeric is not None:
        cur.execute(
            "SELECT MIN(last_updated_ts) FROM states "
            "WHERE metadata_id=? AND last_updated_ts > ? "
            "AND state IN ('unknown','unavailable')",
            (mid, last_numeric),
        )
        inv = cur.fetchone()[0]
        first_invalid = float(inv) if inv is not None else None

    cur.execute(
        "SELECT MAX(last_updated_ts) FROM states WHERE metadata_id=?",
        (mid,),
    )
    ls = cur.fetchone()[0]
    last_state = float(ls) if ls is not None else None

    if last_numeric is not None:
        log(2, f"Last numeric state : {fmt(last_numeric)} "
            f"(state={last_numeric_val})")
    else:
        log(2, "Last numeric state : None")

    if first_invalid is not None:
        last_short = (int(first_invalid) // SHORT_SEC) * SHORT_SEC - SHORT_SEC
        last_long = (int(first_invalid) // LONG_SEC) * LONG_SEC - LONG_SEC
        log(2, f"First invalid state: {fmt(first_invalid)} "
            f"=> Last bucket start: {f'(short) {fmt(last_short)}' if table in ['short', 'both'] else ''}"
            f"{ ', ' if table == 'both' else ''}"
            f"{f'(long) {fmt(last_long)}' if table in ['long', 'both'] else ''}")
    else:
        log(2, "First invalid state: None")

    log(2, f"Last state overall : "
        f"{fmt(last_state) if last_state else 'None'}")
    return last_numeric, first_invalid, last_state

# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------
def resolve(conn: sqlite3.Connection) -> tuple[int, int, str]:
    """Return (statistic_id, metadata_id, entity_id)."""
    n = sum(x is not None for x in
            (args.statistic_id, args.metadata_id, args.entity_id))
    if n != 1:
        raise SystemExit("Supply exactly one of -s / -m / -e")

    cur = conn.cursor()
    sid, mid, eid = args.statistic_id, args.metadata_id, args.entity_id

    if eid is not None:
        cur.execute("SELECT metadata_id FROM states_meta WHERE entity_id=?",
                    (eid,))
        row = cur.fetchone()
        if not row:
            raise SystemExit(f"entity_id {eid!r} not found")
        mid = int(row[0])

    if mid is not None and sid is None:
        cur.execute(
            "SELECT sm.id FROM statistics_meta sm "
            "JOIN states_meta st ON sm.statistic_id = st.entity_id "
            "WHERE st.metadata_id = ?", (mid,),
        )
        row = cur.fetchone()
        if not row:
            raise SystemExit(f"No statistics_meta row for metadata_id={mid}")
        sid = int(row[0])

    if sid is not None and mid is None:
        cur.execute(
            "SELECT st.metadata_id, st.entity_id FROM states_meta st "
            "JOIN statistics_meta sm ON sm.statistic_id = st.entity_id "
            "WHERE sm.id = ?", (sid,),
        )
        row = cur.fetchone()
        if not row:
            raise SystemExit(f"No states_meta row for statistic_id={sid}")
        mid, eid = int(row[0]), str(row[1])

    if eid is None:
        cur.execute("SELECT entity_id FROM states_meta WHERE metadata_id=?",
                    (mid,))
        row = cur.fetchone()
        if not row:
            raise SystemExit(f"metadata_id {mid} not found")
        eid = str(row[0])

    cur.execute("SELECT has_sum FROM statistics_meta WHERE id=?", (sid,))
    row = cur.fetchone()
    if not row or not row[0]:
        raise SystemExit(f"statistic_id={sid} is not a sum statistic")

    assert sid is not None and mid is not None and eid is not None
    return sid, mid, eid


# ---------------------------------------------------------------------------
# Core: recalculate one statistics table
# ---------------------------------------------------------------------------
def process(
    conn: sqlite3.Connection,
    table: str,
    bucket: int,
    sid: int,
    mid: int,
    start_ts: float,
    last_numeric_ts: float | None,
    first_invalid_ts: float | None,
    last_state_ts: float | None,
) -> tuple[int, int, int]:
    """Recalculate *table* from *start_ts* onward.

    Returns (rows_updated, rows_inserted, rows_deleted).
    """
    cur = conn.cursor()
    start_bucket = (int(start_ts) // bucket) * bucket

    # Last completed bucket before now (HA does not finalise the open period)
    now_bucket = (int(datetime.now().timestamp()) // bucket) * bucket
    last_completed = now_bucket - bucket

    # Fill through the earlier of: last completed bucket, or bucket before
    # the first unknown/unavailable.
    fill_end = last_completed
    if last_numeric_ts is not None:
        fill_end = max(fill_end, (int(last_numeric_ts) // bucket) * bucket)
    if first_invalid_ts is not None:
        inv_bucket = (int(first_invalid_ts) // bucket) * bucket
        fill_end = min(fill_end, inv_bucket - bucket)

    # Delete from first invalid bucket (if any) through the far end of data
    cur.execute(f"SELECT MAX(start_ts) FROM {table} WHERE metadata_id=?",
                (sid,))
    last_stat_ts = cur.fetchone()[0]
    delete_through_candidates = [
        t for t in (last_state_ts, last_stat_ts, last_completed)
        if t is not None
    ]
    delete_through = (
        (int(max(delete_through_candidates)) // bucket) * bucket
        if delete_through_candidates else start_bucket
    )

    if fill_end < start_bucket and (
            first_invalid_ts is None
            or (int(first_invalid_ts) // bucket) * bucket < start_bucket):
        log(1, f"{table}: nothing to do")
        return 0, 0, 0

    label = "Short term" if table == "statistics_short_term" else "Long term"
    log(1, f"{label}: {fmt(start_bucket)} -> {fmt(fill_end)}")

    # ---- initial sum/state from stats and/or states ----------------------
    prior_sum_stats: Decimal | None = None
    prior_state_stats: Decimal | None = None
    prior_state_states: Decimal | None = None
    last_reset: float | None = None
    prev_exists = False

    cur.execute(
        f"SELECT sum, state, last_reset_ts FROM {table} "
        f"WHERE metadata_id=? AND start_ts<? "
        f"ORDER BY start_ts DESC LIMIT 1",
        (sid, start_bucket),
    )
    row = cur.fetchone()
    if row and row[0] is not None and row[1] is not None:
        prior_sum_stats = Decimal(str(row[0]))
        prior_state_stats = Decimal(str(row[1]))
        prev_exists = True
        if row[2] is not None:
            last_reset = float(row[2])
        log(2, f"  Initial value (from stats):  state={prior_state_stats} "
            f"sum={prior_sum_stats} last_reset="
            f"{last_reset if last_reset is not None else 'None'}")
    else:
        log(2, "  Initial value (from stats): NA")

    cur.execute(
        "SELECT state FROM states "
        "WHERE metadata_id=? AND last_updated_ts<? "
        "AND state NOT IN ('unknown','unavailable') "
        "ORDER BY last_updated_ts DESC LIMIT 1",
        (mid, start_bucket),
    )
    row = cur.fetchone()
    if row is not None:
        prior_state_states = Decimal(str(row[0]))
        prev_exists = True
        log(2, f"  Initial value (from states): state={prior_state_states}")
    else:
        log(2, "  Initial value (from states): NA")

    if (prior_state_stats is not None and prior_state_states is not None
            and prior_state_stats != prior_state_states):
        raise SystemExit(
            f"{table}: Prior state/stat inconsistency prior to "
            f"{fmt(start_bucket)}: stats state={prior_state_stats} "
            f"but states state={prior_state_states}"
        )

    run_sum = prior_sum_stats if prior_sum_stats is not None else Decimal("0")
    run_state = (
        prior_state_stats if prior_state_stats is not None
        else prior_state_states if prior_state_states is not None
        else Decimal("0")
    )

    # ---- load states once for the fill range -----------------------------
    cur.execute(
        "SELECT last_updated_ts, state FROM states "
        "WHERE metadata_id=? AND last_updated_ts>=? AND last_updated_ts<? "
        "ORDER BY last_updated_ts",
        (mid, start_bucket - bucket, fill_end + bucket),
    )
    states: list[tuple[float, str]] = [
        (float(ts), str(st)) for ts, st in cur.fetchall()
    ]
    log(2, f"  {len(states)} states loaded for fill range")

    cur.execute(
        f"SELECT id, start_ts FROM {table} "
        f"WHERE metadata_id=? AND start_ts>=? AND start_ts<=?",
        (sid, start_bucket, max(fill_end, delete_through)),
    )
    existing: dict[float, int] = {
        float(r[1]): int(r[0]) for r in cur.fetchall()
    }

    updated = inserted = deleted = 0
    now = datetime.now().timestamp()
    si = 0

    # ---- Phase 1: walk buckets that may contain numeric samples ----------
    b = start_bucket
    while b <= fill_end:
        b_end = b + bucket
        saw_state = False
        ends_numeric = False

        while si < len(states) and states[si][0] < b_end:
            ts, st = states[si]
            si += 1
            if ts < b:
                continue
            saw_state = True
            if st in ("unknown", "unavailable"):
                ends_numeric = False
                continue
            try:
                val = Decimal(st)
            except (ArithmeticError, ValueError):
                ends_numeric = False
                continue
            if run_state > 0 and val < run_state * RESET_RATIO:
                run_sum = run_sum + val
                last_reset = ts
                log(2, f"  Reset at {fmt(ts)}: {run_state} -> {val}")
            else:
                run_sum = run_sum + (val - run_state)
            run_state = val
            ends_numeric = True

        do_upsert = ends_numeric or (not saw_state and prev_exists)

        if do_upsert:
            if b in existing:
                cur.execute(
                    f"UPDATE {table} "
                    f"SET sum=?, state=?, last_reset_ts=? WHERE id=?",
                    (float(run_sum), float(run_state), last_reset,
                     existing[b]),
                )
                updated += 1
            else:
                cur.execute(
                    f"INSERT INTO {table} "
                    f"(metadata_id, start_ts, sum, state, last_reset_ts, "
                    f" created_ts, mean, min, max) "
                    f"VALUES (?,?,?,?,?,?,NULL,NULL,NULL)",
                    (sid, float(b), float(run_sum), float(run_state),
                     last_reset, now),
                )
                inserted += 1
            prev_exists = True
        else:
            # Should not happen inside fill_end, but be safe
            if b in existing:
                cur.execute(f"DELETE FROM {table} WHERE id=?",
                            (existing[b],))
                deleted += 1
            prev_exists = False

        b += bucket

    # ---- Phase 2: block-delete from first invalid bucket onward ----------
    if first_invalid_ts is not None:
        del_from = (int(first_invalid_ts) // bucket) * bucket
        if del_from < start_bucket:
            del_from = start_bucket
        cur.execute(
            f"DELETE FROM {table} "
            f"WHERE metadata_id=? AND start_ts>=? AND start_ts<=?",
            (sid, float(del_from), float(delete_through)),
        )
        n = cur.rowcount
        deleted += n
        if n:
            log(1, f"  Deleted {n} rows from {fmt(del_from)} onward")

    log(1, f"  Updated={updated} Inserted={inserted} Deleted={deleted}")
    return updated, inserted, deleted


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    """Open DB, resolve ids, process tables, commit or roll back."""
    if not os.path.isfile(args.database):
        raise SystemExit(f"Database not found: {args.database}")

    conn = sqlite3.connect(args.database, timeout=TIMEOUT)
    try:
        sid, mid, eid = resolve(conn)
        log(1, f"entity_id={eid}  statistic_id={sid}  metadata_id={mid}")
        if args.test:
            log(1, "** DRY RUN **")

        last_numeric, first_invalid, last_state = state_endpoints(conn, mid, args.table)

        conn.execute("BEGIN IMMEDIATE")
        total_u = total_i = total_d = 0

        tables: list[tuple[str, int]] = []
        if args.table in ("short", "both"):
            tables.append(("statistics_short_term", SHORT_SEC))
        if args.table in ("long", "both"):
            tables.append(("statistics", LONG_SEC))

        for table, interval in tables:
            start_ts = args.start_ts
            if start_ts is None:
                start_ts = last_start_ts(conn, table, sid)
                log(3, f"No start_ts -- '{table}' starts at {fmt(start_ts)}")
            u, i, d = process(
                conn, table, interval, sid, mid, start_ts,
                last_numeric, first_invalid, last_state,
            )
            total_u += u
            total_i += i
            total_d += d

        log(1, f"TOTALS: Updated={total_u} Inserted={total_i} Deleted={total_d}")

        if args.test:
            conn.rollback()
            log(1, "** Rolled back (no changes written)")
        else:
            conn.commit()
            log(1, "Committed")

    except Exception as exc:  # pylint: disable=broad-exception-caught
        print(f"[ERROR] {exc}")
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
