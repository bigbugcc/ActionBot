#!/usr/bin/env bash
# Run against each staged platform before publishing public image tags.
set -euo pipefail
image=${1:?Usage: verify-runtime.sh IMAGE PLATFORM}
platform=${2:?Usage: verify-runtime.sh IMAGE PLATFORM}
tmp=$(mktemp -d)
SECONDS=0
stage=run
finish() {
  local status=$? cid=''
  trap - EXIT
  set +e
  if [[ -f "$tmp/cid" ]]; then cid=$(cat "$tmp/cid"); fi
  # Only accept the full ID written by this invocation of docker run.
  if [[ $cid =~ ^[0-9a-f]{64}$ ]]; then
    timeout --kill-after=5s 20s docker rm -f "$cid" >/dev/null 2>&1 ||
      printf 'stage=runtime cleanup=unconfirmed container=%s (may already be removed)\n' "$cid" >&2
  fi
  rm -f -- "$tmp/cid"
  rmdir -- "$tmp"
  printf 'time=%s stage=runtime operation=%s platform=%s duration_s=%s exit_code=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$stage" "$platform" "$SECONDS" "$status" >&2
  exit "$status"
}
trap finish EXIT
printf 'time=%s stage=runtime event=start platform=%s image=%s\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$platform" "$image" >&2

# A fresh, ephemeral database verifies the musl/CGO binary can write settings
# and read them back from a second process. No network or host data is used.
status=0
output=$(timeout --kill-after=10s 120s docker run --rm --cidfile "$tmp/cid" --platform "$platform" \
  --network none --tmpfs /etc/x-ui \
  --env XUI_DB_TYPE=sqlite --env XUI_DB_DSN= \
  --entrypoint /bin/sh "$image" -ec \
  '/app/x-ui setting -port 20531; /app/x-ui setting -show' 2>&1) || status=$?
printf '%s\n' "$output"
if [[ $status != 0 ]]; then exit "$status"; fi
# The upstream CLI can return zero on a DB error; require the persisted value.
stage=sqlite-check
if ! grep -qx 'port: 20531' <<< "$output"; then
  echo 'Runtime check failed: persisted SQLite port was not found' >&2
  exit 1
fi
