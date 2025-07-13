#!/bin/sh
set -euo pipefail

PIDS=""
CONFIG="/etc/warp/config.json"
mkdir -p "$(dirname "$CONFIG")"

# === Default values ===
: "${VERBOSE:=false}"
: "${BIND:=127.0.0.1:1080}"
: "${ENDPOINT:=}"
: "${KEY:=}"
: "${DNS:=1.1.1.1}"
: "${GOOL:=false}"
: "${CFON:=false}"
: "${COUNTRY:=}"
: "${SCAN:=true}"
: "${RTT:=1s}"
: "${CACHE_DIR:=/etc/warp/cache/}"
: "${TUN_EXPERIMENTAL:=false}"
: "${FWMARK:=0x1375}"
: "${WGCONF:=}"
: "${RESERVED:=}"
: "${TEST_URL:=}"
: "${IPV4:=true}"
: "${IPV6:=false}"
: "${EXCLUDE_COUNTRY:=}"
: "${LOGLEVEL:=INFO}"
: "${PIDS:=}"

# === Log function ===
log() {
    level=$1; shift
    level=$(printf '%s' "$level" | tr -d '[:space:]')
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    case "$level" in
        ERROR) current=1 ;;
        WARN*) current=2 ;;
        INFO) current=3 ;;
        DEBUG) current=4 ;;
        *) current=3 ;;
    esac
    case "$LOGLEVEL" in
        ERROR) active=1 ;;
        WARN*) active=2 ;;
        INFO) active=3 ;;
        DEBUG) active=4 ;;
        *) active=3 ;;
    esac
    [ "$current" -gt "$active" ] && return
    case "$level" in
        INFO) color='\033[1;34m' ;;
        WARN*) color='\033[1;33m' ;;
        ERROR) color='\033[1;31m' ;;
        DEBUG) color='\033[1;36m' ;;
        *) color='\033[0m' ;;
    esac
    reset='\033[0m'
    printf "%s %b%s%b - %s\n" "$timestamp" "$color" "$level" "$reset" "$*" >&2
}

add_pid() {
  PIDS="${PIDS:-} $1"
}

kill_all() {
  log WARN "Stopping background processes..."
  for pid in $PIDS; do
    kill "$pid" 2>/dev/null || true
  done
  wait
}

# --- Signal handlers ---
reload_ignore() {
  log INFO "Received SIGUSR1 — ignoring."
}

setup_signal_handlers() {
  trap reload_ignore USR1
  trap 'kill_all; log INFO "All processes stopped."; exit 0' TERM INT QUIT
}

# === Healthcheck function ===
healthcheck_loop() {
  local interval="${HEALTHCHECK_INTERVAL:-300}"
  local timeout="${HEALTHCHECK_TIMEOUT:-30}"
  local max_fails="${HEALTHCHECK_MAX_FAILURES:-3}"
  local url="${HEALTHCHECK_URL:-https://ifconfig.me}"
  local initial_delay="${HEALTHCHECK_INITIAL_DELAY:-60}"

  log INFO "Starting healthcheck via SOCKS5: $url"
  log INFO "Initial delay: ${initial_delay}s, interval: ${interval}s, failure threshold: $max_fails"

  sleep "$initial_delay"

  while :; do
    if ! output=$(curl --socks5 $BIND -sf --max-time "$timeout" "$url"); then
      log ERROR "Healthcheck: Failed to reach $url via SOCKS5. Triggering container restart."
      kill 1
    fi

    fails=$(printf '%s\n' "$output" | grep -o '"fails":[0-9]*' | cut -d: -f2)

    if [ -z "$fails" ]; then
      log DEBUG "Healthcheck: No 'fails' field found. Assuming healthy."
    else
      for count in $fails; do
        if [ "$count" -ge "$max_fails" ]; then
          log ERROR "Healthcheck: Upstream with fails=$count ≥ threshold=$max_fails. Restarting."
          kill 1
        fi
      done
    fi

    log DEBUG "Healthcheck: Healthy (fails: ${fails:-0})"
    sleep "$interval"
  done
}

# === JSON config generation ===
log INFO "Generating warp-plus config..."

add_field() {
  key="$1"
  val="$2"
  type="$3" # string or raw

  [ -n "$val" ] || return 0
  [ "$val" = "null" ] && return 0
  [ "$json" != "{" ] && json="$json,"

  if [ "$type" = "string" ]; then
    json="$json\"$key\":\"$val\""
  else
    json="$json\"$key\":$val"
  fi
}

prepare_config() {

  # === Country selection logic ===
  COUNTRY_LIST="AT BE BG BR CA CH CZ DE DK EE ES FI FR GB HR HU IE IN IT JP LV NL NO PL PT RO RS SE SG SK UA US"

  # Normalize and filter exclusions
  EXCLUDE_LIST=$(echo "$EXCLUDE_COUNTRY" | tr ',;' ' ' | tr '[:lower:]' '[:upper:]' | xargs)

  FILTERED_COUNTRY_LIST=$(for c in $COUNTRY_LIST; do
    echo "$EXCLUDE_LIST" | grep -qw "$c" || echo "$c"
  done)

  if [ -z "$COUNTRY" ]; then
    COUNTRY=$(echo "$FILTERED_COUNTRY_LIST" | shuf -n 1)
    log INFO "COUNTRY not set. Randomly selected: $COUNTRY"
  fi

  json="{"

  add_field "verbose"        "$VERBOSE"        raw
  add_field "bind"           "$BIND"           string
  add_field "endpoint"       "$ENDPOINT"       string
  add_field "key"            "$KEY"            string
  add_field "dns"            "$DNS"            string
  add_field "gool"           "$GOOL"           raw
  add_field "cfon"           "$CFON"           raw
  add_field "country"        "$COUNTRY"        string
  add_field "scan"           "$SCAN"           raw
  add_field "rtt"            "$RTT"            string
  add_field "cache-dir"      "$CACHE_DIR"      string
  add_field "fwmark"         "$FWMARK"         string
  add_field "wgconf"         "$WGCONF"         string
  add_field "reserved"       "$RESERVED"       string
  add_field "test-url"       "$TEST_URL"       string

  if [ "$IPV4" = "true" ] || [ "$IPV4" = "1" ]; then
    add_field "4" true raw
  elif [ "$IPV6" = "true" ] || [ "$IPV6" = "1" ]; then
    add_field "6" true raw
  fi

  json="$json}"

  if ! echo "$json" | jq . > "$CONFIG" 2>/dev/null; then
    log ERROR "Invalid JSON generated:"
    echo "$json" >&2
    exit 1
  fi

  log INFO "Config successfully created:"
  cat "$CONFIG"
}

main() {
  prepare_config
  log INFO "Launching healthcheck background loop..."
  setup_signal_handlers
  healthcheck_loop & add_pid $!

  log INFO "Starting warp-plus..."
  exec /usr/bin/warp-plus -c "$CONFIG"
}

main