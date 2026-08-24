#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
#===============================================================================
#
# App: Eversource Downloader and Importer
# File: run.sh
# Version: 0.9.0
# Copyright Jeff Kosowsky
# Date: August 2026
#
#===============================================================================
## VARIABLES
export HA_DB="/homeassistant/home-assistant_v2.db"
export ADDON_NAME="$(bashio::addon.name)"
export PYTHONUNBUFFERED=1  # Don't buffer output of Python scripts

TEST=""
#TEST="-t" # Uncomment to run routines in test without actually writing to ENERGY_FILE or HA_DB

#===============================================================================
#### Bash Functions

## Post bashio log
# If LEVEL ends in '_', then also send persistent_notification
log() {
    local LEVEL_="${1,,}"  # Convert to lowercase
    LEVEL=${LEVEL_%_}  # Strip trailing '_'
    shift

    bashio::log.${LEVEL%_} "$*"
    if [ "$LEVEL" != "$LEVEL_" ]; then 
	send_persistent_notification "${LEVEL}: $*"
    fi
}


## Send persistent Notification
send_persistent_notification() {
    local title="[$ADDON_NAME] $1"
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
	 -d "{\"notification_id\":\"${ADDON_NAME}-${title}\",\"title\":\"${title}\",\"message\":\"${message}\"}" \
	 "http://supervisor/core/api/services/persistent_notification/create" >/dev/null	
}

## Get config variables from HA add-on & set environment variables
load_config_var() {
    # First, use existing variable if already set (for debugging purposes)
    # If not set, lookup configuration value
    # If null, use optional second parameter or else ""
    local VAR_NAME="$1"
    local DEFAULT="${2:-}"
    local MASK="${3:-}"

    local VALUE
    #Check if $VAR_NAME exists before getting its value since 'set +x' mode
    if declare -p "$VAR_NAME" >/dev/null 2>&1; then  #Variable exist, get its value
        VALUE="${!VAR_NAME}"
    elif bashio::config.exists "${VAR_NAME,,}"; then
        VALUE="$(bashio::config "${VAR_NAME,,}")"
    else
        log WARNING "Unknown config key: ${VAR_NAME,,}"
    fi

    if [ "$VALUE" = "null" ] || [ -z "$VALUE" ]; then
        log WARNING "Config key '${VAR_NAME,,}' unset, setting to default: '$DEFAULT'"
        VALUE="$DEFAULT"
    fi

    # Assign and export safely using 'printf -v' and 'declare -x'
    printf -v "$VAR_NAME" '%s' "$VALUE"
    eval "export $VAR_NAME"

    if [ -z "$MASK" ]; then
        log INFO "$VAR_NAME=$VALUE"
    else
        log INFO "$VAR_NAME=XXXXXX"
    fi
}

#===============================================================================
### Initial logging statements
echo "."  # Almost blank line (Note totally blank or white space lines are swallowed)
printf '%*s\n' 80 '' | tr ' ' '#'  # Separator
log INFO "######## Starting Eversource Energy Downloader and Importer (PID=$$) ########"
log INFO "$(date) [Version: $APP_VERSION]"
log INFO "$(uname -a)"
ha_info=$(bashio::info)
log INFO "Core=$(echo "$ha_info" | jq -r '.homeassistant')  HAOS=$(echo "$ha_info" | jq -r '.hassos')  MACHINE=$(echo "$ha_info" | jq -r '.machine')  ARCH=$(echo "$ha_info" | jq -r '.arch')"

#===============================================================================
#### Load Configuration Variables

