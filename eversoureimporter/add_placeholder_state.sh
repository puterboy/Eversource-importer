#!/usr/bin/env bash

#===============================================================================
#
# App: Eversource Downloader and Importer
# File: add_placeholder_state.sh
# Version: 0.9.0
# Copyright Jeff Kosowsky
# Date: August 2026
#
# Add placeholder state every BUCKET seconds, offset from the bucket end
#
#===============================================================================
#### Bash Functions

SCRIPT_NAME="${0##*/}"

## Post log.
# If LEVEL ends in '_', then also send persistent_notification
log() {
    local LEVEL_="$1"
    LEVEL=${LEVEL_%_}  # Strip trailing '_'    
    shift
    printf '%s %s [%s] %s\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" \
        "$LEVEL" \
        "$SCRIPT_NAME" \
        "$*"
    if [ "$LEVEL" != "$LEVEL_" ]; then 
	send_persistent_notification "${LEVEL}: $*"
    fi
}

## Send persistent Notification
send_persistent_notification() {
    local title="${ADDON_NAME:+ [$ADDON_NAME:$SCRIPT_NAME] }$1"
    local message="${2:-}"

    # Escape characters that have special meaning in JSON strings.
    title="${title//\\/\\\\}"
    title="${title//\"/\\\"}"
    title="${title//$'\n'/\\n}"
    title="${title//$'\r'/\\r}"

    message="${message//\\/\\\\}"
    message="${message//\"/\\\"}"
    message="${message//$'\n'/\\n}"
    message="${message//$'\r'/\\r}"

    curl -fsS \
         -X POST \
         -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
         -H "Content-Type: application/json" \
	 -d "{\"notification_id\":\"${ADDON_NAME}:$SCRIPT_NAME-${title}\",\"title\":\"${title}\",\"message\":\"${message}\"}" \
	 "http://supervisor/core/api/services/persistent_notification/create" >/dev/null
}

trap 'status=$?; log ERROR_ "Exiting with status $status"' EXIT

#===============================================================================
#### INPUT Variables

ENTITY_ID="$1"  # HA sensor for inserting placeholder
if [ -z "$1" ]; then
    log ERROR "Need to enter an ENTITY_ID"
    exit 1
fi
    
OFFSET="${2:--15}"  # Offset from end of BUCKET in seconds (negative is before end)

#===============================================================================
#### User Variables

if [ "$(id -u)" -ne 0 ]; then
    log ERROR "Must be root to run!"
    exit 1
fi

DB="/homeassistant/home-assistant_v2.db"
BUCKET=900  # Length of bucket in seconds

METADATA_ID="$(sqlite3 "$DB" "SELECT metadata_id FROM states_meta WHERE entity_id = '$ENTITY_ID' LIMIT 1")"
if [ -z "$METADATA_ID" ]; then
    log ERROR "Could not find metadata_id for '$ENTITY_ID'"
    exit 1
fi

# Select last non-unknown/unavailable attribute_id for given metadata_id
ATTRIBUTES_ID="$(sqlite3 "$DB" "SELECT attributes_id FROM states WHERE metadata_id = '$METADATA_ID' AND state NOT IN ('unknown','unavailable') ORDER BY last_updated_ts DESC, state_id DESC LIMIT 1")"
if [ -z "$ATTRIBUTES_ID" ]; then
    log ERROR "Could not find a valid (last) attributes_id for '$ENTITY_ID'"
    exit 1
fi

log INFO "Entity_id=$ENTITY_ID  metadata_id=$METADATA_ID  attributes_id=$ATTRIBUTES_ID (Offset=${OFFSET}s)"

#===============================================================================
##### Main update loop

