#!/usr/bin/env python3
#==============================================================================
# pylint: disable=too-many-lines, too-many-instance-attributes, too-many-locals
# pylint: disable=line-too-long, global-statement
#==============================================================================
"""Move source Home Assistant states into missing target time buckets.
#
# App: Eversource Downloader and Importer
# File: insert_missing_placeholders.py
# Version: 0.9.0
# Copyright Jeff Kosowsky
# Date: August 2026
#

For each target bucket that has no target state, the nearest source
state to the bucket end is moved into the target chain, provided it is
within the configured window. Both source and target old_state_id
chains are repaired after any move.

Algorithm summary
-----------------
1. Resolve source/target metadata IDs and the attributes_id to use for
   moved rows.
2. Determine the inclusive range of fixed-length buckets to examine.
   When --to-ts is omitted, the last bucket is the latest one whose entire
   source-search window has already elapsed
   (now >= bucket_end + offset + window).
3. Load all relevant source and target states for the range in two bulk
   queries.
4. For every bucket that still has no target state, select the nearest
   unused source state to (bucket_end + offset) that lies inside the
   configured window. Selection is deterministic using the final metric:
   First absolute time distance, then prefer the side before the target timestamp,
   then state_id. Multiple sources are tried in the order given on the command line
   (first match wins).
5. Inside a single BEGIN IMMEDIATE transaction:
   - Re-verify the chosen source rows have not changed,
   - UPDATE each chosen row (new timestamp, target metadata_id, target attributes_id, state='unknown'),
   - Repair the source old_state_id chain (all moved rows removed),
   - Repair the target old_state_id chain (moved rows inserted at their new chronological positions).
6. COMMIT or ROLLBACK (--test). No new state rows are ever inserted.

The search window is centred on (bucket_end + offset), not on the raw
bucket end. Consequently a bucket is considered complete only after
bucket_end + offset + window.
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, NoReturn, Sequence
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# User Variables (constants)
# ---------------------------------------------------------------------------
DEFAULT_DB_PATH = "/homeassistant/home-assistant_v2.db"
DEFAULT_BUCKET = 900
DEFAULT_OFFSET = -15
DB_TIMEOUT = 30.0
MAX_DAYS_BACK = 3650  # ~10 years

# ---------------------------------------------------------------------------
# Other Global Variables
# ---------------------------------------------------------------------------

LOCAL_TZ: ZoneInfo
LOG_FORMAT = "%(message)s"

# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MoveCandidate:
    """One source state selected for one target bucket."""

    new_ts: float
    source_state_id: int
    source_ts: float
    distance: float
    source_metadata_id: int  # Which source supplied this candidate


@dataclass(frozen=True)
class StateRow:
    """The state fields needed for selection and chain repair."""

    state_id: int
    last_updated_ts: float
    old_state_id: int | None
    metadata_id: int
    attributes_id: int
    state: str


@dataclass(frozen=True)
class Config:
    """Resolved command-line configuration."""

    database: str
    from_ts: float
    to_ts: float
    source_metadata: list[int]  # Ordered, highest priority first
    source_entity: list[str]  # Parallel to source_metadata
    target_metadata: int
    target_entity: str
    attributes_id: int
    bucket: int
    window: int
    offset: int
    test: bool
    verbose: int


#==============================================================================
# COMMAND-LINE PARSING AND VALIDATION
#==============================================================================

def parse_timestamp(value: str) -> float:
    """Parse a positive Unix timestamp, local ISO time, or day offset."""

    try:
        number = float(value)
    except ValueError:
        number = None

    if number is not None:
        # Positive numbers are Unix timestamps. Negative numbers are days back
        # from local midnight; fractional values are intentionally supported.
        now = datetime.now(tz=LOCAL_TZ)
        if -MAX_DAYS_BACK <= number <= 0:
            today = now.replace(hour=0, minute=0, second=0, microsecond=0)
            return today.timestamp()  + number * 86400
        if 0 < now.timestamp() - number < (MAX_DAYS_BACK * 86400):  # In seconds
            return number
        raise argparse.ArgumentTypeError(
            f"Timestamps must be within last 10 years before now: {number!r} ({fmt_ts(number)})"
        )

    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid timestamp value: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.timestamp()

def parse_id_list(value: str) -> list[str]:
    """Split a comma-separated list and strip whitespace."""

    parts = [p.strip() for p in value.split(",") if p.strip()]
    if not parts:
        raise argparse.ArgumentTypeError("list must contain at least one entry")
    return parts


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(
        description="Move source states into missing target time buckets."
    )
    parser.add_argument(
        "-F", "--from-ts", dest="from_ts", type=parse_timestamp, required=True,
        help="Start from bucket containing this local time/date or day offset [REQUIRED]",
    )
    parser.add_argument(
        "-T", "--to-ts", dest="to_ts", type=parse_timestamp,
        help="End with bucket containing this local time/date or day offset "
        "[Default=last bucket whose source window has fully elapsed]",
    )

    target_group = parser.add_mutually_exclusive_group(required=True)
    target_group.add_argument(
        "-m", "--target-metadata", type=int,
        help="Target metadata_id to change TO "
        "[either -m|--target-metadata or -e|--target-entity REQUIRED]",
    )
    target_group.add_argument(
        "-e", "--target-entity",
        help="Target entity_id to change TO "
        "[either -m|--target-metadata or -e|--target-entity REQUIRED]",
    )

    source_group = parser.add_mutually_exclusive_group(required=False)
    source_group.add_argument(
        "-M", "--source-metadata", type=parse_id_list,
        help="Source metadata_id(s) to change FROM. Comma-separated list, ordered by priority "
        "(Note: Audit only if neither source metadata_id nor entity_id given)"
    )
    source_group.add_argument(
        "-E", "--source-entity", type=parse_id_list,
        help="Source entity_id(s) to change FROM. Comma-separated list, ordered by priority "
        "(Note: Audit only if neither source metadata_id nor entity_id given)"
    )

    parser.add_argument(
        "-a", "--attributes-id", type=int,
        help="Target attributes_id [Default=latest non-unknown/non-unavailable]",
    )
    parser.add_argument(
        "-b", "--bucket", type=int, default=DEFAULT_BUCKET,
        help="Bucket length in SECONDS - must be multiple of 60 [Default=900]",
    )
    parser.add_argument(
        "-w", "--window", type=int,
        help="Max distance from bucket end in SECONDS [Default=bucket/2 - 1]",
    )
    parser.add_argument(
        "-o", "--offset", type=int, default=DEFAULT_OFFSET,
        help="Timestamp offset from bucket end in SECONDS [Default=-15]",
    )
    parser.add_argument(
        "-d", "--database", default=DEFAULT_DB_PATH,
        help=f"Home Assistant database [Default={DEFAULT_DB_PATH}]",
    )
    parser.add_argument(
        "-t", "--test", action="store_true",
        help="Dry run (rollback transaction)",
    )
    parser.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="Increase verbosity (-v, -vv, etc.)",
    )
    return parser.parse_args(argv)


def fail(message: str) -> NoReturn:
    """Raise a command-line/configuration error."""

    raise ValueError(message)


def validate_basic_args(args: argparse.Namespace) -> None:
    """Validate arguments whose values do not depend on the database."""

    if args.bucket <= 0:
        fail("Bucket length must be greater than zero.")
    if args.bucket % 60 != 0:
        fail("Bucket length must be a multiple of 60 seconds.")
    if args.window is None:
        args.window = args.bucket // 2 - 1
    if args.window < 0:
        fail("Window must be zero or greater.")
    if args.window * 2 >= args.bucket:
        fail("Window must be strictly less than half the bucket length.")
    if args.offset <= -args.bucket or args.offset > 0:
        fail("Offset must be greater than -bucket and no greater than zero.")
    if not os.path.isfile(args.database):
        raise FileNotFoundError(f"Database does not exist: {args.database}")
    if args.from_ts < 0:
        fail("'-F|--from-ts' resolved to an invalid negative timestamp.")
    if args.to_ts is not None and args.to_ts < 0:
        fail("-T|--to-ts resolved to an invalid negative timestamp.")
    if args.attributes_id is not None and args.attributes_id < 0:
        fail("attributes-id must be non-negative.")


def setup_logging(verbose: int) -> None:
    """Configure simple single-line logging."""

    level = logging.WARNING
    if verbose >= 1:
        level = logging.INFO
    if verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format=LOG_FORMAT)


def log(level: int, verbose: int, message: str) -> None:
    """Log a message when the requested verbosity level is enabled."""

    if verbose >= level:
        logging.info(message)

def fmt_ts(ts: float, no_sec: bool = False) -> str:
    """Return a human-readable local timestamp string."""
    dt = datetime.fromtimestamp(ts, tz=LOCAL_TZ)
    if no_sec:
        return dt.strftime("%Y-%m-%d %H:%M %Z")
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")

def bucket_label(bucket_end: int, bucket: int) -> str:
    """Return a local-time bucket label without seconds."""

    start = datetime.fromtimestamp(bucket_end - bucket, tz=LOCAL_TZ)
    end = datetime.fromtimestamp(bucket_end, tz=LOCAL_TZ)
    return f"[{start:%Y-%m-%d %H:%M %Z} - {end:%H:%M %Z}]"


def bucket_end_for(ts: float, bucket: int) -> int:
    """Return the end of the bucket containing ts."""

    return (int(ts) // bucket + 1) * bucket


def first_bucket_end(from_ts: float, bucket: int) -> int:
    """Return the end of the first bucket to examine."""

    return bucket_end_for(from_ts, bucket)


def last_eligible_bucket_end(now_ts: float, bucket: int, window: int, offset: int) -> int:
    """Return the latest bucket whose complete source window has elapsed (including offset and window around it)."""

    return ((int(now_ts - window - offset)) // bucket) * bucket


def iter_bucket_ends(first_end: int, last_end: int, bucket: int) -> Iterator[int]:
    """Yield bucket end timestamps inclusively."""

    current = first_end
    while current <= last_end:
        yield current
        current += bucket


#==============================================================================
# DATABASE AND METADATA HELPERS
#==============================================================================

def connect_database(path: str) -> sqlite3.Connection:
    """Open the Home Assistant database."""

    connection = sqlite3.connect(path, timeout=DB_TIMEOUT)
    connection.row_factory = sqlite3.Row
    return connection


def metadata_for_entity(connection: sqlite3.Connection, entity_id: str) -> int:
    """Resolve an entity_id to its metadata_id."""

    row = connection.execute(
        "SELECT metadata_id FROM states_meta WHERE entity_id = ?", (entity_id,),
    ).fetchone()
    if row is None:
        fail(f"Entity not found: {entity_id}")
    return int(row["metadata_id"])


def entity_for_metadata(connection: sqlite3.Connection, metadata_id: int) -> str:
    """Resolve a metadata_id to its entity_id."""

    row = connection.execute(
        "SELECT entity_id FROM states_meta WHERE metadata_id = ?", (metadata_id,),
    ).fetchone()
    if row is None:
        fail(f"metadata_id not found: {metadata_id}")
    return str(row["entity_id"])


def latest_attributes_id(connection: sqlite3.Connection, metadata_id: int) -> int:
    """Find the latest non-unknown/non-unavailable attributes_id."""

    row = connection.execute(
        """
        SELECT attributes_id
        FROM states
        WHERE metadata_id = ?
          AND state NOT IN ('unknown', 'unavailable')
        ORDER BY last_updated_ts DESC, state_id DESC
        LIMIT 1
        """,
        (metadata_id,),
    ).fetchone()
    if row is None:
        fail(
            "No non-unknown/non-unavailable state exists for target "
            f"metadata_id={metadata_id}; specify --attributes-id."
        )
    return int(row["attributes_id"])


def resolve_config(args: argparse.Namespace, connection: sqlite3.Connection) -> Config:
    """Resolve IDs and defaults into an immutable configuration."""

    if args.target_metadata is not None:
        target_metadata = int(args.target_metadata)
        target_entity = entity_for_metadata(connection, target_metadata)
    else:
        target_entity = str(args.target_entity)
        target_metadata = metadata_for_entity(connection, target_entity)

    if args.source_metadata is None and args.source_entity is None:
        source_metadata = []
        source_entity = []
    elif args.source_metadata is not None:
        source_metadata = [int(x) for x in args.source_metadata]
        source_entity = [entity_for_metadata(connection, mid) for mid in source_metadata]
    else:
        source_entity = list(args.source_entity)
        source_metadata = [metadata_for_entity(connection, eid) for eid in source_entity]

    # Reject duplicates and overlap with target
    if len(set(source_metadata)) != len(source_metadata):
        fail("Duplicate source metadata_id in list")
    if len(set(source_entity)) != len(source_entity):
        fail("Duplicate source entity_id in list")
    if target_metadata in source_metadata:
        fail("Source list must not contain the target metadata_id")

    attributes_id = (
        int(args.attributes_id)
        if args.attributes_id is not None
        else latest_attributes_id(connection, target_metadata)
    )

    # last_eligible is already a bucket end; do not run it through bucket_end_for again
    last_eligible = last_eligible_bucket_end(time.time(), args.bucket, args.window, args.offset)
    if args.to_ts is None:
        to_ts = float(last_eligible)
    else:
        to_ts = float(args.to_ts)
        requested_last = bucket_end_for(to_ts, args.bucket)
        if requested_last > last_eligible:
            fail("to-ts includes a bucket whose source search window has not fully elapsed.")
        to_ts = float(min(requested_last, last_eligible))

    if args.from_ts > to_ts:
        fail("-F|--from must not be later than -T|--to.")

    return Config(
        database=str(args.database),
        from_ts=float(args.from_ts),
        to_ts=to_ts,
        source_metadata=source_metadata,
        source_entity=source_entity,
        target_metadata=target_metadata,
        target_entity=target_entity,
        attributes_id=attributes_id,
        bucket=int(args.bucket),
        window=int(args.window),
        offset=int(args.offset),
        test=bool(args.test),
        verbose=int(args.verbose),
    )


#==============================================================================
# STATE LOADING AND SOURCE CANDIDATE SELECTION
#==============================================================================

def load_states(
    connection: sqlite3.Connection,
    metadata_id: int,
    start_ts: float,
    end_ts: float,
) -> list[StateRow]:
    """Load states for one metadata_id over an inclusive timestamp range."""

    rows = connection.execute(
        """
        SELECT
            state_id,
            last_updated_ts,
            old_state_id,
            metadata_id,
            attributes_id,
            state
        FROM states
        WHERE metadata_id = ?
          AND last_updated_ts >= ?
          AND last_updated_ts <= ?
        ORDER BY last_updated_ts ASC, state_id ASC
        """,
        (metadata_id, start_ts, end_ts),
    ).fetchall()

    return [
        StateRow(
            state_id=int(row["state_id"]),
            last_updated_ts=float(row["last_updated_ts"]),
            old_state_id=(int(row["old_state_id"]) if row["old_state_id"] is not None else None),
            metadata_id=int(row["metadata_id"]),
            attributes_id=int(row["attributes_id"]),
            state=str(row["state"]),
        )
        for row in rows
    ]


def target_bucket_has_state(
    target_states: Sequence[StateRow],
    bucket_start: int,
    bucket_end: int,
) -> bool:
    """Return whether any target state is inside the bucket."""

    for row in target_states:
        if row.last_updated_ts < bucket_start:
            continue
        if row.last_updated_ts >= bucket_end:
            return False
        return True
    return False


def choose_source_candidate(
    source_states: Sequence[StateRow],
    target_ts: float,
    window: int,
    used_ids: set[int],
    source_metadata_id: int,
) -> MoveCandidate | None:
    """Choose the nearest unused source state to the target timestamp."""

    candidates: list[StateRow] = []
    low = target_ts - window
    high = target_ts + window

    for row in source_states:
        if row.state_id in used_ids:
            continue
        if row.last_updated_ts < low:
            continue
        if row.last_updated_ts > high:
            break
        candidates.append(row)

    if not candidates:
        return None

    def sort_key(row: StateRow) -> tuple[float, int, int]:
        distance = row.last_updated_ts - target_ts
        side = 0 if distance <= 0 else 1
        # At equal distance, prefer the higher state_id on/before target_ts
        # and the lower state_id after target_ts, as specified.
        state_key = -row.state_id if side == 0 else row.state_id
        return (abs(distance), side, state_key)

    selected = min(candidates, key=sort_key)
    distance = selected.last_updated_ts - target_ts
    return MoveCandidate(
        new_ts=target_ts,
        source_state_id=selected.state_id,
        source_ts=selected.last_updated_ts,
        distance=distance,
        source_metadata_id=source_metadata_id,
    )


def collect_candidates(
    target_states: Sequence[StateRow],
    source_states_by_meta: dict[int, list[StateRow]],
    config: Config,
) -> tuple[list[MoveCandidate], int, int, int]:
    """Collect target-bucket decisions before modifying the database."""

    first_end = first_bucket_end(config.from_ts, config.bucket)
    # config.to_ts is already a bucket end (from last_eligible or the clamp)
    last_end = int(config.to_ts)

    candidates: list[MoveCandidate] = []
    used_source_ids: set[int] = set()
    existing = 0
    missing = 0
    examined = 0

    # Map metadata_id -> entity_id for logging
    entity_by_meta = dict(zip(config.source_metadata, config.source_entity))

    for bucket_end in iter_bucket_ends(first_end, last_end, config.bucket):
        examined += 1
        label = bucket_label(bucket_end, config.bucket)

        # The destination timestamp is deliberately offset from bucket end.
        # Source selection and the search window are both centered on
        # (bucket_end + offset), not on the raw bucket end.
        if target_bucket_has_state(target_states, bucket_end - config.bucket, bucket_end):
            existing += 1
            log(3, config.verbose, f"Bucket: {label} Existing (target already exists)")
            continue

        target_ts = bucket_end + config.offset
        candidate = None
        for mid in config.source_metadata:  # Priority order: first match wins
            candidate = choose_source_candidate(
                source_states_by_meta[mid], target_ts, config.window, used_source_ids, mid,
            )
            if candidate is not None:
                break

        if candidate is None:
            missing += 1
            log(2, config.verbose, f"Bucket: {label} **MISSING** Source")
            continue

        used_source_ids.add(candidate.source_state_id)
        candidates.append(candidate)
        log(
            2, config.verbose,
            f"Bucket: {label} "
            f"Source[{entity_by_meta[candidate.source_metadata_id]}({candidate.source_metadata_id})]: "
            f"id={candidate.source_state_id}, ts={candidate.source_ts:.0f} "
            f"[{fmt_ts(candidate.source_ts)}] (offset: {candidate.distance:+.0f}s)",
        )

    return candidates, examined, existing, missing


#==============================================================================
# STATE UPDATES AND CHAIN REPAIR
#==============================================================================

def fetch_state_by_id(connection: sqlite3.Connection, state_id: int) -> StateRow | None:
    """Fetch one state by state_id."""

    row = connection.execute(
        """
        SELECT
            state_id,
            last_updated_ts,
            old_state_id,
            metadata_id,
            attributes_id,
            state
        FROM states
        WHERE state_id = ?
        """,
        (state_id,),
    ).fetchone()

    if row is None:
        return None

    return StateRow(
        state_id=int(row["state_id"]),
        last_updated_ts=float(row["last_updated_ts"]),
        old_state_id=(int(row["old_state_id"]) if row["old_state_id"] is not None else None),
        metadata_id=int(row["metadata_id"]),
        attributes_id=int(row["attributes_id"]),
        state=str(row["state"]),
    )


def verify_source_rows(
    connection: sqlite3.Connection,
    candidates: Sequence[MoveCandidate],
    source_metadata: list[int],
) -> dict[int, StateRow]:
    """Verify selected source rows have not changed since selection."""

    result: dict[int, StateRow] = {}
    for candidate in candidates:
        row = fetch_state_by_id(connection, candidate.source_state_id)
        if row is None:
            fail(f"Selected source state no longer exists: {candidate.source_state_id}")
        if row.metadata_id not in source_metadata:
            fail(
                f"Selected source state changed metadata_id: "
                f"state_id={candidate.source_state_id} "
                f"expected one of {source_metadata} actual={row.metadata_id}"
            )
        if row.last_updated_ts != candidate.source_ts:
            fail(
                f"Selected source state changed timestamp: "
                f"state_id={candidate.source_state_id}"
            )
        result[row.state_id] = row
    return result


def update_moved_rows(
    connection: sqlite3.Connection,
    candidates: Sequence[MoveCandidate],
    source_rows: dict[int, StateRow],
    config: Config,
) -> dict[int, StateRow]:
    """Move selected rows to the target metadata and timestamp."""

    moved: dict[int, StateRow] = {}
    for candidate in candidates:
        old = source_rows[candidate.source_state_id]
        new_ts = candidate.new_ts
        connection.execute(
            """
            UPDATE states
            SET
                last_updated_ts = ?,
                metadata_id = ?,
                attributes_id = ?,
                state = 'unknown'
            WHERE state_id = ?
            """,
            (new_ts, config.target_metadata, config.attributes_id, old.state_id),
        )
        moved[old.state_id] = StateRow(
            state_id=old.state_id,
            last_updated_ts=float(new_ts),
            old_state_id=old.old_state_id,
            metadata_id=config.target_metadata,
            attributes_id=config.attributes_id,
            state="unknown",
        )
        log(
            2, config.verbose,
            f"Moved: id={old.state_id} ts={old.last_updated_ts:.0f} "
            f"[{fmt_ts(old.last_updated_ts)}] -> {new_ts} "
            f"[{fmt_ts(new_ts)}] meta={old.metadata_id}->{config.target_metadata} "
            f"attr={old.attributes_id}->{config.attributes_id} "
            f"(dist={(new_ts - old.last_updated_ts):.0f}s)",
        )
    return moved


def load_chain_rows(
    connection: sqlite3.Connection,
    metadata_id: int,
    min_ts: float,
    max_ts: float,
) -> list[StateRow]:
    """Load a time-bounded metadata chain."""

    return load_states(connection, metadata_id, min_ts, max_ts)


def update_old_state_id(
    connection: sqlite3.Connection,
    state_id: int,
    old_state_id: int | None,
) -> None:
    """Set one state's old_state_id."""

    connection.execute(
        "UPDATE states SET old_state_id = ? WHERE state_id = ?",
        (old_state_id, state_id),
    )