load_config_var USER_EVERSOURCE
load_config_var PASSWD_EVERSOURCE "" 1  #Mask password in log
load_config_var ACCOUNT_EVERSOURCE
load_config_var ENERGY_FILE
if [[ "$ENERGY_FILE" != /* ]]; then
    ENERGY_FILE="/homeassistant/$ENERGY_FILE"
    log INFO "  Saving energy data to: $ENERGY_FILE"
fi
load_config_var DAYS_BACK
load_config_var DOWNLOAD_START
((DOWNLOAD_START = DOWNLOAD_START * 3600 + 60 ))  # Convert to seconds and add a minute to avoid conflicting with top of the hour statistics
load_config_var DOWNLOAD_FREQUENCY
((DOWNLOAD_FREQUENCY *= 3600))  # Convert to seconds
load_config_var ENERGY_SENSOR
load_config_var DONOR_SENSORS
if [ -z "$DONOR_SENSORS" ]; then
    log WARNING "Donor sensors blank, so won't be able to fill in missing placeholders using nearby donor sensor states"
fi
load_config_var OFFSET

log INFO "HA_DB=$HA_DB"

#===============================================================================
#### Launch routine (running in background) to add 'unknown' placeholders to ENERGY_SENSOR every quarter hour
log INFO "##### Launching 'add_placeholder_state.sh' to add new '$ENERGY_SENSOR' state placeholder $(( -OFFSET)) seconds before every quarter hour"
/app/add_placeholder_state.sh "$ENERGY_SENSOR" "$OFFSET" &

#sleep 99999999 # Uncomment for debugging so you can enter docker shell

set +e  # Don't exit on error - not needed/wanted since we explicitly catch and handle the return status

## Determine epoch time of end of last downloaded energy bucket saved in ENERGY_FILE (if any)
LAST_DOWNLOAD_TIME=0
if [ -f "$ENERGY_FILE" ]; then  # If energy file exists, set LAST_DOWNLOAD_TIME to last entry
    LAST_DOWNLOAD_TIME="$(date +%s -d "$(awk -F, 'END {printf "%s %s",  $2, $4}' "$ENERGY_FILE")" 2>/dev/null || echo 0)"
fi
log INFO "Last download bucket end:      $(date -d "@$LAST_DOWNLOAD_TIME" "+%Y-%m-%d %H:%M:%S %Z")"

## Determine epoch time of last valid (non-unknown/unavailable) state in HA_DB for ENERGY_SENSOR
LAST_DB_TIME="$(sqlite3 -batch -readonly $HA_DB "SELECT last_updated_ts FROM states s JOIN states_meta sm ON s.metadata_id = sm.metadata_id WHERE sm.entity_id = '$ENERGY_SENSOR' AND state NOT IN('unknown', 'unavailable') ORDER BY last_updated_ts DESC LIMIT 1" 2>/dev/null)"
LAST_DB_TIME="${LAST_DB_TIME%%.*}"   # Drop fractional part (so can be used in date)
LAST_DB_TIME="${LAST_DB_TIME:-0}"    # Default to zero
log INFO "Last valid HA DB energy state: $(date -d "@$LAST_DB_TIME" "+%Y-%m-%d %H:%M:%S %Z")"

#### Download and Import loop
# Run immediately, then every DOWNLOAD_FREQUENCY seconds (starting at DOWNLOAD_START seconds after 12AM local time, provided "new day" relative to last download and last state)
log INFO "##### Starting download-importer loop (runs every $((DOWNLOAD_FREQUENCY/3600)) hours starting $((DOWNLOAD_START/3600)) hours after midnight until success)"
FIRST_TIME=1
while true; do
    ## Determine when to download and import next
    UTC_OFFSET="$(date +%z)"  # In format +/-NNNN
    UTC_OFFSET=$(( ${UTC_OFFSET:0:1}1 * (10#${UTC_OFFSET:1:2} * 3600 + 10#${UTC_OFFSET:3:2} * 60) ))  # Convert to seconds

    NOW="$(date +%s)"
    
    TODAY_MIDNIGHT=$((  ((NOW + UTC_OFFSET)/86400) * 86400 - UTC_OFFSET )) # Today at 12 AM local time
    TODAY_START=$(( TODAY_MIDNIGHT + DOWNLOAD_START ))  # Download start time for today
    NEXT_DAY_START=$(( TODAY_START + 86400 ))  # Download start time for tomorrow
    if [ -n "$FIRST_TIME" ]; then
       if (( LAST_DOWNLOAD_TIME < TODAY_MIDNIGHT - 60 || LAST_DB_TIME < TODAY_MIDNIGHT - 900 )); then
	   # Download immediately if no new downloaded data after 11:59 PM yesterday or no valid state after 11:45 PM yesterday
	   sleep_time=0
       else  # Already downloaded yesterday's data, wait until download start tomorrow
	   sleep_time=$(( NEXT_DAY_START - NOW ))
       fi
    elif [ "$LAST_DOWNLOAD_TIME" -ge "$TODAY_MIDNIGHT" ]; then  # Already downloaded data today, wait until download start tomorrow
	sleep_time=$(( NEXT_DAY_START - NOW ))
    elif [ "$NOW" -lt "$TODAY_START" ]; then  # If before today's start, wait until today's start 
	sleep_time=$(( TODAY_START - NOW ))
    else  # Sleep until next DOWNLOAD_FREQUENCY
	time_since_start=$(( NOW - TODAY_START ))
	sleep_time=$(( (time_since_start/DOWNLOAD_FREQUENCY + 1) * DOWNLOAD_FREQUENCY - time_since_start ))
    fi
    log INFO "Next download attempt: $(date -d @$((NOW + sleep_time)) "+%Y-%m-%d %H:%M:%S %Z") [Last download: $(date -d "@$LAST_DOWNLOAD_TIME" "+%Y-%m-%d %H:%M:%S %Z")]"
    sleep $sleep_time

    ## Proceed to download and import
    log INFO "Checking for new energy data to be imported..."

    ## Scrape Eversource site for data
    log INFO "### Running Eversource scraper (PID=$$)"
    /app/eversource_scraper.py $TEST -v -v -s $DAYS_BACK ${ACCOUNT_EVERSOURCE:+-a $ACCOUNT_EVERSOURCE} -f "$ENERGY_FILE"
    status="$?"
    if [ "$status" -eq 0 ]; then  # New data    
	LAST_DOWNLOAD_TIME="$(date +%s)"
    elif [ "$status" -eq 2 ]; then  # No new data
	log INFO "##### OK: No new energy data downloaded (PID=$$)" 
        [ -z "$FIRST_TIME" ] && continue
    elif [ "$status" -ne 0  ]; then  # Error
	log ERROR_ "ERROR importing energy data into Home Assistant DB (PID=$$)"
        [ -z "$FIRST_TIME" ] && continue
    fi
    FIRST_TIME=""

    ## Fill in missing placeholders using DONOR_SENSORS
    log INFO "### Checking for missing HA database placeholders using ($DONOR_SENSORS)..."
    /app/insert_missing_placeholders.py  $TEST -v -v -F "$(( -DAYS_BACK ))" -o "$OFFSET" -e "$ENERGY_SENSOR" -E "$DONOR_SENSORS" 2>&1
    status="$?"
    if [ "$status" -ne 0 ]; then
        log ERROR_ "ERROR checking and filling in missing placeholders (PID=$$)"
	continue
    fi

    ## Import energy data into energy state placholders in Home Assistant DB
    log INFO "### Importing energy data into HA database..."
    /app/import_electric_usage.py $TEST -v -e "$ENERGY_SENSOR" -a -f "$ENERGY_FILE"
    status="$?"
    if [ "$status" -eq 2 ]; then  # No new data
	log INFO "##### OK: No new energy data to import (PID=$$)" 
	continue
    elif [ "$status" -ne 0  ]; then  # Error
	log ERROR_ "ERROR importing energy data into Home Assistant DB (PID=$$)"
	continue
    fi

    ## Redo statistics starting from last prior valid statistic
    log INFO "### Updating statistics tables..."
    /app/redo_sum_statistics.py $TEST -v -v -e "$ENERGY_SENSOR"
    status="$?"
    if [ "$status" -ne 0  ]; then  # Error
	log ERROR_ "ERROR updating statistics tables (PID=$$)"
	continue
    fi

    log INFO "##### SUCCESS: Imported new energy data (PID=$$)" 
done