while true; do
    ## Wait until OFFSET seconds relative to next Bucket end
    NOW=$(date +%s)
    WAIT=$(( BUCKET - ((NOW - OFFSET) % BUCKET) ))
    TARGET=$((NOW + WAIT))
    WAIT1=$((WAIT - 1))
    sleep "$WAIT1"  # First wait until within 1 second of target (end of bucket + OFFSET seconds)

    while [ "$(date +%s)" -lt "$TARGET" ]; do  # Then, fractionally approach the final second...
	sleep 0.05
    done

    ## Use REST API to insert state with value 'unknown' and alternating dummy attribute_id
    # NOTE: You cannot insert two states in a row with same value and attribute_id
    if ! RESULT="$(curl -sS -X POST "http://supervisor/core/api/states/$ENTITY_ID" \
        -H "Authorization: Bearer $SUPERVISOR_TOKEN" \
        -H "Content-Type: application/json" \
        -d '{"state":"unknown","attributes":{"'"${ENTITY_ID}"'-dummy":'"$(( $(date "+%M") % 2 ))"'}}')"; then
        log ERROR_ "Failed to create placeholder state for: $ENTITY_ID"
        continue
    fi

    ## Extract date in UTC time
    # NOTE: Required since busybox date won't recognize original format iso format with timezones
    DATE="$(echo "$RESULT" | sed -n 's/.*"last_updated":"\([^"]*\)".*/\1/p')"
    if [ -z "$DATE" ]; then
        log ERROR_ "Could not extract 'last_updated' from Home Assistant response: $RESULT"
        continue
    fi

    # Replace 'T' with ' ' and strip +00:00 UTC timezone since busybox date can't handle that format
    DATE_="$(echo "$DATE" | sed -e 's/T/ /' -e 's/+.*//')"
    ## Remove fractional seconds, convert to timestamp (using +0000 since UTC timezone) then add back fractional seconds
    if ! LAST_UPDATED_TS="$(date "+%s" -d "${DATE_%%.*}+0000").${DATE_##*.}"; then
        log ERROR_ "Could not convert last_updated '$DATE' to a valid 'last_updated_ts' timestamp"
        continue
    fi

    sleep 5  # Wait to ensure state is recorded


    ## Try up to MAX_ATTEMPTS times to update attributes_id (using LAST_UPDATED_TS to find the state)
    MAX_ATTEMPTS=3
    ATTEMPTS=0
    UPDATE_SUCCESS=""
    while true; do
        SQL_OUTPUT="$(sqlite3 -batch -bail "$DB" "
            BEGIN IMMEDIATE;
            UPDATE states
            SET attributes_id = $ATTRIBUTES_ID
            WHERE metadata_id = $METADATA_ID
              AND ABS(last_updated_ts - '$LAST_UPDATED_TS') <= 0.000001;
            SELECT changes();
            COMMIT;
    	" 2>&1)"
	STATUS=$?

	if [ "$STATUS" -eq 0 ] && [ "$SQL_OUTPUT" = "1" ]; then
	    UPDATE_SUCCESS=1
            break  # Successful update
	fi

	if [ "$STATUS" -eq 0 ] && [ "$SQL_OUTPUT" = "0" ]; then  # Sqlite succeeded but failed to update - retrying won't help
	    log ERROR_ "Placeholder insertion succeeded but failed to update attribute [$DATE ($LAST_UPDATED_TS)]"	    
            break
	fi

	if [ "$((++ATTEMPTS))" -ge "$MAX_ATTEMPTS" ]; then  # Sqlite failed on MAX_ATTEMPTS
	   log ERROR_ "Placeholder insertion succeeded but sqlite update command failed in $MAX_ATTEMPTS attempts (output=$SQL_OUTPUT, status=$STATUS) [$DATE ($LAST_UPDATED_TS)]"
	   break
	fi
	   log WARNING "Placeholder insertion succeeded but sqlite update command failed attempt $ATTEMPTS/$MAX_ATTEMPTS... retrying (output=$SQL_OUTPUT, status=$STATUS) [$DATE ($LAST_UPDATED_TS)]"
	sleep 2 # Wait and retry
    done
    [ -z "$UPDATE_SUCCESS" ] && continue # Abort

    ## Convert back to local time for logging purposes
    DATE_LOCAL="$(date  "+%Y-%m-%d %H:%M:%S" -d @"${LAST_UPDATED_TS%%.*}")"  # Note need to trim off fractional part for busybox date
    log INFO "Inserted placeholder 'unknown'state for: $ENTITY_ID ($METADATA_ID) [$DATE_LOCAL]"
done
# If the infinite loop ever stops for any reason, exit with non-zero status.
# This guarantees:
#   1. The EXIT trap runs and sends an error notification to HA.
#   2. When launched by the add-on supervisor (with set -e), the add-on itself stops
#      so it can be restarted and the placeholder inserter relaunched.
exit 3