def repair_source_chain(
    connection: sqlite3.Connection,
    source_metadata: int,
    source_rows_before: Sequence[StateRow],
    moved: dict[int, StateRow],
) -> None:
    """Repair source old_state_id links after removing moved rows."""

    moved_ids = set(moved)
    if not moved_ids:
        return

    remaining = [row for row in source_rows_before if row.state_id not in moved_ids]
    remaining.sort(key=lambda row: (row.last_updated_ts, row.state_id))

    for index, row in enumerate(remaining):
        expected_old = remaining[index - 1].state_id if index > 0 else None
        if row.old_state_id != expected_old:
            update_old_state_id(connection, row.state_id, expected_old)
            # Verify the row we just updated still belongs to the source
            current = fetch_state_by_id(connection, row.state_id)
            if current is None or current.metadata_id != source_metadata:
                fail(f"Source chain verification failed for state_id={row.state_id}")


def repair_target_chain(
    connection: sqlite3.Connection,
    target_metadata: int,
    target_rows_before: Sequence[StateRow],
    moved: dict[int, StateRow],
) -> None:
    """Repair target old_state_id links after inserting moved rows."""

    if not moved:
        return

    moved_ids = set(moved)
    all_rows = [row for row in target_rows_before if row.state_id not in moved_ids]
    all_rows.extend(moved.values())
    all_rows.sort(key=lambda row: (row.last_updated_ts, row.state_id))

    # Every row from the first moved row through the last moved row can have
    # its predecessor changed. The first existing row after that range can
    # also need to point to the final moved row. Rows before the first moved
    # row keep their existing predecessor, because the context contains only
    # one predecessor outside the affected range.
    moved_positions = [index for index, row in enumerate(all_rows) if row.state_id in moved_ids]
    first_moved = min(moved_positions)
    last_moved = max(moved_positions)
    first_affected = first_moved
    last_affected = last_moved
    if last_affected + 1 < len(all_rows):
        last_affected += 1

    for index in range(first_affected, last_affected + 1):
        row = all_rows[index]
        previous = all_rows[index - 1] if index > 0 else None
        expected_old = previous.state_id if previous is not None else None

        current: StateRow | None
        if row.state_id in moved_ids:
            current = moved[row.state_id]
        else:
            current = fetch_state_by_id(connection, row.state_id)

        if current is None:
            fail(f"Target chain state disappeared: state_id={row.state_id}")
        if current.metadata_id != target_metadata:
            fail(f"Target chain metadata verification failed: state_id={row.state_id}")
        if current.old_state_id != expected_old:
            update_old_state_id(connection, row.state_id, expected_old)


