#!/usr/bin/env bash
# Keep runner-side diagnostics outside the Docker context and release payload.
set -euo pipefail
stage=${1:?Usage: run-step.sh STAGE COMMAND [ARG...]}
shift
[[ $stage =~ ^[a-z0-9-]+$ && $# -gt 0 ]] || exit 2
dir=${DIAGNOSTICS_DIR:-diagnostics/3x-ui}
mkdir -p "$dir"
log="$dir/$stage.log"
SECONDS=0
finish() {
  local status=$?
  trap - EXIT
  set +e
  local result
  result=$(printf 'time=%s stage=%s event=end duration_s=%s exit_code=%s' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$stage" "$SECONDS" "$status")
  printf '%s\n' "$result" | tee -a "$log"
  if [[ -n ${GITHUB_STEP_SUMMARY:-} ]]; then
    printf '%s\n\n' "$result" >> "$GITHUB_STEP_SUMMARY"
  fi
  if [[ $status != 0 ]]; then printf '::error::%s failed (exit_code=%s); see diagnostic artifact.\n' "$stage" "$status"; fi
  echo '::endgroup::'
  exit "$status"
}
trap finish EXIT
printf '::group::3x-ui %s\n' "$stage"
printf 'time=%s stage=%s event=start\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$stage" | tee "$log"
# PIPESTATUS must be captured before any other command. A tee failure also fails
# the step, but cannot replace a command's existing nonzero exit code.
set +e
"$@" 2>&1 | tee -a "$log"
statuses=("${PIPESTATUS[@]}")
set -e
if [[ ${statuses[0]} != 0 ]]; then exit "${statuses[0]}"; fi
exit "${statuses[1]}"