def load_chain_context_for(
    connection: sqlite3.Connection,
    metadata_id: int,
    timestamps: Sequence[float],
) -> list[StateRow]:
    """Load the chronological neighbourhood needed to repair one chain."""

    if not timestamps:
        return []
    min_ts = min(timestamps)
    max_ts = max(timestamps)
    rows = load_chain_rows(connection, metadata_id, min_ts, max_ts)
    return expand_chain_context(connection, metadata_id, rows)


def expand_chain_context(
    connection: sqlite3.Connection,
    metadata_id: int,
    rows: Sequence[StateRow],
) -> list[StateRow]:
    """Add immediate chronological neighbors around a loaded range."""

    if not rows:
        return []

    ordered = sorted(rows, key=lambda row: (row.last_updated_ts, row.state_id))
    first = ordered[0]
    last = ordered[-1]

    before = connection.execute(
        """
        SELECT
            state_id,
            last_updated_ts,
            old_state_id,
            metadata_id,
            attributes_id,
            state
        FROM states
        WHERE metadata_id = ?
          AND (
              last_updated_ts < ?
              OR (last_updated_ts = ? AND state_id < ?)
          )
        ORDER BY last_updated_ts DESC, state_id DESC
        LIMIT 1
        """,
        (metadata_id, first.last_updated_ts, first.last_updated_ts, first.state_id),
    ).fetchone()

    after = connection.execute(
        """
        SELECT
            state_id,
            last_updated_ts,
            old_state_id,
            metadata_id,
            attributes_id,
            state
        FROM states
        WHERE metadata_id = ?
          AND (
              last_updated_ts > ?
              OR (last_updated_ts = ? AND state_id > ?)
          )
        ORDER BY last_updated_ts ASC, state_id ASC
        LIMIT 1
        """,
        (metadata_id, last.last_updated_ts, last.last_updated_ts, last.state_id),
    ).fetchone()

    result = list(ordered)
    if before is not None:
        result.insert(
            0,
            StateRow(
                state_id=int(before["state_id"]),
                last_updated_ts=float(before["last_updated_ts"]),
                old_state_id=(int(before["old_state_id"]) if before["old_state_id"] is not None else None),
                metadata_id=int(before["metadata_id"]),
                attributes_id=int(before["attributes_id"]),
                state=str(before["state"]),
            ),
        )
    if after is not None:
        result.append(
            StateRow(
                state_id=int(after["state_id"]),
                last_updated_ts=float(after["last_updated_ts"]),
                old_state_id=(int(after["old_state_id"]) if after["old_state_id"] is not None else None),
                metadata_id=int(after["metadata_id"]),
                attributes_id=int(after["attributes_id"]),
                state=str(after["state"]),
            ),
        )
    return result


#==============================================================================
# MAIN OPERATION
#==============================================================================

def run(config: Config) -> tuple[int, int, int, int]:
    """Run the complete operation and return summary counts."""

    connection = connect_database(config.database)
    try:
        first_end = first_bucket_end(config.from_ts, config.bucket)
        last_end = int(config.to_ts)

        # Candidate selection is centered on bucket_end + offset, so load
        # the complete search window on both sides of every bucket target.
        source_load_start = first_end + config.offset - config.window
        source_load_end = last_end + config.offset + config.window
        target_load_start = first_end - config.bucket
        target_load_end = last_end

        target_states = load_states(connection, config.target_metadata, target_load_start, target_load_end)
        source_states_by_meta: dict[int, list[StateRow]] = {}
        for mid in config.source_metadata:
            source_states_by_meta[mid] = load_states(
                connection, mid, source_load_start, source_load_end,
            )

        candidates, examined, existing, missing = collect_candidates(target_states, source_states_by_meta, config)

        log(1, config.verbose, f"Target: {config.target_entity} (meta={config.target_metadata})")

        if config.source_metadata:
            log(
                1, config.verbose,
                "Source: " + ", ".join(
                    f"{ent} (meta={mid})"
                    for ent, mid in zip(config.source_entity, config.source_metadata)
                ),
            )
        else:
            log(1, config.verbose, "WARNING: No source (donor) sensors given, so auditing existing and missing buckets only")

        log(1, config.verbose, f"Range: {fmt_ts(config.from_ts)} -> {fmt_ts(config.to_ts)} ")
        log(1, config.verbose, f"Bucket={config.bucket}s Window={config.window}s Offset={config.offset}s Attributes={config.attributes_id}")

        if not candidates:
            log(
                1, config.verbose,
                f"Examined {examined} buckets (Existing={existing} Moved=0 Missing={missing})",
            )
            connection.close()
            return examined, existing, 0, missing

        connection.execute("BEGIN IMMEDIATE")
        try:
            source_rows_verified = verify_source_rows(
                connection, candidates, config.source_metadata,
            )

            # Chain context must be based on the pre-update source and target
            # metadata membership. The moved rows are then represented by the
            # new StateRow values returned by update_moved_rows().
            moved = update_moved_rows(connection, candidates, source_rows_verified, config)

            # ----- Chain repair: source side (one chain per original metadata_id) -----
            moved_by_source: dict[int, dict[int, StateRow]] = defaultdict(dict)
            for sid, old_row in source_rows_verified.items():
                if sid in moved:
                    moved_by_source[old_row.metadata_id][sid] = moved[sid]

            for mid, moved_subset in moved_by_source.items():
                src_rows = load_chain_context_for(
                    connection, mid, [r.last_updated_ts for r in moved_subset.values()],
                )
                repair_source_chain(connection, mid, src_rows, moved_subset)

            # ----- Chain repair: target side (exactly the same pattern) -----
            tgt_rows = load_chain_context_for(
                connection, config.target_metadata, [c.new_ts for c in candidates],
            )
            repair_target_chain(connection, config.target_metadata, tgt_rows, moved)

            if config.test:
                connection.rollback()
                log(1, config.verbose, "Test mode: transaction rolled back.")
            else:
                connection.commit()
                log(1, config.verbose, "Transaction committed.")

        except Exception:
            connection.rollback()
            raise

        log(
            1, config.verbose,
            f"Examined {examined} buckets: Existing={existing} "
            f"Moved={len(candidates)} Missing={missing}",
        )
        connection.close()
        return examined, existing, len(candidates), missing

    except Exception:
        connection.close()
        raise


def main(argv: Sequence[str] | None = None) -> int:
    """Program entry point."""

    try:
        global LOCAL_TZ
        name = os.environ.get("TZ")
        if not name:
            raise RuntimeError("TZ environment variable is not set")
        LOCAL_TZ = ZoneInfo(name)

        args = parse_args(argv)
        setup_logging(args.verbose)
        validate_basic_args(args)

        connection = connect_database(args.database)
        try:
            config = resolve_config(args, connection)
        finally:
            connection.close()

        run(config)
        return 0

    except (ValueError, FileNotFoundError, sqlite3.Error) as exc:
        logging.error("[ERROR] %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
